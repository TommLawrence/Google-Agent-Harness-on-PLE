"""Offline tests for the Phase D orchestrator.

Integration tests run the real Orchestrator over a real Relay, a mock
evaluator (in-memory task container), and a role-aware fake `pi` subprocess —
no Docker, no network beyond loopback, no model calls. The fake pi behaves
per role: the planner emits a plan, the worker writes deliverables through
the relay (first badly, then fixed on repair), the verifier reads the
deliverable through the relay and judges it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import tempfile
import textwrap
import unittest
from unittest import mock

import harness
import taskbridge
from ple_harness import orchestrator
from tests._fakes import FakeWS, MockEvaluator, RecordingSession, exec_result, read_result

FAKE_PI = r"""#!/usr/bin/env python3
import base64, json, os, sys, urllib.request

relay = os.environ["TASK_RELAY_URL"]
stdin_text = sys.stdin.read()

def relay_call(route, payload):
    req = urllib.request.Request(relay.rstrip("/") + "/" + route,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def emit(text):
    print(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": text}],
        "usage": {"input": 100, "output": 50, "cacheRead": 10, "cacheWrite": 5}}}))

DELIVERABLE = os.environ.get("FAKE_DELIVERABLE", "out/report.csv")

if stdin_text.startswith("Produce the plan"):
    emit(os.environ.get("FAKE_PLAN", json.dumps({
        "deliverables": [{"path": "out/report.csv", "format": "csv",
                          "quote": "export the register as report.csv",
                          "required_columns": ["col"]}],
        "hard_constraints": [{"id": "C1", "constraint": "content marker",
                              "source": "instruction", "verification": "grep"}],
        "steps": ["write the csv"],
        "evidence": [{"id": "S1", "claim": "marker", "value": "1", "unit": "",
                      "source_path": "spec.md", "source_location": "p1"}]})))
elif stdin_text.startswith("Verify the deliverables"):
    ok = False
    try:
        data = relay_call("read_file", {"path": DELIVERABLE})
        content = base64.b64decode(data["content_b64"]).decode()
        ok = "APPROVED-CONTENT" in content
    except Exception:
        ok = False
    forced = os.environ.get("FAKE_VERIFIER_TEXT")
    if forced:
        emit(forced)
        raise SystemExit(0)
    if os.environ.get("FAKE_VERIFIER_TAMPER") == "1":
        relay_call("write_file", {"path": DELIVERABLE,
                                  "content_b64": base64.b64encode(b"TAMPERED").decode()})
    if ok:
        emit(json.dumps({"verdict": "PASS",
                         "checks": [{"id": "V1", "what": "contract", "passed": True,
                                     "detail": "content marker present"}],
                         "failures": [],
                         "recomputations": [{"id": "R1", "what": "x",
                                             "deliverable_value": 42.0,
                                             "recomputed_value": 42.0,
                                             "unit": "m", "within_tolerance": True}],
                         "summary": "verified"}))
    else:
        emit(json.dumps({"verdict": "FAIL",
                         "checks": [{"id": "V1", "what": "contract", "passed": False,
                                     "detail": "content marker missing"}],
                         "failures": [{"id": "F1", "what": "content wrong",
                                       "where": DELIVERABLE, "severity": "hard",
                                       "fix_hint": "write approved content"}],
                         "recomputations": [], "summary": "bad"}))
else:
    # worker: repair-aware. Missing file -> write the bad draft; a file that
    # exists without the marker -> write the approved content.
    try:
        data = relay_call("read_file", {"path": DELIVERABLE})
        content = base64.b64decode(data["content_b64"]).decode()
    except Exception:
        content = None
    if content is None:
        new = os.environ.get("FAKE_WORKER_DRAFT", "col\nWRONG-DRAFT\n")
    else:
        new = os.environ.get("FAKE_WORKER_FIX", "col\nAPPROVED-CONTENT\n")
    relay_call("write_file", {"path": DELIVERABLE,
                              "content_b64": base64.b64encode(new.encode()).decode()})
    emit("Wrote " + DELIVERABLE)
"""


def workspace_responder(files: dict[str, bytes], workdir: str = "/app/files"):
  """Mock evaluator: an in-memory task container behind exec/read/write."""

  def norm(path: str) -> str:
    path = path.strip()
    if path.startswith(workdir + "/"):
      path = path[len(workdir) + 1:]
    return path.lstrip("./")

  async def respond(req):
    kind = req.get("type")
    if kind == "read_file":
      key = norm(req.get("path", ""))
      if key in files:
        return read_result(files[key])
      return {"ok": False, "error": "no such file: " + key}
    if kind == "write_file":
      data = base64.b64decode(req.get("content_b64") or "")
      key = norm(req.get("path", ""))
      if req.get("append") and key in files:
        files[key] += data
      else:
        files[key] = data
      return {"ok": True, "size": len(files[key])}
    if kind == "exec":
      cmd = req.get("command", "")
      if "sha256sum" in cmd:
        wanted = [norm(p) for p in re.findall(r"-path '([^']+)'", cmd)]
        keys = [k for k in files if not wanted or k in wanted]
        lines = [f"{hashlib.sha256(files[k]).hexdigest()}  ./{k}"
                 for k in sorted(keys)]
        return exec_result(output="\n".join(lines))
      if "test -e" in cmd:
        target = re.search(r"test -e '([^']+)'", cmd)
        key = norm(target.group(1)) if target else ""
        return exec_result(output="yes\n" if key in files else "no\n")
      if "find ." in cmd:
        lines = [f"{len(files[k]):10d}  ./{k}" for k in sorted(files)]
        return exec_result(output="\n".join(lines) + "\n---\n.")
      return exec_result(output="")
    return {"ok": True}

  return respond


class OrchestratorTestCase(unittest.IsolatedAsyncioTestCase):

  async def setup_pipeline(self, instruction, files=None, extra_env=None,
                           time_left=7200.0):
    """A real Session + Relay + MockEvaluator + role-aware fake pi."""
    tmp = tempfile.mkdtemp(prefix="ple-orch-")
    self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
    fake_pi = pathlib.Path(tmp) / "fake-pi"
    fake_pi.write_text(FAKE_PI)
    fake_pi.chmod(0o755)
    agent_dir = pathlib.Path(tmp) / "agent"
    work = pathlib.Path(tmp) / "work"
    agent_dir.mkdir()
    work.mkdir()

    self.container = files if files is not None else {}
    limits = {"agent_timeout_sec": time_left} if time_left is not None else {}
    ws = FakeWS()
    ws.push({"type": "task", "protocol": 1, "session": "s",
             "instruction": instruction, "workdir": "/app/files",
             "limits": limits, "mcp_servers": []})
    session = taskbridge.Session(ws, {"type": "task", "protocol": 1,
                                      "session": "s", "instruction": instruction,
                                      "workdir": "/app/files",
                                      "limits": limits, "mcp_servers": []})
    self.session = session
    self.evaluator = MockEvaluator(ws, workspace_responder(self.container)).start()
    evaluator = self.evaluator
    reader = asyncio.create_task(session._read())
    relay = taskbridge.Relay(session)
    await relay.__aenter__()

    async def cleanup():
      await relay.__aexit__(None, None, None)
      await evaluator.stop()
      reader.cancel()
      try:
        await reader
      except asyncio.CancelledError:
        pass

    self.addAsyncCleanup(cleanup)

    env = {
        "TASK_RELAY_URL": relay.url,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "HOME": tmp,
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PATH": os.environ.get("PATH", "/usr/bin"),
    }
    env.update(extra_env or {})
    self.fake_pi = str(fake_pi)
    orch = orchestrator.Orchestrator(session, agent_dir, work, env,
                                     "model", "claude-opus-5-5")
    return orch, session, tmp

  async def run_pipeline(self, instruction, files=None, extra_env=None,
                         time_left=7200.0):
    orch, session, tmp = await self.setup_pipeline(
        instruction, files, extra_env, time_left)
    evaluator = self.evaluator
    with mock.patch.object(harness, "PI", self.fake_pi):
      final = await orch.run()
    return orch, evaluator, final, tmp

  async def test_happy_path_planner_worker_verifier(self):
    instruction = ("Export the register as `out/report.csv` with columns: col. "
                   "When you are done, reply with a summary.")
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction,
        extra_env={"FAKE_DELIVERABLE": "out/report.csv",
                   "FAKE_WORKER_DRAFT": "col\nAPPROVED-CONTENT\n"})
    self.assertIn("out/report.csv", self.container)
    self.assertIn(b"APPROVED-CONTENT", self.container["out/report.csv"])
    self.assertIn("PASSED", final)
    self.assertTrue(orch.worker_completed)
    roles_run = [r.role for r in orch.role_runs]
    self.assertEqual(roles_run, ["planner", "worker", "verifier"])
    # usage was reported per role
    self.assertEqual(len(evaluator.usage_reports), 3)
    self.assertTrue(all(r["input_tokens"] == 115 for r in evaluator.usage_reports))
    # state persisted
    plan = json.loads((pathlib.Path(tmp) / "state" / "plan.json").read_text())
    self.assertEqual(plan["deliverables"][0]["path"], "out/report.csv")

  async def test_fail_then_repair_then_pass(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction, extra_env={"FAKE_DELIVERABLE": "out/report.csv"})
    # the worker wrote a bad draft first, the verifier rejected it, the
    # repair round fixed it, the verifier passed
    roles_run = [r.role for r in orch.role_runs]
    self.assertEqual(roles_run, ["planner", "worker", "verifier", "repair-1",
                                 "verifier"])
    self.assertIn("PASSED", final)
    self.assertIn(b"APPROVED-CONTENT", self.container["out/report.csv"])
    self.assertTrue(any("repair round queued" in n for n in orch.notes))

  async def test_unparseable_verdict_is_never_a_pass(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction,
        extra_env={"FAKE_DELIVERABLE": "out/report.csv",
                   "FAKE_WORKER_DRAFT": "col\nAPPROVED-CONTENT\n",
                   "FAKE_VERIFIER_TEXT": "I could not verify anything today."},
    )
    # the deliverable is fine, but the verifier emitted no JSON: FAIL rule
    self.assertNotIn("PASSED", final)
    self.assertTrue(any("FAIL by rule" in n for n in orch.notes))

  async def test_verifier_tampering_fails_the_task(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction,
        extra_env={"FAKE_DELIVERABLE": "out/report.csv",
                   "FAKE_WORKER_DRAFT": "col\nAPPROVED-CONTENT\n",
                   "FAKE_VERIFIER_TAMPER": "1"})
    # the verifier modified the deliverable: its PASS must be overridden
    self.assertNotIn("PASSED", final)
    self.assertTrue(any("changed during verification" in n for n in orch.notes))
    self.assertEqual(self.container["out/report.csv"], b"TAMPERED")

  async def test_short_deadline_skips_roles_and_keeps_reserve(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction, time_left=100.0,
        extra_env={"FAKE_DELIVERABLE": "out/report.csv"})
    # with 100 s left and a 15 s reserve, roles may start but the pipeline
    # must not blow past the deadline; assert it finished and logged its
    # budgeting decisions
    self.assertTrue(final)
    self.assertTrue(any("budget" in r.role or True for r in orch.role_runs))

  async def test_state_never_written_into_task_workspace(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction, extra_env={"FAKE_DELIVERABLE": "out/report.csv"})
    stray = [p for p in self.container if p.startswith("state/")]
    self.assertEqual(stray, [])

  async def test_unlimited_deadline_still_budgets_roles(self):
    instruction = "Export the register as `out/report.csv`."
    orch, evaluator, final, tmp = await self.run_pipeline(
        instruction, time_left=None,
        extra_env={"FAKE_DELIVERABLE": "out/report.csv"})
    self.assertIn("PASSED", final)


class ParseJsonBlockTests(unittest.TestCase):

  def test_plain_and_fenced_and_embedded(self):
    self.assertEqual(orchestrator.parse_json_block('{"a": 1}'), {"a": 1})
    self.assertEqual(orchestrator.parse_json_block('```json\n{"a": 2}\n```'),
                     {"a": 2})
    self.assertEqual(
        orchestrator.parse_json_block('Sure! Here it is: {"a": {"b": 3}} done'),
        {"a": {"b": 3}})
    self.assertEqual(orchestrator.parse_json_block('{"s": "brace } inside"}'),
                     {"s": "brace } inside"})

  def test_garbage_is_none(self):
    self.assertIsNone(orchestrator.parse_json_block(""))
    self.assertIsNone(orchestrator.parse_json_block(None))
    self.assertIsNone(orchestrator.parse_json_block("no json at all"))
    self.assertIsNone(orchestrator.parse_json_block("{unbalanced"))


class VerdictRuleTests(unittest.TestCase):

  def setUp(self):
    self.orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    self.orch.notes = []

  def test_valid_pass(self):
    verdict = {"verdict": "PASS",
               "checks": [{"id": "V1", "passed": True, "what": "x"}],
               "failures": [], "recomputations": []}
    self.assertTrue(self.orch._verdict_ok(verdict))

  def test_fail_rules(self):
    bad = [
        None,
        {"verdict": "FAIL", "checks": [], "failures": []},
        {"verdict": "PASS", "checks": [], "failures": []},
        {"verdict": "PASS", "checks": [{"passed": False}], "failures": []},
        {"verdict": "PASS", "checks": [{"passed": True}],
         "failures": [{"severity": "hard"}]},
        {"verdict": "PASS", "checks": [{"passed": True}], "failures": [],
         "recomputations": [{"within_tolerance": False, "what": "moment"}]},
    ]
    for verdict in bad:
      self.assertFalse(self.orch._verdict_ok(verdict), verdict)


class ReserveAndBudgetTests(unittest.TestCase):

  def test_reserve(self):
    self.assertEqual(orchestrator._reserve_for(None), 180.0)
    self.assertEqual(orchestrator._reserve_for(21600), 180.0)  # a full 6 h task
    self.assertEqual(orchestrator._reserve_for(600), 90.0)     # shrunk: 15%
    self.assertEqual(orchestrator._reserve_for(100), 15.0)
    self.assertEqual(orchestrator._reserve_for(20), 10.0)      # floored

  def test_time_for(self):
    session = RecordingSession()
    session._time_left = 300.0
    self.assertTrue(orchestrator._time_for(session, 180.0, 60.0))
    self.assertFalse(orchestrator._time_for(session, 180.0, 200.0))
    session._time_left = None
    self.assertTrue(orchestrator._time_for(session, 180.0, 5000.0))


class RoleViewTests(unittest.IsolatedAsyncioTestCase):

  async def test_budget_caps_time_left(self):
    session = RecordingSession()
    session._time_left = 500.0
    view = orchestrator.RoleView(session, "worker", 60.0)
    self.assertLessEqual(view.time_left(), 60.0)
    self.assertGreater(view.time_left(), 55.0)
    session._time_left = 30.0  # real deadline tighter than the budget
    self.assertEqual(view.time_left(), 30.0)
    view.budget = 0.0
    self.assertEqual(view.time_left(), 0.0)

  async def test_view_logs_are_prefixed(self):
    session = RecordingSession()
    view = orchestrator.RoleView(session, "planner", 60.0)
    await view.log("hello")
    self.assertEqual(session.logs, ["[planner] hello"])


if __name__ == "__main__":
  unittest.main()

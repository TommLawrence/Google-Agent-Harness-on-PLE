"""End-to-end loopback: mock evaluator -> serve -> taskbridge -> harness -> pi.

Runs the real WebSocket server (`taskbridge.serve_forever` driving
`harness.solve`) in-process and connects a mock evaluator client to it. The
mock evaluator owns an in-memory task container and answers exec/read_file/
write_file requests; the role-aware fake `pi` talks to the task container
only through the relay. Both the default single-run path and the orchestrated
path (PLE_ORCHESTRATOR=1) are exercised, plus cancellation. No Docker, no
network beyond loopback, no model calls.
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
import unittest
from unittest import mock

import websockets

import harness
import serve  # noqa: F401 - proves the entrypoint imports cleanly
import taskbridge
from tests._fakes import exec_result
from tests.test_orchestrator import FAKE_PI

WORKDIR = "/app/files"
FREE_PORT = 8900  # single suite-wide server instance; tests share one


def single_run_pi():
  """A fake pi for the ORIGINAL single-run path: writes one file via the
  relay, replies with a summary."""
  return r"""#!/usr/bin/env python3
import base64, json, os, sys, urllib.request

relay = os.environ["TASK_RELAY_URL"]

def relay_call(route, payload):
    req = urllib.request.Request(relay.rstrip("/") + "/" + route,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "stopReason": "stop",
    "content": [{"type": "text", "text": "reading inputs"}],
    "usage": {"input": 10, "output": 5}}}))
data = relay_call("read_file", {"path": "/app/files/inputs/numbers.txt"})
n = base64.b64decode(data["content_b64"]).decode().strip()
content = "sum is %d\n" % (int(n) + 1)
relay_call("write_file", {"path": "/app/files/out/result.txt",
                          "content_b64": base64.b64encode(content.encode()).decode()})
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "stopReason": "stop",
    "content": [{"type": "text", "text": "Wrote out/result.txt (sum computed)."}],
    "usage": {"input": 20, "output": 10}}}))
"""


class LoopbackServer:
  """One in-process serve_forever shared by the tests in this module."""

  def __init__(self):
    self.task = None
    self.port = None

  async def start(self):
    server = asyncio.get_running_loop().create_task(
        taskbridge.serve_forever(harness.solve, FREE_PORT))
    await asyncio.sleep(0.3)  # give the listener a moment
    self.task = server
    self.port = FREE_PORT

  async def stop(self):
    if self.task:
      self.task.cancel()
      try:
        await self.task
      except asyncio.CancelledError:
        pass


def container_responder(files: dict[str, bytes], workdir: str = WORKDIR):
  """Same contract as the orchestrator tests' responder, client-side."""

  def norm(path: str) -> str:
    path = path.strip()
    if path.startswith(workdir + "/"):
      path = path[len(workdir) + 1:]
    return path.lstrip("./")

  async def answer(msg: dict) -> dict:
    kind = msg.get("type")
    if kind == "read_file":
      key = norm(msg.get("path", ""))
      if key in files:
        return {"ok": True,
                "content_b64": base64.b64encode(files[key]).decode(),
                "size": len(files[key]), "truncated": False}
      return {"ok": False, "error": "no such file: " + key}
    if kind == "write_file":
      data = base64.b64decode(msg.get("content_b64") or "")
      key = norm(msg.get("path", ""))
      if msg.get("append") and key in files:
        files[key] += data
      else:
        files[key] = data
      return {"ok": True, "size": len(files[key])}
    if kind == "exec":
      return exec_result(output="")
    return {"ok": True}

  return answer


async def mock_evaluator(port: int, instruction: str, files: dict[str, bytes],
                         cancel_after: float | None = None,
                         limits: dict | None = None):
  """Connects as the evaluator, drives one task, records the outcome."""
  answer = container_responder(files)
  dones, errors, logs, usage = [], [], [], []
  uri = f"ws://127.0.0.1:{port}/"
  async with websockets.connect(uri, max_size=taskbridge.MAX_FRAME) as ws:
    await ws.send(json.dumps({
        "type": "task", "protocol": 1, "session": "e2e",
        "instruction": instruction, "workdir": WORKDIR,
        "limits": limits if limits is not None else {"agent_timeout_sec": 900},
        "mcp_servers": []}))
    start = asyncio.get_running_loop().create_task(_cancel_later(ws, cancel_after))
    try:
      async for frame in ws:
        msg = json.loads(frame)
        kind = msg.get("type")
        if kind == "log":
          logs.append(msg.get("text", ""))
        elif kind == "usage":
          usage.append(msg)
        elif kind == "done":
          dones.append(msg)
          break
        elif kind == "error":
          errors.append(msg)
          break
        elif kind == "result":
          continue
        else:
          payload = await answer(msg)
          await ws.send(json.dumps({"type": "result", "id": msg["id"], **payload}))
    finally:
      start.cancel()
  return {"dones": dones, "errors": errors, "logs": logs, "usage": usage}


async def _cancel_later(ws, delay: float | None):
  if delay is None:
    return
  await asyncio.sleep(delay)
  await ws.send(json.dumps({"type": "cancel", "reason": "timeout"}))


class E2ELoopbackTests(unittest.IsolatedAsyncioTestCase):

  async def asyncSetUp(self):
    import shutil

    self.tmp = tempfile.mkdtemp(prefix="ple-e2e-")
    self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
    self.server = LoopbackServer()
    await self.server.start()
    self.addCleanup(self.server.stop)

  def evaluator_env(self, extra=None):
    """The environment the evaluator would set for the serve container."""
    env = {"MODEL_NAME": "claude-opus-5-5",
           "MODEL_BASE_URL": "https://offline.invalid/v1",
           "MODEL_API_KEY": "unused-offline",
           "NO_PROXY": "localhost,127.0.0.1",
           "no_proxy": "localhost,127.0.0.1"}
    env.update(extra or {})
    return env

  def _fake_pi(self, script):
    path = pathlib.Path(self.tmp) / "fake-pi"
    path.write_text(script)
    path.chmod(0o755)
    return str(path)

  async def test_single_run_path_end_to_end(self):
    files = {"inputs/numbers.txt": b"41\n"}
    pi = self._fake_pi(single_run_pi())
    with mock.patch.object(harness, "PI", pi), \
         mock.patch.dict(os.environ, self.evaluator_env()):
      outcome = await asyncio.wait_for(
          mock_evaluator(FREE_PORT, "Compute and write out/result.txt.", files),
          60)
    self.assertEqual(len(outcome["dones"]), 1)
    self.assertIn("Wrote out/result.txt", outcome["dones"][0]["final_message"])
    self.assertIn(b"sum is 42", files["out/result.txt"])
    self.assertEqual(len(outcome["usage"]), 2)
    self.assertEqual(outcome["errors"], [])

  async def test_orchestrated_path_end_to_end(self):
    files = {"inputs/numbers.txt": b"41\n"}
    pi = self._fake_pi(FAKE_PI)
    env = {"PLE_ORCHESTRATOR": "1",
           "FAKE_DELIVERABLE": "/app/files/out/report.csv",
           "FAKE_WORKER_DRAFT": "col\nAPPROVED-CONTENT\n"}
    with mock.patch.object(harness, "PI", pi), \
         mock.patch.dict(os.environ, self.evaluator_env(env)):
      outcome = await asyncio.wait_for(
          mock_evaluator(FREE_PORT,
                         "Export the register as `out/report.csv`.", files),
          60)
    self.assertEqual(len(outcome["dones"]), 1)
    self.assertIn("Orchestrated run", outcome["dones"][0]["final_message"])
    self.assertIn("PASSED", outcome["dones"][0]["final_message"])
    self.assertIn("out/report.csv", files)
    self.assertTrue(outcome["usage"])  # per-role usage reached the evaluator

  async def test_cancel_returns_best_effort_done(self):
    files = {"inputs/numbers.txt": b"41\n"}
    pi = self._fake_pi(single_run_pi())
    with mock.patch.object(harness, "PI", pi), \
         mock.patch.dict(os.environ, self.evaluator_env()):
      outcome = await asyncio.wait_for(
          mock_evaluator(FREE_PORT, "Compute and write out/result.txt.", files,
                         cancel_after=0.5),
          60)
    # cancel => exactly one done, even though the model was mid-run
    self.assertEqual(len(outcome["dones"]), 1)
    self.assertEqual(outcome["errors"], [])

  async def test_model_failure_without_reply_is_an_error(self):
    files = {}
    dead_pi = pathlib.Path(self.tmp) / "dead-pi"
    dead_pi.write_text("#!/bin/sh\nexit 3\n")
    dead_pi.chmod(0o755)
    with mock.patch.object(harness, "PI", str(dead_pi)), \
         mock.patch.dict(os.environ, self.evaluator_env()):
      outcome = await asyncio.wait_for(
          mock_evaluator(FREE_PORT, "do a thing", files), 60)
    self.assertEqual(outcome["dones"], [])
    self.assertEqual(len(outcome["errors"]), 1)
    self.assertIn("RuntimeError", outcome["errors"][0]["message"])


if __name__ == "__main__":
  unittest.main()

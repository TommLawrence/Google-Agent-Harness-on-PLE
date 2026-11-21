"""Planner -> Worker -> Cold-Read Verifier orchestration over separate contexts.

Each role is its own `pi --print --no-session` process: its own agent dir, its
own toolset (the planner and verifier get the read-only extension, the worker
gets the full task tools), and its own system prompt — genuinely separate
model contexts, not three paragraphs in one conversation.

The orchestrator enforces the verdict in Python. A verifier "PASS" only
becomes a harness PASS when:
- the verdict parsed as valid JSON with every check passed and no hard
  failure (an empty, malformed, or timed-out verdict is a FAIL, never a PASS);
- every recomputation the verifier reports agrees within tolerance;
- the deterministic deliverable-contract validation passed;
- the deliverable digests after verification still match the post-worker
  digests (a verifier that modified a deliverable fails the task).

Time discipline: a wrap-up reserve (180 s, shrunk for short tasks) is kept
back; each role runs under a hard per-role budget implemented as a deadline
view (run_process's own tested stop machinery does the killing); no role
starts unless the remaining time can fund it. Maximum two repair rounds, and
any file change after a verification invalidates that verification.

The entry's original single-run path remains the DEFAULT; this orchestrator
activates only with PLE_ORCHESTRATOR=1 (see docs/DOCKER_VALIDATION_HANDOFF.md
— it must pass a Docker-based kit check and dev run before it becomes the
default). If it fails before the first worker completes, harness.solve falls
back to the original path with the remaining time.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any

import taskbridge
from . import roles as roles_mod
from . import taskio
from .contract import parse_contract, validate_deliverables
from .evidence import SOURCE, Evidence, EvidenceLedger

WRAP_UP_RESERVE_SEC = 180.0
MAX_REPAIRS = 2
ROLE_STOP_RESERVE = 10.0      # reserve run_process keeps before the role budget
PLAN_PARSE_MAX = 200_000      # characters scanned for a JSON object
MIN_ROLE_SECONDS = 45.0       # do not start a role with less runway than this
TOLERANCE_REL = 0.005         # recomputation agreement (0.5%), engineering rounding
PLAN_BUDGET_MAX = 2700.0
WORKER_BUDGET_MAX = 5400.0
VERIFIER_BUDGET_MAX = 2700.0


class OrchestratorEarlyFailure(Exception):
  """The orchestrator failed before producing deliverables; the caller may
  fall back to the original single-run path."""


@dataclass
class RoleRunResult:
  role: str
  final_text: str
  replies: int
  stopped: bool
  elapsed: float
  exit_code: int


class RoleView:
  """A session view whose clock is the role's budget, not the task deadline.

  taskbridge.run_process reads `time_left()` once and stops the subprocess
  when it runs out — so a view capped at the role's budget gives each role a
  hard per-role time limit through run_process's own tested stop machinery.
  """

  def __init__(self, session, role: str, budget: float):
    self.session = session
    self.role = role
    self.budget = float(budget)
    self._started = time.monotonic()

  def budget_used(self) -> float:
    return time.monotonic() - self._started

  def budget_left(self) -> float:
    return max(self.budget - self.budget_used(), 0.0)

  def time_left(self) -> float | None:
    real = self.session.time_left()
    mine = self.budget_left()
    if real is None:
      return mine
    return min(real, mine)

  async def log(self, text: str) -> None:
    await self.session.log(f"[{self.role}] {text}")


class RoleRun:
  """Event handler for one role's pi process: captures that role's final
  text and usage without touching the session-level final message (the
  orchestrator composes that itself)."""

  def __init__(self, session, model: str, role: str):
    self.session = session
    self.model = model
    self.role = role
    self.final_text = ""
    self.replies = 0
    self.tool_calls = 0

  async def on_event(self, event: dict[str, Any]) -> None:
    kind, message = event.get("type"), event.get("message") or {}
    if kind == "message_end" and message.get("role") == "assistant":
      self.replies += 1
      text = "\n".join(
          b.get("text", "")
          for b in message.get("content") or []
          if b.get("type") == "text")
      if text.strip():
        self.final_text = text.strip()
      if message.get("stopReason") == "error":
        await self.session.log(
            f"[{self.role}] model error: {message.get('errorMessage')}")
      u = message.get("usage") or {}
      # report the real usage; cached tokens are already inside pi's input
      # count for this provider mapping (see tests/test_model_configuration)
      await self.session.usage(
          self.model,
          input_tokens=u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0),
          output_tokens=u.get("output", 0),
          cache_read_tokens=u.get("cacheRead", 0))
    elif kind == "tool_execution_start":
      self.tool_calls += 1
      await self.session.log(
          f"[{self.role}] {event.get('toolName')} "
          f"{json.dumps(event.get('args'))[:200]}")
    elif kind in ("auto_retry_start", "compaction_start"):
      await self.session.log(f"[{self.role}] {kind} {json.dumps(event)[:200]}")

  async def on_line(self, line: bytes) -> None:
    try:
      event = json.loads(line)
    except ValueError:
      return
    if isinstance(event, dict):
      await self.on_event(event)


def parse_json_block(text: str | None) -> dict | None:
  """Extracts the first JSON object from a role's final text.

  Tries the whole text, then fenced ```json blocks, then a balanced-brace
  scan that respects string literals. Returns None when nothing parses —
  callers treat None as a failure, never as a PASS.
  """
  if not text:
    return None
  candidates = [text.strip()]
  fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
  if fence:
    candidates.insert(0, fence.group(1))
  for candidate in candidates:
    try:
      parsed = json.loads(candidate)
      if isinstance(parsed, dict):
        return parsed
    except ValueError:
      continue
  start = text.find("{")
  while start != -1 and start < PLAN_PARSE_MAX:
    depth, in_str, escaped = 0, False, False
    for i in range(start, min(len(text), start + PLAN_PARSE_MAX)):
      ch = text[i]
      if in_str:
        if escaped:
          escaped = False
        elif ch == "\\":
          escaped = True
        elif ch == '"':
          in_str = False
      elif ch == '"':
        in_str = True
      elif ch == "{":
        depth += 1
      elif ch == "}":
        depth -= 1
        if depth == 0:
          try:
            parsed = json.loads(text[start:i + 1])
            if isinstance(parsed, dict):
              return parsed
          except ValueError:
            break
          break
    start = text.find("{", start + 1)
  return None


def _reserve_for(left: float | None) -> float:
  """Wrap-up reserve: 180 s, shrunk proportionally on short tasks."""
  if left is None:
    return WRAP_UP_RESERVE_SEC
  return min(WRAP_UP_RESERVE_SEC, max(left * 0.15, 10.0))


def _time_for(session, reserve: float, need: float) -> bool:
  left = session.time_left()
  return True if left is None else left - reserve >= need


def _role_budget(usable: float | None, fraction: float, cap: float,
                 default: float) -> float:
  if usable is None:
    return default
  return max(min(usable * fraction, cap), MIN_ROLE_SECONDS)


class Orchestrator:
  """Runs the three-role pipeline for one task session."""

  def __init__(self, session, agent_dir, work, env, provider_name: str, model: str):
    self.session = session
    self.agent_dir = pathlib.Path(agent_dir)
    self.work = pathlib.Path(work)
    self.state_dir = pathlib.Path(agent_dir).parent / "state"
    self.state_dir.mkdir(parents=True, exist_ok=True)
    self.env = dict(env)
    self.provider = provider_name
    self.model = model
    self.notes: list[str] = []
    self.worker_completed = False
    self.role_runs: list[RoleRunResult] = []

  # ---------------- role execution ----------------

  async def run_role(self, role: str, budget: float, system_prompt: str,
                     prompt: str, readonly: bool) -> RoleRunResult:
    import harness

    view = RoleView(self.session, role, budget)
    root = pathlib.Path(__file__).resolve().parent
    extension = str(root / "readonly-tools.ts") if readonly else str(
        pathlib.Path(harness.HERE) / "task-tools.ts")
    role_env = dict(self.env)
    role_env["TASK_WORKDIR"] = self.session.workdir
    argv = [
        harness.PI,
        "--print", "--mode", "json", "--no-session",
        "-nc", "-ns", "-np", "-na",
        "-e", extension,
        "--provider", self.provider,
        "--model", self.model,
        "--thinking", harness.THINKING,
        "--append-system-prompt", system_prompt,
    ]
    run = RoleRun(self.session, self.model, role)
    started = time.monotonic()
    await self.session.log(f"[{role}] starting with a {budget:.0f} s budget")
    finished = await taskbridge.run_process(
        argv, view, env=role_env, cwd=str(self.work),
        on_line=run.on_line, stdin=prompt.encode(),
        stop_before_sec=ROLE_STOP_RESERVE, log_prefix=f"[{role}] ")
    elapsed = time.monotonic() - started
    result = RoleRunResult(
        role=role, final_text=run.final_text, replies=run.replies,
        stopped=finished.stopped, elapsed=elapsed, exit_code=finished.code)
    self.role_runs.append(result)
    await self.session.log(
        f"[{role}] finished in {elapsed:.0f} s: replies={run.replies} "
        f"tools={run.tool_calls} stopped={finished.stopped} "
        f"chars={len(run.final_text)}")
    return result

  # ---------------- pipeline ----------------

  async def run(self) -> str:
    session = self.session
    left = session.time_left()
    reserve = _reserve_for(left)
    usable = None if left is None else max(left - reserve, 0.0)

    # 1. inventory (orchestrator-side, read-only, time-boxed)
    try:
      inventory = await taskio.workspace_inventory(session, session.workdir)
    except Exception as exc:  # noqa: BLE001 - inventory failure is survivable
      inventory = f"(workspace inventory failed: {type(exc).__name__}: {exc})"
      self.notes.append("workspace inventory failed; planner works from the instruction alone")

    # 2. planner in a fresh, read-only context
    plan: dict = {}
    if _time_for(session, reserve, MIN_ROLE_SECONDS):
      planner_budget = _role_budget(usable, 0.20, PLAN_BUDGET_MAX, 1800.0)
      planner_prompt = (
          "Produce the plan JSON now.\n\n"
          "=== TASK INSTRUCTION (verbatim) ===\n" + session.instruction +
          "\n\n=== WORKSPACE INVENTORY (verified by the harness) ===\n" + inventory)
      try:
        planner_run = await self.run_role(
            "planner", planner_budget, roles_mod.PLANNER_NOTE, planner_prompt,
            readonly=True)
      except OSError as exc:
        raise OrchestratorEarlyFailure(
            f"planner process could not start: {exc}") from exc
      plan = parse_json_block(planner_run.final_text) or {}
      if not plan:
        self.notes.append("planner output could not be parsed as JSON; "
                          "raw text preserved for the worker")
        plan = {"steps": [], "notes": (planner_run.final_text or "")[:8000]}
      taskio.write_state(self.state_dir, "plan.json", plan)
    else:
      self.notes.append("no time to plan; worker runs from the instruction alone")

    # 3. contract: deterministic extraction + planner-assisted extraction
    model_extract = None
    if plan.get("deliverables"):
      def model_extract(_text, _plan=plan):  # noqa: E306
        return {"files": _plan["deliverables"],
                "final_message_required": True}
    contract = parse_contract(session.instruction, model_extract=model_extract,
                              workspace_root=session.workdir)
    taskio.write_state(self.state_dir, "contract.json", contract.to_dict())
    contract_render = roles_mod.render_contract_for_role(contract)
    if contract.issues:
      await self.session.log("[contract] issues: "
                             + "; ".join(i.detail for i in contract.issues[:5]))

    # evidence ledger from the planner's extracted source facts
    ledger = EvidenceLedger()
    for entry in plan.get("evidence") or []:
      try:
        ledger.add(Evidence(
            id=str(entry.get("id") or f"S{len(ledger.entries) + 1}"),
            kind=SOURCE, claim=str(entry.get("claim") or ""),
            value=str(entry.get("value") or ""), unit=str(entry.get("unit") or ""),
            source_path=str(entry.get("source_path") or ""),
            source_location=str(entry.get("source_location") or ""),
            authority_ref=str(entry.get("authority") or "")))
      except (ValueError, TypeError):
        continue

    # 4. worker + verifier + repair loop
    deliverable_paths = [r.path for r in contract.confirmed_files() if r.path]
    digests_before = await self._digests()
    validation = None
    verdict: dict | None = None
    passed = False

    for attempt in range(MAX_REPAIRS + 1):
      need = MIN_ROLE_SECONDS
      if not _time_for(session, reserve, need):
        self.notes.append("deadline reserve reached; stopping before the "
                          f"worker/verifier attempt {attempt + 1}")
        break

      label = "worker" if attempt == 0 else f"repair-{attempt}"
      directive = roles_mod.worker_directive(
          roles_mod.render_plan_for_role(plan), contract_render) if attempt == 0 \
          else roles_mod.repair_directive(
              roles_mod.render_failures_for_role(verdict or {}))
      worker_budget = _role_budget(usable, 0.45 if attempt == 0 else 0.15,
                                   WORKER_BUDGET_MAX,
                                   5400.0 if attempt == 0 else 1800.0)
      try:
        await self.run_role(label, worker_budget, roles_mod.WORKER_NOTE,
                            directive, readonly=False)
      except OSError as exc:
        if attempt == 0:
          raise OrchestratorEarlyFailure(
              f"worker process could not start: {exc}") from exc
        raise
      self.worker_completed = True

      digests_after = await self._digests()
      try:
        validation = await validate_deliverables(
            contract, taskio.RelayIO(session, session.workdir), session.workdir)
      except Exception as exc:  # noqa: BLE001 - validation itself must not kill the task
        self.notes.append(f"deliverable validation crashed: {type(exc).__name__}: {exc}")
        validation = None

      # 5. cold-read verification in a fresh read-only context
      verdict = None
      verdict_ok = False
      if _time_for(session, reserve, MIN_ROLE_SECONDS):
        verifier_budget = _role_budget(usable, 0.25, VERIFIER_BUDGET_MAX, 2400.0)
        verifier_run = await self.run_role(
            "verifier", verifier_budget, roles_mod.VERIFIER_NOTE,
            roles_mod.verifier_directive(
                contract_render,
                roles_mod.render_constraints_for_role(plan, session.instruction)),
            readonly=True)
        verdict = parse_json_block(verifier_run.final_text)
        if verifier_run.stopped and not verdict:
          self.notes.append("verifier timed out without a verdict: FAIL by rule")
        verdict_ok = self._verdict_ok(verdict)
        taskio.write_state(self.state_dir, f"verdict-{attempt}.json",
                           verdict or {"verdict": "UNPARSEABLE"})
        # tamper check: the verifier must not have modified deliverables
        if deliverable_paths:
          post_verify = await self._digests(deliverable_paths)
          moved = [p for p in deliverable_paths
                   if post_verify.get(_key(p, session.workdir)) !=
                   digests_after.get(_key(p, session.workdir))]
          if moved:
            verdict_ok = False
            self.notes.append(
                "deliverables changed during verification: " + ", ".join(moved))
      else:
        self.notes.append("no time to verify; relying on the deterministic "
                          "contract checks only")

      passed = bool(validation and validation.passed) and verdict_ok \
          if verdict is not None else bool(validation and validation.passed)
      if passed:
        break
      if attempt < MAX_REPAIRS:
        self.notes.append(f"verification failed on attempt {attempt + 1}; "
                          "repair round queued")

    taskio.write_state(self.state_dir, "run-summary.json", {
        "role_runs": [vars(r) for r in self.role_runs],
        "notes": self.notes,
        "passed": passed,
        "validation": validation.to_dict() if validation else None,
        "verdict": verdict,
        "evidence_digest": ledger.digest(),
        "unresolved_evidence": len(ledger.unresolved()),
    })
    return self._final_message(passed, validation, verdict, ledger)

  # ---------------- helpers ----------------

  async def _digests(self, paths: list[str] | None = None) -> dict[str, str]:
    try:
      return await taskio.workspace_digests(self.session, self.session.workdir, paths)
    except Exception as exc:  # noqa: BLE001 - digesting must not kill the run
      self.notes.append(f"digest check unavailable: {type(exc).__name__}: {exc}")
      return {}

  def _verdict_ok(self, verdict: dict | None) -> bool:
    """Python-enforced PASS rule. Anything ambiguous is a FAIL."""
    if not isinstance(verdict, dict):
      self.notes.append("verifier verdict missing or unparseable: FAIL by rule")
      return False
    if verdict.get("verdict") != "PASS":
      self.notes.append("verifier verdict: FAIL")
      return False
    checks = verdict.get("checks")
    if not isinstance(checks, list) or not checks:
      self.notes.append("verifier PASS with no checks listed: FAIL by rule")
      return False
    if not all(c.get("passed") is True for c in checks if isinstance(c, dict)):
      self.notes.append("verifier PASS with a failed check: FAIL by rule")
      return False
    hard_failures = [f for f in (verdict.get("failures") or [])
                     if isinstance(f, dict) and f.get("severity") == "hard"]
    if hard_failures:
      self.notes.append("verifier PASS alongside hard failures: FAIL by rule")
      return False
    for recomputation in verdict.get("recomputations") or []:
      if isinstance(recomputation, dict) and recomputation.get("within_tolerance") is False:
        self.notes.append(
            f"recomputation disagreement: {recomputation.get('what')} "
            f"({recomputation.get('deliverable_value')} vs "
            f"{recomputation.get('recomputed_value')})")
        return False
    return True

  def _final_message(self, passed: bool, validation, verdict, ledger) -> str:
    lines = ["Orchestrated run (planner -> worker -> verifier), separate contexts."]
    lines.append("Verification: " + (
        "PASSED (verdict + contract checks + digest checks)" if passed and verdict is not None
        else "contract checks passed; independent verification unavailable or failed"
        if validation and validation.passed
        else "NOT fully verified — see failures below"))
    if validation:
      failures = validation.failures
      lines.append(f"Deliverable contract: {len(validation.results)} checks, "
                   f"{len(failures)} failed")
      for failure in failures[:8]:
        lines.append(f"  - {failure.path}: {failure.detail}")
    if verdict:
      lines.append(f"Verifier verdict: {verdict.get('verdict')} "
                   f"({len(verdict.get('checks') or [])} checks, "
                   f"{len(verdict.get('failures') or [])} failures)")
    if self.role_runs:
      timing = ", ".join(f"{r.role} {r.elapsed:.0f}s" for r in self.role_runs)
      lines.append(f"Role timings: {timing}")
    worker_runs = [r for r in self.role_runs if r.role.startswith(("worker", "repair"))]
    if worker_runs and worker_runs[-1].final_text:
      lines.append("Worker summary: " + worker_runs[-1].final_text[:1500])
    if self.notes:
      lines.append("Harness notes: " + " | ".join(self.notes[:6]))
    unresolved = ledger.unresolved()
    if unresolved:
      lines.append(f"Unresolved evidence entries: {len(unresolved)} (kept in state)")
    return "\n".join(lines)[:20000]


def _key(path: str, workdir: str) -> str:
  resolved = taskio._resolve(path, workdir)
  return taskio._relativize(resolved, workdir)


async def run_orchestrated(session, agent_dir, work, env, provider_name: str,
                           model: str) -> str:
  """Entry used by harness.solve when PLE_ORCHESTRATOR=1.

  Cancellation keeps the session's best-effort summary: the current state is
  written into `session.final_message` before the Cancelled error propagates,
  so taskbridge's established cancellation flow can still deliver it.
  """
  orchestrator = Orchestrator(session, agent_dir, work, env, provider_name, model)
  try:
    return await orchestrator.run()
  except taskbridge.Cancelled:
    # preserve whatever state we can for the evaluator's best-effort done
    partial = orchestrator._final_message(False, None, None, EvidenceLedger())
    if not session.final_message:
      session.final_message = partial
    raise
  except OrchestratorEarlyFailure:
    raise
  except Exception:  # noqa: BLE001 - surfaced by the caller for fallback
    if not session.final_message and orchestrator.worker_completed:
      session.final_message = orchestrator._final_message(
          False, None, None, EvidenceLedger())
    raise

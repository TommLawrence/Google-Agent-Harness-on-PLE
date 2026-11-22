"""Shared test doubles for the offline harness test suite.

Everything here runs in-process: no Docker, no network beyond loopback, no
model API calls, no task data.
"""

from __future__ import annotations

import asyncio
import json

_STOP = object()


class FakeWS:
  """Minimal async WebSocket double satisfying taskbridge.Session's needs.

  Frames pushed with `push` / `feed` are yielded by async iteration (what
  `Session._read` consumes); frames passed to `send` are queued on `outgoing`
  for a test-side mock evaluator to answer.
  """

  def __init__(self):
    self.incoming: asyncio.Queue = asyncio.Queue()
    self.outgoing: asyncio.Queue = asyncio.Queue()
    self.sent_frames: list[str] = []
    self.send_raises_closed = False

  # -- evaluator -> harness direction (Session._read) ------------------------
  def push(self, frame: str | dict) -> None:
    if isinstance(frame, dict):
      frame = json.dumps(frame)
    self.incoming.put_nowait(frame)

  def close(self) -> None:
    """Ends the incoming stream (what socket closure looks like locally)."""
    self.incoming.put_nowait(_STOP)

  def raise_while_reading(self, exc: BaseException) -> None:
    self.incoming.put_nowait(exc)

  async def recv(self) -> str:
    """One-shot receive used by taskbridge._session for the task message."""
    item = await self.incoming.get()
    if item is _STOP or isinstance(item, BaseException):
      raise ConnectionClosed()
    return item

  def __aiter__(self):
    return self

  async def __anext__(self):
    item = await self.incoming.get()
    if item is _STOP:
      raise StopAsyncIteration
    if isinstance(item, BaseException):
      raise item
    return item

  # -- harness -> evaluator direction (Session.send) -------------------------
  async def send(self, frame: str) -> None:
    if self.send_raises_closed:
      raise ConnectionClosed()
    self.sent_frames.append(frame)
    self.outgoing.put_nowait(frame)


# websockets' ConnectionClosed; imported lazily so _fakes stays import-light
from websockets.exceptions import ConnectionClosed  # noqa: E402


class MockEvaluator:
  """Answers protocol requests sent through a FakeWS, like the real evaluator.

  `responder(req)` may be sync or async and returns the result payload (an
  `ok` field plus whatever the request type carries). One-shot replies can
  also be queued with `reply` instead of using a responder.
  """

  def __init__(self, ws: FakeWS, responder=None):
    self.ws = ws
    self.responder = responder
    self.requests: list[dict] = []
    self.logs: list[str] = []
    self.usage_reports: list[dict] = []
    self.dones: list[dict] = []
    self.errors: list[dict] = []
    self.failures: list[tuple[str, BaseException]] = []
    self._task: asyncio.Task | None = None
    self._answers: set[asyncio.Task] = set()

  def start(self) -> MockEvaluator:
    self._task = asyncio.get_running_loop().create_task(self._serve())
    return self

  async def stop(self) -> None:
    for task in list(self._answers):
      task.cancel()
    if self._task:
      self._task.cancel()
      try:
        await self._task
      except asyncio.CancelledError:
        pass

  async def _serve(self) -> None:
    while True:
      frame = await self.ws.outgoing.get()
      req = json.loads(frame)
      kind = req.get("type")
      if kind == "log":
        self.logs.append(req.get("text", ""))
        continue
      if kind == "usage":
        self.usage_reports.append(req)
        continue
      if kind == "done":
        self.dones.append(req)
        continue
      if kind == "error":
        self.errors.append(req)
        continue
      self.requests.append(req)
      # one answer task per request: a hanging responder must not stop the
      # serve loop from recording done/error frames
      answer = asyncio.create_task(self._answer(req))
      self._answers.add(answer)
      answer.add_done_callback(self._answers.discard)

  async def _answer(self, req: dict) -> None:
    try:
      result = self.responder(req) if self.responder else {"ok": True}
      if asyncio.iscoroutine(result):
        result = await result
      self.ws.push({"type": "result", "id": req["id"], **result})
    except asyncio.CancelledError:
      raise
    except Exception as exc:  # keep test failures diagnosable, not silent
      self.failures.append((req.get("type"), exc))


def exec_result(exit_code: int = 0, output: str = "", **extra) -> dict:
  return {
      "ok": True,
      "exit_code": exit_code,
      "output": output,
      "truncated": False,
      "timed_out": False,
      **extra,
  }


def read_result(content: bytes, **extra) -> dict:
  import base64

  return {
      "ok": True,
      "content_b64": base64.b64encode(content).decode(),
      "size": len(content),
      "truncated": False,
      **extra,
  }


class RecordingSession:
  """Stands in for taskbridge.Session where only log/usage/final matter."""

  def __init__(self, model: str = "claude-opus-5-5"):
    self.model = model
    self.logs: list[str] = []
    self.usage_reports: list[dict] = []
    self.final_message = ""
    self.instruction = "do the task"
    self.mcp_servers: list[dict] = []
    self.workdir = "/app"
    self.limits: dict = {}
    self._time_left: float | None = None

  def time_left(self) -> float | None:
    return self._time_left

  async def log(self, text: str) -> None:
    self.logs.append(text)

  async def usage(
      self,
      model: str,
      input_tokens: int = 0,
      output_tokens: int = 0,
      cache_read_tokens: int = 0,
  ) -> None:
    self.usage_reports.append({
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    })


class FakeRelay:
  """Stands in for taskbridge.Relay where only mcp_url/env matter."""

  def __init__(self, url: str = "http://127.0.0.1:9/relay"):
    self.url = url

  def mcp_url(self, name: str) -> str:
    return f"{self.url}/mcp/{name}/"

  def env(self, base: dict | None = None) -> dict:
    env = dict(base if base is not None else {})
    env["TASK_RELAY_URL"] = self.url
    return env


async def drain_queue(queue: asyncio.Queue) -> None:
  """Yields control until anything queued on `queue` has been processed."""
  await asyncio.sleep(0.05)

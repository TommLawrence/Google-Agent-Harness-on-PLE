"""Offline protocol tests for taskbridge: Session, Relay, MCPClient, process.

Covers assignment Phase A: session/protocol handling, deadline management
primitives, usage accounting clamps, and the local HTTP relay — all against
in-process fakes, with no Docker, network, or model calls.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import sys
import unittest

import taskbridge
from tests._fakes import (
    FakeWS,
    MockEvaluator,
    RecordingSession,
    exec_result,
    read_result,
)

MCP_URL = "http://127.0.0.1:8000/mcp/"


async def hang(req):
  """A responder that never answers, simulating a stuck evaluator."""
  await asyncio.sleep(600)


def make_session(task: dict | None = None):
  """Returns (session, ws, evaluator, reader); call stop() when done."""
  ws = FakeWS()
  task = {"type": "task", "protocol": 1, "session": "s", "instruction": "do it",
          "workdir": "/app", "limits": {}, "mcp_servers": [], **(task or {})}
  session = taskbridge.Session(ws, task)
  evaluator = MockEvaluator(ws).start()
  reader = asyncio.get_running_loop().create_task(session._read())
  return session, ws, evaluator, reader


async def stop(session, evaluator, reader):
  await evaluator.stop()
  reader.cancel()
  try:
    await reader
  except asyncio.CancelledError:
    pass


class SessionRequestTests(unittest.IsolatedAsyncioTestCase):

  async def test_exec_round_trip(self):
    session, ws, evaluator, reader = make_session()

    async def respond(req):
      self.assertEqual(req["type"], "exec")
      self.assertEqual(req["command"], "ls -la")
      return exec_result(output="file.txt\n")

    evaluator.responder = respond
    result = await session.exec("ls -la")
    self.assertEqual(result["exit_code"], 0)
    self.assertEqual(result["output"], "file.txt\n")
    await stop(session, evaluator, reader)

  async def test_exec_clamps_timeout(self):
    session, ws, evaluator, reader = make_session()
    seen = []

    async def respond(req):
      seen.append(req["timeout_sec"])
      return exec_result()

    evaluator.responder = respond
    await session.exec("x", timeout_sec=99999)
    await session.exec("x", timeout_sec=0)
    self.assertEqual(seen, [taskbridge.MAX_TIMEOUT_SEC, 1])
    await stop(session, evaluator, reader)

  async def test_exec_rejects_bad_values_locally(self):
    session, _, evaluator, reader = make_session()
    with self.assertRaises(taskbridge.TaskError):
      await session.exec("")  # empty command
    with self.assertRaises(taskbridge.TaskError):
      await session.exec("x\0y")  # NUL byte
    with self.assertRaises(taskbridge.TaskError):
      await session.exec("x", timeout_sec="fast")  # not a number
    self.assertEqual(evaluator.requests, [])
    await stop(session, evaluator, reader)

  async def test_exec_error_result_raises_task_error(self):
    session, ws, evaluator, reader = make_session()
    evaluator.responder = lambda req: {"ok": False, "error": "no such file"}
    with self.assertRaises(taskbridge.TaskError):
      await session.exec("cat missing.txt")
    await stop(session, evaluator, reader)

  async def test_read_file_ok_and_truncated(self):
    session, ws, evaluator, reader = make_session()
    evaluator.responder = lambda req: read_result(b"hello")
    self.assertEqual(await session.read_file("/app/a.txt"), b"hello")
    evaluator.responder = lambda req: read_result(b"x", truncated=True, size=999)
    with self.assertRaises(taskbridge.TaskError):
      await session.read_file("/app/big.bin")
    await stop(session, evaluator, reader)

  async def test_write_file_single_and_chunked(self):
    session, ws, evaluator, reader = make_session()
    calls = []

    async def respond(req):
      calls.append((req.get("append"), len(base64.b64decode(req["content_b64"]))))
      return {"ok": True, "size": sum(n for _, n in calls)}

    evaluator.responder = respond
    size = await session.write_file("/app/out.txt", "hello")
    self.assertEqual(calls, [(False, 5)])
    self.assertEqual(size, 5)

    calls.clear()
    big = b"z" * (taskbridge.WRITE_CHUNK * 2 + 10)
    size = await session.write_file("/app/big.bin", big)
    self.assertEqual([n for _, n in calls],
                     [taskbridge.WRITE_CHUNK, taskbridge.WRITE_CHUNK, 10])
    self.assertEqual([a for a, _ in calls], [False, True, True])
    self.assertEqual(size, len(big))
    await stop(session, evaluator, reader)

  async def test_http_rules(self):
    servers = [{"name": "apps", "url": MCP_URL}]
    session, ws, evaluator, reader = make_session({"mcp_servers": servers})

    async def respond(req):
      self.assertEqual(req["type"], "http")
      self.assertEqual(req["url"], MCP_URL)
      return {"ok": True, "status": 200, "headers": {"Content-Type": "application/json"},
              "body_b64": base64.b64encode(b"{}").decode(), "truncated": False}

    evaluator.responder = respond
    reply = await session.http(MCP_URL, body=b"")
    self.assertEqual(reply["status"], 200)
    with self.assertRaises(taskbridge.TaskError):  # not a declared server
      await session.http("http://127.0.0.1:9999/other/", body=b"")
    with self.assertRaises(taskbridge.TaskError):  # bad method
      await session.http(MCP_URL, method="TRACE", body=b"")
    with self.assertRaises(taskbridge.TaskError):  # oversized body
      await session.http(MCP_URL, body=b"x" * (taskbridge.MAX_HTTP_BYTES + 1))
    with self.assertRaises(taskbridge.TaskError):  # header values must be text
      await session.http(MCP_URL, headers={"X": 3}, body=b"")
    await stop(session, evaluator, reader)

  async def test_cancelled_pending_request(self):
    session, ws, evaluator, reader = make_session()
    evaluator.responder = hang  # this request is never answered
    pending = asyncio.ensure_future(session.exec("slow"))
    await asyncio.sleep(0.05)  # let the request go out
    self.assertEqual(len(evaluator.requests), 1)
    ws.push({"type": "cancel", "reason": "timeout"})
    with self.assertRaises(taskbridge.Cancelled):
      await pending
    with self.assertRaises(taskbridge.Cancelled):  # later requests fail fast
      await session.exec("x")
    await stop(session, evaluator, reader)

  async def test_log_and_usage_are_sent_and_clamped(self):
    session, ws, evaluator, reader = make_session()
    await session.log("log" * 10000)
    await session.usage("m", input_tokens=-5, output_tokens=7, cache_read_tokens=3)
    await stop(session, evaluator, reader)
    sent = [json.loads(f) for f in ws.sent_frames]
    log = next(m for m in sent if m["type"] == "log")
    self.assertEqual(len(log["text"]), 20000)
    usage = next(m for m in sent if m["type"] == "usage")
    self.assertEqual(usage, {"type": "usage", "model": "m", "input_tokens": 0,
                             "output_tokens": 7, "cache_read_tokens": 3})

  async def test_time_left_from_limits(self):
    session, _, evaluator, reader = make_session({"limits": {"agent_timeout_sec": 3600}})
    left = session.time_left()
    self.assertIsNotNone(left)
    self.assertGreater(left, 3500)
    await stop(session, evaluator, reader)
    session2, _, evaluator2, reader2 = make_session({"limits": {}})
    self.assertIsNone(session2.time_left())
    await stop(session2, evaluator2, reader2)


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
  """End-to-end _session flow over a FakeWS with a mock evaluator."""

  async def run_session(self, task, handler, responder=None, cancel_after=None):
    ws = FakeWS()
    evaluator = MockEvaluator(ws, responder).start()
    ws.push(task)
    session_ref = {}

    async def wrapped(session):
      session_ref["session"] = session
      return await handler(session)

    done_task = asyncio.ensure_future(taskbridge._session(ws, wrapped))
    if cancel_after is not None:
      async def cancel_later():
        await asyncio.sleep(cancel_after)
        ws.push({"type": "cancel", "reason": "timeout"})
      asyncio.ensure_future(cancel_later())
    await asyncio.wait_for(done_task, 10)
    await evaluator.stop()
    return session_ref.get("taskbridge_session"), ws, evaluator

  async def test_normal_completion_sends_done(self):
    _, ws, evaluator = await self.run_session(
        {"type": "task", "instruction": "hi", "workdir": "/app"},
        lambda session: asyncio.sleep(0, result="all done"),
    )
    self.assertEqual(evaluator.dones, [{"type": "done", "final_message": "all done"}])
    self.assertEqual(evaluator.errors, [])

  async def test_bad_task_message_is_refused(self):
    ws = FakeWS()
    ws.push({"type": "nonsense"})
    await asyncio.wait_for(taskbridge._session(ws, lambda s: None), 5)
    sent = [json.loads(f) for f in ws.sent_frames]
    self.assertEqual(sent, [{"type": "error", "message": "expected a task message first"}])

  async def test_handler_failure_without_summary_sends_error(self):
    async def boom(session):
      raise RuntimeError("model exploded")

    _, ws, evaluator = await self.run_session(
        {"type": "task", "instruction": "hi"}, boom)
    self.assertEqual(evaluator.dones, [])
    self.assertEqual(len(evaluator.errors), 1)
    self.assertIn("RuntimeError", evaluator.errors[0]["message"])

  async def test_handler_failure_after_summary_still_sends_done(self):
    async def work_then_boom(session):
      session.final_message = "deliverables are in /app/out"
      raise RuntimeError("late failure")

    _, ws, evaluator = await self.run_session(
        {"type": "task", "instruction": "hi"}, work_then_boom)
    self.assertEqual(evaluator.dones,
                     [{"type": "done",
                       "final_message": "deliverables are in /app/out"}])
    # the failure was also logged so the graders see the summary exists
    self.assertTrue(any("harness error" in log for log in evaluator.logs))

  async def test_cancel_flow_returns_best_effort_summary(self):
    task = {"type": "task", "instruction": "hi", "limits": {"agent_timeout_sec": 600}}

    async def hang(req):
      await asyncio.sleep(600)  # the evaluator never answers this exec

    async def handler(session):
      try:
        await session.exec("slow thing")  # left pending until the cancel
      except taskbridge.Cancelled:
        session.final_message = "partial deliverables written"
        return "partial deliverables written"

    _, ws, evaluator = await self.run_session(
        task, handler, responder=hang, cancel_after=0.05)
    self.assertEqual(evaluator.dones,
                     [{"type": "done", "final_message": "partial deliverables written"}])


class RelayTests(unittest.IsolatedAsyncioTestCase):

  async def relay_session(self, responder=None, servers=()):
    session, ws, evaluator, reader = make_session({"mcp_servers": list(servers)})
    evaluator.responder = responder
    relay = taskbridge.Relay(session)
    await relay.__aenter__()

    async def cleanup():
      await relay.__aexit__(None, None, None)
      await stop(session, evaluator, reader)

    self.addAsyncCleanup(cleanup)
    return session, ws, evaluator, relay

  @staticmethod
  def endpoint(relay):
    host, port = relay.url.split("//")[1].split("/")[0].split(":")
    return host, int(port)

  async def http_call(self, relay, route, body=b"", method="POST"):
    """Blocking http.client must run in a thread or it starves the loop."""
    host, port = self.endpoint(relay)

    def call():
      conn = http.client.HTTPConnection(host, port, timeout=5)
      conn.request(method, route, body=body, headers={"Accept": "application/json"})
      resp = conn.getresponse()
      status, data = resp.status, resp.read()
      conn.close()
      return status, data

    return await asyncio.to_thread(call)

  async def test_exec_and_write_through_relay(self):
    seen = []

    async def respond(req):
      seen.append(req)
      if req["type"] == "exec":
        return exec_result(output="ok")
      return {"ok": True, "size": len(base64.b64decode(req["content_b64"]))}

    _, _, _, relay = await self.relay_session(respond)
    body = json.dumps({"command": "echo ok", "timeout_sec": 30}).encode()
    status, data = await self.http_call(relay, f"/{relay.token}/exec", body)
    self.assertEqual(status, 200)
    resp = json.loads(data)
    self.assertEqual(resp["exit_code"], 0)
    self.assertEqual(resp["output"], "ok")

    body = json.dumps({"path": "/app/out/x.txt",
                       "content_b64": base64.b64encode(b"data").decode()}).encode()
    status, data = await self.http_call(relay, f"/{relay.token}/write_file", body)
    resp = json.loads(data)
    self.assertEqual(resp, {"ok": True, "size": 4})
    self.assertEqual(seen[1]["path"], "/app/out/x.txt")

  async def test_relay_rejects_bad_token_and_bad_bodies(self):
    _, _, _, relay = await self.relay_session()
    status, _ = await self.http_call(relay, "/wrong-token/exec", b"{}")
    self.assertEqual(status, 404)

    status, _ = await self.http_call(relay, f"/{relay.token}/write_file", b"{not json")
    self.assertEqual(status, 400)

    body = json.dumps({"path": "/x", "content_b64": "!!!not-b64!!!"}).encode()
    status, data = await self.http_call(relay, f"/{relay.token}/write_file", body)
    resp = json.loads(data)
    self.assertEqual(resp["ok"], False)
    self.assertIn("base64", resp["error"])

  async def test_relay_mcp_proxy_routing(self):
    servers = [{"name": "apps", "url": MCP_URL}]

    async def respond(req):
      if req["type"] == "http":
        self.assertTrue(req["url"].startswith(MCP_URL))
        rpc = json.loads(base64.b64decode(req["body_b64"]))
        self.assertEqual(rpc.get("method"), "tools/list")
        return {"ok": True, "status": 200, "headers": {"Content-Type": "application/json"},
                "body_b64": base64.b64encode(b'{"jsonrpc":"2.0"}').decode(),
                "truncated": False}
      return exec_result()

    _, _, _, relay = await self.relay_session(respond, servers)
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list"}).encode()
    status, data = await self.http_call(relay, f"/{relay.token}/mcp/apps/", body)
    self.assertEqual(status, 200)
    self.assertEqual(data, b'{"jsonrpc":"2.0"}')

    status, _ = await self.http_call(relay, f"/{relay.token}/mcp/apps/", b"", method="GET")
    self.assertEqual(status, 405)  # SSE streams unsupported

    status, _ = await self.http_call(relay, f"/{relay.token}/mcp/ghost/", b"{}")
    self.assertEqual(status, 404)

  async def test_relay_env_sets_no_proxy(self):
    session, _, evaluator, reader = make_session()
    relay = taskbridge.Relay(session)
    await relay.__aenter__()
    try:
      env = relay.env({"NO_PROXY": "internal.example"})
      self.assertTrue(env["TASK_RELAY_URL"].startswith("http://127.0.0.1:"))
      self.assertIn("internal.example", env["NO_PROXY"])
      self.assertEqual(env["no_proxy"], env["NO_PROXY"])
      self.assertIn("127.0.0.1", env["NO_PROXY"])
    finally:
      await relay.__aexit__(None, None, None)
      await stop(session, evaluator, reader)

  async def test_chunked_request_body(self):
    _, _, _, relay = await self.relay_session(lambda req: exec_result(output="hi"))
    _, port = self.endpoint(relay)

    async def raw_chunked():
      reader, writer = await asyncio.open_connection("127.0.0.1", port)
      payload = b'{"command":"echo hi"}'
      head = (f"POST /{relay.token}/exec HTTP/1.1\r\n"
              "Transfer-Encoding: chunked\r\n\r\n").encode()
      writer.write(head)
      writer.write(b"%x\r\n%s\r\n" % (len(payload), payload))
      writer.write(b"0\r\n\r\n")
      await writer.drain()
      raw = await reader.read(65536)
      writer.close()
      return raw

    raw = await asyncio.wait_for(raw_chunked(), 5)
    self.assertTrue(raw.startswith(b"HTTP/1.1 200"))
    self.assertIn(b"exit_code", raw)
    self.assertIn(b'"output": "hi"', raw)


class MCPClientTests(unittest.IsolatedAsyncioTestCase):

  class FakeHTTPSession:
    """MCPClient answers are JSON-RPC envelopes; responders return payloads
    shaped {"result": ...} / {"error": ...} which this fake wraps, or pass a
    str through raw."""

    def __init__(self, responder, servers=()):
      self.responder = responder
      self.calls = []
      self.mcp_servers = list(servers)

    async def http(self, url, method="POST", headers=None, body=b"", timeout_sec=120):
      self.calls.append({"url": url, "method": method, "headers": dict(headers or {}), "body": body})
      status, payload, resp_headers = self.responder(json.loads(body.decode()), headers)
      if isinstance(payload, str):
        raw = payload.encode()
      else:
        raw = json.dumps({"jsonrpc": "2.0", "id": json.loads(body.decode()).get("id"),
                          **payload}).encode()
      return {"status": status, "headers": dict(resp_headers or {}),
              "body": raw, "truncated": False}

  async def test_initialize_handshake_sets_session_and_version(self):
    def respond(msg, headers):
      if msg["method"] == "initialize":
        return (200, {"result": {"protocolVersion": "2025-03-26", "capabilities": {}}},
                {"Mcp-Session-Id": "sid-1"})
      return 200, {"result": {}}, {}

    client = taskbridge.MCPClient(self.FakeHTTPSession(respond), MCP_URL)
    result = await client.initialize()
    self.assertEqual(result["protocolVersion"], "2025-03-26")
    self.assertEqual(client.headers.get("Mcp-Session-Id"), "sid-1")
    self.assertEqual(client.headers.get("MCP-Protocol-Version"), "2025-03-26")

  async def test_list_tools_follows_pagination(self):
    def respond(msg, headers):
      cursor = msg.get("params", {}).get("cursor")
      if not cursor:
        return (200, {"result": {"tools": [
                    {"name": "a", "description": "da"}, {"bogus": True}, "junk"],
                    "nextCursor": "page-2"}}, {})
      return (200, {"result": {"tools": [
                  {"name": "c", "inputSchema": {"type": "object"}}]}}, {})

    client = taskbridge.MCPClient(self.FakeHTTPSession(respond), MCP_URL)
    tools = await client.list_tools()
    self.assertEqual([t["name"] for t in tools], ["a", "c"])
    self.assertEqual(tools[0]["description"], "da")
    self.assertEqual(tools[1]["inputSchema"], {"type": "object"})

  async def test_call_tool_and_rpc_errors(self):
    def respond(msg, headers):
      if msg["method"] == "tools/call":
        if msg["params"]["name"] == "bad":
          return 200, {"error": {"code": -32602, "message": "Invalid params"}}, {}
        return 200, {"result": {"content": [{"type": "text", "text": "42"}]}}, {}
      return 200, {"result": {}}, {}

    client = taskbridge.MCPClient(self.FakeHTTPSession(respond), MCP_URL)
    result = await client.call_tool("good", {"x": 1})
    self.assertEqual(result["content"][0]["text"], "42")
    with self.assertRaises(taskbridge.MCPError):
      await client.call_tool("bad", {})

  async def test_http_error_truncated_and_missing_answer(self):
    def respond(msg, headers):
      if msg["method"] == "explode":
        return 500, {"error": {"message": "server died"}}, {}
      if msg["method"] == "never_answered":
        # a JSON-RPC reply carrying an id that matches no request
        return 200, json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}), {}
      return 200, {"result": {"unrelated": True}}, {}

    client = taskbridge.MCPClient(self.FakeHTTPSession(respond), MCP_URL)
    with self.assertRaises(taskbridge.MCPError):
      await client.rpc("explode")

    session = self.FakeHTTPSession(respond)
    client = taskbridge.MCPClient(session, MCP_URL)
    async def truncated(*a, **k):
      reply = await self.FakeHTTPSession.http(session, *a, **k)
      reply["truncated"] = True
      return reply
    session.http = truncated
    with self.assertRaises(taskbridge.MCPError):
      await client.rpc("huge")

    client = taskbridge.MCPClient(self.FakeHTTPSession(respond), MCP_URL)
    with self.assertRaises(taskbridge.MCPError):
      await client.rpc("never_answered")

  async def test_rpc_rejects_urls_outside_declared_servers(self):
    real, _, evaluator, reader = make_session({"mcp_servers": []})
    client = taskbridge.MCPClient(real, MCP_URL)
    with self.assertRaises(taskbridge.TaskError):
      await client.rpc("initialize", {})
    await stop(real, evaluator, reader)

  async def test_messages_parses_json_and_sse(self):
    single = taskbridge.MCPClient.messages('{"id": 1, "result": {}}')
    self.assertEqual(single[0]["id"], 1)
    batch = taskbridge.MCPClient.messages('[{"id": 1}, {"id": 2}]')
    self.assertEqual(len(batch), 2)
    sse = (
        "event: message\r\ndata: {\"id\": 7, \"result\": {\"x\": 1}}\r\n\r\n"
        "event: message\r\ndata: not json\r\n\r\n"
        "data: {\"id\": 8}\r\n\r\n"
    )
    found = taskbridge.MCPClient.messages(sse)
    self.assertEqual([m.get("id") for m in found], [7, 8])
    self.assertEqual(taskbridge.MCPClient.messages("garbage :("), [])


class HelperFunctionTests(unittest.TestCase):

  def test_seconds_clamps(self):
    self.assertEqual(taskbridge._seconds(None), 120)
    self.assertEqual(taskbridge._seconds(0), 1)
    self.assertEqual(taskbridge._seconds(-10), 1)
    self.assertEqual(taskbridge._seconds(99999), taskbridge.MAX_TIMEOUT_SEC)
    self.assertEqual(taskbridge._seconds(45.9), 45)
    with self.assertRaises(taskbridge.TaskError):
      taskbridge._seconds(True)
    with self.assertRaises(taskbridge.TaskError):
      taskbridge._seconds(float("nan"))
    with self.assertRaises(taskbridge.TaskError):
      taskbridge._seconds("soon")

  def test_text_validation(self):
    self.assertEqual(taskbridge._text("f", "ok"), "ok")
    for bad in ("", None, 5, "a\0b", "bad\ud800 surrogate"):
      with self.assertRaises(taskbridge.TaskError):
        taskbridge._text("f", bad)

  def test_frame_limit(self):
    small = taskbridge._frame({"type": "log", "text": "hi"})
    self.assertIn("log", small)
    with self.assertRaises(taskbridge.TaskError):
      taskbridge._frame({"type": "log", "text": "x" * (taskbridge.MAX_FRAME + 10)})

  def test_final_message_truncation(self):
    self.assertEqual(taskbridge._final("hello"), "hello")
    text = "y" * (taskbridge.MAX_FINAL_CHARS + 10)
    out = taskbridge._final(text)
    self.assertTrue(out.startswith("y" * 100))
    self.assertIn("[... the rest of this message was cut]", out)

  def test_positive_int(self):
    f = taskbridge.positive_int
    self.assertEqual(f(5, 1), 5)
    self.assertEqual(f("7", 1), 7)
    self.assertEqual(f(3.9, 1), 4)
    self.assertEqual(f(0, 9), 9)          # 0 means "no limit" -> default
    self.assertEqual(f(-2, 9), 9)
    self.assertEqual(f(None, 9), 9)
    self.assertEqual(f(True, 9), 9)       # bools are not counts
    self.assertEqual(f(99, 9, cap=10), 10)
    self.assertEqual(f(float("inf"), 9), 9)

  def test_time_share_reserve_shrinks_for_short_tasks(self):
    self.assertIsNone(taskbridge.time_share(None, 180))
    self.assertEqual(taskbridge.time_share(100, 30), 75.0)   # 25% fraction caps
    self.assertAlmostEqual(taskbridge.time_share(10, 180), 7.5)  # 10 - 2.5
    self.assertEqual(taskbridge.time_share(0, 180), 0.0)

  def test_mcp_allows_rules(self):
    servers = [{"name": "apps", "url": MCP_URL}]
    yes = lambda url: taskbridge.mcp_allows(url, servers)
    self.assertTrue(yes(MCP_URL))
    self.assertTrue(yes(MCP_URL + "tools/list"))
    self.assertTrue(yes(MCP_URL + "?cursor=1"))
    self.assertFalse(taskbridge.mcp_allows(MCP_URL, []))
    self.assertFalse(yes("http://127.0.0.1:9999/mcp/"))
    self.assertFalse(yes(MCP_URL + "../escape"))
    self.assertFalse(yes(MCP_URL + "%2e%2e/escape"))
    self.assertFalse(yes(MCP_URL + "%2fencoded"))
    self.assertFalse(yes(MCP_URL + r"\backslash"))
    self.assertFalse(yes(MCP_URL + "x;y/param"))
    # subpaths under a declared server are allowed (mcp_target relies on it)
    self.assertTrue(yes(MCP_URL + "sub/path"))
    # a base with its own query only allows the exact URL back
    q = "http://127.0.0.1:8000/mcp/?token=1"
    qs = [{"name": "q", "url": q}]
    self.assertTrue(taskbridge.mcp_allows(q, qs))
    self.assertFalse(taskbridge.mcp_allows(q + "&extra=2", qs))

  def test_mcp_target_joins_paths(self):
    self.assertEqual(taskbridge.mcp_target(MCP_URL, "", ""), MCP_URL)
    self.assertEqual(taskbridge.mcp_target(MCP_URL, "sub", "a=1"),
                     MCP_URL + "sub?a=1")
    with self.assertRaises(taskbridge.TaskError):
      taskbridge.mcp_target(MCP_URL + "?fixed=1", "sub", "")

  def test_lines_skips_oversized_lines(self):
    async def run():
      sr = asyncio.StreamReader()
      sr.feed_data(b"a" * 20 + b"\nok\n" + b"b" * 15 + b"\nend")
      sr.feed_eof()
      return [line async for line in taskbridge.lines(sr, max_line=10)]

    got = asyncio.run(run())
    self.assertEqual(got, [b"ok", b"end"])


class RunProcessTests(unittest.IsolatedAsyncioTestCase):

  async def test_successful_process_with_output(self):
    session = RecordingSession()
    got = []
    finished = await taskbridge.run_process(
        [sys.executable, "-c",
         "import sys; print('out-line'); print('err-line', file=sys.stderr)"],
        session, on_line=got.append, log_prefix="agent: ")
    self.assertEqual(finished.code, 0)
    self.assertFalse(finished.stopped)
    self.assertIn(b"out-line", b"".join(got))
    self.assertTrue(any("err-line" in log for log in session.logs))

  async def test_stdin_delivery(self):
    session = RecordingSession()
    got = []
    finished = await taskbridge.run_process(
        [sys.executable, "-c",
         "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        session, on_line=got.append, stdin=b"ping\n")
    self.assertEqual(finished.code, 0)
    self.assertIn(b"PING", b"".join(got))

  async def test_stop_before_deadline(self):
    session = RecordingSession()
    session._time_left = 1.0  # one second of task time "remains"
    finished = await taskbridge.run_process(
        [sys.executable, "-c", "import time; time.sleep(15)"],
        session, stop_before_sec=0.5)
    self.assertTrue(finished.stopped)
    self.assertEqual(finished.code, -1)
    self.assertTrue(any("stopped" in log for log in session.logs))

  async def test_process_failure_keeps_stderr_tail(self):
    session = RecordingSession()
    finished = await taskbridge.run_process(
        [sys.executable, "-c", "import sys; print('boom-stderr', file=sys.stderr); sys.exit(3)"],
        session)
    self.assertEqual(finished.code, 3)
    self.assertIn("boom-stderr", finished.stderr)


if __name__ == "__main__":
  unittest.main()

"""Offline tests for harness.py model configuration and event accounting.

Verifies the evaluator environment-variable conventions for all three
provider routes, the unknown-model catalog fallback, Pi settings/MCP config
writing, and the JSON event -> usage accounting path — without launching a
real model process or touching the network.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import harness
from tests._fakes import FakeRelay, RecordingSession


class ProviderSelectionTests(unittest.TestCase):
  """provider() must honor the evaluator's env conventions exactly."""

  def test_anthropic_route_uses_native_api_and_compat(self):
    env = {"ANTHROPIC_BASE_URL": "https://claude.endpoint/v1",
           "ANTHROPIC_API_KEY": "k"}
    with mock.patch.dict(os.environ, env, clear=True):
      name, config = harness.provider()
    self.assertEqual(name, "anthropic")
    self.assertEqual(config["baseUrl"], "https://claude.endpoint/v1")
    self.assertEqual(config["api"], "anthropic-messages")
    self.assertEqual(config["apiKey"], "$ANTHROPIC_API_KEY")
    # the Google Cloud route for Claude refuses strict tools / mid-conversation
    # effort / system messages / tool changes: they must stay off
    self.assertEqual(config.get("compat"), harness.COMPAT)
    self.assertFalse(harness.COMPAT["supportsStrictTools"])
    self.assertFalse(harness.COMPAT["supportsMidConvoEffort"])
    self.assertFalse(harness.COMPAT["supportsMidConvoSystemMessages"])
    self.assertFalse(harness.COMPAT["supportsMidConvoToolChanges"])

  def test_gemini_route_uses_v1beta_and_key_var(self):
    env = {"GOOGLE_GEMINI_BASE_URL": "https://gemini.endpoint",
           "GEMINI_API_KEY": "k"}
    with mock.patch.dict(os.environ, env, clear=True):
      name, config = harness.provider()
    self.assertEqual(name, "google")
    self.assertEqual(config["baseUrl"], "https://gemini.endpoint/v1beta")
    self.assertEqual(config["api"], "google-generative-ai")
    self.assertEqual(config["apiKey"], "$GEMINI_API_KEY")
    self.assertNotIn("compat", config)  # provider-specific flags stay off

  def test_openai_compatible_route_is_the_fallback(self):
    env = {"MODEL_BASE_URL": "https://any.endpoint/v1", "MODEL_API_KEY": "k"}
    with mock.patch.dict(os.environ, env, clear=True):
      name, config = harness.provider()
    self.assertEqual(name, "model")
    self.assertEqual(config["baseUrl"], "https://any.endpoint/v1")
    self.assertEqual(config["api"], "openai-completions")
    self.assertEqual(config["apiKey"], "$MODEL_API_KEY")


class ConfigureTests(unittest.TestCase):
  """configure() writes models.json, settings.json, and mcp.json."""

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.agent_dir = os.path.join(self._tmp.name, "agent")
    os.makedirs(self.agent_dir)

  def tearDown(self):
    self._tmp.cleanup()

  def configure_with(self, env, servers=(), catalog_hit=False):
    env = dict(env)
    env["MODEL_NAME"] = env.get("MODEL_NAME", "claude-opus-5-5")
    relay = FakeRelay()
    session = RecordingSession()
    session.mcp_servers = list(servers)
    with mock.patch.dict(os.environ, env, clear=True):
      if catalog_hit:
        with mock.patch.object(harness, "in_catalog", return_value=True):
          return harness.configure(
              pathlib.Path(self.agent_dir), relay, session, env)
      # no `pi` binary on this machine -> the catalog probe fails -> UNKNOWN
      return harness.configure(
          pathlib.Path(self.agent_dir), relay, session, env)

  def read(self, name):
    with open(os.path.join(self.agent_dir, name)) as f:
      return json.load(f)

  def test_anthropic_unknown_model_gets_explicit_limits(self):
    name, model = self.configure_with(
        {"ANTHROPIC_BASE_URL": "https://x/v1", "ANTHROPIC_API_KEY": "k"})
    self.assertEqual((name, model), ("anthropic", "claude-opus-5-5"))
    models = self.read("models.json")
    config = models["providers"]["anthropic"]
    self.assertEqual(config["compat"], harness.COMPAT)
    # `pi --list-models` cannot run here, so the fallback must have added the
    # model entry with UNKNOWN_MODEL limits rather than trusting the catalog
    self.assertEqual(
        config["models"],
        [{"id": model, "name": model, **harness.UNKNOWN_MODEL}])

  def test_settings_are_deterministic_and_conservative(self):
    self.configure_with({"MODEL_BASE_URL": "https://x/v1", "MODEL_API_KEY": "k"})
    settings = self.read("settings.json")
    self.assertEqual(settings["defaultProjectTrust"], "never")
    self.assertEqual(settings["quietStartup"], True)
    self.assertEqual(settings["cacheWarming"], "off")
    self.assertEqual(settings["retry"]["maxRetries"], 6)

  def test_mcp_config_zero_servers(self):
    self.configure_with({"MODEL_BASE_URL": "https://x/v1", "MODEL_API_KEY": "k"},
                        servers=[])
    self.assertEqual(self.read("mcp.json"), {"mcpServers": {}})

  def test_mcp_config_one_and_multiple_servers(self):
    servers = [{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"},
               {"name": "docs", "url": "http://127.0.0.1:8001/mcp/"}]
    self.configure_with({"MODEL_BASE_URL": "https://x/v1", "MODEL_API_KEY": "k"},
                        servers=servers)
    config = self.read("mcp.json")["mcpServers"]
    self.assertEqual(
        config,
        {"apps": {"url": "http://127.0.0.1:9/relay/mcp/apps/", "timeout": 300},
         "docs": {"url": "http://127.0.0.1:9/relay/mcp/docs/", "timeout": 300}})

  def test_catalog_known_model_uses_catalog_limits(self):
    env = {"MODEL_BASE_URL": "https://x/v1", "MODEL_API_KEY": "k",
           "MODEL_NAME": "known-model"}
    name, model = self.configure_with(env, catalog_hit=True)
    config = self.read("models.json")["providers"][name]
    self.assertNotIn("models", config)  # no UNKNOWN_MODEL entry injected


class InCatalogTests(unittest.TestCase):

  def test_missing_binary_returns_false(self):
    # PI points at a nonexistent path on this machine: the probe must fail
    # closed (False) so the model is configured explicitly
    env = {"MODEL_NAME": "m"}
    with mock.patch.dict(os.environ, env, clear=True):
      self.assertFalse(harness.in_catalog("model", "m", env))

  def test_probe_timeout_returns_false(self):
    import subprocess

    real_run = subprocess.run

    def slow_run(*args, **kwargs):
      raise subprocess.TimeoutExpired(cmd="pi", timeout=0.1)

    env = {"MODEL_NAME": "m"}
    with mock.patch.dict(os.environ, env, clear=True):
      with mock.patch.object(harness.subprocess, "run", slow_run):
        self.assertFalse(harness.in_catalog("model", "m", env))

  def test_catalog_match_requires_two_column_prefix(self):
    import subprocess

    def fake_run(*args, **kwargs):
      out = type("R", (), {"stdout": "model m (some limits)\n"
                                    "anthropic claude-opus-5-5 (known)\n"})()
      return out

    env = {"MODEL_NAME": "claude-opus-5-5"}
    with mock.patch.dict(os.environ, env, clear=True):
      with mock.patch.object(harness.subprocess, "run", fake_run):
        self.assertTrue(harness.in_catalog("anthropic", "claude-opus-5-5", env))
      self.assertFalse(harness.in_catalog("model", "claude-opus-5-5", env))


class RunEventAccountingTests(unittest.IsolatedAsyncioTestCase):
  """Pi JSON events -> session usage/log accounting."""

  def make_run(self):
    session = RecordingSession()
    return harness.Run(session, "claude-opus-5-5"), session

  async def test_assistant_message_becomes_final_message_and_usage(self):
    run, session = self.make_run()
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "stop",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "First draft"},
                {"type": "toolCall", "id": "t1"},
            ],
            "usage": {"input": 900, "output": 120, "cacheRead": 300,
                      "cacheWrite": 45},
        },
    }
    await run.on_event(event)
    self.assertEqual(session.final_message, "First draft")
    self.assertEqual(run.replies, 1)
    report = session.usage_reports[0]
    # total prompt = non-cached input + cache read + cache write (Anthropic
    # reports the three separately; the evaluator's input_tokens counts all)
    self.assertEqual(report["input_tokens"], 900 + 300 + 45)
    self.assertEqual(report["output_tokens"], 120)
    self.assertEqual(report["cache_read_tokens"], 300)
    self.assertEqual(report["model"], "claude-opus-5-5")

  async def test_blank_text_does_not_replace_final_message(self):
    run, session = self.make_run()
    await run.on_event({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Keep me"}]}})
    await run.on_event({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "   "}]}})
    self.assertEqual(session.final_message, "Keep me")
    self.assertEqual(run.replies, 2)

  async def test_model_error_is_logged_not_fatal(self):
    run, session = self.make_run()
    await run.on_event({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error",
        "errorMessage": "rate limited", "content": []}})
    self.assertTrue(any("rate limited" in log for log in session.logs))

  async def test_tool_and_retry_events_are_logged(self):
    run, session = self.make_run()
    await run.on_event({"type": "tool_execution_start",
                        "toolName": "write", "args": {"path": "/app/a.txt"}})
    await run.on_event({"type": "auto_retry_start", "attempt": 2})
    await run.on_event({"type": "compaction_start"})
    joined = "\n".join(session.logs)
    self.assertIn("write", joined)
    self.assertIn("auto_retry_start", joined)
    self.assertIn("compaction_start", joined)

  async def test_non_json_and_non_dict_lines_are_ignored(self):
    run, session = self.make_run()
    await run.on_line(b"not json at all")
    await run.on_line(b"[1, 2, 3]")
    await run.on_line(b'{"type": "message_end", "message": {}}')
    self.assertEqual(run.replies, 0)
    self.assertEqual(session.final_message, "")

  async def test_missing_usage_is_reported_as_zeros(self):
    run, session = self.make_run()
    await run.on_event({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}]}})
    report = session.usage_reports[0]
    self.assertEqual(report["input_tokens"], 0)
    self.assertEqual(report["output_tokens"], 0)
    self.assertEqual(report["cache_read_tokens"], 0)


class SolveFailureTests(unittest.IsolatedAsyncioTestCase):
  """No execution path may silently return success when the model fails."""

  async def test_solve_raises_when_pi_dies_before_replying(self):
    fake_pi = pathlib.Path(tempfile.mkdtemp()) / "pi"
    fake_pi.write_text("#!/bin/sh\necho 'died' >&2\nexit 3\n")
    fake_pi.chmod(0o755)
    with mock.patch.object(harness, "PI", str(fake_pi)):
      session = RecordingSession()
      session.instruction = "do the task"
      session.workdir = "/app"
      env = {"HOME": tempfile.mkdtemp(), "MODEL_NAME": "claude-opus-5-5",
             "MODEL_BASE_URL": "https://offline.invalid/v1",
             "MODEL_API_KEY": "unused-offline"}
      with mock.patch.dict(os.environ, env, clear=False):
        with self.assertRaises(RuntimeError) as ctx:
          await harness.solve(session)
    self.assertIn("exited with 3", str(ctx.exception))
    self.assertIn("died", ctx.exception.stderr if hasattr(
        ctx.exception, "stderr") else str(ctx.exception))


class EntrypointContractTests(unittest.TestCase):
  """serve.py wiring and the limits the orchestrator relies on."""

  def test_module_contract(self):
    import inspect
    import serve  # noqa: F401  (import must not start the server)

    self.assertTrue(hasattr(harness, "solve"))
    sig = inspect.signature(harness.solve)
    self.assertEqual(list(sig.parameters), ["session"])
    self.assertEqual(harness.THINKING, "max")
    self.assertIsInstance(harness.STOP_BEFORE_SEC, int)
    self.assertGreater(harness.STOP_BEFORE_SEC, 0)
    self.assertIn("{workdir}", harness.NOTE)


if __name__ == "__main__":
  unittest.main()

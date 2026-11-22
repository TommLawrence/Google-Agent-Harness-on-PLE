"""Offline tests for ple_harness.context (prefix, trimming, watermarks)."""

from __future__ import annotations

import unittest

from ple_harness import context


class BuildPrefixTests(unittest.TestCase):

  def test_prefix_is_byte_identical_for_identical_inputs(self):
    a = context.build_prefix("System note.", "do X", ("LEDGER\n- S1",))
    b = context.build_prefix("System note.", "do X", ("LEDGER\n- S1",))
    self.assertEqual(a, b)
    self.assertEqual(a.encode(), b.encode())

  def test_prefix_changes_when_blocks_change(self):
    a = context.build_prefix("n", "task", ("A",))
    b = context.build_prefix("n", "task", ("B",))
    self.assertNotEqual(a, b)

  def test_instruction_is_verbatim(self):
    prefix = context.build_prefix("note", "line1\nline2  spaced")
    self.assertIn("TASK INSTRUCTION (verbatim):", prefix)
    self.assertIn("line1\nline2  spaced", prefix)


class TrimTests(unittest.TestCase):

  def test_small_output_untouched(self):
    t = context.trim_tool_output("short output", 100)
    self.assertFalse(t.truncated)
    self.assertEqual(t.text, "short output")

  def test_large_output_keeps_head_tail_and_important_lines(self):
    lines = [f"filler line {i}" for i in range(200)]
    lines[100] = "Wrote /app/files/output/report.md (14 KB)"       # path
    lines[150] = "Load = 185 kip at base"                          # number+unit
    lines[180] = "error: missing required row"                     # flag
    text = "\n".join(lines)
    t = context.trim_tool_output(text, limit=600)
    self.assertTrue(t.truncated)
    self.assertIn("/app/files/output/report.md", t.text)
    self.assertIn("185 kip", t.text)
    self.assertIn("error: missing required row", t.text)
    self.assertIn("line(s) cut", t.text)
    self.assertIn("output truncated", t.text)
    self.assertLess(len(t.text), 800)  # bounded even with kept lines
    self.assertEqual(t.original_chars, len(text))
    self.assertGreater(t.dropped_lines, 0)

  def test_custom_patterns_are_honored(self):
    text = "alpha\nKEEP ME beta\n" * 200
    t = context.trim_tool_output(text, limit=100,
                                 keep_patterns=(__import__("re").compile(r"KEEP ME"),))
    self.assertIn("KEEP ME", t.text)

  def test_pathological_all_important_output_stays_bounded(self):
    text = "error path /app/x.md 12 kip\n" * 2000
    t = context.trim_tool_output(text, limit=2000)
    self.assertTrue(t.truncated)
    self.assertLess(len(t.text), 4000)


class WatermarkTests(unittest.TestCase):

  def test_advance_is_monotonic(self):
    mark = context.SummaryWatermark()
    self.assertTrue(mark.advance(5))
    self.assertFalse(mark.advance(3))  # regressions refused
    self.assertEqual(mark.value, 5)
    self.assertTrue(mark.advance(5))   # equal is fine
    self.assertEqual(mark.value, 5)

  def test_negative_start_rejected(self):
    with self.assertRaises(ValueError):
      context.SummaryWatermark(-1)


class FileReadCacheTests(unittest.TestCase):

  def test_memo_by_digest(self):
    cache = context.FileReadCache()
    digest = context.FileReadCache.digest(b"file body")
    self.assertIsNone(cache.get("a.md", digest))
    cache.put("a.md", digest, {"summary": "stuff"})
    self.assertEqual(cache.get("a.md", digest), {"summary": "stuff"})
    # changed content -> different digest -> miss
    self.assertIsNone(cache.get("a.md", context.FileReadCache.digest(b"changed")))
    stats = cache.stats()
    self.assertEqual((stats["hits"], stats["misses"]), (1, 2))

  def test_digest_is_stable_sha256(self):
    self.assertEqual(context.FileReadCache.digest(b"abc"),
                     "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


if __name__ == "__main__":
  unittest.main()

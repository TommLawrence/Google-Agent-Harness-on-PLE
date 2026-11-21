"""Context and token-efficiency helpers shared by the orchestrator roles.

- `build_prefix`: one byte-identical system prefix per role; anything cached
  (Anthropic prompt caching in particular) only pays off when the prefix is
  exactly stable across calls, so this assembles it once, deterministically.
- `trim_tool_output`: head+tail truncation that preserves lines carrying
  deliverable paths, numbers with units, and explicit flags, because those
  are what the roles must never lose to truncation.
- `SummaryWatermark`: monotonic compaction marker (advancing backwards is
  refused) so repeated summarization cannot rewind the conversation state.
- `FileReadCache`: digest-keyed memo so unchanged files are not re-read and
  re-extracted on every role turn.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_DELIVERABLE_HINT = re.compile(
    r"(?:/[A-Za-z0-9_.\-]+){2,}|\b[\w.\-]+\.(?:md|csv|docx|xlsx|pdf|zip|ifc|dxf|py|json|txt|eml|xml|log)\b|"
    r"\b(?:-?\d+(?:\.\d+)?\s*(?:kip|lbf|kN|N|psi|ksf|psf|kPa|MPa|mm|cm|m|km|in|ft|yd|mi|ft3|in3|m3|"
    r"lb|kg|ton|V|A|kVA|kW|W|ohm|deg|degC|degF|Hz|Pa|GPa|%))\b",
    re.IGNORECASE,
)
_FLAG_HINT = re.compile(r"\b(?:error|fail|warn|missing|required|exit_code=|timed_out|truncated)\b",
                        re.IGNORECASE)


def build_prefix(system_note: str, instruction: str, extra_blocks: tuple[str, ...] = ()) -> str:
  """Returns the stable system prefix for one role's conversation.

  The instruction is appended verbatim after the static note. Callers must
  keep `extra_blocks` deterministic per role run (evidence ledger rendering,
  plan text) so the prefix stays byte-identical across a role's calls.
  """
  parts = [system_note.rstrip("\n"), "", "TASK INSTRUCTION (verbatim):", instruction]
  for block in extra_blocks:
    parts += ["", block]
  return "\n".join(parts)


@dataclass
class Trimmed:
  text: str
  original_chars: int
  kept_lines: int
  dropped_lines: int
  truncated: bool


def trim_tool_output(text: str, limit: int = 12000,
                     keep_patterns: tuple[re.Pattern, ...] = ()) -> Trimmed:
  """Truncates oversized tool output, keeping the load-bearing lines.

  The budget splits three ways: the head, the tail, and lines matching
  `keep_patterns` (deliverable paths, numbers with units, error/requirement
  flags) — so the exact paths, figures, and failures the harness must not
  lose survive truncation even when the head and tail are full of filler.
  """
  if len(text) <= limit:
    return Trimmed(text, len(text), text.count("\n") + 1, 0, False)
  patterns = keep_patterns or (_DELIVERABLE_HINT, _FLAG_HINT)
  lines = text.splitlines()
  important = [i for i, line in enumerate(lines)
               if any(p.search(line) for p in patterns)][:400]

  head_budget = limit // 3
  important_budget = limit // 3
  tail_budget = limit - head_budget - important_budget

  head, used = [], 0
  for i, line in enumerate(lines):
    if used + len(line) + 1 > head_budget:
      break
    head.append(i)
    used += len(line) + 1
  tail, tused = [], 0
  for i in reversed(range(len(lines))):
    if tused + len(lines[i]) + 1 > tail_budget:
      break
    tail.append(i)
    tused += len(lines[i]) + 1
  tail.reverse()

  taken = set(head) | set(tail)
  kept_important, iused = [], 0
  for i in important:
    if i in taken or iused + len(lines[i]) + 1 > important_budget:
      continue
    kept_important.append(i)
    iused += len(lines[i]) + 1

  chosen = sorted(set(head) | set(tail) | set(kept_important))
  dropped = len(lines) - len(chosen)
  out, last = [], None
  total = 0
  for i in chosen:
    if last is not None and i > last + 1:
      out.append(f"[... {i - last - 1} line(s) cut ...]")
      total += len(out[-1]) + 1
    out.append(lines[i])
    total += len(lines[i]) + 1
    last = i
  out.append(f"[output truncated: {len(text)} chars in, {dropped} of "
             f"{len(lines)} lines cut]")
  return Trimmed("\n".join(out), len(text), len(chosen), dropped, True)


class SummaryWatermark:
  """Monotonic marker of how far compaction has summarized a conversation."""

  def __init__(self, value: int = 0):
    if value < 0:
      raise ValueError("watermark starts at 0")
    self._value = value

  @property
  def value(self) -> int:
    return self._value

  def advance(self, new_value: int) -> bool:
    """Moves the watermark forward; regressions are refused."""
    if new_value < self._value:
      return False
    self._value = new_value
    return True


class FileReadCache:
  """Digest-keyed memo: unchanged files are read and extracted once."""

  def __init__(self):
    self._store: dict[tuple[str, str], object] = {}
    self.hits = 0
    self.misses = 0

  @staticmethod
  def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

  def get(self, path: str, digest: str):
    hit = self._store.get((path, digest))
    self.hits += 1 if hit is not None else 0
    self.misses += 0 if hit is not None else 1
    return hit

  def put(self, path: str, digest: str, value) -> None:
    self._store[(path, digest)] = value

  def stats(self) -> dict[str, int]:
    return {"entries": len(self._store), "hits": self.hits, "misses": self.misses}

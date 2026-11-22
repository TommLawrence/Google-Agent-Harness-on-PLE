"""The evidence ledger: traceability for every engineering statement.

The ledger separates three things that must never be conflated:

- `source`   — a value read out of a task document (path, location, unit).
- `interpretation` — what the model says a value means; always starts
  `unverified` and can only become `verified` through `mark_verified`, which
  requires a verifier reference. A model statement can never silently become
  a confirmed engineering fact.
- `computed` — the output of a deterministic solver run, with its inputs,
  governing rule, and unit conversions recorded.

The ledger serializes to plain JSON, so it survives context compaction by
being written through the relay into the task workspace and reloaded; it also
renders a compact, stable-ordered text view for embedding in role prompts.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

SOURCE = "source"
INTERPRETATION = "interpretation"
COMPUTED = "computed"
VERIFICATION = "verification"

UNVERIFIED = "unverified"
VERIFIED = "verified"
FAILED = "failed"


@dataclass
class Evidence:
  """One traceable engineering statement."""

  id: str
  kind: str  # SOURCE | INTERPRETATION | COMPUTED | VERIFICATION
  claim: str
  source_path: str = ""
  source_location: str = ""  # e.g. "page 3", "sheet Grid, cell B7"
  authority_ref: str = ""    # document id + revision from authority.py
  value: str = ""
  unit: str = ""
  conversion: str = ""       # e.g. "1728 in3/ft3"
  rule: str = ""             # governing equation or engineering rule
  constraints: list[str] = field(default_factory=list)
  derived_from: list[str] = field(default_factory=list)  # evidence ids
  verification: str = UNVERIFIED
  verifier_ref: str = ""     # who verified it (verifier run id / check name)
  confidence: float = 1.0    # 0..1; extraction confidence, NOT correctness
  note: str = ""

  def to_dict(self) -> dict[str, Any]:
    return vars(self)

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> Evidence:
    names = {f.name for f in dataclasses.fields(cls)}
    known = {k: data[k] for k in names if k in data}
    if not isinstance(known.get("id"), str) or not known["id"]:
      raise ValueError("evidence entry needs an id")
    if known.get("kind") not in (SOURCE, INTERPRETATION, COMPUTED, VERIFICATION):
      raise ValueError(f"unknown evidence kind: {known.get('kind')!r}")
    return cls(**known)


class EvidenceLedger:
  """Ordered, JSON-serializable ledger with a content digest."""

  def __init__(self, entries: list[Evidence] | None = None):
    self.entries: list[Evidence] = []
    self._ids: set[str] = set()
    for entry in entries or []:
      self.add(entry)

  def add(self, entry: Evidence) -> Evidence:
    if entry.id in self._ids:
      raise ValueError(f"duplicate evidence id: {entry.id}")
    if entry.kind == INTERPRETATION and entry.verification == VERIFIED and not entry.verifier_ref:
      # model interpretations may not enter the ledger as verified facts
      entry.verification = UNVERIFIED
    self.entries.append(entry)
    self._ids.add(entry.id)
    return entry

  def get(self, evidence_id: str) -> Evidence:
    for entry in self.entries:
      if entry.id == evidence_id:
        return entry
    raise KeyError(evidence_id)

  def mark_verified(self, evidence_id: str, verifier_ref: str,
                    passed: bool = True) -> Evidence:
    """Flips an entry's verification status; requires a verifier reference."""
    if not verifier_ref:
      raise ValueError("mark_verified requires a verifier_ref")
    entry = self.get(evidence_id)
    entry.verification = VERIFIED if passed else FAILED
    entry.verifier_ref = verifier_ref
    return entry

  def by_kind(self, kind: str) -> list[Evidence]:
    return [e for e in self.entries if e.kind == kind]

  def unresolved(self) -> list[Evidence]:
    """Unverified interpretations and computations that must be checked."""
    return [e for e in self.entries
            if e.kind in (INTERPRETATION, COMPUTED) and e.verification == UNVERIFIED]

  def failed(self) -> list[Evidence]:
    return [e for e in self.entries if e.verification == FAILED]

  def to_dict(self) -> dict[str, Any]:
    return {"entries": [e.to_dict() for e in self.entries]}

  def to_json(self) -> str:
    return json.dumps(self.to_dict(), sort_keys=True)

  @classmethod
  def from_json(cls, raw: str) -> EvidenceLedger:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
      raise ValueError("ledger JSON must be an object with an entries list")
    return cls([Evidence.from_dict(e) for e in data["entries"]])

  def digest(self) -> str:
    return hashlib.sha256(self.to_json().encode()).hexdigest()

  def render(self, max_entries: int = 200) -> str:
    """Compact, stable text rendering for embedding into role prompts."""
    lines = ["EVIDENCE LEDGER (id | kind | status | claim | unit | source)"]
    unresolved = {id(e) for e in self.unresolved()}
    for entry in self.entries[:max_entries]:
      status = "source" if entry.kind == SOURCE else entry.verification
      src = f"{entry.source_path}{('#' + entry.source_location) if entry.source_location else ''}"
      lines.append(
          f"- {entry.id} | {entry.kind} | {status} | {entry.claim}"
          f"{' [' + entry.value + ' ' + entry.unit + ']' if entry.value else ''}"
          f"{' <' + src + '>' if src else ''}"
          f"{' (via ' + entry.conversion + ')' if entry.conversion else ''}"
          f"{' {' + entry.rule + '}' if entry.rule else ''}"
          f"{' [unresolved]' if id(entry) in unresolved else ''}"
      )
    if len(self.entries) > max_entries:
      lines.append(f"... {len(self.entries) - max_entries} more entries omitted")
    return "\n".join(lines)

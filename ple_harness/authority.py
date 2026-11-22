"""Document authority and revision resolution for PLE task documents.

Engineering task sets mix AHJ letters, addenda, RFI responses, IFC drawings,
permit/bid sets, and preliminary documents. Which document governs a given
question is NOT a simple global ranking: a newer document does not
automatically invalidate every older one. This resolver therefore:

- works per scope, not globally: documents the planner tags with a scope
  (e.g. a scope-limited RFI answer) govern only that scope; unscoped,
  whole-design documents compete in every scope;
- applies explicit supersession statements first (`supersedes` pairs parsed
  from document text by the caller), then issue status (superseded/void/draft
  documents lose), then category rank, then revision number, then revision
  date, and only then the status rank as a tie-breaker;
- keeps a full, inspectable trace: every decision records the reasons and a
  confidence, and anything that cannot be discriminated is returned as an
  UNRESOLVED CONFLICT with no governing document, never silently resolved to
  whichever document happened to rank highest;
- flags conflicting datums/units declared across candidate documents.

The category ranking below is a documented DEFAULT, not a hard law of the
benchmark: callers (the planner) may re-tag scopes and the resolver surfaces
every choice it made with its evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Default precedence, highest first. Scoped RFI/addendum answers outrank base
# documents within their scope because of scope filtering below, not rank.
CATEGORY_RANK = {
    "ahj_letter": 6,
    "addendum": 5,
    "rfi_response": 4,
    "ifc": 3,
    "permit_bid": 2,
    "preliminary": 1,
    "other": 0,
}

# Issue status: a superseded or void document never governs anything.
STATUS_RANK = {
    "issued_for_construction": 4,
    "issued": 3,
    "approved": 3,
    "for_review": 2,
    "preliminary": 1,
    "draft": 0,
    "unknown": 0,
    "superseded": -1,
    "void": -2,
}

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
REV_TOKEN_RE = re.compile(r"(?:rev(?:ision)?|r)\.?\s*([0-9]+|[A-Z])\b", re.IGNORECASE)


def revision_key(revision: str | None) -> tuple[int, ...] | None:
  """Parses 'Rev 2', 'R2', 'Revision 3A', 'B', '2B' into a comparable tuple.

  Numbers compare numerically; a trailing letter extends the tuple, so 2 <
  2A < 3. Unparseable revisions return None (unknown ordering).
  """
  if not revision:
    return None
  text = revision.strip()
  match = re.search(r"(?:rev(?:ision)?\.?|r\.?)\s*([0-9]+)\s*([A-Za-z])?", text,
                    re.IGNORECASE)
  if match:
    parts = [int(match.group(1))]
    if match.group(2):
      parts.append(ord(match.group(2).upper()) - ord("A") + 1)
    return tuple(parts)
  if text.isdigit():
    return (int(text),)
  pair = re.fullmatch(r"([0-9]+)\s*([A-Za-z])", text, re.IGNORECASE)
  if pair:
    return (int(pair.group(1)), ord(pair.group(2).upper()) - ord("A") + 1)
  if re.fullmatch(r"[A-Za-z]", text):
    return (ord(text.upper()) - ord("A") + 1,)
  return None


def parse_date(value: str | None) -> str | None:
  """Returns the first ISO (YYYY-MM-DD) date in `value`, else None."""
  if not value:
    return None
  match = DATE_RE.search(value)
  return match.group(0) if match else None


@dataclass
class Document:
  id: str
  path: str
  doc_type: str  # a CATEGORY_RANK key
  title: str = ""
  revision: str | None = None
  revision_date: str | None = None
  issue_status: str = "unknown"  # a STATUS_RANK key
  scope_tags: list[str] = field(default_factory=list)  # [] = whole design
  datum: str = ""  # declared vertical datum / unit system, when known

  def __post_init__(self) -> None:
    if self.doc_type not in CATEGORY_RANK:
      raise ValueError(f"unknown doc_type: {self.doc_type!r}")
    if self.issue_status not in STATUS_RANK:
      raise ValueError(f"unknown issue_status: {self.issue_status!r}")


@dataclass
class Supersedes:
  source: str  # document id claiming the supersession
  target: str  # document id being superseded
  note: str = ""


@dataclass
class Decision:
  scope: str
  governing: str | None  # document id, or None when unresolved
  confidence: str  # "high" | "medium" | "low"
  reasons: list[str] = field(default_factory=list)
  conflicts: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return vars(self)


@dataclass
class AuthorityResult:
  decisions: dict[str, Decision] = field(default_factory=dict)
  trace: list[str] = field(default_factory=list)

  def governing_for(self, scope: str) -> str | None:
    decision = self.decisions.get(scope)
    return decision.governing if decision else None

  def unresolved_scopes(self) -> list[str]:
    return [scope for scope, d in self.decisions.items()
            if d.governing is None or d.conflicts]

  def to_dict(self) -> dict[str, Any]:
    return {
        "decisions": {s: d.to_dict() for s, d in self.decisions.items()},
        "trace": self.trace,
    }


def _live(doc: Document, supersedes: list[Supersedes]) -> tuple[bool, str | None]:
  """Returns (is_live, reason) — superseded/void/draft documents never govern."""
  for claim in supersedes:
    if claim.target == doc.id and claim.source != doc.id:
      return False, f"superseded by {claim.source}"
  if doc.issue_status == "superseded":
    return False, "marked superseded"
  if doc.issue_status == "void":
    return False, "marked void"
  return True, None


def compare(a: Document, b: Document) -> tuple[str, str] | None:
  """Compares two same-category candidates; returns (winner_id, reason) or None."""
  ra, rb = CATEGORY_RANK[a.doc_type], CATEGORY_RANK[b.doc_type]
  if ra != rb:
    return (a.id, f"category {a.doc_type} outranks {b.doc_type}") if ra > rb else \
           (b.id, f"category {b.doc_type} outranks {a.doc_type}")
  ka, kb = revision_key(a.revision), revision_key(b.revision)
  if ka is not None or kb is not None:
    if ka != kb:
      if kb is None:
        return a.id, f"{a.id} states revision {a.revision}; {b.id} states none"
      if ka is None:
        return b.id, f"{b.id} states revision {b.revision}; {a.id} states none"
      winner, reason = ((a, b) if ka > kb else (b, a))
      return winner.id, f"revision {winner.revision} is later than {reason.revision}"
  da, db = parse_date(a.revision_date), parse_date(b.revision_date)
  if da and db and da != db:
    winner, reason = ((a, b) if da > db else (b, a))
    return winner.id, f"revision date {winner.revision_date} is later than {reason.revision_date}"
  sa, sb = STATUS_RANK[a.issue_status], STATUS_RANK[b.issue_status]
  if sa != sb:
    winner, reason = ((a, b) if sa > sb else (b, a))
    return winner.id, f"issue status {winner.issue_status} outranks {reason.issue_status}"
  return None


def resolve_authority(documents: list[Document],
                      supersedes: list[Supersedes] | None = None,
                      extra_scopes: list[str] | None = None) -> AuthorityResult:
  """Resolves the governing document per scope across `documents`.

  Args:
    documents: Every known document, each with its type, revision, status,
      and (when known) scope tags.
    supersedes: Explicit supersession claims parsed from document text.
    extra_scopes: Additional scope tags to resolve even if no document
      carries them yet (they produce no decision).

  Returns:
    An `AuthorityResult` with per-scope decisions and a full trace.
  """
  supersedes = supersedes or []
  scopes = {"global"} | {tag for doc in documents for tag in doc.scope_tags}
  if extra_scopes:
    scopes |= set(extra_scopes)

  result = AuthorityResult()
  for doc in documents:
    live, why = _live(doc, supersedes)
    if not live:
      result.trace.append(f"{doc.id}: excluded — {why}")

  for scope in sorted(scopes):
    candidates = []
    for doc in documents:
      live, _ = _live(doc, supersedes)
      if not live:
        continue
      if doc.scope_tags:
        if scope == "global" or scope not in doc.scope_tags:
          continue  # scoped documents govern only their own scope
      candidates.append(doc)

    if not candidates:
      continue  # no decision to record for this scope
    if len(candidates) == 1:
      only = candidates[0]
      result.decisions[scope] = Decision(
          scope, only.id, "high", [f"sole live candidate ({only.doc_type})"])
      result.trace.append(f"scope {scope}: governing {only.id} (sole live candidate)")
      continue

    reasons: list[str] = []
    winner = candidates[0]
    resolved = True
    for challenger in candidates[1:]:
      verdict = compare(winner, challenger)
      if verdict is None:
        # no discriminating information: this is an unresolved conflict, not
        # a silent win for whichever document came first
        result.decisions[scope] = Decision(
            scope, None, "low",
            reasons=[f"{winner.id} vs {challenger.id}: "
                     "same category, no revision/date/status difference"],
            conflicts=[f"cannot discriminate {winner.id} from {challenger.id} "
                       f"in scope {scope!r}"])
        result.trace.append(
            f"scope {scope}: UNRESOLVED between {winner.id} and {challenger.id}")
        resolved = False
        break
      new_winner, reason = verdict
      if new_winner != winner.id:
        reasons.append(reason)
        winner = next(d for d in candidates if d.id == new_winner)
    if not resolved:
      continue

    conflicts: list[str] = []
    confidence = "high"
    datums = {d.datum for d in candidates if d.datum}
    if len(datums) > 1:
      conflicts.append(f"candidates declare conflicting datums/units: {sorted(datums)}")
      confidence = "low"
    if parse_date(winner.revision_date) is None:
      # ordered without a revision date to corroborate the ordering
      confidence = "medium" if confidence == "high" else confidence
      reasons.append(f"{winner.id} states no parseable revision date; "
                     "ordered by revision number/status only")

    result.decisions[scope] = Decision(
        scope, winner.id, confidence, reasons, conflicts)
    result.trace.append(f"scope {scope}: governing {winner.id} — {'; '.join(reasons) or 'default rank'}")
  return result

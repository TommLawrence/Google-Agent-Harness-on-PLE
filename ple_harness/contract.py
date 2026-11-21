"""Deliverable-contract extraction and deterministic validation.

Graders read exact deliverable file paths, names, and formats from the task
instruction, so the harness must treat the instruction's explicit deliverable
clauses as a contract and check it mechanically, not by vibes.

`parse_contract` combines a deterministic pass (regexes over the instruction
text) with an optional model-assisted pass (`model_extract`, a callable that
returns structured requirements extracted by the model). Deterministic
findings are marked `confirmed` when they come from an explicit deliverable
clause and `inferred` otherwise; only `confirmed` and model-confirmed
requirements are enforced, so an ambiguous parse can never fail a valid
deliverable.

`validate_deliverables` checks a contract against the task workspace through
a small IO protocol (so it works over the competition relay or any test
double): existence, non-empty files, format signatures (PDF, OOXML zip
containers, STEP/IFC, DXF, XML, JSON, Python), CSV header schemas, NaN/Inf in
numeric fields, and explicitly required register rows.

Never invents mandatory schema fields: required columns come only from the
instruction or the model extraction, and are only enforced when marked
confirmed.
"""

from __future__ import annotations

import base64
import csv
import io as _io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable
from xml.etree import ElementTree

CONFIRMED = "confirmed"
INFERRED = "inferred"

# Formats whose file signature we can verify offline.
SIGNATURE_FORMATS = ("pdf", "docx", "xlsx", "zip", "ifc", "dxf", "xml", "json", "py")
TEXT_FORMATS = ("csv", "txt", "md", "json", "eml", "xml", "py", "log")
OFFICE_FORMATS = ("docx", "xlsx")
NON_FINITE_TOKENS = {"nan", "-nan", "+nan", "inf", "-inf", "+inf",
                     "infinity", "-infinity", "+infinity"}

# A deliverable path: absolute POSIX path, or a bare filename with a known
# deliverable extension. Deliberately generic; nothing here is task-specific.
_PATH_RE = re.compile(
    r"(?:/(?:[\w.\-]+/)*[\w.\-]+|[\w][\w.\-]*\.(?:md|csv|docx|xlsx|pdf|zip|ifc|dxf|py|json|txt|eml|xml|log))\b",
    re.IGNORECASE,
)
_BACKTICK_PATH_RE = re.compile(r"`([^`]+)`")
_DELIVERABLE_VERB_RE = re.compile(
    r"\b(write|create|produce|save|deliver|submit|export|generate|prepare|"
    r"provide|fill(?: in)?|complete|populate|attach|include)\b",
    re.IGNORECASE,
)
_TARGET_PREP_RE = re.compile(
    r"\b(?:to|at|in|into|under|as)\s+(?:the\s+file\s+)?[`'\"]?([^\s`'\".,;)]+[^\s`'\".,;)]*)",
    re.IGNORECASE,
)
_COLUMNS_RE = re.compile(
    r"\b(?:columns?|header(?: row)?|headers?)\s*(?:must (?:be|include)|are|is|of|:|\()\s*"
    r"([A-Za-z0-9_ ,()/\-]*[A-Za-z0-9_][A-Za-z0-9_ ,()/\-]*)",
    re.IGNORECASE,
)
_REGISTER_ROW_RE = re.compile(
    r"\b(?:row|entry|record)\s+(?:for|named|titled|labeled|labelled)\s+[\"'`]?"
    r"([A-Za-z0-9][\w \-/.&]*?)[\"'`]?\s*(?=(?:,|;|\band\b|\bor\b|\.|$))",
    re.IGNORECASE,
)
_REGISTER_FILE_RE = re.compile(
    r"\b(register|ledger|log|schedule|takeoff|inventory|index)\b[^.`\"]{0,120}?`([^`]+)`",
    re.IGNORECASE,
)
_FINAL_MESSAGE_RE = re.compile(
    r"\b(final message|when you are done|when done|in your (?:final )?(?:reply|summary)|"
    r"summar(?:y|ize|ise)\b|respond with|reply with a)\b",
    re.IGNORECASE,
)


class MissingFile(Exception):
  """Raised by a TaskIO when a required file does not exist."""


class TaskIO:
  """What contract validation needs from a workspace (task container or test).

  Implementations: the orchestrator's relay-backed adapter, and in-memory
  doubles in the tests.
  """

  async def read_bytes(self, path: str) -> bytes:
    raise NotImplementedError

  async def exists(self, path: str) -> bool:
    raise NotImplementedError


@dataclass
class Requirement:
  """One explicit deliverable clause from the instruction."""

  kind: str  # "file" | "csv_schema" | "register_row" | "final_message"
  path: str | None
  extension: str | None = None
  description: str = ""
  required_columns: list[str] = field(default_factory=list)
  required_rows: list[str] = field(default_factory=list)
  confidence: str = INFERRED
  quote: str = ""  # the instruction snippet it came from

  def to_dict(self) -> dict[str, Any]:
    return {
        "kind": self.kind,
        "path": self.path,
        "extension": self.extension,
        "description": self.description,
        "required_columns": self.required_columns,
        "required_rows": self.required_rows,
        "confidence": self.confidence,
        "quote": self.quote,
    }


@dataclass
class ContractIssue:
  """A parser anomaly worth surfacing to the orchestrator and the verifier."""

  detail: str
  source: str  # "deterministic" | "model" | "validation"


@dataclass
class DeliverableContract:
  files: list[Requirement] = field(default_factory=list)
  csv_schemas: dict[str, list[str]] = field(default_factory=dict)
  register_rows: dict[str, list[str]] = field(default_factory=dict)
  final_message_required: bool = False
  issues: list[ContractIssue] = field(default_factory=list)
  workspace_root: str | None = None  # set by the caller when known

  def confirmed_files(self) -> list[Requirement]:
    return [r for r in self.files if r.confidence == CONFIRMED]

  def enforced_rows(self, path: str) -> list[str]:
    return self.register_rows.get(path, [])

  def enforced_columns(self, path: str) -> list[str]:
    return self.csv_schemas.get(path, [])

  def to_dict(self) -> dict[str, Any]:
    return {
        "files": [r.to_dict() for r in self.files],
        "csv_schemas": self.csv_schemas,
        "register_rows": self.register_rows,
        "final_message_required": self.final_message_required,
        "issues": [vars(i) for i in self.issues],
        "workspace_root": self.workspace_root,
    }


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
  parts = re.split(r"(?<=[.!?\n])\s+", text)
  return [p.strip() for p in parts if p.strip()]


def _extension_of(path: str) -> str | None:
  match = re.search(r"\.([A-Za-z0-9]+)$", path)
  return match.group(1).lower() if match else None


def _clean_path(raw: str) -> str:
  return raw.strip().strip("`'\"").rstrip(".,;:")


def _is_plausible_path(raw: str) -> bool:
  raw = _clean_path(raw)
  if not raw or len(raw) > 300:
    return False
  if ".." in raw.split("/"):
    return False
  if raw.startswith("/"):
    return _PATH_RE.fullmatch(raw) is not None
  return bool(re.fullmatch(r"[\w][\w.\-]*\.(?:md|csv|docx|xlsx|pdf|zip|ifc|dxf|py|json|txt|eml|xml|log)",
                           raw, re.IGNORECASE))


def _add_requirement(files: dict[str, Requirement], path: str, quote: str,
                     confidence: str, description: str) -> None:
  existing = files.get(path)
  if existing:
    if confidence == CONFIRMED and existing.confidence != CONFIRMED:
      existing.confidence = CONFIRMED
      existing.quote = quote or existing.quote
    if description and description not in existing.description:
      existing.description = f"{existing.description}; {description}".strip("; ")
    return
  files[path] = Requirement(
      kind="file", path=path, extension=_extension_of(path),
      description=description, confidence=confidence, quote=quote)


def deterministic_extract(text: str, contract: DeliverableContract) -> None:
  """Fills `contract` from regex-level evidence in the instruction text."""
  files: dict[str, Requirement] = {r.path: r for r in contract.files}
  current_register: str | None = None  # register file named by the latest sentence

  for sentence in _sentences(text):
    paths = [_clean_path(m.group(0)) for m in _PATH_RE.finditer(sentence)]
    backticked = [_clean_path(m.group(1)) for m in _BACKTICK_PATH_RE.finditer(sentence)
                  if _is_plausible_path(_clean_path(m.group(1)))]
    verb = bool(_DELIVERABLE_VERB_RE.search(sentence))

    for reg_match in _REGISTER_FILE_RE.finditer(sentence):
      target = reg_match.group(2)
      if _is_plausible_path(target):
        current_register = target

    for path in backticked:
      if _extension_of(path) in ("csv", "xlsx") and (
          verb or re.search(r"\b(register|ledger|log|schedule|takeoff|inventory|index)\b",
                            sentence, re.IGNORECASE)):
        current_register = path

    for path in dict.fromkeys(paths + backticked):  # unique, order kept
      if not _is_plausible_path(path):
        continue
      confidence = CONFIRMED if verb and (path in paths or path in backticked) else INFERRED
      _add_requirement(files, path, sentence, confidence,
                       "deliverable clause" if confidence == CONFIRMED else "mentioned path")

    # explicit CSV column lists
    columns = _COLUMNS_RE.search(sentence)
    if columns:
      names = [c.strip().strip("`'\". ") for c in re.split(r"[;,]", columns.group(1))
               if c.strip().strip("`'\". ")]
      targets = backticked or [p for p in paths if _extension_of(p) == "csv"]
      for target in targets:
        contract.csv_schemas.setdefault(target, [])
        for name in names:
          if name not in contract.csv_schemas[target]:
            contract.csv_schemas[target].append(name)
      if names and not targets:
        contract.issues.append(ContractIssue(
            source="deterministic",
            detail=f"column list without a target file: {names!r} in {sentence[:120]!r}"))

    # explicit register rows (a sentence naming no file inherits the
    # register file from the most recent one that did)
    for row_match in _REGISTER_ROW_RE.finditer(sentence):
      row = row_match.group(1).strip()
      targets = backticked or [p for p in paths if _extension_of(p) in ("csv", "xlsx")]
      if not targets and current_register:
        targets = [current_register]
      for target in targets:
        contract.register_rows.setdefault(target, [])
        if row not in contract.register_rows[target]:
          contract.register_rows[target].append(row)
      if not targets:
        contract.issues.append(ContractIssue(
            source="deterministic",
            detail=f"required row without a target file: {row!r} in {sentence[:120]!r}"))

    if _FINAL_MESSAGE_RE.search(sentence):
      contract.final_message_required = True

  contract.files = list(files.values())


def _valid_model_requirement(entry: Any) -> dict[str, Any] | None:
  if not isinstance(entry, dict):
    return None
  path = entry.get("path")
  if not isinstance(path, str) or not _is_plausible_path(_clean_path(path)):
    return None
  return entry


def model_extract_merge(contract: DeliverableContract, model_json: Any) -> None:
  """Merges model-extracted requirements into the contract.

  The model extraction may only add requirements marked confirmed if they are
  structurally valid; malformed entries are recorded as issues and dropped.
  """
  if not isinstance(model_json, dict):
    contract.issues.append(ContractIssue(
        source="model", detail=f"model extraction was {type(model_json).__name__}, not an object"))
    return

  files: dict[str, Requirement] = {r.path: r for r in contract.files}
  added = 0
  for entry in model_json.get("files") or []:
    entry = _valid_model_requirement(entry)
    if entry is None:
      contract.issues.append(ContractIssue(source="model", detail=f"dropped invalid file entry: {entry!r}"))
      continue
    path = _clean_path(entry["path"])
    quote = str(entry.get("quote") or "")
    _add_requirement(files, path, quote, CONFIRMED,
                     str(entry.get("description") or "model-extracted deliverable"))
    added += 1

    fmt = entry.get("format")
    if isinstance(fmt, str) and fmt.lower() in SIGNATURE_FORMATS:
      requirement = files[path]
      if requirement.extension is None:
        requirement.extension = fmt.lower()

    columns = entry.get("required_columns")
    if isinstance(columns, list) and columns:
      names = [str(c).strip() for c in columns if str(c).strip()]
      if names:
        contract.csv_schemas.setdefault(path, [])
        contract.csv_schemas[path] += [n for n in names if n not in contract.csv_schemas[path]]

    rows = entry.get("required_rows")
    if isinstance(rows, list) and rows:
      names = [str(r).strip() for r in rows if str(r).strip()]
      if names:
        contract.register_rows.setdefault(path, [])
        contract.register_rows[path] += [n for n in names if n not in contract.register_rows[path]]

  contract.files = list(files.values())
  if not added:
    contract.issues.append(ContractIssue(
        source="model", detail="model extraction listed no valid deliverable files"))

  fmr = model_json.get("final_message_required")
  if isinstance(fmr, bool) and fmr:
    contract.final_message_required = True


def parse_contract(text: str, model_extract: Callable[[str], Any] | None = None,
                   workspace_root: str | None = None) -> DeliverableContract:
  """Parses the instruction into a deliverable contract.

  Args:
    text: The verbatim task instruction.
    model_extract: Optional callable returning structured requirements for the
      instruction (the orchestrator supplies one backed by the model).
    workspace_root: The task workdir, when known; used to flag requirements
      that resolve outside the workspace.

  Returns:
    The populated `DeliverableContract`.
  """
  contract = DeliverableContract(workspace_root=workspace_root)
  deterministic_extract(text, contract)
  if model_extract is not None:
    try:
      model_json = model_extract(text)
    except Exception as exc:  # noqa: BLE001 - any extractor failure is an issue, not a crash
      contract.issues.append(ContractIssue(
          source="model", detail=f"model extraction failed: {type(exc).__name__}: {exc}"))
    else:
      model_extract_merge(contract, model_json)
  if workspace_root:
    for requirement in contract.files:
      resolved = resolve_path(requirement.path, workspace_root)
      if not path_within(resolved, workspace_root):
        requirement.description = f"{requirement.description}; outside workspace".strip("; ")
        contract.issues.append(ContractIssue(
            source="validation",
            detail=f"required path escapes the workspace: {requirement.path}"))
  return contract


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def resolve_path(path: str, workdir: str) -> str:
  """Resolves `path` against `workdir` without touching the filesystem."""
  if path.startswith("/"):
    return re.sub(r"/{2,}", "/", path)
  return re.sub(r"/{2,}", "/", f"{workdir.rstrip('/')}/{path}")


def path_within(path: str, root: str) -> bool:
  """True when `path` is `root` itself or lies underneath it.

  `..` segments are normalized lexically, so `root/../x` correctly escapes.
  """
  path = posixpath.normpath(resolve_path(path, root))
  root = posixpath.normpath(root)
  if root == "/":
    return path.startswith("/")
  return path == root or path.startswith(root + "/")


def check_pdf(data: bytes) -> str | None:
  return None if data[:1024].lstrip().startswith(b"%PDF-") else "not a PDF (missing %PDF- header)"


def check_zip_office(data: bytes, fmt: str) -> str | None:
  if not data.startswith(b"PK\x03\x04"):
    return f"not an OOXML container (missing PK zip signature) for .{fmt}"
  try:
    with zipfile.ZipFile(_io.BytesIO(data)) as bundle:
      names = set(bundle.namelist())
  except zipfile.BadZipFile as exc:
    return f"corrupt zip container: {exc}"
  if "[Content_Types].xml" not in names:
    return "zip container lacks [Content_Types].xml (not a valid OOXML document)"
  return None


def check_ifc(data: bytes) -> str | None:
  head = data[:512].decode("ascii", "replace").lstrip()
  return None if head.startswith("ISO-10303-21") else "not a STEP/IFC physical file (missing ISO-10303-21 header)"


def check_dxf(data: bytes) -> str | None:
  head = data[:512].decode("ascii", "replace").lstrip()
  if head.startswith("AutoCAD Binary DXF"):
    return None
  first_line = head.splitlines()[0].strip() if head.splitlines() else ""
  if first_line in ("0", "999") and "SECTION" in head[:256].upper():
    return None
  return "not a DXF drawing (missing 0/SECTION group structure)"


def check_xml(data: bytes) -> str | None:
  try:
    ElementTree.fromstring(data)
  except ElementTree.ParseError as exc:
    return f"invalid XML: {exc}"
  return None


def check_json(data: bytes) -> str | None:
  try:
    json.loads(data.decode("utf-8-sig"))
  except (ValueError, UnicodeDecodeError) as exc:
    return f"invalid JSON: {exc}"
  return None


def check_python(data: bytes) -> str | None:
  try:
    compile(data.decode("utf-8"), "<deliverable>", "exec")
  except (SyntaxError, ValueError) as exc:
    return f"invalid Python source: {exc}"
  except UnicodeDecodeError:
    return "python deliverable is not valid UTF-8"
  return None


def sniff(data: bytes, path: str) -> str | None:
  """Returns a format-check failure message, or None when it looks right."""
  fmt = (_extension_of(path) or "").lower()
  if fmt == "pdf":
    return check_pdf(data)
  if fmt in OFFICE_FORMATS:
    return check_zip_office(data, fmt)
  if fmt == "zip":
    if not zipfile.is_zipfile(_io.BytesIO(data)):
      return "not a zip archive"
    return None
  if fmt == "ifc":
    return check_ifc(data)
  if fmt == "dxf":
    return check_dxf(data)
  if fmt == "xml":
    return check_xml(data)
  if fmt == "json":
    return check_json(data)
  if fmt == "py":
    return check_python(data)
  if fmt in TEXT_FORMATS:
    try:
      data.decode("utf-8-sig")
    except UnicodeDecodeError:
      return f"not valid UTF-8 text for .{fmt}"
    return None
  return None


def check_csv_bytes(data: bytes, required_columns: list[str],
                    required_rows: list[str]) -> list[str]:
  """CSV checks shared by validation: schema, NaN/Inf, required rows.

  Only requirements explicitly present in the contract are enforced.
  """
  problems: list[str] = []
  try:
    text = data.decode("utf-8-sig")
  except UnicodeDecodeError:
    return ["csv is not valid UTF-8"]
  rows = list(csv.reader(_io.StringIO(text)))
  rows = [r for r in rows if any(cell.strip() for cell in r)]
  if not rows:
    return ["csv has no data rows"]

  if required_columns:
    header = [c.strip().lower() for c in rows[0]]
    missing = [c for c in required_columns if c.strip().lower() not in header]
    if missing:
      problems.append(f"csv header is missing required columns: {missing}")

  for index, row in enumerate(rows):
    for cell in row:
      if cell.strip().lower() in NON_FINITE_TOKENS:
        problems.append(f"csv row {index + 1} holds a non-finite value: {cell!r}")
        break

  if required_rows:
    for wanted in required_rows:
      needle = wanted.strip().lower()
      hit = any(needle in " ".join(row).lower() for row in rows)
      if not hit:
        problems.append(f"csv register is missing required row: {wanted!r}")
  return problems


@dataclass
class CheckResult:
  name: str
  path: str | None
  ok: bool
  detail: str = ""
  severity: str = "fail"  # "fail" | "warn"


@dataclass
class ValidationReport:
  results: list[CheckResult] = field(default_factory=list)
  notes: list[str] = field(default_factory=list)

  @property
  def passed(self) -> bool:
    return all(r.ok or r.severity == "warn" for r in self.results)

  @property
  def failures(self) -> list[CheckResult]:
    return [r for r in self.results if not r.ok and r.severity == "fail"]

  def to_dict(self) -> dict[str, Any]:
    return {
        "passed": self.passed,
        "results": [vars(r) for r in self.results],
        "notes": self.notes,
    }


async def validate_deliverables(contract: DeliverableContract, io: TaskIO,
                                workdir: str | None = None) -> ValidationReport:
  """Checks every enforced requirement of `contract` against the workspace.

  Empty-file, format, schema, and register checks run on bytes read through
  `io`. Requirements marked `inferred` are checked but only ever produce
  warnings, so an ambiguous parse cannot fail a valid deliverable set.
  """
  report = ValidationReport()
  enforced = contract.confirmed_files()
  if not enforced:
    report.notes.append(
        "no confirmed deliverable files in the contract; validation covers "
        f"{len(contract.files)} inferred path(s) as warnings only")

  for requirement in contract.files:
    severity = "fail" if requirement.confidence == CONFIRMED else "warn"
    path = requirement.path or ""
    resolved = resolve_path(path, workdir) if workdir else path
    try:
      data = await io.read_bytes(resolved)
    except MissingFile:
      report.results.append(CheckResult(
          "file_exists", path, False, "required file is missing", severity))
      continue
    except Exception as exc:  # noqa: BLE001 - unreadable is a failure of this check
      report.results.append(CheckResult(
          "file_readable", path, False, f"read failed: {type(exc).__name__}: {exc}", severity))
      continue

    report.results.append(CheckResult(
        "file_exists", path, True, f"{len(data)} bytes", severity))
    if not data:
      report.results.append(CheckResult(
          "file_non_empty", path, False, "file is empty (placeholder outputs are rejected)", severity))
      continue

    fmt_problem = sniff(data, path)
    report.results.append(CheckResult(
        "file_format", path, fmt_problem is None, fmt_problem or "signature OK", severity))

    columns = contract.enforced_columns(path)
    rows = contract.enforced_rows(path)
    if columns or rows or _extension_of(path) == "csv":
      if _extension_of(path) == "csv":
        csv_problems = check_csv_bytes(data, columns, rows)
        for problem in csv_problems:
          report.results.append(CheckResult(
              "csv_contract", path, False, problem, severity))
        if not csv_problems:
          report.results.append(CheckResult(
              "csv_contract", path, True, "header and register checks passed", severity))
      elif columns or rows:
        report.results.append(CheckResult(
            "csv_contract", path, False,
            f"schema/row requirements exist for a non-csv file ({path})", "warn"))

  if contract.final_message_required:
    report.notes.append("the instruction requires a final-message summary")
  return report


def b64(data: bytes) -> str:
  """Convenience for relay-backed IO implementations."""
  return base64.b64encode(data).decode()

"""Role prompts and the structured verdict schema for the orchestrator.

Three roles run in genuinely separate model contexts (one `pi` process each):

- PLANNER  — read-only; inventories the workspace (orchestrator-provided
  listing), resolves governing documents, extracts the deliverable contract,
  hard constraints, and the applicable calculations; emits a structured plan.
- WORKER   — full tools; executes the plan, runs calculations through the
  deterministic solver library where applicable, writes deliverables EARLY
  and refines them, reads back what it wrote, and reports what it produced.
- VERIFIER — read-only; independently re-derives the engineering numbers,
  checks every deliverable against the contract and the hard constraints,
  and returns a structured verdict. The orchestrator enforces PASS in
  Python: a PASS from the model alone is never sufficient.

The prompts are generic by design: no task IDs, no file names, no numbers.
"""

PLANNER_NOTE = """You are the PLANNER in an engineering harness. You work in a fresh context
with read-only access to the task container: you cannot write or edit files.

Your output is a JSON plan that the harness executes mechanically and that a
second agent will follow blindly. Be explicit and complete; the worker cannot
ask you questions.

Work through, in order:
1. The workspace inventory below. Identify every document that could govern
   the task: drawings, specifications, letters (AHJ), addenda, RFI responses,
   revisions, templates, data files.
2. Document authority. For each conflict, decide which document governs WHICH
   scope. A newer document does not automatically invalidate everything older:
   a scope-limited RFI answer or addendum governs only its subject. Note
   supersession statements, revision numbers, revision dates, and issue status.
   Record every authority decision with its source.
3. The deliverable contract. List EVERY file the instruction requires: exact
   path, exact name, exact format, and any stated schema (CSV headers, register
   rows, sections of a memo, sheets of a workbook). Quote the instruction text
   each requirement comes from. Mark requirements you are SURE about; do not
   invent requirements that are not stated.
4. Hard constraints: every explicit numeric or rule constraint the deliverable
   must satisfy (limits, required rows, thresholds, datums, units).
5. Calculations: which engineering computations are needed, with the governing
   formula and required inputs for each.
6. Steps: an ordered execution plan that writes a first version of every
   deliverable EARLY, then refines.

Reply with ONLY a JSON object of this shape (no markdown fence, no prose):

{
  "governing_documents": [{"id": "doc1", "path": "...", "type": "addendum|rfi_response|ifc|permit_bid|preliminary|ahj_letter|other", "revision": "...", "date": "...", "scope": ["topic", ...], "governs": "what and why"}],
  "deliverables": [{"path": "exact/path/required", "format": "docx|xlsx|csv|pdf|zip|ifc|dxf|md|json|py", "description": "what it must contain", "quote": "instruction text requiring it", "required_columns": ["..."], "required_rows": ["..."]}],
  "hard_constraints": [{"id": "C1", "constraint": "...", "source": "document or instruction", "verification": "how to check it"}],
  "calculations": [{"id": "K1", "what": "...", "formula": "...", "inputs": "with units", "source": "..."}],
  "steps": ["ordered step", "..."],
  "evidence": [{"id": "S1", "claim": "...", "value": "...", "unit": "...", "source_path": "...", "source_location": "..."}],
  "notes": "anything the worker must not miss"
}"""

WORKER_NOTE = """You are the WORKER in an engineering harness. You have full file and shell
access to the task container. A planner has resolved the document authority,
the deliverable contract, and the hard constraints; your job is execution.

Non-negotiable discipline:
1. DELIVERABLES FIRST: produce a first version of every required file as early
   as possible, then refine. An empty handed worker at deadline scores zero.
2. The contract below is binding: exact paths, exact names, exact formats, and
   every stated schema row or column. Re-read it before you finish.
3. Engineering numbers: derive them from source documents. Record where each
   number came from (document, page, sheet, cell). Use the evidence ledger
   below; add to your working notes anything the ledger lacks. Never invent
   data that is not in the sources — state the gap instead.
4. Check your work: after writing each file, read it back from disk and verify
   the format opens (a docx is a zip container; a csv has the required header;
   an IFC starts with ISO-10303-21). Fix what you find.
5. Units: convert explicitly and record the conversion (1728 in3/ft3,
   1000 lbf/kip, mm/m). Percentages are 0-100; fractions 0-1; never mix them.
6. Cumulative quantities (voltage drop, settlement, cost) accumulate over the
   whole path — never reset at intermediate stages.
7. Do not create files at paths other than the required deliverable paths
   (temp working files belong under a scratch/ directory you create).

When you are done, reply with a short plain-text summary: what you wrote,
where each file is, and which constraints you verified."""


VERIFIER_NOTE = """You are the COLD-READ VERIFIER in an engineering harness. You work in a fresh
context with READ-ONLY access: you cannot modify any file, and the harness
detects modification of deliverables (every file is hashed before and after
your run — any change fails the verification).

You have seen NOTHING of the worker's reasoning. Re-derive independently:

1. Deliverable contract: for each required file — exact path, exact name,
   exact format. A file at the wrong path is a failure even if its content is
   perfect. Check stated schemas (CSV headers, register rows) field by field.
2. Hard constraints: check every constraint against the actual file contents
   you read from the container, not against the worker's claims.
3. Independent recomputation: pick the load-bearing calculations and redo them
   YOURSELF from the source numbers (documents the instruction points to).
   Convert units yourself. Record your recomputed value next to the value the
   deliverable states. Any divergence beyond rounding is a failure.
4. Evidence: does the file's reasoning cite real sources? Flag numbers that
   appear from nowhere, unsupported claims, and hidden assumptions.
5. Read the actual bytes: open each deliverable and inspect its content; for
   docx/xlsx say so only after checking the container structure (unzip -l or
   similar via your read tools where available; otherwise state what you could
   and could not verify).

Then reply with ONLY a JSON verdict (no markdown fence, no prose):

{
  "verdict": "PASS" | "FAIL",
  "checks": [{"id": "V1", "what": "path/format/schema/constraint/recomputation", "passed": true, "detail": "evidence for the judgment"}],
  "failures": [{"id": "F1", "what": "...", "where": "file and location", "severity": "hard|soft", "fix_hint": "what the worker should change"}],
  "recomputations": [{"id": "R1", "what": "the quantity", "deliverable_value": 123.45, "recomputed_value": 123.44, "unit": "...", "within_tolerance": true}],
  "summary": "one paragraph: what was verified, what could not be"
}

Rules: "verdict": "PASS" only if EVERY check passed, every hard failure is
absent, and all recomputations agree within tolerance. If you could not
complete the verification (ran out of access or time), the verdict is FAIL
with an explicit note — an incomplete verification is never a PASS."""


def worker_directive(plan_render: str, contract_render: str) -> str:
  return (
      "Execute the plan now. The deliverable contract and plan below are "
      "binding. Write a first version of every deliverable before deep "
      "refinement.\n\n=== DELIVERABLE CONTRACT ===\n" + contract_render +
      "\n\n=== EXECUTION PLAN ===\n" + plan_render +
      "\n\nBegin execution now."
  )


def repair_directive(failures_render: str) -> str:
  return (
      "The cold-read verifier REJECTED the previous deliverables. Fix exactly "
      "the failures below in the existing files (and re-check the contract "
      "afterwards); change nothing else. Read each file before editing it.\n\n"
      "=== VERIFIER FAILURES ===\n" + failures_render +
      "\n\nRepair now."
  )


def verifier_directive(contract_render: str, constraints_render: str) -> str:
  return (
      "Verify the deliverables now, independently. Judge only what you can "
      "see in the task container. Reply with the JSON verdict only.\n\n"
      "=== DELIVERABLE CONTRACT (from the instruction) ===\n" + contract_render +
      "\n\n=== HARD CONSTRAINTS (from the planner and instruction) ===\n" +
      constraints_render + "\n\nBegin verification now."
  )


def render_contract_for_role(contract) -> str:
  """Compact text rendering of a parsed contract for role prompts."""
  lines = []
  confirmed = [r for r in contract.files if r.confidence == "confirmed"]
  inferred = [r for r in contract.files if r.confidence != "confirmed"]
  for r in confirmed:
    columns = contract.csv_schemas.get(r.path) or []
    rows = contract.register_rows.get(r.path) or []
    line = f"- REQUIRED {r.path} ({r.extension or 'format unstated'}): {r.description or 'deliverable'}"
    if columns:
      line += f" | CSV columns: {', '.join(columns)}"
    if rows:
      line += f" | must include rows mentioning: {'; '.join(rows)}"
    lines.append(line)
  for r in inferred:
    lines.append(f"- (mentioned, not clearly required: {r.path})")
  if contract.final_message_required:
    lines.append("- REQUIRED: final message summarizing the deliverables")
  lines.append("(requirements listed above were extracted mechanically; "
               f"{len(confirmed)} confirmed, {len(inferred)} mentioned-only)")
  return "\n".join(lines) or "(no deliverable files extracted)"


def render_plan_for_role(plan: dict) -> str:
  import json
  return json.dumps(plan, indent=1, sort_keys=True)[:20000]


def render_failures_for_role(verdict: dict) -> str:
  lines = []
  for failure in verdict.get("failures") or []:
    lines.append(
        f"- [{failure.get('severity', 'hard')}] {failure.get('id')}: "
        f"{failure.get('what')} (at {failure.get('where')}) -> {failure.get('fix_hint')}")
  for check in (verdict.get("checks") or []):
    if not check.get("passed"):
      lines.append(f"- [{check.get('id')}] FAILED: {check.get('what')} — {check.get('detail')}")
  for recomputation in (verdict.get("recomputations") or []):
    if recomputation.get("within_tolerance") is False:
      lines.append(
          f"- [recompute] {recomputation.get('what')}: deliverable="
          f"{recomputation.get('deliverable_value')} vs recomputed="
          f"{recomputation.get('recomputed_value')} {recomputation.get('unit')}")
  return "\n".join(lines) or "(verifier reported no structured failures)"


def render_constraints_for_role(plan: dict, instruction: str) -> str:
  lines = []
  for constraint in plan.get("hard_constraints") or []:
    lines.append(f"- [{constraint.get('id')}] {constraint.get('constraint')} "
                 f"(source: {constraint.get('source')}; check: {constraint.get('verification')})")
  if not lines:
    lines.append("(planner extracted no explicit hard constraints)")
  return "\n".join(lines)

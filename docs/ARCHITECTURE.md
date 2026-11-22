# Architecture — as implemented

This document describes what EXISTS in the repository (verified by the
offline test suite, `python -m unittest discover -s tests`), not aspirations.
Anything not yet validated against the real evaluator is marked explicitly.

## Layers

```
Evaluator (WebSocket)          ← mocked in tests, real in competition
        │ task message / exec / read_file / write_file / http / cancel
        ▼
serve.py  →  taskbridge.run(harness.solve)          [official protocol]
        │        Session, Relay (local HTTP), MCPClient, run_process
        ▼
harness.solve                                       [per-task entry]
        │   PLE_ORCHESTRATOR=0 (default) → single pi run (baseline path)
        │   PLE_ORCHESTRATOR=1 → ple_harness.orchestrator
        ▼
ple_harness/                                        [Phase B–D modules]
   contract.py    deliverable contract + deterministic validation
   authority.py   per-scope document authority / revision resolution
   evidence.py    evidence ledger (source | interpretation | computed)
   context.py     stable prefixes, head/tail/important trimming, watermarks
   taskio.py      relay-backed inventory, digests, TaskIO adapter
   roles.py       role prompts + renderers
   orchestrator.py  Planner → Worker → Cold-Read Verifier, digest-enforced
   readonly-tools.ts  read-only toolset for planner/verifier
   solvers/       units, structural, civil, electrical, mechanical
```

## The orchestrated path (default OFF)

`harness.solve` keeps the original single-run pi invocation as the default.
With `PLE_ORCHESTRATOR=1` it calls `orchestrator.run_orchestrated`, which:

1. inventories the workspace itself (orchestrator-side `find` through the
   relay — the planner starts from a verified listing);
2. runs the PLANNER in a fresh context with the read-only toolset (only the
   `read` tool is registered — enforcement by tool configuration) and parses
   its JSON plan;
3. merges the plan's deliverable claims into the deterministic contract
   (`parse_contract` with `model_extract`);
4. runs the WORKER in a fresh context with the full task tools, under a hard
   per-role budget (a `RoleView` deadline capped at the budget —
   `taskbridge.run_process`'s own stop machinery does the killing);
5. validates deliverables mechanically (`validate_deliverables` over
   `RelayIO`): exact paths, non-empty, format signatures (PDF, OOXML zip
   containers, STEP/IFC, DXF, XML, JSON, Python compile), CSV schemas,
   NaN/Inf, register rows;
6. runs the COLD-READ VERIFIER in a fresh read-only context and enforces the
   verdict in Python: `PASS` requires valid structured JSON, every check
   passed, no hard failure, recomputations within tolerance, and UNCHANGED
   deliverable digests across verification (the verifier is hashed before
   and after; any modification fails the task);
7. repairs at most twice (fresh worker context fed the verifier's failure
   list), re-verifying from scratch after each repair;
8. keeps a 180 s wrap-up reserve (shrunk proportionally on short tasks),
   never starts a role that cannot be funded, and composes the final message
   from the best available state.

Failure semantics: an empty, malformed, or timed-out verdict is FAIL, never
PASS; if the orchestrator dies before the first worker completes,
`harness.solve` falls back to the original single-run path with the
remaining time; on cancellation the best-effort summary is preserved for
taskbridge's established cancellation flow.

**Validation status:** the orchestrator passes the offline loopback suite
(mock evaluator → serve → taskbridge → harness → roles → relay → container,
including fail→repair→pass, tamper detection, cancellation, and model
failure). It has NOT yet run against the real evaluator — that is the
Docker machine's first task (`DOCKER_VALIDATION_HANDOFF.md`), and the env
switch stays OFF until the kit check and a dev-set run pass with it enabled.

## What was reused, not duplicated

- `taskbridge.py` (official protocol): untouched; the orchestrator reuses
  `Session`, `Relay`, `run_process`, `time_share`, `lines`, `_stop`.
- `task-tools.ts`: reused verbatim for the worker; the read-only extension
  mirrors only its verified API surface (read tool + relay).
- Pi 1.1.0's compaction/retry: unchanged; the orchestrator adds structure
  around it, not a second agent loop.
- `Run`'s event→usage accounting logic is reimplemented per-role in
  `orchestrator.RoleRun` because the session-level final message must be
  composed by the orchestrator, not each role.

## Evidence and determinism rules

- A model statement enters the ledger as `interpretation`, unverified; only
  `mark_verified(verifier_ref)` flips status — a model claim can never
  silently become a fact.
- Authority decisions are per-scope with recorded reasons/confidence and
  unresolved conflicts surfaced, never silently ranked.
- Solvers validate every input (finite, sign, dimension), never default a
  missing value to zero, document assumptions, and are covered by
  hand-checked regression tests; none claims code-compliance on its own.
- Prompt prefixes are byte-stable per role (`context.build_prefix`); tool
  outputs are trimmed head+tail with a dedicated budget for lines carrying
  deliverable paths, unit-bearing numbers, and error flags.

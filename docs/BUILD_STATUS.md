# BUILD_STATUS — Docker-free development pass

Started: 2026-11-22 (EAT, UTC+3). Machine: Ubuntu 24.04, ThinkPad X250-class, 8 GB RAM, 4 vCPU.
Constraint: no Docker / Harbor / gated dataset / live model calls in this pass (per assignment §12).

## Initial state (verified, not assumed)

- Git `main` at `69db190` ("Add FEEDBACK.md feedback channel"), clean tree, `origin/main` in sync.
- Entry is the Pi 1.1.0 sample: `serve.py` → `taskbridge.run(harness.solve)`; `harness.py` runs one
  headless `pi --print --mode json` session per task; `task-tools.ts` routes read/write/edit/bash
  through `taskbridge.Relay`; MCP servers proxied through the same relay (`mcp.json`).
- `node_modules/` not installed locally (only needed at evaluation build time via `npm ci`).
- System Python 3.12.3 without `websockets`; a local `.venv` (gitignored) now holds
  `websockets==15.0.1` from `requirements.txt` so the protocol code can be imported and tested.

## Baseline (verified from `artifacts/manifest.json`, not from the report)

- 29 dev tasks, total reward `14.246023`, **mean `0.491242`** — the reported ≈0.4912 checks out.
- Baseline harness fingerprint: commit `6ab05b9…`, tree `35113078…`, created `2026-10-20`, model
  `google-cloud/claude-opus-5-5`, all 29 tasks `ended: ok` (no crashes; rewards are real grades).
- **Zero-score tasks: four, not two** as the supplied report stated:
  `foundation-strategy`, `solar-witness-test-package`, `data-hall-mep-routing`,
  `data-hall-mep-routing-offset`. Documented in `docs/BASELINE_FAILURE_ANALYSIS.md`.
- Low band (0.21–0.35): `solar-supplemental-review`, `pre-application-capacity-response`,
  `roof-deck-design`, `eor-review-corrections`, `wind-diaphragm-shear`, `seismic-diaphragm-shear`,
  `division-26-specifications`.

## Trajectory evidence (root causes, Phase F)

- `solar-witness-test-package`: 22 steps, 100% reconnaissance (`ls`/`find`/`curl` to MCP endpoints),
  final message is a mid-exploration thought, `files: {}` — **deadline exhausted before any
  deliverable existed**. No plan, no time-boxing, no deliverable-first discipline.
- `foundation-strategy`: 73 steps, final message `[tool call]`, `files: {}` — same failure family
  (budget spent in analysis, nothing written).
- `data-hall-mep-routing` / `-offset`: wrote `solution.py` + IFC + reconciliation, agent's own
  validator reported 0 issues; final message states the grader was unavailable and checks were
  "only against my reading of the rules". Reward 0.0 — **self-validation did not mirror the
  graded contract**; no independent cold-read verification existed.
- Model/protocol/MCP integration produced no crashes anywhere in the 29 runs: failures are
  **orchestration and verification failures**, not tooling failures.

## Source-of-truth reconciliation (assignment §2)

- Official google-research/ple README: only the eight Google-sponsored `google-cloud/<id>` models
  are accepted for submission. The repo's `AGENTS.md` currently lists thirteen identifiers; the
  configured `model_api: google-cloud/claude-opus-5-5` is valid under both lists and stays
  unchanged this pass. Discrepancy recorded, no action required.
- Competition rules (six-hour tasks, artifacts grading, deliverable paths/names decisive) are
  already reflected in the local `AGENTS.md`.

## Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| Pre-flight | inspection, baseline verification, BUILD_STATUS | **done** |
| A | offline test structure; protocol/session/deadline/usage tests | in progress |
| B | `ple_harness/`: contract, authority, evidence, context | pending |
| C | `ple_harness/solvers/`: units, structural, civil, electrical, mechanical | pending |
| D | Planner / Worker / Cold-Read Verifier orchestration + fallback | pending |
| E | full offline suite incl. mock-evaluator loopback | pending |
| F | baseline failure report (`docs/BASELINE_FAILURE_ANALYSIS.md`) | root causes established, write-up pending |
| G | `kaggle/` offline lab (script, notebook, figures pipeline) | pending |
| Docs | ARCHITECTURE, LOCAL_DEVELOPMENT_NO_DOCKER, DOCKER_VALIDATION_HANDOFF, README | pending |

## Known blockers / deferrals

- Official `kit check`, `eval/run.py`, live dev-set evaluation: **deferred** to the Docker machine
  (deliberately not attempted here; see `docs/DOCKER_VALIDATION_HANDOFF.md`).
- Cross-model claims: none possible without a second model family run; writeup keeps placeholders.
- `artifacts/` is treated as immutable baseline evidence; the manifest tree hash means any harness
  change requires a fresh dev-set run before submission (documented in the handoff doc).

# Baseline failure analysis — Pi 1.1.0 on Claude Opus 5.5 (dev set)

Provenance: `artifacts/` is the untouched dev-set run shipped with the
submission template (`manifest.json`: harness commit `6ab05b9…`, tree
`35113078…`, created 2026-10-20, model `google-cloud/claude-opus-5-5`,
all 29 tasks `ended: ok`). It is treated as immutable baseline evidence —
nothing in this analysis modifies or regenerates it. Per-task trajectories
(`ATIF-v1.2` JSON, agent steps with tool calls and observations) are the
source for every claim below; no task content is reproduced here.

## Verified baseline

- 29 dev tasks, mean reward **0.491242** (total 14.246023) — matches the
  reported ≈0.4912.
- Score distribution: 1 perfect task (1.0), 1 at 0.909, 2 at ≈0.91/0.857,
  a 0.5–0.8 mid band, and a long low tail.
- **Zero-score tasks: four** — `foundation-strategy`,
  `solar-witness-test-package`, `data-hall-mep-routing`,
  `data-hall-mep-routing-offset`. (The supplied report claimed two; the
  manifest shows four.)
- Low band (0.21–0.35): `solar-supplemental-review`,
  `pre-application-capacity-response`, `roof-deck-design`,
  `eor-review-corrections`, `wind-diaphragm-shear`,
  `seismic-diaphragm-shear`, `division-26-specifications`.

## Root causes (from trajectories, not assumptions)

### Failure family 1 — deadline exhausted before any deliverable existed

- `solar-witness-test-package`: 22 agent steps, every one reconnaissance
  (directory listing, PDF inspection, probing MCP endpoints, scraping a wiki,
  grepping correspondence). No file was ever written (`files: {}` in the
  manifest). The recorded final message is a mid-investigation thought —
  the run ended inside exploration, having produced nothing gradable.
- `foundation-strategy`: 73 steps of analysis; the final recorded message is
  a tool-call marker, and no deliverable file exists (`files: {}`).

Diagnosis: no plan, no time budget, no deliverable-first discipline. The
agent treated reading as the task and never switched to writing. Both tasks
scored 0 despite the model clearly having ability — the failure is
orchestration, not capability.

### Failure family 2 — self-validation substituted for the graded contract

- `data-hall-mep-routing` (124 steps) and `data-hall-mep-routing-offset`
  (160 steps): both wrote a `solution.py` generator plus IFC output, ran it
  (exit 0), and validated with the agent's OWN checker reporting zero
  issues. Both scored 0.0. The first task's final message states directly
  that the grader was unavailable and everything was checked "only against
  my reading of the rules".

Diagnosis: the agent graded itself with its own rubric. Its validator
encoded its interpretation, not the task's explicit requirements, so a
wrong interpretation passed every self-check. There was no independent
cold-read of the deliverable against the instruction, no external check of
the geometric contract, and no hard-gate list enforced mechanically.

### Failure family 3 — thin-margin engineering checks (the low band)

- The structural tasks (`wind-diaphragm-shear` 0.316, `seismic-diaphragm-
  shear` 0.316, `roof-deck-design` 0.286) and electrical tasks
  (`division-26-specifications` 0.349) produced packaged deliverables with
  plausible content but partial credit — consistent with specific numeric
  or register checks failing inside otherwise-complete packages. The
  trajectories show the agents self-checking with their own scripts and
  finishing with packaged submissions; no independent recomputation pass
  exists anywhere in the runs.
- `eor-review-corrections` (0.302) and `solar-supplemental-review` (0.208)
  are document-authority/revision tasks where partial credit suggests some
  required corrections were applied and others missed or superseded-source
  material was used.

### What the trajectories rule OUT

- Not MCP/tooling: every task ran to `ok`; tools worked (hundreds of
  successful calls across tasks); the one-score tasks used MCP heavily
  (e.g. the 1.0 task's OpenProject workflow).
- Not model/API errors: no task ended in a model or protocol failure.
- Not premature termination mid-write: the zero tasks ended cleanly; they
  simply had nothing on disk (family 1) or shipped the wrong thing with
  confidence (family 2).

## What this analysis drives in the harness

| Failure family | Harness response (implemented this pass) |
|---|---|
| 1 — no deliverable by deadline | Orchestrator: planner produces a time-boxed plan; worker directive mandates deliverables-first; 180 s wrap-up reserve with per-role budgets; deterministic contract validation on whatever exists, best-effort final message |
| 2 — self-graded rubric | Cold-Read Verifier in a fresh read-only context with Python-enforced PASS (malformed/timeout = FAIL); independent recomputation cross-checked numerically; deliverable digests before/after verification |
| 3 — thin-margin checks | Deterministic deliverable contract (exact paths, formats, CSV schemas, register rows) validated mechanically through the relay; evidence ledger keeps source/authority traceability; solver library gives tested formulas instead of ad-hoc model arithmetic |

## Known limits of this analysis

- Rewards are per-task scalars; the exact grader weights behind partial
  scores are not visible, so low-band attributions are consistent-inference,
  not proof.
- Token/usage telemetry is not present in the trajectories' final_metrics
  (only step counts), so cost figures are deferred until a fresh evaluation
  produces them (see `kaggle/README.md`).
- No claim is made here about hidden-test performance; this documents the
  public dev-set baseline only.

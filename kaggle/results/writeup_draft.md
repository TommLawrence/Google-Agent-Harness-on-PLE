# Stage 1 Writeup draft (PLACEHOLDERS MARKED — no results invented yet)

## 1. Insight and failure analysis

The baseline (Pi 1.1.0 calling google-cloud/claude-opus-5-5, mean 0.4912)
fails in two provable ways and one probable one, all documented from real
trajectories in docs/BASELINE_FAILURE_ANALYSIS.md:

1. Deadline exhaustion before delivery: two tasks ended with no file written —
   the entire budget was spent in reconnaissance. The fix is structural, not
   motivational: a planner that time-boxes understanding, a worker instructed
   (and orchestrated) to write a first deliverable version early, and a hard
   wrap-up reserve.
2. Self-graded rubrics: two tasks shipped outputs validated only against the
   agent's own interpretation of the rules and scored 0. The fix is the
   cold-read verifier: a fresh read-only context that re-derives numbers and
   checks the deliverable contract, with PASS enforced in Python — an absent,
   malformed, or timed-out verdict can never pass.
3. Thin-margin engineering checks in the low band (consistent-inference from
   partial credit; the exact grader weights are not visible): deterministic
   contract validation (paths, formats, CSV schemas, register rows) plus a
   tested solver library remove the arithmetic and format slips a single-pass
   agent makes under context pressure.

<AWAITING-FRESH-EVALUATION: per-family score deltas from the official dev set>

## 2. Engineering reasoning

The harness treats the instruction as a CONTRACT, not a prompt: required
paths, names, formats, schemas, and register rows are extracted (deterministic
regex pass plus a model-assisted pass with explicit confidence), validated
mechanically after the worker writes, and re-checked by the verifier. Document
authority is resolved per scope (a scoped RFI/addendum governs only its
subject; supersession statements and revisions are ordered evidence, not a
global ranking), with unresolved conflicts surfaced instead of silently
ranked. Every engineering number carries its source, unit, and conversion in
an evidence ledger that survives compaction. Deterministic solvers (units,
structural, civil, electrical, mechanical) replace model-improvised arithmetic
with tested functions whose assumptions are documented and whose regression
tests include hand-checked values.

## 3. Cross-model generalizability

<NOT YET CLAIMED: only one model family (Claude via google-cloud) has been
evaluated. The orchestrator, contract validation, solvers, and verifier are
model-agnostic by construction — they speak the protocol and validate files —
but no cross-model comparison exists. A second-family run is required before
any generalization statement.>

## 4. Efficiency and scaling

<PENDING TELEMETRY: the baseline trajectories do not record token usage or
wall-clock per task, so score-vs-cost cannot be computed honestly. The
orchestrator emits per-role usage messages and role timings; the next official
evaluation produces the real numbers. kaggle/figures/score_vs_cost.png is a
labeled placeholder until then.>

Mechanically, the role budgets (planner 20%, worker 45%, verifier 25% of the
usable time after a 180 s reserve) bound the cost of verification at roughly
a quarter of the run, and repair rounds are capped at two, so the worst-case
overhead of the orchestrated path is bounded even before measurement.

## 5. Clarity and visuals

The figure pipeline in this lab generates: the implemented architecture
(architecture.png), the dev-set reward distribution with explicit provenance
(dev_failure_breakdown.png), and a clearly-marked pending-data placeholder
for score-vs-cost (score_vs_cost.png). All figure captions state their data
source; no figure presents synthetic numbers as results.

<WATERMARK: this draft contains marked placeholders wherever evidence does
not yet exist. Nothing in it claims a benchmark improvement that has not
been measured by an official evaluation.>

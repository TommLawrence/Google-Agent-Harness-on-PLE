# Docker validation handoff — the remaining work

Everything that could be built offline is built and offline-green (186
tests). What follows is the exact, ordered remainder for the Docker machine.
Nothing here is optional; the orchestrator stays OFF until step 4 passes.

## State you are inheriting

- Entry: Pi 1.1.0 on `google-cloud/claude-opus-5-5`; `submission.yaml`
  unchanged; `serve.py`/`taskbridge.py` untouched; `harness.py` changed by
  13 lines (the `PLE_ORCHESTRATOR` switch, default off).
- `artifacts/` still holds the ORIGINAL template run (mean 0.4912). It is
  stale the moment any harness file changed — the portal compares your
  submitted code against the manifest's tree hash, so a fresh dev-set run is
  REQUIRED before any submission (see docs/artifacts.md in the competition
  repository).
- New code: `ple_harness/` (contract, authority, evidence, context, taskio,
  roles, orchestrator, readonly-tools.ts, solvers/), `tests/` (186 offline
  tests), `kaggle/` lab, `docs/`.

## Step 1 — clone beside the competition repository

```bash
git clone <this-repo> ../google-harness        # next to the ple checkout
cd ple && uv run python -m kit check ../google-harness
```
Expected: submission.yaml/files/env/build/model/listen/smoke all pass. Fix
any packaging issue before moving on (nothing is expected to fail; the
offline suite covered the protocol).

## Step 2 — model proxy for the sponsored model

Follow the competition repo's `kit/model_proxy/start.sh` docs
(`VERTEXAI_PROJECT`, `MODEL_PROXY_URL`, `MODEL_PROXY_KEY`), then re-run
`kit check` so the model-connectivity stage passes.

## Step 3 — smoke + cancel

```bash
uv run python -m kit check ../google-harness --cancel-test
```

## Step 4 — the decision point: enable the orchestrator

```bash
export PLE_ORCHESTRATOR=1                       # in the run env, NOT committed
uv run python eval/run.py ../google-harness --tasks <one dev task>
```
Run ONE task first. Inspect `runs/…/logs`: role budgets applied, planner
JSON parsed, contract validated, verifier verdict enforced, digests stable,
`done` sent before the deadline. If the orchestrated path misbehaves, the
default single-run path is the safe fallback — debug with the path OFF.

## Step 5 — full dev run with the orchestrator ON

```bash
uv run python eval/run.py ../google-harness     # writes artifacts/
python -m kit artifacts ../google-harness       # validate structure
```
Compare per-task rewards against the baseline (docs/BASELINE_FAILURE_ANALYSIS.md
lists the four zero tasks and the low band — those are the targets). If the
orchestrated mean is NOT better, `git checkout` the switch default back to
OFF and keep the modules (the deterministic contract validation and solvers
benefit even the single-run path via a small follow-up, if you choose).

## Step 6 — commit artifacts together

```bash
git add -A && git commit -m "dev artifacts (orchestrated run)" && git push
```
`artifacts/` must be in the SAME commit as the harness that produced it.

## Step 7 — submit

Portal → Submit → pick repo/branch → verify the checks. One submission in
the queue at a time; ~1 day per evaluation (up to ~3 days at the 6-hour
task limit).

## Cost notes (BYO-key only — NOT applicable to the sponsored model)

The sponsored `google-cloud/` model is billed by the competition; no `.env`
key is needed and none exists in this repo. If you ever switch to a
BYO-key/local model for experiments, remember the evaluator's ~36-concurrent
load and that its 429s count against the entry.

## Risk register (honest)

| Risk | Mitigation |
|---|---|
| Orchestrator regressions vs the real evaluator | It is env-gated OFF; the single-run path is intact and test-covered; step 4 gates enabling |
| Verifier eats too much time on 6 h tasks | Budgets are hard-capped per role; the wrap-up reserve is checked before every role start; loopback-tested |
| Planner JSON unparseable on odd instructions | Deterministic contract extraction runs regardless; the worker gets the raw plan text as a note |
| `ifcopenshell`-style in-container deps for future tasks | Unchanged from baseline behavior (the worker uses whatever the task container provides) |
| Artifacts/hash mismatch at submission | Step 6's same-commit rule; `kit artifacts` validates first |

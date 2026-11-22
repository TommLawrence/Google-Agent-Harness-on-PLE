# Docker validation handoff — the remaining work

Everything that could be built offline is built and offline-green (186
tests). What follows is the exact, ordered remainder for the Docker machine.
Nothing here is optional; the orchestrator stays OFF until step 5 passes.

Last updated: 2026-11-22 07:28 EAT (UTC+3).

Official sources for every command below:
- Kit: <https://github.com/google-research/ple/blob/main/kit/README.md>
- Dev-set evaluation: <https://github.com/google-research/ple/blob/main/eval/README.md>
- Models: <https://github.com/google-research/ple/blob/main/docs/models.md>
- Artifacts rules: <https://github.com/google-research/ple/blob/main/docs/artifacts.md>
- Competition pages: <https://www.kaggle.com/competitions/physical-engineerings-last-exam>
  and <https://ple-bench.com/competition/agents>

## State you are inheriting

- Entry: Pi 1.1.0 on `google-cloud/claude-opus-5-5`; `serve.py`/`taskbridge.py`
  untouched; `harness.py` changed by 13 lines (the `PLE_ORCHESTRATOR` switch,
  default off). `submission.yaml` unchanged — which also means the
  orchestrator is OFF in the committed configuration (see step 5 for how it
  is actually activated).
- `artifacts/` still holds the ORIGINAL template run (mean 0.4912). It was
  produced by an older harness tree, and the portal verifies that submitted
  code matches the harness that generated the artifacts (the manifest hashes
  every harness file). Until a fresh dev-set run is exported and committed,
  `kit check` will not end in "ready to submit" and a submission built from
  this tree would be flagged — that is expected, not a bug.
- New code: `ple_harness/` (contract, authority, evidence, context, taskio,
  roles, orchestrator, readonly-tools.ts, solvers/), `tests/` (186 offline
  tests), `kaggle/` lab, `docs/`.

## Step 0 — source provenance (do this BEFORE cloning on the new machine)

- Required commit on the new machine:
  **`682690b8b620934ad18b56a777f4a260c8a7c79c`** (the docs/handoff commit).
- As of 2026-11-22 07:28 EAT these commits exist ONLY on the dev machine:
  `origin/main` is at `69db190` (verified via `git ls-remote origin main`).
  A fresh clone will NOT contain the harness work until it is pushed.
- On the dev machine: `git push origin main` (local commits only; no force).
- On the new machine, verify before cloning anything else:
  ```bash
  git ls-remote https://github.com/TommLawrence/Google-Agent-Harness-on-PLE.git main
  # must print 682690b8b620934ad18b56a777f4a260c8a7c79c
  git clone <repo> ../google-harness && git -C ../google-harness rev-parse HEAD
  # must equal 682690b…
  ```
- If the hash does not match, STOP and resolve the discrepancy (unpushed
  work, a newer local commit, or the FEEDBACK.md watcher's commits) before
  running any evaluation against the wrong tree.

## Machine readiness gate (before any evaluation)

The official guidance sizes each parallel task slot for the largest selected
task: **~6 CPUs and 16.25 GB RAM for the full dev set** (per task container
2–4 CPUs / 4–12 GB, plus 2 CPUs / 4.25 GB for its serve and egress
containers; see eval/README.md). Disk: ~10 GB for base images plus world
images (12 rubric tasks carry ~17 GB images that can take up to an hour to
build on first use).

- [ ] CPU count and RAM meet the slot size for what you intend to run
- [ ] Docker Desktop / daemon resource limits raised to match (CPUs, memory)
- [ ] Disk free ≥ ~10 GB images + headroom for world images and runs/
- [ ] `docker` works unprivileged; `uv` and Harbor 0.20.0 installed
- [ ] Linux/WSL2 with LF line endings (the kit rejects CRLF)

**The ThinkPad X250 (2 cores / 8 GB) is NOT suitable for the full
evaluation** — it cannot fund one full-size slot, and a run squeezed below
the slot size risks harness timeouts that count against results. Use it for
`kit validate`, code review, and the offline suite only. Run the real
evaluation on a machine that passes this gate.

## Step 1 — schema-only validation (no Docker needed yet)

```bash
git clone <repo> ../google-harness        # AFTER the step-0 hash check
cd ple                                    # the competition repository
uv run python -m kit validate ../google-harness
```
`kit validate` checks `submission.yaml` only (schema, field rules) and needs
no Docker or model setup. Fix anything it reports before continuing.

## Step 2 — Hugging Face dataset access (needs internet, once)

Tasks come from the gated dataset
[`google/physical-engineerings-last-exam`](https://huggingface.co/datasets/google/physical-engineerings-last-exam):
1. Sign in on HF and request access to the dataset from that account.
2. Create a READ token and export it as `HF_TOKEN` (or run
   `uv run hf auth login` once).
3. The FIRST task download requires internet access to huggingface.co;
   tasks are then cached under `~/.cache/plebench/tasks/<commit>/`, and
   later runs work offline.

## Step 3 — model proxy for the sponsored model

`kit check` for a `google-cloud/` model requires the proxy variables
(`MODEL_PROXY_URL`, `MODEL_PROXY_KEY`) configured BEFORE the build/model
stages — the model stage sends a real prompt through the egress proxy and
cannot pass without them (see kit/README.md). Start the local proxy per the
competition repo's `kit/model_proxy/start.sh` (`VERTEXAI_PROJECT`,
`MODEL_PROXY_URL`, `MODEL_PROXY_KEY`), authenticating first:

```bash
gcloud auth application-default login
```

**Cost language, corrected:** these local proxy calls are real Vertex AI
calls **billed to your Google Cloud project**. Google's free
sponsored-model evaluation applies to the OFFICIAL evaluation (Stage 2 on
the competition's infrastructure), not to local Vertex development calls.
Budget accordingly and keep local experiments to subsets.

Note: `eval/run.py --env-file local.env` is the kit's supported way to hand
local settings (proxy variables, `HF_TOKEN`) to a run without committing
them. Never put them in `submission.yaml`'s `env` (secrets are forbidden
there) and never commit a `.env` here — the repo is public.

## Step 4 — full kit check (Docker required from here on)

```bash
uv run python -m kit check ../google-harness --cancel-test
```
Expected: stages pass in order (files → build → model → listen → smoke →
mcp → cancel), BUT the final "ready to submit" verdict is blocked by the
stale `artifacts/` (step 0's digest note) until a fresh dev run is exported
and committed in step 7. That is the expected state at this point.

## Step 5 — the decision point: enable the orchestrator (the correct way)

**`export PLE_ORCHESTRATOR=1` in your host shell does NOT reach the serve
container.** The kit constructs the serve environment from the model
variables and `submission.yaml`'s `env` mapping. Activation is therefore a
committed configuration change:

```yaml
# submission.yaml
env:
  PLE_ORCHESTRATOR: "1"        # quoted string — unquoted 1 parses as an int
```

- Commit `submission.yaml` with the flag BEFORE the evaluated run; the kit
  builds from the committed tree, and `artifacts/` must be produced by the
  same configuration that will be submitted.
- Run the one-task experiment (step 6), then the full dev run (step 7) with
  `"1"` committed.
- If you decide to revert to `"0"` (single-run path), commit that change and
  run the FINAL full evaluation with the reverted configuration — the
  submitted artifacts must come from exactly the code you submit.

## Step 6 — one-task experiment (does not touch the baseline artifacts)

```bash
uv run python eval/run.py ../google-harness \
    --tasks data-center-site-model --trials 1 --no-export
```

- `--trials 1` — one task at a time, one resource slot.
- `--no-export` — the run leaves `artifacts/` untouched; a partial
  experimental export would otherwise overwrite deliverables of the
  committed baseline.
- Output goes to `runs/<name>-<time>/` — inspect THERE, not in `artifacts/`.
- **Confirm the orchestrator actually activated inside the serve container**:
  the trial log/serve log must show the role lines the orchestrator emits —
  `[planner] starting with a … s budget`, `[worker] …`, `[verifier] …` — and
  the done/final message must begin "Orchestrated run (planner -> worker →
  verifier)". If you see none of those lines, the flag did not reach the
  serve container (step 5 not committed, or run before the change) — fix
  that before spending hours on a full run.

## Step 7 — full dev run and artifact export

```bash
uv run python eval/run.py ../google-harness       # exports artifacts/
uv run python eval/run.py --artifacts ../google-harness   # offline re-grade check
python -m kit artifacts ../google-harness         # structural validation
```
Compare per-task rewards against the baseline
(docs/BASELINE_FAILURE_ANALYSIS.md lists the four zero tasks and the low
band — those are the targets). If the orchestrated mean is NOT better,
commit `PLE_ORCHESTRATOR: "0"` and re-run the FULL evaluation with the
reverted configuration (step 5's rule), then keep or drop the modules
deliberately.

## Step 8 — commit artifacts together, then submit

```bash
git add -A && git commit -m "dev artifacts (orchestrated run)" && git push
```
`artifacts/` must be in the SAME commit as the harness that produced it
(the manifest hashes harness files; the portal refuses mismatched trees).
Verify with `python -m kit artifacts ../google-harness` before pushing.

Submission itself: follow the CURRENT official Writeups/submission
instructions on the Kaggle competition page
(<https://www.kaggle.com/competitions/physical-engineerings-last-exam>,
Writeups/submission tab) and the agents page
(<https://ple-bench.com/competition/agents>). Queue and turnaround rules
stated anywhere else (including earlier notes in this repo) are unverified —
treat the official pages as the only source.

## Risk register (honest)

| Risk | Mitigation |
|---|---|
| Unpushed commits cloned as stale tree | Step 0's `ls-remote` hash gate before any clone |
| Orchestrator flag not reaching the serve container | Only `submission.yaml` `env` forwards; step 6 verifies activation from the trial log before long runs |
| Stale artifacts blocking readiness/submission | Expected until step 7 exports fresh artifacts; `kit artifacts` + `--artifacts` re-grade before pushing |
| Verifier eats too much time on 6 h tasks | Budgets are hard-capped per role; the wrap-up reserve is checked before every role start; loopback-tested |
| Planner JSON unparseable on odd instructions | Deterministic contract extraction runs regardless; the worker gets the raw plan text as a note |
| Resource-starved run on the X250 | Readiness gate forbids full evaluation on 2-core/8-GB machines; use `--trials 1` and subsets elsewhere |
| Local Vertex costs surprise you | Step 3's cost note: local proxy calls bill your GCP project; free sponsored evaluation is official Stage 2 only |
| `ifcopenshell`-style in-container deps for future tasks | Unchanged from baseline behavior (the worker uses whatever the task container provides) |

## Fallback behaviour (unchanged)

If the orchestrated path misbehaves at any point: revert `submission.yaml`'s
`env` to `PLE_ORCHESTRATOR: "0"` (commit the change), re-verify with the
step-6 experiment that the single-run path is active, and re-run. The
single-run path is the submission-template baseline and is fully
offline-test-covered; the orchestrated modules remain in the tree and can be
re-enabled after a fix.

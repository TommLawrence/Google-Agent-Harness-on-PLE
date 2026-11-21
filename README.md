# Submission template: Pi on Claude Opus 5.5

A working entry for the Physical-Engineering’s Last Exam harness competition: the [Pi](https://github.com/badlogic/pi-mono) coding agent calling `google-cloud/claude-opus-5-5`.

## Use it

1. Click **Use this template**. Public works. Make it private if you add a `.env` with API keys or want to keep your code to yourself, and install the [PLE-submission](https://github.com/apps/ple-submission) app on it.
2. Clone it next to the competition repository and set `name` in `submission.yaml`.
3. From the competition repository, check the entry and run the dev set (a `google-cloud` model needs a local proxy, see `docs/models.md`):
   ```bash
   uv run python -m kit check ../your-repo
   uv run python eval/run.py ../your-repo     # writes artifacts/
   ```
4. Commit everything, `artifacts/` included, push, and submit the commit on the competition site.

`artifacts/` already holds a dev-set run of Pi on Claude Opus 5.5 over all 29 dev tasks, so the template can be submitted unchanged. Once you change anything, `name` included, step 3 replaces it with your harness's run.

## Local development (no Docker)

The harness intelligence lives in `ple_harness/` (deliverable contract, document authority, evidence ledger, deterministic engineering solvers, and an optional Planner → Worker → Cold-Read Verifier orchestrator) and is fully testable offline:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests    # 186 offline tests, ~10 s
.venv/bin/python kaggle/ple_offline_lab.py        # offline lab + figures
```

No Docker, no network, no model calls needed — see `docs/LOCAL_DEVELOPMENT_NO_DOCKER.md`. The orchestrator activates only with `PLE_ORCHESTRATOR=1` and stays off until it passes a Docker-based kit check and dev run (`docs/DOCKER_VALIDATION_HANDOFF.md`); `artifacts/` is the immutable baseline of the template run (mean 0.4912 — see `docs/BASELINE_FAILURE_ANALYSIS.md`).

## Files

| File | Purpose |
|---|---|
| `submission.yaml` | The model, and the install and start commands |
| `serve.py` | The WebSocket endpoint: hands each task to `harness.solve` |
| `harness.py` | Runs Pi on each task. Edit `NOTE` (prompt), `THINKING`, or `UNKNOWN_MODEL` (limits for a model Pi doesn't know). `PLE_ORCHESTRATOR=1` switches to the Planner/Worker/Verifier orchestrator |
| `task-tools.ts` | Points Pi's `read`, `write`, `edit` and `bash` tools at the task container |
| `taskbridge.py` | The competition protocol |
| `ple_harness/` | Harness intelligence: contract validation, authority resolution, evidence ledger, solvers, orchestrator (`readonly-tools.ts` is its read-only toolset) |
| `tests/` | 186 offline tests (protocol, modules, solvers, orchestrator, e2e loopback) |
| `kaggle/` | CPU-only offline lab: runs the suite, validates solvers, generates figures and the writeup draft |
| `docs/` | Architecture, baseline failure analysis, no-Docker development, Docker handoff |
| `package.json`, `package-lock.json` | Pi 1.1.0 and every package it uses, pinned. To upgrade, edit `package.json`, run `npm install --package-lock-only`, commit both |
| `artifacts/` | The dev-set artifacts the portal grades, one folder per dev task (currently the template's baseline run — rerun after any harness change) |
| `AGENTS.md` | The full rules, for a coding agent working on your entry |

# Kaggle offline lab — PLE harness engineering & verification, CPU only

This directory is the offline laboratory for the harness: it runs the repo's
own test suite, validates the deterministic solvers against hand-checked
values, exercises the mock protocol clients, analyzes the committed baseline
artifacts, and prepares the analysis figures and the Stage-1 writeup draft.

**What it is NOT:** an official benchmark evaluation. No Docker, no Harbor,
no gated dataset, no model API, no network. Figures and text state their
provenance; nothing here may be presented as a competition result.

## Contents

| File | Purpose |
|---|---|
| `ple_offline_lab.py` | The lab as a plain script: `python kaggle/ple_offline_lab.py [tests|solvers|protocol|baseline|figures|writeup]` |
| `ple_offline_lab.ipynb` | The same steps as a Kaggle-friendly notebook |
| `figures/` | Generated figures (see provenance notes below) |
| `results/` | Lab outputs (e.g. `writeup_draft.md`) |

## Notebook setup (Kaggle)

1. Upload the repository as a Kaggle dataset (or a subset: `ple_harness/`,
   `tests/`, `kaggle/`, `artifacts/manifest.json`).
2. Settings: **Accelerator: None**, **Internet: Off**.
3. Run all cells. The notebook locates the repo root automatically and needs
   only the standard library plus `matplotlib` and `pandas` (preinstalled on
   Kaggle) for the analysis/figure cells; the test-suite cell itself needs
   nothing beyond the standard library.

## Local usage

```bash
python3 -m venv .venv && .venv/bin/pip install websockets==15.0.1 matplotlib
.venv/bin/python kaggle/ple_offline_lab.py            # everything
.venv/bin/python kaggle/ple_offline_lab.py tests      # offline suite only
```

## Figure provenance

- `figures/architecture.png` — drawn from the modules that actually exist
  (`ple_harness/`, `taskbridge.py`, `harness.py`, the role toolsets).
- `figures/dev_failure_breakdown.png` — computed from
  `artifacts/manifest.json` (the immutable baseline dev-set run); the caption
  records the harness commit and mean reward it came from.
- `figures/score_vs_cost.png` — **deliberately a pending-data placeholder**:
  the baseline trajectories carry no per-task token/runtime telemetry, and
  inventing costs or scores is forbidden. The pipeline is ready; a fresh
  official evaluation supplies the real data.

## Writeup

`results/writeup_draft.md` is the Stage-1 draft. Every claim that lacks
evidence (fresh evaluation deltas, cross-model comparisons, cost figures) is
a marked `<PLACEHOLDER>`, not a number. Fill them only from real evaluations.

## Data handling

Never upload raw competition task files, hidden-test material, or full
trajectories to a public notebook or dataset. This lab reads only aggregate
reward data from `artifacts/manifest.json` plus synthetic fixtures, and keeps
everything else inside the authorized competition boundary.

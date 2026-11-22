#!/usr/bin/env python3
"""PLE offline engineering lab — CPU only, no Docker, no network, no model API.

Runs the harness's offline test suite, validates the deterministic solvers
against hand-checked values, exercises the mock protocol/MCP clients,
analyzes the committed baseline artifacts (if present), and generates the
analysis figures and a Stage-1 writeup draft with explicit evidence gaps.

This lab is for offline engineering and verification. It is NOT an official
benchmark evaluation and must never be presented as one.

Usage (from the repository root, or a Kaggle dataset containing it):
    python kaggle/ple_offline_lab.py            # all steps
    python kaggle/ple_offline_lab.py tests      # just the test suite
    python kaggle/ple_offline_lab.py figures    # just the figures pipeline
    python kaggle/ple_offline_lab.py writeup    # just the writeup draft
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIGURES_DIR = REPO_ROOT / "kaggle" / "figures"
RESULTS_DIR = REPO_ROOT / "kaggle" / "results"

# Steps ----------------------------------------------------------------------

def run_test_suite() -> dict:
    """Runs the offline test suite and returns a pass/fail matrix by module."""
    loader = unittest.TestLoader()
    suite = loader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
    result = runner.run(suite)

    matrix: dict[str, dict[str, int]] = {}
    for case, _ in getattr(result, "failures", []) + getattr(result, "errors", []):
        module = case.__class__.__module__
        matrix.setdefault(module, {"run": 0, "failed": 0})["failed"] += 1
    for case, _ in getattr(result, "skipped", []):
        module = case.__class__.__module__
        matrix.setdefault(module, {"run": 0, "failed": 0})
    summary = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.testsRun - len(result.failures) - len(result.errors),
        "matrix": matrix,
    }
    print(f"offline suite: {summary['passed']}/{summary['tests_run']} passed "
          f"({summary['failures']} failures, {summary['errors']} errors)")
    for module, counts in sorted(matrix.items()):
        print(f"  {module}: {counts}")
    if not matrix:
        print("  (no failures — matrix empty)")
    return summary


def validate_solvers() -> dict:
    """Spot-checks the deterministic solvers against hand-checked values."""
    from ple_harness.solvers import civil, electrical, mechanical, structural
    from ple_harness.solvers.units import as_fraction, ceil_takeoff, convert

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    check("1728 in3 = 1 ft3 (exact)", convert(1728, "in3", "ft3") == 1.0)
    check("5 kip = 5000 lbf (exact)", convert(5, "kip", "lbf") == 5000.0)
    check("25% -> 0.25 fraction", as_fraction(25, "percent") == 0.25)
    check("takeoff ceil(10,6) = 2 packages", ceil_takeoff(10, 6) == 2)
    limit = structural.deflection_limit(20, "ft", 360)
    check("L/360 of 20 ft", abs(limit.value - 20 / 360) < 1e-12)
    settlement, regime = civil.consolidation_settlement(
        4, "m", 0.9, 0.36, 0.06, 100, 100, 200, "kPa")
    check("Terzaghi NC settlement (0.2281 m)",
          abs(settlement.value - 0.2281490) < 1e-4, regime)
    head = mechanical.darcy_weisbach_head_loss(0.02, 100, 0.1, 2.0)
    check("Darcy-Weisbach (4.079 m)", abs(head.value - 4.07886) < 1e-3)
    drop = electrical.voltage_drop(20, 100, "ft", 0.5, "ohm/1000ft", 120, 1)
    check("1ph voltage drop (2.0 V)", abs(drop.volts - 2.0) < 1e-9)
    npv = mechanical.lifecycle_npv(50000, 5000, 0.05, 25)
    check("25-year NPV (120,469.72)", abs(npv.present_worth - 120469.72) < 1.0)
    ok = all(c["ok"] for c in checks)
    print(f"solvers: {'all hand-checked values PASS' if ok else 'FAILURES PRESENT'}")
    return {"checks": checks, "passed": ok}


def exercise_mock_protocol() -> dict:
    """Exercises the mock evaluator/relay/MCP clients (no network needed)."""
    import asyncio
    from ple_harness import orchestrator
    from tests._fakes import FakeWS, MockEvaluator
    import taskbridge

    async def flow():
        ws = FakeWS()
        session = taskbridge.Session(ws, {"type": "task", "instruction": "demo",
                                          "workdir": "/app", "limits": {},
                                          "mcp_servers": []})
        evaluator = MockEvaluator(ws, lambda req: {
            "ok": True, "exit_code": 0, "output": "hi", "truncated": False,
            "timed_out": False}).start()
        reader = asyncio.get_running_loop().create_task(session._read())
        result = await session.exec("echo hi")
        block = orchestrator.parse_json_block('Sure: {"a": 1}')
        await evaluator.stop()
        reader.cancel()
        return result["exit_code"], block

    code, parsed = asyncio.run(flow())
    print(f"mock protocol round trip: exec exit_code={code}, "
          f"JSON block parsed={parsed}")
    return {"exec_ok": code == 0, "json_ok": parsed == {"a": 1}}


def analyze_baseline() -> dict | None:
    """Reads the committed artifacts manifest (immutable baseline evidence)."""
    manifest_path = REPO_ROOT / "artifacts" / "manifest.json"
    if not manifest_path.exists():
        print("no artifacts/manifest.json — baseline analysis skipped")
        return None
    manifest = json.loads(manifest_path.read_text())
    tasks = manifest.get("tasks", {})
    rewards = {name: info.get("reward", 0.0) for name, info in tasks.items()}
    families = {"zero": [], "low": [], "mid": [], "high": []}
    for name, reward in rewards.items():
        if reward == 0.0:
            families["zero"].append(name)
        elif reward < 0.35:
            families["low"].append(name)
        elif reward < 0.7:
            families["mid"].append(name)
        else:
            families["high"].append(name)
    mean = sum(rewards.values()) / len(rewards) if rewards else 0.0
    print(f"baseline: {len(rewards)} tasks, mean {mean:.4f} "
          f"(zero: {len(families['zero'])}, low: {len(families['low'])}, "
          f"mid: {len(families['mid'])}, high: {len(families['high'])})")
    return {"mean": mean, "count": len(rewards), "families": families,
            "rewards": rewards,
            "harness_commit": manifest.get("harness", {}).get("commit")}


# Figures --------------------------------------------------------------------

def _matplotlib():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        print("matplotlib unavailable: figures skipped (the pipeline still runs)")
        return None


def figure_architecture() -> bool:
    plt = _matplotlib()
    if plt is None:
        return False
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title("PLE harness architecture (as implemented — see docs/ARCHITECTURE.md)")

    def box(x, y, w, h, label, color):
        ax.add_patch(plt.Rectangle((x, y), w, h, fill=True, color=color,
                                   alpha=0.85, zorder=2))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
                fontsize=9, color="white", zorder=3)

    box(0.3, 7.6, 2.4, 1.6, "Evaluator\n(mock in tests)", "#444444")
    box(3.4, 7.6, 2.4, 1.6, "serve.py\ntaskbridge.run", "#1f6feb")
    box(6.5, 7.6, 3.2, 1.6, "harness.solve\n(PLE_ORCHESTRATOR switch)", "#1f6feb")
    box(0.3, 5.2, 2.9, 1.7, "PLANNER\nread-only context\nreadonly-tools.ts", "#8250df")
    box(3.6, 5.2, 2.9, 1.7, "WORKER\nfull tools\ntask-tools.ts", "#1a7f37")
    box(6.9, 5.2, 2.8, 1.7, "COLD-READ VERIFIER\nread-only + digest check", "#cf222e")
    box(0.3, 3.0, 2.9, 1.5, "contract.py\ndeliverable contract", "#9e6a03")
    box(3.6, 3.0, 2.9, 1.5, "authority.py + evidence.py\nsources, revisions, ledger", "#9e6a03")
    box(6.9, 3.0, 2.8, 1.5, "context.py\nprefix, trimming, cache", "#9e6a03")
    box(0.3, 0.7, 4.4, 1.5, "solvers/: units, structural, civil,\nelectrical, mechanical", "#0550ae")
    box(5.2, 0.7, 4.5, 1.5, "taskbridge.py: protocol, Relay,\nMCP client, run_process", "#0550ae")

    def arrow(x1, y1, x2, y2):
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle="->", color="#555555", lw=1.4))

    arrow(2.7, 8.4, 3.4, 8.4)
    arrow(5.8, 8.4, 6.5, 8.4)
    arrow(7.6, 7.6, 5.0, 6.9)
    arrow(4.7, 5.2, 4.9, 4.5)
    arrow(8.3, 5.2, 8.3, 4.5)
    arrow(1.7, 5.2, 1.7, 4.5)
    fig.savefig(FIGURES_DIR / "architecture.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {FIGURES_DIR / 'architecture.png'} (provenance: module graph above)")
    return True


def figure_dev_failure_breakdown(baseline: dict | None) -> bool:
    plt = _matplotlib()
    if plt is None:
        return False
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5))
    if baseline:
        labels = ["zero (0.0)", "low (<0.35)", "mid (0.35-0.7)", "high (>=0.7)"]
        sizes = [len(baseline["families"][k]) for k in ("zero", "low", "mid", "high")]
        provenance = (f"source: artifacts/manifest.json, harness commit "
                      f"{baseline['harness_commit'][:12]}…, {baseline['count']} dev tasks, "
                      f"mean {baseline['mean']:.4f}")
    else:
        labels, sizes = ["no data"], [1]
        provenance = "no baseline manifest found — placeholder"
    ax.bar(labels, sizes, color=["#cf222e", "#eac54f", "#1f6feb", "#1a7f37"])
    ax.set_ylabel("tasks")
    ax.set_title("Dev-set reward distribution (baseline, NOT a new evaluation)")
    fig.text(0.5, 0.01, provenance, ha="center", fontsize=8)
    fig.savefig(FIGURES_DIR / "dev_failure_breakdown.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {FIGURES_DIR / 'dev_failure_breakdown.png'} ({provenance})")
    return True


def figure_score_vs_cost() -> bool:
    """Deliberately a pending-data placeholder: no usage telemetry exists in
    the baseline trajectories' final_metrics, and inventing costs is forbidden."""
    plt = _matplotlib()
    if plt is None:
        return False
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.text(0.5, 0.55, "PENDING DATA", ha="center", va="center",
            fontsize=28, color="#999999", transform=ax.transAxes)
    ax.text(0.5, 0.38, "score-vs-cost requires per-task token and runtime\ntelemetry "
            "from a fresh official evaluation.\nThe baseline trajectories do not carry "
            "usage metrics;\nno numbers are invented here.",
            ha="center", va="center", fontsize=10, transform=ax.transAxes)
    ax.set_title("score vs cost — pipeline ready, awaiting real telemetry")
    ax.set_xticks([])
    ax.set_yticks([])
    fig.savefig(FIGURES_DIR / "score_vs_cost.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {FIGURES_DIR / 'score_vs_cost.png'} (pending-data placeholder)")
    return True


# Writeup --------------------------------------------------------------------

WRITEUP = """# Stage 1 Writeup draft (PLACEHOLDERS MARKED — no results invented yet)

## 1. Insight and failure analysis

The baseline (Pi 1.1.0 calling google-cloud/claude-opus-5-5, mean <BASELINE_MEAN>)
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
"""


def write_writeup() -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    baseline = analyze_baseline()
    text = WRITEUP
    if baseline:
        text = text.replace("<BASELINE_MEAN>", f"{baseline['mean']:.4f}")
    else:
        text = text.replace("<BASELINE_MEAN>", "unknown — no manifest")
    target = RESULTS_DIR / "writeup_draft.md"
    target.write_text(text)
    print(f"wrote {target} (placeholders preserved deliberately)")
    return target


# Entry ----------------------------------------------------------------------

STEPS = {
    "tests": run_test_suite,
    "solvers": validate_solvers,
    "protocol": exercise_mock_protocol,
    "baseline": analyze_baseline,
    "figures": lambda: [
        figure_architecture(),
        figure_dev_failure_breakdown(analyze_baseline()),
        figure_score_vs_cost(),
    ],
    "writeup": write_writeup,
}


def main(argv: list[str]) -> int:
    selected = argv[1:] or ["tests", "solvers", "protocol", "baseline",
                            "figures", "writeup"]
    status = 0
    for step in selected:
        if step not in STEPS:
            print(f"unknown step: {step} (available: {', '.join(STEPS)})")
            status = 2
            continue
        print(f"=== {step} ===")
        result = STEPS[step]()
        if step == "tests" and isinstance(result, dict) and (
                result["failures"] or result["errors"]):
            status = 1
        if step == "solvers" and isinstance(result, dict) and not result["passed"]:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

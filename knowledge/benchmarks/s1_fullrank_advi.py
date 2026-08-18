"""S1: benchmark full-rank ADVI against mean-field ADVI and NUTS.

Measures, per inference method, the two quantities the roadmap row asks for —
fit time and credible-interval width — plus the ones needed to interpret them:
whether the interval covers the planted truth, and whether the optimizer
converged (`fit_quality`). The comparison runs on three suites:

- **step** — the calibration suite's DGP (mirrors
  `tests/test_calibration.py::_planted_step_world`): a stationary parent that
  steps up. Easy geometry; establishes the baseline cost of each method.
- **drift** — a parent that drifts as a slow random walk, so the trend and the
  parent compete for the same variance. This is the β-vs-trend posterior ridge
  of `knowledge/advi_vs_nuts_in_breakdown.md` §2 — the geometry mean-field
  ADVI cannot represent and the reason S1 exists. The step suite does *not*
  exercise it (S17 records the same blind spot in the coverage test).
- **whitecube** — the three probabilistic nodes of the committed White Cube
  demo tree (`sessions`, `trials_started`, `new_subscriptions`), fitted from
  the parquet snapshots exactly as the deployed demo fits them. Real
  seasonality, real windows, weekly grain.

The compared quantity is the `beta_raw` posterior (the coefficient in business
units), not the RCA contribution interval: a contribution blends the
coefficient posterior with the block bootstrap on window means, and S1 is
about the coefficient term. Where the truth is planted, coverage is of the
true beta.

Run the **nsweep** suite first: it sweeps full-rank's optimizer steps
(20k/40k/80k/160k) on the drift worlds to find where its ELBO actually
settles — at the engine default of 20k it does not converge on these models —
then pass that step count to the main suites via --fullrank-n.

Usage:
    uv run python knowledge/benchmarks/s1_fullrank_advi.py \
        [--suite step|drift|whitecube|nsweep|all] [--worlds 10] \
        [--fullrank-n 20000] [--out results.jsonl]

Wall-clock, measured on Apple Silicon: ~13 min for `all` (full-rank
dominates), ~9 min for `nsweep` at 5 worlds, ~15 min for `driftlong` at 3
worlds (each 830-period full-rank fit is ~4 min). Every fit is seeded;
results are reproducible per platform, and the first fit of each (suite,
method) pays the PyTensor compile — medians are reported for that reason.
Findings live in `knowledge/s1_fullrank_advi_benchmark.md`; the roadmap S1 row
is the source of truth for the decision.
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)  # runnable from anywhere, without an editable install

METHODS = ["advi", "fullrank_advi", "nuts"]
N = 130
TRUE_BETA = 0.5

PROB_YAML = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""


def _frame(cols, n=N):
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), **cols})


def step_world(seed):
    """Mirror of tests/test_calibration.py::_planted_step_world (lag=0)."""
    rng = np.random.default_rng(seed)
    x = 100.0 + rng.normal(0, 4.0, N)
    x[91:] += 30.0
    y = TRUE_BETA * x + rng.normal(0, 1.0, N)
    return _frame({"x": x, "y": y})


def drift_world(seed, n=N):
    """The ridge: x drifts as a slow random walk, so 'the parent caused the
    drift' and 'the level drifted on its own' both fit — the posterior over
    (beta, trend) is a ridge, and the honest beta interval is wide."""
    rng = np.random.default_rng(seed)
    x = 100.0 + np.cumsum(rng.normal(0, 0.8, n))
    y = TRUE_BETA * x + rng.normal(0, 1.0, n)
    return _frame({"x": x, "y": y}, n=n)


def driftlong_world(seed):
    """The drift world at 830 daily periods — the bundled demo's window, and
    the size where the trace cache's 13.4 MB/entry was measured (roadmap
    2.18). The model carries one latent trend state per period, so this is
    where NUTS gets expensive and ADVI's speed advantage actually lives; a
    default cannot be chosen from 130-period timings alone."""
    return drift_world(seed, n=830)


def fit_record(dag, data, target, method, seed, suite, world, vi_n=20_000):
    """One fit -> one flat record per parent."""
    from breakdown.engine.model import fit_metric

    t0 = time.perf_counter()
    fit = fit_metric(
        dag,
        data,
        target,
        draws=1000,
        inference_method=method,
        random_seed=seed,
        vi_iterations=vi_n,
    )
    elapsed = time.perf_counter() - t0

    records = []
    arr = fit.trace.posterior["beta_raw"].values.reshape(-1, len(fit.parents))
    for i, parent in enumerate(fit.parents):
        s = arr[:, i]
        lo, hi = np.percentile(s, [2.5, 97.5])
        records.append(
            {
                "suite": suite,
                "world": world,
                "target": target,
                "parent": parent,
                "method": method,
                "vi_n": vi_n if method != "nuts" else None,
                "seconds": round(elapsed, 2),
                "beta_mean": float(s.mean()),
                "beta_sd": float(s.std()),
                "ci_lo": float(lo),
                "ci_hi": float(hi),
                "ci_width": float(hi - lo),
                "covered": bool(lo <= TRUE_BETA <= hi) if suite != "whitecube" else None,
                "fit_quality": fit.diagnostics["fit_quality"],
            }
        )
    return records


def run_synthetic(suite, worlds, fullrank_n):
    from breakdown.parser import Parser

    make = {"step": step_world, "drift": drift_world, "driftlong": driftlong_world}[suite]
    out = []
    dag = Parser(PROB_YAML).dag
    for method in METHODS:
        vi_n = fullrank_n if method == "fullrank_advi" else 20_000
        # Untimed warm-up so the PyTensor compile lands outside the medians.
        fit_record(dag, make(999), "y", method, 999, suite, -1, vi_n=vi_n)
        for k in range(worlds):
            recs = fit_record(dag, make(1000 + k), "y", method, k, suite, k, vi_n=vi_n)
            out.extend(recs)
            print(
                f"  {suite}/{method} world {k}: "
                f"{recs[0]['seconds']}s width={recs[0]['ci_width']:.4f} "
                f"covered={recs[0]['covered']} {recs[0]['fit_quality']}"
            )
    return out


def run_nsweep(worlds):
    """Where does full-rank converge? Sweep optimizer steps on the drift
    worlds (the geometry that motivates S1) and compare each width to NUTS on
    the same world. Mean-field at its shipped 20k rides along as the anchor."""
    from breakdown.parser import Parser

    out = []
    dag = Parser(PROB_YAML).dag
    for k in range(worlds):
        frame = drift_world(1000 + k)
        out.extend(fit_record(dag, frame, "y", "nuts", k, "nsweep", k))
        out.extend(fit_record(dag, frame, "y", "advi", k, "nsweep", k))
        for n in (20_000, 40_000, 80_000, 160_000):
            recs = fit_record(dag, frame, "y", "fullrank_advi", k, "nsweep", k, vi_n=n)
            out.extend(recs)
            print(
                f"  nsweep world {k} fullrank n={n}: {recs[0]['seconds']}s "
                f"width={recs[0]['ci_width']:.4f} {recs[0]['fit_quality']}"
            )
    return out


def run_whitecube(fullrank_n):
    """Fit the demo tree's probabilistic nodes from the committed snapshots,
    with no data provider — the same hermetic setup as
    tests/test_white_cube_demo.py."""
    demo = os.path.join(REPO, "demo")
    snapshots = os.path.join(demo, ".breakdown", "snapshots")
    if not os.path.isdir(snapshots) or not os.listdir(snapshots):
        print("whitecube: snapshots absent (run `make -C demo snapshots`); skipping")
        return []
    os.environ.update(
        BREAKDOWN_TREE=os.path.join(demo, "white_cube_tree.yml"),
        BREAKDOWN_START_DATE="2024-06-01",
        BREAKDOWN_END_DATE="2026-07-30",
        BREAKDOWN_SNAPSHOT_DIR=snapshots,
        WHITE_CUBE_DBT_PROJECT="/nonexistent/white-cube-has-no-provider",
    )
    from fastapi.testclient import TestClient

    from breakdown.api.main import app, load_tree

    out = []
    with TestClient(app):
        (tree_id,) = [t for t in app.state.trees]
        tree = app.state.trees[tree_id]
        load_tree(tree)
        if tree.load_error:
            raise RuntimeError(f"white cube load failed: {tree.load_error}")
        dag = tree.parser.dag
        targets = [
            n
            for n in dag.nodes
            if list(dag.predecessors(n)) and dag.nodes[n]["definition"].formula is None
        ]
        print(f"  whitecube probabilistic nodes: {targets}")
        for method in METHODS:
            vi_n = fullrank_n if method == "fullrank_advi" else 20_000
            for target in targets:
                recs = fit_record(dag, tree.data, target, method, 0, "whitecube", 0, vi_n=vi_n)
                out.extend(recs)
                for r in recs:
                    print(
                        f"  whitecube/{method} {target}<-{r['parent']}: "
                        f"{r['seconds']}s width={r['ci_width']:.5f} {r['fit_quality']}"
                    )
    return out


def summarize(records):
    """Per (suite, method): median time, mean width, width ratio vs NUTS,
    coverage, suspect rate."""
    import collections

    by = collections.defaultdict(list)
    for r in records:
        label = r["method"]
        if r["suite"] == "nsweep" and r["method"] == "fullrank_advi":
            label = f"fullrank@{r['vi_n'] // 1000}k"
        by[(r["suite"], label)].append(r)
    # NUTS width per (suite, target, parent, world) for the ratio.
    nuts_width = {
        (r["suite"], r["target"], r["parent"], r["world"]): r["ci_width"]
        for r in records
        if r["method"] == "nuts"
    }
    print(
        f"\n{'suite':10} {'method':14} {'median_s':>8} {'mean_width':>10} "
        f"{'width/nuts':>10} {'coverage':>8} {'suspect':>7}"
    )
    for suite, method in sorted(by):
        rs = by[(suite, method)]
        ratios = [
            r["ci_width"] / nuts_width[(r["suite"], r["target"], r["parent"], r["world"])]
            for r in rs
            if (r["suite"], r["target"], r["parent"], r["world"]) in nuts_width
        ]
        cov = [r["covered"] for r in rs if r["covered"] is not None]
        print(
            f"{suite:10} {method:14} "
            f"{np.median([r['seconds'] for r in rs]):8.1f} "
            f"{np.mean([r['ci_width'] for r in rs]):10.4f} "
            f"{np.mean(ratios) if ratios else float('nan'):10.2f} "
            f"{(sum(cov) / len(cov)) if cov else float('nan'):8.2f} "
            f"{np.mean([r['fit_quality'] == 'suspect' for r in rs]):7.2f}"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--suite",
        default="all",
        choices=["step", "drift", "driftlong", "whitecube", "nsweep", "all"],
    )
    ap.add_argument("--worlds", type=int, default=10)
    ap.add_argument(
        "--fullrank-n",
        type=int,
        default=20_000,
        help="optimizer steps for fullrank_advi in the main suites (pick from the nsweep results)",
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    records = []
    if args.suite in ("step", "all"):
        records += run_synthetic("step", args.worlds, args.fullrank_n)
    if args.suite in ("drift", "all"):
        records += run_synthetic("drift", args.worlds, args.fullrank_n)
    if args.suite == "driftlong":
        records += run_synthetic("driftlong", args.worlds, args.fullrank_n)
    if args.suite == "nsweep":
        records += run_nsweep(args.worlds)
    if args.suite in ("whitecube", "all"):
        records += run_whitecube(args.fullrank_n)

    if args.out:
        with open(args.out, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        print(f"\nwrote {len(records)} records to {args.out}")
    summarize(records)


if __name__ == "__main__":
    main()

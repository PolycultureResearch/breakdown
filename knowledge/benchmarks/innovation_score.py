"""Prototype: rank root causes by each node's own *innovation*, not by its share
of a window-over-window gap.

Design and findings: `knowledge/innovation_rca_design.md`. This is a
measurement script, not engine code — it reuses the engine's fits
(`fit_rca_node`) and data plumbing unchanged and adds only the scoring.

What it computes, per node in the target's ancestor scope:

1. **The node's own window.** The target's analysis window, shifted back by
   the lag on every edge between the node and the target (the engine's
   `parent_windows`, composed along the path).
2. **A fit that ends where that window starts.** Not at the target's
   `analysis_start`: a lagged ancestor's window precedes it, and a fit that
   ends at `analysis_start` has trained on the periods it is asked to judge.
3. **The innovation.** `actual − predicted`, per period, where `predicted` is
   the fitted model's posterior predictive *given the parents' actual values*:
   `alpha + seasonal + forward-simulated level + beta_raw · parents + noise`.
   A source node's prediction is its forecast; a formula node's is the
   identity plus its (usually zero) forecast residual. Summarized as `z` —
   the window-mean innovation over the predictive sd of that mean — and a
   two-sided predictive tail probability.
4. **Its contribution to the target.** Remove that one node's innovation
   (set the node to what was predicted), push the change down through the
   structural equations — formulas exactly, learned edges through the
   posterior-mean `beta_raw`, every other node keeping its own innovation —
   and read how far the target moves. `departure` is the same with every
   innovation removed: actual minus what the whole tree expected.

Then it scores the ranking against the planted root cause, beside the two
rankings available today: the engine's `ranked_causes` and the naive "largest
relative change among the ancestors".

Usage (needs the demo snapshots — `make -C demo snapshots`):
    uv run python knowledge/benchmarks/innovation_score.py            # every story
    uv run python knowledge/benchmarks/innovation_score.py A B-onset  # some

Null calibration (design doc §6.1) — every node over every anomaly-free
window, ~500 fits and ~40 minutes, resumable from the CSV it appends to:
    uv run python knowledge/benchmarks/innovation_score.py null out.csv
    uv run python knowledge/benchmarks/innovation_score.py null-summary out.csv

Trend experiments (roadmap S8). `--nodes a,b,c` restricts the null windows to
those nodes (the planted rows are always scored). `--trend level|local_linear`
forces that trend kind on every scored node, and `--slope-sigma X` sets the
slope innovation's prior scale (`0` = fixed drift), both by editing the loaded
definitions in memory — the tree file is not touched. One CSV per arm, then
compare them side by side:
    ... null level.csv --nodes sessions,signups --trend level
    ... null llt.csv   --nodes sessions,signups --trend local_linear
    ... null drift.csv --nodes sessions,signups --trend local_linear --slope-sigma 0
    ... null-compare level=level.csv llt=llt.csv drift=drift.csv
"""

import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
DEMO = os.path.join(REPO, "demo")
os.environ.update(
    BREAKDOWN_TREE=os.path.join(DEMO, "white_cube_tree.yml"),
    BREAKDOWN_START_DATE="2024-06-01",
    BREAKDOWN_END_DATE="2026-07-30",
    BREAKDOWN_SNAPSHOT_DIR=os.path.join(DEMO, ".breakdown", "snapshots"),
    WHITE_CUBE_DBT_PROJECT="/nonexistent/white-cube-has-no-provider",
)
os.environ.pop("BREAKDOWN_WARM", None)

import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from breakdown.engine import model as M  # noqa: E402
from breakdown.engine.rca import fit_rca_node, run_rca  # noqa: E402
from breakdown.formula import eval_formula  # noqa: E402
from breakdown.grains import (  # noqa: E402
    resample_up,
    shift_periods,
    snap_window,
    steps_between,
)
from breakdown.parser import TrendConfig  # noqa: E402

# The planted anomalies, from fake_companies' white_cube.ground_truth.json
# (demo/demos.yaml). `truth` is where each one *enters* this tree: the first
# tree metric in the anomaly's `affected_metrics`. The "tour" windows are the
# ones knowledge/demo_guided_tour.md walks a prospect through; the "-onset"
# variants start the analysis window on the anomaly's own first period, which
# is the window a monitor would hand over.
STORIES = {
    "A": dict(
        target="new_mrr",
        ana=("2026-02-09", "2026-03-08"),
        ref=("2026-01-05", "2026-02-01"),
        truth={"visit_signup_rate"},  # mobile_signup_cta_regression, from 2026-02-02
    ),
    "A-onset": dict(
        target="signups",
        ana=("2026-02-02", "2026-03-01"),
        ref=None,
        truth={"visit_signup_rate"},
    ),
    "B": dict(
        target="net_new_mrr",
        ana=("2026-05-11", "2026-06-07"),
        ref=("2026-03-16", "2026-04-12"),
        truth={"customer_churn_rate"},  # professional_churn_spike, from 2026-04-27
    ),
    "B-onset": dict(
        target="net_new_mrr",
        ana=("2026-04-27", "2026-05-24"),
        ref=None,
        truth={"customer_churn_rate"},
    ),
    "C": dict(
        target="signups",
        ana=("2025-03-10", "2025-04-06"),
        ref=("2025-02-03", "2025-03-02"),
        # latam_campaign_spend_ramp + latam_campaign_brazil_lift_*, from 2025-03-03
        truth={"marketing_spend", "visit_signup_rate"},
    ),
    "C-onset": dict(
        target="signups",
        ana=("2025-03-03", "2025-03-30"),
        ref=None,
        truth={"marketing_spend", "visit_signup_rate"},
    ),
    "D": dict(
        target="new_mrr",
        ana=("2025-08-11", "2025-09-07"),
        ref=("2025-07-07", "2025-08-03"),
        truth={"trial_activation_rate", "trial_days_active"},  # onboarding_revamp, 2025-08-04
    ),
    "D-onset": dict(
        target="trial_conversion_rate",
        ana=("2025-08-04", "2025-08-31"),
        ref=None,
        truth={"trial_activation_rate", "trial_days_active"},
    ),
}

MAX_DRAWS = 1000
SEED = 0


def load():
    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    client = TestClient(app)
    client.__enter__()
    client.get("/meta")
    st = app.state.trees[next(iter(app.state.trees))]
    assert st.loaded, st.load_error
    return st.parser.dag, st.data


def defn_of(dag, node):
    return dag.nodes[node]["definition"]


def force_trend(dag, nodes, kind, slope_sigma):
    """Force a trend kind on `nodes` (roadmap S8) by replacing each loaded
    definition's `trend` in memory. The level's own `sigma` is kept, so the
    arms differ in the slope and nothing else."""
    if kind is None:
        if slope_sigma is not None:
            raise SystemExit("--slope-sigma needs --trend local_linear")
        return
    for n in nodes:
        defn = defn_of(dag, n)
        fields = {"kind": kind}
        if defn.trend is not None:
            fields["sigma"] = defn.trend.sigma
        if slope_sigma is not None:
            fields["slope_sigma"] = slope_sigma
        defn.trend = TrendConfig(**fields)


def node_windows(dag, data, target, ana):
    """Each in-scope node's own [start, end] (inclusive calendar dates): the
    target's snapped window shifted back by the lag on every edge below it.
    A node reached along two paths with different total lags gets the hull."""
    scope = nx.ancestors(dag, target) | {target}
    snapped = snap_window(ana[0], ana[1], data.grain_of[target])
    windows = {target: (snapped.first_start, snapped.last_end)}
    for child in reversed([n for n in nx.topological_sort(dag) if n in scope]):
        start, end = windows[child]
        grain = data.grain_of[child]
        lags = defn_of(dag, child).lags
        for p in dag.predecessors(child):
            lag = lags.get(p, 0)
            s = shift_periods(start, -lag, grain)
            e = shift_periods(end + pd.Timedelta(days=1), -lag, grain) - pd.Timedelta(days=1)
            if p in windows:
                s, e = min(s, windows[p][0]), max(e, windows[p][1])
            windows[p] = (s, e)
    return scope, windows


def innovation(dag, data, fits, node, window):
    """Per-period expected value and innovation of `node` over `window`, from a
    fit that ends at the window's first period."""
    defn = defn_of(dag, node)
    grain = data.grain_of[node]
    parents = list(dag.predecessors(node))
    frame = data.fit_frame(node, parents, grain)
    in_win = (frame["date"] >= window[0]) & (frame["date"] <= window[1])
    dates = pd.DatetimeIndex(frame.loc[in_win, "date"])
    out = dict(node=node, grain=grain, dates=dates, status="ok", n=len(dates))
    if not len(dates):
        return {**out, "status": "no_periods_in_window"}

    lags = defn.lags
    shifted = {p: frame[p].shift(lags.get(p, 0)) for p in parents}
    is_formula = bool(defn.formula and parents)
    if is_formula:
        structural = eval_formula(defn.formula, {p: shifted[p].to_numpy(float) for p in parents})
        observed = (frame[node].to_numpy(float) - structural)[in_win.to_numpy()]
    else:
        observed = frame.loc[in_win, node].to_numpy(float)
    out["actual"] = float(np.nanmean(frame.loc[in_win, node].to_numpy(float)))

    fit_end = str(dates[0].date())
    key = (node, fit_end)
    if key not in fits:
        t0 = time.perf_counter()
        try:
            fits[key] = fit_rca_node(dag, data, node, fit_end)
        except Exception as exc:  # a per-node degrade, as in run_rca
            fits[key] = exc
        out["fit_seconds"] = time.perf_counter() - t0
    fit = fits[key]
    if isinstance(fit, Exception):
        # An identity that holds to rounding has no residual to model: its
        # innovation is the residual itself, expected to be zero.
        exact = is_formula and "zero variance" in str(fit)
        if not exact:
            # No fit, no expectation: the node's innovation is *unassessed*,
            # not zero. It carries no `e`, so it is never "removed", and the
            # story's departure excludes whatever surprise it holds.
            return {**out, "status": f"unassessed, fit failed: {fit}"[:110]}
        return {
            **out,
            "status": "exact_identity",
            "e": pd.Series(np.nan_to_num(observed), index=dates),
            "innovation": float(np.nanmean(observed)),
            "z": 0.0,
            "tail": None,
        }
    if fit.interventions:
        out["status"] = "ok (declared interventions ignored by this prototype)"

    post = fit.trace.posterior
    n_all = int(post.sizes["chain"] * post.sizes["draw"])
    idx = np.arange(0, n_all, max(1, n_all // MAX_DRAWS))
    rng = np.random.default_rng(SEED)
    t = steps_between(dates, fit.dates[0], grain)
    h = len(dates)

    def draws(name):
        return post[name].values.reshape(n_all, -1)[idx]

    # Normalized space: alpha + seasonal + the trend simulated forward from
    # its last fitted state (so the interval widens with the horizon — roadmap
    # S16) + observation noise. The engine's `simulate_trend_forward` walks a
    # local level on from its last state, and under a local linear trend
    # (roadmap S8) also carries the last fitted slope forward.
    pred = draws("alpha") + M._seasonal_draws(post, defn.seasonality, t)[idx]
    pred += M.simulate_trend_forward(post, h, rng, idx)
    noiseless = fit.y_mean + fit.y_std * pred
    pred = noiseless + fit.y_std * draws("sigma_obs") * rng.standard_normal((len(idx), h))
    beta_mean = {}
    beta_post = {}
    if fit.parents:
        beta_raw = draws("beta_raw")
        fitted_rows = frame["date"].isin(fit.dates).to_numpy()
        for i, p in enumerate(fit.parents):
            x = shifted[p].to_numpy(float)
            centred = (x[in_win.to_numpy()] - np.nanmean(x[fitted_rows]))[None, :]
            pred += beta_raw[:, i : i + 1] * centred
            noiseless += beta_raw[:, i : i + 1] * centred
            beta_mean[p] = float(beta_raw[:, i].mean())
            lo, hi = np.quantile(beta_raw[:, i], [0.03, 0.97])
            beta_post[p] = dict(
                mean=beta_mean[p], sd=float(beta_raw[:, i].std()), lo=float(lo), hi=float(hi)
            )

    expected = noiseless.mean(axis=0)
    e = observed - expected
    ok = np.isfinite(observed)
    if not ok.any():
        return {**out, "status": "undefined_over_window"}
    pred_mean = pred[:, ok].mean(axis=1)
    obs_mean = float(observed[ok].mean())
    sd = float(pred_mean.std())
    tail = 2 * min((pred_mean >= obs_mean).mean(), (pred_mean <= obs_mean).mean())
    return {
        **out,
        "e": pd.Series(np.where(ok, e, 0.0), index=dates),
        "innovation": obs_mean - float(pred_mean.mean()),
        "z": (obs_mean - float(pred_mean.mean())) / sd if sd > 0 else None,
        "tail": float(max(tail, 1.0 / len(idx))),
        # Probability integral transform: where the observed window mean falls
        # in its own predictive distribution. Uniform on [0, 1] when calibrated.
        "pit": float((pred_mean <= obs_mean).mean()),
        "pred_sd": sd,
        "ppc_status": fit.diagnostics.get("ppc_status"),
        "beta_mean": beta_mean,
        "fit_quality": fit.diagnostics.get("fit_quality"),
        # For the trend experiments: the coefficient posterior a slope might
        # compete with, and the last fitted slope in business units a period.
        "beta": json.dumps(beta_post) if beta_post else None,
        "trend": fit.trend_kind,
        "slope": (
            float(draws("trend_slope_end").mean() * fit.y_std)
            if "trend_slope_end" in post
            else None
        ),
        "min_ess": fit.diagnostics.get("min_ess_bulk"),
    }


def propagate(dag, data, scope, windows, inn, removed):
    """`actual − counterfactual` window mean of every node, when the nodes in
    `removed` are set to what their model predicted and everything downstream
    is recomputed with its own innovation kept."""
    delta = {}  # node -> Series(cf − actual) over its window, at its own grain
    for node in [n for n in nx.topological_sort(dag) if n in scope]:
        info = inn[node]
        dates = info["dates"]
        d = pd.Series(0.0, index=dates)
        parents = [p for p in dag.predecessors(node) if p in delta]
        defn = defn_of(dag, node)
        grain = data.grain_of[node]
        if parents and len(dates):
            frame = data.fit_frame(node, list(dag.predecessors(node)), grain)
            pdelta = {}
            for p in parents:
                dp = delta[p]
                if data.grain_of[p] != grain and len(dp):
                    full = data.series(p)
                    wide = pd.Series(0.0, index=pd.DatetimeIndex(full["date"]))
                    wide.loc[dp.index] = dp.to_numpy()
                    dp = resample_up(wide, data.grain_of[p], grain, data.kind_of[p])
                aligned = dp.reindex(pd.DatetimeIndex(frame["date"])).fillna(0.0)
                pdelta[p] = aligned.shift(defn.lags.get(p, 0)).fillna(0.0).to_numpy()
            rows = frame["date"].isin(dates).to_numpy()
            if defn.formula:
                actual = {
                    p: frame[p].shift(defn.lags.get(p, 0)).to_numpy(float)
                    for p in dag.predecessors(node)
                }
                cf = {p: actual[p] + pdelta.get(p, 0.0) for p in actual}
                sd = eval_formula(defn.formula, cf) - eval_formula(defn.formula, actual)
                d += np.nan_to_num(sd[rows])
            else:
                for p, b in info.get("beta_mean", {}).items():
                    if p in pdelta:
                        d += b * pdelta[p][rows]
        if node in removed and info.get("e") is not None:
            d -= info["e"].to_numpy()
        delta[node] = d
    return {n: -float(s.mean()) if len(s) else 0.0 for n, s in delta.items()}


def rank_of(ranking, truth):
    """1-based position of the best-placed true root; None if absent."""
    hits = [i + 1 for i, m in enumerate(ranking) if m in truth]
    return min(hits) if hits else None


def run_story(name, dag, data, fits):
    story = STORIES[name]
    target, ana, truth = story["target"], story["ana"], story["truth"]
    t0 = time.perf_counter()
    scope, windows = node_windows(dag, data, target, ana)
    inn = {n: innovation(dag, data, fits, n, windows[n]) for n in scope}
    usable = {n for n in scope if inn[n].get("e") is not None}
    departure = propagate(dag, data, scope, windows, inn, usable)[target]
    contrib = {n: propagate(dag, data, scope, windows, inn, {n})[target] for n in usable}
    elapsed = time.perf_counter() - t0

    ranked = sorted(usable, key=lambda n: -abs(contrib[n]))
    print(f"\n=== story {name}: {target}  analysis {ana[0]}..{ana[1]}  truth {sorted(truth)}")
    print(
        f"target actual {inn[target]['actual']:.4g}; departure from the tree's expectation "
        f"{departure:+.4g}; sum of single-node contributions {sum(contrib.values()):+.4g} "
        f"({elapsed:.0f}s incl. fits)"
    )
    print(
        f"{'node':28s} {'grain':5s} {'window':23s} {'innov':>10s} {'z':>7s} {'tail':>6s} "
        f"{'contrib':>10s} {'share':>7s}  status"
    )
    for n in ranked + sorted(scope - usable):
        i = inn[n]
        w = f"{windows[n][0].date()}..{windows[n][1].date()}"
        c = contrib.get(n)
        share = c / departure if c is not None and abs(departure) > 1e-12 else None
        f = lambda v, s: "" if v is None else format(v, s)  # noqa: E731
        mark = "*" if n in truth else " "
        print(
            f"{mark}{n:27s} {i['grain']:5s} {w:23s} {f(i.get('innovation'), '10.4g'):>10s} "
            f"{f(i.get('z'), '7.2f'):>7s} {f(i.get('tail'), '6.3f'):>6s} "
            f"{f(c, '10.4g'):>10s} {f(share, '7.1%'):>7s}  {i['status']}"
        )

    # Today's rankings, over the same analysis window.
    kwargs = dict(analysis_start=ana[0], analysis_end=ana[1], reference_sensitivity=False)
    if story["ref"]:
        kwargs.update(reference_start=story["ref"][0], reference_end=story["ref"][1])
    rca = run_rca(dag, data, fits, target, **kwargs)
    engine_rank = [c["metric"] for c in rca["ranked_causes"]]
    rel = {
        n: abs(v["relative_change"])
        for n, v in rca["nodes"].items()
        if n != target and v.get("relative_change") is not None
    }
    naive_rank = sorted(rel, key=lambda n: -rel[n])
    innov_rank = [n for n in ranked if n != target]
    z_rank = sorted(
        (n for n in usable if n != target and inn[n].get("z") is not None),
        key=lambda n: -abs(inn[n]["z"]),
    )
    result = {
        "innovation_contribution": rank_of(innov_rank, truth),
        "innovation_z": rank_of(z_rank, truth),
        "engine_ranked_causes": rank_of(engine_rank, truth),
        "naive_relative_change": rank_of(naive_rank, truth),
    }
    print("rank of the planted root (1 = first):", result)
    print("  innovation top 3:", innov_rank[:3])
    print("  engine     top 3:", engine_rank[:3])
    print("  naive      top 3:", naive_rank[:3])
    return result


# ---------------------------------------------------------------------------
# Null calibration: is `z` standard normal where nothing was planted?
#
# The planted anomalies (white_cube.ground_truth.json) occupy these spans. A
# window is "clean" when it sits in a gap between them, starting at least two
# weeks after the previous one ended — a level shift's end is itself a change,
# and a lagged edge carries it a week further. Every node is scored as its own
# target over every clean window, at several horizons from the same fit, so
# the horizon's effect on the predictive width is read off directly. A fit's
# *history* still contains earlier anomalies, as a real tree's would.
# ---------------------------------------------------------------------------
CLEAN_GAPS = [
    ("2024-12-02", "2025-03-02"),  # after the 2024 surprises, before the LatAm campaign
    ("2025-06-23", "2025-08-03"),  # after the campaign + null spike, before onboarding
    ("2025-12-15", "2026-02-01"),  # after onboarding + the dupes, before the CTA regression
    ("2026-03-30", "2026-04-26"),  # after the CTA regression, before the churn spike
    ("2026-07-13", "2026-07-26"),  # after the churn spike
]
# Where each planted anomaly enters the tree, and when: the positive class.
PLANTED = [
    ("visit_signup_rate", "2026-02-02"),
    ("customer_churn_rate", "2026-04-27"),
    ("marketing_spend", "2025-03-03"),
    ("visit_signup_rate", "2025-03-03"),
    ("trial_activation_rate", "2025-08-04"),
    ("trial_days_active", "2025-08-04"),
]
HORIZON_DAYS = (1, 3, 7, 14, 28)  # 1 and 3 apply to day-grain nodes only
NULL_FIELDS = (
    "label node grain start horizon_days n z pit tail innovation pred_sd ppc_status status "
    "trend slope beta fit_seconds fit_quality min_ess"
).split()


def score_windows(dag, data, node, start, limit, label, writer):
    """Score `node` over every horizon from `start` that ends by `limit`, from
    one fit, and drop the fit afterwards (a daily trace is ~10 MB)."""
    fits = {}
    start = pd.Timestamp(start)
    for h in HORIZON_DAYS:
        if h < 7 and data.grain_of[node] != "day":
            continue
        end = start + pd.Timedelta(days=h - 1)
        if end > pd.Timestamp(limit):
            continue
        try:
            r = innovation(dag, data, fits, node, (start, end))
        except Exception as exc:
            r = {"status": f"error: {exc}"[:100], "grain": data.grain_of[node]}
        row = {k: r.get(k) for k in NULL_FIELDS}
        row.update(label=label, node=node, start=str(start.date()), horizon_days=h)
        writer.writerow(row)


def null_run(out_path, only=None, trend=None, slope_sigma=None):
    import csv

    dag, data = load()
    nodes = list(nx.topological_sort(dag))
    if only:
        unknown = sorted(set(only) - set(nodes))
        if unknown:
            raise SystemExit(f"--nodes names metrics the tree does not have: {unknown}")
        nodes = [n for n in nodes if n in only]
    force_trend(dag, set(nodes) | {n for n, _ in PLANTED}, trend, slope_sigma)
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            done = {(r["label"], r["node"], r["start"]) for r in csv.DictReader(f)}
    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, NULL_FIELDS)
        if not done:
            writer.writeheader()
        jobs = [("planted", n, s, "2026-07-26") for n, s in PLANTED]
        for lo, hi in CLEAN_GAPS:
            for s in pd.date_range(lo, hi, freq="W-MON"):
                jobs += [("null", n, str(s.date()), hi) for n in nodes]
        for i, (label, node, start, limit) in enumerate(jobs):
            if (label, node, start) in done:
                continue
            score_windows(dag, data, node, start, limit, label, writer)
            f.flush()
            print(f"[{i + 1}/{len(jobs)}] {label} {node} {start}", flush=True)


def null_summary(out_path):
    # `keep_default_na=False`: pandas reads the literal label "null" as missing.
    df = pd.read_csv(out_path, keep_default_na=False, na_values=[""])
    df = df[df["z"].notna() & (df["status"] != "exact_identity")]
    null, planted = df[df.label == "null"], df[df.label == "planted"]

    def table(frame, by):
        g = frame.groupby(by)
        return pd.DataFrame(
            {
                "n": g["z"].size(),
                "mean_z": g["z"].mean(),
                "sd_z": g["z"].std(),
                "|z|>2": g["z"].apply(lambda z: (z.abs() > 2).mean()),
                "|z|>3": g["z"].apply(lambda z: (z.abs() > 3).mean()),
                "tail<.05": g["tail"].apply(lambda t: (t < 0.05).mean()),
                "pit<.1|>.9": g["pit"].apply(lambda u: ((u < 0.1) | (u > 0.9)).mean()),
            }
        ).round(3)

    pd.set_option("display.width", 200)
    print("=== null windows, by horizon (calibrated: sd_z 1, |z|>2 0.046, pit tails 0.2)")
    print(table(null, "horizon_days").to_string())
    print("\n=== null windows, by node (all horizons)")
    print(table(null, "node").sort_values("sd_z").to_string())
    print("\n=== null windows, by node, 7-day horizon (non-overlapping windows)")
    print(table(null[null.horizon_days == 7], "node").sort_values("sd_z").to_string())
    print("\n=== planted roots at onset, against the same node's null z at the same horizon")
    rows = []
    for _, r in planted.iterrows():
        ref = null[(null.node == r.node) & (null.horizon_days == r.horizon_days)]["z"]
        allz = null[null.horizon_days == r.horizon_days]["z"]
        rows.append(
            dict(
                node=r.node,
                onset=r.start,
                h=r.horizon_days,
                z=round(r.z, 2),
                node_null_sd=round(ref.std(), 2),
                z_over_null_sd=round(r.z / ref.std(), 2) if len(ref) > 2 else None,
                node_null_exceeding=f"{(ref.abs() >= abs(r.z)).sum()}/{len(ref)}",
                all_null_exceeding=f"{(allz.abs() >= abs(r.z)).mean():.3f}",
            )
        )
    print(pd.DataFrame(rows).to_string(index=False))


def null_compare(specs):
    """Arms side by side, per node and horizon: `name=path.csv` each. The
    comparison roadmap S8's default turns on — bias (`mean_z`), calibration
    (`sd_z`, `|z|>2`), predictive width (`pred_sd`) and cost (`fit_s`, counted
    once per fit) — plus each learned coefficient's posterior and the planted
    roots."""
    arms = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        df = pd.read_csv(path, keep_default_na=False, na_values=[""])
        arms[name] = df[df["status"] != "exact_identity"]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)

    def stats(frame):
        z = frame["z"].dropna()
        return {
            "n": len(z),
            "mean_z": z.mean(),
            "sd_z": z.std(),
            "|z|>2": (z.abs() > 2).mean(),
            "pred_sd": frame["pred_sd"].mean(),
            "fit_s": frame["fit_seconds"].mean() if "fit_seconds" in frame else np.nan,
        }

    for h in (7, 28):
        rows = {}
        for name, df in arms.items():
            null = df[(df.label == "null") & (df.horizon_days == h)]
            # `fit_seconds` is recorded on the first horizon a fit scores.
            first = df[(df.label == "null") & df.get("fit_seconds", pd.Series(dtype=float)).notna()]
            for node, g in null.groupby("node"):
                st = stats(g)
                if "fit_seconds" in df:
                    st["fit_s"] = first[first.node == node]["fit_seconds"].mean()
                rows[(node, name)] = st
        print(f"\n=== null windows, {h}-day horizon")
        print(pd.DataFrame(rows).T.round(3).to_string())

    print("\n=== learned coefficients (beta_raw), mean over null fits of the posterior mean / sd")
    rows = {}
    for name, df in arms.items():
        if "beta" not in df:
            continue
        fitted = df[(df.label == "null") & df["beta"].notna() & df["fit_seconds"].notna()]
        for node, g in fitted.groupby("node"):
            for parent in json.loads(g["beta"].iloc[0]):
                posts = [json.loads(b)[parent] for b in g["beta"]]
                rows[(f"{node} <- {parent}", name)] = {
                    "fits": len(posts),
                    "post_mean": np.mean([b["mean"] for b in posts]),
                    "post_sd": np.mean([b["sd"] for b in posts]),
                    "ci94_width": np.mean([b["hi"] - b["lo"] for b in posts]),
                    "sd_of_means": np.std([b["mean"] for b in posts]),
                    "ci_excludes_0": np.mean([b["lo"] > 0 or b["hi"] < 0 for b in posts]),
                }
    if rows:
        print(pd.DataFrame(rows).T.to_string(float_format=lambda v: f"{v:.4g}"))

    print("\n=== planted roots at onset: z, and clean windows on the same node as extreme")
    rows = {}
    for name, df in arms.items():
        null = df[df.label == "null"]
        for _, r in df[(df.label == "planted") & df["z"].notna()].iterrows():
            ref = null[(null.node == r.node) & (null.horizon_days == r.horizon_days)]["z"].dropna()
            rows[(r.node, r.start, int(r.horizon_days), name)] = {
                "z": round(r.z, 2),
                "pred_sd": r.pred_sd,
                "z/null_sd": round(r.z / ref.std(), 2) if len(ref) > 2 else None,
                "null_as_extreme": f"{(ref.abs() >= abs(r.z)).sum()}/{len(ref)}",
            }
    out = pd.DataFrame(rows).T
    print(out[out.index.get_level_values(2).isin([7, 28])].sort_index().to_string())


def main():
    cli = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    cli.add_argument("args", nargs="*")
    cli.add_argument("--nodes", help="null: comma-separated nodes to restrict the null windows to")
    cli.add_argument("--trend", choices=("level", "local_linear"), help="force this trend kind")
    cli.add_argument("--slope-sigma", type=float, help="with --trend local_linear; 0 = fixed drift")
    opts = cli.parse_args()
    argv = opts.args
    if argv[:1] == ["null"]:
        only = set(opts.nodes.split(",")) if opts.nodes else None
        return null_run(argv[1], only, opts.trend, opts.slope_sigma)
    if argv[:1] == ["null-summary"]:
        return null_summary(argv[1])
    if argv[:1] == ["null-compare"]:
        return null_compare(argv[1:])
    names = argv or list(STORIES)
    dag, data = load()
    force_trend(dag, list(dag.nodes), opts.trend, opts.slope_sigma)
    fits = {}
    results = {n: run_story(n, dag, data, fits) for n in names}
    print("\n=== summary: rank of the planted root cause")
    methods = list(next(iter(results.values())))
    print(f"{'story':10s} " + " ".join(f"{m:>24s}" for m in methods))
    for n, r in results.items():
        print(f"{n:10s} " + " ".join(f"{str(r[m]):>24s}" for m in methods))


if __name__ == "__main__":
    main()

"""Innovation scoring: a node's own surprise, and what it does to the target.

Moved here unchanged from the prototype (`knowledge/benchmarks/innovation_score.py`,
which now imports it back), so the evaluation runner and the prototype score
with the same statistics. Design: `knowledge/innovation_rca_design.md` §4.

Nothing here knows where the data came from. Every function takes the tree's
DAG and its `GrainedData`; `fits` is any mapping keyed `(node, fit_end)` that
holds a `FitResult` or the exception the fit raised (the runner passes a
disk-backed one, `fitcache.FitStore`).
"""

import json
import time

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine import model as M
from breakdown.engine.rca import fit_rca_node
from breakdown.formula import eval_formula
from breakdown.grains import resample_up, shift_periods, snap_window, steps_between
from breakdown.parser import TrendConfig

MAX_DRAWS = 1000
SEED = 0


def defn_of(dag, node):
    return dag.nodes[node]["definition"]


def takes_slope_by_role(dag, node):
    """The role rule under test for roadmap S8's default: a slope on a node
    with no parents that is a flow or a stock; a level on rates and on
    anything with a parent to carry its growth."""
    return dag.in_degree(node) == 0 and defn_of(dag, node).kind in ("flow", "stock")


def force_trend(dag, nodes, kind, slope_sigma):
    """Force a trend kind on `nodes` (roadmap S8) by replacing each loaded
    definition's `trend` in memory. The level's own `sigma` is kept, so the
    arms differ in the slope and nothing else. `kind="role"` gives each node
    the kind `takes_slope_by_role` picks."""
    if kind is None:
        if slope_sigma is not None:
            raise SystemExit("--slope-sigma needs --trend")
        return
    for n in nodes:
        defn = defn_of(dag, n)
        k = kind
        if kind == "role":
            k = "local_linear" if takes_slope_by_role(dag, n) else "level"
        fields = {"kind": k}
        if defn.trend is not None:
            fields["sigma"] = defn.trend.sigma
        if slope_sigma is not None and k == "local_linear":
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
    beta_mean = {}
    beta_post = {}
    if fit.parents:
        beta_raw = draws("beta_raw")
        fitted_rows = frame["date"].isin(fit.dates).to_numpy()
        for i, p in enumerate(fit.parents):
            x = shifted[p].to_numpy(float)
            centred = (x[in_win.to_numpy()] - np.nanmean(x[fitted_rows]))[None, :]
            noiseless += beta_raw[:, i : i + 1] * centred
            beta_mean[p] = float(beta_raw[:, i].mean())
            lo, hi = np.quantile(beta_raw[:, i], [0.03, 0.97])
            beta_post[p] = dict(
                mean=beta_mean[p], sd=float(beta_raw[:, i].std()), lo=float(lo), hi=float(hi)
            )
    # The observation noise comes from the likelihood the node was fitted
    # with (roadmap S20), around the full mean: a count's or a mean-scaled
    # node's spread depends on the level it is drawn at, parents included.
    pred = fit.y_mean + fit.y_std * M.observation_draws(
        fit.likelihood,
        (noiseless - fit.y_mean) / fit.y_std,
        draws(M.LIKELIHOOD_NOISE_PARAM[fit.likelihood]),
        fit.y_mean,
        fit.y_std,
        rng,
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
        "likelihood": fit.likelihood,
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
        "divergences": fit.diagnostics.get("divergences"),
        "max_rhat": fit.diagnostics.get("max_rhat"),
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


def force_likelihoods(dag, forced):
    """Re-declare `likelihood` on the named nodes, in memory. The tree file is
    not touched, and the change goes through the parser's own validation, so a
    likelihood the YAML would refuse is refused here too."""
    for node, likelihood in forced.items():
        defn = defn_of(dag, node)
        dag.nodes[node]["definition"] = type(defn).model_validate(
            {**defn.model_dump(exclude={"derived"}), "likelihood": likelihood}
        )


def node_role(dag, node):
    """The four roles the trend's default is argued over."""
    kind = defn_of(dag, node).kind
    if kind == "rate":
        return "rate"
    return f"{kind}, {'no parents' if dag.in_degree(node) == 0 else 'learned parent'}"


def _pit_ks(u):
    """Largest gap between the PIT's empirical distribution and the uniform's."""
    u = np.sort(np.asarray(u, dtype=float))
    n = len(u)
    return max(np.max(np.arange(1, n + 1) / n - u), np.max(u - np.arange(0, n) / n))

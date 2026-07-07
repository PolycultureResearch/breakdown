"""Root cause analysis over the ancestor DAG of a target metric.

`run_rca` walks the ancestors of an anomalous metric and attributes the change
between a reference window and an analysis window to upstream metrics. Two
attribution methods are used depending on the node type:

- **Formula nodes** (arithmetic identities): exact per-day Shapley attribution —
  each analysis-window day is one Shapley game against the parents' reference
  means, and a parent's contribution is its per-day value averaged over the
  window. Contributions therefore capture within-window covariance shifts, and
  they carry CIs from the window bootstrap below.
- **Probabilistic nodes** (learned BSTS regressions): the posterior over the
  raw-scale coefficient (`beta_raw`) times the parent's window-over-window
  change. The fitted model's own trend and seasonal components are reported
  explicitly in a `components` block (window-over-window deltas with CIs), so
  `unexplained` is residual + model misfit only.

Contribution uncertainty combines two sources: the coefficient posterior and
window-sampling noise. The latter comes from a circular moving-block bootstrap
of the window rows (block <= 7 days, resampled jointly across metrics so
cross-metric correlation within a window is preserved), seeded per `run_rca`
call so API responses are deterministic.

Unfitted probabilistic nodes in scope are fit on demand with ADVI on data
strictly before the analysis window; the caller passes its trace cache, keyed
by `(name, fit_end)`, and new fits are added to it in place.

The `ranked_causes` list is a documented **heuristic**: it propagates an
influence score from the target up the ancestor tree, weighting each hop by the
parent's share of its child's gap (clamped to [0, 1]). It is meant as a triage
ordering, not a rigorous multi-hop uncertainty propagation.
"""

from typing import Any, Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine.model import compute_shapley, fit_metric, seasonal_window_delta
from breakdown.formula import eval_formula

# Bootstrap replicates per window; fixed so contribution CIs are comparable
# across nodes and runs.
_N_BOOT = 500


def window_mean(data: pd.DataFrame, col: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Mean of `col` over rows whose date is within [start, end] (inclusive).

    `data["date"]` must already be datetime. Raises if the window is empty.
    """
    return float(_window_values(data, col, start, end).mean())


def _window_values(
    data: pd.DataFrame, col: str, start: pd.Timestamp, end: pd.Timestamp
) -> np.ndarray:
    """Daily values of `col` over rows whose date is within [start, end]
    (inclusive). Raises if the window is empty."""
    mask = (data["date"] >= start) & (data["date"] <= end)
    if not mask.any():
        raise ValueError(
            f"No data for '{col}' in window [{start.date()}, {end.date()}]"
        )
    return data.loc[mask, col].values.astype(float)


def _eval_scalar(formula: str, values: Dict[str, float]) -> float:
    return float(eval_formula(formula, {k: np.array([v]) for k, v in values.items()})[0])


def _block_bootstrap_indices(n: int, n_boot: int, rng, block: int = 7) -> np.ndarray:
    """(n_boot, n) integer index array; circular moving-block bootstrap.

    Each replicate concatenates ceil(n / block') blocks of block' consecutive
    positions (wrapping circularly) starting at uniform positions, truncated to
    n. The effective block length is capped at n // 2 (min 1) so every
    replicate draws at least two independently placed blocks: with a single
    block covering the whole window, the circular bootstrap degenerates to
    rotations whose means are all identical and the resampled variance of the
    window mean collapses to zero — exactly wrong for the short windows this
    exists to be honest about.
    """
    if n < 1:
        raise ValueError("Cannot bootstrap an empty window.")
    block = max(1, min(block, n // 2)) if n > 1 else 1
    n_blocks = -(-n // block)  # ceil
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(n_boot, n_blocks * block)[:, :n]


def _sample_summary(samples: np.ndarray) -> Dict[str, Any]:
    return {
        "estimate": float(samples.mean()),
        "ci_95": [
            float(np.percentile(samples, 2.5)),
            float(np.percentile(samples, 97.5)),
        ],
    }


def shapley_attribution(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
) -> Dict[str, Any]:
    """Per-day exact Shapley decomposition of a formula metric's
    window-over-window gap.

    Each analysis-window day is its own Shapley game: coalition members take
    their value on that day, non-members their reference-window mean. A
    parent's attribution is its per-day Shapley value averaged over the
    analysis window; by efficiency the attributions sum exactly to
    `gap = actual − baseline` where `actual = mean over analysis days of
    formula(parents on that day)` and `baseline = formula(reference means)`.

    Unlike Shapley on window means, this attributes changes in the parents'
    within-analysis-window covariance (for `revenue = orders × aov`, "the big
    orders disappeared" is an orders–aov covariance shift) to the parents
    instead of leaving them outside the attribution. Returns the
    `GET /shapley` response shape.
    """
    defn = dag.nodes[target]["definition"]
    if not defn.formula:
        raise ValueError(
            f"Metric '{target}' has no formula — Shapley attribution requires a formula definition."
        )
    parents = list(dag.predecessors(target))
    missing = [p for p in parents if p not in data.columns]
    if missing:
        raise ValueError(f"Columns missing from data: {missing}")

    frame = data.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    ref_start, ref_end = pd.to_datetime(reference_start), pd.to_datetime(reference_end)
    an_start, an_end = pd.to_datetime(analysis_start), pd.to_datetime(analysis_end)

    ref_means = {p: window_mean(frame, p, ref_start, ref_end) for p in parents}
    daily_actuals = {p: _window_values(frame, p, an_start, an_end) for p in parents}
    n_days = len(next(iter(daily_actuals.values())))

    baseline = _eval_scalar(defn.formula, ref_means)
    actual = float(eval_formula(defn.formula, daily_actuals).mean())

    phi = compute_shapley(
        defn.formula,
        parents,
        {p: np.full(n_days, ref_means[p]) for p in parents},
        daily_actuals,
    )

    return {
        "target": target,
        "formula": defn.formula,
        "baseline": baseline,
        "actual": actual,
        "gap": actual - baseline,
        "attribution": {p: float(phi[p].mean()) for p in parents},
    }


def run_rca(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    traces: Dict[Tuple[str, Optional[str]], Any],
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    advi_draws: int = 500,
) -> Dict[str, Any]:
    """Attribute `target`'s window-over-window change to its ancestors.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` -> FitResult.
    Probabilistic nodes in scope without a cached trace are fit with ADVI on data
    strictly before `analysis_start` (so the anomaly window is excluded) and added
    to it. A full-window fit (`fit_end=None`) is never reused here — it is
    contaminated by the anomaly for attribution purposes.
    """
    if target not in dag:
        raise ValueError(f"Metric '{target}' not found in the metric tree.")

    frame = data.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    ref_start, ref_end = pd.to_datetime(reference_start), pd.to_datetime(reference_end)
    an_start, an_end = pd.to_datetime(analysis_start), pd.to_datetime(analysis_end)

    # One seeded generator per call: bootstrap replicates (and hence every
    # contribution number) are identical across identical calls.
    rng = np.random.default_rng(0)

    nodes_in_scope = nx.ancestors(dag, target) | {target}

    # Fit any probabilistic (non-formula, non-root) node in scope that lacks a
    # cached trace for this analysis window. Formula nodes and roots need no fit.
    for node in nodes_in_scope:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and (node, analysis_start) not in traces:
            traces[(node, analysis_start)] = fit_metric(
                dag, data, node, draws=advi_draws,
                inference_method="advi", fit_end=analysis_start,
            )

    nodes_out: Dict[str, Any] = {}
    # Sorted order fixes the rng consumption sequence (set iteration order is
    # not stable across processes).
    for node in sorted(nodes_in_scope):
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))

        baseline = window_mean(frame, node, ref_start, ref_end)
        actual = window_mean(frame, node, an_start, an_end)
        gap = actual - baseline
        relative_change = gap / baseline if abs(baseline) >= 1e-12 else None

        contributions = []
        components = None
        if not parents:
            attribution_method = None
            unexplained = None
        elif defn.formula:
            attribution_method = "shapley"
            sh = shapley_attribution(
                dag, data, node, reference_start, reference_end, analysis_start, analysis_end
            )

            # Bootstrap the windows jointly across parents (one set of day
            # indices per replicate) to preserve cross-metric correlation, then
            # run one vectorized per-day Shapley over all replicates: replicate
            # b occupies positions [b*n_an, (b+1)*n_an) with its own resampled
            # reference means repeated across them.
            ref_vals = {p: _window_values(frame, p, ref_start, ref_end) for p in parents}
            an_vals = {p: _window_values(frame, p, an_start, an_end) for p in parents}
            n_an = len(next(iter(an_vals.values())))
            ref_idx = _block_bootstrap_indices(len(next(iter(ref_vals.values()))), _N_BOOT, rng)
            an_idx = _block_bootstrap_indices(n_an, _N_BOOT, rng)
            boot_baselines = {
                p: np.repeat(ref_vals[p][ref_idx].mean(axis=1), n_an) for p in parents
            }
            boot_actuals = {p: an_vals[p][an_idx].reshape(-1) for p in parents}
            phi = compute_shapley(defn.formula, parents, boot_baselines, boot_actuals)

            for p in parents:
                phi_b = phi[p].reshape(_N_BOOT, n_an).mean(axis=1)
                estimate = float(phi_b.mean())
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": [
                        float(np.percentile(phi_b, 2.5)),
                        float(np.percentile(phi_b, 97.5)),
                    ],
                    "prob_same_direction": float(
                        max((phi_b > 0).mean(), (phi_b < 0).mean())
                    ),
                })
            unexplained = gap - sh["gap"]
        else:
            attribution_method = "posterior"
            fit = traces[(node, analysis_start)]
            arr = fit.trace.posterior["beta_raw"].values.reshape(-1, len(parents))
            n_post = arr.shape[0]

            # Map window dates onto the fitted time index: t = days since the
            # first fitted date, matching the model's internal t = arange(len(y)).
            ref_mask = (frame["date"] >= ref_start) & (frame["date"] <= ref_end)
            an_mask = (frame["date"] >= an_start) & (frame["date"] <= an_end)
            t_ref = (frame.loc[ref_mask, "date"] - fit.dates[0]).dt.days.to_numpy()
            t_an = (frame.loc[an_mask, "date"] - fit.dates[0]).dt.days.to_numpy()

            trend_samples = fit.trace.posterior["trend"].values.reshape(n_post, -1)
            if (t_ref < 0).any() or (t_ref >= trend_samples.shape[1]).any():
                raise ValueError(
                    f"Reference window [{reference_start}, {reference_end}] must lie "
                    f"inside the fitted period for '{node}' "
                    f"({fit.dates[0].date()} to {fit.dates[-1].date()})."
                )

            # Trend: the analysis window is outside the fitted period (the fit
            # ends at analysis_start), and the random-walk forecast of a local
            # level is flat at the last fitted state — so the analysis-window
            # trend is trend[-1], per posterior sample. Its CI reflects the
            # posterior of that last state, not forward simulation of new steps.
            trend_delta = (
                trend_samples[:, -1] - trend_samples[:, t_ref].mean(axis=1)
            ) * fit.y_std
            seasonal_delta = (
                seasonal_window_delta(fit.trace, defn.seasonality, t_ref, t_an) * fit.y_std
            )
            components = {
                "trend": _sample_summary(trend_delta),
                "seasonal": _sample_summary(seasonal_delta),
            }

            # Window bootstrap indices, shared across this node's parents
            # (joint resampling); lag-shifted windows span the same number of
            # days on a daily grid, so the same position indices apply.
            ref_idx = _block_bootstrap_indices(len(t_ref), _N_BOOT, rng)
            an_idx = _block_bootstrap_indices(len(t_an), _N_BOOT, rng)

            estimate_sum = 0.0
            for i, p in enumerate(parents):
                lag = defn.lags.get(p, 0)
                # The parent values that influenced the analysis window are
                # those `lag` days earlier, so shift both windows back.
                shift = pd.Timedelta(days=lag)
                p_ref_vals = _window_values(frame, p, ref_start - shift, ref_end - shift)
                p_an_vals = _window_values(frame, p, an_start - shift, an_end - shift)
                r_idx = (
                    ref_idx if len(p_ref_vals) == ref_idx.shape[1]
                    else _block_bootstrap_indices(len(p_ref_vals), _N_BOOT, rng)
                )
                a_idx = (
                    an_idx if len(p_an_vals) == an_idx.shape[1]
                    else _block_bootstrap_indices(len(p_an_vals), _N_BOOT, rng)
                )
                delta_samples = p_an_vals[a_idx].mean(axis=1) - p_ref_vals[r_idx].mean(axis=1)
                # Shuffle so posterior draw i is not systematically paired with
                # the same bootstrap replicate across parents.
                delta_samples = rng.permutation(delta_samples)
                samples = arr[:, i] * delta_samples[np.arange(n_post) % _N_BOOT]
                estimate = float(samples.mean())
                estimate_sum += estimate
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": [
                        float(np.percentile(samples, 2.5)),
                        float(np.percentile(samples, 97.5)),
                    ],
                    "prob_same_direction": float(
                        max((samples > 0).mean(), (samples < 0).mean())
                    ),
                })
            unexplained = (
                gap
                - estimate_sum
                - components["trend"]["estimate"]
                - components["seasonal"]["estimate"]
            )

        nodes_out[node] = {
            "baseline": baseline,
            "actual": actual,
            "gap": gap,
            "relative_change": relative_change,
            "attribution_method": attribution_method,
            "unexplained": unexplained,
            "components": components,
            "contributions": contributions,
        }

    return {
        "target": target,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "nodes": nodes_out,
        "ranked_causes": _rank_causes(dag, target, nodes_in_scope, nodes_out),
    }


def _rank_causes(dag, target, nodes_in_scope, nodes_out):
    """Heuristic influence score: propagate 1.0 from the target up the ancestor
    tree, weighting each hop by the parent's clamped share of its child's gap.

    Processing in reverse topological order (target first) guarantees a child's
    score is complete before it is propagated to its parents.
    """
    score = {n: 0.0 for n in nodes_in_scope}
    score[target] = 1.0
    via: Dict[str, str] = {}
    best_term: Dict[str, float] = {n: float("-inf") for n in nodes_in_scope}

    topo_scope = [n for n in nx.topological_sort(dag) if n in nodes_in_scope]
    for child in reversed(topo_scope):
        for contrib in nodes_out[child]["contributions"]:
            parent = contrib["parent"]
            if parent not in score:
                continue
            share = contrib["share_of_gap"]
            weight = 0.0 if share is None else min(abs(share), 1.0)
            term = score[child] * weight
            score[parent] += term
            if term > best_term[parent]:
                best_term[parent] = term
                via[parent] = child

    ranked = [
        {"metric": n, "score": score[n], "via": via.get(n)}
        for n in nodes_in_scope
        if n != target
    ]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked

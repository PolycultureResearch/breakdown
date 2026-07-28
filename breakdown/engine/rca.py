"""Root cause analysis over the ancestor DAG of a target metric.

`run_rca` walks the ancestors of an anomalous metric and attributes the change
between a reference window and an analysis window to upstream metrics. Two
attribution methods are used depending on the node type:

- **Formula nodes** (arithmetic identities): exact symmetric per-day Shapley
  attribution — a window-means bridge game plus each parent's share of the
  within-window co-movement term of *each* window (analysis added, reference
  subtracted). Both windows are treated per-day, so contributions capture
  covariance *shifts* between windows, attributions sum exactly to the
  formula's own gap, and `unexplained` is measurement residual only. CIs come
  from the window bootstrap below.
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
from breakdown.grains import (
    BOOT_BLOCK,
    ensure_grained,
    fit_grain,
    shift_periods,
    snap_window,
    steps_between,
)

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
    """Per-period values of `col` over rows whose date is within [start, end]
    (inclusive). Raises if the window is empty."""
    mask = (data["date"] >= start) & (data["date"] <= end)
    if not mask.any():
        raise ValueError(
            f"No data for '{col}' in window [{start.date()}, {end.date()}]"
        )
    return data.loc[mask, col].values.astype(float)


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


def _window_info(snapped) -> Dict[str, Any]:
    """The whole periods a requested window snapped to, for the response."""
    return {
        "start": str(snapped.first_start.date()),
        "end": str(snapped.last_end.date()),
        "n_periods": snapped.n_periods,
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
    """Symmetric per-day Shapley decomposition of a formula metric's
    window-over-window gap.

    Both windows are evaluated per-day (`baseline` / `actual` are the mean over
    each window's days of `formula(parents on that day)`), and each parent's
    attribution is the sum of three exact Shapley games:

    - **means**: the window-means bridge (reference means → analysis means);
    - **covariance_analysis**: one game per analysis-window day with
      non-members at the *analysis* means — the parent's share of the
      within-analysis-window co-movement term `mean_an f(daily) − f(μ_an)`;
    - **covariance_reference**: the same within the reference window,
      subtracted.

    The parts telescope, so attributions sum exactly to `gap = actual −
    baseline` for windows of any (unequal) lengths. A covariance *shift*
    between windows (for `revenue = orders × aov`, "the big orders
    disappeared" is an orders–aov covariance shift) is attributed to the
    parents; when nothing moves — means and covariance alike — every part
    cancels and the attribution is zero. For non-product formulas the
    covariance terms are, precisely, each window's full within-window
    co-movement/Jensen term. Because the reference window is now evaluated
    per-day, a single pathological reference day (e.g. a near-zero
    denominator in a ratio formula) affects `baseline` symmetrically with the
    analysis side — resolve those at the data grain, not here.

    Returns the `GET /shapley` response shape; `decomposition` carries the
    per-parent parts with `attribution = means + covariance_analysis −
    covariance_reference`.
    """
    data = ensure_grained(data)
    defn = dag.nodes[target]["definition"]
    if not defn.formula:
        raise ValueError(
            f"Metric '{target}' has no formula — Shapley attribution requires a formula definition."
        )
    parents = list(dag.predecessors(target))
    grain = fit_grain(dag, target)
    frame = data.fit_frame(target, parents, grain)

    snapped_ref = snap_window(reference_start, reference_end, grain)
    snapped_an = snap_window(analysis_start, analysis_end, grain)
    if snapped_ref is None or snapped_an is None:
        which, s, e = (
            ("reference", reference_start, reference_end)
            if snapped_ref is None
            else ("analysis", analysis_start, analysis_end)
        )
        raise ValueError(
            f"The {which} window [{s}, {e}] contains no whole '{grain}' period "
            f"for '{target}'."
        )
    ref_start, ref_end = snapped_ref.first_start, snapped_ref.last_start
    an_start, an_end = snapped_an.first_start, snapped_an.last_start

    ref_daily = {p: _window_values(frame, p, ref_start, ref_end) for p in parents}
    an_daily = {p: _window_values(frame, p, an_start, an_end) for p in parents}
    ref_means = {p: float(ref_daily[p].mean()) for p in parents}
    an_means = {p: float(an_daily[p].mean()) for p in parents}

    # Both windows are evaluated per-day, so an exact identity reconstructs
    # the node's own window means on both sides and the attributions'
    # efficiency holds against the node's own gap.
    baseline = float(eval_formula(defn.formula, ref_daily).mean())
    actual = float(eval_formula(defn.formula, an_daily).mean())

    # Three exact games that telescope to actual - baseline: the window-means
    # bridge, plus each parent's share of the within-window co-movement term
    # (mean_w f(daily) - f(window means)) of each window. Windows may have
    # different lengths — no per-day pairing is ever needed.
    phi_means = compute_shapley(defn.formula, parents, ref_means, an_means)
    phi_cov_an = compute_shapley(defn.formula, parents, an_means, an_daily)
    phi_cov_ref = compute_shapley(defn.formula, parents, ref_means, ref_daily)

    attribution: Dict[str, float] = {}
    decomposition: Dict[str, Dict[str, float]] = {}
    for p in parents:
        means_part = float(phi_means[p])
        cov_an_part = float(phi_cov_an[p].mean())
        cov_ref_part = float(phi_cov_ref[p].mean())
        attribution[p] = means_part + cov_an_part - cov_ref_part
        decomposition[p] = {
            "means": means_part,
            "covariance_analysis": cov_an_part,
            "covariance_reference": cov_ref_part,
        }

    return {
        "target": target,
        "formula": defn.formula,
        "grain": grain,
        "effective_windows": {
            "reference": _window_info(snapped_ref),
            "analysis": _window_info(snapped_an),
        },
        "baseline": baseline,
        "actual": actual,
        "gap": actual - baseline,
        "attribution": attribution,
        "decomposition": decomposition,
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

    data = ensure_grained(data)

    # One seeded generator per call: bootstrap replicates (and hence every
    # contribution number) are identical across identical calls.
    rng = np.random.default_rng(0)

    nodes_in_scope = nx.ancestors(dag, target) | {target}

    # Fit any probabilistic (non-formula, non-root) node in scope that lacks a
    # cached trace for this analysis window. Formula nodes and roots need no
    # fit; nodes whose windows hold no whole period at their grain are skipped
    # (they are reported below without attribution).
    for node in nodes_in_scope:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and (node, analysis_start) not in traces:
            g = fit_grain(dag, node)
            if (
                snap_window(reference_start, reference_end, g) is None
                or snap_window(analysis_start, analysis_end, g) is None
            ):
                continue
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
        grain = fit_grain(dag, node)
        frame = data.fit_frame(node, parents, grain)

        # Windows are interpreted per node at its grain: only whole periods
        # fully inside the requested [start, end] count. A window too short
        # for the grain reports a status instead of failing the whole RCA.
        snapped_ref = snap_window(reference_start, reference_end, grain)
        snapped_an = snap_window(analysis_start, analysis_end, grain)
        if snapped_ref is None or snapped_an is None:
            nodes_out[node] = {
                "status": "window_shorter_than_grain",
                "grain": grain,
                "effective_windows": None,
                "baseline": None,
                "actual": None,
                "gap": None,
                "relative_change": None,
                "attribution_method": None,
                "inference_method": None,
                "fit_quality": None,
                "ci_status": None,
                "unexplained": None,
                "components": None,
                "contributions": [],
            }
            continue
        ref_start, ref_end = snapped_ref.first_start, snapped_ref.last_start
        an_start, an_end = snapped_an.first_start, snapped_an.last_start
        single_period = snapped_ref.n_periods == 1 or snapped_an.n_periods == 1
        block = BOOT_BLOCK[grain]

        baseline = window_mean(frame, node, ref_start, ref_end)
        actual = window_mean(frame, node, an_start, an_end)
        gap = actual - baseline
        relative_change = gap / baseline if abs(baseline) >= 1e-12 else None

        contributions = []
        components = None
        inference_method = None
        fit_quality = None
        if not parents:
            attribution_method = None
            unexplained = None
            ci_status = None
        elif defn.formula:
            attribution_method = "shapley"
            sh = shapley_attribution(
                dag, data, node, reference_start, reference_end, analysis_start, analysis_end
            )

            # Bootstrap the windows jointly across parents (one set of day
            # indices per replicate) to preserve cross-metric correlation, then
            # run the same three-game decomposition per replicate, vectorized
            # over all replicates: a window-means bridge on the resampled
            # means, and each window's per-day co-movement game against that
            # replicate's own resampled means (replicate b occupies positions
            # [b*n, (b+1)*n) of the flattened per-day games).
            ref_vals = {p: _window_values(frame, p, ref_start, ref_end) for p in parents}
            an_vals = {p: _window_values(frame, p, an_start, an_end) for p in parents}
            n_an = len(next(iter(an_vals.values())))
            n_ref = len(next(iter(ref_vals.values())))
            ref_idx = _block_bootstrap_indices(n_ref, _N_BOOT, rng, block=block)
            an_idx = _block_bootstrap_indices(n_an, _N_BOOT, rng, block=block)
            boot_ref_means = {p: ref_vals[p][ref_idx].mean(axis=1) for p in parents}
            boot_an_means = {p: an_vals[p][an_idx].mean(axis=1) for p in parents}

            phi_means = compute_shapley(
                defn.formula, parents, boot_ref_means, boot_an_means
            )
            phi_cov_an = compute_shapley(
                defn.formula,
                parents,
                {p: np.repeat(boot_an_means[p], n_an) for p in parents},
                {p: an_vals[p][an_idx].reshape(-1) for p in parents},
            )
            phi_cov_ref = compute_shapley(
                defn.formula,
                parents,
                {p: np.repeat(boot_ref_means[p], n_ref) for p in parents},
                {p: ref_vals[p][ref_idx].reshape(-1) for p in parents},
            )

            for p in parents:
                phi_b = (
                    phi_means[p]
                    + phi_cov_an[p].reshape(_N_BOOT, n_an).mean(axis=1)
                    - phi_cov_ref[p].reshape(_N_BOOT, n_ref).mean(axis=1)
                )
                estimate = float(phi_b.mean())
                # A single-period window degenerates the block bootstrap to
                # identical replicates; report no interval rather than a
                # falsely-zero-width one.
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": None if single_period else [
                        float(np.percentile(phi_b, 2.5)),
                        float(np.percentile(phi_b, 97.5)),
                    ],
                    "prob_same_direction": None if single_period else float(
                        max((phi_b > 0).mean(), (phi_b < 0).mean())
                    ),
                })
            ci_status = "degenerate_single_period" if single_period else "ok"
            unexplained = gap - sh["gap"]
        else:
            attribution_method = "posterior"
            fit = traces[(node, analysis_start)]
            inference_method = fit.inference_method
            fit_quality = fit.diagnostics.get("fit_quality")
            arr = fit.trace.posterior["beta_raw"].values.reshape(-1, len(parents))
            n_post = arr.shape[0]

            # Map window period starts onto the fitted time index: t = grain
            # steps since the first fitted period, matching the model's
            # internal t = arange(len(y)). For day grain this is exactly the
            # old days-since-first-fitted-date mapping.
            ref_mask = (frame["date"] >= ref_start) & (frame["date"] <= ref_end)
            an_mask = (frame["date"] >= an_start) & (frame["date"] <= an_end)
            t_ref = steps_between(
                pd.DatetimeIndex(frame.loc[ref_mask, "date"]), fit.dates[0], grain
            )
            t_an = steps_between(
                pd.DatetimeIndex(frame.loc[an_mask, "date"]), fit.dates[0], grain
            )

            trend_samples = fit.trace.posterior["trend"].values.reshape(n_post, -1)
            if (t_ref < 0).any() or (t_ref >= trend_samples.shape[1]).any():
                raise ValueError(
                    f"Reference window [{reference_start}, {reference_end}] must lie "
                    f"inside the fitted period for '{node}' (grain '{grain}', "
                    f"{fit.dates[0].date()} to {fit.dates[-1].date()})."
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
            # periods on the grain spine, so the same position indices apply.
            ref_idx = _block_bootstrap_indices(len(t_ref), _N_BOOT, rng, block=block)
            an_idx = _block_bootstrap_indices(len(t_an), _N_BOOT, rng, block=block)

            estimate_sum = 0.0
            for i, p in enumerate(parents):
                lag = defn.lags.get(p, 0)
                # The parent values that influenced the analysis window are
                # those `lag` grain steps earlier, so shift both windows back
                # by whole periods (stays on the spine across month bounds).
                p_ref_vals = _window_values(
                    frame, p,
                    shift_periods(ref_start, -lag, grain),
                    shift_periods(ref_end, -lag, grain),
                )
                p_an_vals = _window_values(
                    frame, p,
                    shift_periods(an_start, -lag, grain),
                    shift_periods(an_end, -lag, grain),
                )
                r_idx = (
                    ref_idx if len(p_ref_vals) == ref_idx.shape[1]
                    else _block_bootstrap_indices(len(p_ref_vals), _N_BOOT, rng, block=block)
                )
                a_idx = (
                    an_idx if len(p_an_vals) == an_idx.shape[1]
                    else _block_bootstrap_indices(len(p_an_vals), _N_BOOT, rng, block=block)
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
            # The beta_raw posterior still carries real uncertainty on a
            # single-period window; the flag says the window-sampling
            # component of the CI is absent.
            ci_status = "posterior_only_single_period" if single_period else "ok"

        nodes_out[node] = {
            "status": "ok",
            "grain": grain,
            "effective_windows": {
                "reference": _window_info(snapped_ref),
                "analysis": _window_info(snapped_an),
            },
            "baseline": baseline,
            "actual": actual,
            "gap": gap,
            "relative_change": relative_change,
            "attribution_method": attribution_method,
            "inference_method": inference_method,
            "fit_quality": fit_quality,
            "ci_status": ci_status,
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

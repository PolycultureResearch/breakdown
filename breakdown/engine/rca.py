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

**One bad node does not end the analysis.** Every node in scope carries a
`status`; anything other than `"ok"` reports the node without attribution and
lets the rest of the tree through, with the reason in `status_reason`:

- `"window_shorter_than_grain"` — the windows hold no whole period at the
  node's grain;
- `"fit_failed"` — the node's own BSTS fit raised (a parent held flat for the
  whole fit window has zero variance and cannot be normalized);
- `"attribution_failed"` — the node's formula does not produce a finite value
  over these windows (a zero denominator). The exception is the RCA *target*:
  the whole response is about that node, so its failure is raised rather than
  buried in a status.
"""

from typing import Any, Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine.model import compute_shapley, fit_metric, seasonal_window_delta
from breakdown.engine.progress import ProgressFn
from breakdown.engine.progress import report as _report
from breakdown.formula import eval_formula
from breakdown.grains import (
    BOOT_BLOCK,
    coarsest,
    default_reference_window,
    ensure_grained,
    fit_grain,
    next_start,
    shift_periods,
    snap_window,
    steps_between,
)

# Bootstrap replicates per window; fixed so contribution CIs are comparable
# across nodes and runs.
_N_BOOT = 500

# Surviving replicates required before a bootstrap interval is reported at all
# (same threshold and posture as `slices._excess_fields`).
_MIN_CI_REPLICATES = 100

# Dates named in a diagnostic before it truncates, as `_align_to_spine` does.
_MAX_SHOWN_DATES = 5


class NonFiniteAttribution(ValueError):
    """A formula node's decomposition is not a finite number over these windows.

    Its own subclass rather than a bare `ValueError` so `run_rca` can degrade
    *this* condition to a per-node status without also swallowing the unrelated
    `ValueError`s the same call can raise (an over-wide parent set, a window
    that misses the data). A `ValueError` still, so the API keeps turning it
    into a 422 carrying the message.
    """


def _reference_alignment(dag: nx.DiGraph, target: str) -> Tuple[bool, str]:
    """Alignment inputs for the default reference window, derived from the
    target's ancestor scope: whether any node in scope declares seasonality
    (whole-week reference lengths keep the weekday mix balanced) and the
    coarsest fit grain in scope (the reference must hold at least one whole
    period at it)."""
    scope = nx.ancestors(dag, target) | {target}
    week_align = any(dag.nodes[n]["definition"].seasonality for n in scope)
    coarsest_grain = coarsest(fit_grain(dag, n) for n in scope)
    return week_align, coarsest_grain


def resolve_reference_window(
    dag: nx.DiGraph,
    data,
    target: str,
    analysis_start: str,
    analysis_end: str,
    reference_start: Optional[str],
    reference_end: Optional[str],
) -> Tuple[str, str, bool]:
    """Resolve possibly-omitted reference dates to concrete ones.

    Both omitted → the matched adjacent block (`default_reference_window`)
    aligned for the target's scope. Exactly one omitted is an error.
    Returns ``(reference_start, reference_end, defaulted)``.
    """
    if (reference_start is None) != (reference_end is None):
        raise ValueError(
            "Pass both reference_start and reference_end, or neither "
            "(omitting both uses the default reference window)."
        )
    if reference_start is not None:
        return reference_start, reference_end, False
    week_align, coarsest_grain = _reference_alignment(dag, target)
    ref_start, ref_end = default_reference_window(
        analysis_start,
        analysis_end,
        ensure_grained(data).date_start,
        week_align=week_align,
        coarsest_grain=coarsest_grain,
    )
    return ref_start, ref_end, True


def _validate_windows(
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
) -> None:
    """Reject window pairs that cannot mean what the caller intended.

    Requires `reference_start <= reference_end < analysis_start <= analysis_end`.
    Overlap is an error, not a warning: the whole premise of the comparison is
    that the reference window describes the normal regime the analysis window
    departed from, and a shared period is counted on both sides of every gap.
    An inverted window is rejected here rather than silently snapping to an
    empty one downstream.

    Grain- and data-independent — coverage is checked per node by
    `_validate_coverage`, since each node reads its own grain frame.
    """
    dates = {
        "reference_start": reference_start,
        "reference_end": reference_end,
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
    }
    parsed = {}
    for label, value in dates.items():
        try:
            parsed[label] = pd.Timestamp(value).normalize()
        except (ValueError, TypeError):
            raise ValueError(f"{label} is not a valid date: {value!r}")

    for first, second in (
        ("reference_start", "reference_end"),
        ("analysis_start", "analysis_end"),
    ):
        if parsed[first] > parsed[second]:
            raise ValueError(
                f"{first} ({dates[first]}) must be on or before {second} ({dates[second]})."
            )

    if parsed["reference_end"] >= parsed["analysis_start"]:
        raise ValueError(
            f"The reference window [{reference_start}, {reference_end}] must end "
            f"strictly before the analysis window starts ({analysis_start}); the "
            "windows overlap, so the same periods would count as both the normal "
            "regime and the departure from it."
        )


def _validate_coverage(
    frame: pd.DataFrame,
    node: str,
    grain: str,
    snapped_ref,
    snapped_an,
    lags: Optional[Dict[str, int]] = None,
) -> None:
    """Every window a node reads must lie inside that node's own data.

    A window that falls *entirely* outside the data already raises in
    `_window_values`. The case this catches is the quiet one: a window that
    only *partly* overlaps the data silently averages the periods that happen
    to exist, so a reference mean over 30 requested days can be computed from
    the 4 that were loaded — a wrong number, not a missing one.

    Lagged parents are checked against their *shifted* windows and reported
    with the parent, its lag, and the shifted dates, so the message names a
    window the caller can act on rather than one they never typed.
    """
    if frame.empty:
        raise ValueError(f"No data at all for '{node}' at grain '{grain}'.")
    data_start = pd.Timestamp(frame["date"].min())
    data_end = pd.Timestamp(frame["date"].max())

    def check(first_start, last_start, label: str, owner: str, shifted_by: int) -> None:
        if first_start >= data_start and last_start <= data_end:
            return
        window = f"[{first_start.date()}, {(next_start(last_start, grain) - pd.Timedelta(days=1)).date()}]"
        via = (
            ""
            if not shifted_by
            else (
                f" (parent '{owner}' is read at lag {shifted_by}, so the node's "
                f"{label} window shifts back {shifted_by} {grain}(s) to this one)"
            )
        )
        raise ValueError(
            f"The {label} window {window} for '{owner}' is not fully covered by "
            f"its data, which runs [{data_start.date()}, {data_end.date()}]{via}. "
            "Attribution over a partly-covered window would average only the "
            "periods that happen to exist."
        )

    for label, snapped in (("reference", snapped_ref), ("analysis", snapped_an)):
        check(snapped.first_start, snapped.last_start, label, node, 0)
        for parent, lag in (lags or {}).items():
            if not lag:
                continue
            check(
                shift_periods(snapped.first_start, -lag, grain),
                shift_periods(snapped.last_start, -lag, grain),
                label,
                parent,
                lag,
            )


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
        raise ValueError(f"No data for '{col}' in window [{start.date()}, {end.date()}]")
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


def _lagged_windows(snapped_ref, snapped_an, lag: int, grain: str) -> Dict[str, Any]:
    """The parent's own windows for a lagged contribution: the node's snapped
    windows shifted back by the lag — the periods that actually influenced the
    child. These are the dates to narrate the parent with, and the windows to
    pass to any follow-up analysis of it (drill-down RCA, slicing)."""

    def shifted(snapped) -> Dict[str, str]:
        start = shift_periods(snapped.first_start, -lag, grain)
        last = shift_periods(snapped.last_start, -lag, grain)
        return {
            "start": str(start.date()),
            "end": str((next_start(last, grain) - pd.Timedelta(days=1)).date()),
        }

    return {"reference": shifted(snapped_ref), "analysis": shifted(snapped_an)}


def _node_out(**fields) -> Dict[str, Any]:
    """One node's RCA record with every key present.

    Callers override what they know; everything else stays null. A node that
    was skipped or failed answers the same shape as one that was attributed —
    consumers (the UI, the MCP compaction) branch on `status`, never on which
    keys happen to exist.
    """
    record: Dict[str, Any] = {
        "status": "ok",
        "status_reason": None,
        "grain": None,
        "effective_windows": None,
        "baseline": None,
        "actual": None,
        "gap": None,
        "relative_change": None,
        "attribution_method": None,
        "inference_method": None,
        "fit_quality": None,
        "sign_warnings": None,
        "fit_window": None,
        "seasonality_warnings": None,
        "ci_status": None,
        "unexplained": None,
        "components": None,
        "interaction": None,
        "contributions": [],
    }
    record.update(fields)
    return record


def _nonfinite_diagnosis(
    frame: pd.DataFrame,
    target: str,
    formula: str,
    grain: str,
    parents,
    lags: Dict[str, int],
    windows,
    baseline: float,
    actual: float,
    attribution: Dict[str, float],
) -> str:
    """Say *why* a formula decomposition came out non-finite, in the terms the
    analyst can act on: which parent series holds zeros (or non-finite values),
    in which window, and on which dates.

    The alternative — emitting the NaN — reaches Starlette's `allow_nan=False`
    encoder as an unhandled 500 with no diagnostic at all, and over MCP turns
    every number in the node into `null` (the same failure `slices.py` refuses
    for its excess replicates). A zero is only fatal as a denominator, and
    deciding which parent *is* the denominator would mean interpreting the
    formula, so every zero-or-non-finite parent series is named and the reader
    picks; that is still far more than the caller gets today.

    `windows` is `(label, first_start, last_start)` per window, at `grain`.
    """
    clauses = []
    for p in parents:
        lag = lags.get(p, 0)
        for label, first_start, last_start in windows:
            mask = (frame["date"] >= shift_periods(first_start, -lag, grain)) & (
                frame["date"] <= shift_periods(last_start, -lag, grain)
            )
            sub = frame.loc[mask, ["date", p]]
            vals = sub[p].to_numpy(dtype=float)
            bad = (vals == 0.0) | ~np.isfinite(vals)
            if not bad.any():
                continue
            dates = [str(d.date()) for d in sub.loc[bad, "date"]]
            shown = ", ".join(dates[:_MAX_SHOWN_DATES])
            if len(dates) > _MAX_SHOWN_DATES:
                shown += ", …"
            via = f" (read at lag {lag})" if lag else ""
            clauses.append(
                f"parent '{p}'{via} is zero or non-finite on {len(dates)} of "
                f"{len(vals)} {label}-window {grain}(s) ({shown})"
            )

    detail = (
        "; ".join(clauses)
        if clauses
        else (
            "no parent series holds a zero or a non-finite value, so the formula "
            "itself overflows or cancels to a non-finite value over these windows"
        )
    )
    bad_parents = sorted(p for p, v in attribution.items() if not np.isfinite(v))
    bad_note = f", non-finite attribution for {bad_parents}" if bad_parents else ""
    return (
        f"Formula attribution for '{target}' ('{formula}') is not a finite number "
        f"(baseline={baseline}, actual={actual}{bad_note}): {detail}. A period "
        "whose denominator is zero has no finite decomposition — narrow the "
        "window to exclude those periods, or fix the series at the source."
    )


def shapley_attribution(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    target: str,
    *,
    analysis_start: str,
    analysis_end: str,
    reference_start: Optional[str] = None,
    reference_end: Optional[str] = None,
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

    Omitting both reference dates uses the default reference window (the
    matched adjacent block before the analysis window); the reference is only
    the comparison baseline, never the fit window.

    Returns the `GET /shapley` response shape; `decomposition` carries the
    per-parent parts with `attribution = means + covariance_analysis −
    covariance_reference`.
    """
    reference_start, reference_end, reference_defaulted = resolve_reference_window(
        dag, data, target, analysis_start, analysis_end, reference_start, reference_end
    )
    _validate_windows(reference_start, reference_end, analysis_start, analysis_end)
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
            f"The {which} window [{s}, {e}] contains no whole '{grain}' period for '{target}'."
        )
    _validate_coverage(frame, target, grain, snapped_ref, snapped_an, defn.lags)
    ref_start, ref_end = snapped_ref.first_start, snapped_ref.last_start
    an_start, an_end = snapped_an.first_start, snapped_an.last_start

    def lagged_window(p, start, end):
        # Cohort-aligned lagged identities (formula + lags): a parent's
        # values are read from windows shifted back by its lag.
        lag = defn.lags.get(p, 0)
        return _window_values(
            frame, p, shift_periods(start, -lag, grain), shift_periods(end, -lag, grain)
        )

    ref_daily = {p: lagged_window(p, ref_start, ref_end) for p in parents}
    an_daily = {p: lagged_window(p, an_start, an_end) for p in parents}
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
    phi_means = compute_shapley(defn.formula, parents, ref_means, an_means, node=target)
    phi_cov_an = compute_shapley(defn.formula, parents, an_means, an_daily, node=target)
    phi_cov_ref = compute_shapley(defn.formula, parents, ref_means, ref_daily, node=target)

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

    # A non-finite number here is not an answer, and every field downstream
    # (gap, shares, CIs, unexplained, every ranked-cause score) inherits it.
    # Refuse it with a diagnostic instead — the API turns this into a 422
    # carrying the message.
    if not (
        np.isfinite(baseline)
        and np.isfinite(actual)
        and all(np.isfinite(v) for v in attribution.values())
    ):
        raise NonFiniteAttribution(
            _nonfinite_diagnosis(
                frame,
                target,
                defn.formula,
                grain,
                parents,
                defn.lags,
                (("reference", ref_start, ref_end), ("analysis", an_start, an_end)),
                baseline,
                actual,
                attribution,
            )
        )

    result = {
        "target": target,
        "formula": defn.formula,
        "grain": grain,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "reference_defaulted": reference_defaulted,
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
    # Cohort-aligned lagged identities read each lagged parent from shifted
    # windows — say which ones. Key absent entirely for unlagged targets.
    lagged = {
        p: _lagged_windows(snapped_ref, snapped_an, defn.lags[p], grain)
        for p in parents
        if defn.lags.get(p, 0)
    }
    if lagged:
        result["parent_windows"] = lagged
    return result


def run_rca(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    traces: Dict[Tuple[str, Optional[str]], Any],
    target: str,
    *,
    analysis_start: str,
    analysis_end: str,
    reference_start: Optional[str] = None,
    reference_end: Optional[str] = None,
    advi_draws: int = 500,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """Attribute `target`'s window-over-window change to its ancestors.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` -> FitResult.
    Probabilistic nodes in scope without a cached trace are fit with ADVI on data
    strictly before `analysis_start` (so the anomaly window is excluded) and added
    to it. A full-window fit (`fit_end=None`) is never reused here — it is
    contaminated by the anomaly for attribution purposes.

    Omitting both reference dates uses the default reference window: the
    matched adjacent block before the analysis window. The reference is only
    the comparison baseline — the fit window is all loaded history before
    `analysis_start` either way. The response echoes the resolved windows and
    `reference_defaulted`.
    """
    if target not in dag:
        raise ValueError(f"Metric '{target}' not found in the metric tree.")

    reference_start, reference_end, reference_defaulted = resolve_reference_window(
        dag, data, target, analysis_start, analysis_end, reference_start, reference_end
    )
    _validate_windows(reference_start, reference_end, analysis_start, analysis_end)
    data = ensure_grained(data)

    # One seeded generator per call: bootstrap replicates (and hence every
    # contribution number) are identical across identical calls.
    rng = np.random.default_rng(0)

    nodes_in_scope = nx.ancestors(dag, target) | {target}

    # Resolve every node's grain frame and snapped windows once, and validate
    # coverage here — *before* any fitting. Coverage used to be checked per
    # node in the attribution loop below, which runs after the fits: a window
    # outside the loaded data therefore paid for an ADVI fit of every ancestor
    # (minutes, holding the caller's lock, leaving a cached trace each) and
    # only then 422'd. A window that holds no whole period at a node's grain is
    # *not* a coverage failure — that node is skipped here and reported with a
    # status below, exactly as before.
    scoped: Dict[str, Tuple[str, pd.DataFrame, Any, Any]] = {}
    for node in sorted(nodes_in_scope):
        defn = dag.nodes[node]["definition"]
        grain = fit_grain(dag, node)
        frame = data.fit_frame(node, list(dag.predecessors(node)), grain)
        snapped_ref = snap_window(reference_start, reference_end, grain)
        snapped_an = snap_window(analysis_start, analysis_end, grain)
        if snapped_ref is None or snapped_an is None:
            scoped[node] = (grain, frame, None, None)
            continue
        _validate_coverage(frame, node, grain, snapped_ref, snapped_an, defn.lags)
        scoped[node] = (grain, frame, snapped_ref, snapped_an)

    # Fit any probabilistic (non-formula, non-root) node in scope that lacks a
    # cached trace for this analysis window. Formula nodes and roots need no
    # fit; nodes whose windows hold no whole period at their grain are skipped
    # (they are reported below without attribution).
    #
    # The work list is built before fitting rather than fitted inline, so the
    # caller's `progress` callback can report a real denominator ("2 of 5")
    # instead of counting up to an unknown total. `sorted` also makes the fit
    # order deterministic — `nodes_in_scope` is a set, so the previous order
    # varied between processes, which a progress display makes visible.
    to_fit = []
    for node in sorted(nodes_in_scope):
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and (node, analysis_start) not in traces:
            if scoped[node][2] is None:
                continue
            to_fit.append(node)

    # A node whose own fit raises is recorded and skipped, not propagated: one
    # unfittable node (a parent held flat all fit window has zero variance and
    # cannot be normalized — the seasonal business whose default state is zero)
    # used to abort the whole tree analysis and return nothing. The `try` wraps
    # the single `fit_metric` call and nothing else, so unrelated failures
    # elsewhere in the loop still surface.
    fit_failures: Dict[str, str] = {}
    for i, node in enumerate(to_fit, 1):
        _report(progress, stage="fitting", metric=node, current=i, total=len(to_fit))
        try:
            traces[(node, analysis_start)] = fit_metric(
                dag,
                data,
                node,
                draws=advi_draws,
                inference_method="advi",
                fit_end=analysis_start,
                random_seed=0,
            )
        except ValueError as e:
            fit_failures[node] = str(e)

    _report(progress, stage="attributing", total=len(to_fit))

    nodes_out: Dict[str, Any] = {}
    # Sorted order fixes the rng consumption sequence (set iteration order is
    # not stable across processes).
    for node in sorted(nodes_in_scope):
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        # Grain frame, snapped windows and coverage were all resolved above.
        grain, frame, snapped_ref, snapped_an = scoped[node]

        # Windows are interpreted per node at its grain: only whole periods
        # fully inside the requested [start, end] count. A window too short
        # for the grain reports a status instead of failing the whole RCA.
        if snapped_ref is None or snapped_an is None:
            nodes_out[node] = _node_out(status="window_shorter_than_grain", grain=grain)
            continue
        ref_start, ref_end = snapped_ref.first_start, snapped_ref.last_start
        an_start, an_end = snapped_an.first_start, snapped_an.last_start
        single_period = snapped_ref.n_periods == 1 or snapped_an.n_periods == 1
        block = BOOT_BLOCK[grain]

        effective_windows = {
            "reference": _window_info(snapped_ref),
            "analysis": _window_info(snapped_an),
        }
        baseline = window_mean(frame, node, ref_start, ref_end)
        actual = window_mean(frame, node, an_start, an_end)
        gap = actual - baseline
        relative_change = gap / baseline if abs(baseline) >= 1e-12 else None

        # An unfittable node still reports its own movement — baseline, actual
        # and gap are read off the data, not the model. Only the attribution is
        # missing, and only for this node.
        if node in fit_failures:
            nodes_out[node] = _node_out(
                status="fit_failed",
                status_reason=fit_failures[node],
                grain=grain,
                effective_windows=effective_windows,
                baseline=baseline,
                actual=actual,
                gap=gap,
                relative_change=relative_change,
            )
            continue

        contributions = []
        components = None
        inference_method = None
        fit_quality = None
        sign_warnings = None
        fit_window = None
        seasonality_warnings = None
        interaction = None
        if not parents:
            attribution_method = None
            unexplained = None
            ci_status = None
        elif defn.formula:
            attribution_method = "shapley"
            try:
                sh = shapley_attribution(
                    dag,
                    data,
                    node,
                    analysis_start=analysis_start,
                    analysis_end=analysis_end,
                    reference_start=reference_start,
                    reference_end=reference_end,
                )
            except NonFiniteAttribution as e:
                # One node's zero denominator is not the tree's problem — every
                # other node still reports, with this one carrying the reason.
                # The target is the exception: the whole response is about that
                # node, so an empty answer for it is no answer at all; raise and
                # let the caller (422 over HTTP) show the diagnostic.
                if node == target:
                    raise
                nodes_out[node] = _node_out(
                    status="attribution_failed",
                    status_reason=str(e),
                    grain=grain,
                    effective_windows=effective_windows,
                    baseline=baseline,
                    actual=actual,
                    gap=gap,
                    relative_change=relative_change,
                    attribution_method=attribution_method,
                )
                continue

            # Bootstrap the windows jointly across parents (one set of day
            # indices per replicate) to preserve cross-metric correlation, then
            # run the same three-game decomposition per replicate, vectorized
            # over all replicates: a window-means bridge on the resampled
            # means, and each window's per-day co-movement game against that
            # replicate's own resampled means (replicate b occupies positions
            # [b*n, (b+1)*n) of the flattened per-day games).
            ref_vals = {
                p: _window_values(
                    frame,
                    p,
                    shift_periods(ref_start, -defn.lags.get(p, 0), grain),
                    shift_periods(ref_end, -defn.lags.get(p, 0), grain),
                )
                for p in parents
            }
            an_vals = {
                p: _window_values(
                    frame,
                    p,
                    shift_periods(an_start, -defn.lags.get(p, 0), grain),
                    shift_periods(an_end, -defn.lags.get(p, 0), grain),
                )
                for p in parents
            }
            n_an = len(next(iter(an_vals.values())))
            n_ref = len(next(iter(ref_vals.values())))
            ref_idx = _block_bootstrap_indices(n_ref, _N_BOOT, rng, block=block)
            an_idx = _block_bootstrap_indices(n_an, _N_BOOT, rng, block=block)
            boot_ref_means = {p: ref_vals[p][ref_idx].mean(axis=1) for p in parents}
            boot_an_means = {p: an_vals[p][an_idx].mean(axis=1) for p in parents}

            phi_means = compute_shapley(
                defn.formula, parents, boot_ref_means, boot_an_means, node=node
            )
            phi_cov_an = compute_shapley(
                defn.formula,
                parents,
                {p: np.repeat(boot_an_means[p], n_an) for p in parents},
                {p: an_vals[p][an_idx].reshape(-1) for p in parents},
                node=node,
            )
            phi_cov_ref = compute_shapley(
                defn.formula,
                parents,
                {p: np.repeat(boot_ref_means[p], n_ref) for p in parents},
                {p: ref_vals[p][ref_idx].reshape(-1) for p in parents},
                node=node,
            )

            # The published point is the **exact** Shapley value; the bootstrap
            # supplies only the interval around it (roadmap C3).
            #
            # Both numbers were already computed here. Reporting the bootstrap
            # *mean* as the estimate broke efficiency: the decomposition is
            # nonlinear in the window means, and joint resampling gives the
            # replicated means a nonzero covariance, so E[phi_boot] != phi_exact
            # and the contributions no longer reconciled with the node's own
            # gap. `unexplained` was always computed from the exact call
            # (`gap - sh["gap"]` below), so the two halves of the same identity
            # disagreed by the bias — small on a multilinear formula, unbounded
            # in principle on a ratio with a noisy denominator.
            #
            # A single-period window degenerates the block bootstrap to
            # identical replicates; report no interval rather than a
            # falsely-zero-width one.
            #
            # Individual replicates can also come out non-finite where the
            # exact decomposition did not — a resampled denominator mean can
            # land on ~0 even when no single period is zero. NaN propagates
            # through `np.percentile` into Starlette's `allow_nan=False`
            # encoder as an unhandled 500 (and into `null`s over MCP), so drop
            # those replicates and report on what survives, withholding the
            # interval entirely if too few do — the same posture as
            # `slices._excess_fields` and as `single_period`.
            withheld_nonfinite = False

            def _finite(samples: np.ndarray) -> Optional[np.ndarray]:
                nonlocal withheld_nonfinite
                finite = samples[np.isfinite(samples)]
                if finite.size < _MIN_CI_REPLICATES:
                    withheld_nonfinite = True
                    return None
                if finite.size < samples.size:
                    withheld_nonfinite = True
                return finite

            def _ci(samples: np.ndarray) -> Optional[list]:
                if single_period:
                    return None
                finite = _finite(samples)
                if finite is None:
                    return None
                return [
                    float(np.percentile(finite, 2.5)),
                    float(np.percentile(finite, 97.5)),
                ]

            def _summary(point: float, samples: np.ndarray) -> Dict[str, Any]:
                return {"estimate": float(point), "ci_95": _ci(samples)}

            interaction_b = np.zeros(_N_BOOT)
            interaction_exact = 0.0
            for p in parents:
                means_b = phi_means[p]
                comovement_b = phi_cov_an[p].reshape(_N_BOOT, n_an).mean(axis=1) - phi_cov_ref[
                    p
                ].reshape(_N_BOOT, n_ref).mean(axis=1)
                interaction_b = interaction_b + comovement_b
                phi_b = means_b + comovement_b

                # The same three parts as the bootstrap replicates, evaluated
                # once on the observed windows: attribution = means +
                # covariance_analysis - covariance_reference.
                parts = sh["decomposition"][p]
                means_exact = parts["means"]
                comovement_exact = parts["covariance_analysis"] - parts["covariance_reference"]
                estimate = float(sh["attribution"][p])
                interaction_exact += comovement_exact

                phi_b_finite = None if single_period else _finite(phi_b)
                contribution = {
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": _ci(phi_b),
                    "prob_same_direction": None
                    if phi_b_finite is None
                    else float(max((phi_b_finite > 0).mean(), (phi_b_finite < 0).mean())),
                    # Two-level view: the window-means bridge part and the
                    # co-movement (covariance/Jensen) shift part. They sum to
                    # `estimate` exactly — as exact values, not just in
                    # expectation over replicates.
                    "decomposition": {
                        "means": _summary(means_exact, means_b),
                        "comovement": _summary(comovement_exact, comovement_b),
                    },
                }
                # Lagged parents were measured over their own earlier windows;
                # surface which ones. Keys absent entirely when unlagged.
                if defn.lags.get(p, 0):
                    contribution["lag"] = defn.lags[p]
                    contribution["parent_windows"] = _lagged_windows(
                        snapped_ref, snapped_an, defn.lags[p], grain
                    )
                contributions.append(contribution)
            # The headline view's explicit interaction row: the total
            # within-window co-movement shift across parents, shown as its own
            # line instead of silently folded into the factors. Note it is
            # already *inside* each contribution's `estimate` — it is a
            # readout of the same quantity, never a term to add on top.
            interaction = _summary(interaction_exact, interaction_b)
            # `nonfinite_bootstrap_replicates`: at least one interval on this
            # node was computed from a subset of the replicates, or withheld
            # because too few survived. The point estimates are unaffected —
            # they are the exact Shapley values, never bootstrap means.
            ci_status = (
                "degenerate_single_period"
                if single_period
                else ("nonfinite_bootstrap_replicates" if withheld_nonfinite else "ok")
            )
            unexplained = gap - sh["gap"]
        else:
            attribution_method = "posterior"
            fit = traces[(node, analysis_start)]
            inference_method = fit.inference_method
            fit_quality = fit.diagnostics.get("fit_quality")
            sign_warnings = fit.diagnostics.get("sign_warnings")
            # What the model actually trained on: all loaded whole periods
            # before analysis_start — not the reference window.
            fit_window = {
                "start": str(fit.dates[0].date()),
                "end": str(fit.dates[-1].date()),
                "n_periods": int(len(fit.dates)),
            }
            seasonality_warnings = fit.diagnostics.get("seasonality_warnings")
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
            t_an = steps_between(pd.DatetimeIndex(frame.loc[an_mask, "date"]), fit.dates[0], grain)

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
            trend_delta = (trend_samples[:, -1] - trend_samples[:, t_ref].mean(axis=1)) * fit.y_std
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
                    frame,
                    p,
                    shift_periods(ref_start, -lag, grain),
                    shift_periods(ref_end, -lag, grain),
                )
                p_an_vals = _window_values(
                    frame,
                    p,
                    shift_periods(an_start, -lag, grain),
                    shift_periods(an_end, -lag, grain),
                )
                r_idx = (
                    ref_idx
                    if len(p_ref_vals) == ref_idx.shape[1]
                    else _block_bootstrap_indices(len(p_ref_vals), _N_BOOT, rng, block=block)
                )
                a_idx = (
                    an_idx
                    if len(p_an_vals) == an_idx.shape[1]
                    else _block_bootstrap_indices(len(p_an_vals), _N_BOOT, rng, block=block)
                )
                delta_samples = p_an_vals[a_idx].mean(axis=1) - p_ref_vals[r_idx].mean(axis=1)
                # Shuffle so posterior draw i is not systematically paired with
                # the same bootstrap replicate across parents.
                delta_samples = rng.permutation(delta_samples)
                samples = arr[:, i] * delta_samples[np.arange(n_post) % _N_BOOT]
                estimate = float(samples.mean())
                estimate_sum += estimate
                contribution = {
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": [
                        float(np.percentile(samples, 2.5)),
                        float(np.percentile(samples, 97.5)),
                    ],
                    "prob_same_direction": float(max((samples > 0).mean(), (samples < 0).mean())),
                }
                # Lagged parents were measured over their own earlier windows;
                # surface which ones. Keys absent entirely when unlagged.
                if lag:
                    contribution["lag"] = lag
                    contribution["parent_windows"] = _lagged_windows(
                        snapped_ref, snapped_an, lag, grain
                    )
                contributions.append(contribution)
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

        nodes_out[node] = _node_out(
            status="ok",
            grain=grain,
            effective_windows=effective_windows,
            baseline=baseline,
            actual=actual,
            gap=gap,
            relative_change=relative_change,
            attribution_method=attribution_method,
            inference_method=inference_method,
            fit_quality=fit_quality,
            sign_warnings=sign_warnings,
            fit_window=fit_window,
            seasonality_warnings=seasonality_warnings,
            ci_status=ci_status,
            unexplained=unexplained,
            components=components,
            interaction=interaction,
            contributions=contributions,
        )

    return {
        "target": target,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "reference_defaulted": reference_defaulted,
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
            # A non-finite share slips straight through `min(abs(share), 1.0)`
            # (NaN compares false against everything), and one NaN term poisons
            # the score of every ancestor above it — the whole ranking, from one
            # node. An undefined share carries no evidence about influence, so
            # it weighs nothing, exactly like the `None` case.
            weight = 0.0 if share is None or not np.isfinite(share) else min(abs(share), 1.0)
            term = score[child] * weight
            score[parent] += term
            if term > best_term[parent]:
                best_term[parent] = term
                via[parent] = child

    ranked = [
        {"metric": n, "score": score[n], "via": via.get(n)} for n in nodes_in_scope if n != target
    ]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked

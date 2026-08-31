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
of the window rows (block <= 7 days and never more than a quarter of the
window, resampled jointly across metrics so cross-metric correlation within a
window is preserved), seeded per `run_rca` call so API responses are
deterministic. An interval that comes out zero-width — a window over which a
parent never moves resamples to the same mean every time — is withheld rather
than published, since it reports certainty the resampling never established.

Unfitted probabilistic nodes in scope are fit on demand with ADVI on data
strictly before the analysis window; the caller passes its trace cache, keyed
by `(name, fit_end)`, and new fits are added to it in place.

The `ranked_causes` list is a documented **heuristic**: it propagates an
influence score from the target up the ancestor tree, weighting each hop by the
parent's share of its child's gap — capped at 1 and divided by the node's
cancellation factor, so a parent that "explains" 165% of a gap ranks below one
that explains a clean 80% (`_hop_weights`). It is meant as a triage ordering,
not a rigorous multi-hop uncertainty propagation. It ranks the nodes some hop
actually reached; a node nothing attributed to is reported in `nodes`, not
ranked.

**One bad node does not end the analysis.** Every node in scope carries a
`status`; anything other than `"ok"` reports the node without attribution and
lets the rest of the tree through, with the reason in `status_reason`:

- `"window_shorter_than_grain"` — the windows hold no whole period at the
  node's grain (`status_reason` names the grain and the windows). When the
  **target** itself has no whole period there is nothing to attribute anywhere,
  so that case raises *before any fitting*, naming the grain and the most
  recent whole period that would work;
- `"fit_failed"` — the node's own BSTS fit raised (a parent held flat for the
  whole fit window has zero variance and cannot be normalized);
- `"attribution_failed"` — the node's formula does not produce a finite value
  over these windows (a zero denominator, or an undefined rate parent). The
  exception is the RCA *target*: the whole response is about that node, so its
  failure is raised rather than buried in a status.
- `"undefined_over_window"` — every period of one of the node's windows is
  undefined, so the node has no value there to compare (roadmap 1.11c). A rate
  aggregates as `Σnumerator / Σdenominator`, so a window merely *containing*
  undefined periods is still fine; this is the case where nothing survives.

`unexplained` is accompanied by `unexplained_status`, which says what the
number is: `"measured"` (the node's own fetched series was compared against
the decomposition) or `"definitional"` (the node is **derived** — its series is
the formula, so the zero means nothing was checked).
"""

from typing import Any, Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine.model import (
    FIT_RANDOM_SEED,
    NUTS_DRAWS,
    cached_fit_is_usable,
    compute_shapley,
    fit_metric,
    seasonal_window_delta,
)
from breakdown.engine.progress import ProgressFn
from breakdown.engine.progress import report as _report
from breakdown.engine.stats import (
    DEGENERATE_CI_REL,
    GAP_REL_EPS,
    MIN_CI_REPLICATES,
    N_BOOT,
    block_bootstrap_indices,
    degenerate_means,
    direction_fields,
    node_scale,
    sample_summary,
    share_of_gap,
)
from breakdown.engine.windows import (
    UndefinedOverWindow,
    node_window_value,
    rate_window_method,
    rate_window_method_reason,
    window_info,
    window_values,
)
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
    to_date,
)
from breakdown.parser import _SIMPLE_RATIO

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


def _earliest_readable_reference(dag: nx.DiGraph, data, target: str) -> pd.Timestamp:
    """The earliest date a reference window may start and still be readable by
    every node in `target`'s scope (roadmap M1).

    `_validate_coverage` requires each node's reference window — and each
    lagged parent's *shifted* reference window — to lie inside that node's own
    data. Two things push the floor later than the tree-wide `data_start` the
    default window clamps to: a node whose grain frame starts late (a coarse
    grain, or a metric the provider returned less history for), and a lag,
    which reads a parent from `lag` whole periods before the window itself.

    Returns a floor the *default* window can respect. An explicitly requested
    window is not touched by this: a caller who names dates that fall outside
    the data still gets the coverage error, because they chose those dates.
    """
    grained = ensure_grained(data)
    floor = grained.date_start
    for node in nx.ancestors(dag, target) | {target}:
        parents = list(dag.predecessors(node))
        grain = fit_grain(dag, node)
        try:
            frame = grained.fit_frame(node, parents, grain)
        except (ValueError, RuntimeError):
            # A node whose frame cannot be built at all reports its own status
            # (or raises) downstream; it constrains no window here.
            continue
        lags = dag.nodes[node]["definition"].lags or {}
        max_lag = max((lags.get(p, 0) for p in parents), default=0)
        floor = max(floor, shift_periods(pd.Timestamp(frame["date"].min()), max_lag, grain))
    return floor


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

    A defaulted window is one the engine will then accept: the block is clamped
    to `_earliest_readable_reference`, not merely to the data start, so a
    lagged node cannot make the engine 422 on a window the caller never typed
    (roadmap M1). If even one period of reference does not fit, the refusal
    comes from here and names the lag, rather than from a coverage check citing
    a shifted date out of nowhere.
    """
    if (reference_start is None) != (reference_end is None):
        raise ValueError(
            "Pass both reference_start and reference_end, or neither "
            "(omitting both uses the default reference window)."
        )
    if reference_start is not None:
        return reference_start, reference_end, False
    week_align, coarsest_grain = _reference_alignment(dag, target)
    grained = ensure_grained(data)
    ref_start, ref_end = default_reference_window(
        analysis_start,
        analysis_end,
        grained.date_start,
        week_align=week_align,
        coarsest_grain=coarsest_grain,
        earliest_start=_earliest_readable_reference(dag, data, target),
    )
    return ref_start, ref_end, True


def _last_whole_period(frame: pd.DataFrame, grain: str) -> Optional[str]:
    """The most recent whole period a node has data for, as "start → end".

    For the refusal message when a target's windows hold no whole period: an
    error that names a window that *would* work is a remedy, one that only
    names the rule is homework. Grain frames hold whole periods by the shared
    date contract (partial edges are dropped at fetch), so the last label is
    the start of a complete period, not a partial one.
    """
    dates = pd.to_datetime(frame["date"])
    if not len(dates):
        return None
    start = dates.max()
    end = next_start(start, grain) - pd.Timedelta(days=1)
    return f"{start.date()} → {end.date()}"


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
            parsed[label] = to_date(value, label)
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
    `window_values`. The case this catches is the quiet one: a window that
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
        # Roadmap S2. `fit_quality` is the gate ("do not trust this fit as-is");
        # these are the evidence behind it for a variational fit, the way R-hat
        # and ESS are for NUTS. `khat` is the PSIS shape parameter and
        # `khat_status` its band: `ok` (<= 0.5), `suspect` (<= 0.7), `unusable`
        # (> 0.7) or `unavailable` (could not be computed — an unchecked fit,
        # not a clean one). All of them stay null on a NUTS fit, which is the
        # default: NUTS is not an approximation, so it has no k-hat, and the
        # absence of the field is not a missing check.
        #
        # `khat_se` and `khat_borderline` are roadmap S22: k-hat's own
        # Monte-Carlo standard error, and whether the estimate sits within one
        # of them of a band edge. They travel with the band for the same reason
        # the band travels with the coefficient — a verdict quoted without its
        # error is read as exact.
        "khat": None,
        "khat_se": None,
        "khat_status": None,
        "khat_borderline": None,
        "khat_warnings": None,
        "sign_warnings": None,
        # Roadmap S4. A property of the *design matrix* this node was fitted
        # on, not of the trace: `collinearity_status` is `ok` (checked, the
        # parents are separable), `moderate` (their total is better determined
        # than the split), `high` (the split is not determined at all),
        # `unavailable` (the check could not run — unchecked, not clean) or
        # null, which means there was nothing to check: a formula node, a
        # single parent, or a node that was never fitted. `collinearity`
        # carries the evidence — the flagged pairs with their correlations,
        # the flagged parents with their VIFs, and `max_abs_correlation` so an
        # `ok` is a measurement rather than an assertion.
        #
        # It never moves `fit_quality`. A collinear fit is not a bad fit; it
        # is a correct fit that is honestly unsure about the split, and the
        # thing to distrust is the per-parent `contributions` and
        # `share_of_gap`, not the node.
        "collinearity_status": None,
        "collinearity": None,
        "collinearity_warnings": None,
        # Roadmap S3. A property of the *model*, not of the sampler that fitted
        # it or of the design matrix it was handed: `ppc_status` is `ok`
        # (checked, the model reproduces its own data on every statistic),
        # `moderate`, `severe` (the worst band any statistic reached),
        # `unavailable` (the check could not run — unchecked, not clean) or
        # null, which means there was nothing to check: a formula node, or a
        # node that was never fitted. `ppc` carries every statistic with its
        # p-value, flagged or not, so an `ok` is a measurement rather than an
        # assertion.
        #
        # `severe` *does* move `fit_quality` — unlike collinearity above. A
        # collinear fit is a correct fit that is unsure about the split; a fit
        # whose model cannot generate its own data is not correct, and the
        # gate is what says so.
        "ppc_status": None,
        "ppc": None,
        "ppc_warnings": None,
        "fit_window": None,
        "seasonality_warnings": None,
        "likelihood_warnings": None,
        "ci_status": None,
        "unexplained": None,
        # What the number in `unexplained` *is*. Never omit it while
        # `unexplained` is present: a 0.0 that was measured and a 0.0 that
        # exists by construction are the same character on screen and
        # completely different facts about the world (roadmap 1.11a).
        #
        # - `"measured"` — the node's own series was fetched and the identity
        #   (or the fitted model) was compared against it. Zero means the
        #   decomposition reconciled with reality.
        # - `"definitional"` — the node is **derived**: its series *is* the
        #   formula, so zero means nothing was checked. This is not a weaker
        #   measurement; it is the absence of one.
        # - `null` — no attribution ran, so there is no residual either.
        "unexplained_status": None,
        # How `baseline`/`actual` were formed for a `kind: rate` node, and null
        # for anything else — see `rate_window_method`. A rate whose window
        # value is the mean of its per-period ratios must not read like one
        # whose value is Σnumerator / Σdenominator, and a rate that *has* no
        # denominator must not read like one nobody has configured yet: same
        # arithmetic, different facts about the world, exactly like the
        # definitional and measured zeros above.
        "window_aggregate": None,
        # For the two `period_mean_*` states that have an author behind them,
        # the reason in the author's own words (`no_denominator:`) or the
        # consequence of the silence. Null when the aggregate is `components` —
        # there is nothing to explain about the right answer.
        "window_aggregate_reason": None,
        "components": None,
        "interaction": None,
        "contributions": [],
    }
    record.update(fields)
    return record


def _nonfinite_window_clauses(
    frame: pd.DataFrame,
    parents,
    lags: Dict[str, int],
    grain: str,
    windows,
    include_zeros: bool,
) -> list:
    """Name which parent series is undefined (or zero, when zeros are fatal)
    over which window, on which dates — the clause list both non-finite
    refusals build their message from.

    An **undefined** period and a zero are reported separately: they have
    different remedies. A zero denominator is a data fact to narrow the window
    around; an undefined rate period is a period the source had no value for,
    and the node it feeds cannot be decomposed over any window containing one
    (roadmap 1.11c). Zeros are only fatal where something divides by them
    (`include_zeros`, the formula path); the posterior path multiplies, so
    only non-finite values are named there.
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
            checks = [
                ("undefined on", np.isnan(vals)),
                (
                    "zero or non-finite on" if include_zeros else "non-finite on",
                    ((vals == 0.0) if include_zeros else np.zeros(vals.shape, dtype=bool))
                    | (~np.isfinite(vals) & ~np.isnan(vals)),
                ),
            ]
            for kind_label, bad in checks:
                if not bad.any():
                    continue
                dates = [str(d.date()) for d in sub.loc[bad, "date"]]
                shown = ", ".join(dates[:_MAX_SHOWN_DATES])
                if len(dates) > _MAX_SHOWN_DATES:
                    shown += ", …"
                via = f" (read at lag {lag})" if lag else ""
                clauses.append(
                    f"parent '{p}'{via} is {kind_label} {len(dates)} of "
                    f"{len(vals)} {label}-window {grain}(s) ({shown})"
                )
    return clauses


def _refuse_nonfinite_parent_windows(
    frame: pd.DataFrame,
    node: str,
    parents,
    lags: Dict[str, int],
    grain: str,
    windows,
) -> None:
    """Refuse a posterior attribution whose parent windows hold a non-finite
    value, before any of it is computed (roadmap C29, grill H1).

    The fit never sees these periods — it ends at `analysis_start`, so a rate
    parent with a zero-denominator period *inside* the analysis window passes
    every fit-time check — and without this refusal the NaN rides
    `beta_raw × Δwindow` into `estimate`, a `[nan, nan]` interval that defeats
    the degeneracy guard (a NaN comparison is False), a `prob_same_direction`
    below its own 0.5 floor, and Starlette's `allow_nan=False` encoder as an
    unhandled 500. The formula branch of this same function has refused this
    by name since C17; this is the same policy, one `elif` over. Filtering the
    samples to finite instead was rejected: replicates that happen to exclude
    the undefined period describe a quietly-censored window — a different
    number wearing the same key.
    """
    bad = [p for p in parents if not np.isfinite(frame[p].to_numpy(dtype=float)).all()]
    if not bad:
        return
    windowed = _nonfinite_window_clauses(frame, bad, lags, grain, windows, include_zeros=False)
    if not windowed:
        # Non-finite values only outside both windows never enter the window
        # deltas; the attribution is finite and there is nothing to refuse.
        return
    raise NonFiniteAttribution(
        f"Posterior attribution for '{node}' is not a finite number: "
        f"{'; '.join(windowed)}. "
        "An undefined parent period inside a window leaves the contribution "
        "with no finite value: narrow the window to exclude those periods, or "
        "fix the series at the source."
    )


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
    clauses = _nonfinite_window_clauses(frame, parents, lags, grain, windows, include_zeros=True)
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
        "whose denominator is zero — or whose rate parent is undefined there — "
        "has no finite decomposition: narrow the window to exclude those "
        "periods, or fix the series at the source."
    )


def aggregates_from_components(defn, parents) -> bool:
    """Whether this node's window value is `formula(parent window means)`
    exactly, rather than the mean of the per-period formula.

    True for the shape roadmap 1.11 documents as the remedy for a rate over
    true-zero periods: `kind: rate` with `formula: "num / den"` where `den` is
    the node's declared `denominator`. There

        Σnum / Σden  =  mean(num) / mean(den)

    over the same periods — the window rate *is* the formula of the window
    aggregates — so the decomposition is the window-means bridge and nothing
    else. The within-window co-movement games exist only to bridge from
    `f(means)` to `mean(f(per period))`, which is not this node's quantity, so
    they are not merely zero here: they are **absent**, and the payload omits
    them rather than publishing a 0.0 with a zero-width interval.
    """
    if defn.kind != "rate" or not defn.formula or defn.denominator is None:
        return False
    m = _SIMPLE_RATIO.match(defn.formula)
    return bool(m and m.group(2) == defn.denominator and set(m.groups()) <= set(parents))


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
        return window_values(
            frame, p, shift_periods(start, -lag, grain), shift_periods(end, -lag, grain)
        )

    ref_daily = {p: lagged_window(p, ref_start, ref_end) for p in parents}
    an_daily = {p: lagged_window(p, an_start, an_end) for p in parents}

    from_components = aggregates_from_components(defn, parents)
    if from_components:
        # The window rate is Σnum / Σden over the periods where it is defined,
        # which is `formula(parent means over those same periods)`. Masking to
        # the defined periods is what makes the two identical rather than
        # merely close: a period whose denominator is zero contributes to
        # neither sum, so it must not contribute to either mean.
        def _means(daily):
            defined = np.isfinite(eval_formula(defn.formula, daily))
            if not defined.any():
                return {p: float("nan") for p in parents}
            return {p: float(daily[p][defined].mean()) for p in parents}

        ref_means = _means(ref_daily)
        an_means = _means(an_daily)
        baseline = float(eval_formula(defn.formula, ref_means))
        actual = float(eval_formula(defn.formula, an_means))
    else:
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

    attribution: Dict[str, float] = {}
    decomposition: Dict[str, Dict[str, float]] = {}
    if from_components:
        # No co-movement games at all: the node's quantity is the formula of
        # the aggregates, so the bridge from `f(means)` to `mean(f(daily))`
        # that those games compute is not part of this gap. Omitted, not
        # zeroed — a term the decomposition does not contain must not be
        # reported as a term measured to be zero.
        for p in parents:
            attribution[p] = float(phi_means[p])
            decomposition[p] = {"means": float(phi_means[p])}
    else:
        phi_cov_an = compute_shapley(defn.formula, parents, an_means, an_daily, node=target)
        phi_cov_ref = compute_shapley(defn.formula, parents, ref_means, ref_daily, node=target)
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
        # Which quantity was decomposed: `per_period` is the mean over the
        # window's periods of `formula(parents that period)`; `components` is
        # `formula(parent window aggregates)`, which is what a rate's window
        # value is (Σnumerator / Σdenominator). The two differ, so the payload
        # says which one the numbers are about.
        "aggregation": "components" if from_components else "per_period",
        # Whether the node's own series was fetched and compared against this
        # identity, or derived from it. A derived node's `unexplained` is zero
        # because there was nothing to check against, never because a check
        # passed (roadmap 1.11a).
        "derived": bool(defn.derived),
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "reference_defaulted": reference_defaulted,
        "effective_windows": {
            "reference": window_info(snapped_ref),
            "analysis": window_info(snapped_an),
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
    inference_method: str = "nuts",
    draws: int = NUTS_DRAWS,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """Attribute `target`'s window-over-window change to its ancestors.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` -> FitResult.
    Probabilistic nodes in scope without a usable cached trace are fitted on data
    strictly before `analysis_start` (so the anomaly window is excluded) and added
    to it. A full-window fit (`fit_end=None`) is never reused here — it is
    contaminated by the anomaly for attribution purposes.

    `inference_method` is `"nuts"` — exact MCMC — by default, because roadmap S2
    measured mean-field ADVI failing the PSIS k-hat check on essentially every
    real node in this engine, moving *point estimates* by 37-57% and in one
    demo case turning an interval that excludes zero into one that does not.
    `"advi"` remains available for triage on a tree where NUTS is genuinely too
    slow (a wide day-grain tree); every node it fits then carries its k-hat and
    the warning that goes with it, so the trade is visible in the payload
    rather than assumed. `draws` is the posterior draw count either way
    (per chain under NUTS); it is forwarded here rather than left to
    `fit_metric` because this module resamples that posterior itself (see
    `N_BOOT` below), so its size is the orchestrator's business. The warm-up
    and chain budgets are pure sampler mechanics and come from `fit_metric`'s
    `NUTS_TUNE` / `NUTS_CHAINS` — one spelling for the whole engine, so that
    the route a caller arrives through cannot change the posterior they get
    (roadmap C27).

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
            # An *ancestor* whose windows hold no whole period degrades to a
            # per-node status below — the rest of the tree still answers. The
            # TARGET is different: with no measured movement there is nothing
            # to attribute anywhere, so the whole analysis is already void.
            # Refuse it here, before any fitting — a user once paid for every
            # ancestor's ADVI fit only to learn the target itself had no whole
            # week in an 8-day default window, from a message that named
            # neither the grain nor a window that would have worked.
            if node == target:
                which = []
                if snapped_an is None:
                    which.append(f"analysis window [{analysis_start}, {analysis_end}]")
                if snapped_ref is None:
                    which.append(f"reference window [{reference_start}, {reference_end}]")
                suggestion = _last_whole_period(frame, grain)
                raise ValueError(
                    f"Target '{target}' is measured at {grain} grain, and the "
                    + " and the ".join(which)
                    + f" hold{'s' if len(which) == 1 else ''} no whole {grain} — a "
                    f"{grain}-grain metric has one value per whole {grain}, so there "
                    "is nothing to measure and nothing was fitted. "
                    + (
                        f"The most recent whole {grain} with data runs {suggestion}; "
                        f"windows of whole {grain}s ending on or before that will work."
                        if suggestion
                        else f"Choose windows made of whole {grain}s."
                    )
                )
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
        if not parents or defn.formula:
            continue
        cached = traces.get((node, analysis_start))
        if cached is not None and cached_fit_is_usable(cached, inference_method):
            continue
        if scoped[node][2] is None:
            continue
        to_fit.append(node)

    # A node whose own fit raises is recorded and skipped, not propagated: one
    # unfittable node (a parent held flat all fit window has zero variance and
    # cannot be normalized — the seasonal business whose default state is zero)
    # used to abort the whole tree analysis and return nothing. The `try` wraps
    # the single `fit_metric` call and nothing else, so unrelated failures
    # elsewhere in the loop still surface.
    #
    # `inference_method` reaches `fit_metric` unchanged — this path never
    # substitutes a sampler for the one it was asked for, in either direction.
    # It used to hardcode `"advi"` and then re-fit with NUTS whatever PSIS
    # rejected; roadmap S2 measured that rejection firing on essentially every
    # real node, which made the escalation the common case rather than the
    # rescue, so the default moved to NUTS outright and the escalation went
    # away with it. A caller who asks for `"advi"` gets ADVI, and gets its
    # k-hat.
    #
    # The write is unconditional and that is safe under the reuse rule above:
    # a node reaches this loop only when the cache held nothing usable for the
    # method asked for, so the fit being stored is never worse than the one it
    # replaces.
    fit_failures: Dict[str, str] = {}
    for i, node in enumerate(to_fit, 1):
        _report(progress, stage="fitting", metric=node, current=i, total=len(to_fit))
        try:
            fit = fit_metric(
                dag,
                data,
                node,
                draws=draws,
                inference_method=inference_method,
                fit_end=analysis_start,
                random_seed=FIT_RANDOM_SEED,
            )
        except ValueError as e:
            fit_failures[node] = str(e)
            continue
        traces[(node, analysis_start)] = fit

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
        # fully inside the requested [start, end] count. A non-target node
        # whose windows hold no whole period reports a status instead of
        # failing the whole RCA. Through a parser-built tree this branch is a
        # backstop: the target is always the coarsest node in its own scope
        # (a parent may never be coarser than its child), so any window that
        # snaps to nothing here snapped to nothing for the target too, and the
        # pre-fit refusal above already fired. It stays for DAGs assembled
        # without the parser's grain validation.
        if snapped_ref is None or snapped_an is None:
            nodes_out[node] = _node_out(
                status="window_shorter_than_grain",
                grain=grain,
                # The reason names the grain, because the status alone made a
                # reader go look it up: every surface that renders a degraded
                # node already appends `status_reason`, so saying it here says
                # it in the table, the export, and the MCP payload at once.
                status_reason=(
                    f"'{node}' is measured at {grain} grain, and the windows "
                    f"({reference_start} → {reference_end} vs {analysis_start} → "
                    f"{analysis_end}) hold no whole {grain} fully inside them. "
                    f"Windows of whole {grain}s give this node a value."
                ),
            )
            continue
        ref_start, ref_end = snapped_ref.first_start, snapped_ref.last_start
        an_start, an_end = snapped_an.first_start, snapped_an.last_start
        single_period = snapped_ref.n_periods == 1 or snapped_an.n_periods == 1
        block = BOOT_BLOCK[grain]

        effective_windows = {
            "reference": window_info(snapped_ref),
            "analysis": window_info(snapped_an),
        }
        # The node's own window values, by kind — a rate aggregates from its
        # components rather than averaging its per-period ratios (1.11c), so a
        # window containing an undefined period is still defined. A window with
        # *no* defined period has no value at all: report the node without
        # numbers rather than publishing a NaN, which is rule 3 (no engine
        # result reaches an encoder unsanitized) and which Starlette would
        # otherwise turn into an unhandled 500.
        baseline = node_window_value(data, node, ref_start, ref_end, grain, frame=frame)
        actual = node_window_value(data, node, an_start, an_end, grain, frame=frame)
        # …and what that arithmetic *was*, said out loud. Both windows are asked
        # because only one of the four answers is window-dependent (a declared
        # denominator whose series does not reach this window), and a payload
        # claiming `components` when one of its two numbers is a period mean
        # would misdescribe itself.
        methods = {
            rate_window_method(data, node, s, e, grain, frame=frame)
            for s, e in ((ref_start, ref_end), (an_start, an_end))
        }
        window_aggregate = "period_mean_weights_unavailable" if len(methods) > 1 else methods.pop()
        window_aggregate_reason = rate_window_method_reason(data, node, window_aggregate)
        rate_fields = {
            "window_aggregate": window_aggregate,
            "window_aggregate_reason": window_aggregate_reason,
        }
        if not (np.isfinite(baseline) and np.isfinite(actual)):
            which = "reference" if not np.isfinite(baseline) else "analysis"
            reason = (
                f"'{node}' has no value over the {which} window: every period in "
                f"it is undefined (a rate whose denominator is zero has no rate). "
                f"Choose a window that contains at least one defined period."
            )
            if node == target:
                raise UndefinedOverWindow(reason)
            nodes_out[node] = _node_out(
                status="undefined_over_window",
                status_reason=reason,
                grain=grain,
                effective_windows=effective_windows,
                **rate_fields,
            )
            continue
        gap = actual - baseline
        # Everything the node publishes that asks "is this number zero?" — the
        # relative change, the shares, the width of an interval — is judged
        # against the node's own level rather than an absolute epsilon, so the
        # answers do not depend on whether the metric is denominated in
        # millions or in rates (roadmap C4a/C5).
        ci_scale = node_scale(baseline, actual)
        relative_change = gap / baseline if abs(baseline) > GAP_REL_EPS * ci_scale else None

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
                **rate_fields,
            )
            continue

        contributions = []
        components = None
        inference_method = None
        fit_quality = None
        khat = None
        khat_se = None
        khat_status = None
        khat_borderline = None
        khat_warnings = None
        sign_warnings = None
        collinearity_status = None
        collinearity = None
        collinearity_warnings = None
        ppc_status = None
        ppc = None
        ppc_warnings = None
        fit_window = None
        seasonality_warnings = None
        likelihood_warnings = None
        interaction = None
        unexplained_status = None
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
                    **rate_fields,
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
                p: window_values(
                    frame,
                    p,
                    shift_periods(ref_start, -defn.lags.get(p, 0), grain),
                    shift_periods(ref_end, -defn.lags.get(p, 0), grain),
                )
                for p in parents
            }
            an_vals = {
                p: window_values(
                    frame,
                    p,
                    shift_periods(an_start, -defn.lags.get(p, 0), grain),
                    shift_periods(an_end, -defn.lags.get(p, 0), grain),
                )
                for p in parents
            }
            from_components = sh["aggregation"] == "components"
            if from_components:
                # Resample the periods the window aggregate is actually made
                # of. An undefined period contributes to neither Σnum nor Σden,
                # so including it in the resampling would let replicates
                # disagree with the exact value about which periods exist. The
                # blocks then run over the defined subsequence, which is the
                # same compromise `block_bootstrap_indices` already makes
                # about serial dependence, applied to a shorter series.
                def _defined(vals):
                    keep = np.isfinite(eval_formula(defn.formula, vals))
                    return {p: vals[p][keep] for p in parents}

                ref_vals = _defined(ref_vals)
                an_vals = _defined(an_vals)
            n_an = len(next(iter(an_vals.values())))
            n_ref = len(next(iter(ref_vals.values())))
            ref_idx = block_bootstrap_indices(n_ref, N_BOOT, rng, block=block)
            an_idx = block_bootstrap_indices(n_an, N_BOOT, rng, block=block)
            boot_ref_means = {p: ref_vals[p][ref_idx].mean(axis=1) for p in parents}
            boot_an_means = {p: an_vals[p][an_idx].mean(axis=1) for p in parents}

            phi_means = compute_shapley(
                defn.formula, parents, boot_ref_means, boot_an_means, node=node
            )
            zero_b = np.zeros(N_BOOT * max(n_an, n_ref))
            phi_cov_an = (
                {p: zero_b[: N_BOOT * n_an] for p in parents}
                if from_components
                else compute_shapley(
                    defn.formula,
                    parents,
                    {p: np.repeat(boot_an_means[p], n_an) for p in parents},
                    {p: an_vals[p][an_idx].reshape(-1) for p in parents},
                    node=node,
                )
            )
            phi_cov_ref = (
                {p: zero_b[: N_BOOT * n_ref] for p in parents}
                if from_components
                else compute_shapley(
                    defn.formula,
                    parents,
                    {p: np.repeat(boot_ref_means[p], n_ref) for p in parents},
                    {p: ref_vals[p][ref_idx].reshape(-1) for p in parents},
                    node=node,
                )
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
            #
            # A single period is not the only way the resampling collapses
            # (roadmap C4a). *Any* window over which a parent is constant
            # contributes no sampling uncertainty, and a product formula with
            # one such parent held at zero collapses that window's replicates
            # entirely. Keyed on the resampled spread of the window means, not
            # on the period count. Every interval on the node is understated
            # when this fires (hence the node-level `ci_status`), and the ones
            # that came out flatly zero-width are withheld: a zero-width
            # interval is never a result.
            #
            # The *status* is keyed on the inputs rather than on the published
            # widths because some quantities here are zero-width by
            # construction: the co-movement term of an additive identity is
            # exactly zero for every replicate whatever the data. Such a term
            # reports its exact estimate with no interval; it is not a
            # degeneracy, and saying so would cry wolf on every additive node
            # in every tree.
            degenerate_inputs = any(
                degenerate_means(boot_ref_means[p]) or degenerate_means(boot_an_means[p])
                for p in parents
            )
            withheld_nonfinite = False

            def _finite(samples: np.ndarray) -> Optional[np.ndarray]:
                nonlocal withheld_nonfinite
                finite = samples[np.isfinite(samples)]
                if finite.size < MIN_CI_REPLICATES:
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
                lo = float(np.percentile(finite, 2.5))
                hi = float(np.percentile(finite, 97.5))
                # No zero-width interval is ever published, whatever produced
                # it — a collapsed resampling (the node's `ci_status` names
                # that one) or a term that is zero by construction, like the
                # co-movement of an additive identity.
                if hi - lo <= DEGENERATE_CI_REL * ci_scale:
                    return None
                return [lo, hi]

            def _summary(point: float, samples: np.ndarray) -> Dict[str, Any]:
                return {"estimate": float(point), "ci_95": _ci(samples)}

            interaction_b = np.zeros(N_BOOT)
            interaction_exact = 0.0
            for p in parents:
                means_b = phi_means[p]
                comovement_b = phi_cov_an[p].reshape(N_BOOT, n_an).mean(axis=1) - phi_cov_ref[
                    p
                ].reshape(N_BOOT, n_ref).mean(axis=1)
                interaction_b = interaction_b + comovement_b
                phi_b = means_b + comovement_b

                # The same three parts as the bootstrap replicates, evaluated
                # once on the observed windows: attribution = means +
                # covariance_analysis - covariance_reference. A node whose
                # window value is the formula of the aggregates has no
                # co-movement part at all (`aggregates_from_components`).
                parts = sh["decomposition"][p]
                means_exact = parts["means"]
                comovement_exact = (
                    0.0
                    if from_components
                    else (parts["covariance_analysis"] - parts["covariance_reference"])
                )
                estimate = float(sh["attribution"][p])
                interaction_exact += comovement_exact

                # `prob_same_direction` is withheld exactly when the interval
                # is: replicates that are all identical would report it as 1.0
                # (or, on an identically-zero contribution, 0.0), which is a
                # confidence read off no information at all.
                ci = _ci(phi_b)
                phi_b_finite = None if ci is None else _finite(phi_b)
                contribution = {
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": share_of_gap(estimate, gap, ci_scale),
                    "ci_95": ci,
                    **direction_fields(phi_b_finite),
                }
                # Two-level view: the window-means bridge part and the
                # co-movement (covariance/Jensen) shift part. They sum to
                # `estimate` exactly — as exact values, not just in expectation
                # over replicates. **Absent** when the node's window value is
                # the formula of the aggregates: there is no second level, and
                # reporting `comovement: {estimate: 0.0}` would claim a term
                # was measured to be zero when the decomposition does not
                # contain one. Same absent-means-no-such-term idiom as `lag`
                # and `components.seasonal`.
                if not from_components:
                    contribution["decomposition"] = {
                        "means": _summary(means_exact, means_b),
                        "comovement": _summary(comovement_exact, comovement_b),
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
            # readout of the same quantity, never a term to add on top. Absent
            # (not 0.0) when the decomposition contains no co-movement term.
            interaction = None if from_components else _summary(interaction_exact, interaction_b)
            # One status per node, most specific cause first:
            #
            # - `degenerate_single_period` — a one-period window, the special
            #   case of a collapsed resampling that can be named exactly;
            # - `degenerate_bootstrap_spread` — some parent is constant over one
            #   of the windows, so its resampled means never move: every
            #   interval on the node is understated by the missing window-
            #   sampling term, and any that came out zero-width was withheld;
            # - `nonfinite_bootstrap_replicates` — at least one interval was
            #   computed from a subset of the replicates, or withheld because
            #   too few survived.
            #
            # The point estimates are unaffected by any of them — they are the
            # exact Shapley values, never bootstrap means.
            ci_status = (
                "degenerate_single_period"
                if single_period
                else "degenerate_bootstrap_spread"
                if degenerate_inputs
                else "nonfinite_bootstrap_replicates"
                if withheld_nonfinite
                else "ok"
            )
            if defn.derived:
                # A derived node's series *is* `formula(parents)`, so the
                # decomposition cannot miss it: the residual is zero because
                # there was nothing to compare against, not because a
                # comparison came out clean. Published as exactly 0.0 (rather
                # than the float residue of subtracting a number from itself)
                # and labelled, so no reader can mistake it for the other zero.
                unexplained = 0.0
                unexplained_status = "definitional"
            else:
                unexplained = gap - sh["gap"]
                unexplained_status = "measured"
        else:
            attribution_method = "posterior"
            fit = traces[(node, analysis_start)]
            inference_method = fit.inference_method
            fit_quality = fit.diagnostics.get("fit_quality")
            khat = fit.diagnostics.get("khat")
            khat_se = fit.diagnostics.get("khat_se")
            khat_status = fit.diagnostics.get("khat_status")
            khat_borderline = fit.diagnostics.get("khat_borderline")
            khat_warnings = fit.diagnostics.get("khat_warnings")
            sign_warnings = fit.diagnostics.get("sign_warnings")
            # Roadmap S4: which parents this node's model cannot tell apart.
            collinearity_status = fit.diagnostics.get("collinearity_status")
            collinearity = fit.diagnostics.get("collinearity")
            collinearity_warnings = fit.diagnostics.get("collinearity_warnings")
            # Roadmap S3: whether this node's model can generate its own data.
            ppc_status = fit.diagnostics.get("ppc_status")
            ppc = fit.diagnostics.get("ppc")
            ppc_warnings = fit.diagnostics.get("ppc_warnings")
            # What the model actually trained on: all loaded whole periods
            # before analysis_start — not the reference window.
            fit_window = {
                "start": str(fit.dates[0].date()),
                "end": str(fit.dates[-1].date()),
                "n_periods": int(len(fit.dates)),
            }
            seasonality_warnings = fit.diagnostics.get("seasonality_warnings")
            likelihood_warnings = fit.diagnostics.get("likelihood_warnings")
            arr = fit.trace.posterior["beta_raw"].values.reshape(-1, len(parents))
            n_post = arr.shape[0]

            # Refuse-by-name before any attribution math (roadmap C29): the
            # same degrade the formula branch has had since C17, so one bad
            # node still does not end the analysis.
            try:
                _refuse_nonfinite_parent_windows(
                    frame,
                    node,
                    parents,
                    defn.lags,
                    grain,
                    [("reference", ref_start, ref_end), ("analysis", an_start, an_end)],
                )
            except NonFiniteAttribution as e:
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
                    **rate_fields,
                )
                continue

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
            # The model always carries a local level, so `trend` is always a
            # term it estimated. Seasonality is declared per node, and a node
            # that declares none has no seasonal parameters at all — the key is
            # absent rather than reporting a delta of 0.0 with a zero-width
            # interval, which asserted infinite precision about a term the
            # model does not contain. Absent-means-no-such-term is the same
            # idiom as `lag` / `parent_windows` on an unlagged contribution.
            components = {"trend": sample_summary(trend_delta, ci_scale)}
            if defn.seasonality:
                seasonal_delta = (
                    seasonal_window_delta(fit.trace, defn.seasonality, t_ref, t_an) * fit.y_std
                )
                components["seasonal"] = sample_summary(seasonal_delta, ci_scale)

            # Window bootstrap indices, shared across this node's parents
            # (joint resampling); lag-shifted windows span the same number of
            # periods on the grain spine, so the same position indices apply.
            ref_idx = block_bootstrap_indices(len(t_ref), N_BOOT, rng, block=block)
            an_idx = block_bootstrap_indices(len(t_an), N_BOOT, rng, block=block)

            estimate_sum = 0.0
            # The same degeneracy the formula path guards against (roadmap
            # C4a) reaches here through the parent's window means: a parent
            # constant over a window contributes no window-sampling
            # uncertainty, and one constant across *both* windows makes every
            # resampled difference exactly zero — `beta_raw x 0` is then a
            # zero-width interval on an identically-zero contribution. The
            # coefficient posterior is real uncertainty, but it is being
            # multiplied by a number the resampling cannot move.
            degenerate_inputs = False
            for i, p in enumerate(parents):
                lag = defn.lags.get(p, 0)
                # The parent values that influenced the analysis window are
                # those `lag` grain steps earlier, so shift both windows back
                # by whole periods (stays on the spine across month bounds).
                p_ref_vals = window_values(
                    frame,
                    p,
                    shift_periods(ref_start, -lag, grain),
                    shift_periods(ref_end, -lag, grain),
                )
                p_an_vals = window_values(
                    frame,
                    p,
                    shift_periods(an_start, -lag, grain),
                    shift_periods(an_end, -lag, grain),
                )
                r_idx = (
                    ref_idx
                    if len(p_ref_vals) == ref_idx.shape[1]
                    else block_bootstrap_indices(len(p_ref_vals), N_BOOT, rng, block=block)
                )
                a_idx = (
                    an_idx
                    if len(p_an_vals) == an_idx.shape[1]
                    else block_bootstrap_indices(len(p_an_vals), N_BOOT, rng, block=block)
                )
                boot_ref = p_ref_vals[r_idx].mean(axis=1)
                boot_an = p_an_vals[a_idx].mean(axis=1)
                degenerate_inputs = (
                    degenerate_inputs or degenerate_means(boot_ref) or degenerate_means(boot_an)
                )
                delta_samples = boot_an - boot_ref
                # Shuffle so posterior draw i is not systematically paired with
                # the same bootstrap replicate across parents.
                delta_samples = rng.permutation(delta_samples)
                samples = arr[:, i] * delta_samples[np.arange(n_post) % N_BOOT]
                estimate = float(samples.mean())
                estimate_sum += estimate
                lo = float(np.percentile(samples, 2.5))
                hi = float(np.percentile(samples, 97.5))
                # Withheld only when the interval is flatly zero-width; a
                # parent constant in one window still leaves the coefficient
                # posterior varying, and that interval is understated (hence
                # the node's `ci_status`) rather than absent.
                degenerate = hi - lo <= DEGENERATE_CI_REL * ci_scale
                contribution = {
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": share_of_gap(estimate, gap, ci_scale),
                    "ci_95": None if degenerate else [lo, hi],
                    # `n_effective=N_BOOT`: `samples` is one value per posterior
                    # draw, but the window delta multiplying them takes only
                    # `N_BOOT` distinct values, so a NUTS fit's 4,000 draws do
                    # not resolve the direction any finer than the bootstrap
                    # behind them does. Take the coarser of the two.
                    **direction_fields(None if degenerate else samples, n_effective=N_BOOT),
                }
                # Lagged parents were measured over their own earlier windows;
                # surface which ones. Keys absent entirely when unlagged.
                if lag:
                    contribution["lag"] = lag
                    contribution["parent_windows"] = _lagged_windows(
                        snapped_ref, snapped_an, lag, grain
                    )
                contributions.append(contribution)
            unexplained = gap - estimate_sum - sum(c["estimate"] for c in components.values())
            # A probabilistic node is always fetched (there is no derived
            # regression), so its residual is always a measurement.
            unexplained_status = "measured"
            # The beta_raw posterior still carries real uncertainty on a
            # single-period window; the flag says the window-sampling
            # component of the CI is absent.
            ci_status = (
                "posterior_only_single_period"
                if single_period
                else "degenerate_bootstrap_spread"
                if degenerate_inputs
                else "ok"
            )

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
            khat=khat,
            khat_se=khat_se,
            khat_status=khat_status,
            khat_borderline=khat_borderline,
            khat_warnings=khat_warnings,
            sign_warnings=sign_warnings,
            collinearity_status=collinearity_status,
            collinearity=collinearity,
            collinearity_warnings=collinearity_warnings,
            ppc_status=ppc_status,
            ppc=ppc,
            ppc_warnings=ppc_warnings,
            fit_window=fit_window,
            seasonality_warnings=seasonality_warnings,
            likelihood_warnings=likelihood_warnings,
            ci_status=ci_status,
            unexplained=unexplained,
            unexplained_status=unexplained_status,
            components=components,
            interaction=interaction,
            contributions=contributions,
            **rate_fields,
        )

    return {
        "target": target,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "reference_defaulted": reference_defaulted,
        "nodes": nodes_out,
        "ranked_causes": _rank_causes(dag, target, nodes_in_scope, nodes_out),
    }


def _hop_weights(contributions) -> Dict[str, float]:
    """Per-parent hop weights for one child node, in [0, 1] (roadmap C5).

    ``weight(p) = min(|share_p|, 1) / max(1, sum_q |share_q|)``.

    The numerator is the old rule. The denominator is the fix: it is the
    node's **cancellation factor** — how much gross parent movement the
    decomposition needed to produce the node's net gap. It is 1 when the
    parents simply split the gap, and large exactly when they fight.

    What that buys, in the terms the failure was reported in:

    - **A share above 1 is penalized, not saturated.** A lone parent explaining
      165% of its child's gap scores 1/1.65 = 0.61, below one explaining a
      clean 80%: needing 65% of cancellation from somewhere else is evidence
      *against* a clean explanation, not for one. (For a single-parent node
      the whole expression collapses to `min(s, 1/s)`.)
    - **Offsetting noise stops handing its full score upward.** Two parents at
      +0.5 and -0.5 of a child whose own gap is ~0 have shares near ±5x10^5 and
      a cancellation factor near 1e6, so they weigh ~1e-6 each instead of the
      clamped 1.0 each. A node with several such children accumulated 1.0 and
      tied a node explaining a clean 100% of the target; it now scores ~0 and
      the tie breaks toward the clean explainer.
    - **A hop can no longer inflate influence.** The weights of one node's
      parents sum to at most 1, so a child never passes upward more than its
      own score. They could previously sum to the parent count.

    It stays a triage weight, not a probability: the ordering among *one*
    node's parents is untouched (they all share the same denominator), so this
    only changes how much of a child's score survives the hop.
    """
    shares = {}
    for contrib in contributions:
        share = contrib["share_of_gap"]
        # A non-finite share slips straight through `min(abs(share), 1.0)`
        # (NaN compares false against everything), and one NaN term poisons the
        # score of every ancestor above it — the whole ranking, from one node.
        # An undefined share carries no evidence about influence, so it weighs
        # nothing, exactly like the `None` case.
        shares[contrib["parent"]] = (
            0.0 if share is None or not np.isfinite(share) else abs(float(share))
        )
    cancellation = max(1.0, sum(shares.values()))
    return {p: min(s, 1.0) / cancellation for p, s in shares.items()}


def _rank_causes(dag, target, nodes_in_scope, nodes_out):
    """Heuristic influence score: propagate 1.0 from the target up the ancestor
    tree, weighting each hop by `_hop_weights`.

    Processing in reverse topological order (target first) guarantees a child's
    score is complete before it is propagated to its parents.
    """
    score = {n: 0.0 for n in nodes_in_scope}
    score[target] = 1.0
    via: Dict[str, str] = {}
    best_term: Dict[str, float] = {n: float("-inf") for n in nodes_in_scope}

    topo_scope = [n for n in nx.topological_sort(dag) if n in nodes_in_scope]
    for child in reversed(topo_scope):
        weights = _hop_weights(nodes_out[child]["contributions"])
        for parent, weight in weights.items():
            if parent not in score:
                continue
            term = score[child] * weight
            score[parent] += term
            if term > best_term[parent]:
                best_term[parent] = term
                via[parent] = child

    # A node nothing ever attributed to is not a triage candidate. It used to
    # be listed anyway, as `{"score": 0.0, "via": null}` — a numbered row in a
    # ranking with no provenance, which cannot be acted on and forced every
    # consumer (UI, export, MCP compaction, anyone reading the JSON) to invent
    # the same filter. `via` is set on any hop that reached the node, including
    # a zero-weight one, so it separates the two ways a node scores zero:
    #
    # - **reached, and explains none of the gap** — kept, at 0.0 with the child
    #   it was reached through. That is a finding, and the same distinction the
    #   UI draws between "not analyzed" and "analyzed, found nothing";
    # - **never reached** — dropped. Not silence: `nodes` still carries every
    #   node in scope with its own status, gap and reason, and that is the
    #   inventory. This list answers a narrower question, which paths carry
    #   influence, and about such a node it has nothing to say.
    ranked = [
        {"metric": n, "score": score[n], "via": via[n]}
        for n in nodes_in_scope
        if n != target and n in via
    ]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked

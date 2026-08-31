"""The engine's window→scalar vocabulary: how a metric's window becomes one
number, and how the payload says which arithmetic formed it.

Extracted from `rca.py` (roadmap C30 / grill M8) so that `slices.py` and
`simulate.py` stop importing underscore-"private" names across a module
boundary. `node_window_value` is the one place a metric's window becomes a
scalar — the kind rules (flow/stock mean vs. a rate's Σnum/Σden) cannot be
applied in one caller and forgotten in its neighbour — and
`rate_window_method`/`rate_window_method_reason` are the labels that keep a
payload from misdescribing its own arithmetic. These names are public because
three modules depend on them; they are the contract.

Nothing here resamples or summarizes — that is `engine/stats.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from breakdown.grains import ensure_grained, rate_window_value


class UndefinedOverWindow(ValueError):
    """A node has no value over one of the windows it was asked about.

    Its own subclass for the same reason `rca.NonFiniteAttribution` is one: the
    condition degrades to a per-node status inside `run_rca` and must not
    swallow the unrelated `ValueError`s the same calls raise. A `ValueError`
    still, so the API turns it into a 422 carrying the message when it is the
    target that has no value.
    """


def window_mask(data: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Rows of `data` whose date is within [start, end] (inclusive)."""
    return (data["date"] >= start) & (data["date"] <= end)


def window_values(
    data: pd.DataFrame, col: str, start: pd.Timestamp, end: pd.Timestamp
) -> np.ndarray:
    """Per-period values of `col` over rows whose date is within [start, end]
    (inclusive). Raises if the window is empty."""
    mask = window_mask(data, start, end)
    if not mask.any():
        raise ValueError(f"No data for '{col}' in window [{start.date()}, {end.date()}]")
    return data.loc[mask, col].values.astype(float)


def window_mean(data: pd.DataFrame, col: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Mean of `col` over rows whose date is within [start, end] (inclusive).

    `data["date"]` must already be datetime. Raises if the window is empty.

    **Flows and stocks only.** A rate's window value is not the mean of its
    per-period ratios — see `node_window_value`, which is the entry point every
    caller uses and which routes a rate to `grains.rate_window_value`. Calling
    this on a rate column is the defect `resample_up` already refuses in the
    time direction, and `tests/test_project_invariants.py` enumerates the
    package to keep this function from acquiring a caller that does.
    """
    return float(window_values(data, col, start, end).mean())


def node_window_value(
    data,
    node: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    grain: Optional[str] = None,
    *,
    frame: Optional[pd.DataFrame] = None,
) -> float:
    """The single number `node` reports for the window [start, end], by kind.

    The one place a metric's window becomes a scalar, so the kind rules cannot
    be applied in one caller and forgotten in its neighbour:

    - **flow / stock** — the mean per period, unchanged.
    - **rate** — `Σ(rate·denominator) / Σdenominator` over the window's defined
      periods (`grains.rate_window_value`), which is what a window's rate *is*.
      A period whose denominator is zero contributes to neither sum and drops
      out, so a window containing an undefined period still has a defined rate.
      A rate that declares no `denominator` falls back to the plain average of
      its defined periods, which is the pre-1.11 number and is disclosed at
      load rather than silently presented as the aggregate. Which of the two
      arithmetics ran — and, for the fallback, *why* — is `rate_window_method`,
      which every payload carrying one of these numbers reports beside it.

    Returns `nan` when the window holds no defined period; callers report that
    as a status rather than publishing it. Raises when the window holds no
    period at all — a different failure, and one the caller chose.
    """
    grained = ensure_grained(data)
    kind = grained.kind_of.get(node, "flow")
    at = grain or grained.grain_of.get(node, "day")
    source = frame if frame is not None else grained.series(node, at)
    values = window_values(source, node, start, end)
    if kind != "rate":
        return float(values.mean())
    return rate_window_value(values, _window_weights(grained, node, source, start, end, at))


def _window_weights(
    grained, node: str, source: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, at: str
) -> Optional[np.ndarray]:
    """The denominator's per-period values over this window, or None.

    Aligned **on the dates**, never on position or length. `source` may be a fit
    frame whose inner join dropped a period (a finer parent's partial coarse
    period), so two arrays of equal length can still be describing different
    weeks — a misalignment nobody could see. A denominator that cannot cover the
    window's periods weights nothing, and the disclosed fallback is better than
    a silent mis-weighting.

    One implementation, two callers: the value (`node_window_value`) and the
    label for how it was formed (`rate_window_method`). If those two ever
    disagreed the payload would describe an arithmetic it did not perform, which
    is the defect class this whole item is about.
    """
    weights = grained.weights_for(node, at)
    if weights is None:
        return None
    dates = pd.DatetimeIndex(source.loc[window_mask(source, start, end), "date"])
    aligned = weights.reindex(dates)
    return None if aligned.isna().any() else aligned.to_numpy(dtype=float)


def rate_window_method(
    data,
    node: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    grain: Optional[str] = None,
    *,
    frame: Optional[pd.DataFrame] = None,
) -> Optional[str]:
    """How `node_window_value` formed this window's number, for the payload.

    `None` for a flow or a stock — they have one aggregation and it is not in
    question. For a rate, one of:

    - `"components"` — `Σ(rate·den) / Σden` over the declared denominator. The
      real thing.
    - `"period_mean_none_exists"` — the node declares `no_denominator`: there is
      no pair of series whose sums make this quantity, so the mean of the
      defined periods is not a fallback from something better, it is the only
      number there is. The reason travels beside it.
    - `"period_mean_undeclared"` — nobody has said what this rate is a rate of.
      Same arithmetic as above, completely different meaning: the tree could
      compute the real aggregate once someone declares the denominator.
    - `"period_mean_weights_unavailable"` — a denominator *is* declared, but its
      series does not cover this window's periods, so this particular window
      fell back. Rare, and worth seeing rather than reading as `components`.

    The first three are the three states of `denominator` (roadmap 1.11); the
    fourth exists because labelling a window `components` while averaging its
    ratios would be a payload that misdescribes its own arithmetic.
    """
    grained = ensure_grained(data)
    if grained.kind_of.get(node, "flow") != "rate":
        return None
    at = grain or grained.grain_of.get(node, "day")
    if node not in grained.denominator_of:
        return (
            "period_mean_none_exists"
            if node in grained.no_denominator_of
            else "period_mean_undeclared"
        )
    source = frame if frame is not None else grained.series(node, at)
    if _window_weights(grained, node, source, start, end, at) is None:
        return "period_mean_weights_unavailable"
    return "components"


def rate_window_method_reason(data, node: str, method: Optional[str]) -> Optional[str]:
    """The sentence that goes beside `rate_window_method`, in prose.

    The status is what a consumer branches on; this is what a *person* reads,
    and for `period_mean_none_exists` it is the author's own words, carried from
    the tree's `no_denominator:` without editing. That is the point of writing
    the reason there rather than in a comment: the argument reaches whoever is
    looking at the number, which is where the question actually gets re-opened.
    """
    if method is None or method == "components":
        return None
    grained = ensure_grained(data)
    if method == "period_mean_none_exists":
        return grained.no_denominator_of.get(node)
    if method == "period_mean_weights_unavailable":
        return (
            f"'{node}' declares denominator '{grained.denominator_of.get(node)}', but its "
            "series does not cover every period of these windows, so this value is the "
            "mean of the per-period ratios rather than Σnumerator / Σdenominator."
        )
    return (
        f"'{node}' declares no `denominator` and no `no_denominator`, so this value is "
        "the mean of its defined per-period ratios rather than Σnumerator / "
        "Σdenominator — which is a different number whenever the denominators differ. "
        "Nobody has said which of the two this metric is; declaring either settles it."
    )


def window_info(snapped) -> Dict[str, Any]:
    """The whole periods a requested window snapped to, for the response."""
    return {
        "start": str(snapped.first_start.date()),
        "end": str(snapped.last_end.date()),
        "n_periods": snapped.n_periods,
    }

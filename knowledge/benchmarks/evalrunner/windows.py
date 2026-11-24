"""Which windows a null world is scored on (evaluation spec §5.1).

One grid of start dates per world, the same for every node and every arm, so
two runs are compared on identical windows. Starts are Mondays (a week-grain
node's periods start there), evenly spaced by a whole number of weeks, and the
first one leaves `MIN_HISTORY_DAYS` of data to fit on.

Windows at one horizon never overlap: a horizon longer than the spacing is
scored on every k-th start only. "Consecutive windows" (the lag-1 correlation
of §5.1) are neighbours on this grid, `spacing_days` apart; when a horizon
equals the spacing they are adjacent in time.
"""

from __future__ import annotations

import math

import pandas as pd

MIN_HISTORY_DAYS = 182  # as the prototype's null run: half a year before the first window
GRAIN_DAYS = {"day": 1, "week": 7}


def window_starts(
    data_start,
    data_end,
    *,
    horizons: tuple[int, ...],
    spacing_days: int | None = None,
    max_windows: int | None = None,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> tuple[list[pd.Timestamp], int]:
    """`(starts, spacing_days)`. Give the spacing, or `max_windows` to get the
    smallest whole-week spacing that fits that many windows or fewer.

    A start is kept when the shortest horizon ends inside the data. The result
    depends on the data's first and last day and nothing else.
    """
    if (spacing_days is None) == (max_windows is None):
        raise ValueError("give exactly one of spacing_days and max_windows")
    data_start, data_end = pd.Timestamp(data_start), pd.Timestamp(data_end)
    first = data_start + pd.Timedelta(days=min_history_days)
    first += pd.Timedelta(days=(7 - first.dayofweek) % 7)  # the next Monday
    last = data_end - pd.Timedelta(days=min(horizons) - 1)
    if first > last:
        return [], spacing_days or 7
    if spacing_days is None:
        span_weeks = (last - first).days // 7 + 1
        spacing_days = 7 * max(1, math.ceil(span_weeks / max_windows))
    if spacing_days % 7 or spacing_days <= 0:
        raise ValueError(f"spacing_days must be a positive multiple of 7, got {spacing_days}")
    return list(pd.date_range(first, last, freq=f"{spacing_days}D")), spacing_days


def horizon_is_scored(index: int, horizon_days: int, spacing_days: int) -> bool:
    """Whether the `index`-th start is scored at this horizon: every start when
    the horizon fits inside the spacing, else every k-th so windows stay apart."""
    return index % max(1, math.ceil(horizon_days / spacing_days)) == 0


def horizons_for(
    index: int, start, grain: str, horizons: tuple[int, ...], spacing_days: int, data_end
) -> list[int]:
    """The horizons scored for a node of `grain` at one start. A horizon
    shorter than the node's own period is not a window of that node."""
    if grain not in GRAIN_DAYS:
        return []
    start, data_end = pd.Timestamp(start), pd.Timestamp(data_end)
    return [
        h
        for h in horizons
        if h >= GRAIN_DAYS[grain]
        and h % GRAIN_DAYS[grain] == 0
        and horizon_is_scored(index, h, spacing_days)
        and start + pd.Timedelta(days=h - 1) <= data_end
    ]

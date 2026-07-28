"""Grain arithmetic and the per-grain data container.

A metric's **grain** is the natural period of its series (`day`, `week`,
`month`); its **kind** determines how the series aggregates over time
(`flow` sums, `stock` takes the last value, `rate` cannot be aggregated —
a coarse rate must be recomputed from its components). Everything
grain-related lives here: period flooring/snapping, grain-step math,
kind-aware resample-up, and `GrainedData`, the per-grain replacement for the
single tree-wide daily frame.

Period labels are **period-start timestamps** throughout: day = midnight,
week = Monday (ISO), month = the 1st. Resampling only ever goes upward
(finer → coarser), and only between grains that nest: days tile weeks and
months, but weeks straddle month boundaries, so week → month is rejected
rather than approximated.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Union

import numpy as np
import pandas as pd

GRAINS = ("day", "week", "month")
_ORDER = {"day": 0, "week": 1, "month": 2}

# Circular moving-block bootstrap block length per grain: serial dependence
# spans about a week of daily rows, about a month of weekly rows, and about
# two months of monthly rows.
BOOT_BLOCK = {"day": 7, "week": 4, "month": 2}

_FREQ = {"day": "D", "week": "W-MON", "month": "MS"}


def _check_grain(grain: str) -> None:
    if grain not in _ORDER:
        raise ValueError(f"Unknown grain '{grain}'; must be one of {list(GRAINS)}.")


def is_finer(a: str, b: str) -> bool:
    """True if grain `a` is strictly finer than grain `b`."""
    _check_grain(a)
    _check_grain(b)
    return _ORDER[a] < _ORDER[b]


def coarsest(grains: Iterable[str]) -> str:
    grains = list(grains)
    if not grains:
        raise ValueError("coarsest() of an empty grain list")
    for g in grains:
        _check_grain(g)
    return max(grains, key=lambda g: _ORDER[g])


def nests_in(fine: str, coarse: str) -> bool:
    """True if every `fine` period lies entirely inside one `coarse` period.

    Days tile weeks and months; weeks straddle month boundaries, so
    week → month does NOT nest and cannot be resampled exactly.
    """
    _check_grain(fine)
    _check_grain(coarse)
    if fine == coarse:
        return True
    if fine == "week" and coarse == "month":
        return False
    return _ORDER[fine] < _ORDER[coarse]


def fit_grain(dag, node: str) -> str:
    """The grain a node is fitted and attributed at: its own declared grain.

    Parse-time validation guarantees no parent is coarser than its child, so
    the coarsest grain among {node, parents} is always the node's own.
    """
    defn = dag.nodes[node]["definition"]
    return getattr(defn, "grain", "day")


def floor_period(
    ts: Union[pd.Timestamp, pd.DatetimeIndex, pd.Series], grain: str
) -> Union[pd.Timestamp, pd.DatetimeIndex, pd.Series]:
    """Floor timestamps to their period start (midnight / Monday / the 1st).

    Accepts a scalar Timestamp, a DatetimeIndex, or a datetime Series and
    returns the same shape.
    """
    _check_grain(grain)
    if isinstance(ts, pd.Series):
        return pd.Series(
            floor_period(pd.DatetimeIndex(ts), grain), index=ts.index, name=ts.name
        )
    scalar = not isinstance(ts, (pd.DatetimeIndex, np.ndarray, list, tuple))
    idx = pd.DatetimeIndex([ts] if scalar else ts).normalize()
    if grain == "day":
        out = idx
    elif grain == "week":
        out = idx - pd.to_timedelta(idx.dayofweek, unit="D")
    else:  # month
        out = idx.to_period("M").to_timestamp()
    return out[0] if scalar else out


def shift_periods(ts: pd.Timestamp, n: int, grain: str) -> pd.Timestamp:
    """Shift a period-start timestamp by `n` whole periods (n may be negative).

    Month shifts land on the 1st of the target month, so period-start labels
    stay on the spine across month/year boundaries.
    """
    _check_grain(grain)
    ts = pd.Timestamp(ts)
    if grain == "day":
        return ts + pd.DateOffset(days=n)
    if grain == "week":
        return ts + pd.DateOffset(weeks=n)
    return ts + pd.DateOffset(months=n)


def next_start(ts: pd.Timestamp, grain: str) -> pd.Timestamp:
    """The start of the period after the one beginning at `ts`."""
    return shift_periods(ts, 1, grain)


def steps_between(
    later: Union[pd.Timestamp, pd.DatetimeIndex, Iterable], earlier: pd.Timestamp, grain: str
) -> Union[int, np.ndarray]:
    """Whole-period steps from `earlier` to `later` (both period starts at
    `grain`). Returns an int for a scalar `later`, an int array otherwise.

    For day grain this is exactly the day difference — the invariant that
    keeps existing daily behavior bit-for-bit identical.
    """
    _check_grain(grain)
    scalar = not isinstance(later, (pd.DatetimeIndex, np.ndarray, list, tuple, pd.Series))
    idx = pd.DatetimeIndex([later] if scalar else later)
    e = pd.Timestamp(earlier)
    if grain == "day":
        out = (idx - e).days
    elif grain == "week":
        out = (idx - e).days // 7
    else:  # month
        out = (idx.year - e.year) * 12 + (idx.month - e.month)
    out = np.asarray(out, dtype=int)
    return int(out[0]) if scalar else out


@dataclass(frozen=True)
class SnappedWindow:
    """The whole `grain` periods fully inside a requested [start, end] window."""

    first_start: pd.Timestamp  # start of the first whole period
    last_start: pd.Timestamp   # start of the last whole period
    last_end: pd.Timestamp     # inclusive end date of the last whole period
    n_periods: int


def snap_window(start, end, grain: str) -> Optional[SnappedWindow]:
    """Snap a day-resolution [start, end] window (inclusive) to the whole
    `grain` periods fully inside it, or None if it contains no whole period.

    For day grain this is the identity on normalized dates.
    """
    _check_grain(grain)
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()

    first = floor_period(start, grain)
    if first < start:  # partial leading period — advance to the next start
        first = next_start(first, grain)
    last = floor_period(end, grain)
    if next_start(last, grain) - pd.Timedelta(days=1) > end:  # partial trailing
        last = shift_periods(last, -1, grain)
    if last < first:
        return None
    return SnappedWindow(
        first_start=first,
        last_start=last,
        last_end=next_start(last, grain) - pd.Timedelta(days=1),
        n_periods=steps_between(last, first, grain) + 1,
    )


def period_spine(start, end, grain: str) -> pd.DatetimeIndex:
    """Period-start spine of all whole `grain` periods fully inside
    [start, end]; empty if there are none."""
    snapped = snap_window(start, end, grain)
    if snapped is None:
        return pd.DatetimeIndex([], dtype="datetime64[ns]")
    return pd.date_range(snapped.first_start, snapped.last_start, freq=_FREQ[grain])


def resample_up(
    series: pd.Series,
    from_grain: str,
    to_grain: str,
    kind: str,
    label: str = "series",
    drop_partial: bool = True,
) -> pd.Series:
    """Aggregate a period-start-indexed series from `from_grain` up to
    `to_grain` by its kind: flow → sum, stock → last. Rates never resample —
    the only correct coarse rate is recomputed from its components.

    With `drop_partial` (default), coarse periods not fully covered by fine
    periods are dropped — a partial-week sum is a lie, not an estimate.
    """
    _check_grain(from_grain)
    _check_grain(to_grain)
    if from_grain == to_grain:
        return series.copy()
    if not is_finer(from_grain, to_grain):
        raise ValueError(
            f"Cannot resample {label} downward from '{from_grain}' to '{to_grain}': "
            "disaggregation is undefined."
        )
    if kind == "rate":
        raise ValueError(
            f"Cannot aggregate rate metric {label} from '{from_grain}' to "
            f"'{to_grain}': averaging per-{from_grain} ratios is wrong. Declare "
            f"the rate at '{to_grain}', recomputed from its components."
        )
    if not nests_in(from_grain, to_grain):
        raise ValueError(
            f"Cannot resample {label} from '{from_grain}' to '{to_grain}': "
            f"'{from_grain}' periods straddle '{to_grain}' boundaries. Declare it "
            f"at '{to_grain}' instead."
        )

    coarse = floor_period(pd.DatetimeIndex(series.index), to_grain)
    grouped = series.groupby(coarse)
    agg = grouped.sum() if kind == "flow" else grouped.last()
    if drop_partial:
        expected = np.array(
            [steps_between(next_start(g, to_grain), g, from_grain) for g in agg.index]
        )
        agg = agg[grouped.size().to_numpy() >= expected]
    agg.index.name = series.index.name
    return agg


@dataclass
class GrainedData:
    """Per-grain metric frames replacing the single tree-wide daily frame.

    `frames[grain]` holds the *native* metrics of that grain, inner-joined on
    their period-start `date` column — metrics only ever join against series
    at the same grain, so a monthly metric no longer erases 29 of every 30
    days from the whole tree. Coarser views of flow/stock metrics are
    resampled on demand (`series` / `fit_frame`).
    """

    frames: Dict[str, pd.DataFrame]
    grain_of: Dict[str, str]
    kind_of: Dict[str, str]

    def frame(self, grain: str) -> pd.DataFrame:
        _check_grain(grain)
        if grain not in self.frames:
            raise ValueError(f"No metrics at grain '{grain}' (have {list(self.frames)}).")
        return self.frames[grain]

    def series(self, metric: str, grain: Optional[str] = None) -> pd.DataFrame:
        """`["date", metric]` frame at the metric's native grain, or resampled
        up to `grain` by the metric's kind."""
        native = self.grain_of[metric]
        out = self.frames[native][["date", metric]]
        if grain is None or grain == native:
            return out.copy()
        ser = pd.Series(
            out[metric].to_numpy(), index=pd.DatetimeIndex(out["date"]), name=metric
        )
        up = resample_up(ser, native, grain, self.kind_of[metric], label=f"'{metric}'")
        return pd.DataFrame({"date": up.index, metric: up.to_numpy()})

    def fit_frame(self, target: str, parents: Iterable[str], grain: str) -> pd.DataFrame:
        """`["date", target, *parents]` aligned at `grain`: the target native,
        finer flow/stock parents resampled up, inner-joined on whole periods."""
        out = self.series(target, grain)
        for p in parents:
            out = out.merge(self.series(p, grain), on="date", how="inner")
        if out.empty:
            raise RuntimeError(
                f"No overlapping whole '{grain}' periods across '{target}' and its "
                f"parents {list(parents)}."
            )
        return out.sort_values("date").reset_index(drop=True)

    @property
    def date_start(self) -> pd.Timestamp:
        return min(f["date"].min() for f in self.frames.values())

    @property
    def date_end(self) -> pd.Timestamp:
        return max(f["date"].max() for f in self.frames.values())

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "GrainedData":
        """Wrap a plain wide daily frame (the pre-grain data shape): every
        column is a day-grain flow metric. This is the back-compat shim that
        keeps existing call sites and tests bit-for-bit identical."""
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        metrics = [c for c in df.columns if c != "date"]
        return cls(
            frames={"day": df},
            grain_of={m: "day" for m in metrics},
            kind_of={m: "flow" for m in metrics},
        )


def build_grained(
    per_metric: Dict[str, pd.DataFrame],
    grain_of: Dict[str, str],
    kind_of: Dict[str, str],
) -> GrainedData:
    """Assemble per-grain frames from per-metric `["date", name]` frames,
    inner-joining within each grain only."""
    frames: Dict[str, pd.DataFrame] = {}
    for grain in GRAINS:
        names = [m for m, g in grain_of.items() if g == grain]
        if not names:
            continue
        cols = []
        for m in names:
            df = per_metric[m].copy()
            df["date"] = pd.to_datetime(df["date"])
            cols.append(df.set_index("date")[[m]])
        joined = pd.concat(cols, axis=1, join="inner")
        if joined.empty:
            raise RuntimeError(
                f"No overlapping periods across metrics at grain '{grain}': "
                f"{names}. Check each metric's date coverage."
            )
        frames[grain] = (
            joined.rename_axis("date").reset_index().sort_values("date").reset_index(drop=True)
        )
    return GrainedData(frames=frames, grain_of=dict(grain_of), kind_of=dict(kind_of))


def ensure_grained(data: Union[pd.DataFrame, GrainedData]) -> GrainedData:
    """Normalize an engine entry point's `data` argument: pass `GrainedData`
    through, wrap a plain wide daily frame as all-day flow metrics."""
    if isinstance(data, GrainedData):
        return data
    return GrainedData.from_frame(data)

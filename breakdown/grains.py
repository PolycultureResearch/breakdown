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

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

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


def to_date(value, label: str) -> pd.Timestamp:
    """`pd.Timestamp(value).normalize()`, refusing what pandas would swallow.

    `pd.Timestamp("")` and `pd.Timestamp(None)` are `NaT`, not an error — so a
    caller that passed an empty date got a value that satisfied every `is None`
    guard and every `str` annotation, and then failed deep inside the engine
    with `'NaTType' object has no attribute 'normalize'`: an unhandled 500 for
    a client that submitted a cleared date field. `pd.Timestamp("banana")`
    raises, so the same input in a different spelling behaved correctly.

    The API rejects these at the boundary now, but the engine is a library with
    its own callers (the MCP tools, notebooks), and a `ValueError` naming the
    parameter is the answer they should get too — the same posture the provider
    boundary takes: refuse, do not coerce.
    """
    ts = pd.Timestamp(value)
    if ts is pd.NaT or pd.isna(ts):
        raise ValueError(f"{label} must be a valid date, got {value!r}")
    return ts.normalize()


def floor_period(
    ts: Union[pd.Timestamp, pd.DatetimeIndex, pd.Series], grain: str
) -> Union[pd.Timestamp, pd.DatetimeIndex, pd.Series]:
    """Floor timestamps to their period start (midnight / Monday / the 1st).

    Accepts a scalar Timestamp, a DatetimeIndex, or a datetime Series and
    returns the same shape.
    """
    _check_grain(grain)
    if isinstance(ts, pd.Series):
        return pd.Series(floor_period(pd.DatetimeIndex(ts), grain), index=ts.index, name=ts.name)
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
    last_start: pd.Timestamp  # start of the last whole period
    last_end: pd.Timestamp  # inclusive end date of the last whole period
    n_periods: int


def snap_window(start, end, grain: str) -> Optional[SnappedWindow]:
    """Snap a day-resolution [start, end] window (inclusive) to the whole
    `grain` periods fully inside it, or None if it contains no whole period.

    For day grain this is the identity on normalized dates.
    """
    _check_grain(grain)
    start = to_date(start, "window start")
    end = to_date(end, "window end")

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


# Default reference-window policy: the "matched adjacent block". The reference
# is only the comparison baseline (the fit already uses all loaded history
# before the analysis window), so it should be adjacent (no trend gap),
# composition-matched (whole weeks when seasonality is in scope), and long
# enough for a stable mean — but not so long it mixes regimes.
MIN_REFERENCE_DAYS = 28
REFERENCE_MULTIPLE = 4


def default_reference_window(
    analysis_start,
    analysis_end,
    data_start,
    *,
    week_align: bool = False,
    coarsest_grain: str = "day",
    earliest_start: Optional[Union[str, pd.Timestamp]] = None,
) -> tuple:
    """Matched adjacent reference block for an analysis window.

    The block immediately preceding the analysis window, sized
    ``REFERENCE_MULTIPLE`` × the analysis length with a floor of
    ``MIN_REFERENCE_DAYS``, rounded up to a whole-week length when
    `week_align` (per-node Monday alignment stays `snap_window`'s job),
    and clamped to the loaded data range. When `coarsest_grain` is coarser
    than a day, the block is extended backwards if needed so it contains at
    least one whole period at that grain (data permitting).

    `earliest_start` is a hard floor later than `data_start`, for callers who
    know the loaded data is not all readable — a lagged node reads its parents
    from whole periods *before* the window it is evaluated over, so the first
    few days of history can never open a reference window (roadmap M1). The
    block is shortened to respect it, and the refusal below says so rather
    than reporting a date the caller never typed.

    Returns ``(reference_start, reference_end)`` as ISO date strings.
    Raises ValueError when no reference day exists before the analysis
    window.
    """
    _check_grain(coarsest_grain)
    try:
        an_s = to_date(analysis_start, "analysis_start")
        an_e = to_date(analysis_end, "analysis_end")
    except (ValueError, TypeError):
        raise ValueError("analysis_start/analysis_end is not a valid date")
    if an_s > an_e:
        raise ValueError("analysis_start must be on or before analysis_end")
    d0 = pd.Timestamp(data_start).normalize()
    floor = d0 if earliest_start is None else max(d0, pd.Timestamp(earliest_start).normalize())

    ref_end = an_s - pd.Timedelta(days=1)
    if ref_end < floor:
        if floor > d0:
            raise ValueError(
                f"There is not enough history before {an_s.date()} for this "
                f"target's lags: the loaded data starts at {d0.date()}, but the "
                f"earliest reference period every node in scope can read is "
                f"{floor.date()} — a lagged parent is read from whole periods "
                f"before the window it explains. Load more history (an earlier "
                f"--start-date) or start the analysis window later."
            )
        raise ValueError(
            f"The analysis window starts at the beginning of the loaded data "
            f"({d0.date()}), so no reference block exists before it. Pass "
            f"reference dates explicitly, or load more history (an earlier "
            f"--start-date)."
        )

    an_len = (an_e - an_s).days + 1
    length = max(REFERENCE_MULTIPLE * an_len, MIN_REFERENCE_DAYS)
    if week_align:
        length = -(-length // 7) * 7  # round up to a whole-week length
    ref_start = ref_end - pd.Timedelta(days=length - 1)

    if ref_start < floor:  # readable data is the hard bound
        ref_start = floor
        if week_align:
            avail = (ref_end - ref_start).days + 1
            if avail >= 7:
                # Trim down to whole weeks ending at ref_end: a balanced
                # weekday mix beats raw length. A <7-day stub is kept —
                # advisories, not this function, own that warning.
                ref_start = ref_end - pd.Timedelta(days=7 * (avail // 7) - 1)

    if coarsest_grain != "day" and snap_window(ref_start, ref_end, coarsest_grain) is None:
        # Reach back to cover the last whole coarse period ending on/before
        # ref_end, so coarse-grain nodes get at least one period to average —
        # but only when the data can actually cover it. Otherwise the block
        # stands and the node reports the existing window_shorter_than_grain
        # status downstream.
        last = floor_period(ref_end, coarsest_grain)
        if next_start(last, coarsest_grain) - pd.Timedelta(days=1) > ref_end:
            last = shift_periods(last, -1, coarsest_grain)
        if last >= floor:
            ref_start = min(ref_start, last)

    return (str(ref_start.date()), str(ref_end.date()))


def resample_up(
    series: pd.Series,
    from_grain: str,
    to_grain: str,
    kind: str,
    label: str = "series",
) -> pd.Series:
    """Aggregate a period-start-indexed series from `from_grain` up to
    `to_grain` by its kind: flow → sum, stock → last. Rates never resample —
    the only correct coarse rate is recomputed from its components.

    Coarse periods not fully covered by fine periods are dropped — a
    partial-week sum is a lie, not an estimate. This used to be a
    `drop_partial: bool = True` parameter that no caller ever varied (grill
    L2): a knob nobody turns is a policy pretending to be a choice, and every
    reader had to reason about the False branch to learn there wasn't one.
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
    expected = np.array([steps_between(next_start(g, to_grain), g, from_grain) for g in agg.index])
    agg = agg[grouped.size().to_numpy() >= expected]
    agg.index.name = series.index.name
    return agg


def rate_window_value(values: np.ndarray, weights: Optional[np.ndarray]) -> float:
    """The single number a window of a `kind: rate` series reports.

    **The whole of roadmap 1.11c, in four lines.** A window's rate is
    `Σnumerator / Σdenominator`, and `numerator_i = rate_i · denominator_i`, so
    it is the *denominator-weighted* mean of the per-period rates:

        Σ(rate_i · den_i) / Σ den_i

    Two things follow, and both are the point:

    - **A period whose denominator is zero drops out of both sums**, so it
      contributes nothing rather than poisoning everything. That is exactly the
      period whose own rate is undefined (`0/0` — nobody churned that week), so
      the window rate stays well-defined precisely where the per-period rate is
      not. It is not a special case bolted on; it is what the arithmetic does.
    - **An average of per-period ratios is a different number**, and a wrong
      one whenever the denominators differ — which is why `resample_up` refuses
      to compute one and why nothing in this engine may.

    Without weights there is no honest aggregate to compute, only the plain
    average of the ratios. That fallback is the pre-1.11 behaviour and stays,
    because some rates genuinely have no denominator — a *median* page-load
    time is not `Σnum / Σden` for any pair of series, and a mean duration whose
    cohort the tree does not carry has no weight to offer. It is disclosed at
    load (`Parser._validate_denominators` names every node reaching this path)
    and in the payload (`rate_window_method`, which says *why* there were no
    weights) rather than passed off as the real thing. It skips undefined
    periods too: averaging over the periods that exist is at least not
    averaging over invented ones.

    Returns `nan` when no period survives — an undefined window, which callers
    report as such rather than publishing.
    """
    values = np.asarray(values, dtype=float)
    if weights is None:
        defined = values[np.isfinite(values)]
        return float(defined.mean()) if defined.size else float("nan")
    weights = np.asarray(weights, dtype=float)
    usable = np.isfinite(values) & np.isfinite(weights)
    total = float(weights[usable].sum())
    if not usable.any() or total == 0.0:
        return float("nan")
    return float((values[usable] * weights[usable]).sum() / total)


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
    # Per-metric freshness: the start label of the last period the provider
    # actually returned (before any within-grain join). Providers trim
    # trailing unloaded periods, so this is the honest data edge per metric.
    last_observed: Dict[str, pd.Timestamp] = field(default_factory=dict)
    # `kind: rate` metric -> the metric whose per-period values weight it over
    # a window (roadmap 1.11b). Carried on the data rather than looked up from
    # the DAG at each call site so that every consumer of a window aggregate —
    # RCA, what-if, the node cards — is weighting by the same declared fact.
    denominator_of: Dict[str, str] = field(default_factory=dict)
    # `kind: rate` metric -> the author's stated reason it has **no**
    # denominator (roadmap 1.11). Carried beside `denominator_of` because the
    # absence of a key there has two meanings — "nobody has said" and "asked and
    # answered" — and every consumer of a fallback aggregate has to be able to
    # tell them apart. A name here is a finding, not a configuration gap.
    no_denominator_of: Dict[str, str] = field(default_factory=dict)

    def weights_for(self, metric: str, grain: Optional[str] = None) -> Optional[pd.Series]:
        """The per-period weights for a rate, indexed by period start, or None.

        None means "this rate declares no denominator", which is a different
        statement from "its weights are all equal" — the caller discloses it
        rather than substituting one for the other.
        """
        den = self.denominator_of.get(metric)
        if den is None or den not in self.grain_of:
            return None
        at = grain or self.grain_of[metric]
        frame = self.series(den, at)
        return pd.Series(
            frame[den].to_numpy(dtype=float), index=pd.DatetimeIndex(frame["date"]), name=den
        )

    def data_through(self, metric: str) -> Optional[pd.Timestamp]:
        """Inclusive last covered date for `metric`: the END of its last
        observed period (the period-start label for day grain, the Sunday for
        a week, the month's last day for a month). None if unknown."""
        if metric not in self.last_observed:
            return None
        return next_start(self.last_observed[metric], self.grain_of[metric]) - pd.Timedelta(days=1)

    def frame(self, grain: str) -> pd.DataFrame:
        _check_grain(grain)
        if grain not in self.frames:
            raise ValueError(f"No metrics at grain '{grain}' (have {list(self.frames)}).")
        return self.frames[grain]

    def series(self, metric: str, grain: Optional[str] = None) -> pd.DataFrame:
        """`["date", metric]` frame at the metric's native grain, or resampled
        up to `grain` by the metric's kind."""
        if metric not in self.grain_of:
            raise ValueError(f"Columns missing from data: ['{metric}']")
        native = self.grain_of[metric]
        out = self.frames[native][["date", metric]]
        if grain is None or grain == native:
            return out.copy()
        ser = pd.Series(out[metric].to_numpy(), index=pd.DatetimeIndex(out["date"]), name=metric)
        up = resample_up(ser, native, grain, self.kind_of[metric], label=f"'{metric}'")
        return pd.DataFrame({"date": up.index, metric: up.to_numpy()})

    def fit_frame(self, target: str, parents: Iterable[str], grain: str) -> pd.DataFrame:
        """`["date", target, *parents]` aligned at `grain`: the target native,
        finer flow/stock parents resampled up, inner-joined on whole periods."""
        parents = list(parents)
        missing = [c for c in [target, *parents] if c not in self.grain_of]
        if missing:
            raise ValueError(f"Columns missing from data: {missing}")
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
        last = df["date"].max() if len(df) else None
        return cls(
            frames={"day": df},
            grain_of={m: "day" for m in metrics},
            kind_of={m: "flow" for m in metrics},
            last_observed={m: last for m in metrics} if last is not None else {},
        )


def _check_contiguous(frame: pd.DataFrame, grain: str, names: list, widest: int) -> None:
    """A grain frame must be a gap-free run of periods.

    Everything downstream indexes by position: the model's `t = arange(len(y))`
    dates the rows, lags shift by *rows*, and the block bootstrap resamples
    contiguous runs. A hole makes all three quietly wrong — `t` compresses the
    calendar, so a lag of 7 rows stops meaning 7 days and the seasonal design
    is fitted against the wrong dates. Nothing raises; the numbers are just
    off, which is the worst failure mode this engine has.

    The inner join is the usual culprit — a date present for only some metrics
    is dropped from the shared frame — so a drop count is logged even when the
    survivors happen to stay contiguous.

    **Contiguity is a property of the dates, not of the values** (roadmap
    1.11c). An undefined rate period is a row carrying `NaN`, so it is not a
    hole and nothing here fires: `t`, the lag shifts and the bootstrap blocks
    all stay aligned to the calendar. That is the stated policy and the reason
    undefined periods are represented in place rather than dropped — the fit
    refuses such a window (`_validate_columns`) and window aggregates exclude
    the period (`rate_window_value`), which costs one period's information;
    deleting the row would instead move every date after it.
    """
    dates = pd.DatetimeIndex(frame["date"])
    dropped = widest - len(frame)
    if dropped > 0:
        logger.warning(
            "Inner join at grain '%s' dropped %d period(s) present in only some "
            "of %s; the shared frame runs [%s, %s].",
            grain,
            dropped,
            names,
            dates.min().date(),
            dates.max().date(),
        )

    expected = pd.date_range(dates.min(), dates.max(), freq=_FREQ[grain])
    missing = expected.difference(dates)
    if len(missing) == 0:
        return

    shown = ", ".join(str(d.date()) for d in missing[:10])
    more = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
    raise RuntimeError(
        f"The '{grain}' grain frame has {len(missing)} missing period(s) between "
        f"{dates.min().date()} and {dates.max().date()}: {shown}{more}. "
        f"Metrics at this grain: {names}. Positional indexing (model time, lags, "
        f"bootstrap blocks) assumes a gap-free spine, so a hole silently shifts "
        f"every downstream date. Backfill the gap, narrow the date range, or drop "
        f"the metric that is missing those periods."
    )


def build_grained(
    per_metric: Dict[str, pd.DataFrame],
    grain_of: Dict[str, str],
    kind_of: Dict[str, str],
    denominator_of: Optional[Dict[str, str]] = None,
    no_denominator_of: Optional[Dict[str, str]] = None,
) -> GrainedData:
    """Assemble per-grain frames from per-metric `["date", name]` frames,
    inner-joining within each grain only. Each metric's `last_observed` is
    captured from its own frame BEFORE the join, so freshness survives even
    when a less-fresh sibling trims the shared grain frame.

    **Undefined values keep their row.** A rate with no value for a period
    (roadmap 1.11) is `NaN` *in* the frame, never a missing date — which is
    what keeps `_check_contiguous`'s guarantee intact: positional indexing
    (model time, lags, bootstrap blocks) assumes a gap-free spine, and dropping
    the row to "clean up" the NaN would shift every downstream date. An
    undefined value costs one period's information; a missing period silently
    relabels the calendar."""
    last_observed = {m: pd.to_datetime(df["date"]).max() for m, df in per_metric.items() if len(df)}
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
        frame = joined.rename_axis("date").reset_index().sort_values("date").reset_index(drop=True)
        _check_contiguous(frame, grain, names, max(len(per_metric[m]) for m in names))
        frames[grain] = frame
    return GrainedData(
        frames=frames,
        grain_of=dict(grain_of),
        kind_of=dict(kind_of),
        last_observed=last_observed,
        denominator_of=dict(denominator_of or {}),
        no_denominator_of=dict(no_denominator_of or {}),
    )


def ensure_grained(data: Union[pd.DataFrame, GrainedData]) -> GrainedData:
    """Normalize an engine entry point's `data` argument: pass `GrainedData`
    through, wrap a plain wide daily frame as all-day flow metrics."""
    if isinstance(data, GrainedData):
        return data
    return GrainedData.from_frame(data)

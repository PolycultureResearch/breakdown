"""Grain arithmetic, kind-aware resampling, and the GrainedData container."""

import logging

import numpy as np
import pandas as pd
import pytest

from breakdown.grains import (
    build_grained,
    coarsest,
    ensure_grained,
    floor_period,
    is_finer,
    nests_in,
    next_start,
    period_spine,
    resample_up,
    shift_periods,
    snap_window,
    steps_between,
)

# --- ordering & nesting ---

def test_grain_order():
    assert is_finer("day", "week") and is_finer("day", "month") and is_finer("week", "month")
    assert not is_finer("month", "day") and not is_finer("day", "day")
    assert coarsest(["day", "month", "week"]) == "month"


def test_nesting():
    assert nests_in("day", "week") and nests_in("day", "month")
    assert not nests_in("week", "month")  # weeks straddle month boundaries
    assert nests_in("week", "week")


def test_unknown_grain_raises():
    with pytest.raises(ValueError, match="Unknown grain"):
        is_finer("hour", "day")


# --- period math ---

def test_floor_period():
    wed = pd.Timestamp("2024-01-31")  # a Wednesday
    assert floor_period(wed, "day") == wed
    assert floor_period(wed, "week") == pd.Timestamp("2024-01-29")  # Monday
    assert floor_period(wed, "month") == pd.Timestamp("2024-01-01")
    idx = pd.DatetimeIndex(["2024-01-31", "2024-02-01"])
    assert list(floor_period(idx, "month")) == [
        pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-01"),
    ]


def test_shift_and_next_cross_boundaries():
    assert next_start(pd.Timestamp("2024-01-01"), "month") == pd.Timestamp("2024-02-01")
    # Leap February: whole-month steps land on the 1st regardless of length.
    assert shift_periods(pd.Timestamp("2024-02-01"), 1, "month") == pd.Timestamp("2024-03-01")
    assert shift_periods(pd.Timestamp("2024-01-01"), -1, "month") == pd.Timestamp("2023-12-01")
    assert shift_periods(pd.Timestamp("2024-12-30"), 1, "week") == pd.Timestamp("2025-01-06")


def test_steps_between():
    assert steps_between(pd.Timestamp("2024-01-11"), pd.Timestamp("2024-01-01"), "day") == 10
    assert steps_between(pd.Timestamp("2024-01-29"), pd.Timestamp("2024-01-01"), "week") == 4
    assert steps_between(pd.Timestamp("2025-02-01"), pd.Timestamp("2024-11-01"), "month") == 3
    out = steps_between(pd.DatetimeIndex(["2024-01-08", "2024-01-15"]),
                        pd.Timestamp("2024-01-01"), "week")
    np.testing.assert_array_equal(out, [1, 2])


# --- window snapping ---

def test_snap_window_day_is_identity():
    s = snap_window("2024-01-03", "2024-01-10", "day")
    assert s.first_start == pd.Timestamp("2024-01-03")
    assert s.last_start == pd.Timestamp("2024-01-10")
    assert s.last_end == pd.Timestamp("2024-01-10")
    assert s.n_periods == 8


def test_snap_window_trims_partial_weeks():
    # Wed 2024-01-03 .. Tue 2024-01-23: whole weeks are Jan 8 and Jan 15.
    s = snap_window("2024-01-03", "2024-01-23", "week")
    assert s.first_start == pd.Timestamp("2024-01-08")
    assert s.last_start == pd.Timestamp("2024-01-15")
    assert s.last_end == pd.Timestamp("2024-01-21")
    assert s.n_periods == 2


def test_snap_window_exact_fit_unchanged():
    s = snap_window("2024-01-01", "2024-02-29", "month")  # Jan 1 .. leap Feb 29
    assert s.first_start == pd.Timestamp("2024-01-01")
    assert s.last_start == pd.Timestamp("2024-02-01")
    assert s.n_periods == 2


def test_snap_window_too_short_returns_none():
    assert snap_window("2024-01-02", "2024-01-25", "month") is None
    assert snap_window("2024-01-02", "2024-01-07", "week") is None


def test_period_spine():
    spine = period_spine("2024-01-03", "2024-01-23", "week")
    assert list(spine) == [pd.Timestamp("2024-01-08"), pd.Timestamp("2024-01-15")]
    assert len(period_spine("2024-01-01", "2024-01-05", "month")) == 0


# --- resample_up ---

def _daily_series(start, n, values=None):
    idx = pd.date_range(start, periods=n, freq="D")
    vals = np.arange(n, dtype=float) if values is None else values
    return pd.Series(vals, index=idx)


def test_resample_up_flow_sums_whole_weeks():
    # Mon 2024-01-01 .. Sun 2024-01-14: two whole weeks of ones.
    s = _daily_series("2024-01-01", 14, np.ones(14))
    up = resample_up(s, "day", "week", "flow")
    assert list(up.index) == [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-08")]
    np.testing.assert_allclose(up.to_numpy(), [7.0, 7.0])


def test_resample_up_drops_partial_periods():
    # 16 days starting Monday: the trailing 2-day partial week is dropped.
    s = _daily_series("2024-01-01", 16, np.ones(16))
    up = resample_up(s, "day", "week", "flow")
    assert len(up) == 2
    # And a partial leading month: Jan 15 .. Mar 3 → only February survives.
    s2 = _daily_series("2024-01-15", 49, np.ones(49))
    up2 = resample_up(s2, "day", "month", "flow")
    assert list(up2.index) == [pd.Timestamp("2024-02-01")]
    assert up2.iloc[0] == 29.0  # leap February


def test_resample_up_stock_takes_last():
    s = _daily_series("2024-01-01", 14)  # 0..13
    up = resample_up(s, "day", "week", "stock")
    np.testing.assert_allclose(up.to_numpy(), [6.0, 13.0])


def test_resample_up_rate_raises():
    s = _daily_series("2024-01-01", 14)
    with pytest.raises(ValueError, match="averaging per-day ratios is wrong"):
        resample_up(s, "day", "week", "rate", label="'arpu'")


def test_resample_up_rejects_downward_and_non_nesting():
    s = _daily_series("2024-01-01", 14)
    with pytest.raises(ValueError, match="disaggregation is undefined"):
        resample_up(s, "month", "day", "flow")
    weekly = pd.Series(np.ones(8), index=pd.date_range("2024-01-01", periods=8, freq="W-MON"))
    with pytest.raises(ValueError, match="straddle"):
        resample_up(weekly, "week", "month", "flow")


# --- GrainedData ---

def test_from_frame_is_all_day_flow():
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=10),
        "a": np.arange(10.0),
        "b": np.ones(10),
    })
    gd = ensure_grained(df)
    assert gd.grain_of == {"a": "day", "b": "day"}
    assert gd.kind_of == {"a": "flow", "b": "flow"}
    pd.testing.assert_frame_equal(gd.frame("day")[["date", "a", "b"]], df)
    assert ensure_grained(gd) is gd


def test_build_grained_joins_within_grain_only():
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=31), "flow_a": np.ones(31)})
    weekly = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=4, freq="W-MON"),
                           "rate_w": np.full(4, 0.5)})
    gd = build_grained(
        {"flow_a": daily, "rate_w": weekly},
        {"flow_a": "day", "rate_w": "week"},
        {"flow_a": "flow", "rate_w": "rate"},
    )
    # The weekly metric does not erase daily rows and vice versa.
    assert len(gd.frame("day")) == 31
    assert len(gd.frame("week")) == 4
    assert gd.date_start == pd.Timestamp("2024-01-01")
    assert gd.date_end == pd.Timestamp("2024-01-31")


# --- Date-grid contiguity (1.1) ---


def test_build_grained_rejects_a_hole_in_the_spine():
    """Positional indexing (model t, lags, bootstrap blocks) assumes a gap-free
    spine, so a hole silently shifts every downstream date rather than failing."""
    dates = pd.date_range("2024-01-01", periods=31).delete(15)  # drop 2024-01-16
    daily = pd.DataFrame({"date": dates, "a": np.ones(len(dates))})

    with pytest.raises(RuntimeError, match="missing period"):
        build_grained({"a": daily}, {"a": "day"}, {"a": "flow"})


def test_missing_dates_are_named_and_capped_at_ten():
    dates = pd.date_range("2024-01-01", periods=60)
    holes = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]  # 12 gaps -> 10 shown + count
    daily = pd.DataFrame({"date": dates.delete(holes), "a": np.ones(60 - len(holes))})

    with pytest.raises(RuntimeError) as excinfo:
        build_grained({"a": daily}, {"a": "day"}, {"a": "flow"})
    message = str(excinfo.value)
    assert "2024-01-06" in message          # the first missing date, named
    assert "and 2 more" in message          # 12 missing, 10 shown
    assert "2024-01-18" not in message      # the 11th+ are not spelled out


def test_contiguous_weekly_and_monthly_spines_are_accepted():
    """The check is grain-aware: consecutive Mondays are contiguous at week
    grain even though they are 7 days apart."""
    weekly = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=8, freq="W-MON"),
                           "w": np.ones(8)})
    monthly = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=6, freq="MS"),
                            "m": np.ones(6)})
    gd = build_grained(
        {"w": weekly, "m": monthly}, {"w": "week", "m": "month"},
        {"w": "flow", "m": "flow"},
    )
    assert len(gd.frame("week")) == 8
    assert len(gd.frame("month")) == 6


def test_inner_join_drop_is_logged(caplog):
    """A date present for only some metrics is dropped by the join; the
    survivors here stay contiguous, so only the log records the loss."""
    full = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=20), "a": np.ones(20)})
    short = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=15), "b": np.ones(15)})

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(
            {"a": full, "b": short}, {"a": "day", "b": "day"},
            {"a": "flow", "b": "flow"},
        )

    assert len(gd.frame("day")) == 15
    assert "dropped 5 period(s)" in caplog.text


def test_series_resamples_up_by_kind():
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=14), "a": np.ones(14)})
    gd = build_grained({"a": daily}, {"a": "day"}, {"a": "flow"})
    weekly = gd.series("a", "week")
    np.testing.assert_allclose(weekly["a"].to_numpy(), [7.0, 7.0])


def test_fit_frame_aligns_mixed_grains():
    # Weekly target over a daily flow parent: parent sums to whole weeks.
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=28), "starts": np.ones(28)})
    weekly = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=4, freq="W-MON"),
                           "conv": [3.0, 4.0, 5.0, 6.0]})
    gd = build_grained(
        {"starts": daily, "conv": weekly},
        {"starts": "day", "conv": "week"},
        {"starts": "flow", "conv": "flow"},
    )
    ff = gd.fit_frame("conv", ["starts"], "week")
    assert list(ff.columns) == ["date", "conv", "starts"]
    assert len(ff) == 4
    np.testing.assert_allclose(ff["starts"].to_numpy(), [7.0] * 4)


def test_grained_missing_grain_raises():
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5), "a": np.ones(5)})
    gd = build_grained({"a": daily}, {"a": "day"}, {"a": "flow"})
    with pytest.raises(ValueError, match="No metrics at grain 'month'"):
        gd.frame("month")


# --- freshness (last_observed / data_through) ---

def test_build_grained_captures_last_observed_before_join():
    """Freshness is per-metric from each provider frame, surviving the
    within-grain inner join that trims to the common tail."""
    fresh = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=10), "a": np.ones(10)})
    stale = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=7), "b": np.ones(7)})
    gd = build_grained(
        {"a": fresh, "b": stale},
        {"a": "day", "b": "day"},
        {"a": "flow", "b": "flow"},
    )
    # Joined day frame is trimmed to the stale metric's tail...
    assert len(gd.frame("day")) == 7
    # ...but per-metric freshness keeps the true edges.
    assert gd.last_observed["a"] == pd.Timestamp("2024-01-10")
    assert gd.last_observed["b"] == pd.Timestamp("2024-01-07")
    assert gd.data_through("a") == pd.Timestamp("2024-01-10")


def test_data_through_is_period_end_for_coarse_grains():
    weekly = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3, freq="W-MON"),
                           "w": np.ones(3)})
    gd = build_grained({"w": weekly}, {"w": "week"}, {"w": "flow"})
    # Last observed week starts Mon Jan 15 -> covered through Sun Jan 21.
    assert gd.last_observed["w"] == pd.Timestamp("2024-01-15")
    assert gd.data_through("w") == pd.Timestamp("2024-01-21")


def test_from_frame_sets_last_observed():
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5), "a": np.ones(5)})
    gd = ensure_grained(df)
    assert gd.data_through("a") == pd.Timestamp("2024-01-05")
    assert gd.data_through("missing") is None

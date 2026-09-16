"""Grain arithmetic, kind-aware resampling, and the GrainedData container."""

import logging

import numpy as np
import pandas as pd
import pytest

from breakdown.grains import (
    build_grained,
    coarsest,
    default_reference_window,
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
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-02-01"),
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
    out = steps_between(
        pd.DatetimeIndex(["2024-01-08", "2024-01-15"]), pd.Timestamp("2024-01-01"), "week"
    )
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
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=10),
            "a": np.arange(10.0),
            "b": np.ones(10),
        }
    )
    gd = ensure_grained(df)
    assert gd.grain_of == {"a": "day", "b": "day"}
    assert gd.kind_of == {"a": "flow", "b": "flow"}
    pd.testing.assert_frame_equal(gd.frame("day")[["date", "a", "b"]], df)
    assert ensure_grained(gd) is gd


def test_build_grained_joins_within_grain_only():
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=31), "flow_a": np.ones(31)})
    weekly = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=4, freq="W-MON"), "rate_w": np.full(4, 0.5)}
    )
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
    assert "2024-01-06" in message  # the first missing date, named
    assert "and 2 more" in message  # 12 missing, 10 shown
    assert "2024-01-18" not in message  # the 11th+ are not spelled out


def test_contiguous_weekly_and_monthly_spines_are_accepted():
    """The check is grain-aware: consecutive Mondays are contiguous at week
    grain even though they are 7 days apart."""
    weekly = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=8, freq="W-MON"), "w": np.ones(8)}
    )
    monthly = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=6, freq="MS"), "m": np.ones(6)}
    )
    gd = build_grained(
        {"w": weekly, "m": monthly},
        {"w": "week", "m": "month"},
        {"w": "flow", "m": "flow"},
    )
    assert len(gd.frame("week")) == 8
    assert len(gd.frame("month")) == 6


def test_a_short_series_narrows_only_itself(caplog):
    """The join is outer (GitHub #112, second suggestion): a metric five days
    shorter than its sibling keeps its own range and the sibling keeps its
    own — the grain frame spans the union, the short metric is `NaN` outside
    its range, and `series` never returns those periods. The log names the
    short metric and says nothing was clipped, because nothing was."""
    full = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=20), "a": np.ones(20)})
    short = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=15), "b": np.ones(15)})

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(
            {"a": full, "b": short},
            {"a": "day", "b": "day"},
            {"a": "flow", "b": "flow"},
        )

    assert len(gd.frame("day")) == 20
    assert gd.frame("day")["b"].isna().sum() == 5
    assert len(gd.series("a")) == 20
    assert len(gd.series("b")) == 15
    assert not gd.series("b")["b"].isna().any()
    assert gd.span_of["b"] == (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-15"))
    assert gd.date_end == pd.Timestamp("2024-01-20")
    assert "`b` at 2024-01-15, 5 day period(s) short" in caplog.text
    assert "nothing was clipped" in caplog.text


# --- short series (GitHub #112) ---


def _days(name, start, end):
    dates = pd.date_range(start, end)
    return pd.DataFrame({"date": dates, name: np.ones(len(dates))})


def test_trailing_short_series_is_named_at_load(caplog):
    """The issue's scenario: a frozen feed ends 2026-08-08, the rest run to
    08-26. The day-grain join used to cut every sibling to the feed's edge;
    now the siblings keep their range and one WARNING per grain names the
    short metric, its edge, the grain's reach, and what that costs — every
    analysis that *reads* the feed stops at its edge. The same facts travel
    on `short_series`."""
    per = {m: _days(m, "2026-06-01", "2026-08-26") for m in ["orders", "sessions"]}
    per["paid_spend"] = _days("paid_spend", "2026-06-01", "2026-08-08")

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(per, {m: "day" for m in per}, {m: "flow" for m in per})

    assert gd.series("orders")["date"].max() == pd.Timestamp("2026-08-26")
    assert gd.series("paid_spend")["date"].max() == pd.Timestamp("2026-08-08")
    assert gd.short_series == {
        "day": {
            "trailing": {
                "reach": "2026-08-26",
                "short": {"paid_spend": {"ends": "2026-08-08", "periods": 18}},
            }
        }
    }
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one warning per grain, not one per metric"
    text = warnings[0].getMessage()
    assert "day grain: `paid_spend` ends before the grain's reach of 2026-08-26" in text
    assert "`paid_spend` at 2026-08-08, 18 day period(s) short" in text
    # Actionable: it says what the short series costs and what to do about it.
    assert "no analysis that reads it may end after 2026-08-08" in text
    assert "nothing was clipped and nothing was filled" in text


def test_leading_short_series_is_named_at_load(caplog):
    """The symmetric case — a channel switched on partway through the window.
    The other metrics keep January; the late one starts when it starts."""
    per = {m: _days(m, "2026-06-01", "2026-08-26") for m in ["orders", "sessions"]}
    per["launched"] = _days("launched", "2026-06-15", "2026-08-26")

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(per, {m: "day" for m in per}, {m: "flow" for m in per})

    assert gd.series("orders")["date"].min() == pd.Timestamp("2026-06-01")
    assert gd.series("launched")["date"].min() == pd.Timestamp("2026-06-15")
    assert gd.short_series == {
        "day": {
            "leading": {
                "reach": "2026-06-01",
                "short": {"launched": {"starts": "2026-06-15", "periods": 14}},
            }
        }
    }
    text = caplog.text
    assert "`launched` starts after the grain's earliest series at 2026-06-01" in text
    assert "no analysis that reads it may start before 2026-06-15" in text


def test_several_short_metrics_at_both_edges_in_one_warning(caplog):
    """Every short metric is named with its own edge — there is no single
    culprit now that none of them bounds the others — and a grain short at
    both ends gets one line carrying both clauses. Grains where every series
    agrees stay absent from the record entirely."""
    per = {m: _days(m, "2026-06-01", "2026-08-26") for m in ["orders", "sessions"]}
    per["feed_a"] = _days("feed_a", "2026-06-01", "2026-08-08")
    per["feed_b"] = _days("feed_b", "2026-06-15", "2026-08-01")
    per["mrr"] = pd.DataFrame(
        {"date": pd.date_range("2026-06-01", periods=3, freq="MS"), "mrr": np.ones(3)}
    )
    grain_of = {m: "day" for m in per}
    grain_of["mrr"] = "month"
    kind_of = {m: "flow" for m in per}
    kind_of["mrr"] = "stock"

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(per, grain_of, kind_of)

    assert set(gd.short_series) == {"day"}
    assert gd.short_series["day"]["trailing"]["short"] == {
        "feed_a": {"ends": "2026-08-08", "periods": 18},
        "feed_b": {"ends": "2026-08-01", "periods": 25},
    }
    assert gd.short_series["day"]["leading"]["short"] == {
        "feed_b": {"starts": "2026-06-15", "periods": 14}
    }
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "`feed_a`, `feed_b` end before" in text
    assert "; and `feed_b` starts after" in text
    # The other day-grain metrics are untouched.
    assert len(gd.series("orders")) == 87
    assert len(gd.frame("month")) == 3


def test_aligned_series_produce_no_short_record_and_no_warning(caplog):
    per = {m: _days(m, "2026-06-01", "2026-08-26") for m in ["orders", "sessions", "spend"]}

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        gd = build_grained(per, {m: "day" for m in per}, {m: "flow" for m in per})

    assert gd.short_series == {}
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not gd.frame("day").isna().any().any()
    # The back-compat shim has nothing to report either.
    assert ensure_grained(per["orders"]).short_series == {}


def test_fit_frame_is_the_intersection_of_the_node_and_its_parents_only():
    """A fit reads the periods the node and *its* parents share; a shorter
    metric elsewhere at the grain is not in the frame and cannot narrow it."""
    per = {
        "a": _days("a", "2024-01-01", "2024-01-20"),
        "b": _days("b", "2024-01-01", "2024-01-15"),
        "c": _days("c", "2024-01-06", "2024-01-10"),
    }
    gd = build_grained(per, {m: "day" for m in per}, {m: "flow" for m in per})
    assert len(gd.fit_frame("a", [], "day")) == 20
    assert len(gd.fit_frame("a", ["b"], "day")) == 15
    assert len(gd.fit_frame("a", ["c"], "day")) == 5
    assert len(gd.fit_frame("b", ["c"], "day")) == 5
    assert gd.spans_at(["a", "c"], "day") == {
        "a": (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-20")),
        "c": (pd.Timestamp("2024-01-06"), pd.Timestamp("2024-01-10")),
    }


def test_disjoint_ranges_share_no_fit_frame_and_the_refusal_names_both():
    """Two metrics with no period in common: the grain frame spans the union
    (the gap between them belongs to neither, `NaN` for both), each `series`
    is its own range, and a fit across them is refused naming both ranges."""
    per = {"a": _days("a", "2024-01-01", "2024-01-10"), "b": _days("b", "2024-01-20", "2024-01-30")}
    gd = build_grained(per, {m: "day" for m in per}, {m: "flow" for m in per})
    assert len(gd.frame("day")) == 30
    assert len(gd.series("a")) == 10
    assert len(gd.series("b")) == 11
    assert gd.frame("day").iloc[12][["a", "b"]].isna().all()
    with pytest.raises(RuntimeError, match=r"'a' runs \[2024-01-01, 2024-01-10\]"):
        gd.fit_frame("a", ["b"], "day")


def test_a_hole_inside_one_metric_is_refused_before_the_join():
    """The outer join would turn a missing date inside one metric's range into
    a `NaN` indistinguishable from an undefined value, so contiguity is
    checked per metric on its own frame, and the refusal names it."""
    a = _days("a", "2024-01-01", "2024-01-10")
    b = _days("b", "2024-01-01", "2024-01-10").drop(index=4)
    with pytest.raises(RuntimeError, match=r"1 missing period\(s\).*\['b'\]"):
        build_grained({"a": a, "b": b}, {"a": "day", "b": "day"}, {"a": "flow", "b": "flow"})


def test_series_resamples_up_by_kind():
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=14), "a": np.ones(14)})
    gd = build_grained({"a": daily}, {"a": "day"}, {"a": "flow"})
    weekly = gd.series("a", "week")
    np.testing.assert_allclose(weekly["a"].to_numpy(), [7.0, 7.0])


def test_fit_frame_aligns_mixed_grains():
    # Weekly target over a daily flow parent: parent sums to whole weeks.
    daily = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=28), "starts": np.ones(28)})
    weekly = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=4, freq="W-MON"), "conv": [3.0, 4.0, 5.0, 6.0]}
    )
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


def test_build_grained_captures_last_observed_per_metric():
    """Freshness is per-metric from each provider frame; the join spans the
    union and each metric's edge is its own."""
    fresh = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=10), "a": np.ones(10)})
    stale = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=7), "b": np.ones(7)})
    gd = build_grained(
        {"a": fresh, "b": stale},
        {"a": "day", "b": "day"},
        {"a": "flow", "b": "flow"},
    )
    assert len(gd.frame("day")) == 10
    assert len(gd.series("b")) == 7
    # Per-metric freshness keeps the true edges.
    assert gd.last_observed["a"] == pd.Timestamp("2024-01-10")
    assert gd.last_observed["b"] == pd.Timestamp("2024-01-07")
    assert gd.data_through("a") == pd.Timestamp("2024-01-10")


def test_data_through_is_period_end_for_coarse_grains():
    weekly = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=3, freq="W-MON"), "w": np.ones(3)}
    )
    gd = build_grained({"w": weekly}, {"w": "week"}, {"w": "flow"})
    # Last observed week starts Mon Jan 15 -> covered through Sun Jan 21.
    assert gd.last_observed["w"] == pd.Timestamp("2024-01-15")
    assert gd.data_through("w") == pd.Timestamp("2024-01-21")


def test_from_frame_sets_last_observed():
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5), "a": np.ones(5)})
    gd = ensure_grained(df)
    assert gd.data_through("a") == pd.Timestamp("2024-01-05")
    assert gd.data_through("missing") is None


# --- default reference window (the matched adjacent block) ---


def test_default_reference_plain_4x():
    # 7-day analysis, ample history: 4x7 = 28 days immediately before.
    ref = default_reference_window("2024-03-01", "2024-03-07", "2023-01-01")
    assert ref == ("2024-02-02", "2024-02-29")


def test_default_reference_min_floor_beats_short_multiple():
    # 3-day analysis: 4x3 = 12 < 28, so the 28-day floor wins.
    ref = default_reference_window("2024-03-01", "2024-03-03", "2023-01-01")
    assert ref == ("2024-02-02", "2024-02-29")


def test_default_reference_week_align_already_multiple():
    # 14-day analysis with seasonality in scope: 56 days, already a 7-multiple.
    ref = default_reference_window("2024-03-01", "2024-03-14", "2023-01-01", week_align=True)
    assert ref == ("2024-01-05", "2024-02-29")
    assert ((pd.Timestamp(ref[1]) - pd.Timestamp(ref[0])).days + 1) % 7 == 0


def test_default_reference_week_align_rounds_up():
    # 10-day analysis: 4x10 = 40 -> 42 (whole weeks).
    ref = default_reference_window("2024-03-01", "2024-03-10", "2023-01-01", week_align=True)
    assert (pd.Timestamp(ref[1]) - pd.Timestamp(ref[0])).days + 1 == 42
    assert ref[1] == "2024-02-29"


def test_default_reference_at_data_start_raises():
    with pytest.raises(ValueError, match="beginning of the loaded data"):
        default_reference_window("2024-01-01", "2024-01-07", "2024-01-01")


def test_default_reference_clamped_to_data_start():
    # Only 20 days of history before the analysis window.
    ref = default_reference_window("2024-01-21", "2024-01-27", "2024-01-01")
    assert ref == ("2024-01-01", "2024-01-20")


def test_default_reference_clamp_trims_to_whole_weeks():
    # 20 days available with week_align: trimmed down to 14 (2 whole weeks)
    # ending the day before the analysis window.
    ref = default_reference_window("2024-01-21", "2024-01-27", "2024-01-01", week_align=True)
    assert ref == ("2024-01-07", "2024-01-20")


def test_default_reference_short_stub_kept():
    # Fewer than 7 days available: the stub is kept (advisories own the
    # warning), even with week_align.
    ref = default_reference_window("2024-01-06", "2024-01-12", "2024-01-01", week_align=True)
    assert ref == ("2024-01-01", "2024-01-05")


def test_default_reference_month_grain_whole_analysis_month():
    # Whole-month analysis: 4x30 = 120 days back from Mar 31 contains whole
    # months without any extension.
    ref = default_reference_window("2024-04-01", "2024-04-30", "2023-01-01", coarsest_grain="month")
    assert ref[1] == "2024-03-31"
    assert snap_window(ref[0], ref[1], "month") is not None


def test_default_reference_month_grain_extends_for_whole_period():
    # Short analysis: the 28-day block [Feb 18, Mar 16] holds no whole month,
    # so the window reaches back to cover February.
    ref = default_reference_window("2024-03-17", "2024-03-23", "2023-01-01", coarsest_grain="month")
    assert ref[1] == "2024-03-16"
    snapped = snap_window(ref[0], ref[1], "month")
    assert snapped is not None and snapped.n_periods >= 1


def test_default_reference_month_extension_reclamps_to_data_start():
    # Data starts mid-February: the month extension cannot reach a whole
    # month, so the clamped window stands (nodes report status downstream).
    ref = default_reference_window("2024-03-17", "2024-03-23", "2024-02-15", coarsest_grain="month")
    assert ref == ("2024-02-18", "2024-03-16")
    assert snap_window(ref[0], ref[1], "month") is None


def test_default_reference_inverted_analysis_raises():
    with pytest.raises(ValueError, match="on or before"):
        default_reference_window("2024-03-10", "2024-03-01", "2023-01-01")


def test_default_reference_invalid_date_raises():
    with pytest.raises(ValueError, match="not a valid date"):
        default_reference_window("not-a-date", "2024-03-01", "2023-01-01")


# --- the readable-history floor (roadmap M1) ---


def test_default_reference_clamped_to_earliest_start():
    """`earliest_start` is a hard floor above the data start: the block is
    shortened to respect it rather than reaching into history that the caller's
    lags make unreadable."""
    ref = default_reference_window(
        "2024-01-21", "2024-01-27", "2024-01-01", earliest_start="2024-01-08"
    )
    assert ref == ("2024-01-08", "2024-01-20")


def test_default_reference_earliest_start_below_data_start_is_ignored():
    """The loaded data is still the other hard bound; the floor only ever
    tightens it."""
    ref = default_reference_window(
        "2024-01-21", "2024-01-27", "2024-01-01", earliest_start="2023-06-01"
    )
    assert ref == ("2024-01-01", "2024-01-20")


def test_default_reference_earliest_start_trims_to_whole_weeks():
    ref = default_reference_window(
        "2024-01-21", "2024-01-27", "2024-01-01", week_align=True, earliest_start="2024-01-03"
    )
    assert ref == ("2024-01-07", "2024-01-20")


def test_default_reference_below_earliest_start_raises_about_the_lags():
    """No readable reference at all: the refusal names the floor, the data
    start and the fix, instead of leaving a coverage check downstream to cite a
    shifted date the caller never typed."""
    with pytest.raises(ValueError, match="not enough history") as excinfo:
        default_reference_window(
            "2024-01-06", "2024-01-12", "2024-01-01", earliest_start="2024-01-10"
        )
    message = str(excinfo.value)
    assert "2024-01-10" in message and "2024-01-01" in message


def test_default_reference_at_data_start_keeps_its_own_message():
    """The pre-existing refusal is unchanged when no floor is in play — it is a
    different problem with a different fix."""
    with pytest.raises(ValueError, match="beginning of the loaded data"):
        default_reference_window(
            "2024-01-01", "2024-01-07", "2024-01-01", earliest_start="2024-01-01"
        )

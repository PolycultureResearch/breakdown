"""Grain-aware RCA and simulation (1.7 phase 4): per-node window snapping,
mixed-grain attribution, degenerate-CI reporting, and cross-grain simulate
scaling. The day-grain path is pinned bit-for-bit in test_rca.py."""

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.engine.simulate import ScenarioRequest, run_scenario
from breakdown.grains import build_grained
from breakdown.parser import Parser

MIXED_YAML = """
metrics:
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: trial_conversion_rate
    source: dbt.metric.trial_conversion_rate
    grain: week
    kind: rate
  - name: conversions
    source: dbt.metric.conversions
    grain: week
    formula: "trial_starts * trial_conversion_rate"
    parents: [trial_starts, trial_conversion_rate]
"""


def mixed_grain_data(n_weeks=20, seed=42):
    """Daily flow `trial_starts`, native-weekly rate, and a weekly formula
    node satisfying `conversions_w = sum_w(trial_starts) * rate_w` exactly."""
    rng = np.random.default_rng(seed)
    n_days = n_weeks * 7
    days = pd.date_range("2024-01-01", periods=n_days)  # Monday start
    starts = 100.0 + rng.normal(0, 10.0, n_days)
    weekly_sum = starts.reshape(n_weeks, 7).sum(axis=1)
    weeks = pd.date_range("2024-01-01", periods=n_weeks, freq="W-MON")
    rate = 0.2 + 0.05 * rng.standard_normal(n_weeks).cumsum() * 0.1
    conversions = weekly_sum * rate  # exact identity at week grain
    return build_grained(
        {
            "trial_starts": pd.DataFrame({"date": days, "trial_starts": starts}),
            "trial_conversion_rate": pd.DataFrame(
                {"date": weeks, "trial_conversion_rate": rate}
            ),
            "conversions": pd.DataFrame({"date": weeks, "conversions": conversions}),
        },
        {"trial_starts": "day", "trial_conversion_rate": "week", "conversions": "week"},
        {"trial_starts": "flow", "trial_conversion_rate": "rate", "conversions": "flow"},
    )


REF = ("2024-01-01", "2024-02-25")   # 8 whole weeks
AN = ("2024-02-26", "2024-04-21")    # 8 whole weeks


def test_mixed_grain_rca_end_to_end():
    """A weekly formula node over a daily flow parent and a native-weekly
    rate: attribution runs at week grain, sums to the gap, and an exact
    identity leaves no unexplained residual."""
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    result = run_rca(dag, data, {}, "conversions", *REF, *AN)

    node = result["nodes"]["conversions"]
    assert node["status"] == "ok"
    assert node["grain"] == "week"
    assert node["attribution_method"] == "shapley"
    assert node["effective_windows"]["reference"]["n_periods"] == 8
    assert node["effective_windows"]["analysis"]["n_periods"] == 8
    assert abs(node["unexplained"]) < 1e-9  # exact identity, symmetric per-period
    total = sum(c["estimate"] for c in node["contributions"])
    assert abs(total - node["gap"]) < 0.1 * max(1.0, abs(node["gap"]))
    # The daily parent is reported at day grain, the rate at week grain.
    assert result["nodes"]["trial_starts"]["grain"] == "day"
    assert result["nodes"]["trial_conversion_rate"]["grain"] == "week"


def test_mixed_grain_shapley_attribution_efficiency():
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    sh = shapley_attribution(dag, data, "conversions", *REF, *AN)

    assert sh["grain"] == "week"
    assert sh["effective_windows"]["analysis"]["n_periods"] == 8
    assert abs(sum(sh["attribution"].values()) - sh["gap"]) < 1e-9


def test_window_shorter_than_grain_degrades_not_errors():
    """A weekly target with sub-week windows reports a status; RCA returns."""
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    result = run_rca(
        dag, data, {}, "conversions",
        "2024-01-02", "2024-01-05", "2024-01-06", "2024-01-09",
    )

    node = result["nodes"]["conversions"]
    assert node["status"] == "window_shorter_than_grain"
    assert node["gap"] is None
    assert node["contributions"] == []
    # Daily ancestors still analyze fine.
    assert result["nodes"]["trial_starts"]["status"] == "ok"
    # No contributions anywhere -> no ranked causes with weight.
    assert all(r["score"] == 0.0 for r in result["ranked_causes"])


def test_shapley_endpoint_errors_loudly_on_short_window():
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    with pytest.raises(ValueError, match="contains no whole 'week' period"):
        shapley_attribution(
            dag, data, "conversions",
            "2024-01-02", "2024-01-05", "2024-01-06", "2024-01-09",
        )


def test_single_period_window_suppresses_bootstrap_ci():
    """One whole week in the analysis window: the block bootstrap degenerates,
    so intervals are withheld and the node is flagged."""
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    result = run_rca(
        dag, data, {}, "conversions",
        "2024-01-01", "2024-01-28", "2024-02-26", "2024-03-03",
    )

    node = result["nodes"]["conversions"]
    assert node["status"] == "ok"
    assert node["ci_status"] == "degenerate_single_period"
    assert node["effective_windows"]["analysis"]["n_periods"] == 1
    for c in node["contributions"]:
        assert c["ci_95"] is None
        assert c["prob_same_direction"] is None
        assert c["estimate"] is not None


MONTHLY_LAG_YAML = """
metrics:
  - name: campaigns
    source: dbt.metric.campaigns
    grain: month
  - name: signups
    source: dbt.metric.signups
    grain: month
    parents: [campaigns]
    lags: { campaigns: 1 }
"""


def test_monthly_lag_shifts_whole_months_across_year_boundary():
    """A month-grain lagged edge: the lag shift lands on month starts across
    the year boundary and RCA attributes the lagged parent's change."""
    rng = np.random.default_rng(5)
    months = pd.date_range("2023-01-01", periods=26, freq="MS")
    campaigns = 50.0 + rng.normal(0, 5.0, 26)
    campaigns[18:] += 30.0  # step up from 2024-07 onward
    signups = np.empty(26)
    signups[0] = 0.4 * campaigns[0]
    signups[1:] = 0.4 * campaigns[:-1]
    signups += rng.normal(0, 1.0, 26)
    data = build_grained(
        {
            "campaigns": pd.DataFrame({"date": months, "campaigns": campaigns}),
            "signups": pd.DataFrame({"date": months, "signups": signups}),
        },
        {"campaigns": "month", "signups": "month"},
        {"campaigns": "flow", "signups": "flow"},
    )
    dag = Parser(MONTHLY_LAG_YAML).dag

    result = run_rca(
        dag, data, {}, "signups",
        "2024-01-01", "2024-06-30", "2024-11-01", "2025-01-31",
        advi_draws=300,
    )

    node = result["nodes"]["signups"]
    assert node["status"] == "ok"
    assert node["grain"] == "month"
    assert node["effective_windows"]["analysis"]["n_periods"] == 3
    contrib = node["contributions"][0]
    assert contrib["parent"] == "campaigns"
    # The lagged parent stepped up ~30; expect a positive contribution near
    # 0.4 * 30 = 12 (posterior + bootstrap tolerance).
    assert 6.0 < contrib["estimate"] < 18.0


SIM_YAML = """
metrics:
  - name: daily_starts
    source: dbt.metric.daily_starts
  - name: weekly_total
    source: dbt.metric.weekly_total
    grain: week
    parents: [daily_starts]
"""


def test_simulate_scales_flow_delta_across_grains():
    """An intervention on a daily flow parent of a weekly child: beta was
    fitted against the weekly SUM, so a +10/day delta moves the child by
    beta * 70, not beta * 10."""
    rng = np.random.default_rng(7)
    n_weeks = 30
    days = pd.date_range("2024-01-01", periods=n_weeks * 7)
    starts = 100.0 + rng.normal(0, 8.0, n_weeks * 7)
    weekly_sum = starts.reshape(n_weeks, 7).sum(axis=1)
    weeks = pd.date_range("2024-01-01", periods=n_weeks, freq="W-MON")
    total = 0.5 * weekly_sum + rng.normal(0, 5.0, n_weeks)
    data = build_grained(
        {
            "daily_starts": pd.DataFrame({"date": days, "daily_starts": starts}),
            "weekly_total": pd.DataFrame({"date": weeks, "weekly_total": total}),
        },
        {"daily_starts": "day", "weekly_total": "week"},
        {"daily_starts": "flow", "weekly_total": "flow"},
    )
    dag = Parser(SIM_YAML).dag

    scenario = ScenarioRequest(
        baseline_start="2024-06-03", baseline_end="2024-07-28",
        interventions=[{"metric": "daily_starts", "mode": "delta", "value": 10.0}],
    )
    result = run_scenario(dag, data, {}, scenario, advi_draws=300)

    node = result["nodes"]["weekly_total"]
    assert node["status"] == "affected"
    # 0.5 * 7 * 10 = 35 per week; generous band for posterior noise.
    assert 25.0 < node["delta"]["estimate"] < 45.0

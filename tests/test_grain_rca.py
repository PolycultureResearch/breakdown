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
from tests.synthetic import win

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
            "trial_conversion_rate": pd.DataFrame({"date": weeks, "trial_conversion_rate": rate}),
            "conversions": pd.DataFrame({"date": weeks, "conversions": conversions}),
        },
        {"trial_starts": "day", "trial_conversion_rate": "week", "conversions": "week"},
        {"trial_starts": "flow", "trial_conversion_rate": "rate", "conversions": "flow"},
    )


REF = ("2024-01-01", "2024-02-25")  # 8 whole weeks
AN = ("2024-02-26", "2024-04-21")  # 8 whole weeks


def test_mixed_grain_rca_end_to_end():
    """A weekly formula node over a daily flow parent and a native-weekly
    rate: attribution runs at week grain, sums to the gap, and an exact
    identity leaves no unexplained residual."""
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    result = run_rca(dag, data, {}, "conversions", **win(REF, AN))

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

    sh = shapley_attribution(dag, data, "conversions", **win(REF, AN))

    assert sh["grain"] == "week"
    assert sh["effective_windows"]["analysis"]["n_periods"] == 8
    assert abs(sum(sh["attribution"].values()) - sh["gap"]) < 1e-9


def test_target_with_no_whole_period_is_refused_before_any_fitting():
    """A weekly target with sub-week windows is a 422-shaped refusal, not a
    per-node status — and it fires *before* the fits.

    This test used to pin the opposite (`degrades, not errors`), and the
    change of policy is deliberate: per-node degradation exists so one bad
    node does not cost the rest of the tree, but when the *target* has no
    whole period there is no rest of the tree — nothing can be attributed to
    a metric with no measured movement. Under the old policy a user paid for
    every day-grain ancestor's ADVI fit and then read "not analyzed" on the
    one node they asked about, from a message that named neither the grain
    nor a window that would have worked. The refusal names all three.

    Per-node `window_shorter_than_grain` still exists, as a backstop for DAGs
    assembled without the parser: through any parser-built tree the target is
    always the coarsest node in its own scope (a parent may never be coarser
    than its child), so if any node's windows snap to nothing the target's do
    too, and the refusal wins.
    """
    fitted_day_yaml = """
metrics:
  - name: sessions
    source: dbt.metric.sessions
  - name: trial_starts
    source: dbt.metric.trial_starts
    parents: [sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
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
    dag = Parser(fitted_day_yaml).dag
    base = mixed_grain_data()
    days = base.series("trial_starts", "day")
    data = build_grained(
        {
            "sessions": pd.DataFrame(
                {"date": days["date"], "sessions": days["trial_starts"].to_numpy() * 10.0}
            ),
            "trial_starts": days,
            "trial_conversion_rate": base.series("trial_conversion_rate", "week"),
            "conversions": base.series("conversions", "week"),
        },
        {
            "sessions": "day",
            "trial_starts": "day",
            "trial_conversion_rate": "week",
            "conversions": "week",
        },
        {
            "sessions": "flow",
            "trial_starts": "flow",
            "trial_conversion_rate": "rate",
            "conversions": "flow",
        },
    )

    traces = {}
    with pytest.raises(ValueError) as exc:
        run_rca(
            dag,
            data,
            traces,
            "conversions",
            **win(("2024-01-02", "2024-01-05"), ("2024-01-06", "2024-01-09")),
        )

    msg = str(exc.value)
    # (1) names the grain, (2) names the offending window, (3) names a window
    # that would work — the most recent whole week the data holds.
    assert "week grain" in msg
    assert "no whole week" in msg
    assert "2024-01-06" in msg and "2024-01-09" in msg
    assert "2024-05-13 → 2024-05-19" in msg
    # (4) and it refused before fitting anything: `trial_starts` is a
    # day-grain probabilistic node whose windows are fine, and under the old
    # policy its ADVI fit ran (and was cached) before the target's status was
    # ever discovered.
    assert traces == {}


def test_shapley_endpoint_errors_loudly_on_short_window():
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    with pytest.raises(ValueError, match="contains no whole 'week' period"):
        shapley_attribution(
            dag,
            data,
            "conversions",
            **win(("2024-01-02", "2024-01-05"), ("2024-01-06", "2024-01-09")),
        )


def test_single_period_window_suppresses_bootstrap_ci():
    """One whole week in the analysis window: the block bootstrap degenerates,
    so intervals are withheld and the node is flagged."""
    dag = Parser(MIXED_YAML).dag
    data = mixed_grain_data()

    result = run_rca(
        dag,
        data,
        {},
        "conversions",
        **win(("2024-01-01", "2024-01-28"), ("2024-02-26", "2024-03-03")),
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
        dag,
        data,
        {},
        "signups",
        **win(("2024-01-01", "2024-06-30"), ("2024-11-01", "2025-01-31")),
        draws=300,
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
    # The surfaced parent windows are the node's snapped windows shifted back
    # one whole month — landing on month starts across the year boundary.
    assert contrib["lag"] == 1
    assert contrib["parent_windows"] == {
        "reference": {"start": "2023-12-01", "end": "2024-05-31"},
        "analysis": {"start": "2024-10-01", "end": "2024-12-31"},
    }


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
        baseline_start="2024-06-03",
        baseline_end="2024-07-28",
        interventions=[{"metric": "daily_starts", "mode": "delta", "value": 10.0}],
    )
    result = run_scenario(dag, data, {}, scenario, draws=300)

    node = result["nodes"]["weekly_total"]
    assert node["status"] == "affected"
    # 0.5 * 7 * 10 = 35 per week; generous band for posterior noise.
    assert 25.0 < node["delta"]["estimate"] < 45.0


# --- Cohort-aligned lagged identities (formula + lags, 1.7 phase 6) ---

COHORT_YAML = """
metrics:
  - name: starts
    source: dbt.metric.starts
  - name: cohort_rate
    source: dbt.metric.cohort_rate
  - name: conversions
    source: dbt.metric.conversions
    formula: "starts * cohort_rate"
    parents: [starts, cohort_rate]
    lags: { starts: 3 }
"""


def cohort_frame(n=100, seed=11, step_day=60, step=40.0, rate=0.25, noise=0.0):
    """conversions[t] = starts[t-3] * rate exactly (+ optional noise);
    starts steps up by `step` from `step_day` onward."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n)
    starts = 100.0 + rng.normal(0, 3.0, n)
    starts[step_day:] += step
    lagged = np.concatenate([np.full(3, starts[0]), starts[:-3]])
    conversions = lagged * rate + (rng.normal(0, noise, n) if noise else 0.0)
    return pd.DataFrame(
        {
            "date": dates,
            "starts": starts,
            "cohort_rate": np.full(n, rate),
            "conversions": conversions,
        }
    )


def test_lagged_identity_rca_recovers_lagged_change():
    """conversions[t] = starts[t-3] * rate: RCA reads the parent from the
    lag-shifted windows, so the starts step is fully attributed, the constant
    rate is a null player, and the exact identity leaves no unexplained."""
    dag = Parser(COHORT_YAML).dag
    frame = cohort_frame()
    # ref: pre-step; an: post-step even after the 3-day shift back.
    ref = ("2024-01-11", "2024-02-09")
    an = ("2024-03-04", "2024-04-02")

    result = run_rca(dag, frame, {}, "conversions", **win(ref, an))

    node = result["nodes"]["conversions"]
    assert abs(node["unexplained"]) < 1e-9
    contribs = {c["parent"]: c["estimate"] for c in node["contributions"]}
    # Gap is ~ rate * step = 10; starts carries it, the constant rate none.
    assert abs(node["gap"] - 10.0) < 1.0
    assert abs(contribs["starts"] - node["gap"]) < 0.5
    assert abs(contribs["cohort_rate"]) < 1e-6

    # The lagged parent's contribution says which of *its* days were examined
    # (3 days back); the unlagged parent carries no lag keys at all.
    by_parent = {c["parent"]: c for c in node["contributions"]}
    assert by_parent["starts"]["lag"] == 3
    assert by_parent["starts"]["parent_windows"] == {
        "reference": {"start": "2024-01-08", "end": "2024-02-06"},
        "analysis": {"start": "2024-03-01", "end": "2024-03-30"},
    }
    assert "lag" not in by_parent["cohort_rate"]
    assert "parent_windows" not in by_parent["cohort_rate"]

    # The other side of the coin from the short-window case above: a parent a
    # hop *did* reach, which explains none of the gap, stays in the ranking at
    # 0.0 with the child it was reached through. "We looked at the constant
    # rate and it moved nothing" is a finding; only a node no hop ever reached
    # is dropped, and this test would go quiet if the two were conflated.
    ranked = {r["metric"]: r for r in result["ranked_causes"]}
    assert set(ranked) == {"starts", "cohort_rate"}
    assert ranked["starts"]["score"] == pytest.approx(1.0)
    assert ranked["cohort_rate"]["score"] == 0.0
    assert ranked["cohort_rate"]["via"] == "conversions"

    sh = shapley_attribution(dag, frame, "conversions", **win(ref, an))
    assert abs(sum(sh["attribution"].values()) - sh["gap"]) < 1e-9
    # The standalone Shapley response carries the same map, lagged parents only.
    assert set(sh["parent_windows"]) == {"starts"}
    assert sh["parent_windows"]["starts"]["analysis"]["start"] == "2024-03-01"


def test_lagged_identity_residual_fit_trims_rows():
    """fit_metric on a lagged identity fits the residual of the *lagged*
    reconstruction and trims the leading max-lag rows."""
    from breakdown.engine.model import fit_metric

    dag = Parser(COHORT_YAML).dag
    frame = cohort_frame(noise=0.5)

    result = fit_metric(dag, frame, "conversions", draws=200, inference_method="advi")

    assert result.dates[0] == pd.Timestamp("2024-01-04")  # 3 rows trimmed
    assert len(result.dates) == 97
    # The lagged identity explains the series; the residual is just noise.
    assert result.y_std < 1.0

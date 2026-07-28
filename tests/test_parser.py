import pytest

from breakdown.parser import Parser


def test_valid_yaml_parsing():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: conversions
    source: dbt.metric.conversions
    parents:
      - dau
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
"""
    parser = Parser(yaml_content)
    assert "dau" in parser.dag.nodes
    assert "conversions" in parser.dag.nodes
    assert parser.dag.has_edge("dau", "conversions")

def test_invalid_distribution():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    priors:
      coefficient:
        distribution: "InvalidDist"
"""
    with pytest.raises(ValueError, match="Invalid distribution: InvalidDist"):
        Parser(yaml_content)

def test_cycle_detection():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [B]
  - name: B
    source: dbt.metric.B
    parents: [A]
"""
    with pytest.raises(ValueError, match="Metric tree contains cycles"):
        Parser(yaml_content)

def test_missing_parent():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [Missing]
"""
    with pytest.raises(ValueError, match="Parent metric 'Missing' not found for metric 'A'"):
        Parser(yaml_content)


# --- Formula validation tests ---

def test_formula_parsed_and_stored():
    yaml_content = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: aov
    source: dbt.metric.aov
  - name: revenue
    source: dbt.metric.revenue
    formula: "orders * aov"
    parents: [orders, aov]
"""
    parser = Parser(yaml_content)
    metric = parser.get_metric("revenue")
    assert metric.formula == "orders * aov"


def test_formula_with_undeclared_parent_raises():
    yaml_content = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: revenue
    source: dbt.metric.revenue
    formula: "orders * mystery_metric"
    parents: [orders]
"""
    with pytest.raises(ValueError, match="not listed in parents"):
        Parser(yaml_content)


def test_formula_invalid_syntax_raises():
    yaml_content = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: revenue
    source: dbt.metric.revenue
    formula: "orders *** aov"
    parents: [orders]
"""
    with pytest.raises(ValueError, match="formula"):
        Parser(yaml_content)


def test_formula_disallows_function_calls():
    yaml_content = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: revenue
    source: dbt.metric.revenue
    formula: "abs(orders)"
    parents: [orders]
"""
    with pytest.raises(ValueError, match="unsupported operation"):
        Parser(yaml_content)


# --- Per-parent prior validation tests ---

def test_per_parent_prior_key_accepted():
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: marketing_spend
    source: dbt.metric.marketing_spend
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions, marketing_spend]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
      marketing_spend:
        distribution: "HalfNormal"
        params: { sigma: 0.2 }
"""
    parser = Parser(yaml_content)
    metric = parser.get_metric("order_count")
    assert set(metric.priors.keys()) == {"coefficient", "marketing_spend"}


def test_prior_key_not_coefficient_or_parent_raises():
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      not_a_parent:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
"""
    with pytest.raises(ValueError, match="Prior key 'not_a_parent'"):
        Parser(yaml_content)


# --- Lag validation tests ---

def test_lag_key_not_parent_raises():
    yaml_content = """
metrics:
  - name: support_tickets
    source: dbt.metric.support_tickets
  - name: churn_rate
    source: dbt.metric.churn_rate
    parents: [support_tickets]
    lags: { daily_sessions: 21 }
"""
    with pytest.raises(ValueError, match="Lag key 'daily_sessions'"):
        Parser(yaml_content)


def test_lag_value_zero_raises():
    yaml_content = """
metrics:
  - name: support_tickets
    source: dbt.metric.support_tickets
  - name: churn_rate
    source: dbt.metric.churn_rate
    parents: [support_tickets]
    lags: { support_tickets: 0 }
"""
    with pytest.raises(ValueError, match="must be an integer >= 1"):
        Parser(yaml_content)


def test_lag_with_formula_accepted_as_cohort_identity():
    """formula + lags declares a cohort-aligned lagged identity:
    A[t] = f(parents shifted back by their lags)."""
    yaml_content = """
metrics:
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: cohort_rate
    source: dbt.metric.cohort_rate
  - name: conversions
    source: dbt.metric.conversions
    formula: "trial_starts * cohort_rate"
    parents: [trial_starts, cohort_rate]
    lags: { trial_starts: 14 }
"""
    metric = Parser(yaml_content).get_metric("conversions")
    assert metric.formula == "trial_starts * cohort_rate"
    assert metric.lags == {"trial_starts": 14}


def test_lagged_edge_accepted():
    yaml_content = """
metrics:
  - name: support_tickets
    source: dbt.metric.support_tickets
  - name: churn_rate
    source: dbt.metric.churn_rate
    parents: [support_tickets]
    lags: { support_tickets: 21 }
"""
    parser = Parser(yaml_content)
    assert parser.get_metric("churn_rate").lags == {"support_tickets": 21}


# --- Trend config tests ---

def test_trend_linear_string_default_sigma():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    trend: linear
"""
    metric = Parser(yaml_content).get_metric("dau")
    assert metric.trend.type == "linear"
    assert metric.trend.sigma == 0.05


def test_trend_dict_sigma():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    trend: { sigma: 0.2 }
"""
    metric = Parser(yaml_content).get_metric("dau")
    assert metric.trend.type == "linear"
    assert metric.trend.sigma == 0.2


def test_trend_invalid_type_string_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    trend: quadratic
"""
    with pytest.raises(ValueError, match="Unsupported trend type"):
        Parser(yaml_content)


def test_trend_negative_sigma_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    trend: { sigma: -1 }
"""
    with pytest.raises(ValueError, match="trend sigma must be > 0"):
        Parser(yaml_content)


# --- Grain & kind validation tests ---

def test_grain_and_kind_default_for_legacy_yaml():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
"""
    metric = Parser(yaml_content).get_metric("dau")
    assert metric.grain == "day"
    assert metric.kind == "flow"


def test_grain_and_kind_parsed():
    yaml_content = """
metrics:
  - name: mrr
    source: dbt.metric.mrr
    grain: month
    kind: stock
"""
    metric = Parser(yaml_content).get_metric("mrr")
    assert metric.grain == "month"
    assert metric.kind == "stock"


def test_invalid_grain_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    grain: hour
"""
    with pytest.raises(ValueError, match="grain must be one of"):
        Parser(yaml_content)


def test_invalid_kind_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    kind: balance
"""
    with pytest.raises(ValueError, match="kind must be one of"):
        Parser(yaml_content)


def test_parent_coarser_than_child_raises():
    yaml_content = """
metrics:
  - name: monthly_mrr
    source: dbt.metric.monthly_mrr
    grain: month
    kind: stock
  - name: daily_signups
    source: dbt.metric.daily_signups
    parents: [monthly_mrr]
"""
    with pytest.raises(ValueError, match="coarser grain 'month'"):
        Parser(yaml_content)


def test_finer_rate_parent_raises():
    yaml_content = """
metrics:
  - name: daily_arpu
    source: dbt.metric.daily_arpu
    kind: rate
  - name: weekly_revenue
    source: dbt.metric.weekly_revenue
    grain: week
    parents: [daily_arpu]
"""
    with pytest.raises(ValueError, match="rate parent 'daily_arpu' at finer grain"):
        Parser(yaml_content)


def test_weekly_parent_under_monthly_child_raises():
    yaml_content = """
metrics:
  - name: weekly_starts
    source: dbt.metric.weekly_starts
    grain: week
  - name: monthly_new_mrr
    source: dbt.metric.monthly_new_mrr
    grain: month
    parents: [weekly_starts]
"""
    with pytest.raises(ValueError, match="does not nest in 'month'"):
        Parser(yaml_content)


def test_finer_flow_parent_accepted():
    yaml_content = """
metrics:
  - name: daily_signups
    source: dbt.metric.daily_signups
  - name: weekly_conversions
    source: dbt.metric.weekly_conversions
    grain: week
    parents: [daily_signups]
"""
    parser = Parser(yaml_content)
    assert parser.dag.has_edge("daily_signups", "weekly_conversions")


def test_seasonality_period_below_two_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    seasonality:
      - period: 1
        name: degenerate
"""
    with pytest.raises(ValueError, match="period must be an integer >= 2"):
        Parser(yaml_content)


def test_day_grain_period_on_coarse_node_warns(caplog):
    yaml_content = """
metrics:
  - name: weekly_active
    source: dbt.metric.weekly_active
    grain: week
    seasonality:
      - period: 7
        name: suspicious
"""
    import logging
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        Parser(yaml_content)
    assert any("grain steps" in r.message for r in caplog.records)


# --- expected_signs validation tests ---

def test_expected_signs_parsed():
    yaml_content = """
metrics:
  - name: paid_cmau
    source: dbt.metric.paid_cmau
  - name: churn_mrr
    source: dbt.metric.churn_mrr
    parents: [paid_cmau]
    expected_signs: { paid_cmau: positive }
"""
    metric = Parser(yaml_content).get_metric("churn_mrr")
    assert metric.expected_signs == {"paid_cmau": "positive"}


def test_expected_signs_key_not_parent_raises():
    yaml_content = """
metrics:
  - name: paid_cmau
    source: dbt.metric.paid_cmau
  - name: churn_mrr
    source: dbt.metric.churn_mrr
    parents: [paid_cmau]
    expected_signs: { nope: positive }
"""
    with pytest.raises(ValueError, match="expected_signs key 'nope'"):
        Parser(yaml_content)


def test_expected_signs_bad_value_raises():
    yaml_content = """
metrics:
  - name: paid_cmau
    source: dbt.metric.paid_cmau
  - name: churn_mrr
    source: dbt.metric.churn_mrr
    parents: [paid_cmau]
    expected_signs: { paid_cmau: up }
"""
    with pytest.raises(ValueError, match="must be 'positive' or 'negative'"):
        Parser(yaml_content)


def test_expected_signs_on_formula_raises():
    yaml_content = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: aov
    source: dbt.metric.aov
  - name: revenue
    source: dbt.metric.revenue
    formula: "orders * aov"
    parents: [orders, aov]
    expected_signs: { orders: positive }
"""
    with pytest.raises(ValueError, match="expected_signs.*formula"):
        Parser(yaml_content)

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


def test_lag_with_formula_raises():
    yaml_content = """
metrics:
  - name: order_count
    source: dbt.metric.order_count
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
    lags: { order_count: 3 }
"""
    with pytest.raises(ValueError, match="both `formula` and `lags`"):
        Parser(yaml_content)


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

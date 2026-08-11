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


@pytest.mark.parametrize("period", [1, 2])
def test_seasonality_period_below_three_raises(period):
    """Period 2 sits at the Nyquist limit of its own grain: every Fourier term
    is identically zero or collinear with the intercept, so no amount of data
    identifies it. That is a config error, not a data shortage."""
    yaml_content = f"""
metrics:
  - name: dau
    source: dbt.metric.dau
    seasonality:
      - period: {period}
        name: degenerate
"""
    with pytest.raises(ValueError, match="period must be an integer >= 3"):
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


# --- direction (display goodness) validation tests ---


def test_direction_default_and_parsed():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: support_tickets
    source: dbt.metric.support_tickets
    direction: down_is_good
"""
    parser = Parser(yaml_content)
    assert parser.get_metric("dau").direction == "up_is_good"
    assert parser.get_metric("support_tickets").direction == "down_is_good"


def test_direction_invalid_raises():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    direction: sideways
"""
    with pytest.raises(ValueError, match="direction must be one of"):
        Parser(yaml_content)


# --- Dimension (slicing) validation tests ---


def test_dimension_shorthand_and_full_form():
    yaml_content = """
metrics:
  - name: signups
    source: dbt.metric.signups
    dimensions:
      region: customer__region
      plan:
        source: subscription__plan_tier
        top_k: 6
        values: [pro, team, enterprise]
"""
    metric = Parser(yaml_content).get_metric("signups")
    assert metric.dimensions["region"].source == "customer__region"
    assert metric.dimensions["region"].top_k == 8
    assert metric.dimensions["plan"].top_k == 6
    assert metric.dimensions["plan"].values == ["pro", "team", "enterprise"]


def test_dimension_top_k_out_of_bounds_raises():
    yaml_content = """
metrics:
  - name: signups
    source: dbt.metric.signups
    dimensions:
      region: { source: customer__region, top_k: 1 }
"""
    with pytest.raises(ValueError, match="top_k must be between 2 and 20"):
        Parser(yaml_content)


def test_dimension_name_must_be_identifier():
    yaml_content = """
metrics:
  - name: signups
    source: dbt.metric.signups
    dimensions:
      "app version": customer__app_version
"""
    with pytest.raises(ValueError, match="must be an identifier"):
        Parser(yaml_content)


def test_rate_dimension_requires_weight():
    yaml_content = """
metrics:
  - name: conversion_rate
    source: dbt.metric.conversion_rate
    kind: rate
    dimensions:
      region: customer__region
"""
    with pytest.raises(ValueError, match="needs a `weight`"):
        Parser(yaml_content)


def test_rate_dimension_weight_defaults_to_formula_denominator():
    yaml_content = """
metrics:
  - name: conversions
    source: dbt.metric.conversions
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: conversion_rate
    source: dbt.metric.conversion_rate
    kind: rate
    formula: "conversions / trial_starts"
    parents: [conversions, trial_starts]
    dimensions:
      region: customer__region
"""
    metric = Parser(yaml_content).get_metric("conversion_rate")
    assert metric.dimensions["region"].weight == "trial_starts"


def test_rate_dimension_explicit_weight_must_be_tree_metric():
    yaml_content = """
metrics:
  - name: conversion_rate
    source: dbt.metric.conversion_rate
    kind: rate
    dimensions:
      region: { source: customer__region, weight: trial_starts }
"""
    with pytest.raises(ValueError, match="weight 'trial_starts', which is not a metric"):
        Parser(yaml_content)


def test_weight_on_non_rate_dimension_raises():
    yaml_content = """
metrics:
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: signups
    source: dbt.metric.signups
    dimensions:
      region: { source: customer__region, weight: trial_starts }
"""
    with pytest.raises(ValueError, match="only meaningful for rate metrics"):
        Parser(yaml_content)


def test_provider_type_none_and_assumed_alias():
    """`provider: none` declares a cold-start tree; `assumed` is an alias
    normalized to `none` so downstream code has one spelling to check."""

    def yaml_for(ptype):
        return f"""
provider:
  type: {ptype}
metrics:
  - name: sessions
    source: assumed
    baseline: 1200
"""

    assert Parser(yaml_for("none")).config.provider.type == "none"
    assert Parser(yaml_for("assumed")).config.provider.type == "none"
    with pytest.raises(Exception, match="mock, local, cloud, warehouse, none"):
        Parser(yaml_for("nonsense"))


# --- Node binding contract (roadmap 2.9) -----------------------------------
#
# The binding is a fetch descriptor, not a semantic layer: every rule below
# exists because breaking it produces a plausible wrong number rather than an
# error. See knowledge/semantic_layer_connectivity_design.md §4.


def _bound_tree(bind_block: str, *, name: str = "revenue", extra: str = "") -> str:
    return f"""
metrics:
  - name: {name}
    source: warehouse.{name}
{extra}    bind:
{bind_block}
"""


SUM_BIND = """      relation: analytics.fct_orders
      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
"""


def test_binding_parses_and_lands_on_the_dag_node():
    defn = Parser(_bound_tree(SUM_BIND)).get_metric("revenue")
    assert defn.bind.relation == "analytics.fct_orders"
    assert defn.bind.grain_key == "order_id"
    assert defn.bind.agg == "sum"
    assert defn.bind.is_non_additive is False


def test_binding_dimension_join_parses():
    bind = (
        SUM_BIND
        + """      dimensions:
        region: {join: dim_customers, key: customer_id, column: region}
        channel: {column: channel}
"""
    )
    dims = Parser(_bound_tree(bind)).get_metric("revenue").bind.dimensions
    assert dims["region"].join == "dim_customers"
    assert dims["region"].key == "customer_id"
    assert dims["channel"].join is None


def test_relation_and_sql_are_mutually_exclusive():
    bind = SUM_BIND + "      sql: select 1\n"
    with pytest.raises(ValueError, match="exactly one of `relation`"):
        Parser(_bound_tree(bind))


def test_binding_needs_a_relation_or_sql():
    bind = """      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
"""
    with pytest.raises(ValueError, match="exactly one of `relation`"):
        Parser(_bound_tree(bind))


def test_sql_in_the_relation_field_is_rejected():
    bind = SUM_BIND.replace("relation: analytics.fct_orders", "relation: select * from t")
    with pytest.raises(ValueError, match="looks like SQL rather than a table"):
        Parser(_bound_tree(bind))


def test_inline_sql_relation_is_accepted():
    bind = """      sql: "select * from analytics.orders where is_test = false"
      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
"""
    assert Parser(_bound_tree(bind)).get_metric("revenue").bind.relation is None


RATIO_BIND = """      relation: analytics.fct_funnel
      grain_key: session_id
      time_column: session_at
      agg: ratio
      numerator: converted
      denominator: sessions
"""


def test_ratio_binding_requires_kind_rate():
    parser = Parser(_bound_tree(RATIO_BIND, name="cvr", extra="    kind: rate\n"))
    assert parser.get_metric("cvr").bind.numerator == "converted"

    with pytest.raises(ValueError, match="A ratio is a rate"):
        Parser(_bound_tree(RATIO_BIND, name="cvr"))


def test_ratio_without_denominator_is_refused_not_approximated():
    bind = RATIO_BIND.replace("      denominator: sessions\n", "")
    with pytest.raises(ValueError, match="confidently wrong cause"):
        Parser(_bound_tree(bind, name="cvr", extra="    kind: rate\n"))


def test_ratio_may_not_declare_measure():
    bind = RATIO_BIND + "      measure: amount\n"
    with pytest.raises(ValueError, match="not `measure`"):
        Parser(_bound_tree(bind, name="cvr", extra="    kind: rate\n"))


def test_non_ratio_requires_measure():
    bind = SUM_BIND.replace("      measure: amount\n", "")
    with pytest.raises(ValueError, match="needs a `measure`"):
        Parser(_bound_tree(bind))


def test_numerator_on_a_non_ratio_is_rejected():
    bind = SUM_BIND + "      numerator: converted\n"
    with pytest.raises(ValueError, match="only apply to `agg: ratio`"):
        Parser(_bound_tree(bind))


def test_summing_a_rate_is_rejected():
    with pytest.raises(ValueError, match="Summing a rate is"):
        Parser(_bound_tree(SUM_BIND, name="cvr", extra="    kind: rate\n"))


def test_unknown_agg_is_rejected():
    bind = SUM_BIND.replace("agg: sum", "agg: median")
    with pytest.raises(ValueError, match="binding agg must be one of"):
        Parser(_bound_tree(bind))


COUNT_DISTINCT_BIND = """      relation: analytics.fct_sessions
      grain_key: session_id
      time_column: started_at
      agg: count_distinct
      measure: user_id
"""


def test_count_distinct_requires_an_entity_key():
    with pytest.raises(ValueError, match="needs an `entity_key`"):
        Parser(_bound_tree(COUNT_DISTINCT_BIND, name="active_users"))

    bind = COUNT_DISTINCT_BIND + "      entity_key: user_id\n"
    defn = Parser(_bound_tree(bind, name="active_users")).get_metric("active_users")
    assert defn.bind.is_non_additive is True


def test_sliced_stock_requires_an_entity_key_but_trend_only_does_not():
    last = """      relation: analytics.dim_subscriptions
      grain_key: subscription_id
      time_column: as_of
      agg: last
      measure: mrr
"""
    assert Parser(_bound_tree(last, name="mrr")).get_metric("mrr").bind.agg == "last"

    sliced = last + "      dimensions:\n        plan: {column: plan_tier}\n"
    with pytest.raises(ValueError, match="sliced `last` binding needs an"):
        Parser(_bound_tree(sliced, name="mrr"))


def test_join_without_a_key_cannot_be_proven_many_to_one():
    bind = SUM_BIND + "      dimensions:\n        region: {join: dim_customers, column: region}\n"
    with pytest.raises(ValueError, match="cannot be proven many-to-one"):
        Parser(_bound_tree(bind))


def test_join_key_without_a_join_is_rejected():
    bind = SUM_BIND + "      dimensions:\n        region: {key: customer_id, column: region}\n"
    with pytest.raises(ValueError, match="without `join`"):
        Parser(_bound_tree(bind))


def test_join_key_is_not_named_on_because_yaml_would_eat_it():
    # PyYAML is YAML 1.1: a bare `on:` key parses as the boolean True, so an
    # `on` field would silently vanish and leave the join unprovable rather
    # than erroring. This pins the field name against a well-meaning rename.
    import yaml as _yaml

    assert _yaml.safe_load("{on: customer_id}") == {True: "customer_id"}
    assert _yaml.safe_load("{key: customer_id}") == {"key": "customer_id"}


def test_binding_dimension_name_must_be_an_identifier():
    bind = SUM_BIND + '      dimensions:\n        "2region": {column: region}\n'
    with pytest.raises(ValueError, match="must be an identifier"):
        Parser(_bound_tree(bind))


def test_bind_and_legacy_sql_are_mutually_exclusive():
    yaml_content = """
metrics:
  - name: revenue
    source: warehouse.revenue
    sql: "SELECT d AS date, v AS value FROM t"
    bind:
      relation: analytics.fct_orders
      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
"""
    with pytest.raises(ValueError, match="both `sql` and `bind`"):
        Parser(yaml_content)


def test_non_additive_parent_may_not_be_resampled_up_to_a_coarser_child():
    # Summing daily distinct users is not the monthly distinct user count, and
    # `resample_up` sums flows — so the tree must be refused at parse time.
    yaml_content = """
metrics:
  - name: daily_active_users
    source: warehouse.dau
    grain: day
    bind:
      relation: analytics.fct_sessions
      grain_key: session_id
      time_column: started_at
      agg: count_distinct
      measure: user_id
      entity_key: user_id
  - name: revenue
    source: warehouse.revenue
    grain: month
    parents: [daily_active_users]
"""
    with pytest.raises(ValueError, match="not re-aggregable"):
        Parser(yaml_content)


def test_non_additive_parent_at_the_childs_grain_is_fine():
    yaml_content = """
metrics:
  - name: monthly_active_users
    source: warehouse.mau
    grain: month
    bind:
      relation: analytics.fct_sessions
      grain_key: session_id
      time_column: started_at
      agg: count_distinct
      measure: user_id
      entity_key: user_id
  - name: revenue
    source: warehouse.revenue
    grain: month
    parents: [monthly_active_users]
"""
    assert Parser(yaml_content).dag.has_edge("monthly_active_users", "revenue")


def test_trees_without_bindings_are_unchanged():
    parser = Parser(
        """
metrics:
  - name: dau
    source: dbt.metric.dau
"""
    )
    assert parser.get_metric("dau").bind is None

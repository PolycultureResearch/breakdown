import re

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
    """An undeclared `direction` is None, not `up_is_good`.

    The field is a display judgment ("green means improved"), and the default
    used to be indistinguishable from a declaration once `/dag` serialized it —
    so the UI painted a claim nobody had made. Undeclared has to survive
    serialization for a renderer to be able to decline to judge.
    """
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: support_tickets
    source: dbt.metric.support_tickets
    direction: down_is_good
  - name: deploys
    source: dbt.metric.deploys
    direction: up_is_good
"""
    parser = Parser(yaml_content)
    assert parser.get_metric("dau").direction is None
    assert parser.get_metric("dau").model_dump()["direction"] is None
    assert parser.get_metric("support_tickets").direction == "down_is_good"
    # An explicit `up_is_good` is a declaration and stays one.
    assert parser.get_metric("deploys").direction == "up_is_good"


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
    with pytest.raises(ValueError, match="needs a `denominator` on the metric"):
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
    # The weight *is* the denominator (roadmap 1.11b), so it is checked as one
    # — one fact, one error, wherever the author spelled it.
    with pytest.raises(ValueError, match="denominator 'trial_starts', which is not a metric"):
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
    with pytest.raises(Exception, match="mock, local, cloud, dbt, warehouse, none"):
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


def test_a_hand_written_where_is_a_parse_error_naming_the_escape_hatch():
    # `where` is the first import-only field (roadmap 2.17). It passes §4.1's
    # first test easily — a predicate is fetch-shaped, not org-wide semantics —
    # and fails the stop rule outright for a hand author, because `bind.sql`
    # already expresses every filter anyone could write. A shorter spelling of
    # something already expressible is the definition of convenience, which is
    # what the stop rule forbids.
    bind = SUM_BIND + '      where: ["is_food_order = TRUE"]\n'
    with pytest.raises(ValueError, match="populated by the dbt importer"):
        Parser(_bound_tree(bind))


def test_the_where_refusal_points_at_bind_sql():
    bind = SUM_BIND + '      where: ["is_food_order = TRUE"]\n'
    with pytest.raises(ValueError, match=r"sql: SELECT \* FROM analytics.fct_orders WHERE"):
        Parser(_bound_tree(bind))


def test_an_empty_where_is_indistinguishable_from_no_where():
    # Absent and empty mean the same thing, so an author writing `where: []`
    # has written nothing and is refused nothing.
    assert (
        Parser(_bound_tree(SUM_BIND + "      where: []\n")).get_metric("revenue").bind.where == []
    )


def test_the_importer_may_populate_where_even_though_an_author_may_not():
    # The discriminator is structural and needs no cleverness: manifest bindings
    # are constructed directly and never pass through YAML, so the check lives
    # on `MetricDefinition` rather than on `BindingSpec`.
    from breakdown.parser import BindingSpec

    b = BindingSpec(
        relation="t",
        grain_key="k",
        time_column="d",
        agg="sum",
        measure="v",
        where=["region = 'US'"],
    )
    assert b.where == ["region = 'US'"]


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


# --- entity-grain resolution (roadmap 3.8 §4) -------------------------------


def _dau_bind(extra=""):
    return f"""
metrics:
  - name: dau
    source: w.dau
    bind:
      relation: analytics.ev
      grain_key: row_id
      time_column: seen_at
      agg: count_distinct
      measure: user_id
      entity_key: user_id
{extra}"""


def test_entity_grain_parses_and_defaults_its_relation():
    tree = _dau_bind("      entity_grain: {resolve: last}\n")
    bind = Parser(tree).get_metric("dau").bind
    assert bind.entity_grain.resolve == "last"
    assert bind.entity_grain.relation is None  # falls back to the binding's own
    assert bind.resolves_to_entity_grain is True


def test_a_binding_without_entity_grain_does_not_claim_resolution():
    assert Parser(_dau_bind()).get_metric("dau").bind.resolves_to_entity_grain is False


@pytest.mark.parametrize("resolve", ["first", "last", "error"])
def test_the_three_resolutions_are_accepted(resolve):
    tree = _dau_bind(f"      entity_grain: {{resolve: {resolve}}}\n")
    assert Parser(tree).get_metric("dau").bind.entity_grain.resolve == resolve


def test_resolve_has_no_default_because_the_choice_is_a_business_question():
    # `first` and `last` answer different questions — what state did they
    # arrive in vs end in — and guessing is the error class 3.8 removes.
    with pytest.raises(ValueError, match="resolve"):
        Parser(_dau_bind("      entity_grain: {}\n"))


def test_an_unknown_resolution_is_rejected():
    with pytest.raises(ValueError, match="must be one of"):
        Parser(_dau_bind("      entity_grain: {resolve: whatever}\n"))


def test_entity_grain_requires_an_entity_key():
    tree = """
metrics:
  - name: revenue
    source: w.revenue
    bind:
      relation: analytics.orders
      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
      entity_grain: {resolve: last}
"""
    with pytest.raises(ValueError, match="needs an `entity_key`"):
        Parser(tree)


def test_resolving_a_different_entity_than_the_one_counted_is_rejected():
    # Resolving to entity grain makes Σ slices the distinct *entity* count. If
    # the metric counts something else, that sum is not the metric and the
    # reconciliation the resolution promises would be false.
    tree = """
metrics:
  - name: sessions
    source: w.sessions
    bind:
      relation: analytics.ev
      grain_key: row_id
      time_column: seen_at
      agg: count_distinct
      measure: session_id
      entity_key: user_id
      entity_grain: {resolve: last}
"""
    with pytest.raises(ValueError, match="counted column"):
        Parser(tree)


# --- the `tree:` block (roadmap 2.16) --------------------------------------

_GOAL_TREE = """
tree:
  title: "Q3 Pro member growth"
  description: "200 net-new paying Pro members by Sep 30"
  owner: "growth@acme.com"
  period: "2026-Q3"
  goal:
    metric: pro_members_net_new
    target: 200
    deadline: "2026-09-30"

metrics:
  - name: pro_members_net_new
    source: w.pro_members_net_new
"""


def test_tree_block_parses():
    meta = Parser(_GOAL_TREE).config.tree
    assert meta.title == "Q3 Pro member growth"
    assert meta.owner == "growth@acme.com"
    assert meta.period == "2026-Q3"
    assert meta.goal.metric == "pro_members_net_new"
    assert meta.goal.target == 200


def test_tree_block_is_optional():
    """A tree with no `tree:` block is exactly as valid as it was before the
    block existed — there is no migration."""
    tree = """
metrics:
  - name: dau
    source: w.dau
"""
    assert Parser(tree).config.tree is None


def test_every_tree_field_is_optional():
    tree = """
tree:
  title: "The business"

metrics:
  - name: dau
    source: w.dau
"""
    meta = Parser(tree).config.tree
    assert meta.title == "The business"
    assert meta.goal is None and meta.owner is None and meta.period is None


def test_goal_metric_must_exist_in_the_tree():
    tree = """
tree:
  goal: {metric: not_a_metric, target: 10}

metrics:
  - name: dau
    source: w.dau
"""
    with pytest.raises(ValueError, match="not a metric in this tree"):
        Parser(tree)


def test_goal_direction_defaults_from_the_metric():
    tree = """
tree:
  goal: {metric: support_tickets, target: 100}

metrics:
  - name: support_tickets
    source: w.tickets
    direction: down_is_good
"""
    assert Parser(tree).config.tree.goal.direction == "down"


def test_goal_direction_defaults_to_up_when_the_metric_is_silent():
    assert Parser(_GOAL_TREE).config.tree.goal.direction == "up"


def test_goal_direction_disagreeing_with_the_metric_is_an_error():
    tree = """
tree:
  goal: {metric: support_tickets, target: 100, direction: up}

metrics:
  - name: support_tickets
    source: w.tickets
    direction: down_is_good
"""
    with pytest.raises(ValueError, match="disagree about which way is winning"):
        Parser(tree)


def test_goal_on_a_neutral_metric_must_state_its_direction():
    tree = """
tree:
  goal: {metric: headcount, target: 100}

metrics:
  - name: headcount
    source: w.headcount
    direction: neutral
"""
    with pytest.raises(ValueError, match="no goal direction can be inferred"):
        Parser(tree)
    assert Parser(tree.replace("target: 100", "target: 100, direction: up")).config.tree.goal


def test_goal_deadline_must_be_a_date():
    tree = """
tree:
  goal: {metric: dau, target: 10, deadline: "Q3"}

metrics:
  - name: dau
    source: w.dau
"""
    with pytest.raises(ValueError, match="deadline must be a YYYY-MM-DD date"):
        Parser(tree)


# --- name collisions (roadmap C6) -------------------------------------------


DUPLICATE_NAME_TREE = """
metrics:
  - name: ads
    source: w.ads
  - name: email
    source: w.email
  - name: signups
    source: w.signups
    parents: [ads]
    priors:
      ads: {distribution: Normal, params: {mu: 2.0, sigma: 0.1}}
  - name: signups
    source: w.signups_v2
    parents: [email]
"""


def test_duplicate_metric_name_is_rejected():
    with pytest.raises(ValueError) as exc:
        Parser(DUPLICATE_NAME_TREE)
    msg = str(exc.value)
    # The message has to find both definitions in a file the author cannot
    # eyeball: the name, how many there are, where each sits, and what
    # distinguishes them.
    assert "'signups' is defined 2 times" in msg
    assert "positions 3, 4" in msg
    assert "'w.signups'" in msg and "'w.signups_v2'" in msg


def test_duplicate_name_can_no_longer_merge_two_definitions_into_one_node():
    """The C6 reproduction: a tree that parsed clean while
    `list(dag.predecessors('signups'))` returned the union of both
    definitions' parents and the surviving definition declared one.

    `predecessors` is the axis order of `beta`/`beta_raw`, so the declared
    prior on `ads` was being applied to whichever axis happened to land there.
    The union is now unconstructible: nothing gets a DAG at all.
    """
    with pytest.raises(ValueError):
        Parser(DUPLICATE_NAME_TREE)

    # And with the duplicate resolved, the two views of a node's parents agree.
    parser = Parser(
        DUPLICATE_NAME_TREE.replace(
            "name: signups\n    source: w.signups_v2", "name: signups_v2\n    source: w.signups_v2"
        )
    )
    for name in parser.dag.nodes:
        defn = parser.get_metric(name)
        assert list(parser.dag.predecessors(name)) == defn.parents


def test_duplicate_metric_name_fails_before_the_dag_exists():
    # The refusal lives on MetricTreeConfig, so it fires while the YAML is
    # still being validated — before any DAG is built or any series fetched.
    import yaml as _yaml

    from breakdown.parser import MetricTreeConfig

    with pytest.raises(ValueError, match="Duplicate metric name"):
        MetricTreeConfig(**_yaml.safe_load(DUPLICATE_NAME_TREE))


def test_three_definitions_of_one_name_report_every_position():
    tree = """
metrics:
  - name: dau
    source: w.a
  - name: dau
    source: w.b
  - name: dau
    source: w.c
"""
    with pytest.raises(ValueError, match="defined 3 times, at positions 1, 2, 3"):
        Parser(tree)


def test_a_parent_listed_twice_is_rejected():
    # `parents` order is the axis order of the learned coefficients while the
    # DAG holds one edge per parent, so a repeat leaves `defn.parents` one
    # longer than `list(dag.predecessors(name))` and shifts every coefficient
    # read positionally off the definition after it.
    tree = """
metrics:
  - name: ads
    source: w.ads
  - name: email
    source: w.email
  - name: signups
    source: w.signups
    parents: [ads, email, ads]
"""
    with pytest.raises(ValueError, match="lists parent 'ads' 2 times"):
        Parser(tree)


def test_a_metric_that_is_its_own_parent_is_caught_as_a_cycle():
    # Already handled: the self-loop makes the graph non-acyclic, so the
    # existing cycle check refuses it. Pinned so the refusal survives.
    tree = """
metrics:
  - name: dau
    source: w.dau
    parents: [dau]
"""
    with pytest.raises(ValueError, match="contains cycles"):
        Parser(tree)


def test_a_repeated_yaml_key_is_rejected_with_both_lines():
    # PyYAML keeps the last of two identical mapping keys and says nothing —
    # the same silent merge as a duplicate metric name, one level down. Here
    # the mu: 2.0 prior would simply never exist.
    tree = """
metrics:
  - name: ads
    source: w.ads
  - name: signups
    source: w.signups
    parents: [ads]
    priors:
      ads: {distribution: Normal, params: {mu: 2.0}}
      ads: {distribution: Normal, params: {mu: 9.0}}
"""
    with pytest.raises(ValueError) as exc:
        Parser(tree)
    msg = str(exc.value)
    assert "Duplicate key 'ads' at line 10, already set at line 9" in msg


def test_a_repeated_dimension_name_is_rejected():
    tree = """
metrics:
  - name: signups
    source: w.signups
    dimensions:
      region: customer__region
      region: customer__country
"""
    with pytest.raises(ValueError, match="Duplicate key 'region'"):
        Parser(tree)


def test_yaml_11_makes_on_and_true_the_same_key_and_that_is_caught():
    # The `on:` trap again (see test_join_key_is_not_named_on_because_yaml_
    # would_eat_it): YAML 1.1 resolves a bare `on` to True, so `on:` and
    # `true:` in one mapping are one key. The duplicate check compares
    # constructed keys, so it sees the collision and says why.
    bind = (
        SUM_BIND + "      dimensions:\n        region:\n          column: region\n"
        "          join: dim_customers\n          on: customer_id\n          true: other_id\n"
    )
    with pytest.raises(ValueError) as exc:
        Parser(_bound_tree(bind))
    msg = str(exc.value)
    assert "are one key" in msg and "YAML 1.1" in msg


def test_yaml_anchors_and_merge_keys_still_load():
    # `<<` is legitimately repeatable, so the duplicate-key check must skip it.
    tree = """
defaults: &defaults
  grain: week
  kind: flow

metrics:
  - name: ads
    source: w.ads
    <<: *defaults
  - name: signups
    source: w.signups
    <<: *defaults
    parents: [ads]
"""
    parser = Parser(tree)
    assert parser.get_metric("signups").grain == "week"


def test_a_repeated_seasonality_name_is_rejected():
    # Each entry names its own Fourier coefficients (`sin_<name>_h1`), so two
    # entries sharing a name collide inside the model and the fit dies with a
    # variable-name error that never mentions the tree.
    tree = """
metrics:
  - name: dau
    source: w.dau
    seasonality:
      - {period: 7, name: weekly}
      - {period: 365, name: weekly}
"""
    with pytest.raises(ValueError, match="seasonality name 'weekly' more than once"):
        Parser(tree)


# --- the exact-Shapley parent cap, at parse time ----------------------------


def _wide_formula_tree(n: int) -> str:
    parents = [f"p{i}" for i in range(n)]
    lines = "".join(f"  - name: {p}\n    source: w.{p}\n" for p in parents)
    return (
        "metrics:\n"
        + lines
        + "  - name: total\n    source: w.total\n"
        + f'    formula: "{" + ".join(parents)}"\n'
        + f"    parents: [{', '.join(parents)}]\n"
    )


def test_a_formula_node_above_the_shapley_cap_is_refused_at_parse_time():
    # `compute_shapley` refuses it too — that is the chokepoint a direct
    # library caller cannot walk around — but an author should hear about a
    # 13-parent node when the tree loads, not five minutes into an RCA.
    from breakdown.engine.model import MAX_SHAPLEY_PARENTS

    with pytest.raises(ValueError) as exc:
        Parser(_wide_formula_tree(MAX_SHAPLEY_PARENTS + 1))
    msg = str(exc.value)
    assert "Formula node 'total' has too many parents" in msg
    assert f"at most {MAX_SHAPLEY_PARENTS} are supported" in msg


def test_a_formula_node_at_the_shapley_cap_parses():
    from breakdown.engine.model import MAX_SHAPLEY_PARENTS

    parser = Parser(_wide_formula_tree(MAX_SHAPLEY_PARENTS))
    assert len(list(parser.dag.predecessors("total"))) == MAX_SHAPLEY_PARENTS


def test_the_cap_is_only_for_formula_nodes():
    # A probabilistic node's parents are regressors, not coalition players:
    # nothing enumerates 2^n over them.
    from breakdown.engine.model import MAX_SHAPLEY_PARENTS

    n = MAX_SHAPLEY_PARENTS + 1
    parents = [f"p{i}" for i in range(n)]
    tree = (
        "metrics:\n"
        + "".join(f"  - name: {p}\n    source: w.{p}\n" for p in parents)
        + f"  - name: total\n    source: w.total\n    parents: [{', '.join(parents)}]\n"
    )
    assert len(list(Parser(tree).dag.predecessors("total"))) == n


# --- `source` is optional on a formula node (roadmap 1.11a) ------------------


def _derived_tree() -> str:
    return """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: sessions
    source: dbt.metric.sessions
  - name: conversion_rate
    kind: rate
    formula: "orders / sessions"
    parents: [orders, sessions]
"""


def test_a_formula_node_may_omit_its_source_and_is_marked_derived():
    parser = Parser(_derived_tree())
    defn = parser.dag.nodes["conversion_rate"]["definition"]
    assert defn.source is None
    assert defn.derived is True
    # It has to survive serialization, or no renderer can act on it — the same
    # reason `direction` is Optional (see test_project_invariants).
    assert defn.model_dump()["derived"] is True
    # And a fetched node is not derived, whatever else it declares.
    assert parser.dag.nodes["orders"]["definition"].derived is False


def test_a_node_with_neither_source_nor_formula_is_refused():
    with pytest.raises(ValueError, match="no `source` and no `formula`"):
        Parser("metrics:\n  - name: mystery\n")


@pytest.mark.parametrize(
    "extra",
    [
        "    dimensions:\n      region: customer__region\n",
        "    lags: { orders: 2 }\n",
        "    sql: SELECT 1\n",
    ],
)
def test_a_derived_node_refuses_fetch_instructions_by_name(extra):
    """Each refusal names the field. Silently ignoring a `sql:` on a node
    nothing fetches leaves an author with the wrong model of what they wrote."""
    tree = _derived_tree().replace(
        "    parents: [orders, sessions]\n",
        "    parents: [orders, sessions]\n" + extra,
    )
    with pytest.raises(ValueError, match="but no `source`"):
        Parser(tree)


# --- a rate declares its denominator (roadmap 1.11b) -------------------------


def test_denominator_defaults_from_a_simple_ratio_formula():
    parser = Parser(_derived_tree())
    assert parser.dag.nodes["conversion_rate"]["definition"].denominator == "sessions"
    assert parser.rates_denominator_unanswered == []
    assert parser.rates_denominator_none == {}


def test_denominator_is_promoted_from_a_dimension_weight():
    """The near-miss 1.11b is built on: `churn_arpu` already declared its
    denominator, filed under slicing where the fetch path could not see it."""
    parser = Parser("""
metrics:
  - name: churned_subscriptions
    source: dbt.metric.churned_subscriptions
  - name: churn_arpu
    source: dbt.metric.churn_arpu
    kind: rate
    dimensions:
      plan: { source: mrr_movement__plan, weight: churned_subscriptions }
""")
    assert parser.dag.nodes["churn_arpu"]["definition"].denominator == "churned_subscriptions"


def test_a_dimension_weight_may_not_contradict_the_denominator():
    with pytest.raises(ValueError, match="They are the same fact"):
        Parser("""
metrics:
  - name: a
    source: dbt.metric.a
  - name: b
    source: dbt.metric.b
  - name: r
    source: dbt.metric.r
    kind: rate
    denominator: a
    dimensions:
      plan: { source: p, weight: b }
""")


def test_the_node_level_denominator_flows_down_into_dimension_weights():
    parser = Parser("""
metrics:
  - name: subs
    source: dbt.metric.subs
  - name: arpu
    source: dbt.metric.arpu
    kind: rate
    denominator: subs
    dimensions:
      plan: p
      country: c
""")
    defn = parser.dag.nodes["arpu"]["definition"]
    assert {d.weight for d in defn.dimensions.values()} == {"subs"}


def test_a_rate_without_a_denominator_warns_rather_than_failing(caplog):
    """43 of this repo's 52 rate metrics declare one nowhere, and no name tells
    you what a rate is over — so this reports, it does not refuse."""
    import logging

    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        parser = Parser("""
metrics:
  - name: mystery_rate
    source: dbt.metric.mystery_rate
    kind: rate
""")
    assert parser.rates_denominator_unanswered == ["mystery_rate"]
    assert parser.rates_denominator_none == {}
    assert "mystery_rate" in caplog.text and "denominator" in caplog.text


# --- ...and the third state: asked and answered (roadmap 1.11) ----------------


def _rate_tree(field_line: str = "") -> str:
    return f"""
metrics:
  - name: base
    source: dbt.metric.base
  - name: page_speed
    source: dbt.metric.page_speed
    kind: rate
{field_line}"""


def test_a_rate_may_declare_that_it_has_no_denominator(caplog):
    """The third state, and the whole point of the field: `page_speed` is a
    median, so no pair of series makes it — that is a finding, not a to-do."""
    import logging

    reason = "a median — not Σnum / Σden for any pair of series"
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        parser = Parser(_rate_tree(f'    no_denominator: "{reason}"\n'))
    defn = parser.dag.nodes["page_speed"]["definition"]
    assert defn.denominator is None and defn.no_denominator == reason
    # Answered, so it is not in the lint — and it is in the inventory of
    # answers, with the reason, which is what `doctor` reports.
    assert parser.rates_denominator_unanswered == []
    assert parser.rates_denominator_none == {"page_speed": reason}
    # And nothing warns: a warning on a settled question is how a log stops
    # being read.
    assert "declare no `denominator`" not in caplog.text


def test_the_answer_survives_serialization():
    """`/dag` serializes with `model_dump()`, which is where `model_fields_set`
    would have died — and where C21's `direction` default reached the browser
    indistinguishable from a declaration. Absent stays absent, answered stays
    answered, and both are readable by a consumer that never sees the YAML."""
    parser = Parser(_rate_tree('    no_denominator: "a median"\n'))
    answered = parser.dag.nodes["page_speed"]["definition"].model_dump()
    assert answered["denominator"] is None and answered["no_denominator"] == "a median"
    unanswered = Parser(_rate_tree()).dag.nodes["page_speed"]["definition"].model_dump()
    assert unanswered["denominator"] is None and unanswered["no_denominator"] is None


@pytest.mark.parametrize(
    "field_line,message",
    [
        # Both answers to one question.
        ('    denominator: base\n    no_denominator: "a median"\n', "opposite answers"),
        # An assertion with no evidence behind it is not worth more than silence.
        ('    no_denominator: ""\n', "empty `no_denominator`"),
        ('    no_denominator: "   "\n', "empty `no_denominator`"),
    ],
)
def test_a_contradictory_or_empty_answer_is_refused(field_line, message):
    with pytest.raises(ValueError, match=message):
        Parser(_rate_tree(field_line))


def test_no_denominator_is_refused_on_a_non_rate():
    """Refused exactly as `denominator` is: a flow sums and a stock takes its
    last value, so neither has a denominator *or* a stated absence of one."""
    with pytest.raises(ValueError, match="declares `no_denominator` but `kind: flow`"):
        Parser("""
metrics:
  - name: orders
    source: dbt.metric.orders
    no_denominator: "orders is not a rate"
""")


@pytest.mark.parametrize(
    "tree,source",
    [
        (
            """
metrics:
  - name: sessions
    source: dbt.metric.sessions
  - name: orders
    source: dbt.metric.orders
  - name: cvr
    kind: rate
    formula: "orders / sessions"
    parents: [orders, sessions]
    no_denominator: "there isn't one"
""",
            "the `formula: orders / sessions`",
        ),
        (
            """
metrics:
  - name: subs
    source: dbt.metric.subs
  - name: arpu
    source: dbt.metric.arpu
    kind: rate
    no_denominator: "there isn't one"
    dimensions:
      plan: { source: p, weight: subs }
""",
            "a `dimensions[].weight`",
        ),
    ],
)
def test_an_answer_contradicted_by_the_tree_is_refused(tree, source):
    """The derivation sources are evidence; `no_denominator` is a claim about
    the evidence. Believing the claim would compute the disclosed fallback while
    the tree holds everything needed for the real aggregate; adopting the
    evidence would overrule the author. Exactly one of them is wrong and only
    the author knows which, so the error names the source."""
    with pytest.raises(ValueError, match="declares `no_denominator`"):
        Parser(tree)
    with pytest.raises(ValueError, match=re.escape(source)):
        Parser(tree)


def test_a_rate_that_has_no_denominator_cannot_be_sliced():
    """A sliced rate blends its slices by the denominator's shares. A rate that
    genuinely has none has nothing to blend by — so the refusal that already
    covers "declared nowhere" covers this too, and says so rather than
    repeating advice the author has already answered."""
    with pytest.raises(ValueError, match="does not stand in for one"):
        Parser("""
metrics:
  - name: page_speed
    source: dbt.metric.page_speed
    kind: rate
    no_denominator: "a median"
    dimensions:
      region: customer__region
""")


@pytest.mark.parametrize(
    "denominator,message",
    [
        ("nope", "not a metric in the tree"),
        ("other_rate", "itself `kind: rate`"),
    ],
)
def test_an_unusable_denominator_is_refused(denominator, message):
    with pytest.raises(ValueError, match=message):
        Parser(f"""
metrics:
  - name: other_rate
    source: dbt.metric.other_rate
    kind: rate
    denominator: base
  - name: base
    source: dbt.metric.base
  - name: r
    source: dbt.metric.r
    kind: rate
    denominator: {denominator}
""")


def test_a_denominator_may_not_be_coarser_than_its_rate():
    with pytest.raises(ValueError, match="at coarser grain"):
        Parser("""
metrics:
  - name: monthly_base
    source: dbt.metric.monthly_base
    grain: month
  - name: weekly_rate
    source: dbt.metric.weekly_rate
    grain: week
    kind: rate
    denominator: monthly_base
""")


def test_a_denominator_on_a_non_rate_is_refused():
    with pytest.raises(ValueError, match=r"A denominator says how a \*rate\*"):
        Parser("""
metrics:
  - name: base
    source: dbt.metric.base
  - name: total
    source: dbt.metric.total
    denominator: base
""")


def test_slice_weight_grain_mismatch_fails_at_parse_not_at_click(caplog):
    """C12: the grain agreement between a rate and its slicing weight used to
    be checked only in the API's slice handler — a tree parsed clean, booted
    clean, and 422'd on the first *slice by* click. Both definitions are in
    hand at parse time, so that is when the author hears about it."""
    yaml_content = """
metrics:
  - name: subs
    source: dbt.metric.subs
    grain: day
  - name: churn_rate
    source: dbt.metric.churn_rate
    kind: rate
    grain: month
    denominator: subs
    dimensions:
      plan: { source: subscription__plan }
"""
    with pytest.raises(ValueError, match="weight 'subs' at grain 'day'"):
        Parser(yaml_content)


def test_finer_nesting_denominator_stays_legal_without_dimensions():
    """The other half of the C12 rule: `_validate_denominators` accepts a
    denominator at a finer, nesting grain because the window aggregate
    resamples it up. Only declaring `dimensions` — which is what makes the
    non-resampling slice path reachable — turns the mismatch into an error."""
    yaml_content = """
metrics:
  - name: subs
    source: dbt.metric.subs
    grain: day
  - name: churn_rate
    source: dbt.metric.churn_rate
    kind: rate
    grain: month
    denominator: subs
"""
    assert Parser(yaml_content).get_metric("churn_rate").denominator == "subs"


def test_share_is_refused_on_a_non_rate():
    """Refused exactly as `denominator` is, and for a plainer reason: a flow
    sums over time and a stock takes its last value, and neither is bounded by
    the whole it would be a share of."""
    with pytest.raises(ValueError, match="declares `share` but `kind: flow`"):
        Parser("""
metrics:
  - name: orders
    source: dbt.metric.orders
    share: true
""")


def test_share_is_a_separate_declaration_from_the_denominator():
    """The two answer different questions, and C26 was filed believing they
    answered the same one.

    `denominator` says how a window's value is formed (Σnum / Σden); `share`
    says the value is a proportion and cannot leave [0, 1]. Every combination
    is legal because every combination occurs: a share with a denominator
    (`visit_signup_rate`), a share without one (`member_activity_rate`, whose
    denominator is member-days and is deliberately not a tree metric), and a
    denominator without a share (`average_order_value`, ~$182 an order).
    """
    dag = Parser("""
metrics:
  - name: sessions
    source: dbt.metric.sessions
  - name: signup_rate
    source: dbt.metric.signup_rate
    kind: rate
    denominator: sessions
    share: true
  - name: activity_rate
    source: dbt.metric.activity_rate
    kind: rate
    no_denominator: "the denominator is member-days, which is not a tree metric"
    share: true
  - name: average_order_value
    source: dbt.metric.aov
    kind: rate
    denominator: sessions
""").dag
    assert dag.nodes["signup_rate"]["definition"].share is True
    assert dag.nodes["activity_rate"]["definition"].share is True
    # Undeclared stays undeclared through serialization — C21's lesson, and the
    # reason the field is Optional rather than defaulting to False.
    aov = dag.nodes["average_order_value"]["definition"]
    assert aov.share is None
    assert aov.model_dump()["share"] is None

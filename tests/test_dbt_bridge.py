"""The dbt semantic-manifest bridge (roadmap 2.10).

Fixtures are built inline rather than checked in as JSON: the interesting axis
is the *shape* of a manifest (classic measure layer vs. new-spec inline
aggregation), and a builder makes that difference legible instead of burying it
in two near-identical blobs.

These parse through breakdown's own models (`dbt_manifest`). That the models
agree with `metricflow_semantic_interfaces` on every field the bridge consumes
is asserted separately, in `test_dbt_manifest.py`.
"""

import json

import pytest

from breakdown.dbt_bridge import bridge_project, load_manifest, translate
from breakdown.dbt_manifest import parse_manifest


def _model(
    *,
    name="orders",
    entities=({"name": "order", "type": "primary", "expr": "order_id"},),
    dimensions=(
        {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
        {"name": "region", "type": "categorical"},
    ),
    measures=({"name": "revenue", "agg": "sum", "expr": "amount"},),
    agg_time_dimension="ordered_at",
):
    return {
        "name": name,
        "node_relation": {
            "alias": f"fct_{name}",
            "schema_name": "marts",
            "database": "analytics",
            "relation_name": "unused",
        },
        "defaults": {"agg_time_dimension": agg_time_dimension},
        "entities": list(entities),
        "dimensions": list(dimensions),
        "measures": list(measures),
    }


def _manifest(models, metrics):
    return parse_manifest(
        {
            "semantic_models": list(models),
            "metrics": list(metrics),
            "project_configuration": {
                "time_spine_table_configurations": [],
                "metadata": None,
            },
        }
    )


def _classic(name="revenue", measure="revenue"):
    """A simple metric in the classic spec: the measure carries the agg."""
    return {"name": name, "type": "simple", "type_params": {"measure": {"name": measure}}}


def _new_spec(name, *, model="orders", agg="sum", expr="amount", agg_time="ordered_at", **params):
    """A simple metric in dbt's new metrics spec: the agg is on the metric and
    the measure layer is gone."""
    return {
        "name": name,
        "type": "simple",
        "type_params": {
            "expr": expr,
            "metric_aggregation_params": {
                "semantic_model": model,
                "agg": agg,
                "agg_time_dimension": agg_time,
                **params,
            },
        },
    }


# --- both manifest shapes ---------------------------------------------------


def test_classic_spec_measure_layer_translates():
    r = translate(_manifest([_model()], [_classic()]))
    b = r.bindings["revenue"]
    assert b.relation == "analytics.marts.fct_orders"
    assert (b.agg, b.measure) == ("sum", "amount")
    assert b.grain_key == "order_id"
    assert b.time_column == "ordered_at"
    assert list(b.dimensions) == ["region"]
    assert (r.grains["revenue"], r.kinds["revenue"]) == ("day", "flow")


def test_new_spec_inline_aggregation_translates_identically():
    # The shape dbt's new metrics spec and Fusion emit. Getting this wrong is
    # the failure the whole MSI-not-DSI decision exists to prevent: DSI parses
    # this manifest, validates with zero errors, and returns no aggregation.
    models = [_model(measures=())]
    r = translate(_manifest(models, [_new_spec("revenue")]))
    b = r.bindings["revenue"]
    assert (b.agg, b.measure) == ("sum", "amount")
    assert b.relation == "analytics.marts.fct_orders"
    assert b.time_column == "ordered_at"


def test_metric_resolving_to_neither_shape_is_reported_not_defaulted():
    metric = {"name": "revenue", "type": "simple", "type_params": {"expr": "amount"}}
    r = translate(_manifest([_model(measures=())], [metric]))
    assert not r.bindings
    assert "neither a `measure`" in r.skipped[0].reason


# --- aggregation mapping ----------------------------------------------------


@pytest.mark.parametrize(
    "mf_agg,expected",
    [
        ("sum", "sum"),
        ("count", "count"),
        ("count_distinct", "count_distinct"),
        ("average", "average"),
        ("sum_boolean", "sum"),
    ],
)
def test_supported_aggregations_map(mf_agg, expected):
    models = [_model(measures=({"name": "m", "agg": mf_agg, "expr": "amount"},))]
    r = translate(_manifest(models, [_classic("m", "m")]))
    assert r.bindings["m"].agg == expected


@pytest.mark.parametrize("mf_agg", ["min", "max", "median", "percentile"])
def test_aggregations_without_an_additive_decomposition_are_skipped(mf_agg):
    models = [_model(measures=({"name": "m", "agg": mf_agg, "expr": "amount"},))]
    r = translate(_manifest(models, [_classic("m", "m")]))
    assert not r.bindings
    assert "no additive decomposition" in r.skipped[0].reason


def test_average_is_a_rate_not_a_flow():
    # An average cannot be re-aggregated over time; declaring it a flow would
    # license resample_up to sum it.
    models = [_model(measures=({"name": "aov", "agg": "average", "expr": "amount"},))]
    r = translate(_manifest(models, [_classic("aov", "aov")]))
    assert r.kinds["aov"] == "rate"


def test_count_distinct_gets_the_entity_key_it_requires():
    models = [_model(measures=({"name": "dau", "agg": "count_distinct", "expr": "user_id"},))]
    r = translate(_manifest(models, [_classic("dau", "dau")]))
    assert r.bindings["dau"].entity_key == "user_id"
    assert r.bindings["dau"].is_non_additive is True


# --- refusals that protect a number -----------------------------------------


def test_non_additive_dimension_is_skipped_because_it_depends_on_query_grain():
    r = translate(
        _manifest(
            [_model(measures=())],
            [
                _new_spec(
                    "balance",
                    agg="sum",
                    expr="amount",
                    non_additive_dimension={"name": "ordered_at", "window_choice": "max"},
                )
            ],
        )
    )
    assert not r.bindings
    assert "per grain window" in r.skipped[0].reason


def _filter(*predicates):
    """dbt's canonical serialisation of a `filter:`."""
    return {"where_filters": [{"where_sql_template": p} for p in predicates]}


def test_filtered_simple_metric_is_refused_rather_than_served_unfiltered():
    # The C15 failure: `BindingSpec` has no filter clause, so translating this
    # metric binds the *whole* relation under the governed metric's name — a
    # larger, plausible number that `doctor` cannot catch, because the grain
    # assertion still sees one row per grain key.
    metric = _classic()
    metric["filter"] = _filter("{{ Dimension('order__is_paid') }}")
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    reason = r.skipped[0].reason
    assert "`filter`" in reason
    assert "{{ Dimension('order__is_paid') }}" in reason
    assert "unfiltered measure" in reason


def test_a_filter_on_the_measure_input_is_refused_the_same_way():
    # Classic spec puts the filter on the measure input; dbt's flattening
    # transform lifts it to the metric. Manifests exist in both states.
    metric = _classic()
    metric["type_params"]["measure"]["filter"] = _filter("amount > 0")
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    assert "its measure input declares a `filter` (amount > 0)" in r.skipped[0].reason


@pytest.mark.parametrize(
    "shape",
    [
        "amount > 0",
        {"where_sql_template": "amount > 0"},
        ["amount > 0"],
        {"where_filters": [{"where_sql_template": "amount > 0"}]},
    ],
)
def test_every_serialisation_a_filter_has_ever_had_is_recognised(shape):
    # MetricFlow has accepted all four and normalises them on parse; manifests
    # written by older dbt versions still carry the legacy ones. A shape we did
    # not recognise would parse as *no filter*, which is the whole failure.
    metric = _classic()
    metric["filter"] = shape
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    assert "declares a `filter` (amount > 0)" in r.skipped[0].reason


def test_an_empty_filter_intersection_is_not_a_filter():
    # A pydantic model is truthy whatever it holds, so a truthiness check here
    # would refuse metrics that have no filter at all.
    metric = _classic()
    metric["filter"] = {"where_filters": []}
    r = translate(_manifest([_model()], [metric]))
    assert "revenue" in r.bindings and not r.skipped


@pytest.mark.parametrize("fill", [0, 5])
def test_fill_nulls_with_is_refused_including_the_falsy_zero(fill):
    # `fill_nulls_with: 0` is both the commonest value and falsy: a truthiness
    # check would let the most common case straight through.
    r = translate(_manifest([_model(measures=())], [_new_spec("revenue")]))
    assert r.bindings  # same metric without the flag translates
    metric = _new_spec("revenue")
    metric["type_params"]["fill_nulls_with"] = fill
    r = translate(_manifest([_model(measures=())], [metric]))
    assert not r.bindings
    assert f"`fill_nulls_with: {fill}`" in r.skipped[0].reason
    assert "gap-fills from the node's `kind`" in r.skipped[0].reason


def test_fill_nulls_with_on_the_measure_input_is_refused_too():
    metric = _classic()
    metric["type_params"]["measure"]["fill_nulls_with"] = 0
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    assert "its measure input declares `fill_nulls_with: 0`" in r.skipped[0].reason


def test_join_to_timespine_is_refused_because_the_empty_periods_are_the_analysis():
    metric = _classic()
    metric["type_params"]["measure"]["join_to_timespine"] = True
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    assert "`join_to_timespine`" in r.skipped[0].reason
    assert "time spine" in r.skipped[0].reason


def test_a_measure_alias_is_not_a_reason_to_refuse_a_simple_metric():
    # `alias` renames the column after aggregation; unlike its neighbours on
    # the same object it changes no number, so refusing on it would cost real
    # metrics for nothing. (A derived metric's input alias is still refused —
    # there the name is what the expression references.)
    metric = _classic()
    metric["type_params"]["measure"]["alias"] = "rev"
    r = translate(_manifest([_model()], [metric]))
    assert r.bindings["revenue"].measure == "amount"


def test_model_without_a_primary_entity_cannot_have_its_grain_asserted():
    models = [_model(entities=({"name": "cust", "type": "foreign", "expr": "customer_id"},))]
    r = translate(_manifest(models, [_classic()]))
    assert not r.bindings
    assert "no primary entity" in r.skipped[0].reason


def test_measure_with_no_time_axis_is_skipped():
    models = [_model(agg_time_dimension=None)]
    r = translate(_manifest(models, [_classic()]))
    assert not r.bindings
    assert "no agg_time_dimension" in r.skipped[0].reason


def test_granularity_coarser_than_month_has_no_breakdown_grain():
    models = [
        _model(
            dimensions=(
                {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "year"}},
            )
        )
    ]
    r = translate(_manifest(models, [_classic()]))
    assert not r.bindings
    assert "coarser than breakdown's coarsest grain" in r.skipped[0].reason


def test_sub_daily_granularity_floors_to_day():
    models = [
        _model(
            dimensions=(
                {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "hour"}},
            )
        )
    ]
    r = translate(_manifest(models, [_classic()]))
    assert r.grains["revenue"] == "day"


@pytest.mark.parametrize(
    "mtype,fragment",
    [
        ("cumulative", "trailing window"),
        ("conversion", "last-touch attribution"),
    ],
)
def test_metric_types_needing_joins_we_do_not_do_are_named_not_approximated(mtype, fragment):
    metric = {"name": "m", "type": mtype, "type_params": {"measure": {"name": "revenue"}}}
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings
    assert fragment in r.skipped[0].reason


def test_private_migration_metrics_are_hidden_entirely():
    # `is_private` sits on type_params, not on the metric: reading it off the
    # metric would silently never match and leak migration scaffolding into
    # every tree scaffolded from a new-spec project.
    metric = _classic()
    metric["type_params"]["is_private"] = True
    r = translate(_manifest([_model()], [metric]))
    assert not r.bindings and not r.skipped


# --- ratio and derived become formula edges, not bindings -------------------


def _ratio(name, num, den):
    return {
        "name": name,
        "type": "ratio",
        "type_params": {"numerator": {"name": num}, "denominator": {"name": den}},
    }


def test_ratio_becomes_a_formula_edge_over_two_bound_metrics():
    # More faithful than one `agg: ratio` binding, and it lands numerator and
    # denominator as separate nodes with their own bindings — which is exactly
    # the "fetch them separately" requirement decomposition needs.
    models = [
        _model(
            measures=(
                {"name": "signups", "agg": "sum", "expr": "1"},
                {"name": "sessions", "agg": "sum", "expr": "1"},
            )
        )
    ]
    metrics = [
        _classic("signups", "signups"),
        _classic("sessions", "sessions"),
        _ratio("signup_rate", "signups", "sessions"),
    ]
    r = translate(_manifest(models, metrics))
    assert set(r.bindings) == {"signups", "sessions"}
    f = r.formulas["signup_rate"]
    assert (f.formula, f.parents) == ("signups / sessions", ("signups", "sessions"))
    assert r.kinds["signup_rate"] == "rate"


@pytest.mark.parametrize("side", ["numerator", "denominator"])
def test_ratio_with_a_filtered_side_is_refused(side):
    # `signups (US only) / sessions` becomes the formula `signups / sessions`,
    # which references the metrics *by name* and so carries no scope: the edge
    # would silently be over the unfiltered signups.
    models = [
        _model(
            measures=(
                {"name": "signups", "agg": "sum", "expr": "1"},
                {"name": "sessions", "agg": "sum", "expr": "1"},
            )
        )
    ]
    ratio = _ratio("signup_rate", "signups", "sessions")
    ratio["type_params"][side]["filter"] = _filter("{{ Dimension('order__region') }} = 'US'")
    r = translate(_manifest(models, [ratio]))
    assert not r.formulas
    skipped = {s.name: s.reason for s in r.skipped}
    assert f"its {side} declares a `filter`" in skipped["signup_rate"]
    assert "'US'" in skipped["signup_rate"]


def test_a_ratio_metrics_own_filter_is_refused():
    ratio = _ratio("signup_rate", "signups", "sessions")
    ratio["filter"] = _filter("region = 'US'")
    r = translate(_manifest([_model()], [ratio]))
    assert not r.formulas
    assert "declares a `filter` (region = 'US')" in r.skipped[0].reason


def test_derived_metric_with_a_filtered_input_is_refused():
    metric = {
        "name": "arr",
        "type": "derived",
        "type_params": {
            "expr": "mrr * 12",
            "metrics": [{"name": "mrr", "filter": _filter("plan = 'pro'")}],
        },
    }
    r = translate(_manifest([_model()], [metric]))
    assert not r.formulas
    assert "its input metric 'mrr' declares a `filter` (plan = 'pro')" in r.skipped[0].reason


def test_derived_metric_becomes_a_formula_edge():
    metric = {
        "name": "arr",
        "type": "derived",
        "type_params": {"expr": "mrr * 12", "metrics": [{"name": "mrr"}]},
    }
    r = translate(_manifest([_model()], [metric]))
    assert r.formulas["arr"].formula == "mrr * 12"
    # `mrr * 12` carries mrr's kind, which the manifest does not state — so the
    # bridge declines to guess rather than forbidding or licensing resampling.
    assert "arr" not in r.kinds


def test_derived_sql_that_is_not_a_breakdown_formula_is_skipped_by_name():
    # Real example from a dbt project: breakdown formulas are arithmetic over
    # metric names with no function calls, so the null guard cannot be carried
    # across — and dropping it would change behaviour at zero.
    metric = {
        "name": "arpu",
        "type": "derived",
        "type_params": {
            "expr": "mrr / nullif(subs, 0)",
            "metrics": [{"name": "mrr"}, {"name": "subs"}],
        },
    }
    r = translate(_manifest([_model()], [metric]))
    assert not r.formulas
    assert "not expressible as a breakdown formula" in r.skipped[0].reason


def test_offset_inputs_are_skipped_rather_than_silently_unshifted():
    metric = {
        "name": "mom",
        "type": "derived",
        "type_params": {
            "expr": "mrr / prev",
            "metrics": [
                {"name": "mrr"},
                {"name": "prev", "offset_window": {"count": 1, "granularity": "month"}},
            ],
        },
    }
    r = translate(_manifest([_model()], [metric]))
    assert not r.formulas
    assert "offsets an input in time" in r.skipped[0].reason


# --- loading ----------------------------------------------------------------


def test_missing_manifest_names_dbt_parse_and_the_fusion_caveat(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run `dbt parse`"):
        bridge_project(str(tmp_path))


def test_a_manifest_file_may_be_given_directly(tmp_path):
    path = tmp_path / "semantic_manifest.json"
    path.write_text(
        json.dumps(
            {
                "semantic_models": [_model()],
                "metrics": [_classic()],
                "project_configuration": {
                    "time_spine_table_configurations": [],
                    "metadata": None,
                },
            }
        )
    )
    assert load_manifest(str(path)).metrics[0].name == "revenue"
    assert "revenue" in bridge_project(str(path)).bindings


def test_bindings_satisfy_the_binding_contract():
    # The bridge's output has to pass the same validation a hand-written
    # binding does — that is the point of emitting BindingSpec rather than a
    # bag of strings.
    models = [_model(measures=({"name": "dau", "agg": "count_distinct", "expr": "user_id"},))]
    b = translate(_manifest(models, [_classic("dau", "dau")])).bindings["dau"]
    assert b.model_dump()["entity_key"] == "user_id"
    assert b.relation and b.sql is None

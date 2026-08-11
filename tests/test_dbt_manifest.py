"""breakdown's manifest models, checked against dbt's own.

`dbt_manifest` exists so the `dbt-bridge` extra needs nothing from dbt Labs at
runtime — `metricflow` was twelve transitive packages and a `<3.15` Python
ceiling for one `parse_obj` call. The cost of that is owning a schema somebody
else evolves, and this file is what makes the trade honest: when `metricflow` is
installed (it stays in the dev group precisely for this), every field the bridge
consumes is parsed both ways and asserted equal.

If dbt changes the artifact, these fail. That is the intended failure mode —
drift surfaces in CI rather than as a wrong number in somebody's tree.
"""

import json

import pytest

from breakdown.dbt_bridge import translate
from breakdown.dbt_manifest import parse_manifest

msi = pytest.importorskip(
    "metricflow_semantic_interfaces",
    reason="the oracle needs metricflow (dev group); breakdown itself does not",
)

from metricflow_semantic_interfaces.implementations.semantic_manifest import (  # noqa: E402
    PydanticSemanticManifest,
)

# Both manifest shapes, and the constructs the bridge actually reads. Anything
# added to the bridge's field list belongs here too.
CLASSIC = {
    "semantic_models": [
        {
            "name": "orders",
            "node_relation": {
                "alias": "fct_orders",
                "schema_name": "marts",
                "database": "analytics",
                "relation_name": '"analytics"."marts"."fct_orders"',
            },
            "defaults": {"agg_time_dimension": "ordered_at"},
            "entities": [
                {"name": "order", "type": "primary", "expr": "order_id"},
                {"name": "customer", "type": "foreign", "expr": "customer_id"},
            ],
            "dimensions": [
                {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
                {"name": "region", "type": "categorical"},
                {"name": "is_paid", "type": "categorical", "expr": "paid_at IS NOT NULL"},
            ],
            "measures": [
                {"name": "revenue", "agg": "sum", "expr": "amount"},
                {"name": "orders", "agg": "count", "expr": "1", "agg_time_dimension": "ordered_at"},
                {"name": "buyers", "agg": "count_distinct", "expr": "customer_id"},
            ],
        }
    ],
    "metrics": [
        {"name": "revenue", "type": "simple", "type_params": {"measure": {"name": "revenue"}}},
        {
            "name": "conv",
            "type": "ratio",
            "type_params": {"numerator": {"name": "revenue"}, "denominator": {"name": "orders"}},
        },
        {
            "name": "arr",
            "type": "derived",
            "type_params": {"expr": "revenue * 12", "metrics": [{"name": "revenue"}]},
        },
        {
            "name": "mom",
            "type": "derived",
            "type_params": {
                "expr": "a / b",
                "metrics": [
                    {"name": "a"},
                    {"name": "b", "offset_window": {"count": 1, "granularity": "month"}},
                ],
            },
        },
        {"name": "wau", "type": "cumulative", "type_params": {"measure": {"name": "revenue"}}},
    ],
    "project_configuration": {"time_spine_table_configurations": [], "metadata": None},
}

NEW_SPEC = {
    "semantic_models": [
        {
            "name": "mrr",
            "node_relation": {
                "alias": "fct_mrr",
                "schema_name": "dbt_x",
                "database": "warehouse",
                "relation_name": "`warehouse`.`dbt_x`.`fct_mrr`",
            },
            "defaults": None,
            "entities": [
                {
                    "name": "sub",
                    "type": "primary",
                    "expr": "concat(subscription_id, '|', cast(day as string))",
                }
            ],
            "dimensions": [
                {
                    "name": "day",
                    "type": "time",
                    "expr": "day",
                    "type_params": {"time_granularity": "day"},
                },
                {"name": "status", "type": "categorical", "expr": "status"},
            ],
            "measures": [],
        }
    ],
    "metrics": [
        {
            "name": "active",
            "type": "simple",
            "type_params": {
                "expr": "customer_id",
                "metric_aggregation_params": {
                    "semantic_model": "mrr",
                    "agg": "count_distinct",
                    "agg_time_dimension": "day",
                    "non_additive_dimension": None,
                },
            },
        },
        {
            "name": "balance",
            "type": "simple",
            "type_params": {
                "expr": "amount",
                "metric_aggregation_params": {
                    "semantic_model": "mrr",
                    "agg": "sum",
                    "agg_time_dimension": "day",
                    "non_additive_dimension": {"name": "day", "window_choice": "max"},
                },
            },
        },
        {
            "name": "hidden",
            "type": "simple",
            "type_params": {
                "measure": {"name": "x"},
                "is_private": True,
            },
        },
    ],
    "project_configuration": {"time_spine_table_configurations": [], "metadata": None},
}

MANIFESTS = {"classic": CLASSIC, "new_spec": NEW_SPEC}


def _msi(payload):
    return PydanticSemanticManifest.parse_obj(json.loads(json.dumps(payload)))


def _enum(v):
    """MSI models these as enums and callers read `.value`; ours are plain
    lowercase strings. Normalise so the comparison is about the *data*."""
    return getattr(v, "value", v)


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_semantic_models_agree_field_for_field(name):
    ours = parse_manifest(MANIFESTS[name])
    theirs = _msi(MANIFESTS[name])
    assert [m.name for m in ours.semantic_models] == [m.name for m in theirs.semantic_models]
    for a, b in zip(ours.semantic_models, theirs.semantic_models):
        assert (a.node_relation.database, a.node_relation.schema_name, a.node_relation.alias) == (
            b.node_relation.database,
            b.node_relation.schema_name,
            b.node_relation.alias,
        )
        assert getattr(a.defaults, "agg_time_dimension", None) == getattr(
            b.defaults, "agg_time_dimension", None
        )
        assert [(e.name, _enum(e.type), e.expr) for e in a.entities] == [
            (e.name, _enum(e.type), e.expr) for e in b.entities
        ]
        assert [
            (d.name, _enum(d.type), d.expr, _enum(getattr(d.type_params, "time_granularity", None)))
            for d in a.dimensions
        ] == [
            (d.name, _enum(d.type), d.expr, _enum(getattr(d.type_params, "time_granularity", None)))
            for d in b.dimensions
        ]
        assert [(m.name, _enum(m.agg), m.expr, m.agg_time_dimension) for m in a.measures] == [
            (m.name, _enum(m.agg), m.expr, m.agg_time_dimension) for m in b.measures
        ]


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_metrics_agree_on_everything_the_bridge_reads(name):
    ours = parse_manifest(MANIFESTS[name])
    theirs = _msi(MANIFESTS[name])
    assert [m.name for m in ours.metrics] == [m.name for m in theirs.metrics]
    for a, b in zip(ours.metrics, theirs.metrics):
        assert _enum(a.type) == _enum(b.type)
        pa, pb = a.type_params, b.type_params
        assert getattr(pa.measure, "name", None) == getattr(pb.measure, "name", None)
        assert pa.expr == pb.expr
        assert getattr(pa, "is_private", False) == getattr(pb, "is_private", False)
        for side in ("numerator", "denominator"):
            assert getattr(getattr(pa, side), "name", None) == getattr(
                getattr(pb, side), "name", None
            )
        assert [i.name for i in (pa.metrics or [])] == [i.name for i in (pb.metrics or [])]
        assert [bool(i.offset_window) for i in (pa.metrics or [])] == [
            bool(i.offset_window) for i in (pb.metrics or [])
        ]
        qa = getattr(pa, "metric_aggregation_params", None)
        qb = getattr(pb, "metric_aggregation_params", None)
        assert (qa is None) == (qb is None)
        if qa is not None:
            assert qa.semantic_model == qb.semantic_model
            assert _enum(qa.agg) == _enum(qb.agg)
            assert qa.agg_time_dimension == qb.agg_time_dimension
            assert bool(qa.non_additive_dimension) == bool(qb.non_additive_dimension)


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_we_read_the_raw_artifact_and_msis_view_of_it_identically(name):
    """The end-to-end claim, and the one that would catch a real divergence.

    Field-level agreement says we see the same values in the same places. This
    says it survives *normalisation*: MSI applies its own defaults and coercions
    when it parses, so round-tripping through it produces a canonicalised view
    of the same manifest. If our models missed something MSI fills in — or kept
    something it drops — the bridge's output would differ between the two, and
    a tree built from a real project would silently disagree with dbt's own
    reading of it.

    `translate` itself is not run against MSI objects: it compares plain
    lowercase strings where MSI holds enums, deliberately, since the models are
    now breakdown's own.
    """
    payload = MANIFESTS[name]
    canonical = json.loads(_msi(payload).json())

    def shape(manifest):
        r = translate(manifest)
        return (
            {k: v.model_dump() for k, v in sorted(r.bindings.items())},
            {k: (v.formula, v.parents) for k, v in sorted(r.formulas.items())},
            sorted((s.name, s.reason) for s in r.skipped),
            dict(sorted(r.grains.items())),
            dict(sorted(r.kinds.items())),
        )

    assert shape(parse_manifest(payload)) == shape(parse_manifest(canonical))


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_the_fixtures_actually_exercise_the_bridge(name):
    # A differential test that compares two empty results proves nothing, so
    # pin that these manifests produce real bindings, formulas and skips.
    r = translate(parse_manifest(MANIFESTS[name]))
    assert r.bindings, "fixture produced no bindings"
    assert r.skipped or r.formulas, "fixture exercises neither skips nor formulas"


def test_unknown_keys_are_ignored_the_same_way_dbt_adds_them():
    # dbt adds fields between minor versions; rejecting unknown keys would make
    # every upgrade a breakage. This is safe only because the bridge hard-fails
    # on a metric that resolves to neither known shape — see `dbt_manifest`.
    payload = json.loads(json.dumps(CLASSIC))
    payload["semantic_models"][0]["some_future_field"] = {"x": 1}
    payload["metrics"][0]["another_one"] = ["y"]
    assert parse_manifest(payload).metrics[0].name == "revenue"


def test_a_metric_with_neither_shape_still_reaches_the_bridges_refusal():
    # The DSI failure this pairing guards against: models that ignore what they
    # do not know cannot catch it, so the bridge must — and does.
    payload = json.loads(json.dumps(CLASSIC))
    payload["metrics"] = [{"name": "orphan", "type": "simple", "type_params": {"expr": "x"}}]
    result = translate(parse_manifest(payload))
    assert not result.bindings
    assert "neither a `measure`" in result.skipped[0].reason

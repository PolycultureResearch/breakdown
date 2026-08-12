"""Typed models for the part of `target/semantic_manifest.json` breakdown reads.

This is a **data format, not a language**. dbt has already resolved every
`ref()`, inherited default and Jinja expression by the time it writes this file,
so parsing it is reading a versioned JSON document — not reimplementing dbt's
parser. That distinction is the whole reason these models exist rather than a
dependency: the alternative, `metricflow_semantic_interfaces`, ships inside the
`metricflow` wheel, which drags in twelve transitive packages and a `<3.15`
Python ceiling so we can make exactly one call.

The cost is that we own the schema. Two things keep that honest:

- **`tests/test_dbt_manifest.py` is a differential oracle.** When `metricflow`
  is installed — it stays in the dev group for this reason — the suite parses
  the same fixtures with MSI and asserts every field breakdown consumes agrees.
  Schema drift fails a test rather than reaching a user.
- **The bridge refuses what it does not recognise.** Unknown keys are ignored
  here, which is exactly how `dbt_semantic_interfaces` silently returned every
  new-spec metric with no aggregation — so ignoring is safe *only* because
  `dbt_bridge._translate_simple` hard-fails when a simple metric resolves to
  neither `measure` nor `metric_aggregation_params`, and every unknown
  aggregation and metric type is reported rather than defaulted. Do not relax
  those checks on the assumption that these models catch the problem; they
  cannot, and that is the point of the pairing.

Enum-valued fields are plain lowercase strings. MSI models them as enums and
callers read `.value`; keeping strings here means the bridge compares strings
either way, and the oracle asserts the two agree.

Some fields here are modelled **only so the bridge can refuse them**:
`filter`, `fill_nulls_with`, `join_to_timespine`, `offset_window`,
`non_additive_dimension`. None of them has a `BindingSpec` counterpart, so none
is ever read for its value — but every one of them changes which rows or which
periods the metric aggregates, and `extra: "ignore"` drops what is not declared.
A dropped `filter` does not fail; it produces a binding over the *whole*
relation under the filtered metric's name (roadmap C15). Model first, refuse
second; the one thing that must never happen is neither.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class _Node(BaseModel):
    # dbt adds fields to this artifact between minor versions, and none of the
    # ones we do not model change the meaning of the ones we do. Rejecting
    # unknown keys would make every dbt upgrade a breakage.
    model_config = {"extra": "ignore"}


class NodeRelation(_Node):
    """Where the semantic model's data actually lives."""

    alias: str
    schema_name: Optional[str] = None
    database: Optional[str] = None
    # dbt pre-quotes this in the adapter's own dialect, so the bridge composes
    # from the parts above instead. Kept for diagnostics.
    relation_name: Optional[str] = None


class Entity(_Node):
    name: str
    type: str  # primary | foreign | unique | natural
    expr: Optional[str] = None


class DimensionTypeParams(_Node):
    time_granularity: Optional[str] = None


class Dimension(_Node):
    name: str
    type: str  # categorical | time
    expr: Optional[str] = None
    type_params: Optional[DimensionTypeParams] = None


class Measure(_Node):
    name: str
    agg: str
    expr: Optional[str] = None
    agg_time_dimension: Optional[str] = None


class SemanticModelDefaults(_Node):
    agg_time_dimension: Optional[str] = None


class SemanticModel(_Node):
    name: str
    node_relation: NodeRelation
    defaults: Optional[SemanticModelDefaults] = None
    entities: List[Entity] = Field(default_factory=list)
    dimensions: List[Dimension] = Field(default_factory=list)
    measures: List[Measure] = Field(default_factory=list)


class WhereFilter(_Node):
    """One predicate of a metric's filter.

    dbt writes the Jinja *template*, not SQL: `{{ Dimension('order__region') }}
    = 'US'`. Rendering it needs the query's join tree, which is the deeper
    reason the bridge refuses filters rather than pasting them into a binding.
    Kept as text so a skip reason can quote the predicate the author wrote.
    """

    where_sql_template: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: Any) -> Any:
        # MetricFlow's original spec allowed a filter to be a bare SQL string,
        # and manifests written by older dbt versions still carry that shape.
        return {"where_sql_template": value} if isinstance(value, str) else value


class WhereFilterIntersection(_Node):
    """The AND of several `WhereFilter`s — dbt's shape for any `filter:`.

    MetricFlow accepts four serialisations here (bare string, a single
    `{where_sql_template: ...}`, a list of either, and the canonical
    `{where_filters: [...]}`) and normalises them on parse. We normalise the
    same four, because a filter shape we failed to recognise would parse as
    *no filter* — the exact silent widening C15 is about.
    """

    where_filters: List[WhereFilter] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_shapes(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"where_filters": [value]}
        if isinstance(value, list):
            return {"where_filters": value}
        if isinstance(value, dict) and "where_sql_template" in value:
            return {"where_filters": [value]}
        return value

    @property
    def predicates(self) -> List[str]:
        """The non-empty predicates, for callers deciding whether a filter is
        really there. An intersection of zero filters is not a filter, and a
        pydantic model is truthy whatever it holds — so truthiness alone would
        refuse metrics that translate perfectly well."""
        return [f.where_sql_template for f in self.where_filters if f.where_sql_template.strip()]


class MetricInputMeasure(_Node):
    """A simple metric's reference to a measure (classic spec).

    Every field past `name` and `alias` changes the number: `filter` narrows the
    rows, `join_to_timespine` adds rows for empty periods, `fill_nulls_with`
    fills them. The bridge refuses all three — see `dbt_bridge`.
    """

    name: str
    # Post-aggregation rename only, so it changes no number. Modelled because
    # MSI has it and the differential oracle compares what we model.
    alias: Optional[str] = None
    filter: Optional[WhereFilterIntersection] = None
    join_to_timespine: bool = False
    # `0` is both the commonest value and falsy, so consumers must test
    # `is not None` rather than truthiness.
    fill_nulls_with: Optional[int] = None


class MetricInput(_Node):
    """A reference from one metric to another metric: a ratio's numerator or
    denominator, or an input to a derived metric."""

    name: str
    alias: Optional[str] = None
    # A per-input filter re-scopes that input alone: `conversion_rate` may be
    # `signups(filtered) / sessions`. Translating it as a formula over the two
    # named metrics drops the scope silently, so it is refused.
    filter: Optional[WhereFilterIntersection] = None
    # Only truthiness is consumed — an offset means a time-spine self-join the
    # generator does not build, so the metric is skipped by name.
    offset_window: Optional[Dict[str, Any]] = None
    offset_to_grain: Optional[str] = None


class MetricAggregationParams(_Node):
    """dbt's new metrics spec: the aggregation moved onto the metric, and the
    measure layer went away. The aggregated column is *not* here — it is
    mirrored on `type_params.expr`, which is where dbt writes it."""

    semantic_model: str
    agg: str
    agg_time_dimension: Optional[str] = None
    # Truthiness only: its MIN/MAX filter is applied per grain window, so it is
    # query-grain-dependent and cannot be a fixed binding.
    non_additive_dimension: Optional[Dict[str, Any]] = None


class MetricTypeParams(_Node):
    # Classic spec.
    measure: Optional[MetricInputMeasure] = None
    # New spec (and Fusion).
    metric_aggregation_params: Optional[MetricAggregationParams] = None
    numerator: Optional[MetricInput] = None
    denominator: Optional[MetricInput] = None
    expr: Optional[str] = None
    metrics: Optional[List[MetricInput]] = None
    # The same two flags as on `MetricInputMeasure`, at the level dbt lifts them
    # to when it flattens a measure input onto its simple metric — the new spec
    # writes them here and nowhere else, so both places must be checked.
    join_to_timespine: bool = False
    fill_nulls_with: Optional[int] = None
    # dbt's marker for metrics auto-created to replace measures during the
    # new-spec migration. On the metric in MSI's older sibling; on type_params
    # here, which is where dbt actually writes it.
    is_private: bool = False


class Metric(_Node):
    name: str
    type: str  # simple | ratio | derived | cumulative | conversion
    type_params: MetricTypeParams = Field(default_factory=MetricTypeParams)
    # A metric-level filter applies to the whole metric, whatever its type — dbt
    # also merges a measure input's filter up to here when it flattens a simple
    # metric, so this is where a classic-spec filter usually ends up.
    filter: Optional[WhereFilterIntersection] = None


class SemanticManifest(_Node):
    semantic_models: List[SemanticModel] = Field(default_factory=list)
    metrics: List[Metric] = Field(default_factory=list)


def parse_manifest(payload: Dict[str, Any]) -> SemanticManifest:
    """Parse a decoded `semantic_manifest.json`."""
    return SemanticManifest.model_validate(payload)

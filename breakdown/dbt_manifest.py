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
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class MetricInput(_Node):
    """A reference from one metric to a measure or another metric."""

    name: str
    alias: Optional[str] = None
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
    measure: Optional[MetricInput] = None
    # New spec (and Fusion).
    metric_aggregation_params: Optional[MetricAggregationParams] = None
    numerator: Optional[MetricInput] = None
    denominator: Optional[MetricInput] = None
    expr: Optional[str] = None
    metrics: Optional[List[MetricInput]] = None
    # dbt's marker for metrics auto-created to replace measures during the
    # new-spec migration. On the metric in MSI's older sibling; on type_params
    # here, which is where dbt actually writes it.
    is_private: bool = False


class Metric(_Node):
    name: str
    type: str  # simple | ratio | derived | cumulative | conversion
    type_params: MetricTypeParams = Field(default_factory=MetricTypeParams)


class SemanticManifest(_Node):
    semantic_models: List[SemanticModel] = Field(default_factory=list)
    metrics: List[Metric] = Field(default_factory=list)


def parse_manifest(payload: Dict[str, Any]) -> SemanticManifest:
    """Parse a decoded `semantic_manifest.json`."""
    return SemanticManifest.model_validate(payload)

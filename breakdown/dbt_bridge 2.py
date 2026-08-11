"""Read a dbt project's own semantic definitions and translate them into
breakdown node bindings.

The input is `target/semantic_manifest.json`, which plain `dbt parse` writes on
**dbt Core** — no dbt Cloud, no Semantic Layer credential, no service token, no
plan tier. It is dbt's own fully-resolved artifact, so nothing here re-derives
what dbt already decided: `node_relation` carries the real warehouse relation,
refs are resolved, and the metric taxonomy is explicit.

Design: `knowledge/semantic_layer_connectivity_design.md` §2 and §5.

Two shapes of manifest exist and both are supported. In the **classic** spec a
simple metric points at a `measure` on a semantic model, and the measure carries
the aggregation. In dbt's **new metrics spec** (also what Fusion emits) the
measure layer is gone and the metric carries its own aggregation inline under
`type_params.metric_aggregation_params`, with the aggregated column in
`type_params.expr`. A metric resolving to neither is an error rather than a
default, because the failure mode this guards against is silent: see
`_require_msi`.

What this module deliberately does not do is guess. Every metric it cannot
translate exactly comes back in `BridgeResult.skipped` with a reason naming the
construct, because a metric breakdown cannot express correctly must fail with
its name attached rather than produce a plausible number.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from breakdown.data_fetch import MissingProviderExtra, _extra_hint
from breakdown.parser import BindingDimension, BindingSpec

logger = logging.getLogger(__name__)

SEMANTIC_MANIFEST_FILE = "semantic_manifest.json"

# MetricFlow aggregation -> binding `agg`. The three absent from the binding
# contract are absent on purpose: `min`/`max` have no additive decomposition
# (the min of two slices is not a function of their contributions), and
# `percentile`/`median` are order statistics, which do not decompose at all.
# They are skipped with a reason rather than coerced to something summable.
#
# `sum_boolean` maps to `sum` because that is exactly what MetricFlow's own
# `BooleanMeasureAggregationRule` rewrites it to before generating SQL.
AGG_MAP = {
    "sum": "sum",
    "sum_boolean": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "average": "average",
}

# MetricFlow granularity -> breakdown grain. Anything finer than a day floors to
# `day`, which is breakdown's finest grain; quarter and year have no breakdown
# equivalent, so a metric whose source column cannot go finer than a quarter is
# skipped rather than silently modelled monthly.
GRAIN_MAP = {
    "nanosecond": "day", "microsecond": "day", "millisecond": "day",
    "second": "day", "minute": "day", "hour": "day",
    "day": "day", "week": "week", "month": "month",
}

# Aggregations whose natural breakdown `kind` is not `flow`. An average is a
# rate — it cannot be re-aggregated over time, which is the whole point of
# `kind: rate`.
KIND_MAP = {"average": "rate"}


@dataclass(frozen=True)
class SkippedMetric:
    """A manifest metric that was not translated, and why."""

    name: str
    reason: str


@dataclass(frozen=True)
class FormulaCandidate:
    """A manifest metric that is a *relationship* between other metrics rather
    than a fetch: `ratio` and `derived`. These become formula edges in the tree
    — the DAG is breakdown's own contribution and is never a binding.

    Carried here rather than turned into tree YAML because that is the
    scaffolder's job (roadmap 2.3); this module's contract is translation, not
    authoring.
    """

    name: str
    formula: str
    parents: Tuple[str, ...]
    # A ratio is a rate: it must not be summed or gap-filled as a flow.
    kind: str = "rate"


@dataclass
class BridgeResult:
    bindings: Dict[str, BindingSpec] = field(default_factory=dict)
    formulas: Dict[str, FormulaCandidate] = field(default_factory=dict)
    skipped: List[SkippedMetric] = field(default_factory=list)
    # Inferred per-metric grain/kind, keyed by metric name. Separate from the
    # binding because they are modeling facts about the node, not fetch facts.
    grains: Dict[str, str] = field(default_factory=dict)
    kinds: Dict[str, str] = field(default_factory=dict)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(SkippedMetric(name=name, reason=reason))


def _require_msi() -> ModuleType:
    """Import `metricflow_semantic_interfaces`, or say which extra installs it.

    It must be MSI and not the older `dbt_semantic_interfaces`. DSI is
    deprecated, and against a new-spec manifest it does not fail — it ignores
    the fields it does not know, returns every simple metric with no
    aggregation at all, and then *validates with zero errors*. A bridge built on
    it would emit silently broken bindings that passed their own checks, which
    is why this module never falls back to it.
    """
    try:
        import metricflow_semantic_interfaces  # noqa: F401
        from metricflow_semantic_interfaces.implementations.semantic_manifest import (
            PydanticSemanticManifest,
        )
    except ImportError as e:
        raise MissingProviderExtra(
            _extra_hint(
                "dbt",
                "dbt-bridge",
                "missing module 'metricflow_semantic_interfaces' (it ships in "
                "metricflow>=0.210; the older dbt-semantic-interfaces is "
                "deprecated and mis-parses new-spec manifests)",
            )
        ) from e
    return PydanticSemanticManifest


def manifest_path(project_path: str) -> str:
    """`target/semantic_manifest.json` under a dbt project, or the file itself
    if that is what was given."""
    if os.path.isfile(project_path):
        return project_path
    return os.path.join(project_path, "target", SEMANTIC_MANIFEST_FILE)


def load_manifest(project_path: str) -> Any:
    """Parse a dbt semantic manifest with MSI."""
    path = manifest_path(project_path)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No semantic manifest at {path}. Run `dbt parse` in the project to "
            "write it. (dbt Core v2 / Fusion does not write this file at all "
            "for projects still on the legacy `semantic_models:` spec — those "
            "must migrate to the new metrics spec first.)"
        )
    PydanticSemanticManifest = _require_msi()
    with open(path) as fh:
        return PydanticSemanticManifest.parse_obj(json.load(fh))


def _relation(model: Any) -> str:
    """The warehouse relation for a semantic model.

    Composed from `database`/`schema_name`/`alias` rather than read from
    `relation_name`, which dbt has already quoted in the adapter's own dialect —
    re-parsing those quotes per warehouse is a bug farm, and the generator
    quotes for the target dialect itself.
    """
    nr = model.node_relation
    parts = [p for p in (nr.database, nr.schema_name, nr.alias) if p]
    return ".".join(parts)


def _column(element: Any) -> str:
    """The column an entity/dimension/measure reads. MetricFlow's `expr` is
    optional: when absent the element's own name is the column."""
    return element.expr or element.name


def _grain_key(model: Any) -> Optional[str]:
    """The column making the relation one row per grain: the primary entity.

    This is what `doctor`'s grain-claim assertion counts distinct values of, so
    a model with no primary entity cannot be bound — there is nothing to prove
    the grain against, and an unproven grain is exactly the silent fan-out the
    contract exists to catch.
    """
    for entity in model.entities:
        if entity.type.value == "primary":
            return _column(entity)
    return None


def _time_dimension(model: Any, agg_time_dimension: Optional[str]) -> Optional[Any]:
    """The dimension a measure aggregates over: its own `agg_time_dimension`,
    else the semantic model's `defaults.agg_time_dimension`."""
    name = agg_time_dimension
    if name is None:
        defaults = getattr(model, "defaults", None)
        name = getattr(defaults, "agg_time_dimension", None) if defaults else None
    if name is None:
        return None
    return next((d for d in model.dimensions if d.name == name), None)


def _categorical_dimensions(model: Any) -> Dict[str, BindingDimension]:
    """Categorical dimensions on the model itself.

    Same-relation only. A dimension reached through an entity join is a
    many-to-one hop the generator could make, but proving it many-to-one needs
    the joined model's primary entity, so it is deferred rather than assumed —
    an unproven join is the fan-out case §6 refuses.
    """
    return {
        d.name: BindingDimension(column=_column(d))
        for d in model.dimensions
        if d.type.value == "categorical"
    }


def _measure_source(manifest: Any, metric: Any) -> Tuple[Optional[Any], Optional[str], Optional[str], Optional[str]]:
    """Resolve a simple metric to (semantic_model, agg, column, agg_time_dim),
    across both manifest shapes. Returns (None, ...) when it resolves to
    neither, which the caller reports rather than defaults.
    """
    tp = metric.type_params
    models = {m.name: m for m in manifest.semantic_models}

    # New metrics spec (and Fusion): the aggregation is on the metric.
    params = getattr(tp, "metric_aggregation_params", None)
    if params is not None:
        model = models.get(params.semantic_model)
        if model is None:
            return None, None, None, None
        # `metric_aggregation_params` omits the aggregated column; it is
        # mirrored on `type_params.expr`, which is where dbt puts it.
        return model, params.agg.value, tp.expr, params.agg_time_dimension

    # Classic spec: the metric points at a measure that carries the aggregation.
    if tp.measure is not None:
        for model in manifest.semantic_models:
            for measure in model.measures:
                if measure.name == tp.measure.name:
                    return (
                        model,
                        measure.agg.value,
                        _column(measure),
                        measure.agg_time_dimension,
                    )
    return None, None, None, None


def _translate_simple(manifest: Any, metric: Any, result: BridgeResult) -> None:
    name = metric.name
    model, agg, column, agg_time = _measure_source(manifest, metric)
    if model is None:
        result.skip(
            name,
            "resolves to neither a `measure` (classic spec) nor "
            "`metric_aggregation_params` (new spec); the manifest may have been "
            "parsed by a tool that dropped fields it did not recognise",
        )
        return

    params = getattr(metric.type_params, "metric_aggregation_params", None)
    if params is not None and getattr(params, "non_additive_dimension", None):
        result.skip(
            name,
            "declares `non_additive_dimension`, whose MIN/MAX filter is applied "
            "per grain window and so depends on the query grain — it cannot be "
            "expressed as a fixed binding without being wrong at some grains",
        )
        return

    if agg not in AGG_MAP:
        result.skip(name, f"aggregation '{agg}' has no additive decomposition")
        return
    if not column:
        result.skip(name, f"aggregation '{agg}' has no column to aggregate")
        return

    grain_key = _grain_key(model)
    if grain_key is None:
        result.skip(
            name,
            f"semantic model '{model.name}' declares no primary entity, so its "
            "grain cannot be asserted and fan-out cannot be ruled out",
        )
        return

    time_dim = _time_dimension(model, agg_time)
    if time_dim is None:
        result.skip(
            name,
            f"no agg_time_dimension on the measure or on semantic model "
            f"'{model.name}' defaults, so there is no time axis to fetch over",
        )
        return

    mf_grain = getattr(time_dim.type_params, "time_granularity", None)
    mf_grain = mf_grain.value if mf_grain is not None else "day"
    if mf_grain not in GRAIN_MAP:
        result.skip(
            name,
            f"time dimension '{time_dim.name}' has granularity '{mf_grain}', "
            "which is coarser than breakdown's coarsest grain (month)",
        )
        return

    binding_agg = AGG_MAP[agg]
    result.bindings[name] = BindingSpec(
        relation=_relation(model),
        grain_key=grain_key,
        time_column=_column(time_dim),
        agg=binding_agg,
        measure=column,
        # A distinct count needs to know what it is counting to be decomposed
        # at the grain where it becomes a sum. The counted column is the entity.
        entity_key=column if binding_agg == "count_distinct" else None,
        dimensions=_categorical_dimensions(model),
    )
    result.grains[name] = GRAIN_MAP[mf_grain]
    result.kinds[name] = KIND_MAP.get(binding_agg, "flow")


def _translate_ratio(metric: Any, result: BridgeResult) -> None:
    """A MetricFlow ratio references two *metrics*, so it becomes a formula edge
    over them rather than one binding.

    That is both more faithful and better for attribution: numerator and
    denominator end up as their own nodes with their own bindings, which is
    exactly the "fetch them separately" requirement, and the ratio node reuses
    breakdown's existing `num / den` rate handling — including the automatic
    slicing `weight` default.
    """
    tp = metric.type_params
    num, den = tp.numerator, tp.denominator
    if num is None or den is None:
        result.skip(metric.name, "ratio metric is missing a numerator or denominator")
        return
    for side in (num, den):
        if getattr(side, "offset_window", None) or getattr(side, "offset_to_grain", None):
            result.skip(
                metric.name,
                "ratio input declares an offset window, which needs a time-spine "
                "self-join the generator does not do yet",
            )
            return
    result.formulas[metric.name] = FormulaCandidate(
        name=metric.name,
        formula=f"{num.name} / {den.name}",
        parents=(num.name, den.name),
    )
    result.kinds[metric.name] = "rate"


def _translate_derived(metric: Any, result: BridgeResult) -> None:
    tp = metric.type_params
    inputs = tuple(i.name for i in (tp.metrics or []))
    if not tp.expr or not inputs:
        result.skip(metric.name, "derived metric has no expression or no input metrics")
        return
    for i in tp.metrics or []:
        if getattr(i, "offset_window", None) or getattr(i, "offset_to_grain", None):
            result.skip(
                metric.name,
                "derived metric offsets an input in time, which needs a "
                "time-spine self-join the generator does not do yet",
            )
            return
    if any(i.alias for i in tp.metrics or []):
        result.skip(
            metric.name,
            "derived metric aliases an input, so its expression does not "
            "reference input metrics by name",
        )
        return
    result.formulas[metric.name] = FormulaCandidate(
        name=metric.name, formula=tp.expr, parents=inputs
    )
    result.kinds[metric.name] = "rate"


def translate(manifest: Any) -> BridgeResult:
    """Translate every metric in a parsed semantic manifest.

    Nothing here raises on an untranslatable metric: they are collected in
    `skipped` so a caller can report all of them at once rather than one per
    run. What *would* be wrong is emitting a binding that is subtly incorrect,
    and that is what each skip reason exists to prevent.
    """
    result = BridgeResult()
    for metric in manifest.metrics:
        # Metrics dbt auto-created to stand in for measures during the new-spec
        # migration are an implementation detail of the migration, not part of
        # anybody's metric tree.
        if getattr(metric, "is_private", False):
            continue
        kind = metric.type.value
        if kind == "simple":
            _translate_simple(manifest, metric, result)
        elif kind == "ratio":
            _translate_ratio(metric, result)
        elif kind == "derived":
            _translate_derived(metric, result)
        elif kind == "cumulative":
            result.skip(
                metric.name,
                "cumulative metrics accumulate over a trailing window or to a "
                "grain boundary, which needs a non-equi join to the time spine",
            )
        elif kind == "conversion":
            result.skip(
                metric.name,
                "conversion metrics need a windowed last-touch attribution join "
                "between two event streams; approximating one silently "
                "misattributes conversions",
            )
        else:
            result.skip(metric.name, f"unsupported metric type '{kind}'")
    return result


def bridge_project(project_path: str) -> BridgeResult:
    """Load a dbt project's semantic manifest and translate it."""
    return translate(load_manifest(project_path))

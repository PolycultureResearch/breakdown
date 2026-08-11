"""Compile a node binding into the one query breakdown needs.

The surface is deliberately narrow — aggregate one measure, bucket it by one
time grain, optionally group by one dimension, bound to a window — because that
is all the engine ever asks a provider for. This is a query builder, not a
semantic layer: joins are many-to-one only, there is no metric-on-metric
composition (that is `formula`, the DAG), and anything the builder cannot
express exactly raises `UnsupportedBinding` rather than approximating.

Design: `knowledge/semantic_layer_connectivity_design.md` §5 and §6.

Output contract matches `BaseDataFetcher`: `[date, value]`, or
`[date, slice, value]` when a dimension is requested. `date` is the period-start
label, so the frame drops straight into `_to_naive_dates` → `_floor_labels` →
`_align_to_spine` like every other provider.
"""

import logging
from types import ModuleType
from typing import Any, Optional

import pandas as pd

from breakdown.data_fetch import MissingProviderExtra, _extra_hint
from breakdown.grains import GRAINS
from breakdown.parser import BindingDimension, BindingSpec

logger = logging.getLogger(__name__)

# Column names the engine expects back. Quoted as identifiers so a warehouse
# that reserves `date` (several do) still round-trips.
DATE_COL = "date"
SLICE_COL = "slice"
# Matches `engine.slices._NULL`, so a NULL dimension value reads the same way
# through the flow path as through the sliced path.
_NULL_SLICE = "__null__"
VALUE_COL = "value"

# Truncation is where dialects disagree, so every expression here is parsed *in
# the target dialect* (see `_parse_dialect`) rather than translated into it from
# a portable form. Two separate hazards made that necessary.
#
# **Week boundaries.** `DATE_TRUNC('WEEK', …)` is ISO Monday on DuckDB, Postgres
# and Spark, but **BigQuery** defaults `WEEK` to *Sunday* and **Snowflake**
# honours the session's `WEEK_START`, so the same query buckets differently for
# different users. Either shifts a bucket's composition by up to six days while
# still emitting exactly one label per week — and `grains.floor_period` then
# relabels it to the previous Monday, landing it on the spine and hiding the
# shift completely. Nothing downstream could detect it, which is why it is fixed
# at the source: `ISOWEEK` on BigQuery, a session-independent `DAYOFWEEKISO`
# offset on Snowflake.
#
# **Databricks day truncation.** Translating a portable `DATE_TRUNC('DAY', …)`
# into Databricks yields `TRUNC(col, 'DAY')` — and Spark's `trunc` accepts only
# YEAR/MONTH/WEEK/QUARTER, returning **NULL** for DAY rather than erroring. Every
# row then collapses into a single NULL-labelled bucket holding the whole
# window's total. `_align_to_spine` does catch it (no row lands on the spine, so
# it raises), but day grain was simply unusable on Databricks, and no local test
# could see it: sqlglot transpiled happily and DuckDB was never asked. It was
# found by running the generated SQL against a real Databricks warehouse.
# Parsing in-dialect keeps Spark's `DATE_TRUNC`, which does support DAY.
_TRUNC = {
    "day": "DATE_TRUNC('DAY', {col})",
    "week": "DATE_TRUNC('WEEK', {col})",
    "month": "DATE_TRUNC('MONTH', {col})",
}

# Per-(dialect, grain) overrides where the portable form is wrong or unsupported.
_TRUNC_OVERRIDES = {
    ("bigquery", "week"): "DATE_TRUNC({col}, ISOWEEK)",
    ("snowflake", "week"): ("DATEADD(DAY, -(DAYOFWEEKISO({col}) - 1), CAST({col} AS DATE))"),
}


class UnsupportedBinding(RuntimeError):
    """The binding is valid but this generator cannot compile it exactly."""


def _require_sqlglot() -> ModuleType:
    try:
        import sqlglot
    except ImportError as e:
        raise MissingProviderExtra(
            _extra_hint("dbt", "dbt-bridge", "missing module 'sqlglot'")
        ) from e
    return sqlglot


def _parse_dialect(dialect: str) -> Optional[str]:
    """The dialect sqlglot should *read* with. Passing the target dialect makes
    every expression below round-trip as written instead of being translated
    from a portable form that may not have an equivalent."""
    return dialect or None


def _truncate(column: str, grain: str, dialect: str) -> str:
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {list(GRAINS)}, got '{grain}'")
    template = _TRUNC_OVERRIDES.get((dialect, grain), _TRUNC[grain])
    return template.format(col=column)


def _window_bounds(start_date: str, end_date: str) -> tuple[str, str]:
    """Half-open `[start, end_exclusive)` bounds.

    breakdown windows are inclusive of `end_date`, but the bound is compared
    against a column that may be a timestamp — so `<= '2024-01-31'` silently
    drops everything after midnight on the last day, losing ~1/31 of a monthly
    figure. Advancing to an exclusive next-day bound is the standard fix and is
    correct for DATE columns too.
    """
    start = pd.Timestamp(start_date).normalize()
    end_exclusive = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end_exclusive.strftime("%Y-%m-%d")


def _aggregate(bind: BindingSpec, measure: str) -> str:
    """The aggregate expression over an already-qualified measure."""
    agg = bind.agg
    if agg == "sum":
        return f"SUM({measure})"
    if agg == "count":
        # NOT COUNT(*). MetricFlow's `count` is null-guarded — it desugars to
        # SUM(CASE WHEN expr IS NOT NULL THEN 1 ELSE 0 END) — and COUNT(x) is
        # exactly that: the count of non-null x. Using COUNT(*) here would
        # silently include rows the source deliberately excludes.
        return f"COUNT({measure})"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {measure})"
    if agg == "average":
        return f"AVG({measure})"
    raise UnsupportedBinding(f"aggregation '{agg}' has no aggregate form here")


def _qualified(expr: str, alias: str) -> str:
    """Qualify a bare column with the fact alias; leave real expressions alone.

    MetricFlow columns are ~99% bare identifiers, but the rest are expressions
    (`concat(a, '|', b)`, `x IS NOT NULL`). Prefixing those textually would
    corrupt them, so only a lone identifier is qualified — anything else is the
    author's expression and is emitted as written, resolved by the warehouse
    against whatever is in scope.
    """
    token = expr.strip()
    if token.replace("_", "").isalnum() and not token[0].isdigit():
        return f"{alias}.{token}"
    return token


def _dimension_join(dim: BindingDimension, fact_alias: str, dim_alias: str) -> Optional[str]:
    if dim.join is None:
        return None
    right = dim.to or dim.key
    return f"{dim.join} AS {dim_alias} ON {fact_alias}.{dim.key} = {dim_alias}.{right}"


def build_query(
    bind: BindingSpec,
    *,
    grain: str,
    start_date: str,
    end_date: str,
    dialect: str = "",
    dimension: Optional[str] = None,
) -> str:
    """Compile `bind` into dialect SQL returning `[date, value]`, or
    `[date, slice, value]` when `dimension` names one of its dimensions.

    `dialect` is a sqlglot dialect name (`duckdb`, `snowflake`, `bigquery`,
    `databricks`, `postgres`, …); the empty string emits generic SQL.
    """
    sqlglot = _require_sqlglot()

    if bind.agg == "last":
        # A stock's period value is its last snapshot, which needs a window
        # function and — once an entity is involved — the stock-and-flow
        # treatment of design §8. Refused rather than approximated: summing or
        # averaging snapshots across a period is a different number entirely.
        raise UnsupportedBinding(
            "`agg: last` needs a per-period last-snapshot window this generator "
            "does not build yet; express it as a `bind.sql` relation that "
            "already reduces to one row per period."
        )

    fact = "bd_fact"
    source = f"({bind.sql}) AS {fact}" if bind.sql else f"{bind.relation} AS {fact}"
    time_col = _qualified(bind.time_column, fact)
    date_expr = _truncate(time_col, grain, dialect)
    start, end_exclusive = _window_bounds(start_date, end_date)

    selects = [f'{date_expr} AS "{DATE_COL}"']
    group_by = ["1"]
    joins = []

    if dimension is not None:
        if dimension not in bind.dimensions:
            raise UnsupportedBinding(
                f"binding declares no dimension '{dimension}' "
                f"(has {sorted(bind.dimensions) or 'none'})"
            )
        dim = bind.dimensions[dimension]
        dim_alias = "bd_dim"
        join = _dimension_join(dim, fact, dim_alias)
        if join is not None:
            joins.append(join)
        slice_expr = _qualified(dim.column, dim_alias if join else fact)
        selects.append(f'{slice_expr} AS "{SLICE_COL}"')
        group_by.append("2")

    if bind.agg == "ratio":
        num = _qualified(bind.numerator, fact)
        den = _qualified(bind.denominator, fact)
        # NULLIF, not a bare divide: a zero denominator is an undefined rate,
        # and NULL propagates to `_align_to_spine`, which refuses to gap-fill a
        # rate. Returning 0 or +inf would invent a number instead.
        value_expr = f"SUM({num}) / NULLIF(SUM({den}), 0)"
    else:
        value_expr = _aggregate(bind, _qualified(bind.measure, fact))
    selects.append(f'{value_expr} AS "{VALUE_COL}"')

    read = _parse_dialect(dialect)
    query = sqlglot.select(*selects, dialect=read).from_(source, dialect=read)
    for join in joins:
        query = query.join(join, join_type="LEFT", dialect=read)
    query = (
        query.where(f"{time_col} >= '{start}'", dialect=read)
        .where(f"{time_col} < '{end_exclusive}'", dialect=read)
        .group_by(*group_by, dialect=read)
        .order_by(*group_by, dialect=read)
    )
    return query.sql(dialect=dialect, pretty=True)


def build_resolved_slice_query(
    bind: BindingSpec,
    *,
    dimension: str,
    grain: str,
    start_date: str,
    end_date: str,
    dialect: str = "",
) -> str:
    """A sliced query that sums back to the metric exactly (roadmap 3.8 §4).

    The plain sliced query groups rows, so an entity holding several values of
    the dimension inside a period lands in each of them — the slices then
    overstate the metric by that overlap. This one first collapses the relation
    to **one row per (entity, period)** using the declared `resolve` rule, so
    every entity contributes to exactly one slice and `Σ_g slices` is the
    distinct-entity count, which *is* the metric.

    `resolve: error` generates the plain query unchanged: it asserts the data is
    already single-valued rather than correcting it, and `doctor` is what proves
    the assertion. Correcting silently under `error` would defeat the point of
    choosing it.
    """
    spec = bind.entity_grain
    if spec is None:
        raise UnsupportedBinding(
            "binding declares no `entity_grain`, so its slices cannot be "
            "resolved to one row per entity per period"
        )
    if dimension not in bind.dimensions:
        raise UnsupportedBinding(
            f"binding declares no dimension '{dimension}' (has {sorted(bind.dimensions) or 'none'})"
        )
    if spec.resolve == "error":
        return build_query(
            bind,
            grain=grain,
            start_date=start_date,
            end_date=end_date,
            dialect=dialect,
            dimension=dimension,
        )

    sqlglot = _require_sqlglot()
    read = _parse_dialect(dialect)
    fact = "bd_fact"
    relation = spec.relation or bind.relation
    source = f"({bind.sql}) AS {fact}" if (relation is None) else f"{relation} AS {fact}"
    time_col = _qualified(bind.time_column, fact)
    date_expr = _truncate(time_col, grain, dialect)
    entity = _qualified(bind.entity_key, fact)
    dim = bind.dimensions[dimension]
    if dim.join is not None:
        # A joined dimension would have to be resolved after the join, which
        # changes what "one row per entity per period" ranks over. Refused
        # rather than guessed.
        raise UnsupportedBinding(
            f"dimension '{dimension}' is reached through a join; entity-grain "
            "resolution supports dimensions on the binding's own relation. "
            "Express the join in a `bind.sql` relation instead."
        )
    slice_expr = _qualified(dim.column, fact)
    start, end_exclusive = _window_bounds(start_date, end_date)
    # `first` keeps the earliest row in the period, `last` the latest — the two
    # answer different business questions, which is why neither is a default.
    order = "ASC" if spec.resolve == "first" else "DESC"

    # Internal aliases are plain identifiers, never quoted ones. A quoted
    # `"date"` is an identifier on DuckDB and Postgres but a **string literal**
    # on Spark and BigQuery, so referencing it in the outer SELECT silently
    # produced a constant column of the word "date" — every row unparseable as
    # a date, found only by running this against Databricks. The public
    # `date`/`slice`/`value` names are applied once, as aliases, which is the
    # path `build_query` already proves on that warehouse.
    ranked = (
        sqlglot.select(
            f"{date_expr} AS bd_date",
            f"{slice_expr} AS bd_slice",
            f"ROW_NUMBER() OVER (PARTITION BY {entity}, {date_expr} "
            f"ORDER BY {time_col} {order}) AS bd_rn",
            dialect=read,
        )
        .from_(source, dialect=read)
        .where(f"{time_col} >= '{start}'", dialect=read)
        .where(f"{time_col} < '{end_exclusive}'", dialect=read)
    )
    return (
        sqlglot.select(
            f'bd_date AS "{DATE_COL}"',
            f'bd_slice AS "{SLICE_COL}"',
            f'COUNT(*) AS "{VALUE_COL}"',
            dialect=read,
        )
        .from_(ranked.subquery("bd_resolved"), dialect=read)
        .where("bd_rn = 1", dialect=read)
        .group_by("1", "2", dialect=read)
        .order_by("1", "2", dialect=read)
        .sql(dialect=dialect, pretty=True)
    )


# An entity absent from a window, as distinct from one present with a NULL
# dimension value. Conflating those would report a user who never appeared as
# having no region, which is a different fact.
ABSENT = "__absent__"


def build_entity_flow_query(
    bind: BindingSpec,
    *,
    dimension: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    dialect: str = "",
) -> str:
    """The transition matrix between two windows: `[ref_slice, analysis_slice,
    entities]` (roadmap 3.8 §6).

    Each entity is resolved to one slice per *window* using the binding's own
    `resolve` rule, then the two sides are FULL OUTER JOINed. Reading the
    matrix gives the four classes — absent→g is new, g→absent is churned, g→g
    is retained, g₁→g₂ is migration — and keeps *where* the migration went,
    which is the part that turns "two offsetting causes" into a finding.

    The join is on the entity, and absence is detected from the joined key
    rather than from a NULL slice, so an entity present with a NULL dimension
    value stays distinguishable from one that was not there at all.
    """
    spec = bind.entity_grain
    if spec is None:
        raise UnsupportedBinding(
            "entity flows need an `entity_grain` block: classifying an entity "
            "as new, churned or migrated requires one slice per entity per "
            "window, and which one is the author's `resolve` choice."
        )
    if dimension not in bind.dimensions:
        raise UnsupportedBinding(f"binding declares no dimension '{dimension}'")
    dim = bind.dimensions[dimension]
    if dim.join is not None:
        raise UnsupportedBinding(
            f"dimension '{dimension}' is reached through a join; entity flows "
            "support dimensions on the binding's own relation."
        )

    sqlglot = _require_sqlglot()
    read = _parse_dialect(dialect)
    fact = "bd_fact"
    relation = spec.relation or bind.relation
    source = f"({bind.sql}) AS {fact}" if (relation is None) else f"{relation} AS {fact}"
    entity = _qualified(bind.entity_key, fact)
    time_col = _qualified(bind.time_column, fact)
    slice_expr = _qualified(dim.column, fact)
    order = "ASC" if spec.resolve == "first" else "DESC"

    def window_cte(start_date: str, end_date: str) -> Any:
        start, end_exclusive = _window_bounds(start_date, end_date)
        ranked = (
            sqlglot.select(
                f"{entity} AS bd_entity",
                f"{slice_expr} AS bd_slice",
                f"ROW_NUMBER() OVER (PARTITION BY {entity} ORDER BY {time_col} {order}) AS bd_rn",
                dialect=read,
            )
            .from_(source, dialect=read)
            .where(f"{time_col} >= '{start}'", dialect=read)
            .where(f"{time_col} < '{end_exclusive}'", dialect=read)
        )
        return (
            sqlglot.select("bd_entity", "bd_slice", dialect=read)
            .from_(ranked.subquery("bd_ranked"), dialect=read)
            .where("bd_rn = 1", dialect=read)
        )

    ref_slice = (
        f"CASE WHEN bd_ref.bd_entity IS NULL THEN '{ABSENT}' "
        f"ELSE COALESCE(bd_ref.bd_slice, '{_NULL_SLICE}') END"
    )
    an_slice = (
        f"CASE WHEN bd_an.bd_entity IS NULL THEN '{ABSENT}' "
        f"ELSE COALESCE(bd_an.bd_slice, '{_NULL_SLICE}') END"
    )
    return (
        sqlglot.select(
            f'{ref_slice} AS "reference_slice"',
            f'{an_slice} AS "analysis_slice"',
            'COUNT(*) AS "entities"',
            dialect=read,
        )
        .from_(window_cte(reference_start, reference_end).subquery("bd_ref"), dialect=read)
        .join(
            window_cte(analysis_start, analysis_end).subquery("bd_an"),
            on="bd_ref.bd_entity = bd_an.bd_entity",
            join_type="FULL OUTER",
            dialect=read,
        )
        .group_by("1", "2", dialect=read)
        .order_by("1", "2", dialect=read)
        .sql(dialect=dialect, pretty=True)
    )


def build_multivalue_assertion(
    bind: BindingSpec, *, dimension: str, grain: str, dialect: str = ""
) -> str:
    """Count (entity, period) pairs holding more than one value of `dimension`.

    Zero means the slices already sum and no resolution is needed; non-zero
    with `resolve: error` is the binding failing its own assertion. `doctor`
    runs it so the answer arrives at startup rather than at the first
    *slice by* click.
    """
    sqlglot = _require_sqlglot()
    read = _parse_dialect(dialect)
    fact = "bd_fact"
    spec = bind.entity_grain
    relation = (spec.relation if spec else None) or bind.relation
    source = f"({bind.sql}) AS {fact}" if (relation is None) else f"{relation} AS {fact}"
    time_col = _qualified(bind.time_column, fact)
    date_expr = _truncate(time_col, grain, dialect)
    entity = _qualified(bind.entity_key, fact)
    slice_expr = _qualified(bind.dimensions[dimension].column, fact)
    per_pair = (
        sqlglot.select(
            f"COUNT(DISTINCT {slice_expr}) AS bd_n_values",
            dialect=read,
        )
        .from_(source, dialect=read)
        .group_by(entity, date_expr, dialect=read)
    )
    return (
        sqlglot.select('COUNT(*) AS "multivalued_pairs"', dialect=read)
        .from_(per_pair.subquery("bd_pairs"), dialect=read)
        .where("bd_n_values > 1", dialect=read)
        .sql(dialect=dialect, pretty=True)
    )


def build_grain_assertion(bind: BindingSpec, *, dialect: str = "") -> str:
    """The grain-claim query: total rows vs. distinct `grain_key`.

    `doctor` fails a binding when these disagree, which turns silent fan-out —
    a join or a source that is not one row per grain, quietly multiplying every
    aggregate — into a startup error. MetricFlow and Cube cannot do this: they
    accept declared relationships on trust, so the same defect reaches the
    number. Owning the contract is what makes it checkable.
    """
    sqlglot = _require_sqlglot()
    fact = "bd_fact"
    source = f"({bind.sql}) AS {fact}" if bind.sql else f"{bind.relation} AS {fact}"
    key = _qualified(bind.grain_key, fact)
    return (
        sqlglot.select('COUNT(*) AS "rows"', f'COUNT(DISTINCT {key}) AS "distinct_keys"')
        .from_(source)
        .sql(dialect=dialect, pretty=True)
    )


# dbt adapter type -> sqlglot dialect. Only the mappings that differ in name or
# that we have verified are listed; an unknown adapter falls back to generic
# SQL, which is more useful than guessing a dialect's quirks.
ADAPTER_DIALECTS = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "spark": "spark",
    "trino": "trino",
    "athena": "athena",
    "clickhouse": "clickhouse",
}


def dialect_for_adapter(adapter_type: Optional[str]) -> str:
    if not adapter_type:
        return ""
    dialect = ADAPTER_DIALECTS.get(adapter_type.lower(), "")
    if not dialect:
        logger.warning(
            "No sqlglot dialect mapped for dbt adapter '%s'; emitting generic "
            "SQL. Week bucketing in particular may not be ISO-Monday.",
            adapter_type,
        )
    return dialect

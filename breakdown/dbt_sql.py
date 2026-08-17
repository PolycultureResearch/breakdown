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
#
# **BigQuery reverses the arguments.** Its signature is
# `DATE_TRUNC(date_expression, date_part)` — expression first, part as a bare
# keyword rather than a quoted string — the mirror of everyone else's
# `DATE_TRUNC('PART', expr)`. Since `_parse_dialect` reads in the *target*
# dialect, sqlglot never rewrote the portable form, so day and month grain
# emitted `DATE_TRUNC('DAY', col)` and BigQuery rejected the whole query with
# "No matching signature for function DATE_TRUNC". Week was usable only because
# its ISOWEEK override happened to be written the right way round. Every grain
# is therefore spelled out here, not just the ones whose *date part* differs.
#
# **The CAST is load-bearing, not decoration.** BigQuery's `DATE_TRUNC` takes a
# DATE; a TIMESTAMP needs `TIMESTAMP_TRUNC` and a DATETIME `DATETIME_TRUNC`, and
# a dbt `agg_time_dimension` is very often a TIMESTAMP — so the week override
# was wrong for the common case too. Choosing the right function would need the
# column's SQL type, and nothing on this path has it: `BindingSpec.time_column`
# is a bare string, the manifest's time dimension carries a `time_granularity`
# but no data type, and the value may be an arbitrary `expr` rather than a
# column name at all. `CAST(… AS DATE)` is correct for DATE, TIMESTAMP and
# DATETIME alike and needs nothing we do not have. It is also free of the two
# costs one might fear: partition pruning is unaffected, because the window
# predicates compare the *raw* column and not this expression; and UTC is the
# reference zone either way, since BigQuery's TIMESTAMP->DATE cast and
# `TIMESTAMP_TRUNC` both default to UTC, so no bucket differs from what the
# type-aware form would have produced.
_TRUNC_OVERRIDES = {
    ("bigquery", "day"): "DATE_TRUNC(CAST({col} AS DATE), DAY)",
    ("bigquery", "week"): "DATE_TRUNC(CAST({col} AS DATE), ISOWEEK)",
    ("bigquery", "month"): "DATE_TRUNC(CAST({col} AS DATE), MONTH)",
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


def _where_predicates(bind: BindingSpec, read: Optional[str], alias: str) -> list:
    """Parse, validate and alias-qualify `bind.where` for one query.

    The predicate is compiled through sqlglot's AST and never pasted as text,
    for two independent reasons.

    **Qualification.** `build_query` LEFT JOINs a dimension table when a slice
    is requested, so an unqualified `region` in a filter is ambiguous against
    `bd_dim.region` and resolves differently — or errors — depending on the
    warehouse. Textual prefixing cannot fix that, which is why `_qualified`
    already refuses to prefix anything that is not a lone identifier. Walking
    `exp.Column` nodes qualifies every column reference and nothing else.

    **Dialect.** A quoted `"date"` is an identifier on DuckDB and a string
    literal on Spark and BigQuery; `TRUNC(col, 'DAY')` returns NULL on Spark
    rather than erroring. This module's rule is *generate in the target
    dialect, never translate into it*, and parsing the stored predicate with
    the target dialect's own parser is the cheapest form of it.

    A predicate that does not parse, or that carries a subquery, a set
    operation or a second statement, raises `UnsupportedBinding`. The bridge
    already refused such a metric at import; this is the same check at the
    builder, for a binding that reached here any other way.
    """
    if not bind.where:
        return []
    sqlglot = _require_sqlglot()
    exp = sqlglot.expressions

    out = []
    for text in bind.where:
        try:
            statements = [s for s in sqlglot.parse(text, read=read) if s is not None]
        except Exception as e:
            raise UnsupportedBinding(
                f"filter predicate {text!r} does not parse as {read or 'generic'} SQL: {e}"
            ) from e
        if len(statements) != 1:
            raise UnsupportedBinding(
                f"filter predicate {text!r} is {len(statements)} statements, not one predicate."
            )
        tree = statements[0]
        forbidden = (exp.Select, exp.Subquery, exp.Union, exp.Except, exp.Intersect)
        if isinstance(tree, forbidden) or any(tree.find_all(*forbidden)):
            raise UnsupportedBinding(
                f"filter predicate {text!r} contains a subquery or set operation, "
                "which this generator does not compile."
            )
        for column in tree.find_all(exp.Column):
            if not column.table:
                column.set("table", exp.to_identifier(alias))
        out.append(tree)
    return out


def _filtered(query, bind: BindingSpec, read: Optional[str], alias: str = "bd_fact"):
    """Apply the binding's `where` to one query."""
    for predicate in _where_predicates(bind, read, alias):
        query = query.where(predicate, dialect=read)
    return query


def _windowed(query, bind: BindingSpec, read, start_date, end_date, alias: str = "bd_fact"):
    """Bound a query to a window, when one is given.

    Unbounded, the diagnostic queries scan the whole relation — fine on a few
    million rows, a full table scan per metric on a genuinely large fact table,
    and `doctor` runs several of them. A window turns each into a **sample**,
    which is a real change in what the check proves: fan-out and
    multi-valuedness are properties of the data, and absence over seven days is
    not proof of absence. Callers therefore report which window they used, so
    the result reads as "checked over these dates" rather than "checked".
    """
    if start_date is None or end_date is None:
        return query
    time_col = _qualified(bind.time_column, alias)
    start, end_exclusive = _window_bounds(start_date, end_date)
    return query.where(f"{time_col} >= '{start}'", dialect=read).where(
        f"{time_col} < '{end_exclusive}'", dialect=read
    )


def _bounded(query, bind: BindingSpec, read, start_date, end_date, alias: str = "bd_fact"):
    """The window **and** the binding's filter: the rows this node reads.

    Every builder routes through here, which is what makes *every diagnostic
    sees the same rows as the series* structural rather than remembered. The
    invariant is worth stating because breaking it is quiet: a filter applied
    to the total query but not to the sliced one produces slices that do not
    sum, which the slicing maths reads as an unexplained residual — a wrong
    *finding* rather than a wrong number, and harder to spot than either.

    The one caller that deliberately does not come through here is
    `build_filter_probe`, which measures the predicate and so must not apply it.
    """
    return _filtered(_windowed(query, bind, read, start_date, end_date, alias), bind, read, alias)


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
    query = _bounded(query, bind, read, start_date, end_date, fact)
    query = query.group_by(*group_by, dialect=read).order_by(*group_by, dialect=read)
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
    ranked = _bounded(
        sqlglot.select(
            f"{date_expr} AS bd_date",
            f"{slice_expr} AS bd_slice",
            f"ROW_NUMBER() OVER (PARTITION BY {entity}, {date_expr} "
            f"ORDER BY {time_col} {order}) AS bd_rn",
            dialect=read,
        ).from_(source, dialect=read),
        bind,
        read,
        start_date,
        end_date,
        fact,
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
    if spec.resolve == "error":
        # The slice query under `error` runs uncorrected, because the author
        # asserted single-valuedness per *period* and correcting silently would
        # defeat the point of choosing it. That assertion says nothing about
        # the choice this query needs: one representative row per entity per
        # *window*, and an entity whose slice legitimately changes mid-window
        # makes `first` and `last` different answers to a question the author
        # has not answered. Until 2026-08-17 `error` silently fell through the
        # ternary below and executed as `last` — the exact silent correction
        # the author opted out of. Refused instead; the flows panel degrades
        # to absent while the attribution stands.
        raise UnsupportedBinding(
            "entity flows need a per-window representative row, and "
            "`resolve: error` asserts a rule only per period. Declare "
            "`resolve: first` or `resolve: last` to enable flows."
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
        ranked = _bounded(
            sqlglot.select(
                f"{entity} AS bd_entity",
                f"{slice_expr} AS bd_slice",
                f"ROW_NUMBER() OVER (PARTITION BY {entity} ORDER BY {time_col} {order}) AS bd_rn",
                dialect=read,
            ).from_(source, dialect=read),
            bind,
            read,
            start_date,
            end_date,
            fact,
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
    bind: BindingSpec,
    *,
    dimension: str,
    grain: str,
    dialect: str = "",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
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
    per_pair = sqlglot.select(
        f"COUNT(DISTINCT {slice_expr}) AS bd_n_values",
        dialect=read,
    ).from_(source, dialect=read)
    per_pair = _bounded(per_pair, bind, read, start_date, end_date).group_by(
        entity, date_expr, dialect=read
    )
    return (
        sqlglot.select('COUNT(*) AS "multivalued_pairs"', dialect=read)
        .from_(per_pair.subquery("bd_pairs"), dialect=read)
        .where("bd_n_values > 1", dialect=read)
        .sql(dialect=dialect, pretty=True)
    )


def build_grain_assertion(
    bind: BindingSpec,
    *,
    dialect: str = "",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """The grain-claim query: total rows vs. distinct `grain_key`.

    `doctor` fails a binding when these disagree, which turns silent fan-out —
    a join or a source that is not one row per grain, quietly multiplying every
    aggregate — into a startup error. MetricFlow and Cube cannot do this: they
    accept declared relationships on trust, so the same defect reaches the
    number. Owning the contract is what makes it checkable.

    **The claim is made post-filter** (roadmap 2.17). Fan-out is a property of
    the relation, so filtering first is the *less* conservative check — but it
    is conservative in the wrong direction: a `fct_order_lines` relation
    filtered to `line_number = 1` is one row per order under this binding and
    multi-row without it, and a pre-filter pass would fail a binding whose every
    number is correct. The assertion exists to protect the aggregate this node
    computes, and that aggregate is the filtered one. What post-filter gives up
    — telling the author the relation is unsafe if the filter is ever widened —
    is covered by naming the predicate in the check's own output, and the thing
    it structurally cannot catch (a mis-translated predicate still leaves one
    row per grain key) is what `build_filter_probe` exists for.
    """
    sqlglot = _require_sqlglot()
    read = _parse_dialect(dialect)
    fact = "bd_fact"
    source = f"({bind.sql}) AS {fact}" if bind.sql else f"{bind.relation} AS {fact}"
    key = _qualified(bind.grain_key, fact)
    query = sqlglot.select(
        'COUNT(*) AS "rows"',
        f'COUNT(DISTINCT {key}) AS "distinct_keys"',
        dialect=read,
    ).from_(source, dialect=read)
    return _bounded(query, bind, read, start_date, end_date).sql(dialect=dialect, pretty=True)


def build_filter_probe(
    bind: BindingSpec,
    *,
    dialect: str = "",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """Kept-vs-total rows for a binding's `where`, over the probe window.

    ```sql
    SELECT COUNT(*)                                      AS "rows",
           SUM(CASE WHEN <predicate> THEN 1 ELSE 0 END)  AS "kept"
    FROM <relation> AS bd_fact
    WHERE <window bounds>
    ```

    This is the honest half of the confidence story for filters, and it is
    shaped like the grain claim for the same reason that made that one a
    differentiator: **it checks the data instead of trusting the metadata.**
    `0 < kept < rows` proves the predicate is live on *this* warehouse, in
    *this* dialect, against *these* columns. `kept == 0` is the signature of a
    dialect-hostile predicate — `= TRUE` against a VARCHAR, a date literal
    parsed as an identifier, a boolean stored as `'Y'` — and would serve an
    empty or all-zero series. `kept == rows` means the predicate excluded
    nothing, which is either genuinely vacuous over a short window or a
    constant-true expression, i.e. C15's original defect arriving through a new
    door.

    Deliberately *not* routed through `_bounded`: this query measures the
    predicate, so applying it would make the answer `kept == rows` by
    construction. Only the window bounds are applied, and `CASE WHEN` counts
    what the `WHERE` would have kept — a NULL predicate falls to `ELSE 0`,
    matching SQL's three-valued `WHERE` semantics exactly.

    What it does not prove is that our row set is MetricFlow's; `kept/rows =
    0.31` says the filter is doing something, not that it is doing the right
    thing. That is roadmap 2.14.
    """
    if not bind.where:
        raise UnsupportedBinding("binding declares no `where` to probe")
    sqlglot = _require_sqlglot()
    exp = sqlglot.expressions
    read = _parse_dialect(dialect)
    fact = "bd_fact"
    source = f"({bind.sql}) AS {fact}" if bind.sql else f"{bind.relation} AS {fact}"

    predicates = _where_predicates(bind, read, fact)
    combined = predicates[0]
    for extra in predicates[1:]:
        combined = exp.and_(combined, extra)
    kept = exp.case().when(combined, exp.Literal.number(1)).else_(exp.Literal.number(0))

    query = sqlglot.select(
        'COUNT(*) AS "rows"',
        exp.alias_(exp.Sum(this=kept), "kept", quoted=True),
        dialect=read,
    ).from_(source, dialect=read)
    return _windowed(query, bind, read, start_date, end_date, fact).sql(
        dialect=dialect, pretty=True
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

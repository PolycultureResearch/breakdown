"""The binding SQL generator (roadmap 2.10).

Most of these tests **execute** the generated SQL against DuckDB rather than
matching strings. A query builder that emits plausible-looking SQL with the
wrong window bound or the wrong week boundary is exactly the failure class the
engine exists to avoid, and only running it catches that.

Dialect-specific expectations (BigQuery, Snowflake, Databricks) have no engine
here to run against. Where it matters they are asserted on the *parse tree* the
dialect's own parser produces — `sqlglot.parse_one(sql, dialect=...)` and then
assertions on node types and argument positions — which is the closest thing to
a warehouse validator available offline. Text matching is the blind spot that
let three dialect defects reach a real warehouse; a string that "looks right"
in one dialect's grammar is wrong in another's, and only a parse can tell.
"""

import pandas as pd
import pytest

from breakdown.dbt_sql import (
    UnsupportedBinding,
    build_entity_flow_query,
    build_filter_probe,
    build_grain_assertion,
    build_multivalue_assertion,
    build_query,
    build_resolved_slice_query,
    dialect_for_adapter,
)
from breakdown.parser import BindingDimension, BindingSpec

sqlglot = pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")
exp = sqlglot.expressions
duckdb = pytest.importorskip("duckdb")


ORDERS = """
CREATE TABLE fct_orders AS SELECT * FROM (VALUES
    (1, DATE '2024-01-01', 10.0, 'EMEA', 'c1'),   -- Mon, week 1
    (2, DATE '2024-01-03', 20.0, 'EMEA', 'c1'),   -- Wed, week 1
    (3, DATE '2024-01-07', 40.0, 'AMER', 'c2'),   -- SUNDAY, still ISO week 1
    (4, DATE '2024-01-08', 80.0, 'AMER', 'c2'),   -- Mon, week 2
    (5, DATE '2024-01-31', 1.0,  'EMEA', 'c3')    -- last day of the window
) AS t(order_id, ordered_at, amount, region, customer_id);
"""

CUSTOMERS = """
CREATE TABLE dim_customers AS SELECT * FROM (VALUES
    ('c1', 'enterprise'), ('c2', 'smb'), ('c3', 'enterprise')
) AS t(customer_id, plan_tier);
"""


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute(ORDERS)
    c.execute(CUSTOMERS)
    return c


def _bind(**over):
    base = dict(
        relation="fct_orders",
        grain_key="order_id",
        time_column="ordered_at",
        agg="sum",
        measure="amount",
    )
    base.update(over)
    return BindingSpec(**base)


def _run(con, bind, **kw):
    kw.setdefault("start_date", "2024-01-01")
    kw.setdefault("end_date", "2024-01-31")
    kw.setdefault("dialect", "duckdb")
    sql = build_query(bind, **kw)
    df = con.execute(sql).df()
    df["date"] = pd.to_datetime(df["date"])
    return df


# --- the output contract ----------------------------------------------------


def test_returns_date_and_value_at_day_grain(con):
    df = _run(con, _bind(), grain="day")
    assert list(df.columns) == ["date", "value"]
    assert df.loc[df["date"] == "2024-01-01", "value"].item() == 10.0
    # No spine filling here — that is `_align_to_spine`'s job, and doing it in
    # SQL too would double-fill.
    assert len(df) == 5


def test_sliced_query_returns_date_slice_value(con):
    bind = _bind(dimensions={"region": BindingDimension(column="region")})
    df = _run(con, bind, grain="day", dimension="region")
    assert list(df.columns) == ["date", "slice", "value"]
    assert set(df["slice"]) == {"EMEA", "AMER"}


def test_slices_sum_to_the_unsliced_total(con):
    bind = _bind(dimensions={"region": BindingDimension(column="region")})
    total = _run(con, bind, grain="month")["value"].sum()
    sliced = _run(con, bind, grain="month", dimension="region")["value"].sum()
    assert total == sliced == 151.0


# --- the window bound -------------------------------------------------------


def test_the_last_day_of_the_window_is_included(con):
    # The bound is half-open on end+1day precisely so this row survives. A
    # `<= end_date` bound drops everything after midnight on the last day,
    # silently losing ~1/31 of a monthly figure against a timestamp column.
    df = _run(con, _bind(), grain="month")
    assert df["value"].item() == 151.0


def test_rows_outside_the_window_are_excluded(con):
    df = _run(con, _bind(), grain="month", start_date="2024-01-02", end_date="2024-01-30")
    assert df["value"].item() == 140.0  # drops the 10.0 on the 1st and 1.0 on the 31st


def test_a_timestamp_column_keeps_the_whole_last_day(con):
    con.execute(
        "CREATE TABLE events AS SELECT * FROM (VALUES "
        "(1, TIMESTAMP '2024-01-31 23:59:00', 5.0)) AS t(id, event_at, v)"
    )
    bind = _bind(relation="events", grain_key="id", time_column="event_at", measure="v")
    assert _run(con, bind, grain="month")["value"].item() == 5.0


# --- week boundaries, the silent one ----------------------------------------


def test_weeks_are_iso_monday_so_sunday_belongs_to_the_week_before(con):
    # 2024-01-07 is a Sunday. Under ISO it closes the week that began Monday
    # the 1st; under a Sunday-start calendar it would open the next one. Both
    # produce one label per week, so nothing downstream can tell them apart —
    # which is why this is asserted against a real engine.
    df = _run(con, _bind(), grain="week").set_index("date")["value"]
    assert df[pd.Timestamp("2024-01-01")] == 70.0  # 10 + 20 + 40 (incl. Sunday)
    assert df[pd.Timestamp("2024-01-08")] == 80.0


def test_bigquery_uses_isoweek_not_its_sunday_default():
    sql = build_query(
        _bind(), grain="week", start_date="2024-01-01", end_date="2024-01-31", dialect="bigquery"
    )
    assert "ISOWEEK" in sql


def test_snowflake_week_does_not_depend_on_the_session_parameter():
    # Snowflake's DATE_TRUNC('week', …) honours WEEK_START, so the same query
    # returns different buckets for different users. DAYOFWEEKISO is Monday=1
    # regardless of session state.
    sql = build_query(
        _bind(), grain="week", start_date="2024-01-01", end_date="2024-01-31", dialect="snowflake"
    )
    assert "DAYOFWEEKISO" in sql and "WEEK" not in sql.replace("DAYOFWEEKISO", "")


@pytest.mark.parametrize("dialect", ["databricks", "spark"])
def test_databricks_day_grain_uses_date_trunc_not_trunc(dialect):
    # Spark's TRUNC(col, fmt) accepts only YEAR/MONTH/WEEK/QUARTER and returns
    # NULL for DAY rather than erroring — so translating a portable
    # DATE_TRUNC('DAY', …) into Databricks collapsed every row into one
    # NULL-labelled bucket holding the whole window's total. Found by running
    # the generated SQL against a real Databricks warehouse; no local engine
    # could see it, because sqlglot transpiled it without complaint.
    sql = build_query(
        _bind(), grain="day", start_date="2024-01-01", end_date="2024-01-31", dialect=dialect
    )
    assert "DATE_TRUNC('DAY'" in sql
    assert "TRUNC(bd_fact.ordered_at, 'DAY')" not in sql


@pytest.mark.parametrize(
    "dialect", ["", "duckdb", "postgres", "databricks", "spark", "bigquery", "snowflake", "trino"]
)
@pytest.mark.parametrize("grain", ["day", "week", "month"])
def test_every_dialect_and_grain_emits_a_date_expression(dialect, grain):
    sql = build_query(
        _bind(), grain=grain, start_date="2024-01-01", end_date="2024-01-31", dialect=dialect
    )
    # Every dialect must bucket the real time column and alias the result
    # `date`. The expression that does it differs per dialect, so assert on the
    # whole statement — a naive per-line search matches Snowflake's `DATEADD`.
    assert "bd_fact.ordered_at" in sql
    assert 'AS "date"' in sql or "AS `date`" in sql


# --- truncation, asserted on the parse tree rather than the characters ------
#
# `DATE_TRUNC` is where the dialects disagree hardest, and text assertions have
# now missed the disagreement three times. **BigQuery's signature is
# `DATE_TRUNC(date_expression, date_part)`** — the expression first, the part as
# a bare keyword — which is the mirror of everyone else's
# `DATE_TRUNC('PART', expr)`. Because the builder parses in the *target*
# dialect, sqlglot never rewrote the portable form, so day and month grain
# emitted `DATE_TRUNC('DAY', col)` and BigQuery rejected every such query with
# "No matching signature for function DATE_TRUNC". `"DATE_TRUNC" in sql` was
# true the whole time.
#
# So these parse the generated SQL with the dialect's own parser and assert on
# structure: which argument holds the date expression, which holds the part.


def _truncation(sql: str, dialect: str, expected: int = 1):
    """The truncation call(s) in `sql`, as the dialect's own parser reads them.

    sqlglot models a truncation as `TimestampTrunc`/`DateTrunc` with `.this`
    (the date expression) and `.unit` (the date part) — *semantic* roles, filled
    positionally per dialect. That is precisely the axis the bug is on, so it is
    the axis to assert.
    """
    tree = sqlglot.parse_one(sql, dialect=dialect)
    calls = list(tree.find_all(exp.DateTrunc, exp.TimestampTrunc))
    assert len(calls) == expected, f"expected {expected} truncation call(s), got {len(calls)}"
    return calls


def _bq(grain: str) -> str:
    return build_query(
        _bind(),
        grain=grain,
        start_date="2024-01-01",
        end_date="2024-01-31",
        dialect="bigquery",
    )


def test_the_bigquery_parser_really_can_tell_the_broken_form_from_the_fixed_one():
    # The premise of everything below: a round trip through sqlglot's BigQuery
    # parser is only worth writing if it *distinguishes* the defect. It does,
    # and for the same reason BigQuery itself rejects the query — the grammar is
    # positional, so argument 1 is read as the date expression whatever it is.
    broken = _truncation("SELECT DATE_TRUNC('MONTH', bd_fact.ordered_at) AS d", "bigquery")[0]
    # A string literal lands where the date belongs...
    assert isinstance(broken.this, exp.Literal) and broken.this.this == "MONTH"
    # ...and the column lands where the date part belongs. That mismatched pair
    # is exactly what "No matching signature" names, and it is what the
    # assertions below detect.
    assert broken.unit.name.upper() == "ORDERED_AT"

    fixed = _truncation("SELECT DATE_TRUNC(bd_fact.ordered_at, MONTH) AS d", "bigquery")[0]
    assert isinstance(fixed.this, exp.Column)
    assert fixed.unit.name.upper() == "MONTH"


@pytest.mark.parametrize("grain, part", [("day", "DAY"), ("week", "ISOWEEK"), ("month", "MONTH")])
def test_bigquery_truncation_puts_the_date_first_and_the_part_second(grain, part):
    call = _truncation(_bq(grain), "bigquery")[0]
    # Argument 1 is a date expression over the real time column — not a string.
    assert not isinstance(call.this, exp.Literal)
    assert {c.name for c in call.this.find_all(exp.Column)} == {"ordered_at"}
    # Argument 2 is the date part, and the right one. ISOWEEK rather than WEEK
    # matters independently: BigQuery's WEEK starts on Sunday.
    assert call.unit.name.upper() == part


@pytest.mark.parametrize("grain", ["day", "week", "month"])
def test_bigquery_truncates_a_date_whatever_the_column_type_is(grain):
    # BigQuery's DATE_TRUNC takes a DATE; a TIMESTAMP needs TIMESTAMP_TRUNC and
    # a DATETIME needs DATETIME_TRUNC. A dbt `agg_time_dimension` is very often
    # a TIMESTAMP, and nothing on this path knows which it is — `time_column` is
    # a bare string and the manifest carries a granularity but no data type. The
    # CAST is what makes one expression right for all three.
    call = _truncation(_bq(grain), "bigquery")[0]
    assert isinstance(call.this, exp.Cast)
    assert call.this.to.this == exp.DataType.Type.DATE


@pytest.mark.parametrize("grain", ["day", "week", "month"])
def test_the_bigquery_cast_stays_out_of_the_window_predicates(grain):
    # The window compares the *raw* column, deliberately: casting there would
    # cost partition pruning on the one clause where it is worth having, and
    # `_bounded` builds the same predicates for the diagnostic queries. So the
    # cast must be confined to the truncation.
    tree = sqlglot.parse_one(_bq(grain), dialect="bigquery")
    bounds = list(tree.find(exp.Where).find_all(exp.GTE, exp.LT))
    assert len(bounds) == 2
    for bound in bounds:
        assert isinstance(bound.this, exp.Column)
        assert bound.this.name == "ordered_at"


@pytest.mark.parametrize(
    "build",
    [
        lambda d: build_resolved_slice_query(
            _dau("last"),
            dimension="status",
            grain="month",
            start_date="2024-01-01",
            end_date="2024-01-02",
            dialect=d,
        ),
        lambda d: build_multivalue_assertion(
            _dau("last"), dimension="status", grain="month", dialect=d
        ),
    ],
)
def test_every_builder_that_truncates_gets_the_bigquery_form(build):
    # `_truncate` feeds three builders, not just `build_query`. A fix that
    # reached only the query the UI runs would still leave `doctor`'s
    # multi-value assertion and the resolved-slice query malformed on BigQuery.
    tree = sqlglot.parse_one(build("bigquery"), dialect="bigquery")
    calls = list(tree.find_all(exp.DateTrunc, exp.TimestampTrunc))
    assert calls, "expected this builder to truncate the time column"
    for call in calls:
        assert isinstance(call.this, exp.Cast)
        assert call.unit.name.upper() == "MONTH"


@pytest.mark.parametrize(
    "dialect", ["duckdb", "postgres", "databricks", "spark", "snowflake", "trino"]
)
@pytest.mark.parametrize("grain, part", [("day", "DAY"), ("week", "WEEK"), ("month", "MONTH")])
def test_other_dialects_keep_the_portable_argument_order(dialect, grain, part):
    # The regression guard in the other direction: BigQuery's order and its cast
    # are a BigQuery override, and a future "fix" that generalised either would
    # break every warehouse that reads `DATE_TRUNC('PART', expr)`. Here the part
    # is argument 1 and the bare column is argument 2, with no cast.
    if dialect == "snowflake" and grain == "week":
        pytest.skip("Snowflake's ISO week is a DATEADD offset, not a truncation")
    sql = build_query(
        _bind(), grain=grain, start_date="2024-01-01", end_date="2024-01-31", dialect=dialect
    )
    call = _truncation(sql, dialect)[0]
    assert call.unit.name.upper() == part
    assert isinstance(call.this, exp.Column)
    assert call.this.name == "ordered_at"


def test_month_grain_buckets_to_the_first(con):
    df = _run(con, _bind(), grain="month")
    assert df["date"].item() == pd.Timestamp("2024-01-01")


# --- aggregation semantics --------------------------------------------------


def test_count_is_null_guarded_not_count_star(con):
    # MetricFlow's `count` desugars to SUM(CASE WHEN x IS NOT NULL THEN 1 ELSE 0
    # END). COUNT(x) is exactly that; COUNT(*) would include rows the source
    # deliberately excludes.
    con.execute(
        "CREATE TABLE partial AS SELECT * FROM (VALUES "
        "(1, DATE '2024-01-01', 5.0), (2, DATE '2024-01-01', NULL)) AS t(id, d, v)"
    )
    bind = _bind(relation="partial", grain_key="id", time_column="d", agg="count", measure="v")
    assert _run(con, bind, grain="day")["value"].item() == 1
    assert "COUNT(*)" not in build_query(
        bind, grain="day", start_date="2024-01-01", end_date="2024-01-31", dialect="duckdb"
    )


def test_count_distinct_deduplicates(con):
    bind = _bind(agg="count_distinct", measure="customer_id", entity_key="customer_id")
    # c1 twice, c2 twice, c3 once -> 3 distinct customers over the month.
    assert _run(con, bind, grain="month")["value"].item() == 3


def test_average_aggregates_as_a_mean(con):
    bind = _bind(agg="average")
    assert _run(con, bind, grain="month")["value"].item() == pytest.approx(151.0 / 5)


def test_ratio_divides_two_separately_aggregated_measures(con):
    con.execute(
        "CREATE TABLE funnel AS SELECT * FROM (VALUES "
        "(1, DATE '2024-01-01', 1, 4), (2, DATE '2024-01-02', 2, 6)) AS t(id, d, conv, sess)"
    )
    bind = BindingSpec(
        relation="funnel",
        grain_key="id",
        time_column="d",
        agg="ratio",
        numerator="conv",
        denominator="sess",
    )
    assert _run(con, bind, grain="month")["value"].item() == pytest.approx(3 / 10)


def test_a_zero_denominator_yields_null_not_a_number(con):
    # An undefined rate must stay undefined: NULL reaches `_align_to_spine`,
    # which refuses to gap-fill a rate. Returning 0 would invent a value.
    con.execute(
        "CREATE TABLE empty_funnel AS SELECT * FROM (VALUES "
        "(1, DATE '2024-01-01', 0, 0)) AS t(id, d, conv, sess)"
    )
    bind = BindingSpec(
        relation="empty_funnel",
        grain_key="id",
        time_column="d",
        agg="ratio",
        numerator="conv",
        denominator="sess",
    )
    assert pd.isna(_run(con, bind, grain="month")["value"].item())


# --- joins ------------------------------------------------------------------


def test_many_to_one_join_slices_by_a_conformed_dimension(con):
    bind = _bind(
        dimensions={
            "plan": BindingDimension(column="plan_tier", join="dim_customers", key="customer_id")
        }
    )
    df = _run(con, bind, grain="month", dimension="plan").set_index("slice")["value"]
    assert df["enterprise"] == 31.0  # c1 (10+20) + c3 (1)
    assert df["smb"] == 120.0  # c2 (40+80)
    assert df.sum() == 151.0  # a many-to-one join cannot fan out


def test_join_key_may_differ_on_the_two_sides(con):
    con.execute("CREATE TABLE dim_alt AS SELECT customer_id AS cid, plan_tier FROM dim_customers")
    bind = _bind(
        dimensions={
            "plan": BindingDimension(
                column="plan_tier", join="dim_alt", key="customer_id", to="cid"
            )
        }
    )
    assert _run(con, bind, grain="month", dimension="plan")["value"].sum() == 151.0


def test_unknown_dimension_is_refused(con):
    with pytest.raises(UnsupportedBinding, match="declares no dimension"):
        build_query(
            _bind(), grain="day", start_date="2024-01-01", end_date="2024-01-31", dimension="nope"
        )


# --- the escape hatch and the grain assertion -------------------------------


def test_inline_sql_relation_is_used_as_a_subquery(con):
    bind = _bind(relation=None, sql="SELECT * FROM fct_orders WHERE region = 'EMEA'")
    assert _run(con, bind, grain="month")["value"].item() == 31.0


def test_expression_measures_are_not_textually_qualified(con):
    # A bare column gets the fact alias; a real expression must be left alone,
    # since prefixing it would corrupt it.
    bind = _bind(measure="amount * 2")
    assert _run(con, bind, grain="month")["value"].item() == 302.0


def test_grain_assertion_passes_on_a_clean_relation(con):
    rows, distinct = con.execute(build_grain_assertion(_bind(), dialect="duckdb")).fetchone()
    assert rows == distinct == 5


def test_grain_assertion_catches_fan_out(con):
    # A relation that is not one row per grain_key multiplies every aggregate
    # silently. This is the check MetricFlow and Cube do not do.
    bind = _bind(relation=None, sql="SELECT o.* FROM fct_orders o, (VALUES (1),(2)) AS d(n)")
    rows, distinct = con.execute(build_grain_assertion(bind, dialect="duckdb")).fetchone()
    assert rows == 10 and distinct == 5


# --- refusals ---------------------------------------------------------------


def test_last_is_refused_rather_than_approximated():
    with pytest.raises(UnsupportedBinding, match="last-snapshot window"):
        build_query(_bind(agg="last"), grain="day", start_date="2024-01-01", end_date="2024-01-31")


def test_unknown_grain_is_rejected():
    with pytest.raises(ValueError, match="grain must be one of"):
        build_query(_bind(), grain="quarter", start_date="2024-01-01", end_date="2024-01-31")


def test_adapter_dialects_map_and_unknown_falls_back_to_generic(caplog):
    assert dialect_for_adapter("databricks") == "databricks"
    assert dialect_for_adapter("BigQuery") == "bigquery"
    assert dialect_for_adapter(None) == ""
    with caplog.at_level("WARNING"):
        assert dialect_for_adapter("exasol") == ""
    assert "generic" in caplog.text


# --- entity-grain resolution (roadmap 3.8 §4) -------------------------------
#
# The claim these tests exist to prove is a numeric one: resolved slices sum
# back to the metric *exactly*, where the naive ones overstate it. Asserting
# that on generated SQL strings would prove nothing, so they run.


EVENTS = """
CREATE TABLE ev AS SELECT * FROM (VALUES
    -- u1 is 'active' in the morning and 'cancelled' by evening on day 1:
    -- one entity holding two values of the dimension inside one period.
    (1, 'u1', TIMESTAMP '2024-01-01 09:00', 'active'),
    (2, 'u1', TIMESTAMP '2024-01-01 18:00', 'cancelled'),
    (3, 'u2', TIMESTAMP '2024-01-01 10:00', 'active'),
    (4, 'u1', TIMESTAMP '2024-01-02 10:00', 'cancelled'),
    (5, 'u2', TIMESTAMP '2024-01-02 11:00', 'active')
) AS t(row_id, user_id, seen_at, status);
"""

WINDOW = dict(grain="day", start_date="2024-01-01", end_date="2024-01-02", dialect="duckdb")


@pytest.fixture
def events():
    c = duckdb.connect()
    c.execute(EVENTS)
    return c


def _dau(resolve=None):
    kw = dict(
        relation="ev",
        grain_key="row_id",
        time_column="seen_at",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        dimensions={"status": BindingDimension(column="status")},
    )
    if resolve is not None:
        kw["entity_grain"] = {"resolve": resolve}
    return BindingSpec(**kw)


def test_naive_slices_overstate_a_distinct_count(events):
    total = events.execute(build_query(_dau(), **WINDOW)).df()["value"].sum()
    naive = events.execute(build_query(_dau(), **WINDOW, dimension="status")).df()["value"].sum()
    assert total == 4  # 2 distinct users on each of 2 days
    assert naive == 5  # u1 counted in both statuses on day 1


@pytest.mark.parametrize("resolve", ["first", "last"])
def test_resolved_slices_sum_to_the_metric_exactly(events, resolve):
    total = events.execute(build_query(_dau(), **WINDOW)).df()["value"].sum()
    resolved = events.execute(
        build_resolved_slice_query(_dau(resolve), dimension="status", **WINDOW)
    ).df()
    assert resolved["value"].sum() == total


def test_first_and_last_give_different_answers(events):
    # Which is right is a business question, which is why neither is a default.
    def by_slice(resolve):
        df = events.execute(
            build_resolved_slice_query(_dau(resolve), dimension="status", **WINDOW)
        ).df()
        day1 = df[df["date"].astype(str).str.startswith("2024-01-01")]
        return dict(zip(day1["slice"], day1["value"]))

    assert by_slice("first") == {"active": 2}
    assert by_slice("last") == {"active": 1, "cancelled": 1}


def test_resolve_error_does_not_silently_correct(events):
    # `error` asserts the data is already single-valued. Correcting under it
    # would defeat the point of choosing it; doctor is what proves it.
    assert (
        events.execute(build_resolved_slice_query(_dau("error"), dimension="status", **WINDOW))
        .df()["value"]
        .sum()
        == 5
    )


def test_the_multivalue_assertion_counts_offending_pairs(events):
    sql = build_multivalue_assertion(
        _dau("last"), dimension="status", grain="day", dialect="duckdb"
    )
    assert events.execute(sql).fetchone()[0] == 1  # u1 on day 1


def test_resolution_needs_an_entity_grain_block():
    with pytest.raises(UnsupportedBinding, match="no `entity_grain`"):
        build_resolved_slice_query(_dau(), dimension="status", **WINDOW)


def test_a_joined_dimension_is_refused_rather_than_resolved_after_the_join():
    bind = BindingSpec(
        relation="ev",
        grain_key="row_id",
        time_column="seen_at",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        entity_grain={"resolve": "last"},
        dimensions={"plan": BindingDimension(column="tier", join="dim_users", key="user_id")},
    )
    with pytest.raises(UnsupportedBinding, match="reached through a join"):
        build_resolved_slice_query(bind, dimension="plan", **WINDOW)


@pytest.mark.parametrize("dialect", ["databricks", "spark", "bigquery", "duckdb", "snowflake"])
def test_resolved_query_never_emits_a_quoted_identifier_as_a_column_ref(dialect):
    # A quoted `"date"` is an identifier on DuckDB and Postgres but a **string
    # literal** on Spark and BigQuery. Referencing one in the outer SELECT
    # produced a constant column of the word "date" on Databricks — every row
    # unparseable as a date, and invisible to every local test because DuckDB
    # reads it the other way. Found by running it against a real warehouse.
    sql = build_resolved_slice_query(
        _dau("last"),
        dimension="status",
        grain="day",
        start_date="2024-01-01",
        end_date="2024-01-02",
        dialect=dialect,
    )
    assert "'date'" not in sql
    assert "'slice'" not in sql
    assert "'value'" not in sql


@pytest.mark.parametrize("dialect", ["databricks", "bigquery"])
def test_resolved_query_still_aliases_the_output_columns(dialect):
    # The public names must survive as *aliases* on the dialects where the
    # quoting differs — the fix must not have dropped them.
    sql = build_resolved_slice_query(
        _dau("last"),
        dimension="status",
        grain="day",
        start_date="2024-01-01",
        end_date="2024-01-02",
        dialect=dialect,
    )
    for col in ("date", "slice", "value"):
        assert f"`{col}`" in sql


# --- window bounds are the one path that takes untrusted input --------------


@pytest.mark.parametrize(
    "hostile",
    [
        "2024-01-01' OR 1=1 --",
        "2024-01-01; DROP TABLE orders",
        "2024-01-01') UNION SELECT 1,2 --",
        "nonsense",
        "",
    ],
)
def test_window_bounds_reject_anything_that_is_not_a_date(hostile):
    # Window dates arrive from API requests — `POST /rca/{name}/slices` takes
    # them from the client — and are interpolated into generated SQL. Every
    # other interpolated value comes from the tree or the manifest, which the
    # author controls; these do not.
    #
    # They are safe today because `pd.Timestamp(...).strftime()` cannot round
    # trip anything that is not a date, but that safety is a *side effect* of
    # normalising to period starts. This test states it as a requirement, so
    # that "optimising" the bounds to pass strings straight through fails here
    # instead of shipping an injection on the one untrusted path.
    from breakdown.dbt_sql import _window_bounds

    with pytest.raises(Exception):
        _window_bounds(hostile, "2024-01-31")
    with pytest.raises(Exception):
        _window_bounds("2024-01-01", hostile)


def test_window_bounds_normalise_to_bare_dates():
    from breakdown.dbt_sql import _window_bounds

    # Whatever a caller passes, what reaches the SQL is a plain YYYY-MM-DD.
    start, end = _window_bounds("2024-01-01T13:45:00", "2024-01-31")
    assert (start, end) == ("2024-01-01", "2024-02-01")


# --- diagnostic queries are bounded, so they sample rather than full-scan ----


def test_assertions_scan_the_whole_relation_when_unbounded():
    # Explicit, because it is the behaviour `doctor` deliberately no longer uses.
    assert "WHERE" not in build_grain_assertion(_dau("last"), dialect="duckdb")


@pytest.mark.parametrize(
    "build",
    [
        lambda b, **kw: build_grain_assertion(b, dialect="duckdb", **kw),
        lambda b, **kw: build_multivalue_assertion(
            b, dimension="status", grain="day", dialect="duckdb", **kw
        ),
    ],
)
def test_a_window_bounds_the_scan(build):
    # `doctor` runs two of these per metric; unbounded they are full table scans
    # on a large fact table. The window makes each a sample — which is why the
    # doctor result names the dates it checked.
    sql = build(_dau("last"), start_date="2024-01-01", end_date="2024-01-07")
    assert "WHERE" in sql
    assert "2024-01-08" in sql  # half-open upper bound, as everywhere else


def test_a_bounded_grain_assertion_still_detects_fan_out(events):
    bind = _dau("last")
    unbounded = events.execute(build_grain_assertion(bind, dialect="duckdb")).fetchone()
    bounded = events.execute(
        build_grain_assertion(
            bind, dialect="duckdb", start_date="2024-01-01", end_date="2024-01-02"
        )
    ).fetchone()
    assert unbounded[0] == 5 and unbounded[1] == 5
    assert bounded[0] == 5 and bounded[1] == 5  # the fixture is inside the window


# --- filters (roadmap 2.17) --------------------------------------------------
#
# The claim a filter makes is numeric — *fewer rows, smaller total* — so these
# run. A predicate that compiles, executes, and quietly matches everything is
# exactly the `kept == rows` case `build_filter_probe` exists to catch, and it
# would pass every string assertion anyone could write.

WHOLE_JANUARY = dict(start_date="2024-01-01", end_date="2024-01-31", dialect="duckdb")


def _emea(**over):
    return _bind(where=["region = 'EMEA'"], **over)


def test_a_filter_actually_narrows_both_the_row_count_and_the_total(con):
    # The fixture is 5 orders totalling 151.0; three of them are EMEA, totalling
    # 31.0. Both numbers have to move, and in the right direction.
    everything = _run(con, _bind(), grain="day")
    filtered = _run(con, _emea(), grain="day")
    assert (len(everything), everything["value"].sum()) == (5, 151.0)
    assert (len(filtered), filtered["value"].sum()) == (3, 31.0)


def test_several_predicates_are_anded_not_ored(con):
    bind = _bind(where=["region = 'EMEA'", "amount > 5"])
    assert _run(con, bind, grain="month")["value"].item() == 30.0  # drops the 1.0


def test_a_filter_that_matches_nothing_returns_no_rows_rather_than_zero(con):
    # `_align_to_spine` is what decides whether an absent period is a zero; the
    # query must not decide it by emitting a row.
    assert _run(con, _bind(where=["region = 'NOWHERE'"]), grain="day").empty


def test_slices_of_a_filtered_metric_sum_to_the_filtered_total(con):
    # A WHERE is a row-level test evaluated independently of the GROUP BY, so
    # filtering and grouping commute and the partition still partitions. This is
    # the invariant slice attribution rests on, and it is cheap to prove.
    bind = _emea(dimensions={"plan": BindingDimension(column="customer_id")})
    total = _run(con, bind, grain="month")["value"].item()
    sliced = _run(con, bind, grain="month", dimension="plan")["value"].sum()
    assert total == sliced == 31.0


def test_the_filter_is_qualified_against_the_fact_relation_under_a_join(con):
    # `build_query` LEFT JOINs a dimension table when a slice is requested, so
    # an unqualified `region` in a filter is ambiguous against `bd_dim.region` —
    # DuckDB refuses the query outright, and other warehouses resolve it
    # differently. Textual prefixing cannot fix this, which is why the predicate
    # is qualified by walking its columns.
    con.execute(
        "CREATE TABLE dim_regional AS SELECT customer_id, plan_tier, "
        "'AMER' AS region FROM dim_customers"
    )
    bind = _emea(
        dimensions={
            "plan": BindingDimension(column="plan_tier", join="dim_regional", key="customer_id")
        }
    )
    df = _run(con, bind, grain="month", dimension="plan").set_index("slice")["value"]
    # Every row's *dimension-table* region is AMER, so a predicate resolved
    # against the join would have kept nothing. 31.0 says it resolved against
    # the fact.
    assert df.sum() == 31.0


def test_the_grain_claim_is_made_over_the_filtered_rows(con):
    # Spec §3.4, and the reason pre-filter is the wrong kind of conservative: a
    # line-level relation is one row per order *under* `line_number = 1` and
    # multi-row without it. A pre-filter assertion would fail a binding whose
    # every number is correct.
    con.execute(
        "CREATE TABLE fct_order_lines AS SELECT * FROM (VALUES "
        "(1, 1, DATE '2024-01-01', 10.0), (1, 2, DATE '2024-01-01', 5.0), "
        "(2, 1, DATE '2024-01-02', 20.0)) AS t(order_id, line_number, ordered_at, amount)"
    )
    unfiltered = _bind(relation="fct_order_lines")
    filtered = _bind(relation="fct_order_lines", where=["line_number = 1"])
    assert con.execute(build_grain_assertion(unfiltered, dialect="duckdb")).fetchone() == (3, 2)
    assert con.execute(build_grain_assertion(filtered, dialect="duckdb")).fetchone() == (2, 2)


def test_the_multivalue_assertion_sees_the_rows_the_node_reads(events):
    # Otherwise it asserts multi-valuedness over rows the node never aggregates,
    # and reports a resolution problem the series cannot have.
    unfiltered = _dau("last")
    filtered = _dau("last")
    filtered = filtered.model_copy(update={"where": ["status = 'active'"]})
    build = dict(dimension="status", grain="day", dialect="duckdb")
    assert events.execute(build_multivalue_assertion(unfiltered, **build)).fetchone()[0] == 1
    assert events.execute(build_multivalue_assertion(filtered, **build)).fetchone()[0] == 0


def test_entity_flows_are_classified_against_the_filtered_population(events):
    # A user leaving the filter's scope is a churn, which is only true if the
    # window CTEs see the filtered rows.
    bind = _dau("last").model_copy(update={"where": ["status = 'active'"]})
    rows = events.execute(
        build_entity_flow_query(
            bind,
            dimension="status",
            reference_start="2024-01-01",
            reference_end="2024-01-01",
            analysis_start="2024-01-02",
            analysis_end="2024-01-02",
            dialect="duckdb",
        )
    ).fetchall()
    # u1 is 'cancelled' on day 2, so under the filter it is absent there: a
    # churn. u2 is active on both days: retained.
    assert sorted(rows) == [("active", "__absent__", 1), ("active", "active", 1)]


# --- the filter probe, and the two answers that are not "fine" ---------------


def _probe(con, bind, **kw):
    return con.execute(build_filter_probe(bind, dialect="duckdb", **kw)).fetchone()


def test_the_probe_counts_kept_against_total(con):
    assert _probe(con, _emea()) == (5, 3)


def test_the_probe_catches_a_predicate_that_excludes_everything(con):
    # The signature of a dialect-hostile predicate — `= TRUE` against a VARCHAR,
    # a date literal parsed as an identifier, a boolean stored as 'Y'. The node
    # would serve an empty or all-zero series.
    assert _probe(con, _bind(where=["region = 'NOWHERE'"])) == (5, 0)


def test_the_probe_catches_a_predicate_that_excludes_nothing(con):
    # C15's original defect arriving through a new door: a filter that is there,
    # compiles, runs, and changes no number.
    assert _probe(con, _bind(where=["1 = 1"])) == (5, 5)


def test_a_null_predicate_counts_as_dropped_like_a_where_clause_would(con):
    # SQL's three-valued logic: `region <> 'EU'` excludes a NULL region, and so
    # does dbt's own query. `CASE WHEN <null> THEN 1 ELSE 0 END` agrees.
    con.execute(
        "CREATE TABLE nullable AS SELECT * FROM (VALUES "
        "(1, DATE '2024-01-01', 1.0, 'EMEA'), (2, DATE '2024-01-02', 1.0, NULL)) "
        "AS t(order_id, ordered_at, amount, region)"
    )
    bind = _bind(relation="nullable", where=["region <> 'AMER'"])
    assert _probe(con, bind) == (2, 1)
    assert _run(con, bind, grain="month")["value"].item() == 1.0


def test_the_probe_is_bounded_by_the_window_without_being_filtered_by_the_predicate(con):
    # Both halves matter. The window keeps it a sample rather than a full scan;
    # *not* applying the predicate is what lets it measure the predicate at all.
    assert _probe(con, _emea(), start_date="2024-01-01", end_date="2024-01-07") == (3, 2)


def test_the_probe_refuses_a_binding_with_nothing_to_probe():
    with pytest.raises(UnsupportedBinding, match="no `where` to probe"):
        build_filter_probe(_bind(), dialect="duckdb")


def test_the_probe_does_not_apply_the_predicate_it_is_measuring():
    # Asserted on the parse tree, because this is the one property no numeric
    # test on a live predicate can distinguish from a correct one: a probe that
    # filtered would report `kept == rows` for every binding, which reads as the
    # constant-true warning rather than as a broken probe.
    tree = sqlglot.parse_one(build_filter_probe(_emea(), dialect="duckdb"), dialect="duckdb")
    where = tree.find(exp.Where)
    assert where is None or "region" not in {c.name for c in where.find_all(exp.Column)}
    assert any(c.name == "region" for c in tree.find(exp.Case).find_all(exp.Column))


def test_that_assertion_would_catch_a_probe_that_filtered():
    # The meta-test: the check above is only worth writing if it distinguishes a
    # broken probe from a fixed one. A probe with the predicate in its WHERE has
    # `region` in both places, and the assertion fails on the first clause.
    broken = sqlglot.parse_one(
        'SELECT COUNT(*) AS "rows", SUM(CASE WHEN region = \'EMEA\' THEN 1 ELSE 0 END) AS "kept" '
        "FROM fct_orders AS bd_fact WHERE region = 'EMEA'",
        dialect="duckdb",
    )
    assert "region" in {c.name for c in broken.find(exp.Where).find_all(exp.Column)}


# --- the predicate is compiled per dialect, not pasted -----------------------


def _filter_columns(sql: str, dialect: str):
    """Every column the WHERE clause's non-window conjuncts reference, as the
    dialect's own parser reads them — `(name, table)` pairs."""
    where = sqlglot.parse_one(sql, dialect=dialect).find(exp.Where)
    return {(c.name, c.table) for c in where.find_all(exp.Column) if c.name == "region"}


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "databricks", "bigquery", "snowflake"])
def test_every_dialect_qualifies_the_predicate_with_the_fact_alias(dialect):
    sql = build_query(
        _emea(), grain="day", start_date="2024-01-01", end_date="2024-01-31", dialect=dialect
    )
    assert _filter_columns(sql, dialect) == {("region", "bd_fact")}


def test_that_assertion_would_catch_an_unqualified_predicate():
    # The meta-test again: an unqualified column parses to an empty `.table`, so
    # the assertion above genuinely separates the two forms rather than passing
    # on anything that mentions `region`.
    unqualified = "SELECT 1 FROM fct_orders AS bd_fact WHERE region = 'EMEA'"
    assert _filter_columns(unqualified, "duckdb") == {("region", "")}


@pytest.mark.parametrize(
    "build",
    [
        lambda b: build_query(b, grain="day", **WHOLE_JANUARY),
        lambda b: build_query(b, grain="day", dimension="plan", **WHOLE_JANUARY),
        lambda b: build_grain_assertion(b, **WHOLE_JANUARY),
    ],
)
def test_every_builder_applies_the_same_predicate(build):
    # §6's invariant, and it is worth a structural test rather than trust: a
    # filter applied to the total query but not to the sliced one produces
    # slices that do not sum, which the slicing maths reads as an unexplained
    # residual — a wrong *finding* rather than a wrong number, and harder to see.
    bind = _emea(dimensions={"plan": BindingDimension(column="customer_id")})
    assert _filter_columns(build(bind), "duckdb") == {("region", "bd_fact")}


def test_a_predicate_that_cannot_be_parsed_is_refused_at_the_builder_too():
    # The bridge refuses such a metric at import; this is the same discipline at
    # the builder, for a binding that reached here some other way.
    bind = _bind(where=["region = "])
    with pytest.raises(UnsupportedBinding, match="does not parse"):
        build_query(bind, grain="day", **WHOLE_JANUARY)


def test_a_subquery_in_a_predicate_is_refused_at_the_builder_too():
    bind = _bind(where=["region IN (SELECT code FROM regions)"])
    with pytest.raises(UnsupportedBinding, match="subquery or set operation"):
        build_query(bind, grain="day", **WHOLE_JANUARY)


def test_a_second_statement_cannot_ride_in_on_a_predicate():
    bind = _bind(where=["region = 'EMEA'; DROP TABLE fct_orders"])
    with pytest.raises(UnsupportedBinding, match="2 statements, not"):
        build_query(bind, grain="day", **WHOLE_JANUARY)

"""The binding SQL generator (roadmap 2.10).

Most of these tests **execute** the generated SQL against DuckDB rather than
matching strings. A query builder that emits plausible-looking SQL with the
wrong window bound or the wrong week boundary is exactly the failure class the
engine exists to avoid, and only running it catches that.

Dialect-specific expectations (BigQuery, Snowflake, Databricks) are asserted on
the emitted text, since there is no engine here to run them against.
"""

import pandas as pd
import pytest

from breakdown.dbt_sql import (
    UnsupportedBinding,
    build_grain_assertion,
    build_query,
    dialect_for_adapter,
)
from breakdown.parser import BindingDimension, BindingSpec

pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")
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

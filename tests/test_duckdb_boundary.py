"""The `duckdb` provider's boundary (grill 2026-10-05: H2, H3, H4, M9, M10, L1, L2).

Rule 1 — *the provider boundary refuses rather than approximates* — applied to
the things a folder of exports gets wrong in ways a warehouse cannot: a CSV has
no types, so a date's field order is a guess; it has no zone, so a `Z`
timestamp's calendar day followed the server's; it is a file, so it changes
under a running process and can be anywhere on the disk. Each test here pins
one refusal, and each failed before the fix by returning a number.
"""

import logging
import os
import subprocess
import sys
import textwrap

import pandas as pd
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")

from breakdown.data_fetch import ReservedSliceValue, SliceSelection  # noqa: E402
from breakdown.dbt_provider import (  # noqa: E402
    AmbiguousDateFormat,
    DataFilesChanged,
    DateFormatMismatch,
    OutsideDataDir,
    open_data_dir,
)
from breakdown.dbt_sql import UnsupportedBinding, build_query  # noqa: E402
from breakdown.engine.slices import slice_attribution  # noqa: E402
from breakdown.loading import NoDataInWindow, build_fetcher, fetch_all_metrics  # noqa: E402
from breakdown.parser import BindingSpec, MetricDefinition, Parser  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _yaml(bind: str, *, grain="day", provider_extra="", dimensions="") -> str:
    return (
        textwrap.dedent(
            """
        provider:
          type: duckdb
          data_dir: ./exports
        """
        )
        + textwrap.indent(textwrap.dedent(provider_extra), "  ")
        + textwrap.dedent(
            f"""
        metrics:
          - name: m
            source: x.m
            grain: {grain}
        """
        )
        + textwrap.indent(textwrap.dedent(dimensions), "    ")
        + "    bind:\n"
        + textwrap.indent(textwrap.dedent(bind), "      ")
    )


COMMON = "grain_key: id\ntime_column: d\nagg: sum\nmeasure: v\n"


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    return d


def _fetcher(tmp_path, yaml_text):
    tree = tmp_path / "tree.yml"
    tree.write_text(yaml_text)
    parser = Parser(yaml_text)
    fetcher = build_fetcher(
        parser.config.provider, parser.dag, parser.config.metrics, tree_path=str(tree)
    )
    return parser, fetcher


# --- H2: an ambiguous date is refused until its format is declared -----------

US_MONTHLY = "id,d,v\n" + "".join(f"{i},{i:02d}/01/2025,100\n" for i in range(1, 13))


def test_an_ambiguous_csv_date_is_refused_with_the_line_to_add(tmp_path, folder):
    """Twelve monthly rows `01/01/2025 … 12/01/2025`: DuckDB read them
    day-first and served one January bucket of 1200."""
    (folder / "rev.csv").write_text(US_MONTHLY)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: rev\n" + COMMON, grain="month"))
    with pytest.raises(AmbiguousDateFormat) as e:
        fetcher.fetch_metric("m", "2025-01-01", "2025-12-31", grain="month")
    message = str(e.value)
    assert "rev.csv" in message and "'d'" in message and "'01/01/2025'" in message
    assert "date_format: '%m/%d/%Y'" in message and "'%d/%m/%Y'" in message
    assert "'m'" in message  # the binding to add it to


@pytest.mark.parametrize(
    "fmt, months",
    [("%m/%d/%Y", list(range(1, 13))), ("%d/%m/%Y", [1])],
)
def test_a_declared_format_decides_the_reading(tmp_path, folder, fmt, months):
    (folder / "rev.csv").write_text(US_MONTHLY)
    _, fetcher = _fetcher(
        tmp_path, _yaml(f"relation: rev\ndate_format: '{fmt}'\n" + COMMON, grain="month")
    )
    df = fetcher.fetch_metric("m", "2025-01-01", "2025-12-31", grain="month")
    assert [d.month for d in df["date"]] == months
    assert df["m"].sum() == 1200.0


def test_a_declared_format_refuses_a_row_that_does_not_match(tmp_path, folder):
    """Refused, not nulled: a NULL time drops out of the window and the total
    is simply smaller."""
    (folder / "rev.csv").write_text("id,d,v\n1,01/02/2025,1\n2,2025-02-03,1\n3,13/40/2025,1\n")
    _, fetcher = _fetcher(tmp_path, _yaml("relation: rev\ndate_format: '%m/%d/%Y'\n" + COMMON))
    with pytest.raises(DateFormatMismatch) as e:
        fetcher.fetch_metric("m", "2025-01-01", "2025-12-31")
    message = str(e.value)
    assert "rev.csv" in message and "2 of 3" in message and "'%m/%d/%Y'" in message
    assert "'13/40/2025'" in message or "'2025-02-03'" in message


def test_dates_the_file_itself_settles_need_no_declaration(tmp_path, folder):
    """ISO order, and a day above 12 somewhere, are both unambiguous."""
    (folder / "iso.csv").write_text("id,d,v\n1,2025-01-02T09:00:00,1\n2,2025-02-01T10:00:00,2\n")
    (folder / "eu.csv").write_text("id,d,v\n1,02/01/2025,1\n2,13/01/2025,2\n")
    (folder / "us.csv").write_text("id,d,v\n1,01/02/2025,1\n2,01/13/2025,2\n")
    for rel, days in (("iso", ["2025-01-02", "2025-02-01"]), ("eu", ["2025-01-02", "2025-01-13"])):
        _, fetcher = _fetcher(tmp_path, _yaml(f"relation: {rel}\n" + COMMON))
        df = fetcher.fetch_metric("m", "2025-01-01", "2025-02-28")
        assert [str(d.date()) for d in df.loc[df["m"] > 0, "date"]] == days
    _, fetcher = _fetcher(tmp_path, _yaml("relation: us\n" + COMMON))
    df = fetcher.fetch_metric("m", "2025-01-01", "2025-02-28")
    assert [str(d.date()) for d in df.loc[df["m"] > 0, "date"]] == ["2025-01-02", "2025-01-13"]


def test_a_two_digit_year_is_ambiguous_whatever_the_fields_say(tmp_path, folder):
    (folder / "rev.csv").write_text("id,d,v\n1,25/01/13,1\n")
    _, fetcher = _fetcher(tmp_path, _yaml("relation: rev\n" + COMMON))
    with pytest.raises(AmbiguousDateFormat, match="fewer than four digits"):
        fetcher.fetch_metric("m", "2025-01-01", "2025-12-31")


def test_a_bind_sql_time_column_is_traced_to_its_file(tmp_path, folder):
    (folder / "rev.csv").write_text(US_MONTHLY)
    sql = "sql: SELECT * FROM rev WHERE v > 0\n"
    _, fetcher = _fetcher(tmp_path, _yaml(sql + COMMON, grain="month"))
    with pytest.raises(AmbiguousDateFormat, match="rev.csv"):
        fetcher.fetch_metric("m", "2025-01-01", "2025-12-31", grain="month")
    _, fetcher = _fetcher(
        tmp_path, _yaml(sql + "date_format: '%m/%d/%Y'\n" + COMMON, grain="month")
    )
    assert len(fetcher.fetch_metric("m", "2025-01-01", "2025-12-31", grain="month")) == 12


def test_an_ambiguous_column_no_binding_governs_is_warned_about(tmp_path, folder, caplog):
    """A derived time column cannot be traced, so the raw column is named."""
    (folder / "rev.csv").write_text(US_MONTHLY.replace("id,d,v", "id,booked,v"))
    sql = "sql: SELECT id, v, booked AS d FROM rev\n"
    _, fetcher = _fetcher(tmp_path, _yaml(sql + COMMON, grain="month"))
    with caplog.at_level(logging.WARNING, logger="breakdown.dbt_provider"):
        fetcher.fetch_metric("m", "2025-01-01", "2025-12-31", grain="month")
    assert any("'booked'" in r.getMessage() and "rev.csv" in r.getMessage() for r in caplog.records)


def test_a_typed_parquet_date_is_untouched_and_refuses_a_format(tmp_path, folder):
    import duckdb

    duckdb.connect().execute(
        "COPY (SELECT 1 AS id, DATE '2025-01-02' AS d, 5 AS v) "
        f"TO '{folder / 'p.parquet'}' (FORMAT parquet)"
    )
    _, fetcher = _fetcher(tmp_path, _yaml("relation: p\n" + COMMON))
    assert fetcher.fetch_metric("m", "2025-01-01", "2025-01-03")["m"].tolist() == [0.0, 5.0]
    _, fetcher = _fetcher(tmp_path, _yaml("relation: p\ndate_format: '%m/%d/%Y'\n" + COMMON))
    with pytest.raises(DateFormatMismatch, match="already stored as DATE"):
        fetcher.fetch_metric("m", "2025-01-01", "2025-01-03")


def test_date_format_is_a_duckdb_field():
    text = _yaml("relation: rev\ndate_format: '%m/%d/%Y'\n" + COMMON).replace(
        "type: duckdb\n  data_dir: ./exports", "type: dbt\n  project_path: ."
    )
    with pytest.raises(ValueError, match="applies to `provider: duckdb` only"):
        Parser(text)
    with pytest.raises(ValueError, match="no strptime specifier"):
        Parser(_yaml("relation: rev\ndate_format: 'mm/dd/yyyy'\n" + COMMON))


# --- H3: a window that misses the data ---------------------------------------

ORDERS_2025 = "id,d,v\n1,2025-06-02,3\n2,2025-06-03,4\n"


def test_an_empty_result_is_warned_about_by_name(tmp_path, folder, caplog):
    (folder / "orders.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON))
    with caplog.at_level(logging.WARNING, logger="breakdown.data_fetch"):
        df = fetcher.fetch_metric("m", "2024-01-01", "2024-01-05")
    assert df.attrs["source_rows"] == 0
    said = [r.getMessage() for r in caplog.records]
    assert any("'m' returned no rows for [2024-01-01, 2024-01-05]" in s for s in said)


def test_a_load_in_which_no_metric_returned_a_row_is_refused(tmp_path, folder):
    (folder / "orders.csv").write_text(ORDERS_2025)
    parser, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON))
    with pytest.raises(NoDataInWindow) as e:
        fetch_all_metrics(parser, fetcher, "duckdb", "2024-01-01", "2024-04-09")
    assert "[2024-01-01, 2024-04-09]" in str(e.value) and "--start-date/--end-date" in str(e.value)
    data = fetch_all_metrics(parser, fetcher, "duckdb", "2025-06-01", "2025-06-30")
    assert data.series("m")["m"].sum() == 7.0


def test_one_quiet_metric_does_not_refuse_the_tree(tmp_path, folder):
    (folder / "orders.csv").write_text(ORDERS_2025)
    (folder / "refunds.csv").write_text("id,d,v\n1,2024-01-02,1\n")
    text = _yaml("relation: orders\n" + COMMON) + (
        "  - name: refunds\n"
        "    source: x.refunds\n"
        "    bind: {relation: refunds, grain_key: id, time_column: d, agg: sum, measure: v}\n"
    )
    parser, fetcher = _fetcher(tmp_path, text)
    data = fetch_all_metrics(parser, fetcher, "duckdb", "2025-06-01", "2025-06-30")
    assert data.series("refunds")["refunds"].sum() == 0.0


def test_a_provider_that_does_not_count_rows_is_never_refused():
    """The mock synthesizes; absent is "not known", which is not zero."""
    parser = Parser("metrics:\n  - {name: a, source: x.a}\n")
    fetcher = build_fetcher(parser.config.provider, parser.dag, parser.config.metrics)
    fetch_all_metrics(parser, fetcher, "mock", "2024-01-01", "2024-01-31")


# --- H4: the session zone is pinned ------------------------------------------

TZ_ORDERS = (
    "id,d,v\n1,2025-06-02T00:30:00Z,10\n2,2025-06-02T23:30:00Z,1\n3,2025-06-03T12:00:00Z,5\n"
)

_TZ_SCRIPT = """
import sys, time, builtins
real_import = builtins.__import__
def no_pytz(name, *a, **k):
    if name == "pytz" or name.startswith("pytz."):
        raise ImportError("pytz is not installed")
    return real_import(name, *a, **k)
builtins.__import__ = no_pytz
from breakdown.parser import Parser
from breakdown.loading import build_fetcher
tree = sys.argv[1]
p = Parser(open(tree).read())
f = build_fetcher(p.config.provider, p.dag, p.config.metrics, tree_path=tree)
df = f.fetch_metric("m", "2025-06-01", "2025-06-04")
sl = f.fetch_metric_sliced("m", "tier", "2025-06-01", "2025-06-04")
print(time.tzname[0], [(str(d.date()), v) for d, v in zip(df["date"], df["m"])])
print([(str(d.date()), s, v) for d, s, v in zip(sl["date"], sl["slice"], sl["value"])])
"""


def test_a_utc_timestamp_buckets_the_same_under_any_process_zone(tmp_path, folder):
    """Same file, tree and window gave 11 / 1 / 10 orders on June 2nd under
    UTC / Los Angeles / Tokyo — or died importing pytz."""
    (folder / "orders.csv").write_text(
        TZ_ORDERS.replace("id,d,v", "id,d,v,tier")
        .replace("\n", ",ga\n")
        .replace("tier,ga", "tier", 1)
    )
    tree = tmp_path / "tree.yml"
    tree.write_text(
        _yaml(
            "relation: orders\n" + COMMON + "dimensions: {tier: {column: tier}}\n",
            dimensions="dimensions: {tier: tier}\n",
        )
    )
    script = tmp_path / "run.py"
    script.write_text(_TZ_SCRIPT)
    outputs = {}
    for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
        env = {**os.environ, "TZ": zone, "PYTHONPATH": REPO}
        run = subprocess.run(
            [sys.executable, str(script), str(tree)], capture_output=True, text=True, env=env
        )
        assert run.returncode == 0, run.stderr
        zone_name, _, rest = run.stdout.partition(" ")
        outputs[zone] = rest
    assert len(set(outputs.values())) == 1, outputs
    assert "('2025-06-02', 11.0), ('2025-06-03', 5.0)" in outputs["UTC"]


def test_the_generated_duckdb_bucket_is_a_date():
    bind = BindingSpec(relation="t", grain_key="id", time_column="d", agg="sum", measure="v")
    for grain in ("day", "week", "month"):
        sql = build_query(
            bind, grain=grain, start_date="2025-01-01", end_date="2025-01-31", dialect="duckdb"
        )
        assert f"CAST(DATE_TRUNC('{grain.upper()}', bd_fact.d) AS DATE)" in sql


def test_every_session_on_the_connection_is_utc(folder, monkeypatch):
    """`cursor()` is a new DuckDB session; the pin has to be global to reach it."""
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        (folder / "orders.csv").write_text(ORDERS_2025)
        con = open_data_dir(str(folder))
        zone = con.cursor().execute("SELECT current_setting('TimeZone')").fetchone()[0]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert zone == "UTC"


# --- M9: a file rewritten under a running server ------------------------------

SLICED = "dimensions: {tier: {column: tier}}\n"
TIERED = "id,d,v,tier\n1,2025-06-02,3,ga\n2,2025-06-03,4,vip\n"


def test_a_rewritten_file_refuses_the_slice_and_names_itself(tmp_path, folder):
    (folder / "orders.csv").write_text(TIERED)
    (folder / "other.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON + SLICED))
    fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")
    assert fetcher.changed_files() == []
    assert len(fetcher.fetch_metric_sliced("m", "tier", "2025-06-01", "2025-06-30")) == 2

    (folder / "orders.csv").write_text(TIERED + "3,2025-06-04,100,ga\n")
    assert fetcher.changed_files() == ["orders.csv"]
    with pytest.raises(DataFilesChanged) as e:
        fetcher.fetch_metric_sliced("m", "tier", "2025-06-01", "2025-06-30")
    assert isinstance(e.value, ValueError)  # the slice route's 422 mapping
    assert "orders.csv" in str(e.value) and "Restart" in str(e.value)
    assert "other.csv" not in str(e.value)


def test_a_removed_file_is_a_changed_file(tmp_path, folder):
    (folder / "orders.csv").write_text(TIERED)
    (folder / "other.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON + SLICED))
    fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")
    (folder / "other.csv").unlink()
    assert fetcher.changed_files() == ["other.csv"]


def test_a_reconnect_does_not_rebaseline(tmp_path, folder):
    (folder / "orders.csv").write_text(TIERED)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON + SLICED))
    fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")
    (folder / "orders.csv").write_text(TIERED + "3,2025-06-04,100,ga\n")
    fetcher.close()
    fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")
    assert fetcher.changed_files() == ["orders.csv"]


# --- M10: `data_dir` is a boundary --------------------------------------------


@pytest.fixture
def secret(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("id,d,v\n1,2025-06-02,424242\n")
    return outside / "secret.csv"


@pytest.mark.parametrize(
    "bind",
    [
        "relation: read_csv_auto('{secret}')\n",
        "sql: SELECT * FROM read_csv_auto('{secret}')\n",
        "relation: read_csv_auto('../outside/secret.csv')\n",
        "sql: SELECT * FROM read_csv_auto('https://example.invalid/x.csv')\n",
        "sql: SELECT * FROM read_parquet('s3://bucket/x.parquet')\n",
    ],
)
def test_a_binding_cannot_read_outside_data_dir(tmp_path, folder, secret, bind):
    (folder / "orders.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml(bind.format(secret=secret) + COMMON))
    with pytest.raises(OutsideDataDir) as e:
        fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")
    assert "allow_external_access: true" in str(e.value) and str(folder) in str(e.value)


def test_tree_sql_cannot_unlock_the_connection(folder):
    (folder / "orders.csv").write_text(ORDERS_2025)
    con = open_data_dir(str(folder))
    for statement in (
        "SET enable_external_access = true",
        "RESET lock_configuration",
        "SET allowed_directories = ['/']",
    ):
        with pytest.raises(Exception, match="locked"):
            con.cursor().execute(statement)
    assert con.execute("SELECT SUM(v) FROM orders").fetchone()[0] == 7


def test_the_opt_out_reads_outside_and_says_so_at_load(tmp_path, folder, secret, caplog):
    (folder / "orders.csv").write_text(ORDERS_2025)
    text = _yaml(
        f"relation: read_csv_auto('{secret}')\n" + COMMON,
        provider_extra="allow_external_access: true\n",
    )
    with caplog.at_level(logging.WARNING, logger="breakdown.dbt_provider"):
        _, fetcher = _fetcher(tmp_path, text)
    assert any("NOT confined" in r.getMessage() for r in caplog.records)
    assert fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")["m"].sum() == 424242.0


def test_the_opt_out_is_a_duckdb_setting():
    with pytest.raises(ValueError, match="applies to provider type 'duckdb' only"):
        Parser("provider: {type: mock, allow_external_access: true}\nmetrics: []\n")


def test_a_file_name_with_a_double_quote_does_not_take_the_provider_down(tmp_path, folder):
    (folder / 'a"b.csv').write_text(ORDERS_2025)
    (folder / "orders.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON))
    assert fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")["m"].sum() == 7.0
    _, fetcher = _fetcher(tmp_path, _yaml("relation: 'a\"b'\n" + COMMON))
    assert fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")["m"].sum() == 7.0


# --- L1: a real dimension value named like a reserved label --------------------

REGIONS = "id,d,v,tier\n" + "".join(
    f"{i},2025-06-{2 + i % 20:02d},{10 + i % 7},{name}\n"
    for i, name in enumerate(["ga", "vip", "__other__", "comp", "staff"] * 12)
)
WINDOWS = (("2025-06-02", "2025-06-11"), ("2025-06-12", "2025-06-21"))


def _defn():
    return MetricDefinition(
        name="m", source="x.m", dimensions={"tier": {"source": "tier", "top_k": 2}}
    )


def test_a_real_other_is_refused_by_name_on_both_roll_up_paths(tmp_path, folder):
    (folder / "orders.csv").write_text(REGIONS)
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON + SLICED))
    selection = SliceSelection(top_k=2, values=None, rank_windows=WINDOWS)
    span = ("2025-06-02", "2025-06-21")
    with pytest.raises(ReservedSliceValue) as in_sql:
        fetcher.fetch_metric_sliced("m", "tier", *span, selection=selection)
    with pytest.raises(ReservedSliceValue) as whole:
        fetcher.fetch_metric_sliced("m", "tier", *span)
    assert str(in_sql.value) == str(whole.value)
    assert "'__other__'" in str(whole.value) and "'m'" in str(whole.value)
    assert isinstance(whole.value, ValueError)


def test_the_engine_refuses_a_whole_frame_that_already_holds_other():
    """The backstop for a provider that labels its own frames (mock, a
    snapshot): client-side this used to fold the real value away with a 200."""
    dates = pd.date_range("2025-06-02", "2025-06-21", freq="D")
    sliced = pd.DataFrame(
        [(d, s, 1.0) for d in dates for s in ("ga", "vip", "__other__")],
        columns=["date", "slice", "value"],
    )
    unsliced = pd.DataFrame({"date": dates, "m": 3.0})
    with pytest.raises(ReservedSliceValue, match="'__other__'"):
        slice_attribution(_defn(), "tier", sliced, unsliced, *WINDOWS[0], *WINDOWS[1])


def test_a_real_null_label_is_refused_too(tmp_path, folder):
    (folder / "orders.csv").write_text(TIERED + "3,2025-06-04,1,__null__\n")
    _, fetcher = _fetcher(tmp_path, _yaml("relation: orders\n" + COMMON + SLICED))
    with pytest.raises(ReservedSliceValue, match="'__null__'"):
        fetcher.fetch_metric_sliced("m", "tier", "2025-06-01", "2025-06-30")


# --- L2: ordinary export names ------------------------------------------------


@pytest.mark.parametrize("stem", ["orders-2025", "2025_orders", "Orders Export", "order"])
def test_a_relation_that_is_a_file_stem_is_quoted_for_the_author(tmp_path, folder, stem):
    (folder / f"{stem}.csv").write_text(ORDERS_2025)
    _, fetcher = _fetcher(tmp_path, _yaml(f"relation: {stem}\n" + COMMON))
    assert fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")["m"].sum() == 7.0


def test_a_spaced_relation_that_is_no_file_is_refused_with_the_stems(tmp_path, folder):
    (folder / "orders.csv").write_text(ORDERS_2025)
    with pytest.raises(RuntimeError, match=r"not a data file.*\['orders'\]"):
        _fetcher(tmp_path, _yaml("relation: Orders Export\n" + COMMON))


def test_a_spaced_relation_is_still_refused_off_duckdb_and_when_it_is_sql():
    bind = dict(grain_key="id", time_column="d", agg="sum", measure="v")
    with pytest.raises(ValueError, match="looks like SQL"):
        BindingSpec(relation="SELECT * FROM orders", **bind)
    with pytest.raises(ValueError, match="looks like SQL"):
        BindingSpec(relation="a; b", **bind)
    text = _yaml("relation: Orders Export\n" + COMMON).replace(
        "type: duckdb\n  data_dir: ./exports", "type: dbt\n  project_path: ."
    )
    with pytest.raises(ValueError, match="looks like SQL"):
        Parser(text)


def test_a_column_name_with_spaces_says_how_to_quote_it(tmp_path, folder):
    (folder / "orders.csv").write_text(ORDERS_2025.replace("id,d,v", "id,Order Date,Unit Qty"))
    with pytest.raises(ValueError) as e:
        Parser(
            _yaml(
                "relation: orders\ngrain_key: id\ntime_column: Order Date\nagg: sum\nmeasure: v\n"
            )
        )
    assert "time_column: '\"Order Date\"'" in str(e.value)
    quoted = (
        "relation: orders\ngrain_key: id\ntime_column: '\"Order Date\"'\n"
        "agg: sum\nmeasure: '\"Unit Qty\"'\n"
    )
    _, fetcher = _fetcher(tmp_path, _yaml(quoted))
    assert fetcher.fetch_metric("m", "2025-06-01", "2025-06-30")["m"].sum() == 7.0


def test_an_expression_with_word_operators_is_not_mistaken_for_a_name():
    bind = BindingSpec(
        relation="t", grain_key="id", time_column="d", agg="count", measure="x IS NOT NULL"
    )
    assert bind.measure == "x IS NOT NULL"


def test_a_binding_that_does_not_parse_says_what_to_quote():
    """The catch-all behind the parser's check: whatever reaches sqlglot
    unparseable comes back as a sentence about the binding."""
    bind = BindingSpec(
        relation="orders-2025", grain_key="id", time_column="d", agg="sum", measure="v"
    )
    with pytest.raises(UnsupportedBinding, match="must be quoted as an identifier"):
        build_query(
            bind, grain="day", start_date="2025-01-01", end_date="2025-01-02", dialect="duckdb"
        )

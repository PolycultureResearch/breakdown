"""The `duckdb` provider (roadmap 2.2): a tree's own `bind:` blocks over a
folder of CSV / Parquet exports.

No fetcher class of its own — every test here runs through `DbtDataFetcher`
with a connection over the folder, so what is being proven is the wiring:
the folder becomes relations, the tree's bindings are the whole binding set,
the fetch lands on the engine's spine, and `doctor` asserts the same grain
claim it asserts for a dbt project. The provider needs the `duckdb` extra,
which the no-extras CI job does not install; the module skips itself there.
"""

import os
import textwrap

import pandas as pd
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")

from breakdown.data_fetch import MissingProviderExtra, provider_query_name  # noqa: E402
from breakdown.dbt_provider import (  # noqa: E402
    DbtDataFetcher,
    fetcher_from_data_dir,
    list_data_files,
    open_data_dir,
)
from breakdown.doctor import run_doctor  # noqa: E402
from breakdown.loading import build_fetcher, resolve_data_dir, wrap_snapshots  # noqa: E402
from breakdown.parser import BindingSpec, Parser  # noqa: E402

# Row-level, as an export arrives: a timestamp rather than a date, a `test`
# row that has to be filtered out, and nothing at all on June 3rd — an
# interior gap for the spine to fill, not the query.
ORDERS_CSV = """\
order_id,created_at,status,ticket_tier,quantity,gross_amount
1,2025-06-02T10:00:00,paid,ga,2,40.0
2,2025-06-02T11:30:00,paid,vip,1,90.0
3,2025-06-02T12:00:00,test,ga,99,0.0
4,2025-06-04T09:00:00,paid,ga,5,100.0
"""

TICKETS_BIND = """\
    bind:
      sql: SELECT * FROM orders WHERE status != 'test'
      grain_key: order_id
      time_column: created_at
      agg: sum
      measure: quantity
      dimensions:
        ticket_tier: {column: ticket_tier}
"""


def _tree(*, data_dir="./exports", grain="day", extra_metrics="", tickets_body=TICKETS_BIND):
    return (
        textwrap.dedent(
            f"""
        provider:
          type: duckdb
          data_dir: {data_dir}
        metrics:
          - name: tickets
            source: exports.orders.tickets
            grain: {grain}
            dimensions:
              tier: ticket_tier
        """
        )
        + tickets_body
        + textwrap.indent(textwrap.dedent(extra_metrics), "  ")
    )


@pytest.fixture
def exports(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    (d / "orders.csv").write_text(ORDERS_CSV)
    return d


@pytest.fixture
def tree_path(tmp_path, exports):
    path = tmp_path / "tree.yml"
    path.write_text(_tree())
    return path


def _fetcher(tree_path, yaml_text=None):
    parser = Parser(yaml_text or tree_path.read_text())
    return build_fetcher(
        parser.config.provider, parser.dag, parser.config.metrics, tree_path=str(tree_path)
    )


# --- the fetch contract, through the tree ------------------------------------


def test_the_factory_serves_duckdb_with_the_dbt_fetcher(tree_path):
    # No new class: the on-ramp is `DbtDataFetcher` with the tree's bindings
    # and a connection over the folder, in the duckdb dialect.
    fetcher = _fetcher(tree_path)
    assert isinstance(fetcher, DbtDataFetcher)
    assert fetcher.dialect == "duckdb"
    assert set(fetcher.bindings) == {"tickets"}


def test_daily_flow_over_a_bind_sql_relation(tree_path):
    df = _fetcher(tree_path).fetch_metric("tickets", "2025-06-01", "2025-06-05", grain="day")
    assert list(df.columns) == ["date", "tickets"]
    assert pd.DatetimeIndex(df["date"]).tz is None
    # June 1 and 3 are gaps the spine fills with 0; June 5 is after the last
    # row, so it is trimmed as not-yet-loaded rather than zeroed. The `test`
    # row (99 tickets) never reaches the sum: `bind.sql` is the relation.
    assert df["date"].tolist() == list(
        pd.to_datetime(["2025-06-01", "2025-06-02", "2025-06-03", "2025-06-04"])
    )
    assert df["tickets"].tolist() == [0.0, 3.0, 0.0, 5.0]


def test_a_weekly_binding_lands_on_monday(tmp_path, exports):
    path = tmp_path / "tree.yml"
    path.write_text(_tree(grain="week"))
    df = _fetcher(path).fetch_metric("tickets", "2025-06-02", "2025-06-15", grain="week")
    # One week of data; the week of June 9 has no row and is trimmed.
    assert df["tickets"].tolist() == [8.0]
    assert df["date"].tolist() == [pd.Timestamp("2025-06-02")]
    assert all(d.dayofweek == 0 for d in df["date"])


def test_a_parquet_export_is_a_relation_too(tmp_path, exports):
    pd.DataFrame(
        {
            "spend_id": [1, 2, 3],
            "day": pd.to_datetime(["2025-06-02", "2025-06-02", "2025-06-03"]).date,
            "channel": ["search", "social", "search"],
            "spend": [10.0, 5.0, 20.0],
        }
    ).to_parquet(exports / "ad_spend.parquet")
    path = tmp_path / "tree.yml"
    path.write_text(
        _tree(
            extra_metrics="""
              - name: spend
                source: exports.ad_spend.spend
                bind:
                  relation: ad_spend
                  grain_key: spend_id
                  time_column: day
                  agg: sum
                  measure: spend
            """
        )
    )
    fetcher = _fetcher(path)
    assert list_data_files(str(exports)) == {"ad_spend": "ad_spend.parquet", "orders": "orders.csv"}
    df = fetcher.fetch_metric("spend", "2025-06-02", "2025-06-03")
    assert df["spend"].tolist() == [15.0, 20.0]


def test_a_sliced_fetch_over_a_declared_dimension(tree_path):
    # What the `bind:` route buys that a `date, value` query never could: the
    # node's declared `tier` slices come from the binding's own dimension.
    df = _fetcher(tree_path).fetch_metric_sliced(
        "tickets", "ticket_tier", "2025-06-02", "2025-06-04", grain="day"
    )
    assert list(df.columns) == ["date", "slice", "value"]
    rows = {(str(d.date()), s): v for d, s, v in df.itertuples(index=False)}
    assert rows == {
        ("2025-06-02", "ga"): 2.0,
        ("2025-06-02", "vip"): 1.0,
        ("2025-06-04", "ga"): 5.0,
    }


def test_a_ratio_binding_over_the_same_export(tmp_path, exports, caplog):
    # `agg: ratio` with separate numerator and denominator is what keeps a rate
    # decomposable; the CSV route gets it for free.
    path = tmp_path / "tree.yml"
    path.write_text(
        _tree(
            extra_metrics="""
              - name: aov
                source: exports.orders.aov
                kind: rate
                bind:
                  sql: SELECT * FROM orders WHERE status != 'test'
                  grain_key: order_id
                  time_column: created_at
                  agg: ratio
                  numerator: gross_amount
                  denominator: quantity
            """
        )
    )
    df = _fetcher(path).fetch_metric("aov", "2025-06-02", "2025-06-04", kind="rate")
    values = df["aov"].tolist()
    assert values[0] == pytest.approx(130.0 / 3)
    assert pd.isna(values[1])  # June 3 has no orders: undefined, never zero
    assert values[2] == pytest.approx(20.0)


# --- the folder ---------------------------------------------------------------


def test_missing_and_empty_folders_are_refused_by_name(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        list_data_files(str(tmp_path / "nope"))
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "notes.txt").write_text("not data")
    with pytest.raises(RuntimeError, match="No .csv or .parquet files"):
        list_data_files(str(tmp_path / "empty"))
    # Through the fetcher too: the connection is opened lazily, at first use.
    bind = BindingSpec(
        relation="orders",
        grain_key="order_id",
        time_column="created_at",
        agg="count",
        measure="order_id",
    )
    fetcher = fetcher_from_data_dir(str(tmp_path / "nope"), {"tickets": bind})
    with pytest.raises(RuntimeError, match="not found"):
        fetcher.fetch_metric("tickets", "2025-06-01", "2025-06-05")


def test_a_stem_collision_is_refused(exports):
    pd.DataFrame({"x": [1]}).to_parquet(exports / "orders.parquet")
    with pytest.raises(RuntimeError, match="map to the relation 'orders'"):
        list_data_files(str(exports))


def test_subfolders_and_other_files_are_ignored(exports):
    (exports / "archive").mkdir()
    (exports / "archive" / "old.csv").write_text("a\n1\n")
    (exports / "README.md").write_text("exports\n")
    assert list_data_files(str(exports)) == {"orders": "orders.csv"}
    con = open_data_dir(str(exports))
    assert con.execute("SELECT count(*) FROM orders").fetchone() == (4,)
    con.close()


def test_the_extra_is_named_before_the_folder_is_looked_at(monkeypatch, tmp_path):
    # A base install must fail with the install to run, not a path error — the
    # same order the CI no-extras job asserts.
    import breakdown.dbt_provider as dp

    def missing(module, provider, extra):
        raise MissingProviderExtra(f"provider type '{provider}' needs the {extra} extra")

    monkeypatch.setattr(dp, "_require_module", missing)
    with pytest.raises(MissingProviderExtra, match="duckdb extra"):
        open_data_dir(str(tmp_path / "nope"))


# --- config -------------------------------------------------------------------


def test_relative_data_dir_resolves_against_the_tree_file(tmp_path, exports, monkeypatch):
    assert resolve_data_dir("./exports", str(tmp_path / "tree.yml")) == str(exports)
    assert resolve_data_dir("../exports", str(tmp_path / "trees" / "t.yml")) == str(exports)
    assert resolve_data_dir(str(exports), "/somewhere/else/tree.yml") == str(exports)
    # And the fetch works from any working directory, which is the point.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    path = tmp_path / "tree.yml"
    path.write_text(_tree())
    assert (
        _fetcher(path).fetch_metric("tickets", "2025-06-02", "2025-06-04")["tickets"].sum() == 8.0
    )


def test_data_dir_expands_environment_variables(tmp_path, exports, monkeypatch):
    monkeypatch.setenv("EXPORTS_DIR", str(exports))
    parser = Parser(_tree(data_dir="${EXPORTS_DIR}"))
    assert parser.config.provider.data_dir == str(exports)


def test_data_dir_is_required():
    with pytest.raises(ValueError, match="requires `data_dir`"):
        Parser(
            "provider:\n  type: duckdb\nmetrics:\n  - name: a\n    source: a\n"
            + textwrap.indent(TICKETS_BIND, "")
        )


def test_bindings_are_keyed_by_the_tree_name(tree_path):
    metric = Parser(tree_path.read_text()).config.metrics[0]
    assert provider_query_name("duckdb", metric) == "tickets"


def test_a_legacy_top_level_sql_is_refused_with_the_pointer_at_bind_sql():
    legacy = """\
    sql: |
      SELECT CAST(created_at AS DATE) AS date, SUM(quantity) AS value
      FROM orders GROUP BY 1
"""
    with pytest.raises(ValueError, match=r"Move the query under `bind\.sql`"):
        Parser(_tree(tickets_body=legacy))


def test_an_unbound_node_is_refused_at_parse():
    with pytest.raises(ValueError, match=r"declare no `bind:` under `provider: duckdb`"):
        Parser(_tree(tickets_body=""))


def test_a_derived_node_needs_no_binding(tree_path):
    yaml_text = _tree(
        extra_metrics="""
          - name: doubled
            formula: "tickets * 2"
            parents: [tickets]
        """
    )
    fetcher = _fetcher(tree_path, yaml_text)
    assert set(fetcher.bindings) == {"tickets"}


def test_duckdb_is_never_wrapped_in_snapshots(tree_path, monkeypatch):
    # The files are the committed artifact; a snapshot keyed without a content
    # hash would freeze an edited CSV silently.
    monkeypatch.delenv("BREAKDOWN_SNAPSHOT_DIR", raising=False)
    inner = _fetcher(tree_path)
    assert wrap_snapshots(inner, "duckdb", str(tree_path)) is inner
    assert not os.path.exists(tree_path.parent / ".breakdown")


# --- doctor -------------------------------------------------------------------


def test_doctor_end_to_end(tree_path):
    results = {r.name: r for r in run_doctor(str(tree_path), "2025-06-01", "2025-06-05")}
    assert results["duckdb extra installed"].status == "pass"
    assert results["data files"].status == "pass"
    assert "orders (orders.csv)" in results["data files"].detail
    assert results["tree metrics bind"].status == "pass"
    assert results["declared dimensions exist"].status == "pass"
    assert results["grain claims hold"].status == "pass"
    assert results["metric sql runs"].status == "pass"
    assert "snapshots" not in results  # never wrapped, so never reported
    # Four days of data is below the fit minimum: doctor says so, honestly.
    assert results["fit readiness"].status == "fail"
    assert "tickets: 4/" in results["fit readiness"].detail


def test_doctor_asserts_the_grain_claim_on_an_export(tmp_path, exports):
    # A hand-exported CSV with a duplicated order id is exactly the fan-out the
    # grain claim exists to catch, and it is the same check a dbt project gets.
    (exports / "orders.csv").write_text(ORDERS_CSV + "4,2025-06-04T09:00:00,paid,ga,5,100.0\n")
    path = tmp_path / "tree.yml"
    path.write_text(_tree())
    results = {r.name: r for r in run_doctor(str(path), "2025-06-01", "2025-06-05")}
    assert results["grain claims hold"].status == "fail"
    assert "tickets (4 rows / 3 distinct)" in results["grain claims hold"].detail


def test_doctor_reports_a_missing_folder_and_skips_the_rest(tmp_path):
    path = tmp_path / "tree.yml"
    path.write_text(_tree())  # no exports/ folder created
    results = {r.name: r for r in run_doctor(str(path), "2025-06-01", "2025-06-05")}
    assert results["data files"].status == "fail"
    assert str(tmp_path / "exports") in results["data files"].detail
    for name in ("tree metrics bind", "grain claims hold", "metric sql runs"):
        assert results[name].status == "skip"
    assert results["fit readiness"].status == "skip"


def test_doctor_reports_a_missing_dimension_before_the_first_slice(tmp_path, exports):
    path = tmp_path / "tree.yml"
    path.write_text(_tree().replace("tier: ticket_tier", "tier: tier_name"))
    results = {r.name: r for r in run_doctor(str(path), "2025-06-01", "2025-06-05")}
    assert results["declared dimensions exist"].status == "fail"
    assert "tickets.tier -> 'tier_name'" in results["declared dimensions exist"].detail

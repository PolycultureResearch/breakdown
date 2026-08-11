"""Query provenance (roadmap 2.11).

Principle 3 is "never ship a number the engine can't defend", and it had a hole:
for most providers a user could not see what was asked, so the number was
unfalsifiable by the person being asked to trust it. These tests pin both halves
— the query when there is one, and an honest reason when there is not.
"""

import pytest
from fastapi.testclient import TestClient

from breakdown.api.main import app
from breakdown.data_fetch import BaseDataFetcher, MockDataFetcher, WarehouseDataFetcher

TREE = """
provider:
  type: {provider}
metrics:
  - name: revenue
    source: dbt.metrics.revenue
    dimensions:
      region: customer__region
"""


def _client(tmp_path, monkeypatch, provider="mock"):
    tree = tmp_path / "tree.yml"
    tree.write_text(TREE.format(provider=provider))
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    return TestClient(app)


# --- the contract -----------------------------------------------------------


def test_the_base_contract_answers_none_rather_than_raising():
    # None is a legitimate answer: a provider that never sees SQL is not a
    # broken provider.
    assert BaseDataFetcher.query_provenance(MockDataFetcher(), "revenue") is None


def test_warehouse_returns_the_sql_its_author_wrote():
    f = WarehouseDataFetcher(
        host="h",
        http_path="p",
        token="t",
        metric_sql={"revenue": "SELECT d AS date, v AS value FROM t"},
    )
    assert "SELECT d AS date" in f.query_provenance("revenue")
    assert f.query_provenance("missing") is None


def test_warehouse_has_no_sliced_query_to_show_yet():
    # Slicing is unimplemented there (roadmap 2.8), so claiming a query would
    # be worse than saying there isn't one.
    f = WarehouseDataFetcher(host="h", http_path="p", token="t", metric_sql={"revenue": "SELECT 1"})
    assert f.query_provenance("revenue", "customer__region") is None


def test_snapshot_fetcher_delegates_so_a_cached_metric_still_shows_its_query(tmp_path):
    from breakdown.snapshots import SnapshotFetcher, SnapshotStore

    inner = WarehouseDataFetcher(
        host="h", http_path="p", token="t", metric_sql={"revenue": "SELECT 42"}
    )
    wrapped = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    assert "SELECT 42" in wrapped.query_provenance("revenue")


def test_the_dbt_provider_reports_what_actually_ran():
    # Deliberately the executed statement, not a re-derivation: a query rebuilt
    # for display could differ from the one behind the number and would then be
    # provenance for nothing.
    pytest.importorskip("sqlglot")
    duckdb = pytest.importorskip("duckdb")

    from breakdown.dbt_provider import DbtDataFetcher
    from breakdown.parser import BindingDimension, BindingSpec

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1, DATE '2024-01-01', 5.0, 'EMEA')) "
        "AS x(id, d, v, region)"
    )
    bind = BindingSpec(
        relation="t",
        grain_key="id",
        time_column="d",
        agg="sum",
        measure="v",
        dimensions={"region": BindingDimension(column="region")},
    )
    f = DbtDataFetcher({"revenue": bind}, connect=lambda: con, dialect="duckdb")

    assert f.query_provenance("revenue") is None  # nothing has run yet
    f.fetch_metric("revenue", "2024-01-01", "2024-01-01")
    assert "FROM t" in f.query_provenance("revenue")
    f.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-01")
    assert "region" in f.query_provenance("revenue", "region")


# --- the endpoint -----------------------------------------------------------


def test_endpoint_explains_why_mock_has_no_query(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        body = c.get("/metrics/revenue/query").json()
    assert body["sql"] is None
    assert body["provider"] == "mock"
    assert "synthesizes" in body["note"]


def test_endpoint_distinguishes_never_see_sql_from_no_sql(tmp_path, monkeypatch):
    # `local` and `cloud` do run SQL — someone else's planner writes it and
    # never hands it back. That is a different fact from "no query is run", and
    # a user deciding whether to trust a number needs to know which. The tree
    # stays on `mock` so startup succeeds; only the reported provider matters.
    with _client(tmp_path, monkeypatch) as c:
        c.app.state.parser.config.provider.type = "cloud"
        note = c.get("/metrics/revenue/query").json()["note"]
    assert "server-side" in note and "not SQL" in note


def test_endpoint_503s_rather_than_500s_when_startup_failed(tmp_path, monkeypatch):
    # Provenance is most wanted when something is wrong, but a degraded start
    # leaves no parser — so it must give the same 503 the data endpoints do.
    with _client(tmp_path, monkeypatch) as c:
        c.app.state.startup_error = "provider unreachable"
        r = c.get("/metrics/revenue/query")
    assert r.status_code == 503
    assert "breakdown doctor" in r.json()["detail"]


def test_endpoint_returns_sql_when_the_provider_has_it(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        c.app.state.fetcher = WarehouseDataFetcher(
            host="h",
            http_path="p",
            token="t",
            metric_sql={"revenue": "SELECT d AS date, v AS value FROM orders"},
        )
        body = c.get("/metrics/revenue/query").json()
    assert "FROM orders" in body["sql"]
    assert body["note"] is None


def test_endpoint_404s_on_an_unknown_metric(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/metrics/nope/query").status_code == 404


def test_endpoint_404s_on_an_undeclared_dimension(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.get("/metrics/revenue/query", params={"dimension": "plan"})
    assert r.status_code == 404
    assert "declares no dimension 'plan'" in r.json()["detail"]


def test_endpoint_maps_the_declared_dimension_to_its_provider_source(tmp_path, monkeypatch):
    # The tree says `region: customer__region`; the fetcher is keyed by the
    # provider's identifier, not the tree's alias.
    seen = {}

    class _Spy(MockDataFetcher):
        def query_provenance(self, metric_name, dimension_source=None, **kw):
            seen["args"] = (metric_name, dimension_source)
            return "SELECT 1"

    with _client(tmp_path, monkeypatch) as c:
        c.app.state.fetcher = _Spy()
        c.get("/metrics/revenue/query", params={"dimension": "region"})
    assert seen["args"] == ("revenue", "customer__region")


# --- provenance must not depend on whether a query happened to run ----------


def test_dbt_generates_the_query_when_a_snapshot_served_the_series():
    # A snapshot hit returns the number without executing anything. Answering
    # "no query" there would understate how defensible the number is: the
    # binding still determines it exactly.
    pytest.importorskip("sqlglot")

    from breakdown.dbt_provider import DbtDataFetcher
    from breakdown.parser import BindingSpec

    bind = BindingSpec(relation="t", grain_key="id", time_column="d", agg="sum", measure="v")
    f = DbtDataFetcher({"revenue": bind}, connect=lambda: None, dialect="duckdb")

    assert f.query_provenance("revenue") is None  # no window, nothing ran
    sql = f.query_provenance("revenue", grain="day", start_date="2024-01-01", end_date="2024-01-31")
    assert "FROM t" in sql and "2024-02-01" in sql  # half-open upper bound
    assert f.executed("revenue") is False


def test_the_executed_statement_wins_over_a_regenerated_one():
    # What ran is what produced the number; a regeneration could differ (a
    # different window, a binding edited since) and would be provenance for
    # nothing.
    pytest.importorskip("sqlglot")
    duckdb = pytest.importorskip("duckdb")

    from breakdown.dbt_provider import DbtDataFetcher
    from breakdown.parser import BindingSpec

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1, DATE '2024-01-01', 5.0)) AS x(id, d, v)"
    )
    bind = BindingSpec(relation="t", grain_key="id", time_column="d", agg="sum", measure="v")
    f = DbtDataFetcher({"revenue": bind}, connect=lambda: con, dialect="duckdb")

    f.fetch_metric("revenue", "2024-01-01", "2024-01-01")
    ran = f.query_provenance("revenue")
    # A different window is requested; the executed statement is still returned.
    assert (
        f.query_provenance("revenue", grain="day", start_date="2020-01-01", end_date="2020-12-31")
        == ran
    )
    assert "2024-01-02" in ran
    assert f.executed("revenue") is True


def test_endpoint_labels_a_snapshot_served_query_as_not_executed(tmp_path, monkeypatch):
    from breakdown.parser import BindingSpec

    pytest.importorskip("sqlglot")
    from breakdown.dbt_provider import DbtDataFetcher

    bind = BindingSpec(relation="fct", grain_key="id", time_column="d", agg="sum", measure="v")
    with _client(tmp_path, monkeypatch) as c:
        c.app.state.parser.config.provider.type = "dbt"
        c.app.state.fetcher = DbtDataFetcher(
            {"revenue": bind}, connect=lambda: None, dialect="duckdb"
        )
        body = c.get("/metrics/revenue/query").json()
    assert body["executed"] is False
    assert "FROM fct" in body["sql"]
    assert body["dialect"] == "duckdb"
    assert "served from a snapshot" in body["note"]


def test_endpoint_reads_dialect_through_the_snapshot_wrapper(tmp_path, monkeypatch):
    # SnapshotFetcher delegates the query but carries none of the provider's
    # own attributes, so the dialect has to be read from the inner fetcher.
    pytest.importorskip("sqlglot")
    from breakdown.dbt_provider import DbtDataFetcher
    from breakdown.parser import BindingSpec
    from breakdown.snapshots import SnapshotFetcher, SnapshotStore

    bind = BindingSpec(relation="fct", grain_key="id", time_column="d", agg="sum", measure="v")
    inner = DbtDataFetcher({"revenue": bind}, connect=lambda: None, dialect="databricks")
    with _client(tmp_path, monkeypatch) as c:
        c.app.state.parser.config.provider.type = "dbt"
        c.app.state.fetcher = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
        body = c.get("/metrics/revenue/query").json()
    assert body["dialect"] == "databricks"
    assert body["executed"] is False

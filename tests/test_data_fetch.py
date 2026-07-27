import numpy as np
import pytest

from breakdown.data_fetch import (
    CloudDataFetcher,
    LocalDataFetcher,
    MockDataFetcher,
    WarehouseDataFetcher,
)
from breakdown.parser import Parser

TREE_YAML = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""


def test_mock_fetcher_returns_data():
    fetcher = MockDataFetcher()
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert not df.empty
    assert "revenue" in df.columns
    assert "date" in df.columns


def test_mock_fetcher_deterministic():
    fetcher = MockDataFetcher()
    df1 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    df2 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert df1["revenue"].equals(df2["revenue"])


def test_mock_fetcher_invalid_date_range():
    fetcher = MockDataFetcher()
    with pytest.raises(ValueError, match="end_date"):
        fetcher.fetch_metric("revenue", "2024-03-31", "2024-01-01")


def test_mock_fetcher_tree_aware_formula_holds():
    """With a DAG, formula nodes should approximately satisfy their formula."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)

    revenue = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")["revenue"]
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-03-31")["order_count"]
    aov = fetcher.fetch_metric("average_order_value", "2024-01-01", "2024-03-31")["average_order_value"]

    corr = np.corrcoef(revenue, orders * aov)[0, 1]
    assert corr > 0.99


def test_mock_fetcher_tree_aware_parent_child_correlated():
    """Probabilistic children should co-move with their parents."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)

    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-03-31")["daily_sessions"]
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-03-31")["order_count"]

    corr = np.corrcoef(sessions, orders)[0, 1]
    assert corr > 0.9


def test_mock_fetcher_tree_aware_deterministic():
    parser = Parser(TREE_YAML)
    df1 = MockDataFetcher(dag=parser.dag).fetch_metric("revenue", "2024-01-01", "2024-03-31")
    df2 = MockDataFetcher(dag=parser.dag).fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert df1["revenue"].equals(df2["revenue"])


def test_mock_fetcher_unknown_metric_falls_back_to_random_walk():
    """Metrics not in the DAG (or no DAG at all) still return data."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    df = fetcher.fetch_metric("not_in_tree", "2024-01-01", "2024-01-31")
    assert "not_in_tree" in df.columns
    assert len(df) == 31


def test_mock_fetcher_lagged_child_correlates_with_shifted_parent():
    """With a lagged edge, the generated child should correlate more strongly
    with the lag-shifted parent than with the contemporaneous parent."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
    lags: { daily_sessions: 5 }
"""
    parser = Parser(yaml_content)
    fetcher = MockDataFetcher(dag=parser.dag)
    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-04-30")["daily_sessions"].values
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-04-30")["order_count"].values

    L = 5
    corr_lagged = np.corrcoef(orders[L:], sessions[:-L])[0, 1]
    corr_contemp = np.corrcoef(orders[L:], sessions[L:])[0, 1]
    assert corr_lagged > corr_contemp


def test_cloud_fetcher_requires_credentials():
    """CloudDataFetcher.__init__ should raise when dbtsl client rejects bad credentials."""
    with pytest.raises(Exception):
        fetcher = CloudDataFetcher(environment_id="0", host="invalid.host", token="bad-token")
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")


def test_local_fetcher_raises_on_bad_project():
    """LocalDataFetcher should raise RuntimeError when mf fails on a non-existent project."""
    fetcher = LocalDataFetcher(project_path="/tmp/nonexistent_dbt_project")
    with pytest.raises(RuntimeError, match="mf query failed"):
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")


# --- warehouse provider ---

WAREHOUSE_TREE = """
provider:
  type: warehouse
  host: ${TEST_WH_HOST}
  http_path: /sql/1.0/warehouses/abc
  token: ${TEST_WH_TOKEN}
  catalog: narrative
  schema: default
metrics:
  - name: new_mrr
    source: narrative.default.fct_mrr_movements
    sql: "SELECT day AS date, SUM(new_mrr_usd) AS value FROM fct_mrr_movements WHERE day BETWEEN :start_date AND :end_date GROUP BY day"
"""


def test_provider_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_WH_HOST", "dbc-xyz.databricks.com")
    monkeypatch.setenv("TEST_WH_TOKEN", "dapi-secret")
    cfg = Parser(WAREHOUSE_TREE).config.provider
    assert cfg.type == "warehouse"
    assert cfg.host == "dbc-xyz.databricks.com"
    assert cfg.token == "dapi-secret"
    assert cfg.db_schema == "default"  # `schema` in YAML, aliased to avoid BaseModel clash


def test_provider_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("TEST_WH_HOST", raising=False)
    monkeypatch.setenv("TEST_WH_TOKEN", "dapi-secret")
    with pytest.raises(Exception, match="TEST_WH_HOST"):
        Parser(WAREHOUSE_TREE)


class _StubCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("date",), ("value",)]
        self.executed = None

    def execute(self, sql, parameters=None):
        self.executed = (sql, parameters)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def test_warehouse_fetcher_reindexes_and_zero_fills(monkeypatch):
    import datetime

    # Two movement days inside a five-day window; the gaps must become 0.
    rows = [
        (datetime.date(2025, 6, 2), 100.0),
        (datetime.date(2025, 6, 4), 250.0),
    ]
    cursor = _StubCursor(rows)
    fetcher = WarehouseDataFetcher(
        host="h", http_path="p", token="t",
        metric_sql={"new_mrr": "SELECT ... :start_date ... :end_date"},
    )
    monkeypatch.setattr(fetcher, "_cursor", lambda: cursor)

    df = fetcher.fetch_metric("new_mrr", "2025-06-01", "2025-06-05")

    assert list(df.columns) == ["date", "new_mrr"]
    assert len(df) == 5  # full daily range
    assert df["new_mrr"].tolist() == [0.0, 100.0, 0.0, 250.0, 0.0]
    # window bounds were bound as named parameters, not string-formatted
    assert cursor.executed[1] == {"start_date": "2025-06-01", "end_date": "2025-06-05"}


def test_warehouse_fetcher_unknown_metric_raises():
    fetcher = WarehouseDataFetcher(host="h", http_path="p", token="t", metric_sql={})
    with pytest.raises(RuntimeError, match="No `sql` defined"):
        fetcher.fetch_metric("missing", "2025-06-01", "2025-06-05")


def test_warehouse_fetcher_requires_auth():
    # Neither a PAT token nor an OAuth profile → construction must fail loudly.
    with pytest.raises(ValueError, match="token.*or.*profile|profile"):
        WarehouseDataFetcher(host="h", http_path="p", token=None, metric_sql={})


def test_warehouse_fetcher_profile_uses_credentials_provider(monkeypatch):
    """With a `profile`, the connector is called with an OAuth credentials
    provider (not access_token), and host defaults to the profile's host."""
    captured = {}

    class _FakeConfig:
        def __init__(self, profile=None):
            captured["profile"] = profile
            self.host = "https://dbc-fake.cloud.databricks.com/"

        def authenticate(self):  # HeaderFactory stand-in
            return {}

    class _StubConnection:
        def cursor(self):
            return _StubCursor([])

    def _fake_connect(**kwargs):
        captured["connect_kwargs"] = kwargs
        return _StubConnection()

    import databricks.sql as dbsql_mod
    from databricks.sdk import core as sdk_core

    monkeypatch.setattr(sdk_core, "Config", _FakeConfig)
    monkeypatch.setattr(dbsql_mod, "connect", _fake_connect)

    fetcher = WarehouseDataFetcher(
        host=None, http_path="/sql/1.0/warehouses/x", token=None,
        metric_sql={}, profile="narrative",
    )
    fetcher._cursor()

    assert captured["profile"] == "narrative"
    kw = captured["connect_kwargs"]
    assert "access_token" not in kw
    assert callable(kw["credentials_provider"])
    # host resolved from the profile, with scheme/trailing slash stripped
    assert kw["server_hostname"] == "dbc-fake.cloud.databricks.com"

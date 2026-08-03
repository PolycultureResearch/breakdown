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

    # Two movement days inside a five-day window: the interior gap becomes 0;
    # the trailing day after the last returned row is trimmed (not-yet-loaded,
    # not zero).
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
    assert len(df) == 4  # daily range through the last observed row
    assert df["new_mrr"].tolist() == [0.0, 100.0, 0.0, 250.0]
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


# --- grain-aware providers (1.7) ---

MIXED_TREE = """
metrics:
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: trial_conversion_rate
    source: dbt.metric.trial_conversion_rate
    grain: week
    kind: rate
  - name: conversions
    source: dbt.metric.conversions
    grain: week
    formula: "trial_starts * trial_conversion_rate"
    parents: [trial_starts, trial_conversion_rate]
"""


def test_mock_all_day_tree_pinned_values():
    """Grain-aware generation must leave all-day trees byte-identical: golden
    values captured from the pre-grain generator."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-04-09")
    assert len(df) == 100
    np.testing.assert_allclose(
        df["revenue"].head(3).to_numpy(),
        [793860.4244541493, 959272.2559016624, 1017094.0476589967],
        rtol=0, atol=1e-6,
    )
    np.testing.assert_allclose(df["revenue"].sum(), 79884063.93374, atol=1e-4)
    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-04-09")
    np.testing.assert_allclose(
        sessions["daily_sessions"].head(3).to_numpy(),
        [1890.3967010755, 2019.680210048, 2028.7438671543],
        rtol=0, atol=1e-6,
    )


def test_mock_mixed_grain_identity_holds_at_node_grain():
    """A weekly formula node satisfies its formula against the daily flow
    parent summed to weeks and the native weekly rate."""
    parser = Parser(MIXED_TREE)
    fetcher = MockDataFetcher(dag=parser.dag)
    window = ("2024-01-01", "2024-03-31")

    conv = fetcher.fetch_metric("conversions", *window, grain="week")
    starts = fetcher.fetch_metric("trial_starts", *window)
    rate = fetcher.fetch_metric("trial_conversion_rate", *window, grain="week", kind="rate")

    weekly_starts = (
        starts.set_index("date")["trial_starts"].resample("W-MON", label="left", closed="left").sum()
    )
    joined = conv.set_index("date").join(weekly_starts, how="inner").join(
        rate.set_index("date"), how="inner"
    )
    expected = joined["trial_starts"] * joined["trial_conversion_rate"]
    assert len(joined) >= 10
    # Generator adds 2% noise to the identity; with ~13 weekly points assert
    # a tight relative residual rather than a fragile correlation threshold.
    rel_err = np.abs(joined["conversions"] - expected) / np.abs(expected)
    assert rel_err.median() < 0.05
    assert np.corrcoef(joined["conversions"], expected)[0, 1] > 0.95
    # Weekly labels are period starts (Mondays).
    assert all(d.dayofweek == 0 for d in conv["date"])


def test_mock_fetcher_coarse_fallback_walk():
    """Non-DAG metrics generate at the requested grain."""
    fetcher = MockDataFetcher()
    df = fetcher.fetch_metric("anything", "2024-01-01", "2024-03-31", grain="week")
    assert all(d.dayofweek == 0 for d in df["date"])
    assert len(df) == 13  # whole weeks fully inside Mon Jan 1 .. Sun Mar 31


def _wh_fetcher(rows):
    import datetime  # noqa: F401
    cursor = _StubCursor(rows)
    fetcher = WarehouseDataFetcher(
        host="h", http_path="p", token="t",
        metric_sql={"m": "SELECT ... :start_date ... :end_date"},
    )
    fetcher._cursor = lambda: cursor
    return fetcher


def test_warehouse_weekly_flow_zero_fills_interior_trims_trailing():
    import datetime
    rows = [
        (datetime.date(2024, 1, 1), 100.0),   # Monday
        (datetime.date(2024, 1, 15), 250.0),  # Monday, gap week between
    ]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")
    # Interior gap (Jan 8) -> 0; trailing week (Jan 22) trimmed as unloaded.
    assert df["m"].tolist() == [100.0, 0.0, 250.0]
    assert all(d.dayofweek == 0 for d in df["date"])


def test_warehouse_weekly_stock_forward_fills():
    import datetime
    rows = [
        (datetime.date(2024, 1, 1), 100.0),
        (datetime.date(2024, 1, 15), 250.0),
    ]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="stock")
    assert df["m"].tolist() == [100.0, 100.0, 250.0]


def test_warehouse_empty_result_keeps_full_zero_fill_for_flow():
    """No rows at all is a legitimate all-quiet flow window (e.g. a coverage
    cliff) — keep the full zero spine rather than trimming to nothing."""
    df = _wh_fetcher([]).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")
    assert df["m"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_warehouse_stock_leading_gap_raises():
    import datetime
    rows = [(datetime.date(2024, 1, 15), 250.0)]
    with pytest.raises(RuntimeError, match="no value at or before the first week period"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="stock")


def test_warehouse_rate_missing_interior_period_raises():
    import datetime
    # Interior gap (Jan 8) between returned rows: a rate cannot be invented.
    # (A trailing gap is trimmed like any other kind — data not loaded yet.)
    rows = [
        (datetime.date(2024, 1, 1), 0.5),
        (datetime.date(2024, 1, 15), 0.6),
    ]
    with pytest.raises(RuntimeError, match="Rate metric 'm' is missing week periods"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="rate")


def test_warehouse_misaligned_labels_raise():
    import datetime
    rows = [(datetime.date(2024, 1, 3), 100.0)]  # a Wednesday at week grain
    with pytest.raises(RuntimeError, match="not aligned to period starts"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")


def test_warehouse_partial_period_rows_dropped():
    """Rows for periods only partially inside the window are dropped, not
    zero-filled into fake periods."""
    import datetime
    rows = [
        (datetime.date(2024, 1, 1), 100.0),
        (datetime.date(2024, 1, 8), 200.0),
    ]
    # Window ends Wednesday Jan 10: the Jan-8 week is partial.
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-10", grain="week")
    assert df["m"].tolist() == [100.0]


# --- sliced-fetch reshape (_sliced_long) ---

def test_sliced_long_reshapes_semantic_layer_frame():
    import pandas as pd

    from breakdown.data_fetch import _sliced_long

    raw = pd.DataFrame({
        "METRIC_TIME__DAY": ["2026-01-05", "2026-01-05", "2026-01-06"],
        "CUSTOMER__REGION": ["emea", "amer", None],
        "signups": [10.0, 20.0, 5.0],
    })
    out = _sliced_long(raw, "signups", "day")
    assert list(out.columns) == ["date", "slice", "value"]
    assert set(out["slice"]) == {"emea", "amer", "__null__"}
    assert out["value"].dtype == float
    assert out["date"].is_monotonic_increasing


def test_sliced_long_ambiguous_columns_raise():
    import pandas as pd

    from breakdown.data_fetch import _sliced_long

    raw = pd.DataFrame({
        "METRIC_TIME__DAY": ["2026-01-05"],
        "CUSTOMER__REGION": ["emea"],
        "CUSTOMER__PLAN": ["pro"],
        "signups": [10.0],
    })
    with pytest.raises(RuntimeError, match="exactly one dimension column"):
        _sliced_long(raw, "signups", "day")

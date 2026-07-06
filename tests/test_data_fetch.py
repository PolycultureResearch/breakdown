import numpy as np
import pytest

from breakdown.data_fetch import CloudDataFetcher, LocalDataFetcher, MockDataFetcher
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

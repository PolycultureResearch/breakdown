import pytest
from breakdown.parser import Parser
from breakdown.data_fetch import MockDataFetcher, CloudDataFetcher, LocalDataFetcher

def test_valid_yaml_parsing():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: conversions
    source: dbt.metric.conversions
    parents:
      - dau
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
"""
    parser = Parser(yaml_content)
    assert "dau" in parser.dag.nodes
    assert "conversions" in parser.dag.nodes
    assert parser.dag.has_edge("dau", "conversions")

def test_invalid_distribution():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    priors:
      coefficient:
        distribution: "InvalidDist"
"""
    with pytest.raises(ValueError, match="Invalid distribution: InvalidDist"):
        Parser(yaml_content)

def test_cycle_detection():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [B]
  - name: B
    source: dbt.metric.B
    parents: [A]
"""
    with pytest.raises(ValueError, match="Metric tree contains cycles"):
        Parser(yaml_content)

def test_missing_parent():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [Missing]
"""
    with pytest.raises(ValueError, match="Parent metric 'Missing' not found for metric 'A'"):
        Parser(yaml_content)


# --- Data fetcher tests ---

def test_mock_fetcher_returns_data():
    fetcher = MockDataFetcher()
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert not df.empty
    assert "revenue" in df.columns
    assert "date" in df.columns


def test_mock_fetcher_deterministic():
    """Same call twice should return identical data."""
    fetcher = MockDataFetcher()
    df1 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    df2 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert df1["revenue"].equals(df2["revenue"])


def test_mock_fetcher_invalid_date_range():
    fetcher = MockDataFetcher()
    with pytest.raises(ValueError, match="end_date"):
        fetcher.fetch_metric("revenue", "2024-03-31", "2024-01-01")


def test_cloud_fetcher_not_implemented():
    # CloudDataFetcher.__init__ imports dbtsl which may not be installed in CI,
    # so we test fetch_metric via a minimal subclass that skips __init__.
    class _TestCloud(CloudDataFetcher):
        def __init__(self):
            pass  # skip SDK init

    fetcher = _TestCloud()
    with pytest.raises(NotImplementedError):
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")


def test_local_fetcher_not_implemented():
    fetcher = LocalDataFetcher(project_path="/tmp/fake")
    with pytest.raises(NotImplementedError):
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")

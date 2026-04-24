import pytest
import pandas as pd
import numpy as np
from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder
from breakdown.data_fetch import generate_mock_data

SIMPLE_YAML = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
"""

YAML_WITH_PRIORS = """
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
"""

YAML_WITH_SEASONALITY = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: revenue
    source: dbt.metric.revenue
    parents: [daily_sessions]
    seasonality:
      - period: 7
        name: weekly
"""


def test_model_builder_sampling():
    """Basic end-to-end: model builds and samples without error."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("order_count", draws=100, tune=100)

    assert "beta" in trace.posterior
    assert "trend" in trace.posterior


def test_model_builder_root_metric():
    """A root metric (no parents) should sample with no beta variable."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("daily_sessions", draws=100, tune=100)

    assert "trend" in trace.posterior
    assert "beta" not in trace.posterior


def test_model_builder_with_priors():
    """Priors specified in YAML should be applied without error."""
    parser = Parser(YAML_WITH_PRIORS)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("order_count", draws=100, tune=100)

    assert "beta" in trace.posterior
    summary = builder.get_summary("order_count")
    assert "beta[0]" in summary.index


def test_model_builder_with_seasonality():
    """Seasonality components from YAML should appear in the trace."""
    parser = Parser(YAML_WITH_SEASONALITY)
    data = generate_mock_data(n_days=50)
    # rename revenue col to match the yaml
    data = data.rename(columns={"revenue": "revenue"})
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("revenue", draws=100, tune=100)

    # Fourier components for weekly seasonality (2 harmonics each = sin/cos)
    assert "sin_weekly_h1" in trace.posterior
    assert "cos_weekly_h1" in trace.posterior


def test_get_summary_hdi():
    """Summary should use 95% HDI."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)
    builder.build_and_sample("order_count", draws=100, tune=100)

    summary = builder.get_summary("order_count")
    # arviz labels HDI columns as hdi_2.5% and hdi_97.5% for 95% HDI
    assert any("hdi" in col for col in summary.columns)
    hdi_cols = [c for c in summary.columns if "hdi" in c]
    # There should be exactly 2 HDI bound columns
    assert len(hdi_cols) == 2


def test_get_summary_no_trace_raises():
    """get_summary should raise if no trace exists for a metric."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    with pytest.raises(ValueError, match="No trace found for metric"):
        builder.get_summary("order_count")


def test_missing_column_raises():
    """build_and_sample should raise ValueError if a metric column is absent."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50).drop(columns=["order_count"])
    builder = ModelBuilder(parser.dag, data)

    with pytest.raises(ValueError, match="Columns missing from data"):
        builder.build_and_sample("order_count", draws=100, tune=100)


def test_nan_column_raises():
    """build_and_sample should raise ValueError if any column contains NaN."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    data.loc[5, "daily_sessions"] = float("nan")
    builder = ModelBuilder(parser.dag, data)

    with pytest.raises(ValueError, match="NaN values found"):
        builder.build_and_sample("order_count", draws=100, tune=100)

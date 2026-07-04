import pytest
import pandas as pd
import numpy as np
from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder, compute_shapley
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

YAML_WITH_FORMULA = """
metrics:
  - name: order_count
    source: dbt.metric.order_count
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
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
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("revenue", draws=100, tune=100)

    assert "sin_weekly_h1" in trace.posterior
    assert "cos_weekly_h1" in trace.posterior


def test_get_summary_hdi():
    """Summary should use 95% HDI."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)
    builder.build_and_sample("order_count", draws=100, tune=100)

    summary = builder.get_summary("order_count")
    hdi_cols = [c for c in summary.columns if "hdi" in c]
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


# --- Formula model tests ---

def test_formula_node_samples_without_beta():
    """A formula node should fit a residual BSTS — no beta regressor needed."""
    parser = Parser(YAML_WITH_FORMULA)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("revenue", draws=100, tune=100)

    assert "trend" in trace.posterior
    assert "beta" not in trace.posterior


def test_formula_node_registered():
    """After sampling, the formula should be stored in formula_nodes."""
    parser = Parser(YAML_WITH_FORMULA)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)
    builder.build_and_sample("revenue", draws=100, tune=100)

    assert "revenue" in builder.formula_nodes
    assert builder.formula_nodes["revenue"] == "order_count * average_order_value"


# --- Shapley attribution tests ---

def test_compute_shapley_multiplicative_sums_to_gap():
    """Shapley values for a product formula must sum to the total gap."""
    baselines = {"orders": 100.0, "aov": 50.0}
    actuals = {"orders": 110.0, "aov": 55.0}
    formula = "orders * aov"

    sv = compute_shapley(formula, ["orders", "aov"], baselines, actuals)

    gap = 110 * 55 - 100 * 50  # 6050 - 5000 = 1050
    assert abs(sum(sv.values()) - gap) < 1e-6


def test_compute_shapley_multiplicative_known_values():
    """For a 2-player multiplicative game, Shapley values have a closed form."""
    baselines = {"orders": 100.0, "aov": 50.0}
    actuals = {"orders": 110.0, "aov": 55.0}
    formula = "orders * aov"

    sv = compute_shapley(formula, ["orders", "aov"], baselines, actuals)

    # φ(orders) = Δorders * (baseline_aov + actual_aov) / 2 = 10 * 52.5 = 525
    # φ(aov)    = Δaov * (baseline_orders + actual_orders) / 2 = 5 * 105 = 525
    assert abs(sv["orders"] - 525.0) < 1e-6
    assert abs(sv["aov"] - 525.0) < 1e-6


def test_compute_shapley_additive():
    """For additive formulas, Shapley values equal the individual deltas."""
    baselines = {"a": 100.0, "b": 200.0}
    actuals = {"a": 120.0, "b": 230.0}
    formula = "a + b"

    sv = compute_shapley(formula, ["a", "b"], baselines, actuals)

    assert abs(sv["a"] - 20.0) < 1e-6
    assert abs(sv["b"] - 30.0) < 1e-6


def test_model_builder_compute_shapley():
    """ModelBuilder.compute_shapley returns attribution keyed by parent names."""
    parser = Parser(YAML_WITH_FORMULA)
    data = generate_mock_data(n_days=100)
    builder = ModelBuilder(parser.dag, data)

    result = builder.compute_shapley(
        target_metric_name="revenue",
        reference_start="2024-01-01",
        reference_end="2024-02-15",
        analysis_start="2024-02-16",
        analysis_end="2024-04-09",
    )

    assert "attribution" in result
    assert set(result["attribution"].keys()) == {"order_count", "average_order_value"}
    assert abs(result["gap"] - (result["actual"] - result["baseline"])) < 1e-3
    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-3


def test_compute_shapley_no_formula_raises():
    """compute_shapley should raise if the metric has no formula."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    with pytest.raises(ValueError, match="no formula"):
        builder.compute_shapley(
            "order_count", "2024-01-01", "2024-02-01", "2024-02-01", "2024-03-01"
        )


# --- Inference method tests ---

def test_invalid_inference_method_raises():
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    with pytest.raises(ValueError, match="inference_method"):
        builder.build_and_sample("order_count", draws=100, tune=100, inference_method="mcmc")


def test_advi_inference_samples_posterior():
    """ADVI should produce a valid trace with posterior samples."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    builder = ModelBuilder(parser.dag, data)

    trace = builder.build_and_sample("order_count", draws=200, tune=100, inference_method="advi")

    assert trace is not None
    assert "trend" in trace.posterior

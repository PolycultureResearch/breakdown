import numpy as np
import pandas as pd
import pytest

from breakdown.engine.model import (
    FitResult,
    compute_shapley,
    fit_metric,
    identifiable_harmonics,
    scale_prior_params,
    seasonal_window_delta,
    summarize_trace,
)
from breakdown.parser import Parser, Seasonality
from tests.synthetic import generate_mock_data

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


def test_fit_metric_sampling():
    """Basic end-to-end: model builds and samples without error."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    assert "beta" in trace.posterior
    assert "trend" in trace.posterior


def test_fit_metric_root_metric():
    """A root metric (no parents) should sample with no beta variable."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "daily_sessions", draws=100, tune=100).trace

    assert "trend" in trace.posterior
    assert "beta" not in trace.posterior


def test_fit_metric_with_priors():
    """Priors specified in YAML should be applied without error."""
    parser = Parser(YAML_WITH_PRIORS)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    assert "beta" in trace.posterior
    summary = summarize_trace(trace)
    assert "beta[0]" in summary.index


def test_fit_metric_with_seasonality():
    """Seasonality components from YAML should appear in the trace."""
    parser = Parser(YAML_WITH_SEASONALITY)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "revenue", draws=100, tune=100).trace

    assert "sin_weekly_h1" in trace.posterior
    assert "cos_weekly_h1" in trace.posterior


def test_summarize_trace_hdi():
    """Summary should use 95% HDI."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    summary = summarize_trace(trace)
    hdi_cols = [c for c in summary.columns if "hdi" in c]
    assert len(hdi_cols) == 2


def test_missing_column_raises():
    """fit_metric should raise ValueError if a metric column is absent."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50).drop(columns=["order_count"])

    with pytest.raises(ValueError, match="Columns missing from data"):
        fit_metric(parser.dag, data, "order_count", draws=100, tune=100)


def test_nan_column_raises():
    """fit_metric should raise ValueError if any column contains NaN."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    data.loc[5, "daily_sessions"] = float("nan")

    with pytest.raises(ValueError, match="NaN values found"):
        fit_metric(parser.dag, data, "order_count", draws=100, tune=100)


# --- Formula model tests ---


def test_formula_node_samples_without_beta():
    """A formula node should fit a residual BSTS — no beta regressor needed."""
    parser = Parser(YAML_WITH_FORMULA)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "revenue", draws=100, tune=100).trace

    assert "trend" in trace.posterior
    assert "beta" not in trace.posterior


# --- Shapley computation tests (pure function) ---


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


def test_compute_shapley_vectorized_per_day():
    """Array inputs run one Shapley game per day; per-day-averaged values sum
    to v(all) − v(∅) = mean_analysis(formula(x_t)) − formula(reference means)."""
    actuals = {"a": np.array([110.0, 90.0]), "b": np.array([55.0, 45.0])}
    baselines = {"a": np.full(2, 100.0), "b": np.full(2, 50.0)}

    phi = compute_shapley("a * b", ["a", "b"], baselines, actuals)

    assert isinstance(phi["a"], np.ndarray) and phi["a"].shape == (2,)
    # v(all) = mean(110*55, 90*45) = 5050; v(empty) = 100*50 = 5000
    total = phi["a"].mean() + phi["b"].mean()
    assert abs(total - 50.0) < 1e-9


def test_compute_shapley_mismatched_lengths_raise():
    with pytest.raises(ValueError, match="length"):
        compute_shapley(
            "a * b",
            ["a", "b"],
            {"a": np.zeros(3), "b": np.zeros(2)},
            {"a": np.zeros(3), "b": np.zeros(3)},
        )


# --- Seasonal window delta (pure helper, T5) ---


def test_seasonal_window_delta_matches_numpy():
    """With known Fourier coefficients per sample, the helper must reproduce a
    direct numpy computation of the window-mean difference exactly."""
    import arviz as az

    n_samples = 8
    a1 = np.linspace(0.5, 2.0, n_samples)  # sin h1 coefficient, varies per sample
    zeros = np.zeros(n_samples)
    trace = az.from_dict(
        posterior={
            "sin_weekly_h1": a1[None, :],  # (chain, draw)
            "cos_weekly_h1": zeros[None, :],
            "sin_weekly_h2": zeros[None, :],
            "cos_weekly_h2": zeros[None, :],
        }
    )
    t_ref = np.arange(0, 14)
    t_an = np.arange(14, 24)

    delta = seasonal_window_delta(trace, [Seasonality(period=7, name="weekly")], t_ref, t_an)

    expected = a1 * (np.sin(2 * np.pi * t_an / 7).mean() - np.sin(2 * np.pi * t_ref / 7).mean())
    assert delta.shape == (n_samples,)
    np.testing.assert_allclose(delta, expected, atol=1e-10)


def test_seasonal_window_delta_no_seasonality_is_zero():
    import arviz as az

    trace = az.from_dict(posterior={"alpha": np.zeros((1, 5))})
    delta = seasonal_window_delta(trace, [], np.arange(5), np.arange(5, 10))

    assert delta.shape == (5,)
    assert (delta == 0).all()


# --- Prior scaling tests ---


def test_scale_prior_params_normal():
    scaled = scale_prior_params("Normal", {"mu": 0.1, "sigma": 0.02}, 2.0)
    assert np.isclose(scaled["mu"], 0.2)
    assert np.isclose(scaled["sigma"], 0.04)


def test_scale_prior_params_halfnormal():
    scaled = scale_prior_params("HalfNormal", {"sigma": 0.5}, 2.0)
    assert np.isclose(scaled["sigma"], 1.0)


def test_scale_prior_params_exponential():
    # Scaling the variable by s divides the rate by s
    scaled = scale_prior_params("Exponential", {"lam": 4.0}, 2.0)
    assert np.isclose(scaled["lam"], 2.0)


def test_scale_prior_params_lognormal():
    # Scaling the variable by s shifts mu by log(s); sigma unchanged
    scaled = scale_prior_params("LogNormal", {"mu": 1.0, "sigma": 0.5}, float(np.e))
    assert np.isclose(scaled["mu"], 2.0)
    assert scaled["sigma"] == 0.5


def test_scale_prior_params_unknown_distribution_raises():
    with pytest.raises(ValueError, match="Unsupported prior distribution"):
        scale_prior_params("Cauchy", {}, 1.0)


def test_beta_raw_recovers_business_units():
    """With a tight Normal(0.1, 0.02) prior in business units and mock data
    generated with a true coefficient of 0.1, the raw-scale posterior should
    land near 0.1 — not near the z-scored coefficient."""
    parser = Parser(YAML_WITH_PRIORS)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=200, tune=200).trace

    assert "beta_raw" in trace.posterior
    beta_raw_mean = float(trace.posterior["beta_raw"].mean())
    assert 0.05 < beta_raw_mean < 0.15


def test_halfnormal_prior_constrains_beta_positive():
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "HalfNormal"
        params: { sigma: 0.2 }
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    assert (trace.posterior["beta"].values > 0).all()
    assert (trace.posterior["beta_raw"].values > 0).all()


def test_per_parent_prior_overrides_coefficient():
    """A parent-specific prior wins over the shared `coefficient` prior."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions, average_order_value]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
      daily_sessions:
        distribution: "HalfNormal"
        params: { sigma: 0.2 }
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    assert "beta_daily_sessions" in trace.posterior
    assert "beta_average_order_value" in trace.posterior
    assert (trace.posterior["beta_daily_sessions"].values > 0).all()
    assert "beta" in trace.posterior


def test_unsupported_prior_distribution_in_engine_raises():
    """The engine must reject unknown distributions rather than silently
    substituting Normal(0, 1)."""
    parser = Parser(YAML_WITH_PRIORS)
    data = generate_mock_data(n_days=50)
    parser.dag.nodes["order_count"]["definition"].priors["coefficient"].distribution = "Cauchy"

    with pytest.raises(ValueError, match="Unsupported prior distribution"):
        fit_metric(parser.dag, data, "order_count", draws=100, tune=100)


# --- Lagged regressor tests ---


def test_lagged_model_trims_rows():
    """A lag of 5 on a 50-row dataset should leave 45 aligned rows in the fit."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    lags: { daily_sessions: 5 }
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(parser.dag, data, "order_count", draws=100, tune=100).trace

    assert trace.posterior["trend"].values.shape[-1] == 45


def test_lag_recovery_beta_raw():
    """When y[t] = 0.5 * x[t-5] + small noise, fitting with the correct lag
    should recover a raw-scale coefficient near 0.5."""
    rng = np.random.default_rng(7)
    n = 120
    lag = 5
    x = 100.0 + np.cumsum(rng.normal(0, 3.0, n))
    y = np.empty(n)
    y[:lag] = 0.5 * x[0]
    y[lag:] = 0.5 * x[:-lag]
    y = y + rng.normal(0, 0.5, n)
    data = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x": x,
            "y": y,
        }
    )

    yaml_content = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
    lags: { x: 5 }
"""
    parser = Parser(yaml_content)

    trace = fit_metric(parser.dag, data, "y", draws=300, tune=300).trace

    beta_raw_mean = float(trace.posterior["beta_raw"].mean())
    assert 0.25 < beta_raw_mean < 0.75


def test_lag_too_few_rows_raises():
    """If applying the lag leaves fewer than 10 rows, raise."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    lags: { daily_sessions: 8 }
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=15)

    with pytest.raises(ValueError, match="Not enough rows after applying lags"):
        fit_metric(parser.dag, data, "order_count", draws=50, tune=50)


# --- Inference method tests ---


def test_invalid_inference_method_raises():
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    with pytest.raises(ValueError, match="inference_method"):
        fit_metric(parser.dag, data, "order_count", draws=100, tune=100, inference_method="mcmc")


def test_advi_inference_samples_posterior():
    """ADVI should produce a valid trace with posterior samples."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    trace = fit_metric(
        parser.dag, data, "order_count", draws=200, tune=100, inference_method="advi"
    ).trace

    assert trace is not None
    assert "trend" in trace.posterior


# --- FitResult contract (T1) ---


def test_fit_metric_returns_fit_result():
    """fit_metric returns a FitResult carrying the normalization constants and
    the fitted date index, not just the trace."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    result = fit_metric(parser.dag, data, "order_count", draws=100, tune=100)

    assert isinstance(result, FitResult)
    assert result.y_std > 0
    assert result.parents == ["daily_sessions"]
    assert len(result.dates) == 50
    assert result.x_stds.shape == (1,)
    assert result.inference_method == "nuts"
    assert result.fit_end is None


def test_fit_result_dates_reflect_lag_trim():
    """With a lag of 5 on 50 days, the fitted date index drops the first 5 rows."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    lags: { daily_sessions: 5 }
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=50)

    result = fit_metric(parser.dag, data, "order_count", draws=100, tune=100)

    assert len(result.dates) == 45
    assert result.dates[0] == data["date"].iloc[5]


# --- Pre-anomaly fit window (T2) ---


def test_fit_end_excludes_anomaly_window():
    """Fitting only on rows before fit_end recovers the normal-regime coefficient,
    even when a driver outside the tree shifts y in the excluded window."""
    rng = np.random.default_rng(0)
    n = 120
    x = 100.0 + np.cumsum(rng.normal(0, 3.0, n))
    y = 0.5 * x + rng.normal(0, 0.5, n)
    y[90:] -= 20.0  # an off-tree driver, days 90-119
    dates = pd.date_range("2024-01-01", periods=n)
    data = pd.DataFrame({"date": dates, "x": x, "y": y})

    yaml_content = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""
    parser = Parser(yaml_content)
    fit_end = str(dates[90].date())

    result = fit_metric(parser.dag, data, "y", draws=300, tune=300, fit_end=fit_end)

    beta_raw_mean = float(result.trace.posterior["beta_raw"].mean())
    assert 0.4 <= beta_raw_mean <= 0.6
    assert len(result.dates) == 90


def test_fit_end_too_few_rows_raises():
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    with pytest.raises(ValueError, match="before fit_end"):
        fit_metric(
            parser.dag,
            data,
            "order_count",
            draws=50,
            tune=50,
            fit_end=str(data["date"].iloc[8].date()),
        )


# --- Trend specification (T3) ---


def test_trend_sigma_config_widens_posterior():
    """A larger `trend.sigma` prior admits a larger sigma_trend posterior on the
    same data (weak but stable check that the YAML knob is wired through)."""
    data = generate_mock_data(n_days=50)

    default_yaml = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
"""
    wide_yaml = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    trend: { sigma: 0.2 }
"""
    # Same seed for both fits: the claim is about the prior, so the sampler's
    # randomness has to be held fixed or the comparison is a coin flip on a
    # weak effect. Observed failing once in ~5 unseeded runs.
    default = fit_metric(
        Parser(default_yaml).dag, data, "order_count", draws=200, tune=200, random_seed=42
    ).trace
    wide = fit_metric(
        Parser(wide_yaml).dag, data, "order_count", draws=200, tune=200, random_seed=42
    ).trace

    assert float(wide.posterior["sigma_trend"].mean()) > float(
        default.posterior["sigma_trend"].mean()
    )


def test_tight_trend_keeps_beta_identified():
    """The point of the tight non-centered trend: beta_raw stays sharply
    identified rather than being stolen by a flexible random walk."""
    rng = np.random.default_rng(0)
    n = 120
    x = 100.0 + np.cumsum(rng.normal(0, 3.0, n))
    y = 0.5 * x + rng.normal(0, 1.0, n)
    data = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x": x,
            "y": y,
        }
    )
    yaml_content = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""
    parser = Parser(yaml_content)

    result = fit_metric(parser.dag, data, "y", draws=300, tune=300)

    summary = summarize_trace(result.trace)
    lo = summary.loc["beta_raw[0]", "hdi_2.5%"]
    hi = summary.loc["beta_raw[0]", "hdi_97.5%"]
    assert hi - lo < 0.4
    assert lo <= 0.5 <= hi


# --- Convergence diagnostics (T8) ---


def test_nuts_diagnostics_ok_on_well_behaved_data():
    """A NUTS fit of the (post-T3, non-centered) model on clean synthetic data
    must self-report as healthy. Seeded: an unseeded marginal run can trip the
    diagnostic thresholds by chance, which made this the suite's one flake."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    result = fit_metric(parser.dag, data, "order_count", draws=300, tune=300, random_seed=42)

    d = result.diagnostics
    assert d["method"] == "nuts"
    assert d["fit_quality"] == "ok"
    assert isinstance(d["divergences"], int)
    assert d["max_rhat"] < 1.05


def test_advi_diagnostics_present():
    """ADVI fits report a fit_quality verdict and the recent ELBO movement."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    result = fit_metric(parser.dag, data, "order_count", draws=200, inference_method="advi")

    d = result.diagnostics
    assert d["method"] == "advi"
    assert d["fit_quality"] in ("ok", "suspect")
    assert "elbo_drop" in d and isinstance(d["elbo_drop"], float)


# --- Grain-aware fitting (1.7 phase 3) ---

MIXED_GRAIN_YAML = """
metrics:
  - name: daily_starts
    source: dbt.metric.daily_starts
  - name: weekly_total
    source: dbt.metric.weekly_total
    grain: week
    parents: [daily_starts]
"""


def _mixed_grain_data(n_weeks=30, seed=7, beta=0.5):
    """Daily flow parent; weekly child = beta * (weekly sum of parent) + noise."""
    from breakdown.grains import build_grained

    rng = np.random.default_rng(seed)
    n_days = n_weeks * 7
    days = pd.date_range("2024-01-01", periods=n_days)  # Monday start
    starts = 100.0 + rng.normal(0, 8.0, n_days)
    weekly_sum = starts.reshape(n_weeks, 7).sum(axis=1)
    weeks = pd.date_range("2024-01-01", periods=n_weeks, freq="W-MON")
    total = beta * weekly_sum + rng.normal(0, 5.0, n_weeks)
    return build_grained(
        {
            "daily_starts": pd.DataFrame({"date": days, "daily_starts": starts}),
            "weekly_total": pd.DataFrame({"date": weeks, "weekly_total": total}),
        },
        {"daily_starts": "day", "weekly_total": "week"},
        {"daily_starts": "flow", "weekly_total": "flow"},
    )


def test_fit_metric_weekly_node_with_daily_parent():
    """A native-weekly node fits at week grain against the summed daily
    parent, recovering the planted coefficient."""
    parser = Parser(MIXED_GRAIN_YAML)
    data = _mixed_grain_data()

    result = fit_metric(parser.dag, data, "weekly_total", draws=300, inference_method="advi")

    assert result.grain == "week"
    assert len(result.dates) == 30
    assert all(d.dayofweek == 0 for d in result.dates)
    beta_raw = result.trace.posterior["beta_raw"].values.reshape(-1)
    assert abs(np.mean(beta_raw) - 0.5) < 0.1


def test_fit_end_cuts_whole_periods():
    """A weekly node with a mid-week fit_end excludes the straddling week."""
    parser = Parser(MIXED_GRAIN_YAML)
    data = _mixed_grain_data()

    # 2024-07-25 is a Thursday: the week starting Mon 2024-07-22 must be cut.
    result = fit_metric(
        parser.dag, data, "weekly_total", draws=200, inference_method="advi", fit_end="2024-07-25"
    )

    assert result.dates.max() == pd.Timestamp("2024-07-15")


def test_fit_end_too_few_periods_message_is_grain_aware():
    parser = Parser(MIXED_GRAIN_YAML)
    data = _mixed_grain_data()

    with pytest.raises(ValueError, match="whole week periods before fit_end"):
        fit_metric(
            parser.dag,
            data,
            "weekly_total",
            draws=100,
            inference_method="advi",
            fit_end="2024-02-15",
        )


def test_unidentifiable_seasonality_warns_in_diagnostics():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    seasonality:
      - period: 60
        name: bimonthly
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=80)[["date", "daily_sessions"]].rename(
        columns={"daily_sessions": "dau"}
    )

    result = fit_metric(parser.dag, data, "dau", draws=200, inference_method="advi")

    warnings = result.diagnostics.get("seasonality_warnings", [])
    assert len(warnings) == 1 and "unidentifiable" in warnings[0]


# --- Nyquist harmonic filter (1.1) ---


@pytest.mark.parametrize(
    "period,expected",
    [(3, (1,)), (4, (1,)), (5, (1, 2)), (7, (1, 2)), (12, (1, 2)), (365, (1, 2))],
)
def test_identifiable_harmonics_respects_nyquist(period, expected):
    assert identifiable_harmonics(period) == expected


@pytest.mark.parametrize("period", [3, 4, 5, 7])
def test_fitted_seasonal_design_is_full_rank(period):
    """The regression the filter exists to prevent: with both harmonics
    unconditionally, periods 3 and 4 give a rank-deficient design whose extra
    parameters are sampled but never informed by the data."""
    t = np.arange(4 * period)
    cols = [np.ones(len(t))]
    for k in identifiable_harmonics(period):
        cols.append(np.sin(2 * np.pi * k * t / period))
        cols.append(np.cos(2 * np.pi * k * t / period))
    design = np.column_stack(cols)
    assert np.linalg.matrix_rank(design) == design.shape[1]


def test_period_four_drops_second_harmonic_and_says_so():
    """A dropped harmonic is a property of the period, not the sample size, so
    it is reported separately from the not-enough-data warning — and the fitted
    component is narrower than the YAML implies."""
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    seasonality:
      - period: 4
        name: monthly_ish
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=80)[["date", "daily_sessions"]].rename(
        columns={"daily_sessions": "dau"}
    )

    result = fit_metric(parser.dag, data, "dau", draws=200, inference_method="advi")

    warnings = result.diagnostics.get("seasonality_warnings", [])
    assert any("harmonic(s) [2] dropped" in w for w in warnings)
    # The dropped harmonic has no posterior variable...
    assert "sin_monthly_ish_h2" not in result.trace.posterior
    # ...and the kept one does.
    assert "sin_monthly_ish_h1" in result.trace.posterior


def test_seasonal_window_delta_matches_the_filtered_fit():
    """`seasonal_window_delta` reads posterior variables by name, so its filter
    must mirror the model's exactly or a dropped harmonic is a KeyError."""
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    seasonality:
      - period: 4
        name: monthly_ish
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=80)[["date", "daily_sessions"]].rename(
        columns={"daily_sessions": "dau"}
    )
    result = fit_metric(parser.dag, data, "dau", draws=200, inference_method="advi")
    defn = parser.dag.nodes["dau"]["definition"]

    delta = seasonal_window_delta(
        result.trace, defn.seasonality, np.arange(0, 20), np.arange(20, 40)
    )
    assert delta.shape == (200,)
    assert np.isfinite(delta).all()


# --- expected_signs diagnostic ---

SIGNED_YAML = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
    expected_signs: { x: positive }
"""


def _negative_edge_data(seed=13, n=120):
    """y falls when x rises: the true coefficient is decisively negative."""
    rng = np.random.default_rng(seed)
    x = 100.0 + rng.normal(0, 5.0, n)
    y = 500.0 - 2.0 * x + rng.normal(0, 1.0, n)
    dates = pd.date_range("2024-01-01", periods=n)
    return pd.DataFrame({"date": dates, "x": x, "y": y})


def test_contradicted_expected_sign_warns():
    parser = Parser(SIGNED_YAML)
    data = _negative_edge_data()

    result = fit_metric(parser.dag, data, "y", draws=300, inference_method="advi")

    warnings = result.diagnostics.get("sign_warnings", [])
    assert len(warnings) == 1
    assert "declared positive effect" in warnings[0]
    assert "contradicts" in warnings[0]


def test_supported_expected_sign_stays_quiet():
    yaml_flipped = SIGNED_YAML.replace("positive", "negative")
    parser = Parser(yaml_flipped)
    data = _negative_edge_data()

    result = fit_metric(parser.dag, data, "y", draws=300, inference_method="advi")

    assert "sign_warnings" not in result.diagnostics

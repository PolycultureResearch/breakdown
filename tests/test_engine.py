import numpy as np
import pandas as pd
import pytest

from breakdown.engine.model import (
    MAX_SHAPLEY_PARENTS,
    FitResult,
    compute_shapley,
    fit_metric,
    identifiable_harmonics,
    scale_prior_params,
    seasonal_window_delta,
    summarize_trace,
)
from breakdown.parser import Parser, Seasonality
from tests.synthetic import generate_mock_data, win

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

    with pytest.raises(ValueError, match="Cannot fit over undefined periods"):
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


def test_compute_shapley_at_cap_still_works():
    """Exactly `MAX_SHAPLEY_PARENTS` parents is supported, and exact."""
    parents = [f"p{i}" for i in range(MAX_SHAPLEY_PARENTS)]
    baselines = {p: 100.0 + i for i, p in enumerate(parents)}
    actuals = {p: 110.0 + 2 * i for i, p in enumerate(parents)}
    formula = " + ".join(parents)

    sv = compute_shapley(formula, parents, baselines, actuals, node="total")

    gap = sum(actuals.values()) - sum(baselines.values())
    assert abs(sum(sv.values()) - gap) < 1e-6


def test_compute_shapley_over_cap_refuses_by_name():
    """Past the cap the O(2^n) enumeration is refused, not approximated —
    naming the node, its parent count, the cap, and the remedy."""
    n = MAX_SHAPLEY_PARENTS + 1
    parents = [f"p{i}" for i in range(n)]
    vals = {p: 1.0 for p in parents}

    with pytest.raises(ValueError) as exc:
        compute_shapley(" + ".join(parents), parents, vals, vals, node="total_revenue")

    msg = str(exc.value)
    assert "total_revenue" in msg
    assert f"{n} parents" in msg
    assert f"at most {MAX_SHAPLEY_PARENTS}" in msg
    assert "Split" in msg


def test_compute_shapley_over_cap_refuses_without_node_name():
    """Callers that don't have the node name still get an identifiable refusal."""
    parents = [f"p{i}" for i in range(MAX_SHAPLEY_PARENTS + 1)]
    vals = {p: 1.0 for p in parents}
    formula = " + ".join(parents)

    with pytest.raises(ValueError, match="too many parents"):
        compute_shapley(formula, parents, vals, vals)


def test_run_rca_refuses_wide_formula_node():
    """The cap covers the path that actually pays the cost: RCA's six
    enumerations per formula node cannot bypass it."""
    from breakdown.engine.rca import run_rca

    n = MAX_SHAPLEY_PARENTS + 1
    parents = [f"p{i}" for i in range(n)]
    yaml = "metrics:\n"
    for p in parents:
        yaml += f"  - name: {p}\n    source: mock.{p}\n    kind: flow\n"
    yaml += (
        "  - name: total\n    source: mock.total\n    kind: flow\n"
        f'    formula: "{" + ".join(parents)}"\n'
        f"    parents: [{', '.join(parents)}]\n"
    )

    n_days = 60
    rng = np.random.default_rng(0)
    cols = {"date": pd.date_range("2024-01-01", periods=n_days)}
    total = np.zeros(n_days)
    for p in parents:
        v = 100 + np.cumsum(rng.normal(0, 3, n_days))
        cols[p] = v
        total = total + v
    cols["total"] = total

    with pytest.raises(ValueError, match=f"at most {MAX_SHAPLEY_PARENTS}"):
        run_rca(
            Parser(yaml).dag,
            pd.DataFrame(cols),
            {},
            "total",
            reference_start="2024-01-01",
            reference_end="2024-02-10",
            analysis_start="2024-02-11",
            analysis_end="2024-02-29",
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

    with pytest.raises(ValueError, match=r"Only 7 whole day periods to fit 'order_count'"):
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
    """ADVI fits report both checks: the optimizer's, and the approximation's.

    `elbo_drop` says whether the optimizer settled; `khat` / `khat_status` say
    how far from the posterior it settled (roadmap S2). The second is the one
    the intervals depend on, so it is present on every variational fit, and
    `khat` is a finite float or is withheld under a named status — never a NaN
    on its way to an encoder.
    """
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)

    result = fit_metric(
        parser.dag, data, "order_count", draws=200, inference_method="advi", random_seed=0
    )

    d = result.diagnostics
    assert d["method"] == "advi"
    assert d["fit_quality"] in ("ok", "suspect")
    assert "elbo_drop" in d and isinstance(d["elbo_drop"], float)
    assert d["khat_status"] in ("ok", "suspect", "unusable", "unavailable")
    if d["khat_status"] == "unavailable":
        assert d["khat"] is None
        assert d["khat_se"] is None
    else:
        assert isinstance(d["khat"], float) and np.isfinite(d["khat"])
    # Roadmap S22: the estimate's own Monte-Carlo error, withheld as None
    # rather than zeroed when it cannot be computed — never a NaN on its way
    # to an encoder (rule 3).
    assert d["khat_se"] is None or (isinstance(d["khat_se"], float) and np.isfinite(d["khat_se"]))
    assert isinstance(d["khat_borderline"], bool)
    # `ok` travels without an explanation attached — unless the estimate
    # cannot tell `ok` from `suspect`, which is a thing to say out loud.
    assert ("khat_warnings" in d) == (d["khat_status"] != "ok" or d["khat_borderline"])


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

    with pytest.raises(ValueError, match=r"whole week periods to fit 'weekly_total'"):
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


# --- The fit-length floor, on every path (M2) --------------------------------
#
# `MIN_FIT_PERIODS` used to be checked in exactly two places: behind
# `fit_end is not None`, and behind a node declaring lags. Neither is true on
# `POST /analyze`'s default path or `run_scenario`'s, so a three-observation
# series fitted and reported `fit_quality: "ok"` while `breakdown doctor`
# called the same metric "not fittable yet" and the README named 10 whole
# periods as the floor. The engine and the doctor answered different questions
# about the same tree, and the engine is the one that answers a user.

THIN_YAML = """
metrics:
  - name: sessions
    source: mock.sessions
    kind: flow
  - name: signups
    source: mock.signups
    kind: flow
    parents: [sessions]
"""


def _thin_data(n):
    rng = np.random.default_rng(3)
    sessions = 100.0 + np.cumsum(rng.normal(0, 5.0, n))
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "sessions": sessions,
            "signups": 0.1 * sessions + rng.normal(0, 0.3, n),
        }
    )


def test_default_path_refuses_a_series_below_the_floor():
    """No fit_end, no lags — the path everything actually uses. This used to
    sample happily and report `fit_quality: "ok"`."""
    parser = Parser(THIN_YAML)

    with pytest.raises(ValueError) as e:
        fit_metric(
            parser.dag, _thin_data(3), "signups", draws=200, inference_method="advi", random_seed=0
        )

    assert "Only 3 whole day periods to fit 'signups'" in str(e.value)
    assert "need >= 10" in str(e.value)


def test_default_path_accepts_exactly_the_floor():
    """The floor is inclusive: 10 periods fit, 9 do not. Pinned so a later
    off-by-one can't quietly move the line."""
    parser = Parser(THIN_YAML)

    result = fit_metric(
        parser.dag, _thin_data(10), "signups", draws=200, inference_method="advi", random_seed=0
    )
    assert len(result.dates) == 10

    with pytest.raises(ValueError, match="Only 9 whole day periods"):
        fit_metric(
            parser.dag, _thin_data(9), "signups", draws=200, inference_method="advi", random_seed=0
        )


def test_root_node_refuses_a_series_below_the_floor():
    """A parentless node fits trend + seasonality only, and is subject to the
    same floor — it is the trend state per observation that the floor is
    about, and a root has one of those per period too."""
    parser = Parser(THIN_YAML)

    with pytest.raises(ValueError, match="Only 4 whole day periods to fit 'sessions'"):
        fit_metric(
            parser.dag, _thin_data(4), "sessions", draws=200, inference_method="advi", random_seed=0
        )


def test_floor_counts_periods_after_the_fit_end_cut():
    """A long window still refuses when the fit_end cut leaves too little —
    the message names the cut rather than the window."""
    parser = Parser(THIN_YAML)
    data = _thin_data(60)

    with pytest.raises(ValueError) as e:
        fit_metric(
            parser.dag,
            data,
            "signups",
            draws=200,
            inference_method="advi",
            fit_end="2024-01-06",
            random_seed=0,
        )

    assert "Only 5 whole day periods to fit 'signups'" in str(e.value)
    assert "60 whole day periods cover 'signups' and its parents" in str(e.value)
    assert "fit_end=2024-01-06" in str(e.value)


def test_floor_counts_periods_after_the_lag_trim():
    """The lag trim comes out of the count too, and the message says by how
    much."""
    yaml_content = THIN_YAML + "    lags: { sessions: 4 }\n"
    parser = Parser(yaml_content)

    with pytest.raises(ValueError) as e:
        fit_metric(
            parser.dag, _thin_data(13), "signups", draws=200, inference_method="advi", random_seed=0
        )

    assert "Only 9 whole day periods to fit 'signups'" in str(e.value)
    assert "the 4-period max lag trims 4 more" in str(e.value)


def test_floor_counts_periods_after_the_parent_join():
    """What the fit trains on, not what was handed in.

    A weekly node with a daily parent is fitted on the *inner join* at week
    grain. 63 daily rows is plenty of data by any row count, and nine whole
    weeks is still below the floor.
    """
    parser = Parser(MIXED_GRAIN_YAML)
    data = _mixed_grain_data(n_weeks=9)

    with pytest.raises(ValueError, match="Only 9 whole week periods to fit 'weekly_total'"):
        fit_metric(parser.dag, data, "weekly_total", draws=200, inference_method="advi")


def test_doctor_and_engine_agree_about_a_thin_tree(tmp_path):
    """The defect was a disagreement, so this is the test that closes it: over
    one window, on one tree, `doctor` and `fit_metric` must reach the same
    verdict — both refusing at 5 days, both accepting at 31."""
    from breakdown.data_fetch import MockDataFetcher
    from breakdown.doctor import run_doctor

    tree = tmp_path / "tree.yml"
    tree.write_text("provider:\n  type: mock\n" + THIN_YAML)
    parser = Parser(tree.read_text())

    def fit_over(start, end):
        fetcher = MockDataFetcher(parser.dag)
        frames = [fetcher.fetch_metric(m, start, end) for m in ("sessions", "signups")]
        data = frames[0].merge(frames[1], on="date")
        return fit_metric(
            parser.dag, data, "signups", draws=200, inference_method="advi", random_seed=0
        )

    thin = {r.name: r for r in run_doctor(str(tree), "2024-01-01", "2024-01-05")}["fit readiness"]
    assert thin.status == "fail" and "not fittable yet" in thin.detail
    with pytest.raises(ValueError, match="need >= 10"):
        fit_over("2024-01-01", "2024-01-05")

    ample = {r.name: r for r in run_doctor(str(tree), "2024-01-01", "2024-01-31")}["fit readiness"]
    assert ample.status == "pass" and "not fittable yet" not in ample.detail
    assert len(fit_over("2024-01-01", "2024-01-31").dates) == 31


# ---------------------------------------------------------------------------
# S20's disclosure half: a zero-inflated fit window is disclosed, not silent.


def test_zero_inflation_helper_thresholds():
    from breakdown.engine.model import _zero_inflation_warnings

    live = np.full(40, 100.0)
    assert _zero_inflation_warnings(live, "m", "day") == []

    dark = live.copy()
    dark[:12] = 0.0  # 30% exact zeros, one leading run
    (msg,) = _zero_inflation_warnings(dark, "m", "day")
    assert "12 of 40" in msg and "30%" in msg and "longest run 12" in msg
    assert "Gaussian" in msg and "S20" in msg

    # Near-zero is a different regime, not this one.
    tiny = live.copy()
    tiny[:12] = 1e-9
    assert _zero_inflation_warnings(tiny, "m", "day") == []

    assert _zero_inflation_warnings(np.array([]), "m", "day") == []


def test_zero_inflated_fit_carries_likelihood_warnings():
    """The Gaussian likelihood on a mostly-zero series converges and reports
    `fit_quality: "ok"` — convergence measures the optimizer, not the model —
    which made this the one undisclosed misspecification in the engine's
    orbit. The disclosure travels in diagnostics, like `seasonality_warnings`."""
    parser = Parser(SIMPLE_YAML)
    data = generate_mock_data(n_days=50)
    dark = data.copy()
    dark.loc[dark.index[:20], "daily_sessions"] = 0.0  # a 20-day off-season

    fit = fit_metric(parser.dag, dark, "daily_sessions", draws=100, tune=100)
    (msg,) = fit.diagnostics["likelihood_warnings"]
    assert "daily_sessions" in msg and "20 of 50" in msg

    clean = fit_metric(parser.dag, data, "daily_sessions", draws=100, tune=100)
    assert "likelihood_warnings" not in clean.diagnostics


# --- PSIS k-hat: does the approximation match the posterior? (roadmap S2) ---
#
# The ELBO check answers "did the optimizer stop?"; these answer "did it stop
# anywhere near the posterior?". The tests below need a *failing* case above
# all — a diagnostic that has never said no is not evidence that it can.

RIDGE_YAML = """
metrics:
  - name: x1
    source: dbt.metric.x1
  - name: x2
    source: dbt.metric.x2
  - name: y
    source: dbt.metric.y
    parents: [x1, x2]
"""


def _ridge_frame(n: int = 60, seed: int = 7) -> pd.DataFrame:
    """Two near-collinear drifting parents: the posterior is a ridge.

    `x2` is `x1` plus a little noise, so the data pins the *sum* of the two
    coefficients tightly and says almost nothing about the split — and the
    split is exactly what RCA reports. A factorized (mean-field) approximation
    cannot represent a diagonal ridge at all: it collapses to an axis-aligned
    blob somewhere on it. This is the geometry roadmap S4 warns about and the
    one white-paper weakness #1 is about, built small enough to fit in a test.
    """
    rng = np.random.default_rng(seed)
    x1 = 100 + np.cumsum(rng.normal(0, 3, n))
    x2 = x1 * 0.5 + rng.normal(0, 0.4, n)
    y = 1.0 * x1 + 0.5 * x2 + rng.normal(0, 1.0, n)
    return pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=n), "x1": x1, "x2": x2, "y": y}
    )


def test_psis_khat_passes_an_approximation_that_is_exact():
    """The diagnostic must be able to say yes, or its no means nothing.

    Independent standard normals with no likelihood: the posterior *is* the
    prior, a product of independent Gaussians, which is exactly the family
    mean-field ADVI optimizes over. So q converges to p, the importance ratios
    are near-constant, and k-hat lands well inside the good band. Thirty
    dimensions, to rule out "k-hat just fails in high dimension" as the
    explanation for the failing cases below.
    """
    import pymc as pm

    from breakdown.engine.model import _psis_khat

    with pm.Model():
        pm.Normal("z", 0.0, 1.0, shape=30)
        approx = pm.fit(n=20_000, method="advi", progressbar=False, random_seed=0)

    khat, khat_se, status, reason = _psis_khat(approx, n_draws=1000, random_seed=0)
    assert reason is None
    assert status == "ok", f"k-hat {khat} on an exactly-representable posterior"
    assert khat <= 0.5
    # And it knows how well it knows that (roadmap S22): a real, positive,
    # finite standard error, never a NaN or a zero standing in for one.
    assert isinstance(khat_se, float) and np.isfinite(khat_se) and khat_se > 0


def test_psis_khat_catches_an_approximation_that_is_wrong():
    """Neal's funnel: the textbook posterior a factorized Gaussian cannot fit.

    `x ~ Normal(0, exp(v/2))` with `v ~ Normal(0, 3)` has a neck whose width
    depends on `v`, so no product of independent Gaussians is close to it at
    both ends. The optimizer converges perfectly well onto the wrong thing —
    which is the entire failure mode S2 exists to detect — and k-hat must
    refuse it.
    """
    import pymc as pm
    import pytensor.tensor as pt

    from breakdown.engine.model import _psis_khat

    with pm.Model():
        v = pm.Normal("v", 0.0, 3.0)
        pm.Normal("x", 0.0, pt.exp(v / 2), shape=20)
        approx = pm.fit(n=20_000, method="advi", progressbar=False, random_seed=0)

    khat, khat_se, status, reason = _psis_khat(approx, n_draws=1000, random_seed=0)
    assert reason is None
    assert status == "unusable", f"k-hat {khat} on Neal's funnel"
    assert khat > 0.7
    # Far enough past 0.7 that its own error does not reach the edge — the
    # verdict is one this estimate can support (roadmap S22).
    assert khat - khat_se > 0.7


def test_the_published_khat_error_is_the_error_the_estimate_actually_has():
    """Roadmap S22(b). A standard error that is not one is worse than none.

    `_khat_se` is analytic — the generalized Pareto shape parameter's
    asymptotic variance `(1 + k)^2 / M` over the M tail points ArviZ fits,
    scaled by that estimator's shrinkage toward 0.5 — so nothing about the
    published number is checked by the code that produces it. This checks it
    against the thing it claims to describe: hold the approximation fixed,
    re-estimate k-hat over independent draws of the importance ratios, and
    compare the spread of those estimates against the error the engine
    publishes for each of them.

    The band is loose (half to double) because 20 replicates estimate a
    standard deviation to about +/-16% themselves, and because the point is
    not a calibration proof — it is that this number moves with the real
    sampling error rather than being a decoration beside it. Measured over 60
    replicates while S22 was written, the ratio was 1.06 / 1.11 / 0.90 / 1.04
    on four posteriors with k-hat from 0.03 to 1.15.
    """
    import pymc as pm
    import pytensor.tensor as pt

    from breakdown.engine.model import _psis_khat

    with pm.Model():
        v = pm.Normal("v", 0.0, 0.8)
        pm.Normal("x", 0.0, pt.exp(v / 2), shape=8)
        approx = pm.fit(n=20_000, method="advi", progressbar=False, random_seed=0)

    estimates, errors = [], []
    for seed in range(20):
        k, se, status, _ = _psis_khat(approx, n_draws=1000, random_seed=1000 + seed)
        assert status != "unavailable" and k is not None and se is not None
        estimates.append(k)
        errors.append(se)

    empirical = float(np.std(estimates, ddof=1))
    published = float(np.mean(errors))
    assert 0.5 * empirical < published < 2.0 * empirical, (
        f"k-hat's published standard error ({published:.3f}) does not describe the "
        f"spread it actually has ({empirical:.3f})"
    )


def test_a_khat_on_a_band_edge_refuses_to_pick_a_side():
    """Roadmap S22(b), the case it exists for.

    This posterior sits almost exactly on the 0.7 bar: the estimate lands
    either side of it depending on which 1,000 importance ratios were drawn,
    and the spread that decides it is an order of magnitude larger than the
    distance to the edge. Reporting `suspect` or `unusable` there — whichever
    the draw happened to give — hands the reader a verdict about whether their
    intervals are evidence that the estimate cannot support.

    So the band is still the measured one (nothing downstream has to
    reinterpret `khat_status`), and `khat_borderline` is what says it is not
    resolved. `fit_quality` follows the flag, because the gate's question is
    whether this fit can be trusted as-is and the honest answer here is "not
    shown to be".
    """
    import pymc as pm
    import pytensor.tensor as pt

    from breakdown.engine.model import _advi_diagnostics

    with pm.Model():
        v = pm.Normal("v", 0.0, 0.8)
        pm.Normal("x", 0.0, pt.exp(v / 2), shape=8)
        approx = pm.fit(n=20_000, method="advi", progressbar=False, random_seed=0)

    d = _advi_diagnostics(approx, method="advi", target="edge", random_seed=0)

    assert d["khat_status"] in ("suspect", "unusable")
    assert abs(d["khat"] - 0.7) < d["khat_se"]
    assert d["khat_borderline"] is True
    assert d["fit_quality"] == "suspect"
    # One sentence, carrying the estimate, its error and what the pair means —
    # every surface renders `khat_warnings` verbatim, so a second entry could
    # be shown without the first.
    (msg,) = d["khat_warnings"]
    assert "+/-" in msg and "0.7" in msg and "does not separate" in msg


def test_a_khat_that_cannot_state_its_own_error_says_so(monkeypatch):
    """Rule 3, one field over from where S2 applied it.

    A standard error the asymptotics do not cover is withheld as None — never
    a NaN on its way to `allow_nan=False` JSON, and never a zero, which would
    claim the estimate is exact and would make every band edge look resolved.
    The k-hat itself still publishes: "checked, with an error we cannot state"
    and "not checked" are different facts about a fit, and collapsing them
    would lose the more common one.
    """
    import pymc as pm

    import breakdown.engine.model as model

    with pm.Model():
        pm.Normal("z", 0.0, 1.0, shape=5)
        approx = pm.fit(n=2_000, method="advi", progressbar=False, random_seed=0)

    monkeypatch.setattr(model, "_khat_se", lambda khat, log_ratios: None)
    d = model._advi_diagnostics(approx, method="advi", target="m", random_seed=0)

    assert d["khat"] is not None and np.isfinite(d["khat"])
    assert d["khat_se"] is None
    # No error means no band edge can be tested against one, so the estimate
    # is not called borderline on the strength of nothing.
    assert d["khat_borderline"] is False
    assert d["khat_status"] in ("ok", "suspect", "unusable")


def test_khat_flags_a_fit_whose_elbo_check_passed():
    """The whole point of S2, tested as a composition.

    On real trees the two checks disagree in both directions (a White Cube node
    fits with a clean ELBO and k-hat ~1.0), but the ELBO verdict depends on the
    noise in one stochastic loss trace, which is not a thing to assert on. So
    the converged-optimizer half is supplied directly: the real fitted
    approximation, wearing the ELBO history of an optimizer that plainly
    settled. If `fit_quality` still came back "ok" there, it would be exactly
    as uninformative as it was before this landed.
    """
    import pymc as pm
    import pytensor.tensor as pt

    from breakdown.engine.model import _advi_diagnostics

    with pm.Model():
        v = pm.Normal("v", 0.0, 3.0)
        pm.Normal("x", 0.0, pt.exp(v / 2), shape=20)
        approx = pm.fit(n=20_000, method="advi", progressbar=False, random_seed=0)

    class _Settled:
        """The fit, with a loss trace that stopped moving 2,000 steps ago."""

        def __init__(self, inner):
            self._inner = inner
            rng = np.random.default_rng(0)
            self.hist = 100.0 + rng.normal(0, 1.0, 2000)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    settled = _Settled(approx)
    d = _advi_diagnostics(settled, method="advi", target="funnel", random_seed=0)

    # The ELBO check on its own is satisfied: the loss is stationary.
    assert abs(d["elbo_drop"]) < 0.5 * float(np.std(settled.hist[-200:]))
    # And the fit is still refused, on the evidence that actually matters.
    assert d["khat_status"] == "unusable" and d["khat"] > 0.7
    assert d["fit_quality"] == "suspect"
    (msg,) = d["khat_warnings"]
    assert "funnel" in msg and "k-hat" in msg and "NUTS" in msg


def test_a_khat_that_cannot_be_computed_is_withheld_not_zeroed(monkeypatch):
    """Rule 3, on the number this change adds.

    A k-hat is a float on its way to `allow_nan=False` JSON and to
    `round_floats`, which turns a NaN into `null` — an approximation verdict of
    nothing, sitting beside the intervals it is supposed to qualify. So a
    k-hat that cannot be computed is withheld under a named status, and the
    fit says it was *unchecked* rather than saying nothing at all.
    """
    import pymc as pm

    import breakdown.engine.model as model

    with pm.Model():
        pm.Normal("z", 0.0, 1.0, shape=5)
        approx = pm.fit(n=2_000, method="advi", progressbar=False, random_seed=0)

    real = model._advi_diagnostics(approx, method="advi", target="m", random_seed=0)
    # The reported number, when there is one, is a real float — never a NaN or
    # an infinity dressed up as a measurement.
    assert real["khat"] is None or np.isfinite(real["khat"])

    def _boom(*a, **k):
        raise RuntimeError("pytensor said no")

    monkeypatch.setattr(model, "_psis_khat", _boom)
    d = model._advi_diagnostics(approx, method="advi", target="m", random_seed=0)

    assert d["khat"] is None
    assert d["khat_status"] == "unavailable"
    # Unchecked is not failed: a missing k-hat does not override the ELBO
    # verdict. But it is not silence either.
    (msg,) = d["khat_warnings"]
    assert "could not be checked" in msg and "pytensor said no" in msg


def test_fit_metric_never_substitutes_the_sampler_asked_for():
    """The policy k-hat's measurement forced: a rejected approximation is
    *reported*, never silently replaced.

    `inference_method` is a promise about which sampler runs. An earlier draft
    of S2 re-fitted a k-hat-rejected node with NUTS behind the caller's back;
    the measurement then showed mean-field failing on essentially every real
    node, which made "escalation" the common path rather than a rescue. The
    honest response was to make NUTS the default everywhere and leave the fast
    path fast — so this asserts the substitution does not happen in either
    direction.
    """
    parser = Parser(RIDGE_YAML)
    frame = _ridge_frame()

    approx = fit_metric(parser.dag, frame, "y", draws=200, inference_method="advi", random_seed=0)
    assert approx.inference_method == "advi"
    assert approx.diagnostics["khat_status"] == "unusable"
    assert np.isfinite(approx.diagnostics["khat"]) and approx.diagnostics["khat"] > 0.7
    assert approx.diagnostics["fit_quality"] == "suspect"
    # Rejected, and it says what to do about it rather than doing it.
    (msg,) = approx.diagnostics["khat_warnings"]
    assert "inference_method=advi" in msg or "inference_method=nuts" in msg

    exact = fit_metric(
        parser.dag, frame, "y", draws=200, tune=300, inference_method="nuts", random_seed=0
    )
    assert exact.inference_method == "nuts"
    assert exact.diagnostics["method"] == "nuts"
    # NUTS is not an approximation, so it carries no k-hat at all. The absence
    # is the honest render: an `unavailable` here would say "we tried to check
    # and could not", which is a different and worse fact.
    assert "khat" not in exact.diagnostics
    assert "khat_status" not in exact.diagnostics
    assert "max_rhat" in exact.diagnostics

    # And they are genuinely different posteriors, not two labels on one: the
    # ridge is exactly where mean-field and MCMC disagree. This is the
    # measurement that made NUTS the default.
    def widths(arr):
        return np.percentile(arr, 97.5, axis=0) - np.percentile(arr, 2.5, axis=0)

    a = widths(approx.trace.posterior["beta_raw"].values.reshape(-1, 2))
    b = widths(exact.trace.posterior["beta_raw"].values.reshape(-1, 2))
    assert not np.allclose(a, b, rtol=0.05)


def test_run_rca_defaults_to_nuts_and_the_opt_in_is_reachable():
    """The orchestrators fit exactly, unless asked not to.

    Both call sites moved together — `run_rca` here, `run_scenario` in
    test_simulate.py — because a default chosen in one file and not its
    neighbour is the meta-defect the four rules exist for.
    """
    from breakdown.engine.rca import run_rca

    parser = Parser(RIDGE_YAML)
    frame = _ridge_frame(n=90)
    windows = win(("2024-01-08", "2024-02-04"), ("2024-02-05", "2024-03-03"))

    exact = run_rca(parser.dag, frame, {}, "y", **windows, draws=200)["nodes"]["y"]
    assert exact["inference_method"] == "nuts"
    assert exact["khat_status"] is None and exact["khat"] is None

    fast = run_rca(parser.dag, frame, {}, "y", **windows, draws=200, inference_method="advi")[
        "nodes"
    ]["y"]
    assert fast["inference_method"] == "advi"
    # The opt-in is honest rather than silent: the approximation is published
    # with the verdict that says not to trust its intervals.
    assert fast["khat_status"] == "unusable"
    assert np.isfinite(fast["khat"])
    assert fast["khat_warnings"]


def test_a_cached_approximation_does_not_answer_a_request_for_nuts():
    """`traces` is shared by every viewer of a process.

    One colleague's deliberate `?inference_method=advi` triage run must not
    decide the sampler behind everybody else's default analysis of the same
    window — the payload would then report a method nobody asked for. Reuse is
    allowed only upward, and `cached_fit_is_usable` is imported by both
    orchestrators so the rule cannot differ between them.
    """
    from breakdown.engine.model import cached_fit_is_usable
    from breakdown.engine.rca import run_rca

    parser = Parser(RIDGE_YAML)
    frame = _ridge_frame(n=90)
    windows = win(("2024-01-08", "2024-02-04"), ("2024-02-05", "2024-03-03"))
    traces: dict = {}

    fast = run_rca(parser.dag, frame, traces, "y", **windows, draws=200, inference_method="advi")[
        "nodes"
    ]["y"]
    assert fast["inference_method"] == "advi"
    assert traces[("y", windows["analysis_start"])].inference_method == "advi"

    # The default request re-fits rather than inheriting the approximation...
    exact = run_rca(parser.dag, frame, traces, "y", **windows, draws=200)["nodes"]["y"]
    assert exact["inference_method"] == "nuts"
    assert traces[("y", windows["analysis_start"])].inference_method == "nuts"

    # ...and the reverse is free: an exact fit answers a request for the
    # approximation, because it is the thing being approximated.
    reused = run_rca(parser.dag, frame, traces, "y", **windows, draws=200, inference_method="advi")[
        "nodes"
    ]["y"]
    assert reused["inference_method"] == "nuts"

    nuts_fit = traces[("y", windows["analysis_start"])]
    assert cached_fit_is_usable(nuts_fit, "nuts") and cached_fit_is_usable(nuts_fit, "advi")


# --- Parent collinearity (roadmap S4) ----------------------------------------
#
# The ridge fixture above is the failure shape: `x2` restates `x1`, so the
# likelihood is nearly flat along `beta_x1 + beta_x2` and the posterior is
# honestly wide on each — which S2 already delivers. What S4 adds is the
# *name*: a wide interval does not say the width comes from a split between
# those two parents specifically, and RCA publishes exactly that split.
#
# These tests deliberately include a passing case and a not-applicable case. A
# diagnostic that has never said "ok" is not evidence when it says "high".

WIDE_YAML = """
metrics:
  - name: x1
    source: dbt.metric.x1
  - name: x2
    source: dbt.metric.x2
  - name: x3
    source: dbt.metric.x3
  - name: x4
    source: dbt.metric.x4
  - name: y
    source: dbt.metric.y
    parents: [x1, x2, x3, x4]
"""


def _separable_frame(n: int = 90, seed: int = 11) -> pd.DataFrame:
    """Four parents that move independently — the case that must NOT warn."""
    rng = np.random.default_rng(seed)
    x1 = 100 + np.cumsum(rng.normal(0, 3, n))
    x2 = 50 + rng.normal(0, 5, n)
    x3 = 20 + rng.normal(0, 2, n)
    x4 = 70 + rng.normal(0, 4, n)
    y = 1.0 * x1 + 0.5 * x2 - 0.3 * x3 + 0.2 * x4 + rng.normal(0, 1.0, n)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x1": x1,
            "x2": x2,
            "x3": x3,
            "x4": x4,
            "y": y,
        }
    )


def _multiway_frame(n: int = 90, seed: int = 13) -> pd.DataFrame:
    """`x4 = x1 + x2 + x3`, and no *pair* of the four is even moderately
    correlated.

    This is the shape a pairwise correlation cannot see: each of the three
    explains only a third of x4's variance, so every pairwise r sits near
    1/sqrt(3) ~ 0.58, comfortably inside the `ok` band, while the four
    together are degenerate and no individual coefficient is identified. It is
    why the diagnostic reports VIF as well as pairwise r rather than only the
    cheaper one.
    """
    rng = np.random.default_rng(seed)
    x1 = 100 + rng.normal(0, 10, n)
    x2 = 100 + rng.normal(0, 10, n)
    x3 = 100 + rng.normal(0, 10, n)
    x4 = x1 + x2 + x3 + rng.normal(0, 1.0, n)
    y = 1.0 * x1 + 0.5 * x2 + 0.2 * x3 + 0.1 * x4 + rng.normal(0, 1.0, n)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x1": x1,
            "x2": x2,
            "x3": x3,
            "x4": x4,
            "y": y,
        }
    )


def test_collinearity_names_the_two_parents_the_fit_cannot_separate():
    """S4's whole content: which parents, and how strongly.

    The interval on each parent was already honest before this shipped (S2's
    NUTS default). What was missing is that a reader holding two wide intervals
    has no way to know they are one ridge measured twice — so the warning has
    to name both parents, and the payload has to carry the number behind it.
    """
    parser = Parser(RIDGE_YAML)
    fit = fit_metric(parser.dag, _ridge_frame(n=90), "y", draws=200, tune=300, random_seed=0)

    assert fit.diagnostics["collinearity_status"] == "high"
    block = fit.diagnostics["collinearity"]
    assert block["reason"] is None
    (pair,) = block["pairs"]
    # Parent order is `list(dag.predecessors(...))`, the axis order of beta_raw.
    assert pair["parents"] == ["x1", "x2"]
    assert pair["status"] == "high"
    assert 0.99 < pair["correlation"] < 1.0
    assert block["max_abs_correlation"] == pytest.approx(pair["correlation"])
    # Two parents: VIF is identically 1/(1 - r^2), so restating it would add a
    # second, more obscure form of the same number.
    assert block["vif"] == []

    (msg,) = fit.diagnostics["collinearity_warnings"]
    assert "'x1'" in msg and "'x2'" in msg and "correlation +0.99" in msg

    # And it does not move `fit_quality`. A collinear fit is not a broken fit:
    # it is a correct one that is properly unsure about the split, and telling
    # the reader to distrust the node would be the opposite of the finding.
    assert fit.diagnostics["method"] == "nuts"
    assert fit.diagnostics["fit_quality"] in ("ok", "suspect")


def test_collinearity_says_ok_when_the_parents_are_separable():
    """The passing case, without which the failing one proves nothing."""
    parser = Parser(WIDE_YAML)
    fit = fit_metric(parser.dag, _separable_frame(), "y", draws=200, tune=300, random_seed=0)

    assert fit.diagnostics["collinearity_status"] == "ok"
    assert "collinearity_warnings" not in fit.diagnostics
    block = fit.diagnostics["collinearity"]
    # `ok` is a measurement, not an assertion: the number that produced it
    # rides along so a reader can see how much headroom there was.
    assert block["max_abs_correlation"] < 0.7
    assert block["pairs"] == [] and block["vif"] == []


def test_collinearity_catches_a_degeneracy_no_pair_of_parents_shows():
    """`x4 = x1 + x2 + x3`: every pair looks fine, the design does not.

    VIF earns its place here. A diagnostic that only ever looked at pairs would
    return `ok` on a design where one parent's coefficient is not separately
    identified at all.
    """
    parser = Parser(WIDE_YAML)
    fit = fit_metric(parser.dag, _multiway_frame(), "y", draws=200, tune=300, random_seed=0)

    block = fit.diagnostics["collinearity"]
    assert fit.diagnostics["collinearity_status"] == "high"
    assert block["max_abs_correlation"] < 0.7, "the point of this fixture is that no pair crosses"
    assert block["pairs"] == []
    flagged = {e["parent"] for e in block["vif"]}
    assert flagged == {"x1", "x2", "x3", "x4"}
    for entry in block["vif"]:
        assert entry["vif"] is None or entry["vif"] >= 10.0
    warnings = fit.diagnostics["collinearity_warnings"]
    assert any("VIF" in w or "linear combination" in w for w in warnings)


def test_a_single_parent_has_no_split_to_be_unstable():
    """Null is not `ok`, and `ok` is not null.

    One parent has nothing to check — and the payload has to be able to say
    *that* rather than reporting a clean check that never ran. The MCP
    compaction and the UI both branch on the difference.
    """
    parser = Parser(SIMPLE_YAML)
    fit = fit_metric(
        parser.dag,
        generate_mock_data(n_days=120),
        "order_count",
        draws=200,
        tune=300,
        random_seed=0,
    )
    assert "collinearity_status" not in fit.diagnostics
    assert "collinearity" not in fit.diagnostics


@pytest.mark.parametrize(
    "col, expected",
    [
        (np.full(40, np.nan), "non-finite"),
        (np.zeros(40), "zero or non-finite variance"),
    ],
)
def test_an_uncheckable_design_is_withheld_and_not_zeroed(col, expected):
    """Rule 3, on this channel.

    A constant or non-finite regressor gives a 0/0 correlation. Emitting the
    NaN would 500 the encoder and `round_floats` would turn it into `null`; a
    zero would read as "these parents are unrelated", which is the most
    dangerous of the three. It is withheld under `unavailable` with the reason,
    and `unavailable` is not `ok` anywhere downstream.
    """
    from breakdown.engine.model import _collinearity_diagnostic

    rng = np.random.default_rng(3)
    X = np.column_stack([rng.normal(size=40), col])
    block, warnings = _collinearity_diagnostic(X, ["a", "b"], "y", "day")

    assert block["status"] == "unavailable"
    assert block["max_abs_correlation"] is None
    assert block["pairs"] == [] and block["vif"] == []
    assert expected in block["reason"] and "'b'" in block["reason"]
    (msg,) = warnings
    assert "unchecked design, not a clean one" in msg


def test_the_collinearity_verdict_reaches_the_rca_payload_and_mcp():
    """End to end on the channel S4 was told to compose with.

    `sign_warnings` is produced in `model.py`, carried on the RCA node record,
    compacted for MCP and rendered in the UI; this follows the same path rather
    than opening a parallel one, so the two cannot drift apart.
    """
    from breakdown.engine.rca import run_rca
    from breakdown.mcp.shaping import compact_rca

    parser = Parser(RIDGE_YAML)
    result = run_rca(
        parser.dag,
        _ridge_frame(n=90),
        {},
        "y",
        **win(("2024-01-08", "2024-02-04"), ("2024-02-05", "2024-03-03")),
        draws=200,
    )
    node = result["nodes"]["y"]
    assert node["collinearity_status"] == "high"
    assert node["collinearity"]["pairs"][0]["parents"] == ["x1", "x2"]
    assert "'x1'" in node["collinearity_warnings"][0]

    compact = compact_rca(result)["nodes"]["y"]
    assert compact["collinearity_status"] == "high"
    assert compact["collinearity_warnings"]
    # The evidence rides only where it is bad news, exactly as `khat` does.
    assert compact["collinearity"]["pairs"][0]["correlation"] > 0.99


# --- Posterior predictive checks (roadmap S3) ---------------------------------
#
# r-hat, ESS and divergences say the sampler explored the posterior; k-hat (S2)
# says a variational approximation is close to it. Neither asks whether the
# *model* is right for the data, and until S3 a badly misspecified node passed
# both in silence as long as it converged.
#
# The fixtures below are a matched pair on purpose. `_gaussian_frame` is a
# series the model's own likelihood generated, so it must come back `ok`; a
# check that has never passed is not evidence when it fails. `_count_frame` is
# the misspecification the Gaussian observation model cannot represent at all —
# low-mean counts, floored at zero — and it is also the shape S20's zero-share
# heuristic only approximates.


def _gaussian_frame(n: int = 90, seed: int = 3) -> pd.DataFrame:
    """A series drawn from the model's own likelihood: level + beta*x + normal."""
    rng = np.random.default_rng(seed)
    x = 100 + np.cumsum(rng.normal(0, 3, n))
    y = 0.8 * x + np.cumsum(rng.normal(0, 0.5, n)) + rng.normal(0, 4.0, n)
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "x": x, "y": y})


def _count_frame(n: int = 90, seed: int = 5) -> pd.DataFrame:
    """Low-mean Poisson counts: non-negative, floored, discrete.

    A Gaussian likelihood fitted here puts posterior mass below zero on a
    quantity that cannot go there, so the replicated series reaches minima the
    observed one never does — which is exactly what the `min` statistic sees.
    """
    rng = np.random.default_rng(seed)
    x = 10 + np.cumsum(rng.normal(0, 0.3, n))
    y = rng.poisson(np.clip(0.25 * x - 1.5, 0.2, None)).astype(float)
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "x": x, "y": y})


COUNT_YAML = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""


def test_ppc_passes_on_a_series_the_model_itself_generated():
    """The passing case. Without it, a failure below proves nothing."""
    parser = Parser(COUNT_YAML)
    fit = fit_metric(parser.dag, _gaussian_frame(), "y", draws=300, tune=300, random_seed=0)

    assert fit.diagnostics["ppc_status"] == "ok"
    assert "ppc_warnings" not in fit.diagnostics
    block = fit.diagnostics["ppc"]
    assert block["reason"] is None
    # `ok` is a measurement, not an assertion: every statistic rides along with
    # its p-value so a reader can see how much headroom there was, and so S10
    # has the material to plot.
    assert {s["statistic"] for s in block["statistics"]} == {
        "min",
        "max",
        "resid_max",
        "resid_acf1",
    }
    assert all(s["status"] == "ok" for s in block["statistics"])
    assert all(0.10 <= s["p_value"] <= 1.0 for s in block["statistics"])


def test_ppc_flags_counts_the_gaussian_likelihood_cannot_generate():
    """The misspecification convergence diagnostics cannot see.

    This node converges — that is the entire point. r-hat, ESS and divergences
    are all fine, because NUTS explored the posterior of a model that is simply
    the wrong model for the data.
    """
    parser = Parser(COUNT_YAML)
    fit = fit_metric(parser.dag, _count_frame(), "y", draws=300, tune=300, random_seed=0)

    assert fit.diagnostics["ppc_status"] == "severe"
    block = fit.diagnostics["ppc"]
    worst = block["statistics"][0]
    # Sorted by p-value, so the worst offender leads — and it is the floor.
    assert worst["statistic"] == "min"
    assert worst["p_value"] < 0.02

    (msg,) = [m for m in fit.diagnostics["ppc_warnings"] if "'min'" in m]
    assert "'y'" in msg and "cannot go below a floor" in msg
    assert "docs/model.md" in msg

    # `severe` moves the two-valued gate every consumer already branches on:
    # a model that cannot generate its own data is not one whose intervals
    # mean what they say. This is the one place S3 differs from S4.
    assert fit.diagnostics["fit_quality"] == "suspect"


def test_ppc_moderate_is_reachable_and_leaves_the_fit_quality_gate_alone():
    """The band split is the design decision S3 had to make, so it gets a test.

    Four statistics per node cross a `moderate` band often enough on honest
    fits that wiring that band to `fit_quality` would make `suspect` the common
    verdict and drain it of meaning. `severe` — a model that cannot generate
    its own data — is a different claim and does move it.
    """
    from breakdown.engine.model import _ppc_diagnostic, _ppc_moves_fit_quality

    # The policy itself, stated once and checked directly.
    assert _ppc_moves_fit_quality("severe") is True
    assert _ppc_moves_fit_quality("moderate") is False
    assert _ppc_moves_fit_quality("unavailable") is False
    assert _ppc_moves_fit_quality("ok") is False
    assert _ppc_moves_fit_quality(None) is False

    # And the `moderate` band is actually reachable — a band nothing can land
    # in is not a band. The observed maximum sits just outside the bulk of the
    # replicated maxima: extreme enough to notice, not enough to condemn.
    n, draws = 40, 400
    rng = np.random.default_rng(1)
    mu = np.zeros((draws, n))
    y_rep = rng.normal(0, 1.0, (draws, n))
    rep_max = np.max(y_rep, axis=1)
    # Take a genuine replicate — so every *other* statistic is typical by
    # construction — and cap only its peak at the 3rd percentile of the
    # replicated maxima: one-sided p ~ 0.97, two-sided ~ 0.06, which is inside
    # `moderate` and outside `severe`. Rescaling the whole series instead would
    # move `min` and `resid_max` too, and the block status is the worst of the
    # four, so the test would be measuring the wrong statistic.
    y = np.clip(rng.normal(0, 1.0, n), None, float(np.quantile(rep_max, 0.03)))

    block, warnings = _ppc_diagnostic(y, y_rep, mu, "y", "day")
    flagged = {s["statistic"]: s for s in block["statistics"] if s["status"] != "ok"}
    assert "max" in flagged
    assert flagged["max"]["status"] == "moderate"
    assert 0.02 <= flagged["max"]["p_value"] < 0.10
    assert block["status"] == "moderate"
    assert not _ppc_moves_fit_quality(block["status"])
    # The prose says it is a caveat, not a verdict — the distinction the band
    # exists to carry.
    (msg,) = [m for m in warnings if "'max'" in m]
    assert "usable and this is a caveat on it" in msg


def test_ppc_omits_the_dispersion_statistic_that_cannot_fail():
    """`sd` is deliberately absent, and this pins the reason.

    `_prepare_series` z-scores `y` and `sigma_obs` is free, so a replicated
    standard deviation matches the observed one *by construction*. Measured
    across a well-specified world, a t(3) world and a Poisson world it returned
    p = 0.479 / 0.499 / 0.521 — a statistic that cannot fail is worse than no
    statistic, because a reader scores it as a check that passed.
    """
    from breakdown.engine.model import _ppc_test_statistics

    rng = np.random.default_rng(0)
    stats = _ppc_test_statistics(rng.normal(0, 1, 50), rng.normal(0, 1, 50))
    assert "sd" not in stats and "resid_sd" not in stats
    assert set(stats) == {"min", "max", "resid_max", "resid_acf1"}


def test_ppc_mid_p_does_not_false_alarm_on_a_tied_statistic():
    """A statistic with a point mass — a count series whose minimum is 0 in most
    replicates — returns p = 1.000 under a plain `>=` comparison, putting the
    only false alarm on a *correctly* specified node. Mid-p splits the tied mass."""
    from breakdown.engine.model import _ppc_diagnostic

    n, draws = 40, 300
    y = np.zeros(n)
    mu = np.zeros((draws, n))
    y_rep = np.zeros((draws, n))  # every replicate ties with the observation
    block, warnings = _ppc_diagnostic(y, y_rep, mu, "y", "day")
    # All ties: mid-p gives one-sided 0.5, two-sided 1.0 — the least extreme
    # verdict there is, which is the honest reading of "identical".
    by_name = {s["statistic"]: s for s in block["statistics"]}
    assert by_name["min"]["p_value"] == pytest.approx(1.0)
    assert block["status"] == "ok"
    assert warnings == []


def test_ppc_withholds_rather_than_invents_when_the_draws_are_unusable():
    """Rule 3 at the S3 boundary: a NaN p-value rounds to `null` on the agent
    payload, which reads as a check that never ran rather than one that failed.
    Withhold the block under a named status carrying its reason."""
    from breakdown.engine.model import _ppc_diagnostic

    y = np.arange(20, dtype=float)
    mu = np.zeros((10, 20))
    y_rep = np.full((10, 20), np.nan)
    block, warnings = _ppc_diagnostic(y, y_rep, mu, "y", "day")

    assert block["status"] == "unavailable"
    assert block["statistics"] == []
    assert "non-finite" in block["reason"]
    (msg,) = warnings
    assert "unchecked model, not a validated one" in msg

    # A shape mismatch is withheld too, and says which shapes.
    block, _ = _ppc_diagnostic(y, np.zeros((10, 5)), np.zeros((10, 5)), "y", "day")
    assert block["status"] == "unavailable"
    assert "do not match" in block["reason"]


def test_ppc_is_absent_rather_than_ok_on_a_node_that_was_never_fitted():
    """Nothing to check is not the same as checked and clean."""
    from breakdown.engine.model import _ppc_diagnostic

    block, warnings = _ppc_diagnostic(np.zeros(10), None, None, "y", "day")
    assert block is None and warnings == []


# --- Roadmap S10: the band S3's p-values are a summary of --------------------


def test_ppc_band_carries_the_fitted_window_in_the_metrics_own_units():
    """The plot's whole payload, on the node S3's own test flags.

    Every array is the length of the *fitted* window, not the loaded one, and
    the observed series is in raw units — the fit runs z-scored, so a band
    left in that space would share no axis with the metric's own history and
    a reader could not tell a floor violation from a rounding difference.
    """
    parser = Parser(COUNT_YAML)
    frame = _count_frame()
    fit = fit_metric(parser.dag, frame, "y", draws=300, tune=300, random_seed=0)

    band = fit.ppc_band
    assert band["reason"] is None
    n = band["n_periods"]
    assert n == len(fit.dates)
    assert band["dates"][0] == fit.dates[0].strftime("%Y-%m-%d")
    assert band["dates"][-1] == fit.dates[-1].strftime("%Y-%m-%d")

    # Raw units: the observed series is the metric's own history back again.
    np.testing.assert_allclose(band["observed"], frame["y"].to_numpy(dtype=float), rtol=1e-9)

    rep = band["replicated"]
    assert set(rep) == {"lo95", "lo50", "median", "hi50", "hi95"}
    assert all(len(v) == n for v in rep.values())
    # Quantiles are ordered because they are quantiles; a band drawn from
    # crossed edges fills the wrong way and reads as a negative-width interval.
    for lo, hi in (("lo95", "lo50"), ("lo50", "median"), ("median", "hi50"), ("hi50", "hi95")):
        assert all(a <= b + 1e-9 for a, b in zip(rep[lo], rep[hi]))

    # `outside` indexes the observed series, and says only what it says: the
    # periods the 95% band misses. No status, no band, no second verdict.
    assert all(0 <= i < n for i in band["outside"])
    for i in band["outside"]:
        assert band["observed"][i] < rep["lo95"][i] or band["observed"][i] > rep["hi95"][i]
    assert band["fitted_quantity"] == "metric"


def test_ppc_band_never_reaches_an_encoder_with_a_non_finite_value():
    """Rule 3. One NaN quantile is a 500 through `allow_nan=False`, and a hole
    in a band reads as certainty rather than as an absence."""
    from breakdown.engine.model import _ppc_band

    dates = pd.date_range("2024-01-01", periods=5)
    y = np.arange(5, dtype=float)

    # The block still exists — it is what carries the reason — but it carries
    # no arrays for a caller to draw.
    band = _ppc_band(y, np.full((8, 5), np.nan), dates, 0.0, 1.0, False, "y")
    assert band.get("observed") is None
    assert "non-finite" in band["reason"]

    # A shape that cannot be zipped is withheld the same way, not truncated.
    band = _ppc_band(y, np.zeros((8, 3)), dates, 0.0, 1.0, False, "y")
    assert band.get("observed") is None and "do not match" in band["reason"]

    # And so is a date index that does not line up with the fitted periods.
    band = _ppc_band(y, np.zeros((8, 5)), dates[:3], 0.0, 1.0, False, "y")
    assert band.get("observed") is None and "fitted dates" in band["reason"]

    # There is no band at all when there were no draws to build one from.
    assert _ppc_band(y, None, dates, 0.0, 1.0, False, "y") is None


def test_ppc_band_says_when_the_series_it_plots_is_a_residual():
    """A formula node fits `observed - formula(parents)`, so its band is a band
    of the residual. Plotting that against the metric's own history would be
    two quantities on one axis, and the payload has to make the label
    impossible to omit."""
    parser = Parser(YAML_WITH_FORMULA)
    data = generate_mock_data(n_days=60)
    fit = fit_metric(parser.dag, data, "revenue", draws=200, tune=200, random_seed=0)

    band = fit.ppc_band
    assert band["fitted_quantity"] == "formula_residual"
    # And the numbers are the residual's, not revenue's: revenue runs in the
    # thousands, its identity residual around zero.
    assert abs(float(np.mean(band["observed"]))) < float(data["revenue"].mean()) / 10

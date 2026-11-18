"""Roadmap S20: observation models other than the constant-variance Gaussian.

Synthetic worlds where the noise law is known by construction. The claim under
test is narrow: a node that declares a `likelihood` keeps the same additive
mean (so every coefficient and component reads as before) and gets noise that
grows with its level, and a node that declares none is fitted exactly as it
was.

Fast tests (parser rules, the replicate draws, the support refusals, the
dispatch) run in the `-m "not slow"` loop. The fits are marked slow.
"""

import json

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.model import (
    LIKELIHOOD_NOISE_PARAM,
    _check_likelihood_support,
    _level_is_marginalized,
    _mean_scaled_sigma,
    _positive_mean,
    fit_metric,
    observation_draws,
)
from breakdown.engine.rca import run_rca
from breakdown.engine.simulate import Intervention, ScenarioRequest, run_scenario
from breakdown.parser import LIKELIHOODS, Parser


def tree(likelihood=None, parents=False, extra=""):
    x = "  - name: x\n    source: dbt.metric.x\n" if parents else ""
    par = "    parents: [x]\n" if parents else ""
    like = f"    likelihood: {likelihood}\n" if likelihood else ""
    return f"metrics:\n{x}  - name: y\n    source: dbt.metric.y\n{par}{like}{extra}"


def frame(**cols):
    n = len(next(iter(cols.values())))
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), **cols})


def nb_counts(rng, mu, overdispersion):
    """Counts with `Var = mu + overdispersion^2 * mu^2`."""
    alpha = 1.0 / overdispersion**2
    return rng.poisson(rng.gamma(alpha, mu / alpha)).astype(float)


def coverage(fit, lo, hi):
    """Share of fitted periods `lo:hi` inside the fit's own 95% predictive band."""
    band = fit.ppc_band
    obs = np.asarray(band["observed"])[lo:hi]
    low = np.asarray(band["replicated"]["lo95"])[lo:hi]
    high = np.asarray(band["replicated"]["hi95"])[lo:hi]
    return float(((obs >= low) & (obs <= high)).mean())


def ppc_p(fit, statistic):
    return next(
        s["p_value"] for s in fit.diagnostics["ppc"]["statistics"] if s["statistic"] == statistic
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_likelihood_defaults_to_gaussian_and_accepts_the_named_few():
    assert Parser(tree()).dag.nodes["y"]["definition"].likelihood == "gaussian"
    for name in LIKELIHOODS:
        assert Parser(tree(name)).dag.nodes["y"]["definition"].likelihood == name
    assert set(LIKELIHOOD_NOISE_PARAM) == set(LIKELIHOODS)


def test_an_unknown_likelihood_is_refused_naming_the_choices():
    with pytest.raises(ValueError, match="likelihood must be one of") as e:
        Parser(tree("poisson"))
    assert "negative_binomial" in str(e.value)


def test_a_likelihood_on_a_formula_node_is_refused():
    yaml = """
metrics:
  - name: a
    source: dbt.metric.a
  - name: b
    source: dbt.metric.b
  - name: y
    source: dbt.metric.y
    formula: "a * b"
    parents: [a, b]
    likelihood: negative_binomial
"""
    with pytest.raises(ValueError, match="formula node"):
        Parser(yaml)


def test_a_count_likelihood_on_a_rate_is_refused():
    with pytest.raises(ValueError, match="kind: rate"):
        Parser(tree("negative_binomial", extra="    kind: rate\n"))


# ---------------------------------------------------------------------------
# The pieces, without a sampler
# ---------------------------------------------------------------------------


def test_only_a_gaussian_nuts_fit_marginalizes_the_level():
    assert _level_is_marginalized("nuts", "gaussian")
    assert not _level_is_marginalized("nuts", "negative_binomial")
    assert not _level_is_marginalized("nuts", "mean_scaled_gaussian")
    assert not _level_is_marginalized("advi", "gaussian")


def test_the_positive_mean_is_the_same_in_the_graph_and_in_numpy():
    import pytensor.tensor as pt

    mu = np.array([-50.0, -1.0, 0.0, 0.3, 2.0, 40.0])
    in_numpy = _positive_mean(mu, 10.0)
    in_graph = _positive_mean(pt.as_tensor(mu), 10.0, pt).eval()
    np.testing.assert_allclose(in_graph, in_numpy, rtol=1e-12)
    assert (in_numpy > 0).all()
    # Away from zero the floor leaves the mean alone.
    assert in_numpy[-1] == pytest.approx(40.0, rel=1e-6)
    assert in_numpy[-2] == pytest.approx(2.0, rel=1e-3)


def test_negative_binomial_draws_are_counts_with_the_declared_variance():
    rng = np.random.default_rng(0)
    y_mean, y_std, od = 30.0, 12.0, 0.4
    mu_raw = np.array([5.0, 30.0, 90.0])
    mu = np.tile((mu_raw - y_mean) / y_std, (200_000, 1))
    draws = observation_draws(
        "negative_binomial", mu, np.full((len(mu), 1), od), y_mean, y_std, rng
    )
    raw = draws * y_std + y_mean
    assert raw.min() >= -1e-9
    np.testing.assert_allclose(raw, np.rint(raw), atol=1e-9)
    np.testing.assert_allclose(raw.mean(axis=0), mu_raw, rtol=0.02)
    np.testing.assert_allclose(raw.var(axis=0), mu_raw + od**2 * mu_raw**2, rtol=0.03)


def test_mean_scaled_gaussian_noise_grows_with_the_square_root_of_the_mean():
    rng = np.random.default_rng(0)
    y_mean, y_std, sigma = 100.0, 40.0, 0.3
    mu_raw = np.array([25.0, 100.0, 400.0])
    mu = np.tile((mu_raw - y_mean) / y_std, (200_000, 1))
    draws = observation_draws(
        "mean_scaled_gaussian", mu, np.full((len(mu), 1), sigma), y_mean, y_std, rng
    )
    sd = draws.std(axis=0)
    # `sigma_obs` is the sd at the series' mean level; four times the mean is
    # twice the sd (the floor moves these by about 1%).
    assert sd[1] == pytest.approx(sigma, rel=0.02)
    assert sd[2] / sd[1] == pytest.approx(2.0, rel=0.03)
    assert sd[0] / sd[1] == pytest.approx(0.5, rel=0.05)
    # Bounded away from zero where the level has been driven to zero.
    assert _mean_scaled_sigma(sigma, np.array([-1e6]), y_mean)[0] > 0.1 * sigma


def test_gaussian_draws_are_the_constant_variance_model():
    mu = np.zeros((4, 3))
    noise = np.full((4, 1), 0.5)
    got = observation_draws("gaussian", mu, noise, 7.0, 2.0, np.random.default_rng(5))
    want = 0.5 * np.random.default_rng(5).standard_normal((4, 3))
    np.testing.assert_array_equal(got, want)


def test_a_series_outside_the_likelihoods_support_is_refused_by_name():
    _check_likelihood_support("gaussian", np.array([-3.0, 0.5]), "y")
    _check_likelihood_support("negative_binomial", np.array([0.0, 3.0, 12.0]), "y")
    _check_likelihood_support("mean_scaled_gaussian", np.array([0.0, 3.5]), "y")
    with pytest.raises(ValueError, match="not whole numbers") as e:
        _check_likelihood_support("negative_binomial", np.array([0.0, 3.5, 12.0]), "y")
    assert "mean_scaled_gaussian" in str(e.value)  # the remedy
    for name in ("negative_binomial", "mean_scaled_gaussian"):
        with pytest.raises(ValueError, match="non-negative"):
            _check_likelihood_support(name, np.array([4.0, -1.0]), "y")


def test_a_fractional_series_is_refused_before_any_sampling():
    rng = np.random.default_rng(1)
    data = frame(y=rng.gamma(4.0, 5.0, 60))
    with pytest.raises(ValueError, match="not whole numbers"):
        fit_metric(Parser(tree("negative_binomial")).dag, data, "y", random_seed=0)


# ---------------------------------------------------------------------------
# Fits
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_an_undeclared_node_is_fitted_exactly_as_before():
    """The default is the Gaussian, on the marginalized path, with nothing
    added to its diagnostics: declaring `likelihood: gaussian` and declaring
    nothing are the same seeded fit, value for value."""
    rng = np.random.default_rng(7)
    x = 100.0 + rng.normal(0, 4.0, 90)
    data = frame(x=x, y=0.5 * x + rng.normal(0, 1.0, 90))
    undeclared = fit_metric(Parser(tree(parents=True)).dag, data, "y", draws=200, random_seed=4)
    declared = fit_metric(
        Parser(tree("gaussian", parents=True)).dag, data, "y", draws=200, random_seed=4
    )
    post = undeclared.trace.posterior
    assert "trend_z" not in post  # still the Kalman path
    assert set(post.data_vars) == {
        "alpha",
        "sigma_obs",
        "sigma_trend",
        "beta_x",
        "beta",
        "beta_raw",
        "trend",
    }
    for name in post.data_vars:
        np.testing.assert_array_equal(post[name].values, declared.trace.posterior[name].values)
    assert undeclared.diagnostics == declared.diagnostics
    assert "likelihood" not in undeclared.diagnostics and "level" not in undeclared.diagnostics
    assert undeclared.ppc_band == declared.ppc_band
    assert undeclared.likelihood == declared.likelihood == "gaussian"


@pytest.fixture(scope="module")
def growing_count():
    """A count that grows 15-fold with overdispersion 0.35: noise sd about 2
    at the start of the window and about 22 at the end."""
    n = 180
    rng = np.random.default_rng(20)
    mu = 4.0 * np.exp(np.linspace(0, np.log(15), n))
    data = frame(y=nb_counts(rng, mu, 0.35))
    fits = {
        name: fit_metric(Parser(tree(name)).dag, data, "y", draws=300, random_seed=1)
        for name in (None, "negative_binomial")
    }
    return n, fits[None], fits["negative_binomial"]


@pytest.mark.slow
def test_a_planted_overdispersed_count_is_recovered(growing_count):
    _, _, nb = growing_count
    od = nb.trace.posterior["nb_overdispersion"].values.ravel()
    lo, hi = np.quantile(od, [0.03, 0.97])
    assert lo < 0.35 < hi, (lo, hi)
    assert hi - lo < 0.2  # informed by the data, not the prior restated
    assert nb.likelihood == "negative_binomial"
    assert nb.diagnostics["likelihood"] == "negative_binomial"
    # The dispatch, disclosed: no Kalman marginalization for a count.
    assert nb.diagnostics["level"] == "explicit_latent"
    assert "trend_z" in nb.trace.posterior and "sigma_obs" not in nb.trace.posterior
    assert nb.diagnostics["ppc_status"] == "ok"
    assert nb.diagnostics["fit_quality"] == "ok"


@pytest.mark.slow
def test_the_count_interval_covers_where_the_gaussians_does_not(growing_count):
    """One noise sd for the whole history is too wide where the count was
    small and too narrow where it is large, which is where an analysis window
    sits. The negative binomial's 95% band covers at about the nominal rate in
    both thirds; the Gaussian's covers everything early and under-covers late."""
    n, gaussian, nb = growing_count
    third = n // 3
    assert coverage(gaussian, 0, third) == 1.0
    assert coverage(gaussian, n - third, n) < 0.90
    assert coverage(nb, n - third, n) >= 0.92
    assert 0.92 <= coverage(nb, 0, third) <= 1.0
    # And the Gaussian's own check says so, on the same statistics.
    assert gaussian.diagnostics["ppc_status"] == "severe"
    assert ppc_p(gaussian, "resid_max") < 0.02 and ppc_p(nb, "resid_max") > 0.10


@pytest.fixture(scope="module")
def zero_run():
    """A seasonal count: exactly zero for the middle third of the window."""
    rng = np.random.default_rng(21)
    y = nb_counts(rng, np.full(150, 12.0), 0.3)
    y[50:100] = 0.0
    data = frame(y=y)
    return {
        name: fit_metric(Parser(tree(name)).dag, data, "y", draws=300, random_seed=1)
        for name in (None, "negative_binomial")
    }


@pytest.mark.slow
def test_a_series_with_a_zero_run_gets_no_mass_below_zero(zero_run):
    gaussian, nb = zero_run[None], zero_run["negative_binomial"]
    # The Gaussian's predictive band goes well below zero, and is disclosed.
    assert min(gaussian.ppc_band["replicated"]["lo95"]) < -3.0
    assert ppc_p(gaussian, "min") < 0.02
    assert "likelihood_warnings" in gaussian.diagnostics
    # The count's never does, in any period, and its `min` is reproduced.
    assert min(nb.ppc_band["replicated"]["lo95"]) >= -1e-9
    assert ppc_p(nb, "min") > 0.10
    assert "likelihood_warnings" not in nb.diagnostics
    # Inside the run the count's band has collapsed onto zero rather than
    # straddling it.
    assert max(nb.ppc_band["replicated"]["hi95"][60:95]) <= 2.0
    assert max(gaussian.ppc_band["replicated"]["hi95"][60:95]) > 3.0


@pytest.mark.slow
def test_rca_reads_a_count_node_with_a_parent_as_it_reads_a_gaussian_one():
    """The identity link is what makes this true: `beta_raw` on a count node
    is still d(target) per unit of parent, so `beta_raw x delta-parent`
    attribution needs no change and agrees with the Gaussian fit's."""
    n = 130
    rng = np.random.default_rng(23)
    x = 100.0 + rng.normal(0, 12.0, n)
    x[91:] += 40.0
    data = frame(x=x, y=nb_counts(rng, 0.4 * x, 0.15))
    out = {}
    for name in (None, "negative_binomial"):
        result = run_rca(
            Parser(tree(name, parents=True)).dag,
            data,
            {},
            "y",
            reference_start="2024-01-15",
            reference_end="2024-03-10",
            analysis_start="2024-04-01",
            analysis_end="2024-05-09",
            draws=300,
            reference_sensitivity=False,
        )
        json.dumps(result, allow_nan=False)  # rule 3
        node = result["nodes"]["y"]
        assert node["status"] == "ok" and node["attribution_method"] == "posterior"
        out[name] = node
    truth = 0.4 * (x[91:].mean() - x[14:70].mean())
    (count,), (gauss,) = out["negative_binomial"]["contributions"], out[None]["contributions"]
    assert count["estimate"] == pytest.approx(gauss["estimate"], rel=0.15)
    assert count["estimate"] == pytest.approx(truth, rel=0.35)
    assert out["negative_binomial"]["gap"] == out[None]["gap"]


@pytest.mark.slow
def test_a_what_if_propagates_through_a_count_node_additively():
    """The same property read by the other consumer of `beta_raw`: a lever on
    the parent moves the count by `beta_raw x delta`, in the count's units."""
    n = 130
    rng = np.random.default_rng(23)
    x = 100.0 + rng.normal(0, 12.0, n)
    data = frame(x=x, y=nb_counts(rng, 0.4 * x, 0.15))
    traces = {}
    result = run_scenario(
        Parser(tree("negative_binomial", parents=True)).dag,
        data,
        traces,
        ScenarioRequest(
            baseline_start="2024-01-01",
            baseline_end="2024-05-09",
            interventions=[Intervention(metric="x", mode="delta", value=50.0)],
        ),
        draws=300,
    )
    json.dumps(result, allow_nan=False)  # rule 3
    y = result["nodes"]["y"]
    assert y["status"] == "affected"
    (fit,) = traces.values()
    assert fit.likelihood == "negative_binomial"
    beta_raw = float(fit.trace.posterior["beta_raw"].values.mean())
    assert y["delta"]["estimate"] == pytest.approx(beta_raw * 50.0, rel=0.02)
    assert y["delta"]["estimate"] == pytest.approx(0.4 * 50.0, rel=0.35)

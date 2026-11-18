"""Roadmap S8: the local linear trend (level + slope), checked exactly and fitted.

The fast half runs no sampler. A local linear trend is linear in its Gaussian
inputs — the slope before the first period, the level innovations and the
slope innovations —

    level[t] = level[t-1] + slope[t-1] + eta[t],   slope[t] = slope[t-1] + zeta[t]

so `level = A w` for a fixed matrix `A` and `w ~ N(0, diag(...))`, and both the
marginal likelihood of `r = level + eps` and the conditional `level | r` have
dense closed forms. The two-state Kalman filter and the backward sampler are
compared against that algebra, as `tests/test_kalman_level.py` does for the
local level.

The slow half fits: the marginalized likelihood against the explicit latent on
one seeded world, a planted drift, and a flat series the slope must not harm.
"""

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.model import (
    _SLOPE0_SD,
    _kalman_local_level_loglik,
    _kalman_local_linear_loglik,
    _sample_local_linear,
    fit_metric,
    simulate_trend_forward,
    trend_at,
    trend_kind,
)
from breakdown.parser import Parser


def _recursion(slope0, eta, zeta):
    """The model, written the slow way: (level, slope) for one set of inputs."""
    T = len(eta)
    level, slope = np.empty(T), np.empty(T)
    prev_level, prev_slope = 0.0, slope0
    for t in range(T):
        level[t] = prev_level + prev_slope + eta[t]
        slope[t] = prev_slope + zeta[t]
        prev_level, prev_slope = level[t], slope[t]
    return level, slope


def _dense_covs(T, s2, q2, v0=_SLOPE0_SD**2):
    """Prior covariance of the stacked `[level, slope]` path (2T x 2T)."""
    basis = np.eye(1 + 2 * T)
    A = np.column_stack([np.concatenate(_recursion(w[0], w[1 : 1 + T], w[1 + T :])) for w in basis])
    var = np.concatenate([[v0], np.full(T, s2), np.full(T, q2)])
    return (A * var) @ A.T


def _dense_loglik(r, s2, q2, o2):
    T = len(r)
    cov = _dense_covs(T, s2, q2)[:T, :T] + o2 * np.eye(T)
    sign, logdet = np.linalg.slogdet(cov)
    assert sign > 0
    return float(-0.5 * (T * np.log(2 * np.pi) + logdet + r @ np.linalg.solve(cov, r)))


def _series(T, s2, q2, o2, seed=7, slope0=0.05):
    rng = np.random.default_rng(seed)
    level, _ = _recursion(slope0, rng.normal(0, np.sqrt(s2), T), rng.normal(0, np.sqrt(q2), T))
    return level + rng.normal(0, np.sqrt(o2), T)


@pytest.mark.parametrize(
    "s2, q2, o2",
    [
        (0.05**2, 0.001**2, 1.0),  # the default priors' scales
        (0.3, 0.02, 0.1),  # a slope that really moves
        (0.05**2, 0.0, 0.5),  # fixed drift: the slope innovation an exact zero
        (1e-8, 1e-10, 0.3),  # both states near-deterministic
    ],
)
def test_two_state_kalman_loglik_is_the_marginal_likelihood(s2, q2, o2):
    import pytensor.tensor as pt

    r = _series(40, s2, q2, o2)
    ll = _kalman_local_linear_loglik(
        pt.as_tensor(r),
        pt.as_tensor(np.sqrt(s2)),
        pt.as_tensor(np.sqrt(q2)),
        pt.as_tensor(np.sqrt(o2)),
    ).eval()
    assert float(ll) == pytest.approx(_dense_loglik(r, s2, q2, o2), rel=1e-9, abs=1e-9)


def test_no_slope_at_all_is_the_local_level():
    """With the initial slope and its innovation both pinned at zero the
    two-state filter is the scalar one: the slope is an addition to the local
    level, not a different model."""
    import pytensor.tensor as pt

    r = _series(40, 0.2, 0.0, 0.5, slope0=0.0)
    args = (pt.as_tensor(r), pt.as_tensor(np.sqrt(0.2)))
    two = _kalman_local_linear_loglik(
        *args, pt.as_tensor(0.0), pt.as_tensor(np.sqrt(0.5)), slope0_sd=0.0
    ).eval()
    one = _kalman_local_level_loglik(*args, pt.as_tensor(np.sqrt(0.5))).eval()
    assert float(two) == pytest.approx(float(one), rel=1e-12)


@pytest.mark.parametrize("q2", [0.02, 0.0])
def test_state_draws_match_the_conditional_posterior_of_level_and_slope(q2):
    """Mean and covariance of the drawn level path, and of the last slope (the
    one the fit keeps), match the dense conditional. `q2 = 0` is the
    fixed-drift limit."""
    T, s2, o2 = 8, 0.3, 0.5
    r_obs = _series(T, s2, q2, o2, seed=11, slope0=0.2)

    prior = _dense_covs(T, s2, q2)
    H = np.hstack([np.eye(T), np.zeros((T, T))])  # r observes the level
    S = H @ prior @ H.T + o2 * np.eye(T)
    gain = prior @ H.T @ np.linalg.inv(S)
    post_mean = gain @ r_obs
    post_cov = prior - gain @ H @ prior
    keep = list(range(T)) + [2 * T - 1]  # every level, and the last slope

    n = 40_000
    level, slope_end = _sample_local_linear(
        np.tile(r_obs, (n, 1)),
        np.full(n, s2),
        np.full(n, q2),
        np.full(n, o2),
        np.random.default_rng(3),
    )
    draws = np.column_stack([level, slope_end])
    cov = post_cov[np.ix_(keep, keep)]
    se_mean = np.sqrt(np.diag(cov) / n)
    assert np.all(np.abs(draws.mean(axis=0) - post_mean[keep]) < 5 * se_mean)
    assert np.allclose(np.cov(draws, rowvar=False), cov, atol=0.03 * cov.max())


def test_state_draws_stay_finite_when_the_states_are_near_deterministic():
    """The regression this sampler was rewritten for. A draw whose
    `sigma_trend` is ~1e-9 makes the level and slope indistinguishable to
    machine precision at the first periods; backward sampling, which inverts
    that covariance, returned NaN or a level thousands of sds from the data
    for such draws. Every draw here must be finite and stay on the series — a
    straight line under noise, so a near-deterministic trend is that line."""
    rng = np.random.default_rng(0)
    T, n = 100, 20_000
    # Through the model's own start (`level[-1] = 0`, and no intercept here).
    line = 0.03 * (np.arange(T) + 1)
    y = line + rng.normal(0, 0.14, T)
    s = 10 ** rng.uniform(-10, -6, n)
    q = 10 ** rng.uniform(-13, -9, n)
    q[: n // 4] = 0.0
    o = 10 ** rng.uniform(-1.5, 0, n)
    level, slope_end = _sample_local_linear(
        np.tile(y, (n, 1)), s**2, q**2, o**2, np.random.default_rng(1)
    )
    assert np.isfinite(level).all() and np.isfinite(slope_end).all()
    # A line fitted to 100 points with noise sd `o`: the level is known to
    # well within one `o` everywhere, and the slope to a small fraction of it.
    assert (np.abs(level - line).max(axis=1) < 1.5 * np.maximum(o, 0.14)).all()
    assert (np.abs(slope_end - 0.03) < 0.05 * np.maximum(o, 0.14)).all()


# --- the YAML, and the one function that picks the kind ---------------------


def _tree(trend_line=""):
    return f"""
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
{trend_line}
"""


def test_trend_kind_defaults_to_level_and_reads_the_declaration():
    assert trend_kind(Parser(_tree()).dag, "y") == "level"
    assert trend_kind(Parser(_tree("    trend: linear")).dag, "y") == "level"
    assert trend_kind(Parser(_tree("    trend: {sigma: 0.1}")).dag, "y") == "level"
    dag = Parser(_tree("    trend: {kind: local_linear}")).dag
    assert trend_kind(dag, "y") == "local_linear"
    assert trend_kind(dag, "x") == "level"
    assert dag.nodes["y"]["definition"].trend.slope_sigma == 0.001
    # An explicit `level` is a declaration, distinct from saying nothing, so a
    # later role-based default cannot override it.
    explicit = Parser(_tree("    trend: {kind: level}")).dag.nodes["y"]["definition"]
    assert explicit.trend.kind == "level"
    assert Parser(_tree("    trend: linear")).dag.nodes["y"]["definition"].trend.kind is None


def test_trend_kind_and_slope_sigma_are_validated():
    with pytest.raises(ValueError, match="Unsupported trend kind"):
        Parser(_tree("    trend: {kind: quadratic}"))
    with pytest.raises(ValueError, match="slope_sigma must be >= 0"):
        Parser(_tree("    trend: {kind: local_linear, slope_sigma: -1}"))
    # Refused rather than ignored: nothing would read it.
    with pytest.raises(ValueError, match="has no slope"):
        Parser(_tree("    trend: {slope_sigma: 0.01}"))
    fixed = Parser(_tree("    trend: {kind: local_linear, slope_sigma: 0}")).dag
    assert fixed.nodes["y"]["definition"].trend.slope_sigma == 0


def _posterior(trend, slope_end=None, sigma_trend=0.0, sigma_slope=None):
    import xarray as xr

    n = trend.shape[0]
    variables = {
        "trend": (("chain", "draw", "trend_dim_0"), trend[None]),
        "sigma_trend": (("chain", "draw"), np.full((1, n), sigma_trend)),
    }
    if slope_end is not None:
        variables["trend_slope_end"] = (("chain", "draw"), np.asarray(slope_end)[None])
    if sigma_slope is not None:
        variables["sigma_slope"] = (("chain", "draw"), np.full((1, n), sigma_slope))
    return xr.Dataset(variables)


def test_trend_at_is_flat_for_a_level_and_extrapolates_a_slope():
    trend = np.array([[0.0, 1.0, 2.0], [0.0, 0.5, 1.0]])
    t_idx = np.array([1, 2, 3, 5])
    flat = trend_at(_posterior(trend), t_idx)
    assert np.array_equal(flat, [[1.0, 2.0, 2.0, 2.0], [0.5, 1.0, 1.0, 1.0]])
    sloped = trend_at(_posterior(trend, slope_end=[1.0, 0.5]), t_idx)
    assert np.array_equal(sloped, [[1.0, 2.0, 3.0, 5.0], [0.5, 1.0, 1.5, 2.5]])


def test_simulate_trend_forward_centres_on_trend_at_and_widens_with_a_slope():
    n, h = 20_000, 12
    trend = np.zeros((n, 3))
    rng = np.random.default_rng(0)
    level = simulate_trend_forward(_posterior(trend, sigma_trend=0.1), h, rng)
    drift = simulate_trend_forward(
        _posterior(trend, slope_end=np.full(n, 0.2), sigma_trend=0.1), h, rng
    )
    walking = simulate_trend_forward(
        _posterior(trend, slope_end=np.full(n, 0.2), sigma_trend=0.1, sigma_slope=0.05), h, rng
    )
    k = np.arange(1, h + 1)
    assert np.allclose(level.mean(axis=0), 0.0, atol=0.02)
    assert np.allclose(drift.mean(axis=0), 0.2 * k, atol=0.02)
    assert np.allclose(walking.mean(axis=0), 0.2 * k, atol=0.05)
    # A known slope moves the forecast and adds no spread; a walking one adds
    # sum_{j<k} j^2 * sigma_slope^2 on top of the level's k * sigma_trend^2.
    assert np.allclose(level.std(axis=0), 0.1 * np.sqrt(k), rtol=0.05)
    assert np.allclose(drift.std(axis=0), 0.1 * np.sqrt(k), rtol=0.05)
    expected = np.sqrt(0.01 * k + 0.0025 * np.cumsum(np.arange(h) ** 2))
    assert np.allclose(walking.std(axis=0), expected, rtol=0.05)


# --- fitted ------------------------------------------------------------------


def _s8_world(trend_line="    trend: {kind: local_linear}"):
    """Drift, a parent and weekly seasonality: every term the two-state path
    has to reconstruct around."""
    rng = np.random.default_rng(8)
    n = 120
    x = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
    level = 0.15 * np.arange(n) + np.cumsum(rng.normal(0, 0.3, n))
    weekly = 2.0 * np.sin(2 * np.pi * np.arange(n) / 7)
    y = 0.5 * x + level + weekly + rng.normal(0, 1.0, n)
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "x": x, "y": y})
    yaml = f"""
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
{trend_line}
    seasonality:
      - period: 7
        name: weekly
"""
    return Parser(yaml).dag, frame


@pytest.mark.slow
def test_marginalized_local_linear_trend_samples_the_same_posterior(monkeypatch):
    """S8's acceptance test, the counterpart of S25's: the two-state Kalman
    path and the explicit latent (level and slope walks sampled directly) are
    one model. Every sampled parameter, the recovered `trend` at three
    periods and the last slope agree within four Monte-Carlo standard errors.

    A failure means the filter, the backward sampler or the explicit form's
    timing is wrong, and is investigated, never re-pinned.
    """
    import arviz as az

    import breakdown.engine.model as model

    dag, frame = _s8_world()
    new = fit_metric(dag, frame, "y", draws=500, random_seed=3)
    monkeypatch.setattr(model, "_level_is_marginalized", lambda method: False)
    old = fit_metric(dag, frame, "y", draws=500, random_seed=3)

    assert new.trend_kind == old.trend_kind == "local_linear"
    assert "trend_slope_z" in old.trace.posterior and "trend_slope_z" not in new.trace.posterior
    for name in ("trend", "trend_slope_end"):
        assert new.trace.posterior[name].dims == old.trace.posterior[name].dims
        assert new.trace.posterior[name].shape == old.trace.posterior[name].shape
    assert new.trace.posterior["trend"].dims == ("chain", "draw", "trend_dim_0")

    def mcse(x, method="mean", prob=None):
        kw = {"prob": prob} if prob is not None else {}
        return float(np.ravel(az.mcse(x, method=method, **kw))[0])

    names = (
        "alpha",
        "sigma_obs",
        "sigma_trend",
        "sigma_slope",
        "beta_x",
        "sin_weekly_h1",
        "cos_weekly_h1",
        "trend_slope_end",
    )
    pairs = [(n, old.trace.posterior[n].values, new.trace.posterior[n].values) for n in names]
    T = new.trace.posterior["trend"].shape[-1]
    for j in (0, T // 2, T - 1):
        pairs.append(
            (
                f"trend[{j}]",
                old.trace.posterior["trend"].values[..., j],
                new.trace.posterior["trend"].values[..., j],
            )
        )
    for name, a, b in pairs:
        se = np.hypot(mcse(a), mcse(b))
        assert abs(a.mean() - b.mean()) < 4 * se, (name, a.mean(), b.mean(), se)
        for q in (0.05, 0.95):
            qa, qb = np.quantile(a, q), np.quantile(b, q)
            q_se = np.hypot(mcse(a, "quantile", q), mcse(b, "quantile", q))
            assert abs(qa - qb) < 4 * q_se, (name, q, qa, qb, q_se)

    # Diagnostics are over what NUTS sampled (the slope's scale included), and
    # the posterior predictive check still ran.
    assert new.diagnostics["min_ess_bulk"] is not None
    assert new.diagnostics["ppc_status"] != "unavailable"


def _source_world(drift, n=100, seed=4):
    rng = np.random.default_rng(seed)
    y = 50.0 + drift * np.arange(n) + rng.normal(0, 2.0, n)
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "y": y})

    def dag(trend_line):
        return Parser(f"metrics:\n  - name: y\n    source: dbt.metric.y\n{trend_line}\n").dag

    return dag, frame


def _forecast(fit, h, seed=0, noise=False):
    """Business-unit draws `h` periods ahead: the forecast of the mean
    (`alpha + trend`), or with `noise` the predictive of the observations."""
    post = fit.trace.posterior
    rng = np.random.default_rng(seed)
    ahead = post["alpha"].values.reshape(-1, 1) + simulate_trend_forward(post, h, rng)
    if noise:
        ahead = ahead + post["sigma_obs"].values.reshape(-1, 1) * rng.standard_normal(ahead.shape)
    return fit.y_mean + fit.y_std * ahead


@pytest.mark.slow
@pytest.mark.parametrize(
    "trend_line",
    ["    trend: {kind: local_linear}", "    trend: {kind: local_linear, slope_sigma: 0}"],
    ids=["tight-slope-innovation", "fixed-drift"],
)
def test_a_planted_drift_is_recovered_and_carried_into_the_forecast(trend_line):
    """A series climbing 0.5 a period: the last slope's posterior covers the
    planted drift and is nowhere near zero, and the forecast keeps climbing
    where a local level's stays at the last level."""
    dag, frame = _source_world(drift=0.5)
    h = 12
    truth = 50.0 + 0.5 * (len(frame) - 1 + h)

    fit = fit_metric(dag(trend_line), frame, "y", draws=500, random_seed=1)
    slope = fit.trace.posterior["trend_slope_end"].values.reshape(-1) * fit.y_std
    lo, hi = np.quantile(slope, [0.03, 0.97])
    assert lo < 0.5 < hi and lo > 0.3, (lo, hi)
    assert ("sigma_slope" in fit.trace.posterior) == ("slope_sigma: 0" not in trend_line)
    ahead = _forecast(fit, h)[:, -1]
    assert abs(ahead.mean() - truth) < 2.0, (ahead.mean(), truth)

    level = fit_metric(dag(""), frame, "y", draws=500, random_seed=1)
    assert "trend_slope_end" not in level.trace.posterior
    flat = _forecast(level, h)[:, -1]
    assert truth - flat.mean() > 4.0, (flat.mean(), truth)  # ~ h * drift = 6 short


@pytest.mark.slow
def test_a_flat_series_is_not_harmed_by_the_slope():
    """No drift planted: the slope's posterior sits on zero, and the
    predictive is neither moved nor made materially wider than the local
    level's."""
    dag, frame = _source_world(drift=0.0)
    h = 4
    llt = fit_metric(dag("    trend: {kind: local_linear}"), frame, "y", draws=500, random_seed=1)
    level = fit_metric(dag(""), frame, "y", draws=500, random_seed=1)

    slope = llt.trace.posterior["trend_slope_end"].values.reshape(-1) * llt.y_std
    lo, hi = np.quantile(slope, [0.03, 0.97])
    assert lo < 0.0 < hi
    # Against a series whose noise sd is 2.0: under a tenth of that per period.
    assert abs(slope.mean()) < 0.1 and hi - lo < 0.4, (slope.mean(), lo, hi)

    a, b = _forecast(llt, h), _forecast(level, h)
    assert abs(a.mean() - b.mean()) < 0.5
    # What the slope costs here, stated rather than hidden. The forecast of
    # the *mean* is wider: an unknown slope, even one the data puts near zero,
    # adds `k * slope` of uncertainty `k` periods out (measured 1.5x at k = 4
    # on this series). Against the observation noise that is small, so the
    # predictive of what will be observed — one period, or the mean of the
    # window — is within 15% of the local level's.
    assert a[:, -1].std() < 2.0 * b[:, -1].std(), (a[:, -1].std(), b[:, -1].std())
    a, b = _forecast(llt, h, noise=True), _forecast(level, h, noise=True)
    assert a[:, -1].std() < 1.15 * b[:, -1].std(), (a[:, -1].std(), b[:, -1].std())
    assert a.mean(axis=1).std() < 1.15 * b.mean(axis=1).std()
    assert llt.diagnostics["fit_quality"] == level.diagnostics["fit_quality"]


@pytest.mark.slow
def test_rca_reads_a_sloped_trend_along_its_forecast_not_flat():
    """`components.trend` on a node with a local linear trend is the forecast
    along the last fitted slope. A child climbing 0.3 a period on top of a
    stationary parent: the analysis window's mean sits 14 periods past the
    reference window's, so the trend's share of the gap is about 4.2. Read
    flat, as a local level forecasts, the same node reports roughly half of
    that and leaves the rest unexplained."""
    from breakdown.engine.rca import run_rca

    rng = np.random.default_rng(5)
    n = 140
    x = 100.0 + rng.normal(0, 3.0, n)
    y = 0.5 * x + 0.3 * np.arange(n) + rng.normal(0, 1.0, n)
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "x": x, "y": y})
    windows = dict(
        reference_start="2024-04-22",
        reference_end="2024-05-05",
        analysis_start="2024-05-06",
        analysis_end="2024-05-19",
        reference_sensitivity=False,
    )

    def trend_component(trend_line):
        dag = Parser(_tree(trend_line)).dag
        return run_rca(dag, frame, {}, "y", **windows)["nodes"]["y"]["components"]["trend"]

    sloped = trend_component("    trend: {kind: local_linear}")
    lo, hi = sloped["ci_95"]
    assert lo < 4.2 < hi and abs(sloped["estimate"] - 4.2) < 1.0, sloped
    flat = trend_component("")
    assert flat["estimate"] < 3.2, flat


@pytest.mark.slow
@pytest.mark.parametrize("slope_sigma", ["0.001", "0"])
def test_advi_carries_the_same_trend_as_an_explicit_latent(slope_sigma):
    """The ADVI opt-in keeps explicit latents (roadmap S25), so the slope has
    an explicit form too rather than a refusal: the posterior holds `trend`
    and `trend_slope_end` under the names and dims the NUTS path writes, and
    readers past the fit work on it unchanged. Whether mean-field ADVI
    *approximates* this posterior well is what its k-hat reports, and is not
    asserted here."""
    dag, frame = _s8_world(f"    trend: {{kind: local_linear, slope_sigma: {slope_sigma}}}")
    fit = fit_metric(dag, frame, "y", draws=200, inference_method="advi", random_seed=2)
    post = fit.trace.posterior
    assert fit.trend_kind == "local_linear"
    assert post["trend"].dims == ("chain", "draw", "trend_dim_0")
    assert post["trend_slope_end"].dims == ("chain", "draw")
    assert ("trend_slope_z" in post) == (slope_sigma != "0")
    ahead = trend_at(post, np.array([len(frame) - 1, len(frame) + 9]))
    assert np.isfinite(ahead).all()
    slope = post["trend_slope_end"].values.reshape(-1)
    assert np.allclose(ahead[:, 1] - ahead[:, 0], 10 * slope)
    assert "khat" in fit.diagnostics

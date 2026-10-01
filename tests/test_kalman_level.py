"""Roadmap S25: the local level integrated out of the NUTS fit, checked exactly.

No sampler runs here. The claim S25 rests on is that the Kalman filter computes
the *same* likelihood the explicit per-period latent defined, and that the
forward-filtering backward-sampling draws come from the *same* conditional
posterior of the level. Both have closed forms for a local-level model, because
the level is a Gaussian random walk:

    level ~ N(0, s2 * K),  K[i, j] = min(i, j) + 1      (level[-1] = 0)
    r     = level + eps,   eps ~ N(0, o2 * I)

so `r ~ N(0, s2 K + o2 I)`, and `level | r` is Gaussian with precision
`(s2 K)^-1 + I / o2`. These tests compare the engine's filter and sampler
against that dense algebra. The sampler-level equivalence (old path vs new, on
a fitted world) lives in `tests/test_engine.py`, which is marked slow.
"""

import numpy as np
import pytest

from breakdown.engine.model import (
    _ffbs_local_level,
    _kalman_local_level_loglik,
    _level_is_marginalized,
    _seed_stream,
)


def _walk_cov(T: int, s2: float) -> np.ndarray:
    idx = np.arange(T)
    return s2 * (np.minimum.outer(idx, idx) + 1.0)


def _dense_loglik(r: np.ndarray, s2: float, o2: float) -> float:
    T = len(r)
    cov = _walk_cov(T, s2) + o2 * np.eye(T)
    sign, logdet = np.linalg.slogdet(cov)
    assert sign > 0
    return float(-0.5 * (T * np.log(2 * np.pi) + logdet + r @ np.linalg.solve(cov, r)))


@pytest.mark.parametrize(
    "s2, o2",
    [
        (0.05**2, 1.0),  # the default prior's scale: a slow level under noise
        (0.5, 0.1),  # a level that moves more than the noise
        (1e-8, 0.3),  # a near-constant level (sigma_trend at its lower edge)
    ],
)
def test_kalman_loglik_is_the_marginal_likelihood(s2, o2):
    """The prediction-error decomposition equals the dense Gaussian marginal,
    so NUTS is sampling the posterior the explicit latent defined."""
    import pytensor.tensor as pt

    rng = np.random.default_rng(7)
    T = 40
    level = np.cumsum(rng.normal(0, np.sqrt(s2), T))
    r = level + rng.normal(0, np.sqrt(o2), T)

    ll = _kalman_local_level_loglik(
        pt.as_tensor(r), pt.as_tensor(np.sqrt(s2)), pt.as_tensor(np.sqrt(o2))
    ).eval()

    assert float(ll) == pytest.approx(_dense_loglik(r, s2, o2), rel=1e-9, abs=1e-9)


def test_ffbs_draws_match_the_conditional_posterior_of_the_level():
    """Mean and covariance of the backward-sampled level match `level | r`
    in closed form. These are exact conditional draws — not smoothed means —
    so the recovered `trend` carries the level's posterior uncertainty."""
    rng = np.random.default_rng(11)
    T, s2, o2 = 8, 0.3, 0.5
    r_obs = np.cumsum(rng.normal(0, np.sqrt(s2), T)) + rng.normal(0, np.sqrt(o2), T)

    prior_prec = np.linalg.inv(_walk_cov(T, s2))
    post_cov = np.linalg.inv(prior_prec + np.eye(T) / o2)
    post_mean = post_cov @ (r_obs / o2)

    n = 40_000
    draws = _ffbs_local_level(
        np.tile(r_obs, (n, 1)), np.full(n, s2), np.full(n, o2), np.random.default_rng(3)
    )

    se_mean = np.sqrt(np.diag(post_cov) / n)
    assert np.all(np.abs(draws.mean(axis=0) - post_mean) < 5 * se_mean)
    # Covariance entries: an absolute tolerance scaled to the largest variance.
    assert np.allclose(np.cov(draws, rowvar=False), post_cov, atol=0.03 * post_cov.max())


def test_ffbs_is_vectorized_per_draw():
    """Each row gets its own parameters: a draw with a tiny sigma_trend
    recovers a near-flat level, a draw with a large one tracks the series."""
    T = 30
    r = np.linspace(-2, 2, T)
    level = _ffbs_local_level(
        np.stack([r, r]), np.array([1e-10, 10.0]), np.array([0.01, 0.01]), np.random.default_rng(0)
    )
    assert np.ptp(level[0]) < 1e-3
    assert np.allclose(level[1], r, atol=0.5)


def test_level_recovery_is_seeded_on_its_own_stream():
    """A seeded fit stays a pure function of its inputs, and the level and
    replicate streams never share draws with each other."""
    a = np.random.default_rng(_seed_stream(0, 25)).standard_normal(5)
    b = np.random.default_rng(_seed_stream(0, 25)).standard_normal(5)
    c = np.random.default_rng(_seed_stream(0, 3)).standard_normal(5)
    assert np.array_equal(a, b)
    assert not np.allclose(a, c)


def test_marginalization_is_nuts_only():
    """The ADVI variants keep the explicit latent their PSIS k-hat was
    measured against (roadmap S2)."""
    assert _level_is_marginalized("nuts")
    assert not _level_is_marginalized("advi")
    assert not _level_is_marginalized("fullrank_advi")

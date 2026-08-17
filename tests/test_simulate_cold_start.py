"""Tests for cold-start mode: `run_scenario` with data=None.

Degenerate priors (sigma 0) and point baselines make propagation exactly
predictable; spread priors and baseline ranges exercise the draw-aligned
uncertainty paths. No PyMC sampling runs anywhere in this file — cold-start mode
fits nothing by construction.
"""

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.simulate import (
    COLD_START_CAVEATS,
    Assumption,
    EffectRange,
    Intervention,
    ScenarioRequest,
    _prior_mean,
    run_scenario,
    validate_cold_start,
)
from breakdown.parser import Parser, Prior

COLD_START_YAML = """
metrics:
  - name: daily_sessions
    source: assumed
    baseline: 1000
    plausible: {min: 0, max: 5000}
  - name: order_count
    source: assumed
    parents: [daily_sessions]
    baseline: 100
    plausible: {min: 0, max: 150}
    priors:
      daily_sessions:
        distribution: Normal
        params: {mu: 0.1, sigma: 0.0}
  - name: average_order_value
    source: assumed
    baseline: 50
  - name: revenue
    source: assumed
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""


def make_dag(yaml_src: str = COLD_START_YAML):
    return Parser(yaml_src).dag


def run(dag, **scenario_kwargs):
    return run_scenario(dag, None, None, ScenarioRequest(**scenario_kwargs))


def test_point_beliefs_propagate_exactly():
    """Degenerate priors and point baselines: cold-start mode is exact arithmetic,
    and the response is labeled as beliefs, not fits."""
    result = run(
        dag := make_dag(),
        interventions=[
            Intervention(metric="daily_sessions", mode="delta", value=200.0),
        ],
    )
    assert result["mode"] == "cold_start"
    assert result["baseline_window"] is None
    assert result["caveats"] == COLD_START_CAVEATS
    assert validate_cold_start(dag) == []

    # asserted baselines pass through; the formula baseline is derived
    assert result["nodes"]["daily_sessions"]["baseline"] == pytest.approx(1000.0)
    assert result["nodes"]["revenue"]["baseline"] == pytest.approx(100.0 * 50.0)

    oc = result["nodes"]["order_count"]
    assert oc["status"] == "affected"
    assert oc["delta"]["estimate"] == pytest.approx(0.1 * 200.0)
    assert oc["delta"]["ci_95"] == [pytest.approx(20.0), pytest.approx(20.0)]
    assert oc["fit_quality"] is None
    assert oc["baseline_ci_95"] is None  # point baseline
    assert oc["extrapolation"] == {"flag": False, "plausible_min": 0, "plausible_max": 150}

    rev = result["nodes"]["revenue"]
    assert rev["delta"]["estimate"] == pytest.approx(20.0 * 50.0)
    assert rev["simulated"] == pytest.approx(6000.0)
    assert rev["prob_direction"] == 1.0
    # no plausible bounds declared on revenue -> honest nulls, never a flag
    assert rev["extrapolation"] == {
        "flag": False,
        "plausible_min": None,
        "plausible_max": None,
    }

    assert result["nodes"]["average_order_value"]["status"] == "baseline"


def test_spread_prior_widens_ci_draw_aligned():
    """A Normal prior's spread becomes the delta distribution, and the CI
    scales draw-aligned through the downstream formula node — the fitted-mode
    posterior test, with the prior in the posterior's seat."""
    dag = make_dag(COLD_START_YAML.replace("sigma: 0.0", "sigma: 0.02"))
    result = run(
        dag,
        interventions=[
            Intervention(metric="daily_sessions", mode="delta", value=200.0),
        ],
    )
    oc = result["nodes"]["order_count"]
    # delta ~ Normal(20, 4): 95% CI ~ [12.2, 27.8]
    assert oc["delta"]["estimate"] == pytest.approx(20.0, abs=0.5)
    lo, hi = oc["delta"]["ci_95"]
    assert 11.0 < lo < 13.5
    assert 26.5 < hi < 29.0

    rev = result["nodes"]["revenue"]
    assert rev["delta"]["ci_95"][0] == pytest.approx(50.0 * lo)
    assert rev["delta"]["ci_95"][1] == pytest.approx(50.0 * hi)

    # seeded rng: identical calls are identical responses
    again = run(
        dag,
        interventions=[
            Intervention(metric="daily_sessions", mode="delta", value=200.0),
        ],
    )
    assert again == result


def test_uncertain_baseline_composes_into_deltas():
    """A baseline range becomes baseline_ci_95, flows into the derived formula
    baseline, and widens deltas that consume it (revenue = orders x aov with
    uncertain aov)."""
    dag = make_dag(COLD_START_YAML.replace("baseline: 50", "baseline: {low: 40, high: 60}"))
    result = run(
        dag,
        interventions=[
            Intervention(metric="order_count", mode="delta", value=20.0),
        ],
    )

    aov = result["nodes"]["average_order_value"]
    assert aov["status"] == "baseline"
    assert aov["baseline"] == pytest.approx(50.0, abs=0.5)  # seeded MC mean
    lo, hi = aov["baseline_ci_95"]  # Normal(50, 6.08): ~[38.1, 61.9]
    assert 36.5 < lo < 39.5
    assert 60.5 < hi < 63.5

    rev = result["nodes"]["revenue"]
    assert rev["baseline"] == pytest.approx(5000.0, abs=60.0)  # MC mean of 100·aov
    blo, bhi = rev["baseline_ci_95"]
    assert 3650.0 < blo < 3950.0
    assert 6050.0 < bhi < 6350.0
    # delta = 20 · aov_draws: estimate ~1000, CI ~20·[38.1, 61.9]
    assert rev["delta"]["estimate"] == pytest.approx(1000.0, abs=15.0)
    dlo, dhi = rev["delta"]["ci_95"]
    assert 730.0 < dlo < 790.0
    assert 1210.0 < dhi < 1270.0
    # Every draw is positive but the draws do vary, so the direction
    # probability saturates and publishes the Monte Carlo's ceiling as a bound.
    assert rev["prob_direction_censored"] is True
    assert rev["prob_direction"] == pytest.approx(1.0 - 1.0 / result["n_draws"])


def test_set_intervention_pins_level_not_delta():
    """`set` pins the LEVEL exactly under an uncertain baseline: the simulated
    value is the pinned value, while the delta honestly carries baseline
    uncertainty."""
    dag = make_dag(COLD_START_YAML.replace("baseline: 1000", "baseline: {low: 800, high: 1200}"))
    result = run(
        dag,
        interventions=[
            Intervention(metric="daily_sessions", mode="set", value=1500.0),
        ],
    )
    ds = result["nodes"]["daily_sessions"]
    assert ds["status"] == "intervened"
    assert ds["simulated"] == pytest.approx(1500.0)
    assert ds["delta"]["estimate"] == pytest.approx(500.0, abs=10.0)
    dlo, dhi = ds["delta"]["ci_95"]
    assert dlo < 500.0 < dhi and dhi - dlo > 100.0  # inherits baseline spread


def test_relative_assumption_scales_by_baseline_draws():
    """A relative assumption multiplies the target's baseline draws, staying
    draw-aligned with the uncertain worldview."""
    dag = make_dag(COLD_START_YAML.replace("baseline: 50", "baseline: {low: 40, high: 60}"))
    result = run(
        dag,
        assumptions=[
            Assumption(
                source="promo",
                target="average_order_value",
                effect=EffectRange(kind="relative", low=0.1, high=0.1),
            ),
        ],
    )
    aov = result["nodes"]["average_order_value"]
    assert aov["delta"]["estimate"] == pytest.approx(5.0, abs=0.2)  # 0.1 · ~50
    dlo, dhi = aov["delta"]["ci_95"]  # 0.1 · [~38.1, ~61.9]
    assert 3.6 < dlo < 4.0
    assert 6.0 < dhi < 6.4


def test_plausible_bounds_flag_extrapolation():
    result = run(
        make_dag(),
        interventions=[
            Intervention(metric="daily_sessions", mode="delta", value=1000.0),
        ],
    )
    oc = result["nodes"]["order_count"]  # 100 + 100 = 200 > plausible max 150
    assert oc["extrapolation"]["flag"] is True
    assert any(
        w["kind"] == "extrapolation"
        and w["metric"] == "order_count"
        and "plausible max" in w["detail"]
        for w in result["warnings"]
    )
    # revenue moved further but declares no bounds: no flag, no invented band
    assert result["nodes"]["revenue"]["extrapolation"]["flag"] is False


def test_shapley_decomposition_uses_prior_means():
    """Source contributions sum exactly to the point delta (efficiency), with
    the orders x aov interaction apportioned — the fitted decomposition test
    with analytic prior means in the posterior means' seat."""
    result = run(
        make_dag(),
        interventions=[Intervention(metric="daily_sessions", mode="pct", value=0.15)],
        assumptions=[
            Assumption(
                source="promo",
                target="average_order_value",
                effect=EffectRange(kind="relative", low=0.1, high=0.1),
            )
        ],
    )
    # sessions +150 -> orders +15; aov +5; revenue = 115*55 - 100*50 = 1325
    rev = result["nodes"]["revenue"]
    assert rev["delta"]["estimate"] == pytest.approx(1325.0)
    contribs = {c["source"]: c["estimate"] for c in rev["contributions"]}
    assert sum(contribs.values()) == pytest.approx(1325.0)
    assert contribs["i:daily_sessions"] == pytest.approx(15 * 50 + 75 / 2)
    assert contribs["a0"] == pytest.approx(100 * 5 + 75 / 2)


def test_sign_constrained_prior_stays_positive():
    """HalfNormal encodes 'surely positive': every draw respects the sign and
    the point value is the analytic mean."""
    dag = make_dag(
        COLD_START_YAML.replace(
            "distribution: Normal\n        params: {mu: 0.1, sigma: 0.0}",
            "distribution: HalfNormal\n        params: {sigma: 0.1}",
        )
    )
    result = run(
        dag,
        interventions=[
            Intervention(metric="daily_sessions", mode="delta", value=200.0),
        ],
    )
    oc = result["nodes"]["order_count"]
    expected_mean = 200.0 * 0.1 * np.sqrt(2.0 / np.pi)  # ~15.96
    assert oc["delta"]["estimate"] == pytest.approx(expected_mean, rel=0.05)
    assert oc["delta"]["ci_95"][0] > 0.0
    # "Every draw respects the sign" is a saturated count, not certainty: it
    # publishes the resolution ceiling, flagged as the bound it is.
    assert oc["prob_direction_censored"] is True
    assert oc["prob_direction"] == pytest.approx(1.0 - 1.0 / result["n_draws"])
    contribs = {c["source"]: c["estimate"] for c in oc["contributions"]}
    assert contribs["i:daily_sessions"] == pytest.approx(expected_mean)


def test_prior_means_analytic():
    assert _prior_mean(Prior(distribution="Normal", params={"mu": 0.3})) == 0.3
    assert _prior_mean(Prior(distribution="HalfNormal", params={"sigma": 2.0})) == (
        pytest.approx(2.0 * np.sqrt(2.0 / np.pi))
    )
    assert _prior_mean(Prior(distribution="Exponential", params={"lam": 4.0})) == 0.25
    assert _prior_mean(Prior(distribution="LogNormal", params={"mu": 0.0, "sigma": 0.5})) == (
        pytest.approx(np.exp(0.125))
    )


def test_validation_errors():
    iv = [Intervention(metric="daily_sessions", mode="delta", value=1.0)]

    # cold-start mode rejects a baseline window — operating points come from YAML
    with pytest.raises(ValueError, match="no baseline window"):
        run_scenario(
            make_dag(),
            None,
            None,
            ScenarioRequest(
                baseline_start="2024-01-01", baseline_end="2024-01-31", interventions=iv
            ),
        )

    # fitted mode still requires the window (now optional in the model)
    with pytest.raises(ValueError, match="required"):
        run_scenario(make_dag(), pd.DataFrame(), {}, ScenarioRequest(interventions=iv))

    # missing prior on a probabilistic edge
    no_prior = COLD_START_YAML.replace(
        """    priors:
      daily_sessions:
        distribution: Normal
        params: {mu: 0.1, sigma: 0.0}
""",
        "",
    )
    problems = validate_cold_start(make_dag(no_prior))
    assert any("daily_sessions' -> 'order_count'" in p for p in problems)
    with pytest.raises(ValueError, match="not cold-start ready"):
        run(make_dag(no_prior), interventions=iv)

    # missing baseline on a non-formula node
    no_baseline = COLD_START_YAML.replace("    baseline: 50\n", "")
    problems = validate_cold_start(make_dag(no_baseline))
    assert any("average_order_value" in p and "baseline" in p for p in problems)
    with pytest.raises(ValueError, match="not cold-start ready"):
        run(make_dag(no_baseline), interventions=iv)


def test_parser_contract():
    # shorthand coercion: `baseline: 1000` == {low: 1000, high: 1000}
    defn = Parser(COLD_START_YAML).get_metric("daily_sessions")
    assert defn.baseline.low == defn.baseline.high == 1000.0
    assert defn.baseline.is_point and defn.baseline.mu == 1000.0

    # formula nodes derive their baseline; asserting one is rejected
    with pytest.raises(Exception, match="formula"):
        Parser(
            COLD_START_YAML.replace(
                'formula: "order_count * average_order_value"',
                'formula: "order_count * average_order_value"\n    baseline: 4000',
            )
        )

    # malformed ranges are rejected
    with pytest.raises(Exception, match="low"):
        Parser(COLD_START_YAML.replace("baseline: 50", "baseline: {low: 60, high: 40}"))
    with pytest.raises(Exception, match="plausible"):
        Parser(
            COLD_START_YAML.replace(
                "plausible: {min: 0, max: 150}", "plausible: {min: 150, max: 0}"
            )
        )
    with pytest.raises(Exception, match="plausible"):
        Parser(COLD_START_YAML.replace("plausible: {min: 0, max: 150}", "plausible: {}"))


# ---------------------------------------------------------------------------
# C7: baseline draws respect the declared bounds, LogNormal beliefs exist,
# and a ratio over a zero-crossing divisor is refused rather than summarized.


def _stub_defn(low, high, distribution="Normal", pmin=None, pmax=None):
    yaml_src = f"""
metrics:
  - name: m
    source: assumed
    baseline: {{low: {low}, high: {high}, distribution: {distribution}}}
"""
    if pmin is not None or pmax is not None:
        parts = []
        if pmin is not None:
            parts.append(f"min: {pmin}")
        if pmax is not None:
            parts.append(f"max: {pmax}")
        yaml_src = yaml_src.rstrip() + f"\n    plausible: {{{', '.join(parts)}}}\n"
    return Parser(yaml_src).dag.nodes["m"]["definition"]


def test_baseline_draws_are_truncated_at_plausible_bounds():
    """C7a: `plausible` used to be consulted only *after* sampling, for
    extrapolation flags — the shipped example drew ~1% negative customer
    counts. Truncation is rejection resampling, not clipping, so no point
    mass piles up on the bound."""
    from breakdown.engine.simulate import _baseline_draws

    # A belief hugging zero: mu 70, sigma ~30 puts real mass below 0.
    defn = _stub_defn(20, 120, pmin=0)
    draws = _baseline_draws("m", defn, np.random.default_rng(0), 20_000)
    assert float(draws.min()) >= 0.0
    # Rejection, not clipping: nothing sits exactly on the bound.
    assert (draws == 0.0).sum() == 0

    # Without bounds the belief is taken at its word (draws may be negative).
    free = _stub_defn(20, 120)
    assert float(_baseline_draws("m", free, np.random.default_rng(0), 20_000).min()) < 0.0


def test_lognormal_baseline_reads_low_high_on_the_log_scale():
    """C7b: an order-of-magnitude belief ("between 2 and 40") as a LogNormal —
    support excludes zero by construction, and [low, high] is the central 90%
    interval on the log scale."""
    from breakdown.engine.simulate import _baseline_draws

    defn = _stub_defn(2, 40, distribution="LogNormal")
    draws = _baseline_draws("m", defn, np.random.default_rng(0), 50_000)
    assert float(draws.min()) > 0.0
    assert np.median(draws) == pytest.approx(np.sqrt(2 * 40), rel=0.05)
    inside = ((draws >= 2) & (draws <= 40)).mean()
    assert inside == pytest.approx(0.90, abs=0.01)


def test_lognormal_baseline_requires_positive_low():
    with pytest.raises(Exception, match="LogNormal requires low > 0"):
        _stub_defn(0, 40, distribution="LogNormal")


def test_contradictory_baseline_and_plausible_raise_with_both_named():
    """A belief whose mass lies almost entirely outside its own plausible
    range cannot be resampled honestly — both declarations came from the same
    author, and only the author knows which is wrong."""
    from breakdown.engine.simulate import _baseline_draws

    defn = _stub_defn(20, 120, pmin=1000)
    with pytest.raises(ValueError, match="contradict"):
        _baseline_draws("m", defn, np.random.default_rng(0), 1000)


RATIO_YAML = """
metrics:
  - name: spend
    source: assumed
    baseline: {low: 8000, high: 12000}
    plausible: {min: 0}
  - name: signups
    source: assumed
    baseline: {low: 2, high: 40}
  - name: cac
    source: assumed
    formula: "spend / signups"
    parents: [spend, signups]
"""


def test_zero_crossing_divisor_is_refused_not_summarized():
    """C7c, option 1: the Monte-Carlo mean of a ratio whose divisor draws
    cross zero does not exist — the $2.1M-CAC pathology. Refused with the
    remedies named, rather than switching the centre to a median, which would
    break the property that per-source contributions sum exactly to the delta
    (the mean is linear; a median is not)."""
    dag = make_dag(RATIO_YAML)
    with pytest.raises(ValueError, match="divisor 'signups'.*cross zero"):
        run(dag, interventions=[Intervention(metric="spend", mode="pct", value=0.1)])


def test_bounded_divisor_yields_a_finite_defensible_cac():
    """The same belief with a plausible floor (or a LogNormal) is fine: the
    mean exists once the divisor cannot cross zero."""
    floored = RATIO_YAML.replace(
        "    baseline: {low: 2, high: 40}\n",
        "    baseline: {low: 2, high: 40}\n    plausible: {min: 1}\n",
    )
    result = run(
        make_dag(floored),
        interventions=[Intervention(metric="spend", mode="pct", value=0.1)],
    )
    cac = result["nodes"]["cac"]
    assert np.isfinite(cac["baseline"])
    # An order-of-magnitude denominator belief gives a wide but sane CAC —
    # hundreds to thousands per signup, not the pre-fix $2.1M artifact.
    assert 100 < cac["baseline"] < 20_000

    lognormal = RATIO_YAML.replace(
        "baseline: {low: 2, high: 40}",
        "baseline: {low: 2, high: 40, distribution: LogNormal}",
    )
    result = run(
        make_dag(lognormal),
        interventions=[Intervention(metric="spend", mode="pct", value=0.1)],
    )
    assert np.isfinite(result["nodes"]["cac"]["baseline"])

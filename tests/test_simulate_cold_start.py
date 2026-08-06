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
    result = run(dag := make_dag(), interventions=[
        Intervention(metric="daily_sessions", mode="delta", value=200.0),
    ])
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
        "flag": False, "plausible_min": None, "plausible_max": None,
    }

    assert result["nodes"]["average_order_value"]["status"] == "baseline"


def test_spread_prior_widens_ci_draw_aligned():
    """A Normal prior's spread becomes the delta distribution, and the CI
    scales draw-aligned through the downstream formula node — the fitted-mode
    posterior test, with the prior in the posterior's seat."""
    dag = make_dag(COLD_START_YAML.replace("sigma: 0.0", "sigma: 0.02"))
    result = run(dag, interventions=[
        Intervention(metric="daily_sessions", mode="delta", value=200.0),
    ])
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
    again = run(dag, interventions=[
        Intervention(metric="daily_sessions", mode="delta", value=200.0),
    ])
    assert again == result


def test_uncertain_baseline_composes_into_deltas():
    """A baseline range becomes baseline_ci_95, flows into the derived formula
    baseline, and widens deltas that consume it (revenue = orders x aov with
    uncertain aov)."""
    dag = make_dag(COLD_START_YAML.replace("baseline: 50", "baseline: {low: 40, high: 60}"))
    result = run(dag, interventions=[
        Intervention(metric="order_count", mode="delta", value=20.0),
    ])

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
    assert rev["prob_direction"] == 1.0


def test_set_intervention_pins_level_not_delta():
    """`set` pins the LEVEL exactly under an uncertain baseline: the simulated
    value is the pinned value, while the delta honestly carries baseline
    uncertainty."""
    dag = make_dag(COLD_START_YAML.replace("baseline: 1000", "baseline: {low: 800, high: 1200}"))
    result = run(dag, interventions=[
        Intervention(metric="daily_sessions", mode="set", value=1500.0),
    ])
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
    result = run(dag, assumptions=[
        Assumption(source="promo", target="average_order_value",
                   effect=EffectRange(kind="relative", low=0.1, high=0.1)),
    ])
    aov = result["nodes"]["average_order_value"]
    assert aov["delta"]["estimate"] == pytest.approx(5.0, abs=0.2)  # 0.1 · ~50
    dlo, dhi = aov["delta"]["ci_95"]  # 0.1 · [~38.1, ~61.9]
    assert 3.6 < dlo < 4.0
    assert 6.0 < dhi < 6.4


def test_plausible_bounds_flag_extrapolation():
    result = run(make_dag(), interventions=[
        Intervention(metric="daily_sessions", mode="delta", value=1000.0),
    ])
    oc = result["nodes"]["order_count"]  # 100 + 100 = 200 > plausible max 150
    assert oc["extrapolation"]["flag"] is True
    assert any(
        w["kind"] == "extrapolation" and w["metric"] == "order_count"
        and "plausible max" in w["detail"]
        for w in result["warnings"]
    )
    # revenue moved further but declares no bounds: no flag, no invented band
    assert result["nodes"]["revenue"]["extrapolation"]["flag"] is False


def test_shapley_decomposition_uses_prior_means():
    """Source contributions sum exactly to the point delta (efficiency), with
    the orders x aov interaction apportioned — the fitted decomposition test
    with analytic prior means in the posterior means' seat."""
    result = run(make_dag(),
        interventions=[Intervention(metric="daily_sessions", mode="pct", value=0.15)],
        assumptions=[Assumption(source="promo", target="average_order_value",
                                effect=EffectRange(kind="relative", low=0.1, high=0.1))],
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
    dag = make_dag(COLD_START_YAML.replace(
        "distribution: Normal\n        params: {mu: 0.1, sigma: 0.0}",
        "distribution: HalfNormal\n        params: {sigma: 0.1}",
    ))
    result = run(dag, interventions=[
        Intervention(metric="daily_sessions", mode="delta", value=200.0),
    ])
    oc = result["nodes"]["order_count"]
    expected_mean = 200.0 * 0.1 * np.sqrt(2.0 / np.pi)  # ~15.96
    assert oc["delta"]["estimate"] == pytest.approx(expected_mean, rel=0.05)
    assert oc["delta"]["ci_95"][0] > 0.0
    assert oc["prob_direction"] == 1.0
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
        run_scenario(make_dag(), None, None, ScenarioRequest(
            baseline_start="2024-01-01", baseline_end="2024-01-31", interventions=iv))

    # fitted mode still requires the window (now optional in the model)
    with pytest.raises(ValueError, match="required"):
        run_scenario(make_dag(), pd.DataFrame(), {}, ScenarioRequest(interventions=iv))

    # missing prior on a probabilistic edge
    no_prior = COLD_START_YAML.replace(
        """    priors:
      daily_sessions:
        distribution: Normal
        params: {mu: 0.1, sigma: 0.0}
""", "")
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
        Parser(COLD_START_YAML.replace(
            'formula: "order_count * average_order_value"',
            'formula: "order_count * average_order_value"\n    baseline: 4000',
        ))

    # malformed ranges are rejected
    with pytest.raises(Exception, match="low"):
        Parser(COLD_START_YAML.replace("baseline: 50", "baseline: {low: 60, high: 40}"))
    with pytest.raises(Exception, match="plausible"):
        Parser(COLD_START_YAML.replace(
            "plausible: {min: 0, max: 150}", "plausible: {min: 150, max: 0}"))
    with pytest.raises(Exception, match="plausible"):
        Parser(COLD_START_YAML.replace("plausible: {min: 0, max: 150}", "plausible: {}"))


# --- bounded belief draws and defensible central points (C7) ---

BOUNDED_YAML = """
metrics:
  - name: paying_customers
    source: assumed
    kind: stock
    baseline: {low: 20, high: 120}
    plausible: {min: 0}
  - name: avg_price
    source: assumed
    kind: rate
    baseline: {low: 30, high: 80}
    plausible: {min: 0}
  - name: mrr
    source: assumed
    formula: "paying_customers * avg_price"
    parents: [paying_customers, avg_price]
    plausible: {min: 0}
"""


def test_baseline_draws_respect_plausible_min():
    """`plausible: {min: 0}` is the author stating an impossibility. It used to
    be consulted only when flagging the result, so the shipped example tree
    drew ~1.1% negative customer counts and `mrr` inherited them (C7)."""
    dag = make_dag(BOUNDED_YAML)
    res = run(dag, interventions=[Intervention(metric="avg_price", mode="delta", value=0.0)],
              n_draws=4000)

    for name in ("paying_customers", "avg_price", "mrr"):
        ci = res["nodes"][name]["baseline_ci_95"]
        assert ci[0] >= 0.0, f"{name} lower belief bound went negative: {ci}"


def test_truncation_keeps_the_upper_bound_and_shifts_only_the_clipped_tail():
    """Truncation is rejection, not clipping: no spike of mass piles up on the
    boundary, and the untouched side of the interval is unmoved."""
    dag = make_dag(BOUNDED_YAML)
    res = run(dag, interventions=[Intervention(metric="avg_price", mode="delta", value=0.0)],
              n_draws=4000)

    lo, hi = res["nodes"]["paying_customers"]["baseline_ci_95"]
    # Upper tail untouched by a floor at 0; lower tail lifted off the negatives.
    assert 120 < hi < 140
    assert lo > 0


def test_lognormal_baseline_honours_the_stated_interval_exactly():
    """A truncated normal renormalizes the belief; a lognormal reproduces the
    stated central 90% exactly and is positive by construction."""
    yaml_src = """
metrics:
  - name: signups
    source: assumed
    baseline: {low: 20, high: 120, distribution: lognormal}
    plausible: {min: 0}
"""
    dag = make_dag(yaml_src)
    res = run(dag, interventions=[Intervention(metric="signups", mode="delta", value=0.0)],
              n_draws=20000)

    ci = res["nodes"]["signups"]["baseline_ci_95"]
    assert ci[0] > 0
    # The stated belief is reproduced without renormalization: a lognormal
    # fitted to [20, 120] needs no truncation, so its 95% span brackets that
    # interval rather than being clipped inside it.
    assert ci[0] < 20 and ci[1] > 120

    # Right-skewed, so its reported mean (~57) sits well below the normal's 70.
    # That shift is exactly why the distribution is opt-in rather than the
    # default: it re-reads the author's interval as a multiplicative belief.
    assert 52 < res["nodes"]["signups"]["baseline"] < 62


def test_lognormal_baseline_rejects_a_nonpositive_low():
    with pytest.raises(ValueError, match="lognormal baseline must have low > 0"):
        Parser("""
metrics:
  - name: x
    source: assumed
    baseline: {low: 0, high: 100, distribution: lognormal}
""")


def test_baseline_contradicting_its_plausible_bounds_raises():
    """A belief lying essentially outside its own bound is a contradiction in
    the tree; looping forever to satisfy it would hide the mistake."""
    yaml_src = """
metrics:
  - name: x
    source: assumed
    baseline: {low: 500, high: 900}
    plausible: {max: 10}
"""
    with pytest.raises(ValueError, match="lies almost entirely outside"):
        run(make_dag(yaml_src),
            interventions=[Intervention(metric="x", mode="delta", value=0.0)])


RATIO_YAML = """
metrics:
  - name: spend
    source: assumed
    baseline: {low: 4000, high: 12000}
    plausible: {min: 0}
  - name: signups
    source: assumed
    baseline: {low: 2, high: 40}
    plausible: {min: 0}
  - name: cac
    source: assumed
    kind: rate
    formula: "spend / signups"
    parents: [spend, signups]
    plausible: {min: 0}
"""


def test_ratio_node_reports_a_defensible_point_and_says_it_switched():
    """"Somewhere between 2 and 40 signups a month" — an ordinary
    order-of-magnitude belief — produced a CAC of $2.1M with a negative lower
    bound, because the MC mean of a near-zero-denominator ratio estimates
    nothing (C7)."""
    dag = make_dag(RATIO_YAML)
    res = run(dag, interventions=[Intervention(metric="spend", mode="delta", value=0.0)],
              n_draws=2000)

    cac = res["nodes"]["cac"]
    assert 100 < cac["baseline"] < 2000, cac["baseline"]
    assert cac["baseline_ci_95"][0] > 0, "a cost per acquisition cannot be negative"
    # The swap is disclosed, and names the node whose number changed meaning.
    assert any("median" in c and "cac" in c for c in res["caveats"])


def test_ratio_point_is_stable_across_seeds():
    """The property the median fallback exists for: the reported number must
    not swing several-fold between runs. The mean varied 2.5-6x."""
    dag = make_dag(RATIO_YAML)
    points = []
    for seed in range(6):
        res = run(dag, interventions=[Intervention(metric="spend", mode="delta", value=0.0)],
                  n_draws=2000, random_seed=seed)
        points.append(res["nodes"]["cac"]["baseline"])
    assert max(points) / min(points) < 1.25, points


def test_well_behaved_nodes_keep_the_mean_and_carry_no_swap_caveat():
    """The fallback must not fire on the node shape cold-start trees are mostly
    made of: the mean reconciles a product to 0.25% where the median is 5.4%
    off, so switching everything would have been a regression."""
    dag = make_dag(BOUNDED_YAML)
    res = run(dag, interventions=[Intervention(metric="avg_price", mode="delta", value=0.0)],
              n_draws=4000)

    assert not any("median" in c for c in res["caveats"])
    assert res["caveats"] == COLD_START_CAVEATS

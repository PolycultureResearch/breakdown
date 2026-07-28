import numpy as np
import pandas as pd
import pytest

from breakdown.engine.rca import _block_bootstrap_indices, run_rca, shapley_attribution
from breakdown.parser import Parser
from tests.synthetic import generate_mock_data

JAFFLE_YAML = """
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
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""

REF = ("2024-01-01", "2024-02-15")
AN = ("2024-02-16", "2024-04-09")


def make_tree():
    parser = Parser(JAFFLE_YAML)
    data = generate_mock_data(n_days=100)
    return parser.dag, data


def rca_on(dag, data, traces, target):
    return run_rca(dag, data, traces, target, REF[0], REF[1], AN[0], AN[1], advi_draws=300)


def test_rca_formula_attribution():
    """Formula node revenue uses Shapley; bootstrap-mean estimates approximately
    sum to gap - unexplained, and carry CIs (T7)."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    rev = result["nodes"]["revenue"]
    assert rev["attribution_method"] == "shapley"
    assert rev["components"] is None
    parents = {c["parent"] for c in rev["contributions"]}
    assert parents == {"order_count", "average_order_value"}

    # Estimates are bootstrap means, so the identity holds only approximately.
    total = sum(c["estimate"] for c in rev["contributions"])
    gap = rev["gap"]
    assert abs(total - (gap - rev["unexplained"])) < 0.05 * max(1, abs(gap))

    for c in rev["contributions"]:
        assert c["ci_95"] is not None and c["ci_95"][0] <= c["ci_95"][1]
        assert 0.5 <= c["prob_same_direction"] <= 1.0


def test_rca_posterior_attribution():
    """Probabilistic node order_count uses the posterior over beta_raw."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "order_count")

    oc = result["nodes"]["order_count"]
    assert oc["attribution_method"] == "posterior"
    assert len(oc["contributions"]) == 1

    c = oc["contributions"][0]
    assert c["parent"] == "daily_sessions"
    assert c["ci_95"][0] < c["estimate"] < c["ci_95"][1]
    assert 0.5 <= c["prob_same_direction"] <= 1.0

    # T5: probabilistic nodes report the fitted trend/seasonal deltas.
    comps = oc["components"]
    assert set(comps) == {"trend", "seasonal"}
    for term in comps.values():
        assert term["ci_95"][0] <= term["estimate"] <= term["ci_95"][1]
    # No seasonality declared on order_count -> exactly zero seasonal delta.
    assert comps["seasonal"]["estimate"] == 0.0


def test_rca_on_demand_fitting_minimal():
    """Only probabilistic non-root nodes in scope get fit; roots and formula
    nodes are not. New fits land in the caller's trace cache, keyed by the
    analysis-window start (fit_end), not the bare metric name."""
    dag, data = make_tree()
    traces = {}

    rca_on(dag, data, traces, "revenue")

    assert set(traces.keys()) == {("order_count", AN[0])}


def test_rca_trace_reuse():
    """A cached trace is reused, not re-fit, on a subsequent call with the same
    analysis window."""
    dag, data = make_tree()
    traces = {}
    rca_on(dag, data, traces, "revenue")
    trace = traces[("order_count", AN[0])]

    rca_on(dag, data, traces, "revenue")

    assert traces[("order_count", AN[0])] is trace


def test_rca_trace_keyed_by_fit_end():
    """The on-demand fit is keyed by (name, analysis_start); the bare name is
    never used, so a contaminated full-window fit cannot shadow it."""
    dag, data = make_tree()
    traces = {}

    rca_on(dag, data, traces, "order_count")

    assert ("order_count", AN[0]) in traces
    assert "order_count" not in traces


def test_rca_root_target():
    """RCA on a root returns just that node with no contributions or causes."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "daily_sessions")

    assert set(result["nodes"].keys()) == {"daily_sessions"}
    node = result["nodes"]["daily_sessions"]
    assert node["attribution_method"] is None
    assert node["contributions"] == []
    assert node["components"] is None
    assert result["ranked_causes"] == []


def test_rca_ranked_causes():
    """ranked_causes is non-empty, sorted descending, and excludes the target."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    ranked = result["ranked_causes"]
    assert len(ranked) > 0
    assert all(r["metric"] != "revenue" for r in ranked)
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rca_unknown_target_raises():
    dag, data = make_tree()
    with pytest.raises(ValueError, match="not found in the metric tree"):
        rca_on(dag, data, {}, "nope")


# --- Standalone Shapley attribution (the GET /shapley contract) ---

def test_shapley_attribution_sums_to_gap():
    dag, data = make_tree()

    result = shapley_attribution(dag, data, "revenue", REF[0], REF[1], AN[0], AN[1])

    assert set(result["attribution"].keys()) == {"order_count", "average_order_value"}
    assert abs(result["gap"] - (result["actual"] - result["baseline"])) < 1e-3
    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-3


def test_shapley_attribution_no_formula_raises():
    dag, data = make_tree()

    with pytest.raises(ValueError, match="no formula"):
        shapley_attribution(dag, data, "order_count", REF[0], REF[1], AN[0], AN[1])


# --- Trend & seasonal components (T5) ---

def test_rca_seasonal_component_captures_weekly_pattern():
    """y = 0.3x + 5·sin(2πt/7) + noise, x ~flat. A whole-week reference window
    vs a weekday-skewed analysis window creates a purely seasonal gap; the
    seasonal component must pick it up (right sign) and shrink |unexplained|."""
    rng = np.random.default_rng(3)
    n = 120
    t = np.arange(n)
    x = 100.0 + rng.normal(0, 1.0, n)
    y = 0.3 * x + 5.0 * np.sin(2 * np.pi * t / 7) + rng.normal(0, 0.3, n)
    dates = pd.date_range("2024-01-01", periods=n)
    data = pd.DataFrame({"date": dates, "x": x, "y": y})

    yaml_content = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
    seasonality:
      - period: 7
        name: weekly
"""
    parser = Parser(yaml_content)
    ref = (str(dates[56].date()), str(dates[83].date()))    # 4 whole weeks
    an = (str(dates[84].date()), str(dates[93].date()))     # 10 days, weekday-skewed

    result = run_rca(parser.dag, data, {}, "y", ref[0], ref[1], an[0], an[1], advi_draws=300)

    node = result["nodes"]["y"]
    comps = node["components"]
    true_delta = 5.0 * (np.sin(2 * np.pi * t[84:94] / 7).mean()
                        - np.sin(2 * np.pi * t[56:84] / 7).mean())
    assert np.sign(comps["seasonal"]["estimate"]) == np.sign(true_delta)
    # Without the seasonal term, its share of the gap would sit in unexplained.
    assert abs(node["unexplained"]) < abs(node["unexplained"] + comps["seasonal"]["estimate"])


def test_rca_reference_window_outside_fit_raises():
    """With a lag, the fitted period starts after the data start; a reference
    window reaching before it must raise a clear error."""
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
    data = generate_mock_data(n_days=100)

    with pytest.raises(ValueError, match="inside the fitted period"):
        run_rca(parser.dag, data, {}, "order_count",
                "2024-01-01", "2024-02-15", "2024-02-16", "2024-03-14",
                advi_draws=100)


# --- Per-day Shapley (T6) ---

def test_per_day_shapley_attributes_covariance_shift():
    """Marginal window means of orders and aov are identical in both windows,
    but their within-window correlation flips sign. Shapley on window means sees
    gap = 0; symmetric per-day Shapley attributes the covariance *delta*
    between the windows to the parents."""
    n = 60
    dates = pd.date_range("2024-01-01", periods=n)
    f = np.tile([5.0, -5.0], n // 2)             # zero-mean shared factor
    orders = 100.0 + f
    aov = np.where(np.arange(n) < 30, 50.0 + 0.5 * f, 50.0 - 0.5 * f)
    revenue = orders * aov                        # exact identity
    data = pd.DataFrame({
        "date": dates,
        "order_count": orders,
        "average_order_value": aov,
        "revenue": revenue,
    })
    yaml_content = """
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
    parser = Parser(yaml_content)

    result = shapley_attribution(
        parser.dag, data, "revenue",
        "2024-01-01", "2024-01-30", "2024-01-31", "2024-02-29",
    )

    gap = result["actual"] - result["baseline"]
    # cov flips from +12.5 to -12.5: both windows are evaluated per-day, so
    # the gap is the full covariance delta, split evenly for a product.
    assert abs(gap - (-25.0)) < 1e-9
    assert abs(sum(result["attribution"].values()) - gap) < 1e-6
    for phi in result["attribution"].values():
        assert abs(phi - (-12.5)) < 1e-9


# --- Window bootstrap (T7) ---

def test_block_bootstrap_indices_contract():
    rng = np.random.default_rng(0)
    idx = _block_bootstrap_indices(20, 50, rng)

    assert idx.shape == (50, 20)
    assert idx.min() >= 0 and idx.max() < 20
    # Deterministic given the seed.
    idx2 = _block_bootstrap_indices(20, 50, np.random.default_rng(0))
    np.testing.assert_array_equal(idx, idx2)


def test_block_bootstrap_indices_short_window():
    """n < block degenerates gracefully: valid shape/range, and the resampled
    window means still vary (the bootstrap must not collapse to rotations)."""
    rng = np.random.default_rng(0)
    idx = _block_bootstrap_indices(3, 100, rng)

    assert idx.shape == (100, 3)
    assert idx.min() >= 0 and idx.max() < 3
    vals = np.array([10.0, 20.0, 60.0])
    assert np.unique(vals[idx].mean(axis=1)).size > 1


def test_short_analysis_window_widens_ci():
    """The point of the bootstrap: a 3-day analysis window must yield a wider
    contribution CI than a 28-day one from the same fit."""
    rng = np.random.default_rng(11)
    n = 100
    x = 100.0 + rng.normal(0, 5.0, n)
    y = 0.5 * x + rng.normal(0, 1.0, n)
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
    traces = {}
    ref = ("2024-01-01", "2024-02-29")
    # Both analyses start 2024-03-01 -> same fit_end -> the same cached fit,
    # so the only difference is window-sampling uncertainty.
    r3 = run_rca(parser.dag, data, traces, "y", ref[0], ref[1],
                 "2024-03-01", "2024-03-03", advi_draws=300)
    r28 = run_rca(parser.dag, data, traces, "y", ref[0], ref[1],
                  "2024-03-01", "2024-03-28", advi_draws=300)

    def ci_width(result):
        ci = result["nodes"]["y"]["contributions"][0]["ci_95"]
        return ci[1] - ci[0]

    assert ci_width(r3) > ci_width(r28)


def test_rca_deterministic():
    """Two identical run_rca calls return identical contribution numbers."""
    dag, data = make_tree()
    traces = {}

    r1 = rca_on(dag, data, traces, "revenue")
    r2 = rca_on(dag, data, traces, "revenue")

    for node in r1["nodes"]:
        assert r1["nodes"][node]["contributions"] == r2["nodes"][node]["contributions"]
        assert r1["nodes"][node]["components"] == r2["nodes"][node]["components"]
        assert r1["nodes"][node]["unexplained"] == r2["nodes"][node]["unexplained"]


def test_rca_day_grain_golden_pinned():
    """The grain refactor must leave the day-grain path bit-for-bit
    identical: golden numbers captured before per-node grain landed. Only the
    formula node is pinned — its contributions come solely from the seeded
    bootstrap, independent of the ADVI posterior."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    rev = result["nodes"]["revenue"]
    assert rev["status"] == "ok"
    assert rev["grain"] == "day"
    assert rev["effective_windows"]["reference"] == {
        "start": "2024-01-01", "end": "2024-02-15", "n_periods": 46,
    }
    assert abs(rev["gap"] - 943.1485825183736) < 1e-9
    assert abs(rev["unexplained"] - (-8.603117931383167)) < 1e-9
    contribs = {c["parent"]: c["estimate"] for c in rev["contributions"]}
    assert abs(contribs["order_count"] - 715.6923261624328) < 1e-9
    assert abs(contribs["average_order_value"] - 239.8411235487748) < 1e-9

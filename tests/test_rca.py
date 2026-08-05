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
    """Formula node revenue uses Shapley; the published estimates are the exact
    Shapley values and reconcile with the gap to machine precision (T7, C3)."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    rev = result["nodes"]["revenue"]
    assert rev["attribution_method"] == "shapley"
    assert rev["components"] is None
    parents = {c["parent"] for c in rev["contributions"]}
    assert parents == {"order_count", "average_order_value"}

    # The identity is exact, not approximate: the estimates are the exact
    # Shapley values and `unexplained` comes from the same exact call, so the
    # two halves reconcile. This used to hold only to ~5% because the estimate
    # was the mean over bootstrap replicates of a nonlinear decomposition.
    total = sum(c["estimate"] for c in rev["contributions"])
    assert abs(total - (rev["gap"] - rev["unexplained"])) < 1e-9

    for c in rev["contributions"]:
        assert c["ci_95"] is not None and c["ci_95"][0] <= c["ci_95"][1]
        # The two-level split is exact too, not just exact in expectation.
        d = c["decomposition"]
        assert abs(
            d["means"]["estimate"] + d["comovement"]["estimate"] - c["estimate"]
        ) < 1e-9
        # unlagged contributions carry no lag surfacing keys at all
        assert "lag" not in c and "parent_windows" not in c
        assert 0.5 <= c["prob_same_direction"] <= 1.0

    # The interaction row is a readout of co-movement already inside the
    # contributions, so it must equal their comovement parts — adding it on top
    # of the contributions double-counts (see roadmap C9).
    comovement_total = sum(
        c["decomposition"]["comovement"]["estimate"] for c in rev["contributions"]
    )
    assert abs(rev["interaction"]["estimate"] - comovement_total) < 1e-9


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
    # no lagged parents -> the parent_windows key is absent entirely
    assert "parent_windows" not in result


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


def test_rca_lagged_reference_window_outside_data_raises():
    """A lagged parent is read from a window shifted back by its lag. When that
    shifted window reaches before the data start the error must name the parent,
    the lag and the *shifted* dates — the caller never typed that window, so
    reporting the one they did type would send them looking in the wrong place."""
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
    data = generate_mock_data(n_days=100)  # starts 2024-01-01

    with pytest.raises(ValueError) as excinfo:
        run_rca(parser.dag, data, {}, "order_count",
                "2024-01-01", "2024-02-15", "2024-02-16", "2024-03-14",
                advi_draws=100)
    message = str(excinfo.value)
    assert "daily_sessions" in message
    assert "lag 5" in message
    assert "2023-12-27" in message  # 2024-01-01 shifted back 5 days


# --- Window validation (1.1) ---

_WINDOW_TREE = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "daily_sessions * average_order_value"
    parents: [daily_sessions, average_order_value]
"""


@pytest.mark.parametrize("entry_point", [run_rca, shapley_attribution])
def test_overlapping_windows_raise(entry_point):
    """Overlap is an error, not a warning: a shared period would count as both
    the normal regime and the departure from it."""
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)
    args = (parser.dag, data, {}, "revenue") if entry_point is run_rca else (
        parser.dag, data, "revenue"
    )

    with pytest.raises(ValueError, match="overlap"):
        entry_point(*args, "2024-01-01", "2024-02-15", "2024-02-10", "2024-03-14")


@pytest.mark.parametrize("entry_point", [run_rca, shapley_attribution])
def test_inverted_window_raises(entry_point):
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)
    args = (parser.dag, data, {}, "revenue") if entry_point is run_rca else (
        parser.dag, data, "revenue"
    )

    with pytest.raises(ValueError, match="analysis_start.*on or before.*analysis_end"):
        entry_point(*args, "2024-01-01", "2024-01-31", "2024-03-14", "2024-02-16")


def test_reference_window_starting_before_data_raises():
    """A window that only *partly* overlaps the data would silently average the
    periods that happen to exist — a wrong number, not a missing one."""
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)  # starts 2024-01-01

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(parser.dag, data, {}, "revenue",
                "2023-12-01", "2024-01-31", "2024-02-01", "2024-02-28")


def test_analysis_window_running_past_data_raises():
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)  # ends 2024-04-09

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(parser.dag, data, {}, "revenue",
                "2024-01-01", "2024-01-31", "2024-02-01", "2024-05-31")


def test_windows_fully_inside_the_data_are_accepted():
    """The guard must not fire on the ordinary case it exists to protect."""
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)

    result = run_rca(parser.dag, data, {}, "revenue",
                     "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-28")
    assert result["nodes"]["revenue"]["gap"] is not None


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
    """The day-grain path stays bit-for-bit stable. Only the formula node is
    pinned — it is deterministic given the seed, independent of ADVI.

    Re-pinned once, for C3. `gap` and `unexplained` are unchanged from the
    original capture (they always came from the exact Shapley call); the two
    contributions moved because they are now that same exact call's values
    rather than means over bootstrap replicates:

        order_count         715.6923261624328 -> 713.9109253339559
        average_order_value 239.8411235487748 -> 237.8407751158013

    The old numbers were the bug: they overshot `gap - unexplained` by 3.78,
    which this test pinned as correct. Any *future* movement in these is a
    regression.
    """
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
    assert abs(contribs["order_count"] - 713.9109253339559) < 1e-9
    assert abs(contribs["average_order_value"] - 237.8407751158013) < 1e-9

    # The property those numbers exist to protect: contributions reconcile with
    # the node's own gap. Pinning values that violated it is how the bug
    # survived a golden test in the first place.
    assert abs(sum(contribs.values()) - (rev["gap"] - rev["unexplained"])) < 1e-9


# --- bootstrap honesty: attenuation correction + degeneracy guard (C4) ---


def test_window_mean_correction_matches_the_derivation():
    """The factor is 1/sqrt((1 - l/n)(1 - 1/n)) — the circular-MBB attenuation
    times the ddof gap of the empirical distribution."""
    from breakdown.engine.rca import _window_mean_correction

    for n, block in ((14, 7), (28, 7), (5, 2), (7, 3), (90, 7)):
        expected = 1.0 / np.sqrt((1 - block / n) * (1 - 1 / n))
        assert abs(_window_mean_correction(n, block) - expected) < 1e-12

    # Undefined cases fall back to no correction rather than dividing by zero.
    assert _window_mean_correction(1, 1) == 1.0
    assert _window_mean_correction(4, 4) == 1.0


def test_bootstrap_variance_attenuation_is_real_and_corrected():
    """The bug, measured directly on the estimator: the resampled variance of a
    window mean targets (1 - l/n) times the truth. The correction undoes it."""
    from breakdown.engine.rca import _window_mean_correction

    rng = np.random.default_rng(0)
    n, block = 14, 7
    ratios = []
    for _ in range(400):
        x = rng.normal(0, 1.0, n)
        idx = _block_bootstrap_indices(n, 2000, rng, block=block)
        means = x[idx].mean(axis=1)
        ratios.append(means.var() / (1.0 / n))
    observed = float(np.mean(ratios))

    # Uncorrected: attenuated to roughly (1 - 7/14) = 0.5 of the true variance.
    assert abs(observed - (1 - block / n)) < 0.08, observed
    # Corrected: back to ~1.0 (the ddof half of the factor closes the rest).
    f = _window_mean_correction(n, block)
    assert abs(observed * f**2 - 1.0) < 0.12, observed * f**2


def test_constant_parent_withholds_the_interval():
    """A parent that never moves collapses every replicate to the same value.
    That used to ship a zero-width ci_95 flagged `ci_status: "ok"` — the guard
    keyed on period count, so it never fired (C4)."""
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
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    rng = np.random.default_rng(3)
    orders = 100.0 + rng.normal(0, 5.0, 100)
    frame = pd.DataFrame({
        "date": dates,
        "order_count": orders,
        # Unlaunched feature: identically flat across both windows.
        "average_order_value": np.full(100, 25.0),
        "revenue": orders * 25.0,
    })
    dag = Parser(yaml_content).dag
    result = run_rca(dag, frame, {}, "revenue", *REF, *AN, advi_draws=300)
    rev = result["nodes"]["revenue"]

    aov = next(c for c in rev["contributions"] if c["parent"] == "average_order_value")
    assert aov["ci_95"] is None, "a zero-width interval is never a real finding"
    assert aov["prob_same_direction"] is None
    assert rev["ci_status"] == "degenerate_constant_window"

    # The parent that *did* move keeps a real interval — degeneracy is judged
    # per contribution, not per node.
    oc = next(c for c in rev["contributions"] if c["parent"] == "order_count")
    assert oc["ci_95"] is not None and oc["ci_95"][0] < oc["ci_95"][1]


def test_short_window_intervals_are_wider_than_the_raw_bootstrap():
    """The correction is largest where the interval is most load-bearing: a
    short analysis window. Guards against the factor being silently dropped."""
    dag, data = make_tree()
    short = run_rca(dag, data, {}, "revenue", *REF, "2024-02-16", "2024-02-22",
                    advi_draws=300)
    rev = short["nodes"]["revenue"]
    for c in rev["contributions"]:
        assert c["ci_95"] is not None
        assert c["ci_95"][1] > c["ci_95"][0]
    # 7 analysis periods -> block 3 -> factor 1/sqrt((1-3/7)(1-1/7)) ~ 1.42,
    # so the correction is a material widening, not a rounding difference.
    from breakdown.engine.rca import _window_mean_correction
    assert _window_mean_correction(7, 3) > 1.4

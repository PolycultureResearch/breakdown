import json

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.rca import (
    NonFiniteAttribution,
    _hop_weights,
    _rank_causes,
    run_rca,
    shapley_attribution,
)
from breakdown.engine.stats import block_bootstrap_indices, effective_block, share_of_gap
from breakdown.grains import BOOT_BLOCK
from breakdown.parser import Parser
from tests.synthetic import generate_mock_data, win

# Grill L10: this module fits real samplers; `-m "not slow"` is the fast loop.
pytestmark = pytest.mark.slow

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
    return run_rca(dag, data, traces, target, **win(REF, AN), draws=300)


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
        assert abs(d["means"]["estimate"] + d["comovement"]["estimate"] - c["estimate"]) < 1e-9
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
    for term in comps.values():
        assert term["ci_95"][0] <= term["estimate"] <= term["ci_95"][1]
    # order_count declares no seasonality, so the model contains no seasonal
    # term and the key is absent. It used to report `{estimate: 0.0, ci_95:
    # [0.0, 0.0]}` — a 95% credible interval of zero width on a parameter that
    # was never estimated, which is C4's defect class through another door.
    # "There is no such term" is not the same statement as "we could not
    # estimate this", so it is not a `ci_status` either.
    assert set(comps) == {"trend"}
    assert oc["ci_status"] == "ok"


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

    result = shapley_attribution(dag, data, "revenue", **win(REF, AN))

    assert set(result["attribution"].keys()) == {"order_count", "average_order_value"}
    assert abs(result["gap"] - (result["actual"] - result["baseline"])) < 1e-3
    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-3
    # no lagged parents -> the parent_windows key is absent entirely
    assert "parent_windows" not in result


def test_shapley_attribution_no_formula_raises():
    dag, data = make_tree()

    with pytest.raises(ValueError, match="no formula"):
        shapley_attribution(dag, data, "order_count", **win(REF, AN))


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
    ref = (str(dates[56].date()), str(dates[83].date()))  # 4 whole weeks
    an = (str(dates[84].date()), str(dates[93].date()))  # 10 days, weekday-skewed

    result = run_rca(parser.dag, data, {}, "y", **win(ref, an), draws=300)

    node = result["nodes"]["y"]
    comps = node["components"]
    true_delta = 5.0 * (
        np.sin(2 * np.pi * t[84:94] / 7).mean() - np.sin(2 * np.pi * t[56:84] / 7).mean()
    )
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
        run_rca(
            parser.dag,
            data,
            {},
            "order_count",
            **win(("2024-01-01", "2024-02-15"), ("2024-02-16", "2024-03-14")),
            draws=100,
        )
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
    args = (
        (parser.dag, data, {}, "revenue")
        if entry_point is run_rca
        else (parser.dag, data, "revenue")
    )

    with pytest.raises(ValueError, match="overlap"):
        entry_point(*args, **win(("2024-01-01", "2024-02-15"), ("2024-02-10", "2024-03-14")))


@pytest.mark.parametrize("entry_point", [run_rca, shapley_attribution])
def test_inverted_window_raises(entry_point):
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)
    args = (
        (parser.dag, data, {}, "revenue")
        if entry_point is run_rca
        else (parser.dag, data, "revenue")
    )

    with pytest.raises(ValueError, match="analysis_start.*on or before.*analysis_end"):
        entry_point(*args, **win(("2024-01-01", "2024-01-31"), ("2024-03-14", "2024-02-16")))


def test_reference_window_starting_before_data_raises():
    """A window that only *partly* overlaps the data would silently average the
    periods that happen to exist — a wrong number, not a missing one."""
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)  # starts 2024-01-01

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(
            parser.dag,
            data,
            {},
            "revenue",
            **win(("2023-12-01", "2024-01-31"), ("2024-02-01", "2024-02-28")),
        )


def test_analysis_window_running_past_data_raises():
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)  # ends 2024-04-09

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(
            parser.dag,
            data,
            {},
            "revenue",
            **win(("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-05-31")),
        )


def test_windows_fully_inside_the_data_are_accepted():
    """The guard must not fire on the ordinary case it exists to protect."""
    parser = Parser(_WINDOW_TREE)
    data = generate_mock_data(n_days=100)

    result = run_rca(
        parser.dag,
        data,
        {},
        "revenue",
        **win(("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-28")),
    )
    assert result["nodes"]["revenue"]["gap"] is not None


# --- Per-day Shapley (T6) ---


def test_per_day_shapley_attributes_covariance_shift():
    """Marginal window means of orders and aov are identical in both windows,
    but their within-window correlation flips sign. Shapley on window means sees
    gap = 0; symmetric per-day Shapley attributes the covariance *delta*
    between the windows to the parents."""
    n = 60
    dates = pd.date_range("2024-01-01", periods=n)
    f = np.tile([5.0, -5.0], n // 2)  # zero-mean shared factor
    orders = 100.0 + f
    aov = np.where(np.arange(n) < 30, 50.0 + 0.5 * f, 50.0 - 0.5 * f)
    revenue = orders * aov  # exact identity
    data = pd.DataFrame(
        {
            "date": dates,
            "order_count": orders,
            "average_order_value": aov,
            "revenue": revenue,
        }
    )
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
        parser.dag,
        data,
        "revenue",
        **win(("2024-01-01", "2024-01-30"), ("2024-01-31", "2024-02-29")),
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
    idx = block_bootstrap_indices(20, 50, rng)

    assert idx.shape == (50, 20)
    assert idx.min() >= 0 and idx.max() < 20
    # Deterministic given the seed.
    idx2 = block_bootstrap_indices(20, 50, np.random.default_rng(0))
    np.testing.assert_array_equal(idx, idx2)


def test_block_bootstrap_indices_short_window():
    """n < block degenerates gracefully: valid shape/range, and the resampled
    window means still vary (the bootstrap must not collapse to rotations)."""
    rng = np.random.default_rng(0)
    idx = block_bootstrap_indices(3, 100, rng)

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
    r3 = run_rca(parser.dag, data, traces, "y", **win(ref, ("2024-03-01", "2024-03-03")), draws=300)
    r28 = run_rca(
        parser.dag, data, traces, "y", **win(ref, ("2024-03-01", "2024-03-28")), draws=300
    )

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
        "start": "2024-01-01",
        "end": "2024-02-15",
        "n_periods": 46,
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


# --- Default reference window (the matched adjacent block) ---


def test_rca_defaults_reference_to_matched_adjacent_block():
    """Omitting both reference dates uses the matched adjacent block; the
    response echoes the resolved window and flags it as defaulted. The 54-day
    analysis wants a 216-day reference, so it clamps to the loaded data."""
    dag, data = make_tree()

    result = run_rca(dag, data, {}, "revenue", analysis_start=AN[0], analysis_end=AN[1], draws=300)

    assert result["reference_defaulted"] is True
    # Adjacent (ends the day before the analysis window), clamped to the
    # start of the loaded data.
    assert result["reference_window"] == {"start": "2024-01-01", "end": "2024-02-15"}
    assert result["nodes"]["revenue"]["gap"] is not None


def test_rca_explicit_reference_is_unchanged_and_not_flagged():
    dag, data = make_tree()

    defaulted = run_rca(
        dag, data, {}, "revenue", analysis_start=AN[0], analysis_end=AN[1], draws=300
    )
    explicit = run_rca(
        dag,
        data,
        {},
        "revenue",
        **win((defaulted["reference_window"]["start"], defaulted["reference_window"]["end"]), AN),
        draws=300,
    )

    assert explicit["reference_defaulted"] is False
    assert explicit["reference_window"] == defaulted["reference_window"]
    assert explicit["nodes"]["revenue"]["gap"] == defaulted["nodes"]["revenue"]["gap"]


def test_rca_single_reference_date_raises():
    dag, data = make_tree()
    with pytest.raises(ValueError, match="both reference_start and reference_end"):
        run_rca(
            dag,
            data,
            {},
            "revenue",
            analysis_start=AN[0],
            analysis_end=AN[1],
            reference_start=REF[0],
        )


def test_rca_analysis_at_data_start_raises():
    dag, data = make_tree()
    with pytest.raises(ValueError, match="beginning of the loaded data"):
        run_rca(dag, data, {}, "revenue", analysis_start="2024-01-01", analysis_end="2024-01-14")


def test_shapley_defaults_reference_and_echoes_windows():
    dag, data = make_tree()

    result = shapley_attribution(dag, data, "revenue", analysis_start=AN[0], analysis_end=AN[1])

    assert result["reference_defaulted"] is True
    assert result["reference_window"]["end"] == "2024-02-15"
    assert result["analysis_window"] == {"start": AN[0], "end": AN[1]}
    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-3


# --- Fit-window provenance and seasonality warnings (1.10) ---


def test_rca_surfaces_fit_window_and_seasonality_warnings():
    """Posterior nodes report what the model actually trained on (all loaded
    history before analysis_start) and any seasonality-identifiability
    warnings; formula and root nodes carry neither."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    seasonality:
      - period: 60
        name: bimonthly
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=100)

    result = run_rca(parser.dag, data, {}, "revenue", **win(REF, AN), draws=300)

    oc = result["nodes"]["order_count"]
    # Fit = whole days from data start to the day before the analysis window,
    # regardless of the reference dates.
    assert oc["fit_window"] == {
        "start": "2024-01-01",
        "end": "2024-02-15",
        "n_periods": 46,
    }
    # 46 fitted days < 2 x 60: the declared seasonality is unidentifiable
    # and the warning must reach the RCA response, not just the log.
    assert oc["seasonality_warnings"]
    assert any("bimonthly" in w or "60" in w for w in oc["seasonality_warnings"])

    assert result["nodes"]["revenue"]["fit_window"] is None
    assert result["nodes"]["daily_sessions"]["fit_window"] is None
    assert result["nodes"]["revenue"]["seasonality_warnings"] is None


# --- Degrading instead of dying: zero denominators, unfittable nodes,
# --- out-of-range windows (H1, M3, M7)

_ZERO_DENOMINATOR_YAML = """
metrics:
  - name: sessions
    source: dbt.metric.sessions
  - name: order_count
    source: dbt.metric.order_count
  - name: revenue
    source: dbt.metric.revenue
  - name: aov
    source: dbt.metric.aov
    formula: "revenue / order_count"
    parents: [revenue, order_count]
  - name: revenue_per_session
    source: dbt.metric.revenue_per_session
    formula: "aov * sessions"
    parents: [aov, sessions]
"""

_ZD_REF = ("2024-01-01", "2024-02-15")
_ZD_AN = ("2024-02-16", "2024-03-14")
_ZD_ZERO_DAY = "2024-02-20"  # inside the analysis window


def _zero_denominator_data(n_days: int = 100) -> pd.DataFrame:
    """One structural zero in a flow denominator, exactly as `_align_to_spine`
    manufactures it when the source has an interior gap."""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2024-01-01", periods=n_days)
    order_count = 100.0 + rng.normal(0, 5.0, n_days)
    revenue = 50.0 * order_count + rng.normal(0, 50.0, n_days)
    sessions = 1000.0 + rng.normal(0, 20.0, n_days)
    df = pd.DataFrame(
        {
            "date": dates,
            "sessions": sessions,
            "order_count": order_count,
            "revenue": revenue,
        }
    )
    i = df.index[df["date"] == pd.Timestamp(_ZD_ZERO_DAY)][0]
    df.loc[i, "order_count"] = 0.0
    df["aov"] = np.where(
        df["order_count"] == 0.0, 0.0, df["revenue"] / df["order_count"].replace(0.0, 1.0)
    )
    df["revenue_per_session"] = df["aov"] * df["sessions"]
    return df


def test_zero_denominator_does_not_crash_and_names_the_parent():
    """A zero denominator used to raise `KeyError: '__import__'` out of the
    restricted `eval` — an unhandled 500. It now evaluates to inf, and the
    decomposition refuses the non-finite result with a diagnostic naming the
    parent series and the dates (a 422 at the API), rather than emitting NaNs
    that Starlette's `allow_nan=False` encoder cannot serialize."""
    parser = Parser(_ZERO_DENOMINATOR_YAML)
    data = _zero_denominator_data()

    with pytest.raises(NonFiniteAttribution) as excinfo:
        shapley_attribution(parser.dag, data, "aov", **win(_ZD_REF, _ZD_AN))

    message = str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)  # the API turns this into a 422
    assert "order_count" in message
    assert _ZD_ZERO_DAY in message
    assert "analysis-window" in message


def test_zero_denominator_node_degrades_and_the_rest_of_the_tree_reports():
    """The failing node carries a status and its own numbers; every other node
    is still attributed, and no score anywhere is non-finite."""
    parser = Parser(_ZERO_DENOMINATOR_YAML)
    data = _zero_denominator_data()

    result = run_rca(parser.dag, data, {}, "revenue_per_session", **win(_ZD_REF, _ZD_AN))

    aov = result["nodes"]["aov"]
    assert aov["status"] == "attribution_failed"
    assert "order_count" in aov["status_reason"] and _ZD_ZERO_DAY in aov["status_reason"]
    # Its own movement is read off the data, not the model, so it still reports.
    assert aov["baseline"] is not None and aov["gap"] is not None
    assert aov["contributions"] == []

    rps = result["nodes"]["revenue_per_session"]
    assert rps["status"] == "ok"
    assert {c["parent"] for c in rps["contributions"]} == {"aov", "sessions"}
    assert all(result["nodes"][n]["status"] == "ok" for n in ("sessions", "revenue", "order_count"))

    assert all(np.isfinite(r["score"]) for r in result["ranked_causes"])
    # The property the encoder cares about: nothing non-finite anywhere.
    json.dumps(result, allow_nan=False)


def test_zero_denominator_on_the_target_itself_raises():
    """The target's decomposition is the whole point of the request — an empty
    status there is no answer, so it raises (422) with the diagnostic."""
    parser = Parser(_ZERO_DENOMINATOR_YAML)
    data = _zero_denominator_data()

    with pytest.raises(NonFiniteAttribution, match="order_count"):
        run_rca(parser.dag, data, {}, "aov", **win(_ZD_REF, _ZD_AN))


def test_rank_causes_ignores_a_non_finite_share():
    """`min(abs(nan), 1.0)` is nan — every comparison against nan is false, so
    the clamp let it through and one node's NaN share poisoned the score of
    every ancestor above it. An undefined share weighs nothing."""
    parser = Parser(JAFFLE_YAML)
    scope = {"revenue", "order_count", "average_order_value", "daily_sessions"}
    nodes_out = {
        "revenue": {
            "contributions": [
                {"parent": "order_count", "share_of_gap": float("nan")},
                {"parent": "average_order_value", "share_of_gap": 0.4},
            ]
        },
        "order_count": {"contributions": [{"parent": "daily_sessions", "share_of_gap": 0.5}]},
        "average_order_value": {"contributions": []},
        "daily_sessions": {"contributions": []},
    }

    ranked = _rank_causes(parser.dag, "revenue", scope, nodes_out)

    assert all(np.isfinite(r["score"]) for r in ranked)
    scores = {r["metric"]: r["score"] for r in ranked}
    assert scores["order_count"] == 0.0  # nan share -> no evidence -> no weight
    assert scores["daily_sessions"] == 0.0  # and nothing propagates above it
    assert scores["average_order_value"] == pytest.approx(0.4)


_ZERO_VARIANCE_YAML = """
metrics:
  - name: leads
    source: dbt.metric.leads
  - name: signups
    source: dbt.metric.signups
    parents: [leads]
  - name: price
    source: dbt.metric.price
  - name: revenue
    source: dbt.metric.revenue
    formula: "signups * price"
    parents: [signups, price]
"""


def test_zero_variance_parent_leaves_the_rest_of_the_tree_intact():
    """A parent held at zero for the whole fit window cannot be normalized, so
    `fit_metric` raises. That used to abort the entire RCA and return nothing;
    the node is now reported with a `fit_failed` status and everything else is
    still attributed."""
    rng = np.random.default_rng(5)
    n = 100
    dates = pd.date_range("2024-01-01", periods=n)
    data = pd.DataFrame(
        {
            "date": dates,
            "leads": np.zeros(n),  # the seasonal business's default state
            "signups": 40.0 + rng.normal(0, 3.0, n),
            "price": 20.0 + rng.normal(0, 1.0, n),
        }
    )
    data["revenue"] = data["signups"] * data["price"]
    parser = Parser(_ZERO_VARIANCE_YAML)
    traces = {}

    result = run_rca(parser.dag, data, traces, "revenue", **win(REF, AN), draws=300)

    signups = result["nodes"]["signups"]
    assert signups["status"] == "fit_failed"
    assert "zero variance" in signups["status_reason"]
    assert signups["gap"] is not None and signups["contributions"] == []
    assert ("signups", AN[0]) not in traces

    assert result["nodes"]["revenue"]["status"] == "ok"
    assert len(result["nodes"]["revenue"]["contributions"]) == 2
    assert result["nodes"]["leads"]["status"] == "ok"
    assert result["nodes"]["price"]["status"] == "ok"
    json.dumps(result, allow_nan=False)


def test_out_of_range_window_is_refused_before_any_fit():
    """Coverage is validated for the whole scope before the fit loop: an
    out-of-range window used to pay for an ADVI fit of every ancestor —
    minutes, holding the caller's lock, leaving a cached trace each — and only
    then 422. The empty trace cache is the observable property."""
    dag, data = make_tree()  # data ends 2024-04-09
    traces = {}

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(
            dag,
            data,
            traces,
            "revenue",
            **win(("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-05-31")),
            draws=300,
        )

    assert traces == {}


# --- C4(b): the block length may not eat the window -------------------------


def _iid_variance_ratio(n: int, n_boot: int = 20000, seed: int = 0) -> float:
    """Resampled variance of the window mean over its true sampling variance.

    Computed from the resampling scheme itself rather than by simulating
    series, which removes the realization noise that otherwise swamps this at
    small n. Writing a replicate's per-position draw counts as `c`, its mean is
    `c.x / n`; for iid `x` with variance s^2 that gives an expected resampled
    variance of `s^2 (E||c||^2 - ||E c||^2) / n^2`, and the circular scheme
    draws every position equally often in expectation, so `E c` is all ones.
    Against a true sampling variance of `s^2 / n`, the ratio is
    `(E||c||^2 - n) / n` — no `x` needed. It reproduces the known values as a
    check: `block = 1` (the ordinary bootstrap) gives exactly `1 - 1/n`, and a
    block covering the whole window gives 0.
    """
    rng = np.random.default_rng(seed)
    idx = block_bootstrap_indices(n, n_boot, rng, block=BOOT_BLOCK["day"])
    flat = (np.arange(n_boot)[:, None] * n + idx).reshape(-1)
    counts = np.bincount(flat, minlength=n_boot * n).reshape(n_boot, n)
    return float(((counts.astype(float) ** 2).sum(axis=1).mean() - n) / n)


def test_block_length_is_capped_at_a_quarter_of_the_window():
    """The cap is a quarter, and the block never shrinks as the window grows.

    The old cap was `n // 2`, which put the block on the midpoint of the very
    degeneracy curve it was reasoning about — and made the block *fall* when the
    window grew by a period (n=13 -> 6, n=14 -> 7 gave a wider interval at 13
    than at 14). A user widening their window by a day could get a narrower
    interval; monotonicity of the rule is what rules that out.
    """
    ns = range(1, 61)
    blocks = [effective_block(n, BOOT_BLOCK["day"]) for n in ns]

    assert all(b >= 1 for b in blocks)
    assert all(b == 1 or 4 * b <= n for n, b in zip(ns, blocks))
    assert blocks == sorted(blocks), "the block must never shrink as the window grows"
    # The two windows this is quoted on: the README's fortnight, and the
    # default reference floor.
    assert effective_block(14, 7) == 3
    assert effective_block(28, 7) == 7


def test_short_window_variance_is_not_halved_by_the_block_cap():
    """The property the cap exists to protect: on iid data the resampled
    variance of a window mean stays close to the truth at every window length.

    Under the old `n // 2` cap this ratio was ~0.5 for every n <= 15 —
    intervals ~30% too narrow, worst exactly on the short windows the bootstrap
    exists to be honest about — and it was not monotone in n: 0.54 at n=13 and
    0.50 at n=14, so widening the window by a day narrowed the interval. Both
    assertions below fail on that cap.
    """
    ratios = {n: _iid_variance_ratio(n) for n in range(4, 41)}

    worst = min(ratios.items(), key=lambda kv: kv[1])
    assert worst[1] > 0.72, f"window mean variance attenuated to {worst[1]:.2f} at n={worst[0]}"
    # The specific pair the old cap inverted: one more period of data must not
    # buy a narrower interval.
    assert ratios[14] >= ratios[13] - 0.02


# --- C4(a): a degenerate resampling yields no interval, not a narrow one ----


_CONSTANT_PARENT_YAML = """
metrics:
  - name: intensity
    source: dbt.metric.intensity
  - name: base
    source: dbt.metric.base
  - name: total
    source: dbt.metric.total
    formula: "base * intensity"
    parents: [base, intensity]
"""

_C4_REF = ("2024-03-01", "2024-03-31")
_C4_AN = ("2024-04-01", "2024-04-29")


def _constant_parent_data(constant=3.0):
    rng = np.random.default_rng(5)
    dates = pd.date_range("2024-01-01", periods=120)
    base = 100.0 + rng.normal(0, 5.0, len(dates))
    base[dates >= pd.Timestamp("2024-04-01")] += 20.0
    intensity = np.full(len(dates), constant)
    return pd.DataFrame(
        {"date": dates, "intensity": intensity, "base": base, "total": base * intensity}
    )


def test_constant_parent_window_yields_no_interval_rather_than_a_zero_width_one():
    """A parent that never moves collapses every replicate to the same value.

    That used to ship as `ci_95: [0.0, 0.0]` with `ci_status: "ok"` and
    `prob_same_direction: 0.0` — the engine's most-quoted number reporting
    perfect precision from no information at all, on the state a seasonal
    business spends months in. It is withheld now, and the node says why.
    """
    dag = Parser(_CONSTANT_PARENT_YAML).dag
    data = _constant_parent_data()

    result = run_rca(dag, data, {}, "total", **win(_C4_REF, _C4_AN))

    node = result["nodes"]["total"]
    assert node["status"] == "ok"  # the node still reports its own movement
    assert node["ci_status"] == "degenerate_bootstrap_spread"
    by_parent = {c["parent"]: c for c in node["contributions"]}
    held = by_parent["intensity"]
    assert held["ci_95"] is None
    assert held["prob_same_direction"] is None
    # The parent that did move keeps a real interval — the guard is per
    # quantity, not a blanket refusal of the node.
    assert by_parent["base"]["ci_95"][0] < by_parent["base"]["ci_95"][1]

    # The property the pinned values above exist to protect: nothing anywhere
    # in the response ships an interval of zero width.
    for name, out in result["nodes"].items():
        for c in out["contributions"]:
            ci = c["ci_95"]
            assert ci is None or ci[1] > ci[0], f"zero-width ci_95 on {name} -> {c['parent']}"
    json.dumps(result, allow_nan=False)


def test_a_parent_held_at_zero_is_the_same_case():
    """The shape C4 was confirmed on in production: the held parent is zero
    rather than merely constant, so the whole window's product collapses."""
    dag = Parser(_CONSTANT_PARENT_YAML).dag
    data = _constant_parent_data(constant=0.0)

    result = run_rca(dag, data, {}, "total", **win(_C4_REF, _C4_AN))

    node = result["nodes"]["total"]
    assert node["ci_status"] == "degenerate_bootstrap_spread"
    assert all(c["ci_95"] is None for c in node["contributions"])


_ADDITIVE_YAML = """
metrics:
  - name: left
    source: dbt.metric.left
  - name: right
    source: dbt.metric.right
  - name: total
    source: dbt.metric.total
    formula: "left + right"
    parents: [left, right]
"""


def test_a_structurally_zero_term_is_not_mistaken_for_a_degenerate_one():
    """An additive identity has no co-movement term: it is exactly zero for
    every replicate whatever the data.

    Two statements have to stay apart here. The term carries **no interval**,
    like everything else that would otherwise publish a zero-width one. But the
    node is **not flagged**: nothing about its resampling collapsed, and keying
    the status on the published widths would raise a degeneracy on every
    additive node in every tree.
    """
    rng = np.random.default_rng(7)
    dates = pd.date_range("2024-01-01", periods=120)
    left = 10.0 + rng.normal(0, 0.5, len(dates))
    right = 100.0 + rng.normal(0, 2.0, len(dates))
    right[dates >= pd.Timestamp("2024-04-01")] += 5.0
    data = pd.DataFrame({"date": dates, "left": left, "right": right, "total": left + right})

    result = run_rca(Parser(_ADDITIVE_YAML).dag, data, {}, "total", **win(_C4_REF, _C4_AN))

    node = result["nodes"]["total"]
    assert node["ci_status"] == "ok"
    assert all(c["ci_95"] is not None for c in node["contributions"])
    comovement = node["contributions"][0]["decomposition"]["comovement"]
    assert comovement["estimate"] == pytest.approx(0.0, abs=1e-9)
    assert comovement["ci_95"] is None


def test_posterior_node_withholds_an_interval_on_an_unmoving_parent():
    """The same degeneracy reaches the posterior path through the parent's
    window means: `beta_raw x 0` is a zero-width interval on an identically
    zero contribution, however wide the coefficient posterior is.

    The parent varies over the fit window (so the fit itself succeeds) and is
    held flat across both comparison windows.
    """
    rng = np.random.default_rng(9)
    dates = pd.date_range("2024-01-01", periods=120)
    x = 100.0 + rng.normal(0, 5.0, len(dates))
    flat = ((dates >= pd.Timestamp(_C4_REF[0])) & (dates <= pd.Timestamp(_C4_REF[1]))) | (
        (dates >= pd.Timestamp(_C4_AN[0])) & (dates <= pd.Timestamp(_C4_AN[1]))
    )
    x[flat] = 100.0
    y = 0.5 * x + rng.normal(0, 1.0, len(dates))
    data = pd.DataFrame({"date": dates, "x": x, "y": y})
    yaml_content = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""

    result = run_rca(Parser(yaml_content).dag, data, {}, "y", **win(_C4_REF, _C4_AN), draws=300)

    node = result["nodes"]["y"]
    assert node["ci_status"] == "degenerate_bootstrap_spread"
    c = node["contributions"][0]
    assert c["estimate"] == 0.0
    assert c["ci_95"] is None and c["prob_same_direction"] is None


# --- C5: shares above the gap are penalized, not saturated ------------------


def test_share_of_gap_is_withheld_relative_to_the_node_scale():
    """The guard was `abs(gap) < 1e-12`, absolute — so float residue on a node
    denominated in millions published shares in the thousands, while a real
    move on a node denominated in rates was thrown away."""
    # Float residue at the scale of a large node: no share.
    assert share_of_gap(5.0, 1e-4, 1e9) is None
    # A real move on a tiny node: a share, where the absolute guard withheld it.
    assert share_of_gap(5e-14, 1e-13, 1e-9) == pytest.approx(0.5)
    # An ordinary gap is untouched, and a dead-flat node still has no share.
    assert share_of_gap(984.5, 595.5, 26386.5) == pytest.approx(1.6533, rel=1e-3)
    assert share_of_gap(0.0, 0.0, 0.0) is None


def test_hop_weight_penalizes_a_share_above_the_gap():
    """A parent explaining 165% of its child's gap is *less* well identified
    than one explaining a clean 80%: it needs 65% of cancellation from
    somewhere else. It used to score 1.0 — the maximum — for it."""

    def weight(share):
        return _hop_weights([{"parent": "p", "share_of_gap": share}])["p"]

    assert weight(1.0) == pytest.approx(1.0)  # a clean explanation still scores 1
    assert weight(0.8) == pytest.approx(0.8)  # under-explaining is unchanged
    assert weight(1.653) == pytest.approx(1 / 1.653)  # the bundled demo's own share
    assert weight(1.653) < weight(0.8)
    assert weight(5e5) < 1e-4  # offsetting noise carries no influence
    assert weight(-1.653) == weight(1.653)  # direction is not the question here
    assert weight(None) == 0.0 and weight(float("nan")) == 0.0

    # Within one node the ordering by |share| is untouched: only how much of
    # the child's score survives the hop changes.
    weights = _hop_weights(
        [
            {"parent": "big", "share_of_gap": 1.653},
            {"parent": "small", "share_of_gap": -0.616},
        ]
    )
    assert weights["big"] > weights["small"]
    # ...and a hop can no longer inflate: the parents of one node share out at
    # most the child's own score, where they could previously each take all of
    # it.
    assert sum(weights.values()) <= 1.0


_ACCUMULATION_YAML = """
metrics:
  - name: noisy_driver
    source: dbt.metric.noisy_driver
  - name: offset_a
    source: dbt.metric.offset_a
  - name: offset_b
    source: dbt.metric.offset_b
  - name: offset_c
    source: dbt.metric.offset_c
  - name: clean_driver
    source: dbt.metric.clean_driver
  - name: child_a
    source: dbt.metric.child_a
    formula: "noisy_driver + offset_a"
    parents: [noisy_driver, offset_a]
  - name: child_b
    source: dbt.metric.child_b
    formula: "noisy_driver + offset_b"
    parents: [noisy_driver, offset_b]
  - name: child_c
    source: dbt.metric.child_c
    formula: "noisy_driver + offset_c"
    parents: [noisy_driver, offset_c]
  - name: top
    source: dbt.metric.top
    formula: "child_a + child_b + child_c + clean_driver"
    parents: [child_a, child_b, child_c, clean_driver]
"""


def test_offsetting_noise_scores_below_a_clean_explainer():
    """roadmap C5's accumulation case, with the row's own numbers.

    `noisy_driver` is a parent of three children whose own gaps are small
    residues of two large opposing parent moves, so its share in each is
    enormous. Clamped to 1.0, those hops handed it its children's scores in
    full — 0.33 + 0.33 + 0.34 = 1.0, an *exact tie* with `clean_driver`, which
    explains 100% of the target's gap on its own. Scores accumulate across
    children, so a well-connected node needs only a few quiet ones to top the
    ranking on pure offsetting noise.

    The tie must now break toward the clean explainer, and by a wide margin.
    """
    dag = Parser(_ACCUMULATION_YAML).dag
    scope = set(dag.nodes)

    def node(*shares):
        return {"contributions": [{"parent": p, "share_of_gap": s} for p, s in shares]}

    nodes_out = {
        "top": node(("child_a", 0.33), ("child_b", 0.33), ("child_c", 0.34), ("clean_driver", 1.0)),
        "child_a": node(("noisy_driver", 500.0), ("offset_a", -499.0)),
        "child_b": node(("noisy_driver", 500.0), ("offset_b", -499.0)),
        "child_c": node(("noisy_driver", 500.0), ("offset_c", -499.0)),
        **{
            n: {"contributions": []}
            for n in scope
            if n.endswith("driver") or n.startswith("offset")
        },
    }

    ranked = _rank_causes(dag, "top", scope, nodes_out)
    scores = {r["metric"]: r["score"] for r in ranked}

    assert scores["clean_driver"] > scores["noisy_driver"]
    assert scores["noisy_driver"] < 0.01
    assert ranked[0]["metric"] == "clean_driver"
    # The old rule scored both at exactly 1.0. Separating them must not have
    # dragged the clean explainer down into the noise: it still leads every
    # offsetting node by orders of magnitude.
    offsetting = [scores[n] for n in scores if n.startswith("offset") or n == "noisy_driver"]
    assert scores["clean_driver"] > 10 * max(offsetting)


def test_ranked_causes_no_longer_saturates_on_an_ordinary_window():
    """The everyday shape this was found on, on the jaffle tree over two
    ordinary fortnights: `revenue`'s parents pull in opposite directions
    (+146% / -51%, the same shape as the bundled demo's +165% / -62%), which is
    exactly what the unclamped `share_of_gap` exists to express — and the clamp
    reported the leader's influence as exactly **1.0**, reading as certainty at
    the moment the split was least settled.

    Nothing about this window is pathological: the gap is -442 on a baseline of
    ~7,900. The leader is unchanged; the false certainty is gone."""
    dag, data = make_tree()
    result = run_rca(
        dag, data, {}, "revenue", **win(("2024-02-26", "2024-03-10"), ("2024-03-11", "2024-03-24"))
    )

    shares = {c["parent"]: c["share_of_gap"] for c in result["nodes"]["revenue"]["contributions"]}
    assert shares["order_count"] > 1.0, "this window no longer cancels"
    assert shares["average_order_value"] < 0.0
    ranked = result["ranked_causes"]
    assert ranked[0]["metric"] == "order_count"  # the bigger mover still leads
    assert ranked[0]["score"] < 1.0  # but no longer at the saturation point
    assert all(0.0 <= r["score"] <= 1.0 for r in ranked)


# --- M1: a defaulted reference window is one the engine will accept ---------


_LAGGED_YAML = """
metrics:
  - name: spend
    source: dbt.metric.spend
  - name: signups
    source: dbt.metric.signups
    parents: [spend]
    lags: { spend: 7 }
"""


def _lagged_data(n_days: int):
    rng = np.random.default_rng(4)
    dates = pd.date_range("2024-01-01", periods=n_days)
    spend = 100.0 + rng.normal(0, 5.0, n_days)
    signups = 0.5 * spend + rng.normal(0, 1.0, n_days)
    return pd.DataFrame({"date": dates, "spend": spend, "signups": signups})


def test_defaulted_reference_window_leaves_room_for_the_lags():
    """The engine must not reject the window it chose for the caller.

    With 45 loaded days the matched adjacent block reaches back to the first
    loaded day, and a 7-day lag then reads the parent from a week before
    that — so the whole RCA 422'd citing 2023-12-25, a date nobody typed, on a
    tree that works fine at 60 or 200 days. Short history is a new client's
    first weeks, which is the worst possible moment for a confusing refusal.
    """
    dag = Parser(_LAGGED_YAML).dag
    data = _lagged_data(45)  # 2024-01-01 .. 2024-02-14

    result = run_rca(
        dag,
        data,
        {},
        "signups",
        analysis_start="2024-02-01",
        analysis_end="2024-02-14",
        draws=300,
    )

    assert result["reference_defaulted"]
    # Clamped to the first day whose lagged parent window is loaded, not to the
    # first loaded day.
    assert result["reference_window"]["start"] == "2024-01-08"
    assert result["reference_window"]["end"] == "2024-01-31"
    assert result["nodes"]["signups"]["status"] == "ok"
    assert (
        result["nodes"]["signups"]["contributions"][0]["parent_windows"]["reference"]["start"]
        == "2024-01-01"
    )


def test_defaulted_reference_window_is_unchanged_when_the_lags_fit():
    """The floor only ever binds on short history: with room to spare the
    matched adjacent block is exactly what it always was."""
    dag = Parser(_LAGGED_YAML).dag

    long_result = run_rca(
        dag,
        _lagged_data(200),
        {},
        "signups",
        analysis_start="2024-06-01",
        analysis_end="2024-06-14",
        draws=300,
    )

    assert long_result["reference_window"] == {"start": "2024-04-06", "end": "2024-05-31"}


def test_no_readable_reference_window_says_so_in_terms_of_the_lags():
    """When even one period of reference cannot be read, the refusal names the
    cause and the fix rather than a shifted date out of nowhere."""
    dag = Parser(_LAGGED_YAML).dag
    data = _lagged_data(12)  # 2024-01-01 .. 2024-01-12

    with pytest.raises(ValueError) as excinfo:
        run_rca(
            dag,
            data,
            {},
            "signups",
            analysis_start="2024-01-06",
            analysis_end="2024-01-12",
            draws=300,
        )

    message = str(excinfo.value)
    assert "not enough history" in message
    assert "2024-01-08" in message  # the earliest readable reference date
    assert "--start-date" in message


def test_an_explicit_reference_window_still_fails_coverage():
    """M1 is about not handing someone an error for a choice they did not make.
    A window the caller typed is still theirs to answer for."""
    dag = Parser(_LAGGED_YAML).dag
    data = _lagged_data(45)

    with pytest.raises(ValueError, match="not fully covered by its data"):
        run_rca(
            dag,
            data,
            {},
            "signups",
            **win(("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-14")),
            draws=300,
        )


# --- payload invariants the renderers should not have to enforce ------------


def _walk_intervals(result):
    """Every `ci_95` the response publishes, with a label for the failure."""
    for name, node in result["nodes"].items():
        for key in ("interaction",):
            if node[key]:
                yield f"{name}.{key}", node[key]["ci_95"]
        for term, summary in (node["components"] or {}).items():
            yield f"{name}.components.{term}", summary["ci_95"]
        for c in node["contributions"]:
            yield f"{name} -> {c['parent']}", c["ci_95"]
            for part, summary in c.get("decomposition", {}).items():
                yield f"{name} -> {c['parent']}.{part}", summary["ci_95"]


def test_no_published_interval_is_ever_zero_width():
    """One invariant covering the whole payload, in place of a filter in each
    renderer: `[0, 0]` as a 95% interval asserts a precision nothing measured.

    It arrives from three directions — a collapsed resampling (C4a), a term the
    model does not contain, and an identity whose co-movement is zero by
    construction — and a consumer cannot tell which from the interval, so none
    of them ships one.
    """
    dag, data = make_tree()
    for target in ("revenue", "order_count"):
        result = rca_on(dag, data, {}, target)
        for label, ci in _walk_intervals(result):
            assert ci is None or ci[1] > ci[0], f"zero-width ci_95 at {label}"

    result = run_rca(
        Parser(_CONSTANT_PARENT_YAML).dag,
        _constant_parent_data(),
        {},
        "total",
        **win(_C4_REF, _C4_AN),
    )
    for label, ci in _walk_intervals(result):
        assert ci is None or ci[1] > ci[0], f"zero-width ci_95 at {label}"


def test_ranked_causes_only_lists_metrics_something_attributed_to():
    """A ranking row with no provenance cannot be acted on.

    Every node in scope used to be listed, so a tree whose target could not be
    decomposed produced numbered rows of `{score: 0.0, via: null}` — zero-width
    bars and "via —" in the UI, and the same filter reinvented by every
    consumer. Nodes a hop actually reached are still listed at 0.0: "reached,
    explains nothing" is a finding, and `nodes` remains the full inventory.
    """
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    ranked = result["ranked_causes"]
    assert ranked, "the jaffle tree has ranked causes"
    assert all(r["via"] is not None for r in ranked)
    assert {r["metric"] for r in ranked} <= set(result["nodes"]) - {"revenue"}

    # A node reached by a hop that carried no weight stays, with its via.
    scope = {"revenue", "order_count", "average_order_value", "daily_sessions"}
    nodes_out = {
        "revenue": {
            "contributions": [
                {"parent": "order_count", "share_of_gap": 0.0},
                {"parent": "average_order_value", "share_of_gap": 1.0},
            ]
        },
        "order_count": {"contributions": []},  # never reaches daily_sessions
        "average_order_value": {"contributions": []},
        "daily_sessions": {"contributions": []},
    }

    ranked = _rank_causes(Parser(JAFFLE_YAML).dag, "revenue", scope, nodes_out)

    by_metric = {r["metric"]: r for r in ranked}
    assert by_metric["order_count"] == {"metric": "order_count", "score": 0.0, "via": "revenue"}
    assert "daily_sessions" not in by_metric


def test_a_node_with_no_aligned_frame_degrades_by_name(monkeypatch):
    """Roadmap C38 (grill M1): `GrainedData.fit_frame` raises RuntimeError on
    an empty grain join, the scoping loop called it outside any try, and the
    route catches ValueError — so a month-grain ancestor whose parent covers
    no whole month was an unhandled 500 with the diagnostic thrown away. A
    non-target node now degrades to `frame_unavailable` with the reason ("one
    bad node does not end the analysis"); the target re-raises as ValueError
    so the API's 422 carries the message."""
    from breakdown.grains import GrainedData

    dag, data = make_tree()
    real = GrainedData.fit_frame

    def broken(self, node, parents, grain):
        if node == "order_count":
            raise RuntimeError(
                "No overlapping whole 'month' periods across 'order_count' "
                "and its parents ['daily_sessions']."
            )
        return real(self, node, parents, grain)

    monkeypatch.setattr(GrainedData, "fit_frame", broken)

    result = rca_on(dag, make_tree()[1], {}, "revenue")
    node = result["nodes"]["order_count"]
    assert node["status"] == "frame_unavailable"
    assert "No overlapping" in node["status_reason"]
    assert node["baseline"] is None and node["gap"] is None
    # The rest of the tree still answers.
    assert result["nodes"]["revenue"]["status"] == "ok"

    with pytest.raises(ValueError, match="No overlapping"):
        rca_on(dag, make_tree()[1], {}, "order_count")

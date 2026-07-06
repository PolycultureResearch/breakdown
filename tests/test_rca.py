import pytest

from breakdown.engine.rca import run_rca, shapley_attribution
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
    """Formula node revenue uses Shapley; estimates sum to gap - unexplained."""
    dag, data = make_tree()
    result = rca_on(dag, data, {}, "revenue")

    rev = result["nodes"]["revenue"]
    assert rev["attribution_method"] == "shapley"
    parents = {c["parent"] for c in rev["contributions"]}
    assert parents == {"order_count", "average_order_value"}

    total = sum(c["estimate"] for c in rev["contributions"])
    assert abs(total - (rev["gap"] - rev["unexplained"])) < 1e-6

    for c in rev["contributions"]:
        assert c["ci_95"] is None
        assert c["prob_same_direction"] is None


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

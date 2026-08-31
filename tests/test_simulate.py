"""Tests for the steady-state scenario engine (`run_scenario`).

Fast tests stub FitResults with `az.from_dict` posteriors so coefficients are
exact and no PyMC sampling runs; constant input data makes window means (and
therefore deltas) exactly predictable. One slow test exercises the real
fit-on-demand path, which samples with NUTS (the default since roadmap S2).
"""

import arviz as az
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from breakdown.engine.model import FitResult
from breakdown.engine.simulate import (
    Assumption,
    EffectRange,
    Intervention,
    ScenarioRequest,
    run_scenario,
)
from breakdown.grains import build_grained
from breakdown.parser import Parser
from tests.synthetic import generate_mock_data

JAFFLE_YAML = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""

BASELINE = {"baseline_start": "2024-01-01", "baseline_end": "2024-02-29"}


def make_dag():
    return Parser(JAFFLE_YAML).dag


def make_data(n: int = 60) -> pd.DataFrame:
    """Constant series: window means are exact, so deltas are too."""
    dates = pd.date_range("2024-01-01", periods=n)  # ends 2024-02-29 for n=60
    return pd.DataFrame(
        {
            "date": dates,
            "daily_sessions": np.full(n, 1000.0),
            "order_count": np.full(n, 100.0),
            "average_order_value": np.full(n, 50.0),
            "revenue": np.full(n, 5000.0),
        }
    )


def stub_fit(target: str, parents: list, beta_post) -> FitResult:
    """FitResult whose beta_raw posterior is exactly `beta_post` (n_post,) or
    (n_post, n_parents).

    `inference_method` is load-bearing rather than decorative: `run_scenario`
    defaults to NUTS and reuses a cached fit only when it is at least as good
    as the one the request would produce, so a stub labelled `"advi"` is
    correctly *rejected* and the engine tries to fit the constant series these
    fixtures use. These stubs stand in for "the fit the engine would have
    made", and that fit is a NUTS fit."""
    beta = np.asarray(beta_post, dtype=float)
    if beta.ndim == 1:
        beta = beta[:, None]
    trace = az.from_dict(posterior={"beta_raw": beta[None, :, :]})
    return FitResult(
        trace=trace,
        target=target,
        parents=parents,
        y_mean=0.0,
        y_std=1.0,
        x_stds=None,
        dates=pd.DatetimeIndex([]),
        inference_method="nuts",
        fit_end=None,
        diagnostics={"fit_quality": "good"},
    )


def order_count_traces(beta_post):
    """Trace cache with a stubbed order_count fit under the full-window key
    (the baseline in these tests runs to the end of the data)."""
    return {("order_count", None): stub_fit("order_count", ["daily_sessions"], beta_post)}


def test_deterministic_chain_exact():
    """Intervening on a formula parent propagates exactly; no fit is needed
    because no probabilistic edge is on the affected path."""
    result = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            interventions=[
                Intervention(metric="order_count", mode="set", value=120.0),
            ],
        ),
    )
    oc = result["nodes"]["order_count"]
    assert oc["status"] == "intervened"
    assert oc["delta"]["estimate"] == pytest.approx(20.0)
    assert oc["delta"]["ci_95"] == [pytest.approx(20.0), pytest.approx(20.0)]

    rev = result["nodes"]["revenue"]
    assert rev["status"] == "affected"
    assert rev["delta"]["estimate"] == pytest.approx(20.0 * 50.0)
    assert rev["delta"]["ci_95"] == [pytest.approx(1000.0), pytest.approx(1000.0)]
    assert rev["prob_direction"] == 1.0
    assert rev["simulated"] == pytest.approx(6000.0)

    assert result["nodes"]["daily_sessions"]["status"] == "baseline"
    assert result["nodes"]["average_order_value"]["status"] == "baseline"


def test_prob_edge_point_mass_beta():
    """A degenerate posterior propagates as beta * delta with zero CI width."""
    result = run_scenario(
        make_dag(),
        make_data(),
        order_count_traces(np.full(400, 0.1)),
        ScenarioRequest(
            **BASELINE,
            interventions=[
                Intervention(metric="daily_sessions", mode="delta", value=200.0),
            ],
        ),
    )
    oc = result["nodes"]["order_count"]
    assert oc["status"] == "affected"
    assert oc["delta"]["estimate"] == pytest.approx(0.1 * 200.0)
    assert oc["delta"]["ci_95"] == [pytest.approx(20.0), pytest.approx(20.0)]
    assert oc["fit_quality"] == "good"

    rev = result["nodes"]["revenue"]
    assert rev["delta"]["estimate"] == pytest.approx(20.0 * 50.0)


def test_prob_edge_posterior_spread_composes_downstream():
    """A spread posterior yields a matching delta distribution, and the CI
    scales draw-aligned through the downstream formula node."""
    beta_post = np.linspace(0.05, 0.15, 500)  # mean 0.1
    result = run_scenario(
        make_dag(),
        make_data(),
        order_count_traces(beta_post),
        ScenarioRequest(
            **BASELINE,
            interventions=[
                Intervention(metric="daily_sessions", mode="delta", value=200.0),
            ],
        ),
    )
    oc = result["nodes"]["order_count"]
    assert oc["delta"]["estimate"] == pytest.approx(20.0, abs=0.5)
    lo, hi = oc["delta"]["ci_95"]
    assert 9.0 < lo < 12.0  # ~ 200 * 5.25th-percentile beta
    assert 28.0 < hi < 31.0
    # Every draw is positive, so the proportion saturates: the published value
    # is the Monte Carlo's resolution ceiling, flagged as the bound it is,
    # rather than a 1.0 the estimator cannot express.
    assert oc["prob_direction_censored"] is True
    assert oc["prob_direction"] == pytest.approx(1.0 - 1.0 / len(beta_post))

    # revenue delta = 50 * order_count delta per draw, so the CI scales by 50.
    rev = result["nodes"]["revenue"]
    assert rev["delta"]["ci_95"][0] == pytest.approx(50.0 * lo)
    assert rev["delta"]["ci_95"][1] == pytest.approx(50.0 * hi)


def test_do_operator_clamps_intervened_node():
    """Intervening on a node severs it from its parents: the sessions delta
    must not flow into the pinned order_count."""
    result = run_scenario(
        make_dag(),
        make_data(),
        order_count_traces(np.full(400, 0.1)),
        ScenarioRequest(
            **BASELINE,
            interventions=[
                Intervention(metric="daily_sessions", mode="delta", value=200.0),
                Intervention(metric="order_count", mode="set", value=130.0),
            ],
        ),
    )
    oc = result["nodes"]["order_count"]
    assert oc["status"] == "intervened"
    assert oc["delta"]["estimate"] == pytest.approx(30.0)  # not 30 + 20
    assert oc["delta"]["ci_95"] == [pytest.approx(30.0), pytest.approx(30.0)]

    rev = result["nodes"]["revenue"]
    assert rev["delta"]["estimate"] == pytest.approx(30.0 * 50.0)

    # Shapley over sources: with the order_count pin inactive, sessions would
    # have contributed 20; averaging the two orders gives 10 vs 20.
    oc_contribs = {c["source"]: c["estimate"] for c in oc["contributions"]}
    assert oc_contribs["i:daily_sessions"] == pytest.approx(10.0)
    assert oc_contribs["i:order_count"] == pytest.approx(20.0)
    assert sum(oc_contribs.values()) == pytest.approx(oc["delta"]["estimate"])
    rev_contribs = {c["source"]: c["estimate"] for c in rev["contributions"]}
    assert sum(rev_contribs.values()) == pytest.approx(rev["delta"]["estimate"])


def test_assumption_deterministic_and_stochastic():
    """low == high is a deterministic effect; a range becomes a Normal whose
    central 90% interval matches the stated bounds."""
    exact = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            assumptions=[
                Assumption(
                    source="discount_pct",
                    target="average_order_value",
                    effect=EffectRange(kind="relative", low=-0.1, high=-0.1),
                ),
            ],
        ),
    )
    aov = exact["nodes"]["average_order_value"]
    assert aov["status"] == "affected"
    assert aov["delta"]["estimate"] == pytest.approx(-5.0)
    assert exact["nodes"]["revenue"]["delta"]["estimate"] == pytest.approx(-500.0)
    assert exact["sources"][0]["kind"] == "assumption"

    spread = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            assumptions=[
                Assumption(
                    source="discount_pct",
                    target="average_order_value",
                    effect=EffectRange(kind="relative", low=-0.12, high=-0.08),
                ),
            ],
        ),
    )
    aov = spread["nodes"]["average_order_value"]
    assert aov["delta"]["estimate"] == pytest.approx(-5.0, abs=0.3)
    lo, hi = aov["delta"]["ci_95"]
    # stated 90% interval is [-6, -4]; the 95% CI is slightly wider
    assert -6.8 < lo < -5.7
    assert -4.3 < hi < -3.2

    # seeded rng: identical calls are identical responses
    again = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            assumptions=[
                Assumption(
                    source="discount_pct",
                    target="average_order_value",
                    effect=EffectRange(kind="relative", low=-0.12, high=-0.08),
                ),
            ],
        ),
    )
    assert again == spread


def test_decomposition_sums_through_nonlinear_formula():
    """Source contributions sum exactly to the node delta (Shapley efficiency),
    with the order x aov interaction term apportioned rather than dangling."""
    result = run_scenario(
        make_dag(),
        make_data(),
        order_count_traces(np.full(400, 0.1)),
        ScenarioRequest(
            **BASELINE,
            interventions=[Intervention(metric="daily_sessions", mode="pct", value=0.15)],
            assumptions=[
                Assumption(
                    source="promo",
                    target="average_order_value",
                    effect=EffectRange(kind="relative", low=0.1, high=0.1),
                )
            ],
        ),
    )
    # sessions +150 -> orders +15; aov +5; revenue = 115*55 - 100*50 = 1325
    rev = result["nodes"]["revenue"]
    assert rev["delta"]["estimate"] == pytest.approx(1325.0)
    contribs = {c["source"]: c["estimate"] for c in rev["contributions"]}
    assert sum(contribs.values()) == pytest.approx(1325.0)
    # each source gets its main effect plus half the 15*5 interaction
    assert contribs["i:daily_sessions"] == pytest.approx(15 * 50 + 75 / 2)
    assert contribs["a0"] == pytest.approx(100 * 5 + 75 / 2)


def test_scope_flags_and_override():
    result = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            interventions=[Intervention(metric="average_order_value", mode="set", value=55.0)],
            assumptions=[
                Assumption(
                    source="promo",
                    target="average_order_value",
                    effect=EffectRange(kind="relative", low=0.2, high=0.3),
                )
            ],
        ),
    )
    # upstream/disjoint nodes untouched
    assert result["nodes"]["daily_sessions"]["status"] == "baseline"
    assert result["nodes"]["order_count"]["status"] == "baseline"
    # the intervention wins over the assumption on the same node
    assert result["nodes"]["average_order_value"]["delta"]["estimate"] == pytest.approx(5.0)
    assert any(w["kind"] == "override" for w in result["warnings"])
    # constant history: any move is outside [min, max]
    assert result["nodes"]["average_order_value"]["extrapolation"]["flag"] is True
    assert any(
        w["kind"] == "extrapolation" and w["metric"] == "average_order_value"
        for w in result["warnings"]
    )


def test_validation_errors():
    dag, data = make_dag(), make_data()
    with pytest.raises(ValueError, match="not found"):
        run_scenario(
            dag,
            data,
            {},
            ScenarioRequest(
                **BASELINE, interventions=[Intervention(metric="nope", mode="set", value=1.0)]
            ),
        )
    with pytest.raises(ValueError, match="at least one"):
        run_scenario(dag, data, {}, ScenarioRequest(**BASELINE))
    with pytest.raises(ValueError, match="too many sources"):
        run_scenario(
            dag,
            data,
            {},
            ScenarioRequest(
                **BASELINE,
                assumptions=[
                    Assumption(
                        source=f"l{i}",
                        target="revenue",
                        effect=EffectRange(kind="absolute", low=1.0, high=2.0),
                    )
                    for i in range(11)
                ],
            ),
        )
    with pytest.raises(ValueError, match="before"):
        run_scenario(
            dag,
            data,
            {},
            ScenarioRequest(
                baseline_start="2024-02-01",
                baseline_end="2024-01-01",
                interventions=[Intervention(metric="revenue", mode="set", value=1.0)],
            ),
        )
    with pytest.raises(ValidationError):
        EffectRange(kind="absolute", low=2.0, high=1.0)


def test_fit_on_demand_with_a_real_fit():
    """Uncached probabilistic nodes on affected paths are fitted on demand and
    cached under (name, fit_end); baseline short of the data end uses a dated
    key. The fit runs NUTS, `run_scenario`'s default."""
    parser = Parser(JAFFLE_YAML)
    data = generate_mock_data(n_days=100)  # ends 2024-04-09
    traces = {}
    result = run_scenario(
        parser.dag,
        data,
        traces,
        ScenarioRequest(
            baseline_start="2024-03-01",
            baseline_end="2024-03-31",
            interventions=[Intervention(metric="daily_sessions", mode="pct", value=0.1)],
        ),
        draws=200,
    )
    assert ("order_count", "2024-04-01") in traces
    assert result["nodes"]["order_count"]["status"] == "affected"
    assert result["nodes"]["revenue"]["delta"]["estimate"] > 0
    ci = result["nodes"]["revenue"]["delta"]["ci_95"]
    assert ci[0] < result["nodes"]["revenue"]["delta"]["estimate"] < ci[1]


def test_a_what_if_lever_says_when_the_fit_cannot_separate_it():
    """Roadmap S4 on the what-if surface, which is where it bites hardest.

    A scenario pins one metric and propagates the child's *fitted slope* on it.
    When that parent is collinear with a sibling, the slope is one arbitrary
    point on a ridge — so the lever is not one you can pull on its own, and
    the node's own `delta.ci_95` does not say that. It travels here for the
    same reason `khat_status` does, and the four rules exist because the same
    disclosure reaching one orchestrator and not its neighbour is this repo's
    recurring defect.
    """
    from tests.test_engine import RIDGE_YAML, _ridge_frame

    parser = Parser(RIDGE_YAML)
    frame = _ridge_frame(n=90)
    traces: dict = {}
    result = run_scenario(
        parser.dag,
        frame,
        traces,
        ScenarioRequest(
            baseline_start="2024-02-05",
            baseline_end="2024-03-03",
            interventions=[Intervention(metric="x1", mode="pct", value=0.1)],
        ),
        draws=200,
    )
    y = result["nodes"]["y"]
    assert y["status"] == "affected"
    assert y["collinearity_status"] == "high"
    (msg,) = y["collinearity_warnings"]
    assert "'x1'" in msg and "'x2'" in msg

    # An intervened *source* node was never fitted, so it has nothing to
    # check — and null must not be mistaken for a check that passed.
    assert result["nodes"]["x1"]["collinearity_status"] is None

    # And it survives MCP compaction, where an agent decides what to advise.
    from breakdown.mcp.shaping import compact_scenario

    compact = compact_scenario(result)["nodes"]["y"]
    assert compact["collinearity_status"] == "high"
    assert compact["collinearity_warnings"]


# ---------------------------------------------------------------------------
# C25: /simulate labels a rate's baseline and refuses non-finite results.

RATE_YAML = """
metrics:
  - name: trials
    source: d.m.trials
  - name: conversions
    source: d.m.conversions
  - name: conversion_rate
    source: d.m.conversion_rate
    kind: rate
    denominator: trials
  - name: unexplained_rate
    source: d.m.unexplained_rate
    kind: rate
  - name: signups
    source: d.m.signups
    formula: "trials * conversion_rate"
    parents: [trials, conversion_rate]
"""


def test_rate_baselines_carry_the_window_aggregate_label():
    """C25a: RCA labels which arithmetic formed a rate's window value
    (`window_aggregate`, roadmap 1.11d) and /simulate computed the same number
    through the same entry point and published it bare — on both node shapes,
    since an unaffected rate still shows a baseline."""
    from breakdown.grains import build_grained

    n = 60
    dates = pd.date_range("2024-01-01", periods=n)
    names = ["trials", "conversions", "conversion_rate", "unexplained_rate", "signups"]
    values = {
        "trials": 200.0,
        "conversions": 20.0,
        "conversion_rate": 0.1,
        "unexplained_rate": 0.5,
        "signups": 20.0,
    }
    data = build_grained(
        {m: pd.DataFrame({"date": dates, m: np.full(n, values[m])}) for m in names},
        grain_of={m: "day" for m in names},
        kind_of={
            m: ("rate" if m in ("conversion_rate", "unexplained_rate") else "flow") for m in names
        },
        denominator_of={"conversion_rate": "trials"},
    )
    result = run_scenario(
        Parser(RATE_YAML).dag,
        data,
        {},
        ScenarioRequest(
            **BASELINE,
            interventions=[Intervention(metric="trials", mode="set", value=240.0)],
        ),
    )
    declared = result["nodes"]["conversion_rate"]
    assert declared["window_aggregate"] == "components"
    assert declared["window_aggregate_reason"] is None

    undeclared = result["nodes"]["unexplained_rate"]
    assert undeclared["window_aggregate"] == "period_mean_undeclared"
    assert "declares no `denominator`" in undeclared["window_aggregate_reason"]

    # Flows and stocks have one aggregation and it is not in question.
    assert "window_aggregate" not in result["nodes"]["trials"]


def test_non_finite_scenario_is_refused_not_encoded():
    """C25b (the 2026-08-12 review's L4): rule 3 says no engine result reaches
    an encoder unsanitized. A scenario with a non-finite number anywhere in a
    node payload raises a ValueError naming the node — the API's 422 — instead
    of meeting Starlette's `allow_nan=False` as an unhandled 500."""
    from breakdown.engine.simulate import _refuse_non_finite

    good = {
        "a": {"status": "baseline", "baseline": 1.0, "delta": {"ci_95": [0.0, 1.0]}},
    }
    _refuse_non_finite(good)  # no raise

    bad = {
        "a": {"status": "baseline", "baseline": 1.0},
        "b": {"status": "affected", "delta": {"estimate": float("inf"), "ci_95": [0.0, 1.0]}},
        "c": {"status": "affected", "contributions": [{"source": "i:x", "estimate": float("nan")}]},
    }
    with pytest.raises(ValueError, match="non-finite results for: b, c"):
        _refuse_non_finite(bad)


SHARE_YAML = """
metrics:
  - name: sessions
    source: dbt.metric.sessions
  - name: signup_rate
    source: dbt.metric.signup_rate
    kind: rate
    denominator: sessions
    share: true
  - name: sessions_per_visitor
    source: dbt.metric.sessions_per_visitor
    kind: rate
    denominator: sessions
"""


def share_data(n: int = 60) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "sessions": np.full(n, 1000.0),
            "signup_rate": np.full(n, 0.1),
            "sessions_per_visitor": np.full(n, 2.0),
        }
    )


def _simulate_share(metric: str, value: float):
    return run_scenario(
        Parser(SHARE_YAML).dag,
        share_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            interventions=[Intervention(metric=metric, mode="set", value=value)],
        ),
    )


def test_a_declared_share_cannot_be_simulated_above_one():
    """The ceiling half of the physical-bound check (roadmap C26).

    A share pushed past 1 is impossible, not unprecedented: 102.5% of members
    active is not a bold forecast. Before this, the response said only "above
    the historical max", which is the sentence a reader discounts.
    """
    result = _simulate_share("signup_rate", 1.5)
    node = result["nodes"]["signup_rate"]
    assert node["simulated"] == pytest.approx(1.5)
    assert node["non_physical"] is True
    detail = next(w["detail"] for w in result["warnings"] if w["kind"] == "non_physical")
    # The reason is the declaration, so a reader can check it against the tree.
    assert "share" in detail and "1.5" in detail


def test_a_declared_share_cannot_be_simulated_below_zero():
    result = _simulate_share("signup_rate", -0.2)
    assert result["nodes"]["signup_rate"]["non_physical"] is True


def test_a_share_inside_its_bounds_gains_no_impossibility():
    """A big move is still only a big move; `extrapolation` keeps that job."""
    result = _simulate_share("signup_rate", 0.9)
    node = result["nodes"]["signup_rate"]
    assert node["extrapolation"]["flag"] is True  # constant history, so any move
    assert node["non_physical"] is False
    assert not [w for w in result["warnings"] if w["kind"] == "non_physical"]


def test_a_rate_that_is_not_a_share_keeps_its_denominator_and_no_ceiling():
    """`denominator` is not the declaration; this is the node that proves it.

    `sessions_per_visitor` declares exactly what `signup_rate` does about how
    it aggregates over time, and 2.5 sessions per visitor is ordinary. Reading
    a ceiling off `denominator` — C26's original scope — would have called it
    impossible, and would have said the same of the bundled example tree's
    ~$182 `average_order_value`.
    """
    result = _simulate_share("sessions_per_visitor", 2.5)
    assert result["nodes"]["sessions_per_visitor"]["non_physical"] is False
    assert not [w for w in result["warnings"] if w["kind"] == "non_physical"]


def test_a_non_rate_node_is_untouched_by_the_ceiling():
    """A value above 1 on a non-rate node is just a value: $55 is not an
    impossibility, and neither is the $5,500 of revenue it implies."""
    result = run_scenario(
        make_dag(),
        make_data(),
        {},
        ScenarioRequest(
            **BASELINE,
            interventions=[Intervention(metric="average_order_value", mode="set", value=55.0)],
        ),
    )
    assert result["nodes"]["average_order_value"]["simulated"] == pytest.approx(55.0)
    assert result["nodes"]["average_order_value"]["non_physical"] is False
    assert result["nodes"]["revenue"]["non_physical"] is False
    assert not [w for w in result["warnings"] if w["kind"] == "non_physical"]


DISCONNECTED_MONTH_YAML = JAFFLE_YAML + """
  - name: board_mrr
    source: dbt.metric.board_mrr
    grain: month
"""


def _data_with_month_node(n: int = 60):
    dates = pd.date_range("2024-01-01", periods=n)
    day = {
        m: pd.DataFrame({"date": dates, m: np.full(n, v)})
        for m, v in (
            ("daily_sessions", 1000.0),
            ("order_count", 100.0),
            ("average_order_value", 50.0),
            ("revenue", 5000.0),
        )
    }
    months = pd.DataFrame(
        {"date": pd.to_datetime(["2024-01-01", "2024-02-01"]), "board_mrr": [9000.0, 9100.0]}
    )
    names = list(day) + ["board_mrr"]
    return build_grained(
        {**day, "board_mrr": months},
        grain_of={**{m: "day" for m in day}, "board_mrr": "month"},
        kind_of={m: "flow" for m in names},
    )


def test_an_unrelated_month_node_no_longer_blocks_a_sub_month_scenario():
    """Roadmap C39 (grill M2): the baseline loop raised for *every* node, so
    one disconnected month-grain metric made every sub-month what-if on the
    tree unusable. A node outside the affected cone is now omitted from
    `nodes` and named in `warnings`; a node the scenario propagates through
    still refuses loudly."""
    dag = Parser(DISCONNECTED_MONTH_YAML).dag
    data = _data_with_month_node()
    two_weeks = {"baseline_start": "2024-01-01", "baseline_end": "2024-01-14"}

    result = run_scenario(
        dag,
        data,
        {},
        ScenarioRequest(
            **two_weeks,
            interventions=[Intervention(metric="order_count", mode="pct", value=0.10)],
        ),
    )
    assert "board_mrr" not in result["nodes"]
    skips = [w for w in result["warnings"] if w["kind"] == "baseline_unavailable"]
    assert [w["metric"] for w in skips] == ["board_mrr"]
    assert "whole 'month' period" in skips[0]["detail"]
    assert result["nodes"]["revenue"]["status"] == "affected"

    # The cone still refuses: intervene ON the month node over the same window.
    with pytest.raises(ValueError, match="whole 'month' period"):
        run_scenario(
            dag,
            data,
            {},
            ScenarioRequest(
                **two_weeks,
                interventions=[Intervention(metric="board_mrr", mode="pct", value=0.10)],
            ),
        )

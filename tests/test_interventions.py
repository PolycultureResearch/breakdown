"""Roadmap S24: known, dated interventions as a declared step/pulse term.

The test plan of `knowledge/step_change_design.md` §4.3, against synthetic
worlds where the truth is known by construction. `_planted_step_world` in
`tests/test_calibration.py` plants a step in the *parent*; these plant one in
the *target's own history* — the shape issue #114 reported, where a local
level with a tight step-size prior cannot take a step and the posterior
predictive check says so.

Fast tests (parser rules, the indicator arithmetic, the per-node fit window,
MCP compaction) run in the `-m "not slow"` loop. The fits are marked slow.
"""

import json
import logging

import numpy as np
import pandas as pd
import pytest

from breakdown.engine import rca as rca_mod
from breakdown.engine.model import (
    fit_metric,
    intervention_indicator,
    intervention_record,
)
from breakdown.engine.rca import run_rca
from breakdown.mcp.shaping import RCA_HOW_TO_READ, compact_rca, rca_how_to_read, round_floats
from breakdown.parser import Parser
from tests.synthetic import win

# ---------------------------------------------------------------------------
# Worlds. Rows 0..129 are 2024-01-01..2024-05-09; t=40 is 2024-02-10, t=60 is
# 2024-03-01, t=91 is 2024-04-01 (the analysis window's first day).
# ---------------------------------------------------------------------------

N = 130
REF = ("2024-01-15", "2024-03-10")
AN = ("2024-04-01", "2024-05-09")
DATE_AT = {40: "2024-02-10", 50: "2024-02-20", 60: "2024-03-01", 91: "2024-04-01"}

BETA = 0.5
STEP = 30.0
NOISE = 1.0


def _frame(cols):
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=N), **cols})


def step_world(step_at, seed=101, step=STEP, x_step=0.0, extra_step=0.0):
    """y = β·x + step·1[t >= step_at] + noise, x stationary around 100 unless
    `x_step` makes the parent co-step on the same date. `extra_step` plants a
    second, unrelated shift in y on the same date (§4.3 test 9's second half)."""
    rng = np.random.default_rng(seed)
    x = 100.0 + rng.normal(0, 4.0, N)
    on = (np.arange(N) >= step_at).astype(float)
    x = x + x_step * on
    y = BETA * x + (step + extra_step) * on + rng.normal(0, NOISE, N)
    return _frame({"x": x, "y": y})


def pulse_world(pulse_at, seed=505, size=40.0):
    rng = np.random.default_rng(seed)
    x = 100.0 + rng.normal(0, 4.0, N)
    y = BETA * x + rng.normal(0, NOISE, N)
    y[pulse_at] += size
    return _frame({"x": x, "y": y})


def yaml_with(interventions="", extra=""):
    return f"""
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
{extra}{interventions}
"""


PLAIN_YAML = yaml_with()
STEP_AT_40 = yaml_with(
    """    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
"""
)


def rca(yaml, frame, traces=None, **kw):
    return run_rca(
        Parser(yaml).dag,
        frame,
        {} if traces is None else traces,
        "y",
        **win(REF, AN),
        draws=300,
        reference_sensitivity=False,
        **kw,
    )


def _strict(payload):
    json.dumps(payload, allow_nan=False)


def _identity_residual(node):
    return (
        node["gap"]
        - sum(c["estimate"] for c in node["contributions"])
        - sum(v["estimate"] for v in (node["components"] or {}).values())
        - sum((iv["estimate"] or 0.0) for iv in (node["interventions"] or []))
        - node["unexplained"]
    )


# ===========================================================================
# Parser (fast)
# ===========================================================================


def test_interventions_parse_with_every_field():
    parser = Parser(
        yaml_with(
            """    fit_start: 2024-01-08
    interventions:
      - name: tier_2_flip
        date: 2024-02-10
        kind: step
        prior: { distribution: Normal, params: { mu: 150, sigma: 100 } }
      - name: lineup_announce
        date: 2024-02-03
        kind: pulse
        until: 2024-02-04
      - name: spring_push
        date: 2024-03-07
        kind: pulse
        learn_from: window
    expected_signs: { tier_2_flip: positive, x: positive }
"""
        )
    )
    d = parser.dag.nodes["y"]["definition"]
    assert [iv.name for iv in d.interventions] == ["tier_2_flip", "lineup_announce", "spring_push"]
    assert d.interventions[0].prior.params == {"mu": 150, "sigma": 100}
    assert d.interventions[1].until.isoformat() == "2024-02-04"
    assert d.interventions[2].until is None and d.interventions[2].learn_from == "window"
    assert d.interventions[0].learn_from == "history"
    assert str(d.fit_start) == "2024-01-08"
    # Dates survive serialization as dates, not as opaque strings.
    dumped = d.model_dump()
    assert str(dumped["interventions"][0]["date"]) == "2024-02-10"


def test_a_quoted_date_string_parses_like_a_yaml_date():
    d = Parser(
        yaml_with(
            """    interventions:
      - {name: flip, date: "2024-02-10", kind: step}
"""
        )
    ).dag.nodes["y"]["definition"]
    assert d.interventions[0].date.isoformat() == "2024-02-10"


@pytest.mark.parametrize(
    "block, message",
    [
        (
            "      - {name: flip, date: 2024-02-10, kind: ramp}\n",
            "intervention kind must be one of ['step', 'pulse']",
        ),
        (
            "      - {name: flip, date: not-a-date, kind: step}\n",
            "date",
        ),
        (
            "      - {name: flip, date: 2024-02-10, kind: step, until: 2024-02-12}\n",
            "`until` is only meaningful on a `pulse`",
        ),
        (
            "      - {name: sale, date: 2024-02-10, kind: pulse, until: 2024-02-09}\n",
            "until=2024-02-09 before date=2024-02-10",
        ),
        (
            "      - {name: flip, date: 2024-02-10, kind: step, learn_from: always}\n",
            "learn_from must be one of ['history', 'window']",
        ),
        (
            "      - {name: 2flip, date: 2024-02-10, kind: step}\n",
            "must be an identifier",
        ),
        (
            "      - {name: flip, date: 2024-02-10, kind: step}\n"
            "      - {name: flip, date: 2024-03-10, kind: step}\n",
            "declares the intervention name 'flip' more than once",
        ),
        (
            "      - {name: x, date: 2024-02-10, kind: step}\n",
            "same name as one of its parents",
        ),
        (
            "      - {name: flip, date: 2024-02-10, kind: step, prior: {distribution: Cauchy}}\n",
            "Invalid distribution: Cauchy",
        ),
    ],
)
def test_the_parser_refuses_a_malformed_intervention(block, message):
    with pytest.raises(ValueError, match=__import__("re").escape(message)):
        Parser(yaml_with("    interventions:\n" + block))


def test_interventions_are_refused_on_a_formula_node():
    """Design §5: the identity has no regressors; the step happened to a parent."""
    with pytest.raises(ValueError, match="declares `interventions` on a formula node"):
        Parser(
            """
metrics:
  - name: orders
    source: a.orders
  - name: aov
    source: a.aov
  - name: revenue
    source: a.revenue
    formula: "orders * aov"
    parents: [orders, aov]
    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
"""
        )


def test_interventions_are_refused_under_provider_none():
    """Design §5: a prior-only step is a belief draw, and cold start is not
    earning surface — refuse rather than half-support."""
    with pytest.raises(ValueError, match="declare `interventions` under `provider: none`"):
        Parser(
            """
provider: { type: none }
metrics:
  - name: y
    source: a.y
    baseline: 100
    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
"""
        )


def test_interventions_are_allowed_on_a_source_node():
    """The reporter's own shape may be a root: a series whose history steps
    and which has no declared parents. The fit has a term to learn there."""
    d = Parser(
        """
metrics:
  - name: y
    source: a.y
    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
"""
    ).dag.nodes["y"]["definition"]
    assert d.parents == [] and len(d.interventions) == 1


def test_expected_signs_may_name_an_intervention_and_nothing_else():
    Parser(
        yaml_with(
            """    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
    expected_signs: { flip: negative }
"""
        )
    )
    with pytest.raises(ValueError, match="or interventions \\['flip'\\]"):
        Parser(
            yaml_with(
                """    interventions:
      - {name: flip, date: 2024-02-10, kind: step}
    expected_signs: { flop: negative }
"""
            )
        )


def test_a_date_off_the_grain_warns_and_names_the_snapped_period(caplog):
    """§4.1: a mid-week step on a weekly node is a partial-period effect the
    author should date to the period start. Warned, not refused."""
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        d = Parser(
            """
metrics:
  - name: y
    source: a.y
    grain: week
    interventions:
      - {name: flip, date: 2024-02-14, kind: step}
"""
        ).dag.nodes["y"]["definition"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("date=2024-02-14 is not aligned to the node's week grain" in m for m in msgs), msgs
    assert any("snaps to the period starting 2024-02-12" in m for m in msgs)
    # …and the engine's record carries the snapped date, not the YAML one.
    assert intervention_record(d.interventions[0], "week")["date"] == "2024-02-12"


def test_an_aligned_date_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        Parser(STEP_AT_40)
    assert not [r for r in caplog.records if "not aligned" in r.getMessage()]


# ===========================================================================
# The indicator and the per-node fit window (fast)
# ===========================================================================


def test_the_indicator_is_the_step_and_pulse_the_design_defines():
    dates = pd.date_range("2024-01-01", periods=6)
    step = {"name": "s", "date": "2024-01-03", "kind": "step", "until": None}
    pulse = {"name": "p", "date": "2024-01-03", "kind": "pulse", "until": "2024-01-04"}
    np.testing.assert_array_equal(intervention_indicator(step, dates), [0, 0, 1, 1, 1, 1])
    np.testing.assert_array_equal(intervention_indicator(pulse, dates), [0, 0, 1, 1, 0, 0])


def test_a_pulse_without_until_is_one_period_and_snaps_with_its_date():
    d = Parser(
        """
metrics:
  - name: y
    source: a.y
    grain: month
    interventions:
      - {name: p, date: 2024-02-14, kind: pulse}
"""
    ).dag.nodes["y"]["definition"]
    rec = intervention_record(d.interventions[0], "month")
    assert rec == {
        "name": "p",
        "date": "2024-02-01",
        "kind": "pulse",
        "until": "2024-02-01",
        "learn_from": "history",
    }


def test_the_rca_fit_end_is_analysis_start_unless_an_intervention_opts_out():
    from breakdown.grains import snap_window

    snapped = snap_window(AN[0], AN[1], "day")
    history = Parser(STEP_AT_40).dag.nodes["y"]["definition"]
    assert rca_mod._node_fit_end(history, "day", AN[0], snapped) == AN[0]

    window_step = Parser(
        yaml_with(
            "    interventions:\n      - {name: f, date: 2024-04-01, kind: step, learn_from: window}\n"
        )
    ).dag.nodes["y"]["definition"]
    # Through the analysis window's last whole day: fit_end is exclusive.
    assert rca_mod._node_fit_end(window_step, "day", AN[0], snapped) == "2024-05-10"

    window_pulse = Parser(
        yaml_with(
            "    interventions:\n"
            "      - {name: f, date: 2024-04-03, kind: pulse, until: 2024-04-05, learn_from: window}\n"
        )
    ).dag.nodes["y"]["definition"]
    assert rca_mod._node_fit_end(window_pulse, "day", AN[0], snapped) == "2024-04-06"

    # A window-mode pulse already inside history changes nothing.
    early = Parser(
        yaml_with(
            "    interventions:\n"
            "      - {name: f, date: 2024-02-03, kind: pulse, until: 2024-02-04, learn_from: window}\n"
        )
    ).dag.nodes["y"]["definition"]
    assert rca_mod._node_fit_end(early, "day", AN[0], snapped) == AN[0]


def test_the_rca_node_shape_carries_both_intervention_fields():
    record = rca_mod._node_out()
    assert record["interventions"] is None and record["dropped_interventions"] is None


# ===========================================================================
# MCP compaction (fast)
# ===========================================================================


def _rca_result_with(interventions=None, dropped=None, extended=None):
    node = rca_mod._node_out(
        status="ok",
        grain="day",
        effective_windows={
            "reference": {"start": REF[0], "end": REF[1], "n_periods": 56},
            "analysis": {"start": AN[0], "end": AN[1], "n_periods": 39},
        },
        baseline=100.0,
        actual=130.0,
        gap=30.0,
        relative_change=0.3,
        attribution_method="posterior",
        inference_method="nuts",
        fit_quality="ok",
        fit_window={
            "start": "2024-01-01",
            "end": "2024-03-31",
            "n_periods": 91,
            "extended_for": extended or [],
        },
        ci_status="ok",
        unexplained=0.5,
        unexplained_status="measured",
        components={"trend": {"estimate": 0.1, "ci_95": [-1.0, 1.2]}},
        interventions=interventions,
        dropped_interventions=dropped,
        contributions=[
            {
                "parent": "x",
                "estimate": 1.0,
                "share_of_gap": 0.03,
                "ci_95": [0.5, 1.5],
                "prob_same_direction": 0.99,
            }
        ],
    )
    return {
        "target": "y",
        "reference_window": {"start": REF[0], "end": REF[1]},
        "analysis_window": {"start": AN[0], "end": AN[1]},
        "reference_defaulted": False,
        "nodes": {"y": node},
        "ranked_causes": [{"metric": "x", "score": 0.03, "via": "y"}],
    }


def test_compact_rca_keeps_interventions_and_the_guide_says_what_they_are():
    iv = {
        "name": "flip",
        "date": "2024-02-10",
        "kind": "step",
        "until": None,
        "learn_from": "history",
        "window_delta": 1.0,
        "estimate": 28.4,
        "share_of_gap": 0.95,
        "ci_95": [27.0, 29.8],
        "ci_status": "ok",
        "prob_same_direction": 0.998,
        "prob_same_direction_censored": True,
    }
    out = compact_rca(_rca_result_with(interventions=[iv]))["nodes"]["y"]
    (kept,) = out["interventions"]
    assert kept["name"] == "flip" and kept["estimate"] == 28.4 and kept["ci_status"] == "ok"
    assert "learn_from" not in kept and "until" not in kept  # implied / null
    assert "fit_extended_for" not in out
    # The static guide carries the clause, whether or not a node has one.
    assert "`interventions` are dated changes the tree's author declared" in RCA_HOW_TO_READ
    assert "not in `ranked_causes`" in RCA_HOW_TO_READ


def test_compact_rca_carries_the_window_mode_claim_and_extension():
    iv = {
        "name": "push",
        "date": "2024-04-01",
        "kind": "pulse",
        "until": "2024-04-14",
        "learn_from": "window",
        "window_delta": 0.36,
        "estimate": 10.0,
        "share_of_gap": 0.33,
        "ci_95": [8.0, 12.0],
        "ci_status": "ok",
        "prob_same_direction": 0.99,
        "claim": "shift coincident with 2024-04-01; not separable from anything else dated the same.",
    }
    out = compact_rca(_rca_result_with(interventions=[iv], extended=["push"]))["nodes"]["y"]
    assert out["interventions"][0]["claim"].startswith("shift coincident with 2024-04-01")
    assert out["interventions"][0]["until"] == "2024-04-14"
    assert out["fit_extended_for"] == ["push"]


def test_a_dropped_intervention_survives_compaction_and_the_guide_names_it():
    result = _rca_result_with()
    assert rca_how_to_read(result) == RCA_HOW_TO_READ
    dropped = [
        {
            "intervention": "flip",
            "date": "2024-04-01",
            "kind": "step",
            "reason": "declared intervention 'flip' (2024-04-01) has no instance inside the fit window (2024-01-01 to 2024-03-31) and was not fitted; if the analysis window contains it, its effect is in `unexplained` or in the parents that moved with it.",
        }
    ]
    result = _rca_result_with(dropped=dropped)
    assert compact_rca(result)["nodes"]["y"]["dropped_interventions"] == dropped
    guide = rca_how_to_read(result)
    addendum = guide[len(RCA_HOW_TO_READ) :]
    assert "`flip` (2024-04-01) on `y` was **not fitted**" in addendum
    assert "Do not narrate it as having had no effect" in addendum


def test_a_withheld_intervention_estimate_stays_withheld_through_shaping():
    """Rule 3: a non-finite estimate is withheld by name upstream; nothing on
    the way to the encoder may turn it into a zero."""
    iv = {
        "name": "flip",
        "date": "2024-02-10",
        "kind": "step",
        "until": None,
        "learn_from": "history",
        "window_delta": 1.0,
        "estimate": None,
        "share_of_gap": None,
        "ci_95": None,
        "ci_status": "nonfinite_posterior",
        "prob_same_direction": None,
    }
    out = round_floats(compact_rca(_rca_result_with(interventions=[iv])))
    _strict(out)
    kept = out["nodes"]["y"]["interventions"][0]
    assert kept["estimate"] is None and kept["ci_status"] == "nonfinite_posterior"


# ===========================================================================
# The fits (slow)
# ===========================================================================


@pytest.mark.slow
def test_world_1_the_defect_reproduced():
    """§4.3 test 1: what an undeclared step in the target's own history costs
    (design §1.2), pinned to what the engine measurably does on this world
    rather than to what the design expected of it.

    The design predicted a `moderate`/`severe` PPC with `resid_acf1` or
    `resid_max` flagged. On this world it does not fire: the local level
    absorbs the step over a few periods by inflating `σ_trend` some 40x
    (0.175 vs 0.004 in z-space) and the residuals around it are not
    autocorrelated enough to trip the check (`ok` up to a step of 30; a step
    of 60 reaches `moderate`, on `min`). What *is* measured, and asserted
    here: `σ_obs` is inflated (1.26x at step 30, 2.09x at 60), and β's
    interval is 2.5–4.5x wider with the mean pulled off the truth. The
    sweep is in roadmap_log S24; the PPC verdict is deliberately not
    asserted either way."""
    frame = step_world(40)
    undeclared = fit_metric(
        Parser(PLAIN_YAML).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0
    )
    declared = fit_metric(
        Parser(STEP_AT_40).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0
    )

    def sigma_obs(fit):
        return float(fit.trace.posterior["sigma_obs"].values.mean()) * fit.y_std

    def sigma_trend(fit):
        return float(fit.trace.posterior["sigma_trend"].values.mean())

    def beta_width(fit):
        b = fit.trace.posterior["beta_raw"].values.reshape(-1)
        return float(np.percentile(b, 97.5) - np.percentile(b, 2.5))

    assert sigma_obs(undeclared) > 1.15 * NOISE, sigma_obs(undeclared)
    assert sigma_obs(undeclared) > 1.15 * sigma_obs(declared)
    assert sigma_trend(undeclared) > 10 * sigma_trend(declared)
    assert beta_width(undeclared) > 1.8 * beta_width(declared)
    assert undeclared.interventions == [] and undeclared.dropped_interventions == []
    assert undeclared.diagnostics["ppc"]["conditioned_on_interventions"] == []


@pytest.mark.slow
def test_world_2_declared_and_recovered():
    """§4.3 test 2: the same world with the step declared. The coefficient
    axis is untouched (`beta` still has one column, `x`), the step's size is
    recovered on its own axis, σ_obs returns to the planted noise, β to the
    truth, and the PPC passes conditioned on the declaration."""
    frame = step_world(40)
    fit = fit_metric(Parser(STEP_AT_40).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0)

    assert fit.parents == ["x"]
    assert fit.trace.posterior["beta"].shape[-1] == 1
    assert [r["name"] for r in fit.interventions] == ["flip"]
    assert fit.interventions[0]["date"] == "2024-02-10"
    assert fit.dropped_interventions == []

    beta = fit.trace.posterior["beta_raw"].values.reshape(-1)
    assert abs(float(beta.mean()) - BETA) < 0.25 * BETA
    assert np.percentile(beta, 2.5) <= BETA <= np.percentile(beta, 97.5)
    step = fit.trace.posterior["beta_intervention_raw"].values.reshape(-1)
    assert abs(float(step.mean()) - STEP) < 0.25 * STEP
    assert np.percentile(step, 2.5) <= STEP <= np.percentile(step, 97.5)
    sigma_obs = float(fit.trace.posterior["sigma_obs"].values.mean()) * fit.y_std
    assert sigma_obs < 1.2 * NOISE, sigma_obs

    assert fit.diagnostics["ppc_status"] == "ok"
    assert fit.diagnostics["ppc"]["conditioned_on_interventions"] == ["flip"]
    # One parent and one intervention: two regressors, so the design was
    # checked and found separable (x is stationary, the step is not x).
    assert fit.diagnostics["collinearity_status"] == "ok"

    # And through RCA. t=40 (Feb 10) is inside REF (Jan 15 – Mar 10): 30 of
    # its 56 days are after the step, so the term carries 1 − 30/56 of it.
    result = rca(STEP_AT_40, frame)
    _strict(result)
    node = result["nodes"]["y"]
    (iv,) = node["interventions"]
    assert iv["window_delta"] == pytest.approx(1.0 - 30 / 56)
    assert abs(iv["estimate"] - STEP * iv["window_delta"]) < 0.25 * STEP * iv["window_delta"]
    assert abs(node["unexplained"]) < 0.25 * abs(node["gap"])
    assert abs(_identity_residual(node)) < 1e-9
    assert [c["metric"] for c in result["ranked_causes"]] == ["x"]

    # A step entirely before both windows (t=10, Jan 11) fixed the fit and
    # moves the gap by nothing — reported as such, not as a zero-width
    # interval.
    early = rca(
        yaml_with("    interventions:\n      - {name: flip, date: 2024-01-11, kind: step}\n"),
        step_world(10),
    )
    _strict(early)
    (iv0,) = early["nodes"]["y"]["interventions"]
    assert iv0["window_delta"] == 0.0
    assert iv0["estimate"] == 0.0 and iv0["ci_95"] is None
    assert iv0["ci_status"] == "indicator_unchanged" and iv0["prob_same_direction"] is None
    assert abs(_identity_residual(early["nodes"]["y"])) < 1e-9


@pytest.mark.slow
def test_world_3_step_inside_the_reference_window():
    """§4.3 test 3: a step at t=60 sits inside REF (Jan 15 – Mar 10, 56 days;
    10 of them after the step). Its window delta is 1 − 10/56, the term
    carries the gap, and the identity holds to the C3 tolerance."""
    frame = step_world(60)
    result = rca(
        yaml_with("    interventions:\n      - {name: flip, date: 2024-03-01, kind: step}\n"), frame
    )
    _strict(result)
    node = result["nodes"]["y"]
    (iv,) = node["interventions"]
    assert iv["window_delta"] == pytest.approx(1.0 - 10 / 56)
    assert abs(iv["estimate"] - STEP * iv["window_delta"]) < 0.25 * STEP * iv["window_delta"]
    assert iv["ci_95"][0] <= STEP * iv["window_delta"] <= iv["ci_95"][1]
    assert iv["prob_same_direction"] > 0.95
    assert abs(node["unexplained"]) < 0.25 * abs(node["gap"])
    assert abs(_identity_residual(node)) < 1e-9
    # Not a cause to drill into.
    assert "flip" not in {c["metric"] for c in result["ranked_causes"]}


@pytest.mark.slow
def test_world_4_step_only_in_the_analysis_window_is_dropped_by_name():
    """§4.3 test 4: a step at t=91 has no instance in the fit window. It is
    on `dropped_interventions` with the fit-window reason, nothing is in
    `interventions`, β is byte-identical to the undeclared fit, and the
    payload sentence says where the effect went."""
    frame = step_world(91)
    declared = yaml_with("    interventions:\n      - {name: flip, date: 2024-04-01, kind: step}\n")
    with_decl = fit_metric(
        Parser(declared).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0
    )
    without = fit_metric(
        Parser(PLAIN_YAML).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0
    )
    assert with_decl.interventions == []
    (dropped,) = with_decl.dropped_interventions
    assert dropped["intervention"] == "flip" and dropped["date"] == "2024-04-01"
    assert "has no instance inside the fit window (2024-01-01 to 2024-03-31)" in dropped["reason"]
    assert (
        "its effect is in `unexplained` or in the parents that moved with it" in dropped["reason"]
    )
    assert "beta_intervention" not in with_decl.trace.posterior
    np.testing.assert_allclose(
        with_decl.trace.posterior["beta_raw"].values,
        without.trace.posterior["beta_raw"].values,
        rtol=1e-6,
    )

    result = rca(declared, frame)
    _strict(result)
    node = result["nodes"]["y"]
    assert node["interventions"] is None
    assert node["dropped_interventions"][0]["intervention"] == "flip"
    # The step's whole effect is in `unexplained`, measured.
    assert node["unexplained_status"] == "measured"
    assert abs(node["unexplained"] - STEP) < 0.25 * STEP
    assert abs(_identity_residual(node)) < 1e-9


@pytest.mark.slow
def test_world_5_a_pulse_recovers_the_spike():
    """§4.3 test 5: a one-day spike of +40 at t=50, declared as a pulse. The
    coefficient recovers it and the PPC no longer has `max` to complain about."""
    frame = pulse_world(50)
    declared = yaml_with(
        "    interventions:\n      - {name: spike, date: 2024-02-20, kind: pulse}\n"
    )
    fit = fit_metric(Parser(declared).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0)
    assert fit.interventions[0]["until"] == "2024-02-20"
    pulse = fit.trace.posterior["beta_intervention_raw"].values.reshape(-1)
    assert abs(float(pulse.mean()) - 40.0) < 0.25 * 40.0
    assert np.percentile(pulse, 2.5) <= 40.0 <= np.percentile(pulse, 97.5)
    assert fit.diagnostics["ppc_status"] == "ok"
    assert not [
        s
        for s in fit.diagnostics["ppc"]["statistics"]
        if s["statistic"] == "max" and s["status"] != "ok"
    ]


@pytest.mark.slow
def test_world_6_collinear_with_a_parent_is_flagged_and_names_both():
    """§4.3 test 6: x itself steps on the declared date. Which of the two
    "caused" the target's step is not a determined quantity, and the S4
    check says so, naming the parent and the intervention alike."""
    frame = step_world(40, seed=606, x_step=30.0)
    fit = fit_metric(Parser(STEP_AT_40).dag, frame, "y", draws=300, fit_end=AN[0], random_seed=0)
    assert fit.diagnostics["collinearity_status"] in ("moderate", "high")
    names = {n for pair in fit.diagnostics["collinearity"]["pairs"] for n in pair["parents"]}
    assert names == {"x", "flip"}, fit.diagnostics["collinearity"]
    assert any("flip" in w for w in fit.diagnostics["collinearity_warnings"])


@pytest.mark.slow
def test_world_8_fit_start_cuts_the_history_per_node():
    """§4.3 test 8: with `fit_start` after the planted step and no
    declaration, the fit sees one regime, uses exactly the whole periods on
    or after the date, and passes its PPC; a `fit_start` that leaves fewer
    than MIN_FIT_PERIODS is refused with the existing message naming it."""
    frame = step_world(40)
    fit = fit_metric(
        Parser(yaml_with(extra="    fit_start: 2024-02-15\n")).dag,
        frame,
        "y",
        draws=300,
        fit_end=AN[0],
        random_seed=0,
    )
    assert str(fit.dates[0].date()) == "2024-02-15"
    assert str(fit.dates[-1].date()) == "2024-03-31"
    assert len(fit.dates) == 46
    assert fit.fit_start == "2024-02-15"
    beta = fit.trace.posterior["beta_raw"].values.reshape(-1)
    assert np.percentile(beta, 2.5) <= BETA <= np.percentile(beta, 97.5)
    assert fit.diagnostics["ppc_status"] == "ok"

    with pytest.raises(ValueError, match="Only 5 whole day periods") as e:
        fit_metric(
            Parser(yaml_with(extra="    fit_start: 2024-03-27\n")).dag,
            frame,
            "y",
            draws=300,
            fit_end=AN[0],
            random_seed=0,
        )
    assert "fit_start=2024-03-27" in str(e.value)
    assert "The node's own `fit_start` is what cut this window" in str(e.value)


@pytest.mark.slow
def test_world_8_rca_default_reference_respects_fit_start():
    """A defaulted reference block may not start before the node's fit
    window, or the attribution would read trend states the fit never had."""
    frame = step_world(40)
    dag = Parser(yaml_with(extra="    fit_start: 2024-02-15\n")).dag
    result = run_rca(
        dag,
        frame,
        {},
        "y",
        analysis_start=AN[0],
        analysis_end=AN[1],
        draws=300,
        reference_sensitivity=False,
    )
    assert result["reference_defaulted"] is True
    assert result["reference_window"]["start"] >= "2024-02-15"
    assert result["nodes"]["y"]["fit_window"]["start"] == "2024-02-15"
    assert result["nodes"]["y"]["status"] == "ok"


@pytest.mark.slow
def test_world_9_learn_from_window_identifies_the_step_from_its_own_periods():
    """§4.3 test 9: world 4 again with the exception on. The fit's dates run
    through analysis_end, the estimate recovers the step with the truth inside
    its interval, `claim` and `fit_window.extended_for` are on the node, and
    `unexplained` falls by the recovered amount against world 4. Then a second,
    unrelated shift on the same date: the estimate takes both — §3.2 (i),
    pinned rather than asserted."""
    frame = step_world(91)
    history = yaml_with("    interventions:\n      - {name: flip, date: 2024-04-01, kind: step}\n")
    window = yaml_with(
        "    interventions:\n      - {name: flip, date: 2024-04-01, kind: step, learn_from: window}\n"
    )
    traces = {}
    res_hist = rca(history, frame)
    res_win = rca(window, frame, traces)
    _strict(res_win)
    node = res_win["nodes"]["y"]

    assert list(traces) == [("y", "2024-05-10")]
    fit = traces[("y", "2024-05-10")]
    assert str(fit.dates[-1].date()) == AN[1]
    assert node["fit_window"] == {
        "start": "2024-01-01",
        "end": AN[1],
        "n_periods": N,
        "extended_for": ["flip"],
    }
    (iv,) = node["interventions"]
    assert iv["window_delta"] == 1.0
    assert abs(iv["estimate"] - STEP) < 0.25 * STEP
    assert iv["ci_95"][0] <= STEP <= iv["ci_95"][1]
    assert iv["claim"].startswith("shift coincident with 2024-04-01; not separable")
    assert node["dropped_interventions"] is None
    # The recovered step left `unexplained`.
    hist_unexpl = res_hist["nodes"]["y"]["unexplained"]
    assert abs((hist_unexpl - node["unexplained"]) - iv["estimate"]) < 0.25 * STEP
    assert abs(_identity_residual(node)) < 1e-9

    # A second, unrelated +20 shift on the same date lands in the same term.
    frame2 = step_world(91, extra_step=20.0)
    node2 = rca(window, frame2)["nodes"]["y"]
    (iv2,) = node2["interventions"]
    assert abs(iv2["estimate"] - (STEP + 20.0)) < 0.25 * (STEP + 20.0)
    assert iv2["ci_95"][0] <= STEP + 20.0 <= iv2["ci_95"][1]


@pytest.mark.slow
def test_a_source_node_with_an_intervention_is_fitted_and_attributed():
    """A root that declares a step is fitted for it — the reporter's shape
    when the flip has no declared parent to carry it — and the term reaches
    the RCA node with the level and nothing else in the split."""
    frame = step_world(60)
    yaml = """
metrics:
  - name: y
    source: dbt.metric.y
    interventions:
      - {name: flip, date: 2024-03-01, kind: step}
"""
    result = run_rca(
        Parser(yaml).dag,
        frame[["date", "y"]],
        {},
        "y",
        **win(REF, AN),
        draws=300,
        reference_sensitivity=False,
    )
    _strict(result)
    node = result["nodes"]["y"]
    assert node["status"] == "ok" and node["attribution_method"] == "posterior"
    assert node["contributions"] == [] and node["dropped_parents"] is None
    (iv,) = node["interventions"]
    assert iv["window_delta"] == pytest.approx(1.0 - 10 / 56)
    assert abs(iv["estimate"] - STEP * iv["window_delta"]) < 0.25 * STEP * iv["window_delta"]
    assert abs(_identity_residual(node)) < 1e-9


@pytest.mark.slow
def test_a_declared_sign_on_an_intervention_is_checked():
    frame = step_world(40)
    fit = fit_metric(
        Parser(
            yaml_with(
                "    interventions:\n      - {name: flip, date: 2024-02-10, kind: step}\n    expected_signs: {flip: negative}\n"
            )
        ).dag,
        frame,
        "y",
        draws=300,
        fit_end=AN[0],
        random_seed=0,
    )
    (warning,) = fit.diagnostics["sign_warnings"]
    assert warning.startswith("Intervention 'flip' on 'y': declared negative effect")
    assert "P(beta_intervention_raw < 0)" in warning

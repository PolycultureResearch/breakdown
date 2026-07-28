"""Calibration suite (roadmap 1.2 / T10): RCA against synthetic ground truth.

Three properties a trustworthy RCA must have, each tested on data where the
truth is known by construction:

- **Recovery**: a planted cause (a parent that really moved, with a known
  coefficient) is attributed the right amount — contemporaneous, lagged, and
  through exact formula identities.
- **Restraint**: when nothing happened, or when a declared parent had nothing
  to do with the change, RCA does not manufacture a confident cause.
- **Coverage**: the 95% contribution intervals actually cover the true
  contribution across repeated worlds (many data seeds, one per world).

Everything is seeded — mock data by seed, fits via fit_metric's sampler
seeding (run_rca seeds its on-demand ADVI fits internally) — so these pass
or fail reproducibly, not probabilistically.
"""

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.rca import run_rca
from breakdown.parser import Parser

PROB_YAML = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
"""

TWO_PARENT_YAML = """
metrics:
  - name: x1
    source: dbt.metric.x1
  - name: x2
    source: dbt.metric.x2
  - name: y
    source: dbt.metric.y
    parents: [x1, x2]
"""

LAGGED_YAML = """
metrics:
  - name: x
    source: dbt.metric.x
  - name: y
    source: dbt.metric.y
    parents: [x]
    lags: { x: 5 }
"""

FORMULA_YAML = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: aov
    source: dbt.metric.aov
  - name: revenue
    source: dbt.metric.revenue
    formula: "orders * aov"
    parents: [orders, aov]
"""

N = 130
REF = ("2024-01-15", "2024-03-10")   # inside the fitted (pre-analysis) period
AN = ("2024-04-01", "2024-05-09")    # rows 91..129


def _frame(cols):
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=N), **cols})


def _planted_step_world(seed, beta=0.5, step=30.0, lag=0, noise=1.0):
    """x is stationary around 100 then steps up by `step` for the analysis
    window (shifted earlier by `lag` so the effect lands in the window);
    y = beta * x[t-lag] + noise. Returns (frame, true_contribution)."""
    rng = np.random.default_rng(seed)
    x = 100.0 + rng.normal(0, 4.0, N)
    an0 = 91  # index of AN start
    x[an0 - lag :] += step
    shifted = np.concatenate([np.full(lag, x[0]), x[: N - lag]]) if lag else x
    y = beta * shifted + rng.normal(0, noise, N)
    frame = _frame({"x": x, "y": y})
    # True contribution: beta times the (lag-shifted) parent's window delta.
    dates = frame["date"]
    ref_mask = (dates >= REF[0]) & (dates <= REF[1])
    an_mask = (dates >= AN[0]) & (dates <= AN[1])
    shift = pd.Timedelta(days=lag)
    ref_shift = (dates >= pd.Timestamp(REF[0]) - shift) & (dates <= pd.Timestamp(REF[1]) - shift)
    an_shift = (dates >= pd.Timestamp(AN[0]) - shift) & (dates <= pd.Timestamp(AN[1]) - shift)
    truth = beta * (x[an_shift].mean() - x[ref_shift].mean())
    assert ref_mask.any() and an_mask.any()
    return frame, truth


# ---------- recovery ----------

def test_recovers_planted_contemporaneous_cause():
    """The parent moved +30 with beta=0.5: RCA must hand it ~15/day and
    leave almost nothing unexplained."""
    frame, truth = _planted_step_world(seed=101)
    dag = Parser(PROB_YAML).dag

    result = run_rca(dag, frame, {}, "y", *REF, *AN, advi_draws=300)

    node = result["nodes"]["y"]
    c = node["contributions"][0]
    assert abs(c["estimate"] - truth) < 0.25 * abs(truth)
    assert c["ci_95"][0] <= truth <= c["ci_95"][1]
    assert c["prob_same_direction"] > 0.95
    # The planted cause explains the bulk of the gap.
    assert abs(node["unexplained"]) < 0.25 * abs(node["gap"])


def test_recovers_planted_lagged_cause():
    """Same, but the effect arrives 5 days late and the tree declares the lag."""
    frame, truth = _planted_step_world(seed=202, lag=5)
    dag = Parser(LAGGED_YAML).dag

    result = run_rca(dag, frame, {}, "y", *REF, *AN, advi_draws=300)

    c = result["nodes"]["y"]["contributions"][0]
    assert abs(c["estimate"] - truth) < 0.25 * abs(truth)
    assert c["ci_95"][0] <= truth <= c["ci_95"][1]


def test_recovers_moved_factor_in_identity():
    """revenue = orders x aov with only orders moving: Shapley must hand the
    gap to orders, give aov ~nothing, and leave zero unexplained."""
    rng = np.random.default_rng(303)
    orders = 100.0 + rng.normal(0, 3.0, N)
    orders[91:] += 25.0
    aov = 50.0 + rng.normal(0, 1.0, N)
    revenue = orders * aov
    frame = _frame({"orders": orders, "aov": aov, "revenue": revenue})
    dag = Parser(FORMULA_YAML).dag

    result = run_rca(dag, frame, {}, "revenue", *REF, *AN)

    node = result["nodes"]["revenue"]
    by_parent = {c["parent"]: c for c in node["contributions"]}
    assert abs(node["unexplained"]) < 1e-6  # exact identity
    assert by_parent["orders"]["estimate"] > 0.75 * node["gap"]
    assert abs(by_parent["aov"]["estimate"]) < 0.20 * abs(node["gap"])


# ---------- restraint ----------

def test_null_case_attributes_nothing_confidently():
    """Nothing happened: both windows come from the same stationary regime.
    The contribution interval must cover zero and the gap must be noise-sized."""
    rng = np.random.default_rng(404)
    x = 100.0 + rng.normal(0, 4.0, N)
    y = 0.5 * x + rng.normal(0, 1.0, N)
    frame = _frame({"x": x, "y": y})
    dag = Parser(PROB_YAML).dag

    result = run_rca(dag, frame, {}, "y", *REF, *AN, advi_draws=300)

    node = result["nodes"]["y"]
    c = node["contributions"][0]
    assert c["ci_95"][0] <= 0 <= c["ci_95"][1]
    # Gap is on the scale of sampling noise of window means, not a finding.
    assert abs(node["gap"]) < 2.0


def test_unrelated_parent_gets_no_credit():
    """y is driven by x1 alone; x2 is declared as a parent but is pure noise.
    The planted cause is recovered and the bystander's CI covers zero."""
    rng = np.random.default_rng(505)
    x1 = 100.0 + rng.normal(0, 4.0, N)
    x1[91:] += 30.0
    x2 = 200.0 + rng.normal(0, 6.0, N)  # stationary, unrelated
    y = 0.5 * x1 + rng.normal(0, 1.0, N)
    frame = _frame({"x1": x1, "x2": x2, "y": y})
    dag = Parser(TWO_PARENT_YAML).dag

    result = run_rca(dag, frame, {}, "y", *REF, *AN, advi_draws=300)

    by_parent = {c["parent"]: c for c in result["nodes"]["y"]["contributions"]}
    truth = 0.5 * 30.0
    assert abs(by_parent["x1"]["estimate"] - truth) < 0.35 * truth
    assert by_parent["x2"]["ci_95"][0] <= 0 <= by_parent["x2"]["ci_95"][1]


# ---------- coverage ----------

@pytest.mark.parametrize("worlds", [20])
def test_contribution_ci_coverage(worlds):
    """Across `worlds` independent datasets with the same planted cause, the
    95% contribution interval must cover the true contribution in at least
    80% of them (slack for the ADVI approximation and finite bootstrap)."""
    covered = 0
    for k in range(worlds):
        frame, truth = _planted_step_world(seed=1000 + k)
        dag = Parser(PROB_YAML).dag
        result = run_rca(dag, frame, {}, "y", *REF, *AN, advi_draws=300)
        ci = result["nodes"]["y"]["contributions"][0]["ci_95"]
        if ci[0] <= truth <= ci[1]:
            covered += 1
    assert covered >= int(0.8 * worlds), f"coverage {covered}/{worlds}"

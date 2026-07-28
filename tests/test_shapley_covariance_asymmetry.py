"""Characterization tests for the covariance asymmetry in per-day Shapley
attribution (roadmap 1.8).

`shapley_attribution` collapses the reference window to per-parent means
(`baseline = formula(reference means)`) while evaluating the analysis window
per-day. For an exact per-day product `volume_d = orders_d * rate_d` the
reconstruction therefore drops the reference-window covariance, and
`run_rca`'s residual absorbs it: `unexplained = gap - sh["gap"]
= -cov_ref(orders, rate)` — a mathematically exact identity yields nonzero
unexplained.

The frames below pin each window's means exactly while making its covariance
tunable: with `d` a fixed zero-mean pattern and `B = mu + s*d`,
`C = mu + sign(K)*s*d`, the window means stay `mu` for any `s` while
cov(B, C) = K exactly (population, ddof=0). That isolates covariance from
means and from any grain/reindex leakage.

These tests characterize the CURRENT (asymmetric) behavior and pass against
it; the fix change replaces them with the symmetric expectations.
"""

import numpy as np
import pandas as pd

from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.parser import Parser

YAML = """
metrics:
  - name: orders
    source: dbt.metric.orders
  - name: rate
    source: dbt.metric.rate
  - name: volume
    source: dbt.metric.volume
    formula: "orders * rate"
    parents: [orders, rate]
"""

REF = ("2024-01-01", "2024-01-30")
AN = ("2024-01-31", "2024-02-29")

MU_B, MU_C = 10.0, 5.0


def make_frame(cov_ref, cov_an, mu_b_an=None, mu_c_an=None):
    """60 daily rows (reference = first 30, analysis = last 30).

    Each window's orders/rate series is `mu + s*d` with `d` a fixed zero-mean
    pattern, so the window mean is exactly `mu` while cov(orders, rate) is
    exactly the requested value. `volume = orders * rate` elementwise — a
    clean per-day identity with no residual.
    """
    d = np.tile([1.0, -1.0], 15)

    def window(cov, mu_b, mu_c):
        s = np.sqrt(abs(cov))
        return mu_b + s * d, mu_c + np.sign(cov) * s * d

    b_ref, c_ref = window(cov_ref, MU_B, MU_C)
    b_an, c_an = window(
        cov_an,
        MU_B if mu_b_an is None else mu_b_an,
        MU_C if mu_c_an is None else mu_c_an,
    )
    orders = np.concatenate([b_ref, b_an])
    rate = np.concatenate([c_ref, c_an])
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=60),
        "orders": orders,
        "rate": rate,
        "volume": orders * rate,
    })


def window_cov(frame, start, end):
    mask = (frame["date"] >= start) & (frame["date"] <= end)
    b = frame.loc[mask, "orders"].to_numpy()
    c = frame.loc[mask, "rate"].to_numpy()
    return np.mean(b * c) - np.mean(b) * np.mean(c)


def test_efficiency_vs_formula_gap():
    """Shapley efficiency holds against the formula's own gap (both before and
    after the fix): the attributions sum exactly to actual - baseline."""
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=2.0, cov_an=1.0)

    result = shapley_attribution(dag, frame, "volume", *REF, *AN)

    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-9


def test_unexplained_equals_minus_cov_ref():
    """The leak: for an exact identity, unexplained == -cov_ref exactly."""
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=2.0, cov_an=1.0)
    cov_ref = window_cov(frame, *REF)

    result = run_rca(dag, frame, {}, "volume", *REF, *AN)

    assert abs(result["nodes"]["volume"]["unexplained"] - (-cov_ref)) < 1e-6


def test_zero_gap_nonzero_attribution():
    """Means equal and cov_an == cov_ref == K != 0: the node did not move at
    all, yet the factors receive attributions summing to K, cancelled by an
    equal-and-opposite unexplained."""
    K = 2.0
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=K, cov_an=K)

    sh = shapley_attribution(dag, frame, "volume", *REF, *AN)
    result = run_rca(dag, frame, {}, "volume", *REF, *AN)
    node = result["nodes"]["volume"]

    assert abs(node["gap"]) < 1e-9
    assert abs(sum(sh["attribution"].values()) - K) < 1e-3
    assert abs(node["unexplained"] + K) < 1e-3


def test_leak_linear_in_cov_ref():
    """Sweeping the reference covariance with everything else fixed, the
    unexplained residual tracks -cov_ref with slope -1 and intercept 0."""
    dag = Parser(YAML).dag
    ks = [0.0, 0.5, 1.0, 2.0, 4.0]
    leaks = []
    for k in ks:
        frame = make_frame(cov_ref=k, cov_an=1.0)
        result = run_rca(dag, frame, {}, "volume", *REF, *AN)
        leaks.append(result["nodes"]["volume"]["unexplained"])

    slope, intercept = np.polyfit(ks, leaks, 1)
    assert abs(slope - (-1.0)) < 1e-6
    assert abs(intercept) < 1e-6

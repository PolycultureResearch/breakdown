"""Covariance symmetry of per-day Shapley attribution (roadmap 1.8).

`shapley_attribution` evaluates BOTH windows per-day and decomposes each
parent's attribution as `means + covariance_analysis - covariance_reference`
(three exact Shapley games that telescope to `actual - baseline`). For an
exact per-day identity the reconstruction matches the node's own series in
both windows, so `run_rca`'s `unexplained` is pure measurement residual —
exactly 0 here — and a covariance *shift* between windows is attributed to
the parents while an unchanged covariance contributes nothing.

The frames pin each window's means exactly while making its covariance
tunable: with `d` a fixed zero-mean pattern and `B = mu + s*d`,
`C = mu + sign(K)*s*d`, the window means stay `mu` for any `s` while
cov(B, C) = K exactly (population, ddof=0). That isolates covariance from
means and from any grain/reindex leakage.

The pre-fix characterization versions of these tests (asserting
`unexplained == -cov_ref`) live in the commit that introduced this file.
"""

import numpy as np
import pandas as pd

from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.parser import Parser
from tests.synthetic import win

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
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=60),
            "orders": orders,
            "rate": rate,
            "volume": orders * rate,
        }
    )


def test_efficiency_vs_formula_gap():
    """Shapley efficiency holds against the formula's own gap: the
    attributions sum exactly to actual - baseline, and each attribution is
    the sum of its decomposition parts."""
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=2.0, cov_an=1.0)

    result = shapley_attribution(dag, frame, "volume", **win(REF, AN))

    assert abs(sum(result["attribution"].values()) - result["gap"]) < 1e-9
    for p, parts in result["decomposition"].items():
        recomposed = parts["means"] + parts["covariance_analysis"] - parts["covariance_reference"]
        assert abs(result["attribution"][p] - recomposed) < 1e-12


def test_nothing_moved_nothing_attributed():
    """Means equal and cov_an == cov_ref == K != 0: the node did not move, so
    nothing is attributed and nothing is unexplained."""
    K = 2.0
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=K, cov_an=K)

    sh = shapley_attribution(dag, frame, "volume", **win(REF, AN))
    result = run_rca(dag, frame, {}, "volume", **win(REF, AN))
    node = result["nodes"]["volume"]

    assert abs(node["gap"]) < 1e-9
    assert abs(node["unexplained"]) < 1e-9
    # Exact path: attributions are identically zero (every game's baseline
    # equals its actual up to the shared pattern).
    for phi in sh["attribution"].values():
        assert abs(phi) < 1e-9
    # Bootstrap path: resampled windows perturb means and covariances, so
    # estimates only vanish approximately.
    for c in node["contributions"]:
        assert abs(c["estimate"]) < 0.5


def test_mean_shift_recovered_covariance_cancels():
    """Means move with the covariance held fixed: attributions are the
    classic symmetric means bridge and the covariance parts cancel exactly."""
    K = 2.0
    mu_b_an, mu_c_an = 12.0, 4.0
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=K, cov_an=K, mu_b_an=mu_b_an, mu_c_an=mu_c_an)

    sh = shapley_attribution(dag, frame, "volume", **win(REF, AN))
    result = run_rca(dag, frame, {}, "volume", **win(REF, AN))

    expected_orders = (mu_b_an - MU_B) * (MU_C + mu_c_an) / 2
    expected_rate = (mu_c_an - MU_C) * (MU_B + mu_b_an) / 2
    assert abs(sh["attribution"]["orders"] - expected_orders) < 1e-9
    assert abs(sh["attribution"]["rate"] - expected_rate) < 1e-9
    assert abs(result["nodes"]["volume"]["unexplained"]) < 1e-9


def test_covariance_shift_attributed_symmetrically():
    """The covariance moves with means held fixed: the node's gap IS the
    covariance delta, it is attributed to the parents (half each for a
    product), and unexplained stays zero."""
    cov_ref, cov_an = 2.0, -1.0
    delta = cov_an - cov_ref
    dag = Parser(YAML).dag
    frame = make_frame(cov_ref=cov_ref, cov_an=cov_an)

    sh = shapley_attribution(dag, frame, "volume", **win(REF, AN))
    result = run_rca(dag, frame, {}, "volume", **win(REF, AN))
    node = result["nodes"]["volume"]

    assert abs(node["gap"] - delta) < 1e-9
    assert abs(sum(sh["attribution"].values()) - delta) < 1e-9
    for phi in sh["attribution"].values():
        assert abs(phi - delta / 2) < 1e-9
    assert abs(node["unexplained"]) < 1e-9


def test_unexplained_is_measurement_residual_only():
    """Sweeping the reference covariance no longer leaks into unexplained:
    it stays 0 for an exact identity regardless of cov_ref."""
    dag = Parser(YAML).dag
    for k in [0.0, 0.5, 1.0, 2.0, 4.0]:
        frame = make_frame(cov_ref=k, cov_an=1.0)
        result = run_rca(dag, frame, {}, "volume", **win(REF, AN))
        assert abs(result["nodes"]["volume"]["unexplained"]) < 1e-9


def test_two_level_decomposition_separates_means_and_comovement():
    """The two-level view: per-parent decomposition (means + comovement =
    estimate, exactly per replicate) and the node-level interaction row. A
    pure covariance shift lands in the interaction, not the means bridge; a
    pure mean shift leaves the interaction at zero."""
    dag = Parser(YAML).dag

    # Covariance shift only (means fixed): gap = cov_an - cov_ref = -3.
    frame = make_frame(cov_ref=2.0, cov_an=-1.0)
    node = run_rca(dag, frame, {}, "volume", **win(REF, AN))["nodes"]["volume"]
    total_means = 0.0
    for c in node["contributions"]:
        parts = c["decomposition"]
        assert (
            abs(parts["means"]["estimate"] + parts["comovement"]["estimate"] - c["estimate"]) < 1e-9
        )
        assert abs(parts["means"]["estimate"]) < 0.5  # bootstrap noise only
        total_means += parts["means"]["estimate"]
    # Structural: interaction == sum of per-parent comovement parts.
    comovement_sum = sum(
        c["decomposition"]["comovement"]["estimate"] for c in node["contributions"]
    )
    assert abs(node["interaction"]["estimate"] - comovement_sum) < 1e-9
    # The interaction carries the (covariance-driven) gap; the block
    # bootstrap attenuates resampled covariance somewhat, so assert sign and
    # magnitude rather than equality.
    assert np.sign(node["interaction"]["estimate"]) == np.sign(node["gap"])
    assert abs(node["interaction"]["estimate"]) > 0.5 * abs(node["gap"])
    assert node["interaction"]["ci_95"] is not None

    # Mean shift only (covariance fixed): the interaction row is ~0.
    frame = make_frame(cov_ref=2.0, cov_an=2.0, mu_b_an=12.0, mu_c_an=4.0)
    node = run_rca(dag, frame, {}, "volume", **win(REF, AN))["nodes"]["volume"]
    assert abs(node["interaction"]["estimate"]) < 0.5
    # And the means bridge carries the gap.
    total_means = sum(c["decomposition"]["means"]["estimate"] for c in node["contributions"])
    assert abs(total_means - node["gap"]) < 0.15 * abs(node["gap"])

"""Fit numerics (grill 2026-10-05, H1).

H1: "is this series constant?" was `std() == 0`, and most decimals are not
representable — `pd.Series([4.99] * 90).std()` is 8.9e-16. A parent held at
4.99 survived the constant-parent drop, was z-scored into rounding noise with
`x_std` ~1e-15, and RCA published its contribution as -1.6e13 under
`fit_quality: ok`. One shared, scale-relative test (`effectively_constant`)
now answers the question for the drop, for `_normalize` and for the formula
residual.

Most of this file never fits; the tests that do are marked slow one by one.
"""

import numpy as np
import pandas as pd
import pytest

from breakdown.engine.model import _normalize, _prepare_series, fit_metric
from breakdown.engine.rca import run_rca
from breakdown.engine.stats import effectively_constant
from breakdown.parser import Parser

# The reviewer's three, plus one whose rounding lands on a different period
# (1/3 of a billion) and the two exactly-representable cases the old test
# already caught — the helper must not have traded one set for the other.
HELD_VALUES = [4.99, 0.1, 19.99, 1e9 / 3, -3.3, 5.0, 0.0]

PRICE_YAML = """
metrics:
  - name: x
    source: a.b.x
  - name: price
    source: a.b.price
  - name: y
    source: a.b.y
    parents: [x, price]
"""

RATE_PARENT_YAML = """
metrics:
  - name: x
    source: a.b.x
  - name: rate
    source: a.b.rate
  - name: y
    source: a.b.y
    parents: [x, rate]
"""

IDENTITY_YAML = """
metrics:
  - name: a
    source: a.b.a
  - name: b
    source: a.b.b
  - name: c
    source: a.b.c
  - name: total
    source: a.b.total
    parents: [a, b, c]
    formula: "a + b + c"
"""

REF = ("2024-03-01", "2024-03-31")
AN = ("2024-04-01", "2024-05-09")


def _held_price_frame(held: float, n: int = 130, seed: int = 7) -> pd.DataFrame:
    """The review's repro: `price` is held for the whole fit window and moves
    only inside the analysis window. Truth is y = 0.5·x + noise — price has no
    effect at all, so any contribution attributed to it is invented."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n)
    x = 100 + rng.normal(0, 4, n)
    price = np.full(n, held)
    price[dates >= AN[0]] = held + 0.5
    y = 0.5 * x + rng.normal(0, 1, n)
    return pd.DataFrame({"date": dates, "x": x, "price": price, "y": y})


def _fit_window(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["date"] < AN[0]].reset_index(drop=True)


# -- the shared helper --------------------------------------------------------


@pytest.mark.parametrize("held", HELD_VALUES)
def test_a_held_value_is_constant_whatever_its_float_representation(held):
    assert effectively_constant(np.full(90, held))
    # The path that actually bit: a pandas column, lag-shifted and trimmed.
    assert effectively_constant(pd.Series([held] * 90).shift(2).iloc[2:].values)


def test_the_old_exact_test_really_did_miss_these():
    """Pins the premise, so nobody 'simplifies' the helper back to `std == 0`."""
    assert pd.Series([4.99] * 90).std() != 0
    assert pd.Series([5.0] * 90).std() == 0


def test_a_small_magnitude_series_that_really_varies_is_not_constant():
    """The tolerance is relative to the series' own level. An absolute epsilon
    large enough to catch 19.99's rounding would swallow a rate like this."""
    rng = np.random.default_rng(0)
    assert not effectively_constant(rng.uniform(0.0010, 0.0012, 90))
    assert not effectively_constant(np.array([0.0010] * 89 + [0.0012]))
    assert not effectively_constant(rng.uniform(1e-9, 1.2e-9, 90))


def test_a_large_series_with_ordinary_variation_is_not_constant():
    rng = np.random.default_rng(0)
    assert not effectively_constant(1e9 + rng.normal(0, 50, 90))
    # One real step inside an otherwise flat window is variation.
    assert not effectively_constant(np.array([4.99] * 89 + [5.49]))


def test_accumulated_rounding_on_a_flat_aggregate_is_constant():
    """What a warehouse SUM of a flat quantity looks like: flat to ~1e-13."""
    rng = np.random.default_rng(0)
    assert effectively_constant(1e6 * (1 + 1e-13 * rng.normal(size=90)))


def test_level_overrides_the_yardstick_for_a_residual():
    residue = np.array([0.0, 2e-12, -1e-12, 3e-12])
    assert not effectively_constant(residue)  # 100%+ of its own magnitude
    assert effectively_constant(residue, level=5_000.0)  # nothing, next to the metric


def test_a_non_finite_series_is_not_called_constant():
    """It is undefined, which is a different refusal with its own words."""
    assert not effectively_constant(np.array([4.99, np.nan, 4.99]))
    assert not effectively_constant(np.array([np.inf, np.inf]))


# -- the constant-parent drop and _normalize ----------------------------------


@pytest.mark.parametrize("held", [4.99, 0.1, 19.99])
def test_a_parent_held_at_an_inexact_decimal_is_dropped_by_name(held):
    dag = Parser(PRICE_YAML).dag
    frame = _fit_window(_held_price_frame(held))
    _, X, scale, _, _, x_stds, _, fitted, dropped = _prepare_series(
        dag.nodes["y"]["definition"], list(dag.predecessors("y")), frame, "y"
    )
    assert fitted == ["x"]
    assert X.shape[1] == 1 and x_stds.shape == (1,) and scale.shape == (1,)
    (record,) = dropped
    assert record["parent"] == "price"
    assert "zero variance over fit window 2024-01-01..2024-03-31" in record["reason"]
    # The reason still reads as the number a person typed, not 4.9900000000001.
    assert f"(held at {held:g})" in record["reason"]


def test_a_small_magnitude_varying_parent_is_kept():
    dag = Parser(RATE_PARENT_YAML).dag
    rng = np.random.default_rng(1)
    n = 90
    rate = rng.uniform(0.0010, 0.0012, n)
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x": 100 + rng.normal(0, 4, n),
            "rate": rate,
            "y": rng.normal(0, 1, n),
        }
    )
    _, X, _, _, _, x_stds, _, fitted, dropped = _prepare_series(
        dag.nodes["y"]["definition"], list(dag.predecessors("y")), frame, "y"
    )
    assert fitted == ["x", "rate"] and dropped == []
    assert x_stds[1] == pytest.approx(pd.Series(rate).std())
    assert X[:, 1].std(ddof=1) == pytest.approx(1.0)


@pytest.mark.parametrize("held", [4.99, 0.1, 19.99])
def test_normalize_refuses_a_held_series_instead_of_dividing_by_rounding(held):
    """The target's own series goes through `_normalize` with no drop in front
    of it; before, this returned `std` ~1e-15 and a column of noise."""
    with pytest.raises(ValueError, match="'y' has zero variance"):
        _normalize(pd.Series([held] * 90, name="y"))


def test_normalize_still_normalizes_a_small_varying_series():
    rate = pd.Series(np.random.default_rng(2).uniform(0.0010, 0.0012, 90), name="r")
    z, mean, std = _normalize(rate)
    assert mean == pytest.approx(rate.mean()) and std == pytest.approx(rate.std())
    assert z.std(ddof=1) == pytest.approx(1.0)


def test_an_exact_identitys_residual_is_refused_whether_or_not_it_rounds():
    """`a + b + c` against a stored total leaves float residue on some rows
    and exactly 0.0 on others. Only an all-zero residual used to be refused,
    so whether an identity node could be 'fitted' — to pure rounding noise,
    with a `y_std` of 1e-13 — depended on the arithmetic's luck."""
    dag = Parser(IDENTITY_YAML).dag
    rng = np.random.default_rng(3)
    n = 90
    a, b, c = (rng.uniform(1_000, 9_000, n) for _ in range(3))
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n), "a": a, "b": b, "c": c})
    frame["total"] = (frame["c"] + frame["b"]) + frame["a"]  # another summation order
    residue = frame["total"] - (frame["a"] + frame["b"] + frame["c"])
    assert residue.abs().max() > 0 and residue.std() > 0  # the case the old test let through

    defn, parents = dag.nodes["total"]["definition"], list(dag.predecessors("total"))
    with pytest.raises(ValueError, match="'total_residual' has zero variance"):
        _prepare_series(defn, parents, frame, "total")

    # A residual that is really there — the stored total rounded to cents is
    # ~1e-6 of this metric's level — is still a series to fit.
    frame["total"] = frame["total"].round(2)
    y, X, *_ = _prepare_series(defn, parents, frame, "total")
    assert X is None and np.isfinite(y).all()


# -- end to end ---------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("held", [4.99, 0.1, 19.99])
def test_rca_does_not_rank_a_held_price_as_the_cause(held):
    """The finding itself. Before: `dropped_parents: None`, a `price` row at
    -1.6e13 with share 2.5e13, `unexplained` +1.6e13, `fit_quality: ok`."""
    result = run_rca(
        Parser(PRICE_YAML).dag,
        _held_price_frame(held),
        {},
        "y",
        analysis_start=AN[0],
        analysis_end=AN[1],
        reference_start=REF[0],
        reference_end=REF[1],
        draws=200,
        reference_sensitivity=False,
    )
    y = result["nodes"]["y"]
    assert y["status"] == "ok", y["status_reason"]
    (dropped,) = y["dropped_parents"]
    assert dropped["parent"] == "price"
    assert f"held at {held:g}" in dropped["reason"]
    assert [c["parent"] for c in y["contributions"]] == ["x"]
    # Everything published is on the scale of the data (y moves by ~1).
    (x,) = y["contributions"]
    assert abs(x["estimate"]) < 5 and abs(y["unexplained"]) < 5
    assert all(abs(v) < 10 for v in x["ci_95"])
    assert [r["metric"] for r in result["ranked_causes"]] == ["x"]


@pytest.mark.slow
def test_a_small_magnitude_rate_parent_is_fitted_and_its_coefficient_recovered():
    """The other side of the tolerance: a parent whose whole range is 2e-4 is
    a real regressor. y = 0.5·x + 40000·rate + noise."""
    rng = np.random.default_rng(4)
    n = 120
    x = 100 + rng.normal(0, 4, n)
    rate = rng.uniform(0.0010, 0.0012, n)
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n),
            "x": x,
            "rate": rate,
            "y": 0.5 * x + 40_000 * rate + rng.normal(0, 0.5, n),
        }
    )
    fit = fit_metric(Parser(RATE_PARENT_YAML).dag, frame, "y", draws=300, tune=400, random_seed=0)
    assert fit.parents == ["x", "rate"] and fit.dropped_parents == []
    beta = fit.trace.posterior["beta_raw"].values.reshape(-1, 2).mean(axis=0)
    assert beta[0] == pytest.approx(0.5, abs=0.1)
    assert beta[1] == pytest.approx(40_000, rel=0.2)

"""Slice attribution engine (`breakdown.engine.slices`) and the mock
provider's sliced fetch.

The engine tests build long-format frames directly with seeded generators;
the closed forms make additivity and zero-sum-excess *exact* claims (up to
float tolerance), not statistical ones.
"""
import json

import numpy as np
import pandas as pd
import pytest

from breakdown.data_fetch import MockDataFetcher, SliceNotSupported, WarehouseDataFetcher
from breakdown.engine.slices import slice_attribution
from breakdown.parser import MetricDefinition, Parser

REF = ("2026-01-05", "2026-02-01")   # 4 whole weeks of days
AN = ("2026-02-02", "2026-02-08")    # 1 whole week


def _dates(start, end):
    return pd.date_range(start, end, freq="D")


def _long(frames):
    """{slice_name: Series} -> long [date, slice, value]."""
    return pd.concat(
        [
            pd.DataFrame({"date": s.index, "slice": name, "value": s.to_numpy()})
            for name, s in frames.items()
        ],
        ignore_index=True,
    )


def _flow_defn(**dim_kwargs):
    return MetricDefinition(
        name="signups",
        source="mock.signups",
        dimensions={"region": {"source": "customer__region", **dim_kwargs}},
    )


def _flow_slices(seed=7, boost=None):
    """Three seeded slice series over both windows; `boost` optionally adds a
    level shift to one slice during the analysis window only."""
    rng = np.random.default_rng(seed)
    dates = _dates(REF[0], REF[1]).union(_dates(AN[0], AN[1]))
    n = len(dates)
    frames = {}
    for i, name in enumerate(["amer", "emea", "apac"]):
        level = 200.0 * (i + 1)
        frames[name] = pd.Series(level + rng.normal(0, 5, n), index=dates)
    if boost is not None:
        name, delta = boost
        an_mask = (dates >= pd.Timestamp(AN[0])) & (dates <= pd.Timestamp(AN[1]))
        frames[name] = frames[name] + delta * an_mask.astype(float)
    return frames, dates


def _unsliced(frames, dates, name="signups"):
    total = sum(s.to_numpy() for s in frames.values())
    return pd.DataFrame({"date": dates, name: total})


def test_flow_additivity_and_excess_zero_sum():
    frames, dates = _flow_slices()
    result = slice_attribution(
        _flow_defn(), "region", _long(frames), _unsliced(frames, dates), *REF, *AN
    )
    assert result["attribution_method"] == "slice_sum"
    total_contribution = sum(r["contribution"] for r in result["slices"])
    assert total_contribution == pytest.approx(result["gap"], abs=1e-9)
    assert sum(r["excess"] for r in result["slices"]) == pytest.approx(0.0, abs=1e-9)
    assert result["reconciliation"]["status"] == "ok"
    assert result["ci_status"] == "ok"


def test_localization_recovery():
    """A planted analysis-window drop in one slice is the top excess, with
    high concentration probability."""
    frames, dates = _flow_slices(boost=("emea", -80.0))
    result = slice_attribution(
        _flow_defn(), "region", _long(frames), _unsliced(frames, dates), *REF, *AN
    )
    top = result["slices"][0]
    assert top["value"] == "emea"
    assert top["excess"] < 0
    assert top["prob_concentrated"] > 0.9
    assert top["noise_level"] is False


def _two_slice_defn():
    return MetricDefinition(
        name="churned_mrr",
        source="mock.churned_mrr",
        dimensions={"plan": {"source": "subscription__plan"}},
    )


def _two_slices(boost):
    """Two slices only — the case where |excess| ties exactly."""
    rng = np.random.default_rng(11)
    dates = _dates(REF[0], REF[1]).union(_dates(AN[0], AN[1]))
    n = len(dates)
    frames = {
        "studio": pd.Series(600.0 + rng.normal(0, 8, n), index=dates),
        "professional": pd.Series(400.0 + rng.normal(0, 8, n), index=dates),
    }
    name, delta = boost
    an_mask = (dates >= pd.Timestamp(AN[0])) & (dates <= pd.Timestamp(AN[1]))
    frames[name] = frames[name] + delta * an_mask.astype(float)
    return frames, dates


@pytest.mark.parametrize("delta", [150.0, -150.0])
def test_two_slice_ranking_is_not_a_coin_flip(delta):
    """With two slices `Σ excess = 0` makes the excesses exactly ±x, so ranking
    by |excess| ties and the culprit's position falls to dict order. It must be
    ranked by excess signed toward the gap instead — for a rise, the slice that
    rose beyond its share; for a fall, the one that fell beyond it."""
    frames, dates = _two_slices(boost=("professional", delta))
    result = slice_attribution(
        _two_slice_defn(), "plan", _long(frames),
        _unsliced(frames, dates, name="churned_mrr"), *REF, *AN,
    )
    excesses = [r["excess"] for r in result["slices"]]
    assert abs(excesses[0]) == pytest.approx(abs(excesses[1]), rel=1e-9)  # the tie is real
    assert result["slices"][0]["value"] == "professional"
    # the mover leads regardless of direction; the offsetting slice trails
    assert np.sign(excesses[0]) == np.sign(result["gap"])


def test_null_restraint_uniform_change():
    """A proportional shrink across all slices concentrates nowhere: every
    excess is small relative to the gap."""
    frames, dates = _flow_slices()
    an_mask = (dates >= pd.Timestamp(AN[0])) & (dates <= pd.Timestamp(AN[1]))
    scaled = {
        name: s * (1.0 - 0.2 * an_mask.astype(float)) for name, s in frames.items()
    }
    result = slice_attribution(
        _flow_defn(), "region", _long(scaled), _unsliced(scaled, dates), *REF, *AN
    )
    gap = abs(result["gap"])
    for row in result["slices"]:
        assert abs(row["excess"]) < 0.05 * gap


def test_other_fold_preserves_additivity():
    rng = np.random.default_rng(3)
    dates = _dates(REF[0], REF[1]).union(_dates(AN[0], AN[1]))
    frames = {
        f"v{i}": pd.Series(50.0 * (i + 1) + rng.normal(0, 2, len(dates)), index=dates)
        for i in range(6)
    }
    result = slice_attribution(
        _flow_defn(top_k=3), "region", _long(frames), _unsliced(frames, dates), *REF, *AN
    )
    values = [r["value"] for r in result["slices"]]
    assert "__other__" in values
    other = next(r for r in result["slices"] if r["value"] == "__other__")
    assert other["n_values"] == 3
    total_contribution = sum(r["contribution"] for r in result["slices"])
    assert total_contribution == pytest.approx(result["gap"], abs=1e-9)


def test_reconciliation_flags_nonadditive_dimension():
    frames, dates = _flow_slices()
    inflated = _unsliced(frames, dates)
    inflated["signups"] *= 1.05  # slices now sum to ~95% of the metric
    result = slice_attribution(
        _flow_defn(), "region", _long(frames), inflated, *REF, *AN
    )
    assert result["reconciliation"]["status"] == "discrepant"
    assert any("partition" in c for c in result["caveats"])


def test_pinned_values_and_absent_pin_caveat():
    frames, dates = _flow_slices()
    result = slice_attribution(
        _flow_defn(values=["emea", "nowhere"]), "region",
        _long(frames), _unsliced(frames, dates), *REF, *AN,
    )
    kept = {r["value"] for r in result["slices"]}
    assert kept == {"emea", "__other__"}
    assert any("nowhere" in c for c in result["caveats"])


def test_determinism():
    frames, dates = _flow_slices()
    args = (_flow_defn(), "region", _long(frames), _unsliced(frames, dates), *REF, *AN)
    a = slice_attribution(*args)
    b = slice_attribution(*args)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --- rate (mix vs within) tests ---


def _rate_defn():
    return MetricDefinition(
        name="conversion_rate",
        source="mock.conversion_rate",
        kind="rate",
        dimensions={
            "region": {"source": "customer__region", "weight": "trial_starts"}
        },
    )


def _rate_inputs(shift_shares=False, new_slice=False, rate_drop=None):
    """Weights + per-slice rates whose per-date blend defines the unsliced
    rate, so reconciliation is exact by construction."""
    dates = _dates(REF[0], REF[1]).union(_dates(AN[0], AN[1]))
    an_mask = ((dates >= pd.Timestamp(AN[0])) & (dates <= pd.Timestamp(AN[1]))).astype(float)
    n = len(dates)
    weights = {
        "amer": pd.Series(np.full(n, 600.0), index=dates),
        "emea": pd.Series(np.full(n, 300.0), index=dates),
        "apac": pd.Series(np.full(n, 100.0), index=dates),
    }
    if shift_shares:
        # Traffic moves amer -> apac in the analysis window; rates unchanged.
        weights["amer"] = weights["amer"] - 300.0 * an_mask
        weights["apac"] = weights["apac"] + 300.0 * an_mask
    if new_slice:
        weights["latam"] = pd.Series(200.0 * an_mask, index=dates)
    rates = {
        "amer": pd.Series(np.full(n, 0.20), index=dates),
        "emea": pd.Series(np.full(n, 0.10), index=dates),
        "apac": pd.Series(np.full(n, 0.05), index=dates),
    }
    if new_slice:
        rates["latam"] = pd.Series(np.full(n, 0.02), index=dates)
    if rate_drop is not None:
        name, delta = rate_drop
        rates[name] = rates[name] - delta * an_mask
    w_arr = np.stack([weights[k].to_numpy() for k in weights])
    r_arr = np.stack([rates[k].to_numpy() for k in weights])
    blend = (w_arr * r_arr).sum(axis=0) / w_arr.sum(axis=0)
    unsliced = pd.DataFrame({"date": dates, "conversion_rate": blend})
    return _long(rates), _long(weights), unsliced


def test_rate_bennet_exactness_on_mix_shift():
    sliced, weight_sliced, unsliced = _rate_inputs(shift_shares=True)
    result = slice_attribution(
        _rate_defn(), "region", sliced, unsliced, *REF, *AN,
        weight_sliced=weight_sliced,
    )
    assert result["attribution_method"] == "slice_blend"
    total = sum(r["within"] + r["mix"] for r in result["slices"])
    assert total == pytest.approx(result["gap"], abs=1e-9)
    # Rates never moved: the gap is pure mix.
    for row in result["slices"]:
        assert row["within"] == pytest.approx(0.0, abs=1e-9)
    assert result["mix_total"]["estimate"] == pytest.approx(result["gap"], rel=0.05)
    assert result["reconciliation"]["status"] == "ok"


def test_rate_within_localizes_a_rate_drop():
    sliced, weight_sliced, unsliced = _rate_inputs(rate_drop=("emea", 0.04))
    result = slice_attribution(
        _rate_defn(), "region", sliced, unsliced, *REF, *AN,
        weight_sliced=weight_sliced,
    )
    top = result["slices"][0]
    assert top["value"] == "emea"
    assert top["within"] < 0
    assert top["mix"] == pytest.approx(0.0, abs=1e-9)


def test_rate_new_slice_contributes_through_mix():
    sliced, weight_sliced, unsliced = _rate_inputs(new_slice=True)
    result = slice_attribution(
        _rate_defn(), "region", sliced, unsliced, *REF, *AN,
        weight_sliced=weight_sliced,
    )
    latam = next(r for r in result["slices"] if r["value"] == "latam")
    assert latam["within"] == pytest.approx(0.0, abs=1e-9)
    assert latam["mix"] != 0.0
    assert latam["share_reference"] == pytest.approx(0.0)
    total = sum(r["within"] + r["mix"] for r in result["slices"])
    assert total == pytest.approx(result["gap"], abs=1e-9)


def test_rate_requires_weight_frame():
    sliced, _, unsliced = _rate_inputs()
    with pytest.raises(ValueError, match="weight metric"):
        slice_attribution(_rate_defn(), "region", sliced, unsliced, *REF, *AN)


def test_unknown_dimension_raises():
    frames, dates = _flow_slices()
    with pytest.raises(ValueError, match="declares no dimension 'geo'"):
        slice_attribution(
            _flow_defn(), "geo", _long(frames), _unsliced(frames, dates), *REF, *AN
        )


# --- mock provider sliced fetch ---

_TREE_YAML = """
metrics:
  - name: signups
    source: mock.signups
    dimensions:
      region: customer__region
  - name: trial_starts
    source: mock.trial_starts
    parents: [signups]
    dimensions:
      region: customer__region
  - name: conversion_rate
    source: mock.conversion_rate
    kind: rate
    dimensions:
      region: { source: customer__region, weight: trial_starts }
"""


def test_mock_slices_sum_to_series():
    parser = Parser(_TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    df = fetcher.fetch_metric("signups", "2026-01-01", "2026-03-01")
    sliced = fetcher.fetch_metric_sliced(
        "signups", "customer__region", "2026-01-01", "2026-03-01"
    )
    summed = sliced.groupby("date")["value"].sum()
    base = df.set_index("date")["signups"]
    assert np.allclose(summed.reindex(base.index).to_numpy(), base.to_numpy())


def test_mock_sliced_subwindow_consistent_with_startup_fetch():
    """A slice fetch over a sub-window must split the same numbers the wide
    startup fetch produced (the covering-cache path)."""
    parser = Parser(_TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    wide = fetcher.fetch_metric("signups", "2026-01-01", "2026-03-01")
    sliced = fetcher.fetch_metric_sliced(
        "signups", "customer__region", "2026-02-02", "2026-02-08"
    )
    summed = sliced.groupby("date")["value"].sum()
    base = wide.set_index("date")["signups"].loc[summed.index]
    assert np.allclose(summed.to_numpy(), base.to_numpy())


def test_mock_rate_blend_reconciles_with_weight_slices():
    parser = Parser(_TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    fetcher.fetch_metric("conversion_rate", "2026-01-01", "2026-03-01")
    rate_sliced = fetcher.fetch_metric_sliced(
        "conversion_rate", "customer__region", "2026-01-01", "2026-03-01",
        kind="rate",
    )
    weight_sliced = fetcher.fetch_metric_sliced(
        "trial_starts", "customer__region", "2026-01-01", "2026-03-01"
    )
    rate = fetcher.fetch_metric("conversion_rate", "2026-01-01", "2026-03-01")
    r = rate_sliced.pivot(index="date", columns="slice", values="value")
    w = weight_sliced.pivot(index="date", columns="slice", values="value")
    blend = (r * w).sum(axis=1) / w.sum(axis=1)
    base = rate.set_index("date")["conversion_rate"]
    assert np.allclose(blend.reindex(base.index).to_numpy(), base.to_numpy())


def test_mock_slice_determinism():
    parser = Parser(_TREE_YAML)
    a = MockDataFetcher(dag=parser.dag).fetch_metric_sliced(
        "signups", "customer__region", "2026-01-01", "2026-02-01"
    )
    b = MockDataFetcher(dag=parser.dag).fetch_metric_sliced(
        "signups", "customer__region", "2026-01-01", "2026-02-01"
    )
    pd.testing.assert_frame_equal(a, b)


def test_warehouse_slicing_not_supported():
    fetcher = WarehouseDataFetcher(
        host="h", http_path="p", token="t", metric_sql={}
    )
    with pytest.raises(SliceNotSupported, match="does not support dimensional"):
        fetcher.fetch_metric_sliced("signups", "customer__region", "2026-01-01", "2026-02-01")


def test_near_zero_reference_total_withholds_the_interval_not_a_500():
    """Replicates whose reference total resamples to ~zero have no defined
    baseline share, so their excess is NaN. That used to poison the whole
    percentile and reach Starlette's `allow_nan=False` as an unhandled 500
    (roadmap C8). Sparse counts hit it easily."""
    import numpy as np

    from breakdown.engine.slices import _excess_fields

    # Mostly-undefined replicates: interval withheld rather than computed off
    # the handful that survived.
    mostly_nan = np.full(500, np.nan)
    mostly_nan[:100] = np.random.default_rng(0).normal(0, 1, 100)
    out = _excess_fields(mostly_nan, single_period=False)
    assert out["ci_95"] is None
    assert out["prob_concentrated"] is None

    # A minority undefined: the interval is computed from the finite ones and
    # every reported number is JSON-serializable (no NaN reaches the response).
    some_nan = np.random.default_rng(1).normal(0, 1, 500)
    some_nan[:50] = np.nan
    out = _excess_fields(some_nan, single_period=False)
    assert out["ci_95"] is not None
    assert all(np.isfinite(v) for v in out["ci_95"])
    assert np.isfinite(out["prob_concentrated"])

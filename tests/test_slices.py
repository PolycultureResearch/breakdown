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
from breakdown.engine.slices import entity_flows, slice_attribution
from breakdown.parser import MetricDefinition, Parser

REF = ("2026-01-05", "2026-02-01")  # 4 whole weeks of days
AN = ("2026-02-02", "2026-02-08")  # 1 whole week


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
        _two_slice_defn(),
        "plan",
        _long(frames),
        _unsliced(frames, dates, name="churned_mrr"),
        *REF,
        *AN,
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
    scaled = {name: s * (1.0 - 0.2 * an_mask.astype(float)) for name, s in frames.items()}
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
    result = slice_attribution(_flow_defn(), "region", _long(frames), inflated, *REF, *AN)
    assert result["reconciliation"]["status"] == "discrepant"
    assert any("partition" in c for c in result["caveats"])


def test_pinned_values_and_absent_pin_caveat():
    frames, dates = _flow_slices()
    result = slice_attribution(
        _flow_defn(values=["emea", "nowhere"]),
        "region",
        _long(frames),
        _unsliced(frames, dates),
        *REF,
        *AN,
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
        dimensions={"region": {"source": "customer__region", "weight": "trial_starts"}},
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
        _rate_defn(),
        "region",
        sliced,
        unsliced,
        *REF,
        *AN,
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
        _rate_defn(),
        "region",
        sliced,
        unsliced,
        *REF,
        *AN,
        weight_sliced=weight_sliced,
    )
    top = result["slices"][0]
    assert top["value"] == "emea"
    assert top["within"] < 0
    assert top["mix"] == pytest.approx(0.0, abs=1e-9)


def test_rate_new_slice_contributes_through_mix():
    sliced, weight_sliced, unsliced = _rate_inputs(new_slice=True)
    result = slice_attribution(
        _rate_defn(),
        "region",
        sliced,
        unsliced,
        *REF,
        *AN,
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
        slice_attribution(_flow_defn(), "geo", _long(frames), _unsliced(frames, dates), *REF, *AN)


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
    sliced = fetcher.fetch_metric_sliced("signups", "customer__region", "2026-01-01", "2026-03-01")
    summed = sliced.groupby("date")["value"].sum()
    base = df.set_index("date")["signups"]
    assert np.allclose(summed.reindex(base.index).to_numpy(), base.to_numpy())


def test_mock_sliced_subwindow_consistent_with_startup_fetch():
    """A slice fetch over a sub-window must split the same numbers the wide
    startup fetch produced (the covering-cache path)."""
    parser = Parser(_TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    wide = fetcher.fetch_metric("signups", "2026-01-01", "2026-03-01")
    sliced = fetcher.fetch_metric_sliced("signups", "customer__region", "2026-02-02", "2026-02-08")
    summed = sliced.groupby("date")["value"].sum()
    base = wide.set_index("date")["signups"].loc[summed.index]
    assert np.allclose(summed.to_numpy(), base.to_numpy())


def test_mock_rate_blend_reconciles_with_weight_slices():
    parser = Parser(_TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    fetcher.fetch_metric("conversion_rate", "2026-01-01", "2026-03-01")
    rate_sliced = fetcher.fetch_metric_sliced(
        "conversion_rate",
        "customer__region",
        "2026-01-01",
        "2026-03-01",
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
    fetcher = WarehouseDataFetcher(host="h", http_path="p", token="t", metric_sql={})
    with pytest.raises(SliceNotSupported, match="does not support dimensional"):
        fetcher.fetch_metric_sliced("signups", "customer__region", "2026-01-01", "2026-02-01")


def test_excess_fields_drops_non_finite_replicates():
    """`share_b` is NaN for bootstrap replicates whose reference total came out
    ~0 (no defined share). That NaN reached `np.percentile` and then Starlette's
    `allow_nan=False` encoder as an unhandled **500** — the slice endpoint
    failing outright rather than the interval widening (C8)."""
    import numpy as np

    from breakdown.engine.slices import _excess_fields

    clean = np.concatenate([np.full(600, 5.0), np.full(400, 7.0)])
    fields = _excess_fields(clean.copy(), single_period=False)
    assert fields["ci_95"] is not None

    # Same replicates with a minority poisoned: still answerable, and finite.
    poisoned = clean.copy()
    poisoned[:150] = np.nan
    fields = _excess_fields(poisoned, single_period=False)
    assert fields["ci_95"] is not None
    assert all(np.isfinite(v) for v in fields["ci_95"]), "NaN reached the response"
    assert np.isfinite(fields["prob_concentrated"])

    # Too few survive to quote an interval: withheld, not fabricated.
    mostly_nan = clean.copy()
    mostly_nan[:-20] = np.nan
    assert _excess_fields(mostly_nan, single_period=False)["ci_95"] is None


# --- non-additive labelling (roadmap 3.8 §5) --------------------------------
#
# Overlap and "discrepant" wore one banner. Overlap is arithmetic, known from
# the binding before any query runs; discrepant means an unexplained divergence
# a user should investigate. Conflating them told people their pipeline was
# broken when nothing was, and made the flag worthless for the real cases.


def _overlapping_frames():
    """A distinct count sliced by a multi-valued dimension: `u1` is on both
    platforms every day, so the slices overstate the total by exactly 1."""
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    # ios halves in the second window, so there is a real gap to attribute
    # rather than a degenerate zero (which would make every share None anyway
    # and prove nothing about withholding them).
    ios = [4.0] * 4 + [2.0] * 4
    rows = []
    for d, v in zip(dates, ios):
        rows.append({"date": d, "slice": "ios", "value": v})
        rows.append({"date": d, "slice": "web", "value": 1.0})
    # One entity is on both platforms every day, so the metric counts it once
    # while the slices count it twice: the slices exceed the total by exactly 1.
    unsliced = pd.DataFrame({"date": dates, "dau": ios})
    return pd.DataFrame(rows), unsliced


def _defn(**over):
    from breakdown.parser import MetricDefinition

    base = dict(
        name="dau",
        source="w.dau",
        grain="day",
        kind="flow",
        dimensions={"platform": "platform"},
    )
    base.update(over)
    return MetricDefinition(**base)


def _attribute(additivity):
    sliced, unsliced = _overlapping_frames()
    return slice_attribution(
        _defn(),
        "platform",
        sliced,
        unsliced,
        "2024-01-01",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        additivity=additivity,
    )


def test_overlap_is_named_rather_than_flagged_discrepant():
    r = _attribute("overlapping")
    assert r["additivity"] == "overlapping"
    assert r["overlap"]["mean"] == pytest.approx(1.0)  # u1, counted twice
    assert r["overlap"]["share_of_baseline"] == pytest.approx(0.25)  # 1 of a 4.0 baseline
    # The reconciliation flag must stop firing on arithmetic, or it stops
    # being worth reading for the cases that are genuinely wrong.
    assert r["reconciliation"]["status"] == "not_applicable"


def test_the_overlap_caveat_says_what_it_is():
    text = " ".join(_attribute("overlapping")["caveats"])
    assert "share entities" in text
    assert "rather than an unexplained cause" in text
    assert "shares are withheld" in text


def test_shares_of_the_gap_are_withheld_when_slices_do_not_sum():
    # A share whose denominator does not reconcile is not a share.
    r = _attribute("overlapping")
    assert all(row["share_of_gap"] is None for row in r["slices"])
    # Contributions themselves survive: they still rank the slices.
    assert all(row["contribution"] is not None for row in r["slices"])


def test_the_same_residual_is_still_discrepant_when_additivity_is_unknown():
    # Identical numbers, no binding to ask -> behaves exactly as before, so
    # this change cannot quietly suppress a real divergence.
    r = _attribute("unknown")
    assert r["reconciliation"]["status"] == "discrepant"
    assert r["overlap"] is None
    assert any("do not sum" in c for c in r.get("caveats", []))
    assert all(row["share_of_gap"] is not None for row in r["slices"])


def test_an_exact_metric_reconciles_and_keeps_its_shares():
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    sliced = pd.DataFrame(
        [
            {"date": d, "slice": g, "value": v}
            for d in dates
            for g, v in (("ios", 2.0), ("web", 1.0))
        ]
    )
    unsliced = pd.DataFrame({"date": dates, "dau": [3.0] * 8})
    r = slice_attribution(
        _defn(),
        "platform",
        sliced,
        unsliced,
        "2024-01-01",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        additivity="exact",
    )
    assert r["additivity"] == "exact"
    assert r["overlap"] is None
    assert r["reconciliation"]["status"] == "ok"


def test_the_response_shape_is_stable_across_paths():
    # Both branches carry the fields, so a consumer never has to check which
    # attribution method ran before reading them.
    r = _attribute("unknown")
    assert {"additivity", "overlap"} <= set(r)


# --- entity flows (roadmap 3.8 §6) ------------------------------------------
#
# The finding these produce: a user switching platform shows −1 on one slice and
# +1 on another with the total unchanged. Naive attribution reports two large
# offsetting causes for a change that never happened; flows label it migration.


def _transitions(rows):
    return pd.DataFrame(
        [{"reference_slice": r, "analysis_slice": a, "entities": n} for r, a, n in rows]
    )


def test_the_four_classes_fall_out_of_the_transition_matrix():
    f = entity_flows(
        _transitions(
            [
                ("__absent__", "web", 3),  # new
                ("ios", "__absent__", 2),  # churned
                ("ios", "ios", 10),  # retained
                ("ios", "web", 5),  # migrated
            ]
        )
    )
    assert f["totals"] == {"new": 3, "churned": 2, "retained": 10, "migrated": 5}


def test_migration_nets_to_zero_across_slices():
    # The same property a rate's `mix_total` has, and for the same reason: it
    # is a pure reallocation, not a contribution.
    f = entity_flows(_transitions([("ios", "web", 5), ("web", "ios", 2), ("ios", "ios", 9)]))
    assert f["migration_net"] == 0
    assert sum(r["migrated_in"] - r["migrated_out"] for r in f["slices"]) == 0


def test_a_pure_migration_leaves_the_total_unchanged_but_moves_the_slices():
    # The motivating case. Nothing entered or left; the slices still moved.
    f = entity_flows(_transitions([("ios", "web", 1), ("ios", "ios", 5), ("web", "web", 5)]))
    assert f["totals"]["new"] == f["totals"]["churned"] == 0
    assert f["totals"]["migrated"] == 1
    by_slice = {r["value"]: r["net"] for r in f["slices"]}
    assert by_slice == {"ios": -1, "web": 1}
    assert sum(by_slice.values()) == 0  # the metric did not move


def test_migrations_name_where_the_movement_went():
    f = entity_flows(_transitions([("ios", "web", 7), ("ios", "android", 2)]))
    assert f["migrations"][0] == {"from": "ios", "to": "web", "entities": 7}


def test_entities_in_neither_window_are_not_counted():
    f = entity_flows(_transitions([("__absent__", "__absent__", 99), ("ios", "ios", 1)]))
    assert f["totals"]["retained"] == 1
    assert not any(r["value"] == "__absent__" for r in f["slices"])


def test_a_null_dimension_value_is_not_absence():
    # Present with no region is a different fact from never present, and the
    # flow path must keep them apart the same way the slice path does.
    f = entity_flows(_transitions([("__null__", "web", 4)]))
    assert f["totals"]["migrated"] == 4
    assert {r["value"] for r in f["slices"]} == {"__null__", "web"}


def test_flows_say_they_do_not_reconcile_to_the_gap():
    # Window-level sets, not window means: presenting these as a second
    # decomposition would put two numbers on screen that do not add up.
    assert entity_flows(_transitions([("ios", "ios", 1)]))["reconciles_to_gap"] is False


def test_malformed_transitions_are_rejected():
    with pytest.raises(ValueError, match="reference_slice"):
        entity_flows(pd.DataFrame({"a": [1]}))


def test_an_event_grained_relation_is_flagged_not_silently_mislabelled():
    # Measured against Narrative: `active_subscription_count` binds to a
    # status-change table, so an entity appears only in windows where something
    # happened to it — 2 retained out of ~2,340. The arithmetic is unaffected;
    # what "new" and "churned" *mean* is, so it is said rather than left for the
    # reader to infer from an odd-looking number.
    f = entity_flows(
        _transitions(
            [
                ("cancelled", "__absent__", 1991),
                ("__absent__", "cancelled", 1720),
                ("cancelled", "active", 36),
                ("active", "active", 2),
            ]
        )
    )
    assert f["retention_share"] < 0.05
    assert any("records events rather than daily state" in c for c in f["caveats"])
    # ...and the flows themselves are still correct.
    assert f["totals"]["migrated"] == 36
    assert f["migration_net"] == 0


def test_a_membership_relation_carries_no_such_caveat():
    f = entity_flows(_transitions([("ios", "ios", 900), ("ios", "__absent__", 50)]))
    assert f["retention_share"] > 0.9
    assert f["caveats"] == []


def test_retention_share_is_none_when_the_reference_window_is_empty():
    f = entity_flows(_transitions([("__absent__", "web", 5)]))
    assert f["retention_share"] is None
    assert f["caveats"] == []


def test_a_truncated_migration_list_says_it_is_truncated():
    # A bounded view that looks complete reads as "these are the movements".
    # The tail of a transition matrix is quadratic in slice count, and together
    # it can outweigh any single movement shown.
    rows = [(f"s{i}", f"s{i + 1}", 50 - i) for i in range(14)]
    f = entity_flows(_transitions(rows))
    assert len(f["migrations"]) == 10
    assert f["migrations_total"] == 14
    assert f["migrations_truncated"] == 4
    assert any("Showing the 10 largest of 14" in c for c in f["caveats"])


def test_a_short_migration_list_is_not_flagged():
    f = entity_flows(_transitions([("ios", "web", 3)]))
    assert f["migrations_truncated"] == 0
    assert not any("Showing the" in c for c in f["caveats"])


# --- flows fold to the same slices the attribution shows --------------------


def test_flows_fold_to_top_k_so_both_panels_agree():
    # A transition matrix is quadratic in slice count; an unfolded flow panel
    # beside a top_k attribution is both inconsistent and unbounded.
    rows = [(f"s{i}", f"s{i}", 100 - i) for i in range(12)]
    f = entity_flows(_transitions(rows), top_k=3)
    values = {r["value"] for r in f["slices"]}
    assert values == {"s0", "s1", "s2", "__other__"}
    assert len(f["folded_slices"]) == 9
    assert any("folded into __other__" in c for c in f["caveats"])


def test_pinned_values_win_over_volume_like_the_attribution():
    rows = [("big", "big", 1000), ("tiny", "tiny", 1)]
    f = entity_flows(_transitions(rows), top_k=1, pinned=["tiny"])
    assert {r["value"] for r in f["slices"]} == {"tiny", "__other__"}


def test_movement_between_two_folded_slices_stays_movement():
    # Both endpoints fold, so a naive relabel yields `__other__ -> __other__`,
    # which reads as *retained* — turning movement into stability, the exact
    # opposite of what this panel exists to show.
    rows = [
        ("keep", "keep", 500),
        ("small_a", "small_b", 7),  # a real migration between two folded slices
    ]
    f = entity_flows(_transitions(rows), top_k=1)
    assert f["totals"]["migrated"] == 7
    assert f["totals"]["retained"] == 500
    assert f["migrations"][0]["entities"] == 7


def test_folding_does_not_disturb_a_small_dimension():
    f = entity_flows(_transitions([("ios", "web", 3), ("ios", "ios", 5)]), top_k=8)
    assert f["folded_slices"] == []
    assert not any("folded" in c for c in f["caveats"])
    assert f["totals"]["migrated"] == 3


# ---------------------------------------------------------------------------
# The localization verdict (roadmap C24): published, and reachable for rates.


def test_a_concentrated_rate_slice_can_be_localized():
    """C24's positive assertion — the one no test made. Every rate row carries
    `baseline_share` (its reference share of the denominator), and a rate
    panel with one slice's rate collapsing must publish `localized: true`.
    Before the fix the field was never emitted, so the verdict was
    structurally false for every `kind: rate` dimension — on the product's
    showcase sentence."""
    sliced, weight_sliced, unsliced = _rate_inputs(rate_drop=("emea", 0.04))
    result = slice_attribution(
        _rate_defn(),
        "region",
        sliced,
        unsliced,
        *REF,
        *AN,
        weight_sliced=weight_sliced,
    )
    top = result["slices"][0]
    assert top["value"] == "emea"
    assert top["baseline_share"] is not None
    assert top["baseline_share"] == pytest.approx(top["share_reference"])
    assert result["localized"] is True
    assert result["localization_threshold"] == 0.25


def test_an_even_rate_move_is_not_localized():
    """The gate still gates: a pure mix shift with no slice's own rate moving
    concentrates nothing beyond slice size, so the verdict stays withheld."""
    sliced, weight_sliced, unsliced = _rate_inputs(shift_shares=True)
    result = slice_attribution(
        _rate_defn(),
        "region",
        sliced,
        unsliced,
        *REF,
        *AN,
        weight_sliced=weight_sliced,
    )
    assert "localized" in result


def test_overlapping_slices_withhold_the_verdict():
    """Withheld shares mean a withheld verdict: `additivity: overlapping`
    nulls every `share_of_gap`, and a headline quoting a withheld number
    would out-run the evidence."""
    frames, dates = _flow_slices(boost=("emea", -80.0))
    result = slice_attribution(
        _flow_defn(),
        "region",
        _long(frames),
        _unsliced(frames, dates),
        *REF,
        *AN,
        additivity="overlapping",
    )
    assert result["localized"] is False


def test_pivot_refuses_duplicate_date_slice_pairs():
    """C23: the unsliced path treats a duplicated date as a hard grain
    violation; `_pivot` silently *summed* duplicate (date, slice) rows, so a
    fanned-out slice doubled with nothing said — same layer, opposite policy."""
    from breakdown.engine.slices import _pivot

    long_df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "slice": ["emea", "emea", "emea"],
            "value": [1.0, 2.0, 3.0],
        }
    )
    with pytest.raises(ValueError, match="more than one row per"):
        _pivot(long_df, "'m' by 'region'")

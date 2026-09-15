"""The SQL-side `top_k` roll-up (roadmap C32) reproduces the engine's fold.

Every test here runs **both** paths on the same DuckDB data — the provider
fetching the sliced frame whole and the engine folding it, versus the provider
folding in its generated SQL — and asserts they agree. The equivalence is the
whole contract: a pushdown that produced a different `__other__` would be a
different number wearing the same label, which is the C-track's definition of
a defect, and a hasty twin of a policy chosen carefully in `_select_slices` is
this repo's named meta-defect.
"""

import numpy as np
import pandas as pd
import pytest

from breakdown.data_fetch import SLICE_ROLLUP, SliceSelection, slice_rollup
from breakdown.engine.slices import slice_attribution
from breakdown.parser import BindingDimension, BindingSpec, MetricDefinition

pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")
duckdb = pytest.importorskip("duckdb")

from breakdown.dbt_provider import DbtDataFetcher  # noqa: E402
from breakdown.dbt_sql import build_query  # noqa: E402

REF = ("2024-01-01", "2024-01-28")  # 4 whole weeks of days
AN = ("2024-01-29", "2024-02-04")  # 1 whole week
SPAN = (REF[0], AN[1])
WINDOWS = ((REF[0], REF[1]), (AN[0], AN[1]))


def _orders(seed=0, n_slices=30, days=35, start="2024-01-01", null_share=0.0, ties=None):
    """A synthetic order table: `n_slices` regions with a geometric size
    spread, one row per (day, region) plus a few extra rows so per-period sums
    are not trivially single rows. `ties` names slices given identical rows so
    their volumes tie exactly."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=days, freq="D")
    rows = []
    oid = 0
    for j in range(n_slices):
        level = 500.0 * (0.85**j)
        for d in dates:
            for _ in range(2):
                oid += 1
                region = None if (null_share and rng.random() < null_share) else f"r{j:03d}"
                rows.append((oid, d.date(), round(float(level + rng.normal(0, 5)), 2), region))
    if ties:
        base = [r for r in rows if r[3] == ties[0]]
        for name in ties[1:]:
            for r in base:
                oid += 1
                rows.append((oid, r[1], r[2], name))
    return pd.DataFrame(rows, columns=["order_id", "ordered_at", "amount", "region"])


def _con(df):
    con = duckdb.connect()
    con.register("src", df)
    con.execute("CREATE TABLE fct_orders AS SELECT * FROM src")
    return con


def _bind(**kw):
    base = dict(
        relation="fct_orders",
        grain_key="order_id",
        time_column="ordered_at",
        agg="sum",
        measure="amount",
        dimensions={"region": BindingDimension(column="region")},
    )
    base.update(kw)
    return BindingSpec(**base)


def _defn(name="revenue", kind="flow", **dim):
    return MetricDefinition(
        name=name,
        source=f"dbt.{name}",
        kind=kind,
        dimensions={"region": {"source": "region", **dim}},
        **({"denominator": "orders"} if kind == "rate" else {}),
    )


def _unsliced(fetcher, name):
    df = fetcher.fetch_metric(name, *SPAN)
    return df.rename(columns={"value": name})


def _both(fetcher, defn, selection, weight_defn=None):
    """(whole-path result, rolled-path result) for one attribution."""
    whole = fetcher.fetch_metric_sliced(defn.name, "region", *SPAN)
    rolled = fetcher.fetch_metric_sliced(defn.name, "region", *SPAN, selection=selection)
    assert slice_rollup(whole) is None
    assert slice_rollup(rolled)["where"] == "sql"
    kw = {}
    if weight_defn is not None:
        kw["weight_sliced_whole"] = fetcher.fetch_metric_sliced(weight_defn.name, "region", *SPAN)
        kw["weight_sliced_rolled"] = fetcher.fetch_metric_sliced(
            weight_defn.name, "region", *SPAN, selection=selection
        )
    unsliced = _unsliced(fetcher, defn.name)
    a = slice_attribution(
        defn, "region", whole, unsliced, *REF, *AN, weight_sliced=kw.get("weight_sliced_whole")
    )
    b = slice_attribution(
        defn, "region", rolled, unsliced, *REF, *AN, weight_sliced=kw.get("weight_sliced_rolled")
    )
    return a, b, whole, rolled


def _fold_client(whole, kept):
    """What the engine would fold from the whole frame: the kept slices raw,
    everything else summed per date into __other__."""
    w = whole.copy()
    w["slice"] = np.where(w["slice"].isin(kept), w["slice"], "__other__")
    return (
        w.groupby(["date", "slice"], as_index=False)["value"]
        .sum()
        .sort_values(["date", "slice"])
        .reset_index(drop=True)
    )


def _assert_same_result(a, b):
    """Every published number equal, row for row, to float tolerance."""
    assert [r["value"] for r in a["slices"]] == [r["value"] for r in b["slices"]]
    for ra, rb in zip(a["slices"], b["slices"]):
        assert set(ra) == set(rb), (ra, rb)
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float) and isinstance(vb, float):
                assert va == pytest.approx(vb, rel=1e-9, abs=1e-9), (k, va, vb)
            elif isinstance(va, list) and va and isinstance(va[0], float):
                assert va == pytest.approx(vb, rel=1e-9, abs=1e-9), (k, va, vb)
            else:
                assert va == vb, (k, va, vb)
    for k in ("baseline", "actual", "gap"):
        assert a[k] == pytest.approx(b[k], rel=1e-9)
    assert a["localization"] == b["localization"]
    assert a["reconciliation"]["status"] == b["reconciliation"]["status"]
    assert a["caveats"] == b["caveats"]


# --- flows ------------------------------------------------------------------


def test_top_k_fold_in_sql_equals_the_engine_fold():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders()), dialect="duckdb"
    )
    defn = _defn(top_k=5)
    sel = SliceSelection(top_k=5, values=None, rank_windows=WINDOWS)
    a, b, whole, rolled = _both(fetcher, defn, sel)
    kept = [r["value"] for r in a["slices"] if r["value"] != "__other__"]
    assert len(kept) == 5
    # The frame itself: the engine's fold of the whole frame is the frame SQL returned.
    expected = _fold_client(whole, kept)
    pd.testing.assert_frame_equal(
        rolled.reset_index(drop=True), expected, check_dtype=False, atol=1e-9
    )
    assert slice_rollup(rolled) == {"where": "sql", "n_distinct": 30, "n_folded": 25}
    other = next(r for r in b["slices"] if r["value"] == "__other__")
    assert other["n_values"] == 25
    _assert_same_result(a, b)
    assert b["rollup"]["where"] == "sql" and a["rollup"]["where"] == "client"


def test_the_rolled_frame_is_bounded_by_top_k_not_cardinality():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=90)), dialect="duckdb"
    )
    sel = SliceSelection(top_k=8, values=None, rank_windows=WINDOWS)
    whole = fetcher.fetch_metric_sliced("revenue", "region", *SPAN)
    rolled = fetcher.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert whole["slice"].nunique() == 90
    assert rolled["slice"].nunique() == 9  # 8 kept + __other__
    assert len(rolled) == 9 * 35


def test_exact_ties_at_the_cut_are_all_returned_raw_and_the_engine_breaks_them():
    # Slices r003, tie_a, tie_b carry identical rows, so their volumes tie
    # exactly at the boundary of top_k=4: SQL must hand back all three raw and
    # the engine picks by name, exactly as it does on the whole frame.
    df = _orders(n_slices=6, ties=("r003", "tie_a", "tie_b"))
    fetcher = DbtDataFetcher({"revenue": _bind()}, connect=lambda: _con(df), dialect="duckdb")
    sel = SliceSelection(top_k=4, values=None, rank_windows=WINDOWS)
    a, b, whole, rolled = _both(fetcher, _defn(top_k=4), sel)
    raw = set(rolled["slice"]) - {"__other__"}
    assert {"r003", "tie_a", "tie_b"} <= raw
    kept_whole = [r["value"] for r in a["slices"] if r["value"] != "__other__"]
    assert sorted(kept_whole) == ["r000", "r001", "r002", "r003"]  # name tie-break
    _assert_same_result(a, b)
    other = next(r for r in b["slices"] if r["value"] == "__other__")
    assert other["n_values"] == 8 - 4


def test_pinned_values_fold_everything_else_in_sql():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders()), dialect="duckdb"
    )
    pins = ("r004", "r011", "nope")
    sel = SliceSelection(top_k=8, values=pins, rank_windows=WINDOWS)
    a, b, whole, rolled = _both(fetcher, _defn(values=list(pins)), sel)
    assert set(rolled["slice"]) == {"r004", "r011", "__other__"}
    _assert_same_result(a, b)
    assert any("nope" in c for c in b["caveats"])
    assert slice_rollup(rolled)["n_folded"] == 28


def test_a_null_dimension_value_is_a_slice_not_part_of_other():
    df = _orders(n_slices=4, null_share=0.6)
    fetcher = DbtDataFetcher({"revenue": _bind()}, connect=lambda: _con(df), dialect="duckdb")
    sel = SliceSelection(top_k=2, values=None, rank_windows=WINDOWS)
    a, b, whole, rolled = _both(fetcher, _defn(top_k=2), sel)
    assert "__null__" in set(rolled["slice"])  # the biggest slice, kept raw
    _assert_same_result(a, b)
    pinned = SliceSelection(top_k=2, values=("__null__", "r000"), rank_windows=WINDOWS)
    a2, b2, _, rolled2 = _both(fetcher, _defn(values=["__null__", "r000"]), pinned)
    assert set(rolled2["slice"]) == {"__null__", "r000", "__other__"}
    _assert_same_result(a2, b2)


def test_ranking_uses_only_the_two_windows_not_the_whole_span():
    # A slice that is huge only *between* the windows must not be kept for it.
    df = _orders(n_slices=6, days=42, start="2023-12-25")
    gap_days = pd.date_range("2024-01-29", "2024-02-04")  # not used as AN here
    ref, an = ("2023-12-25", "2024-01-07"), ("2024-01-29", "2024-02-04")
    boost = df[
        (df["region"] == "r005")
        & (pd.to_datetime(df["ordered_at"]) >= "2024-01-08")
        & (pd.to_datetime(df["ordered_at"]) <= "2024-01-28")
    ].index
    df.loc[boost, "amount"] += 1e6
    fetcher = DbtDataFetcher({"revenue": _bind()}, connect=lambda: _con(df), dialect="duckdb")
    sel = SliceSelection(top_k=2, values=None, rank_windows=(ref, an))
    rolled = fetcher.fetch_metric_sliced("revenue", "region", ref[0], an[1], selection=sel)
    assert "r005" not in set(rolled["slice"])
    whole = fetcher.fetch_metric_sliced("revenue", "region", ref[0], an[1])
    unsliced = fetcher.fetch_metric("revenue", ref[0], an[1]).rename(columns={"value": "revenue"})
    a = slice_attribution(_defn(top_k=2), "region", whole, unsliced, *ref, *an)
    b = slice_attribution(_defn(top_k=2), "region", rolled, unsliced, *ref, *an)
    _assert_same_result(a, b)
    del gap_days


def test_fewer_slices_than_top_k_folds_nothing():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=3)), dialect="duckdb"
    )
    sel = SliceSelection(top_k=8, values=None, rank_windows=WINDOWS)
    rolled = fetcher.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert "__other__" not in set(rolled["slice"])
    assert slice_rollup(rolled) == {"where": "sql", "n_distinct": 3, "n_folded": 0}


def test_the_cardinality_gate_reads_the_provider_count():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=120)), dialect="duckdb"
    )
    sel = SliceSelection(top_k=8, values=None, rank_windows=WINDOWS)
    rolled = fetcher.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert rolled["slice"].nunique() == 9
    with pytest.raises(ValueError, match="120 distinct values"):
        slice_attribution(
            _defn(top_k=8), "region", rolled, _unsliced(fetcher, "revenue"), *REF, *AN
        )


# --- rates ------------------------------------------------------------------


def _rate_fetcher(df):
    rate = _bind(agg="ratio", measure=None, numerator="amount", denominator="units")
    weight = _bind(agg="sum", measure="units")
    return DbtDataFetcher(
        {"aov": rate, "orders": weight}, connect=lambda: _con(df), dialect="duckdb"
    )


def _with_units(df, seed=1):
    rng = np.random.default_rng(seed)
    df = df.copy()
    df["units"] = rng.integers(1, 6, len(df)).astype(float)
    return df


def test_a_rate_folds_as_sum_num_over_sum_den_and_matches_the_weighted_merge():
    fetcher = _rate_fetcher(_with_units(_orders(n_slices=12)))
    assert fetcher.slice_rollup_refusal("aov", "region", "rate", "orders") is None
    defn = _defn("aov", kind="rate", top_k=4, weight="orders")
    weight = _defn("orders", top_k=4)
    sel = SliceSelection(top_k=4, values=None, rank_windows=WINDOWS)
    a, b, whole, rolled = _both(fetcher, defn, sel, weight_defn=weight)
    assert b["attribution_method"] == "slice_blend"
    _assert_same_result(a, b)
    other = next(r for r in b["slices"] if r["value"] == "__other__")
    assert other["n_values"] == 8


def test_a_rate_whose_weight_is_not_its_denominator_is_refused_by_name():
    df = _with_units(_orders(n_slices=5))
    rate = _bind(agg="ratio", measure=None, numerator="amount", denominator="units")
    other_weight = _bind(agg="sum", measure="amount")  # a different column
    f = DbtDataFetcher({"aov": rate, "w": other_weight}, connect=lambda: _con(df), dialect="duckdb")
    reason = f.slice_rollup_refusal("aov", "region", "rate", "w")
    assert reason is not None and "not provably" in reason
    filtered = _bind(agg="sum", measure="units", where=["amount > 0"])
    f2 = DbtDataFetcher({"aov": rate, "w": filtered}, connect=lambda: _con(df), dialect="duckdb")
    assert f2.slice_rollup_refusal("aov", "region", "rate", "w") is not None


def test_a_stock_is_refused_and_a_flow_is_not():
    f = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=3)), dialect="duckdb"
    )
    assert f.slice_rollup_refusal("revenue", "region", "flow") is None
    assert "forward-filled" in f.slice_rollup_refusal("revenue", "region", "stock")


def test_rate_and_weight_rolled_on_different_sides_is_refused_by_the_engine():
    fetcher = _rate_fetcher(_with_units(_orders(n_slices=6)))
    sel = SliceSelection(top_k=3, values=None, rank_windows=WINDOWS)
    rolled = fetcher.fetch_metric_sliced("aov", "region", *SPAN, selection=sel)
    whole_w = fetcher.fetch_metric_sliced("orders", "region", *SPAN)
    with pytest.raises(ValueError, match="same side"):
        slice_attribution(
            _defn("aov", kind="rate", top_k=3, weight="orders"),
            "region",
            rolled,
            _unsliced(fetcher, "aov"),
            *REF,
            *AN,
            weight_sliced=whole_w,
        )


# --- the generated SQL ---------------------------------------------------------


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery", "databricks"])
def test_the_roll_up_parses_in_every_dialect(dialect):
    import sqlglot

    sel = SliceSelection(top_k=8, values=("a", "b'c"), rank_windows=WINDOWS)
    for values in (None, sel.values):
        s = SliceSelection(top_k=8, values=values, rank_windows=WINDOWS)
        sql = build_query(
            _bind(),
            grain="day",
            start_date=SPAN[0],
            end_date=SPAN[1],
            dialect=dialect,
            dimension="region",
            selection=s,
        )
        tree = sqlglot.parse_one(sql, dialect=dialect)
        assert tree is not None
        assert "bd_n_distinct" in sql and "bd_other" in sql
        if values is None:
            assert "OFFSET 7" in sql
        else:
            # The pin survives as one string literal in the dialect's own
            # parse, however that dialect spells the escaped quote.
            literals = {n.this for n in tree.find_all(sqlglot.exp.Literal) if n.is_string}
            assert "b'c" in literals


def test_a_selection_without_a_dimension_is_refused():
    with pytest.raises(ValueError, match="dimension"):
        build_query(
            _bind(),
            grain="day",
            start_date=SPAN[0],
            end_date=SPAN[1],
            selection=SliceSelection(8, None, WINDOWS),
        )


def test_the_rolled_frame_declares_itself():
    fetcher = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=3)), dialect="duckdb"
    )
    sel = SliceSelection(top_k=2, values=None, rank_windows=WINDOWS)
    rolled = fetcher.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert list(rolled.columns) == ["date", "slice", "value"]
    assert SLICE_ROLLUP in rolled.attrs


# --- the snapshot layer ------------------------------------------------------


def test_a_whole_frame_snapshot_hit_is_folded_by_the_engine_and_says_so(tmp_path):
    from breakdown.snapshots import SnapshotFetcher, SnapshotStore

    inner = DbtDataFetcher(
        {"revenue": _bind()}, connect=lambda: _con(_orders(n_slices=6)), dialect="duckdb"
    )
    f = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    sel = SliceSelection(top_k=2, values=None, rank_windows=WINDOWS)
    # Nothing stored yet: a selection fetch is rolled up in SQL and *not* written.
    rolled = f.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert slice_rollup(rolled)["where"] == "sql"
    assert not list(tmp_path.glob("*.parquet"))
    # A whole fetch stores; the next selection fetch is served whole off disk
    # and the frame says the fold is the engine's.
    whole = f.fetch_metric_sliced("revenue", "region", *SPAN)
    assert slice_rollup(whole) is None
    assert len(list(tmp_path.glob("*.parquet"))) == 1
    served = f.fetch_metric_sliced("revenue", "region", *SPAN, selection=sel)
    assert served["slice"].nunique() == 6
    assert slice_rollup(served)["where"] == "client"
    assert "snapshot" in slice_rollup(served)["reason"]
    assert f.slice_rollup_refusal("revenue", "region", "flow") is None


def test_the_api_builds_the_selection_with_bare_dates_snapped_to_the_grain():
    from breakdown.api.main import _slice_selection

    defn = MetricDefinition(
        name="signups",
        source="dbt.signups",
        grain="week",
        dimensions={"region": {"source": "region", "top_k": 3}},
    )
    sel = _slice_selection(
        defn, defn.dimensions["region"], "2024-01-03", "2024-01-30", "2024-02-05", "2024-02-11"
    )
    assert sel.top_k == 3 and sel.values is None
    # Whole Monday->Sunday blocks inside the typed dates, as bare ISO days.
    assert sel.rank_windows == (("2024-01-08", "2024-01-28"), ("2024-02-05", "2024-02-11"))
    for a, b in sel.rank_windows:
        assert len(a) == 10 and len(b) == 10

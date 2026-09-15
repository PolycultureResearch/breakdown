"""Per-metric windows (GitHub #112, second suggestion).

One short series used to narrow every metric at its grain: `_align_to_spine`
trims each series to its own range, and `build_grained` inner-joined the
grain, so a frozen ad feed ending 08-08 cut a production tree's day grain to
ten periods and every daily RCA failed with "reference window not fully
covered". Now the join is outer, each metric keeps its own range, and the
only inner join left is the one a fit genuinely needs — the periods the node
*and its parents* all cover.

The scenario here is the issue's, on the mock provider: `paid_spend` is a
feed that stops 20 days early (trailing), `late_signups` a metric that came
online a month late (leading), and everything else runs the full window.
"""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from breakdown.data_fetch import MockDataFetcher
from breakdown.engine.model import fit_metric
from breakdown.engine.rca import run_rca
from breakdown.loading import fetch_all_metrics
from breakdown.parser import Parser

TREE = """
provider:
  type: mock

metrics:
  - name: sessions
    source: shop.metrics.sessions
  - name: paid_spend
    source: shop.metrics.paid_spend
  - name: orders
    source: shop.metrics.orders
    parents: [sessions, paid_spend]
  - name: signups
    source: shop.metrics.signups
    parents: [sessions]
  - name: late_signups
    source: shop.metrics.late_signups
    parents: [sessions]
  - name: cost_per_order
    formula: "paid_spend / orders"
    parents: [paid_spend, orders]
"""

START, END = "2024-01-01", "2024-04-09"  # 100 days
SPEND_ENDS = "2024-03-20"  # 20 periods short at the trailing edge
LATE_STARTS = "2024-02-01"  # 31 periods short at the leading edge


def _load(monkeypatch, *, short=True):
    """The tree's data, with (or without) the two short series."""
    parser = Parser(TREE)
    original = MockDataFetcher.fetch_metric

    def fetch(self, metric_name, start_date, end_date, grain="day", kind="flow"):
        df = original(self, metric_name, start_date, end_date, grain=grain, kind=kind)
        if metric_name == "paid_spend":
            df = df[df["date"] <= SPEND_ENDS]
        if metric_name == "late_signups":
            df = df[df["date"] >= LATE_STARTS]
        return df.reset_index(drop=True)

    if short:
        monkeypatch.setattr(MockDataFetcher, "fetch_metric", fetch)
    data = fetch_all_metrics(parser, MockDataFetcher(dag=parser.dag), "mock", START, END)
    return parser, data


def test_other_metrics_keep_their_full_range(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="breakdown.grains"):
        parser, data = _load(monkeypatch)

    # The two short series are exactly as short as their sources, no shorter.
    assert len(data.series("paid_spend")) == 80
    assert data.series("paid_spend")["date"].max() == pd.Timestamp(SPEND_ENDS)
    assert len(data.series("late_signups")) == 69
    assert data.series("late_signups")["date"].min() == pd.Timestamp(LATE_STARTS)
    # Everyone else is untouched — the shape the inner join used to break.
    for m in ["sessions", "orders", "signups"]:
        s = data.series(m)
        assert len(s) == 100, m
        assert not s[m].isna().any(), m
    assert data.date_start == pd.Timestamp(START)
    assert data.date_end == pd.Timestamp(END)
    # The grain frame spans the union; outside a metric's range it is `NaN`,
    # which `series` never returns and no consumer may read as zero.
    frame = data.frame("day")
    assert len(frame) == 100
    assert frame["paid_spend"].isna().sum() == 20
    assert frame["late_signups"].isna().sum() == 31
    assert data.short_series == {
        "day": {
            "trailing": {
                "reach": END,
                "short": {
                    # The derived node stops where its short input stops.
                    "cost_per_order": {"ends": SPEND_ENDS, "periods": 20},
                    "paid_spend": {"ends": SPEND_ENDS, "periods": 20},
                },
            },
            "leading": {
                "reach": START,
                "short": {"late_signups": {"starts": LATE_STARTS, "periods": 31}},
            },
        }
    }
    assert "nothing was clipped and nothing was filled" in caplog.text


def test_a_formula_over_the_short_leaf_is_undefined_outside_it(monkeypatch):
    """`cost_per_order = paid_spend / orders` is derived, so it exists only
    where both inputs do: it ends with `paid_spend`, and outside that range
    it is absent — not zero, not a forward-fill (roadmap 1.11's
    representation for a value the identity cannot compute)."""
    parser, data = _load(monkeypatch)
    cpo = data.series("cost_per_order")
    assert len(cpo) == 80
    assert cpo["date"].max() == pd.Timestamp(SPEND_ENDS)
    assert np.isfinite(cpo["cost_per_order"]).all()
    assert data.frame("day")["cost_per_order"].isna().sum() == 20


@pytest.mark.slow
def test_short_series_fit_on_their_own_range(monkeypatch):
    """A fit's window is the intersection of the node's range and its
    parents' — and only theirs. `orders` reads `paid_spend`, so it trains
    through 03-20; `late_signups` is itself short, so it trains from 02-01;
    `signups` reads neither and trains on all 100 days."""
    parser, data = _load(monkeypatch)
    dag = parser.dag

    orders = fit_metric(dag, data, "orders", draws=200, random_seed=0)
    assert orders.dates[0] == pd.Timestamp(START)
    assert orders.dates[-1] == pd.Timestamp(SPEND_ENDS)
    assert len(orders.dates) == 80

    late = fit_metric(dag, data, "late_signups", draws=200, random_seed=0)
    assert late.dates[0] == pd.Timestamp(LATE_STARTS)
    assert late.dates[-1] == pd.Timestamp(END)
    assert len(late.dates) == 69

    signups = fit_metric(dag, data, "signups", draws=200, random_seed=0)
    assert len(signups.dates) == 100


@pytest.mark.slow
def test_rca_on_a_target_that_does_not_read_the_short_leaf_is_unaffected(monkeypatch):
    """The whole point: an analysis over the last two weeks of the window —
    past `paid_spend`'s edge — on a target that never reads `paid_spend` runs,
    and its payload is byte-identical to the same tree with the feed intact.
    The inner join used to refuse this with a coverage error naming nothing."""
    windows = dict(
        reference_start="2024-02-27",
        reference_end="2024-03-25",
        analysis_start="2024-03-26",
        analysis_end="2024-04-08",
    )
    parser, data = _load(monkeypatch, short=True)
    with_short = run_rca(parser.dag, data, {}, "signups", draws=200, **windows)

    parser, data = _load(monkeypatch, short=False)
    intact = run_rca(parser.dag, data, {}, "signups", draws=200, **windows)

    assert with_short["nodes"]["signups"]["status"] is None or (
        with_short["nodes"]["signups"]["status"] == intact["nodes"]["signups"]["status"]
    )
    assert json.dumps(with_short, sort_keys=True, default=str) == json.dumps(
        intact, sort_keys=True, default=str
    )


def test_rca_on_a_target_that_reads_the_short_leaf_names_it(monkeypatch):
    """Same windows, a target that *does* read `paid_spend`: refused before
    any fit, and the refusal names the series that stops short and where it
    runs — not the grain, and not "the data"."""
    parser, data = _load(monkeypatch)
    with pytest.raises(ValueError) as e:
        run_rca(
            parser.dag,
            data,
            {},
            "orders",
            draws=200,
            reference_start="2024-02-27",
            reference_end="2024-03-25",
            analysis_start="2024-03-26",
            analysis_end="2024-04-08",
        )
    text = str(e.value)
    assert "not fully covered by its data" in text
    assert "`paid_spend` runs [2024-01-01, 2024-03-20]" in text
    assert "`sessions`" not in text, "a series that covers the window is not blamed"


def test_rca_over_the_shared_range_still_answers_for_every_node(monkeypatch):
    """Windows every series covers: nothing is refused, and `cost_per_order`
    decomposes exactly over the periods it has. (Fits happen here, so the
    reference block is short to keep it cheap; the point is the coverage
    logic, which runs before any sampler.)"""
    parser, data = _load(monkeypatch)
    from breakdown.engine.rca import _validate_coverage, snap_window

    grain = "day"
    frame = data.fit_frame("cost_per_order", ["paid_spend", "orders"], grain)
    ref = snap_window("2024-02-20", "2024-03-05", grain)
    an = snap_window("2024-03-06", "2024-03-19", grain)
    _validate_coverage(
        frame,
        "cost_per_order",
        grain,
        ref,
        an,
        None,
        grained=data,
        parents=["paid_spend", "orders"],
    )
    # And past the edge, the derived node's own range is the one named.
    an_late = snap_window("2024-03-26", "2024-04-08", grain)
    with pytest.raises(ValueError, match=r"`cost_per_order` runs \[2024-01-01, 2024-03-20\]"):
        _validate_coverage(
            frame,
            "cost_per_order",
            grain,
            ref,
            an_late,
            None,
            grained=data,
            parents=["paid_spend", "orders"],
        )


def test_the_load_is_a_pure_function_of_its_inputs(monkeypatch):
    """Loading twice gives the same frames — the outer join and the span
    record are deterministic, so a snapshot re-run reproduces them."""
    _, a = _load(monkeypatch)
    _, b = _load(monkeypatch)
    pd.testing.assert_frame_equal(a.frame("day"), b.frame("day"))
    assert copy.deepcopy(a.short_series) == b.short_series
    assert a.span_of == b.span_of

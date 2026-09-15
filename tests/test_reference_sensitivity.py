"""Roadmap S23: the attribution is re-run under neighbouring reference blocks
and the payload says whether the answer survived the move.

Formula-only trees throughout, so nothing here fits a sampler: every world is
exact Shapley over planted series, and the suite stays in the fast loop.
"""

import json

import numpy as np
import pandas as pd

from breakdown.engine.rca import (
    REFERENCE_SENSITIVITY_SHIFTS,
    REFERENCE_SENSITIVITY_STATUSES,
    _reference_alternatives,
    run_rca,
)
from breakdown.parser import Parser

YAML = """
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

# 140 days from 2024-01-01; the analysis week is 2024-04-30 → 2024-05-06
# (days 120-126), so the defaulted reference is the 28 days 2024-04-02 →
# 2024-04-29 (days 92-119), one week earlier is days 85-112, and one whole
# block earlier is days 64-91.
AN = {"analysis_start": "2024-04-30", "analysis_end": "2024-05-06"}


def world(n=140, orders_step_at=120, aov_bump=None, seed=0):
    """`orders` steps up at day 120 (the analysis week); `aov` is flat unless
    `aov_bump=(lo, hi, amount)` raises it over one earlier block."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    orders = 100 + rng.normal(0, 3, n)
    orders[orders_step_at:] += 30
    aov = 50 + rng.normal(0, 1, n)
    if aov_bump:
        lo, hi, amt = aov_bump
        aov[lo:hi] += amt
    return pd.DataFrame({"date": dates, "orders": orders, "aov": aov, "revenue": orders * aov})


def rca(data, **kw):
    return run_rca(Parser(YAML).dag, data, {}, "revenue", **AN, **kw)


def test_a_planted_step_survives_a_moved_reference():
    res = rca(world())
    rs = res["reference_sensitivity"]

    assert rs["status"] == "stable"
    assert rs["top_cause"] == "orders" == res["ranked_causes"][0]["metric"]
    assert rs["top_cause_stable"] is True and rs["gap_sign_stable"] is True
    assert [a["shift"] for a in rs["alternatives"]] == list(REFERENCE_SENSITIVITY_SHIFTS)
    assert all(a["status"] == "ok" for a in rs["alternatives"])
    assert all(a["top_cause"] == "orders" for a in rs["alternatives"])
    # The published gap is inside the band, and the band is not a point.
    lo, hi = rs["gap_range"]
    assert lo <= res["nodes"]["revenue"]["gap"] <= hi and lo < hi
    # Neighbouring blocks, whole-day aligned, ending before the published one
    # starts (block) or a week before its end (period).
    windows = {a["shift"]: a["reference_window"] for a in rs["alternatives"]}
    assert windows["one_period_earlier"] == {"start": "2024-03-26", "end": "2024-04-22"}
    assert windows["one_block_earlier"] == {"start": "2024-03-05", "end": "2024-04-01"}


def test_a_reference_straddling_a_prior_step_flips_the_verdict():
    """`aov` sat 60 higher over exactly the block one earlier (days 64-91), so
    under that reference the gap reverses sign and `aov`, not `orders`, tops
    the ranking; under the published block nothing about `aov` moved."""
    res = rca(world(aov_bump=(64, 92, 60)))
    rs = res["reference_sensitivity"]

    assert res["ranked_causes"][0]["metric"] == "orders"
    assert rs["status"] == "unstable"
    assert rs["top_cause_stable"] is False
    assert rs["gap_sign_stable"] is False
    by_shift = {a["shift"]: a for a in rs["alternatives"]}
    assert by_shift["one_block_earlier"]["top_cause"] == "aov"
    assert by_shift["one_block_earlier"]["gap"] < 0 < res["nodes"]["revenue"]["gap"]
    assert rs["gap_range"][0] == by_shift["one_block_earlier"]["gap"]


def test_the_alternatives_do_not_recurse_and_can_be_switched_off():
    res = rca(world(), reference_sensitivity=False)
    assert "reference_sensitivity" not in res

    on = rca(world())
    # The published numbers are untouched by the check.
    assert on["nodes"]["revenue"]["gap"] == res["nodes"]["revenue"]["gap"]
    assert on["ranked_causes"] == res["ranked_causes"]


def test_no_readable_history_is_reported_not_invented():
    """An analysis at the start of the loaded data leaves no room for an
    earlier block: every alternative names why, and the verdict is
    `unavailable` — never `stable` by default."""
    data = world(n=36, orders_step_at=28)
    # A chosen five-day reference at the very start of the data: neither a
    # week earlier nor a block earlier has a single readable day.
    res = run_rca(
        Parser(YAML).dag,
        data,
        {},
        "revenue",
        reference_start="2024-01-01",
        reference_end="2024-01-05",
        analysis_start="2024-01-29",
        analysis_end="2024-02-04",
    )
    rs = res["reference_sensitivity"]

    assert res["reference_defaulted"] is False  # the check runs on a chosen block too
    assert rs["status"] == "unavailable"
    assert rs["gap_range"] is None and rs["top_cause_stable"] is None
    assert "no loaded history before 2024-01-01" in rs["reason"]
    assert all(a["status"] == "unavailable" for a in rs["alternatives"])
    assert all(a["reference_window"] is None and a["gap"] is None for a in rs["alternatives"])

    # The defaulted block at the data start is the partial case: a week
    # earlier still has 21 readable days and answers (shortened, and saying
    # so); a whole block earlier has none.
    res = run_rca(
        Parser(YAML).dag,
        data,
        {},
        "revenue",
        analysis_start="2024-01-29",
        analysis_end="2024-02-04",
    )
    rs = res["reference_sensitivity"]
    assert res["reference_window"] == {"start": "2024-01-01", "end": "2024-01-28"}
    by_shift = {a["shift"]: a for a in rs["alternatives"]}
    assert by_shift["one_period_earlier"]["status"] == "ok"
    assert by_shift["one_period_earlier"]["reference_window"] == {
        "start": "2024-01-01",
        "end": "2024-01-21",
    }
    assert (
        by_shift["one_period_earlier"]["note"] == "shortened to the loaded history: 21 of 28 days"
    )
    assert by_shift["one_block_earlier"]["status"] == "unavailable"
    assert rs["status"] == "stable"  # one block answered, and agreed


def test_a_partly_readable_block_is_shortened_and_says_so():
    data = world(n=60, orders_step_at=53)
    alts = _reference_alternatives(Parser(YAML).dag, data, "revenue", "2024-01-29", "2024-02-21")
    by_shift = {a["shift"]: a for a in alts}
    # Both fit whole: one week earlier, and the 24-day block before the
    # block (2024-01-05 → 2024-01-28) — nothing shortened, nothing noted.
    assert by_shift["one_period_earlier"]["note"] is None
    assert by_shift["one_block_earlier"]["reference_window"] == {
        "start": "2024-01-05",
        "end": "2024-01-28",
    }
    assert by_shift["one_block_earlier"]["note"] is None
    # A block needing more than exists is shortened, not silently fitted.
    alts = _reference_alternatives(Parser(YAML).dag, data, "revenue", "2024-01-15", "2024-02-21")
    block = {a["shift"]: a for a in alts}["one_block_earlier"]
    assert block["reference_window"] == {"start": "2024-01-01", "end": "2024-01-14"}
    assert block["note"] == "shortened to the loaded history: 14 of 38 days"


def test_an_undefined_gap_under_an_alternative_is_withheld_not_emitted():
    """Rule 3. A rate target whose denominator is zero across one alternative
    block has no gap there: that alternative answers nothing and the payload
    still encodes strictly."""
    yaml = """
metrics:
  - name: revenue
    source: dbt.metric.revenue
  - name: orders
    source: dbt.metric.orders
  - name: aov
    source: dbt.metric.aov
    kind: rate
    formula: "revenue / orders"
    parents: [revenue, orders]
"""
    n = 140
    rng = np.random.default_rng(1)
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    orders = 100 + rng.normal(0, 3, n)
    # Days 64-84 (2024-03-05 → 2024-03-25): no orders at all. That lies inside
    # the block one earlier (Mar 5 → Apr 1) and outside the week-earlier block
    # (Mar 26 → Apr 22), so exactly one alternative meets the undefined rate.
    orders[64:85] = 0.0
    revenue = 50 * orders + rng.normal(0, 10, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        aov = np.where(orders > 0, revenue / orders, np.nan)
    data = pd.DataFrame({"date": dates, "orders": orders, "revenue": revenue, "aov": aov})

    res = run_rca(Parser(yaml).dag, data, {}, "aov", **AN)
    rs = res["reference_sensitivity"]

    by_shift = {a["shift"]: a for a in rs["alternatives"]}
    block = by_shift["one_block_earlier"]
    assert block["status"] in ("unavailable", "gap_unavailable")
    assert block["gap"] is None and block["top_cause"] is None
    assert block["reason"]
    # The other alternative still answers, so there is a verdict.
    assert by_shift["one_period_earlier"]["status"] == "ok"
    assert rs["status"] in REFERENCE_SENSITIVITY_STATUSES and rs["status"] != "unavailable"
    json.dumps(res, allow_nan=False)


def test_every_status_the_engine_can_emit_is_a_declared_one():
    """The renderers key their wording on this tuple; a status outside it
    would fall through to "not checked" on every surface."""
    for data in (world(), world(aov_bump=(64, 92, 60)), world(n=36, orders_step_at=28)):
        kw = (
            AN if len(data) > 36 else {"analysis_start": "2024-01-29", "analysis_end": "2024-02-04"}
        )
        res = run_rca(Parser(YAML).dag, data, {}, "revenue", **kw)
        assert res["reference_sensitivity"]["status"] in REFERENCE_SENSITIVITY_STATUSES

"""`sparse: true` — an absent period on an event-count flow is a zero, at
every edge, by the tree's own declaration (GitHub #112, third suggestion).

The alignment contract trims a trailing gap because "not loaded yet" is the
common meaning of one; for a source that emits a row only when something
happened, the common meaning is "nothing happened", and the trim clipped a
production tree's whole day grain to the one event day it had. The
declaration reverses the trim and turns the leading/interior warnings into a
counted record — never a silent fill (rule 1)."""

import logging

import numpy as np
import pandas as pd
import pytest

from breakdown.data_fetch import _align_to_spine
from breakdown.loading import fetch_all_metrics
from breakdown.parser import MetricDefinition, Parser
from breakdown.snapshots import _realign_snapshot

START, END = "2026-08-01", "2026-08-30"  # 30 whole days


def _rows(name, dates, value=1.0):
    return pd.DataFrame({"date": pd.to_datetime(dates), name: [value] * len(dates)})


def _align(df, name="m", kind="flow", sparse=False):
    return _align_to_spine(df, name, "day", kind, START, END, name, sparse=sparse)


# ---------------------------------------------------------------- parser


def test_parser_refuses_sparse_on_a_stock_a_rate_and_a_derived_node():
    """Zero is a claim only a flow can make: a stock's absent period has a
    level the source did not return, a rate's is undefined (1.11), and a
    derived node is never fetched. Each refusal names the metric and says
    what would satisfy it."""
    with pytest.raises(ValueError, match="a stock's absent period has a level"):
        MetricDefinition(name="balance", source="x.balance", kind="stock", sparse=True)
    with pytest.raises(ValueError, match="a rate's is undefined"):
        MetricDefinition(name="conv", source="x.conv", kind="rate", sparse=True)
    with pytest.raises(ValueError, match="never fetched"):
        MetricDefinition(name="d", formula="a + b", parents=["a", "b"], sparse=True)
    assert MetricDefinition(name="comms", source="x.comms", sparse=True).sparse is True
    assert MetricDefinition(name="comms", source="x.comms").sparse is False


# ------------------------------------------------------ alignment contract


def test_the_trailing_run_is_filled_and_counted_under_sparse(caplog):
    """The issue's case: one event day, then quiet. Without the flag the
    series ends at the event; with it the series spans the window, the
    filled periods are counted per edge on the frame's `attrs`, one INFO
    line names them, and no WARNING fires — the fill is a declaration,
    not a judgement call."""
    df = _rows("comms", ["2026-08-01", "2026-08-03"])

    plain = _align(df, "comms")
    assert len(plain) == 3 and plain["date"].max() == pd.Timestamp("2026-08-03")
    assert "sparse_fill" not in plain.attrs

    caplog.clear()  # the plain call above warned about its interior gap, correctly
    with caplog.at_level(logging.INFO, logger="breakdown.data_fetch"):
        out = _align(df, "comms", sparse=True)

    assert len(out) == 30
    assert out["date"].max() == pd.Timestamp("2026-08-30")
    assert out["comms"].sum() == 2.0 and np.isfinite(out["comms"]).all()
    assert out.attrs["sparse_fill"] == {
        "first_row": "2026-08-01",
        "last_row": "2026-08-03",
        "leading": 0,
        "interior": 1,
        "trailing": 27,
        "whole_window": 0,
        "filled": 28,
    }
    levels = {r.levelno for r in caplog.records if r.name == "breakdown.data_fetch"}
    assert levels == {logging.INFO}, "a declared fill is INFO, never a WARNING"
    text = next(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "`sparse: true`" in text and "28 day period(s)" in text
    assert "27 trailing (after the last row on 2026-08-03, through 2026-08-30)" in text
    assert "a stale feed would look identical" in text


def test_the_leading_run_is_declared_not_warned_about(caplog):
    """Both edges, one policy. The plain contract warns that a leading zero
    run is a manufactured level shift; under `sparse` the same zeros are
    the author's statement, counted rather than warned about."""
    df = _rows("comms", ["2026-08-05", "2026-08-06"])

    with caplog.at_level(logging.WARNING, logger="breakdown.data_fetch"):
        _align(df, "comms")
    assert any("manufactured level shift" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="breakdown.data_fetch"):
        out = _align(df, "comms", sparse=True)
    rec = out.attrs["sparse_fill"]
    assert rec["leading"] == 4 and rec["trailing"] == 24 and rec["interior"] == 0
    assert rec["filled"] == 28
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_source_with_no_rows_fills_the_whole_window_under_its_own_key():
    """An all-quiet window on a declared-sparse source is a season with no
    events. The record keeps it apart from "one event, then quiet"."""
    empty = pd.DataFrame({"date": pd.to_datetime([]), "comms": []})
    out = _align(empty, "comms", sparse=True)
    assert len(out) == 30 and out["comms"].sum() == 0.0
    assert out.attrs["sparse_fill"] == {
        "first_row": None,
        "last_row": None,
        "leading": 0,
        "interior": 0,
        "trailing": 0,
        "whole_window": 30,
        "filled": 30,
    }


def test_a_dense_series_under_sparse_records_nothing(caplog):
    """The dbt interaction: a metric that arrives dense — MetricFlow's
    `join_to_timespine` + `fill_nulls_with: 0`, which the bridge accepts —
    has nothing to fill. The flag is harmless there: zero counted, no log
    line, the series untouched."""
    df = _rows("comms", pd.date_range(START, END))
    with caplog.at_level(logging.INFO, logger="breakdown.data_fetch"):
        out = _align(df, "comms", sparse=True)
    assert out["comms"].tolist() == [1.0] * 30
    assert out.attrs["sparse_fill"]["filled"] == 0
    assert not caplog.records


def test_the_contract_refuses_sparse_on_a_non_flow_defensively():
    """The parser is the gate; the contract says so too rather than filling
    a stock's tail with a level the source never returned."""
    df = _rows("balance", ["2026-08-01"])
    with pytest.raises(ValueError, match="`sparse` applies to `kind: flow` only"):
        _align(df, "balance", kind="stock", sparse=True)


def test_a_snapshot_read_applies_the_flag_too():
    """A snapshot taken before the declaration stores the trimmed series;
    re-aligning on read fills its tail exactly as a fresh fetch would, so
    declaring `sparse` needs no refetch."""
    stored = _rows("comms", ["2026-08-01", "2026-08-03"])  # what a pre-flag write kept
    out = _realign_snapshot(stored, "comms", "day", "flow", START, END, sparse=True)
    assert len(out) == 30
    assert out.attrs["sparse_fill"]["trailing"] == 27


# ------------------------------------------------------------- the tree


_TREE = """
provider:
  type: warehouse
  host: h
  http_path: p
  token: t
metrics:
  - name: orders
    source: w.orders
    sql: "select 1"
    parents: [comms]
  - name: comms
    source: w.comms
    sql: "select 1"
    kind: flow
    {sparse}
"""


class _StubFetcher:
    """Returns a dense `orders` and a one-row `comms`, through the contract."""

    def fetch_metric(self, name, start, end, grain="day", kind="flow", sparse=False):
        dates = pd.date_range(start, end) if name == "orders" else ["2026-08-01"]
        return _align_to_spine(
            _rows(name, dates), name, grain, kind, start, end, name, sparse=sparse
        )


def _load(sparse: bool):
    parser = Parser(_TREE.format(sparse="sparse: true" if sparse else ""))
    return fetch_all_metrics(parser, _StubFetcher(), "warehouse", START, END)


def test_a_sparse_leaf_no_longer_clips_the_grain_and_the_fill_travels():
    """The #112 failure end to end: without the flag the one-row leaf bounds
    its own range (`short_series` names it, per #112); with it the leaf
    runs the full window and `sparse_fills` says what was filled."""
    clipped = _load(sparse=False)
    assert list(clipped.short_series["day"]["trailing"]["short"]) == ["comms"]
    assert clipped.span_of["comms"][1] == pd.Timestamp("2026-08-01")
    assert clipped.sparse_fills == {}

    full = _load(sparse=True)
    assert full.short_series == {}
    assert full.span_of["comms"][1] == pd.Timestamp("2026-08-30")
    assert full.frame("day")["date"].max() == pd.Timestamp("2026-08-30")
    assert full.sparse_fills == {
        "comms": {
            "first_row": "2026-08-01",
            "last_row": "2026-08-01",
            "leading": 0,
            "interior": 0,
            "trailing": 29,
            "whole_window": 0,
            "filled": 29,
        }
    }
    # By declaration the source is complete through the window.
    assert full.data_through("comms") == pd.Timestamp("2026-08-30")


# --------------------------------------------------------------- surfaces


def test_meta_health_and_get_tree_carry_the_record():
    """Same standing as `short_series`: `/meta` and `/health` always carry
    it (`{}` when none), MCP `get_tree` only when it happened."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def get_tree(client):
        resp = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_tree", "arguments": {}},
            },
            headers=headers,
        )
        return resp.json()["result"]["structuredContent"]["result"]

    with TestClient(app, base_url="http://127.0.0.1:9090") as client:
        assert client.get("/meta").json()["sparse_fills"] == {}
        assert client.get("/health").json()["sparse_fills"] == {}
        assert "sparse_fills" not in get_tree(client)

        record = {
            "first_row": "2024-01-01",
            "last_row": "2024-01-01",
            "leading": 0,
            "interior": 0,
            "trailing": 5,
            "whole_window": 0,
            "filled": 5,
        }
        tree = next(iter(app.state.trees.values()))
        tree.data.sparse_fills = {"daily_sessions": record}
        assert client.get("/meta").json()["sparse_fills"] == {"daily_sessions": record}
        assert client.get("/health").json()["sparse_fills"] == {"daily_sessions": record}
        assert get_tree(client)["sparse_fills"] == {"daily_sessions": record}


def test_check_names_the_metrics_that_declare_it(tmp_path):
    """`breakdown check` sees the declaration without data, so it says which
    metrics will be filled; the counts need the fetch and are on `/meta`."""
    from breakdown.check import run_check

    tree = tmp_path / "events.yml"
    tree.write_text(
        """
provider:
  type: mock
metrics:
  - name: orders
    source: m.orders
    parents: [comms]
  - name: comms
    source: m.comms
    sparse: true
"""
    )
    results = run_check(str(tree))
    assert results[0].status == "pass"
    assert "1 sparse (comms)" in results[0].detail

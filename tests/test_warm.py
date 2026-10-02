"""Roadmap 3.10, step 1: warm every metric's default analysis in the background.

The property that matters is the cache hit: a warmed fit is worth something
only if it is keyed exactly as the request a person will send. So the
window arithmetic is pinned against the UI's presets, the keys come from
`run_rca`'s own planner, and the end-to-end test asserts an RCA on the
default window makes no fit at all.
"""

import time

import pytest

from breakdown.grains import default_analysis_window

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from breakdown.api.main import app  # noqa: E402

# --- the window: a mirror of app.js's DEFAULT_PRESET --------------------------


@pytest.mark.parametrize(
    "end, grain, expected",
    [
        # Thursday edge: the last whole Mon-Sun week ended the Sunday before.
        ("2026-07-30", "week", ("2026-07-20", "2026-07-26")),
        # A Sunday edge closes its own week.
        ("2026-07-26", "week", ("2026-07-20", "2026-07-26")),
        # Mid-month edge: the last whole month is the previous one.
        ("2026-07-30", "month", ("2026-06-01", "2026-06-30")),
        # A month-end edge closes its own month.
        ("2026-07-31", "month", ("2026-07-01", "2026-07-31")),
        # Day grain: the last seven days, inclusive.
        ("2026-07-30", "day", ("2026-07-24", "2026-07-30")),
    ],
)
def test_default_analysis_window_matches_the_ui_presets(end, grain, expected):
    assert default_analysis_window("2024-06-01", end, grain) == expected


@pytest.mark.parametrize(
    "start, end, grain",
    [
        # lastFullWeeks: `anStart <= start` leaves no history before it.
        ("2026-07-20", "2026-07-30", "week"),
        # lastFullMonths, same rule.
        ("2026-06-01", "2026-07-30", "month"),
        # last7 needs at least 8 days of data.
        ("2026-07-24", "2026-07-30", "day"),
    ],
)
def test_default_analysis_window_is_none_where_the_ui_omits_the_preset(start, end, grain):
    assert default_analysis_window(start, end, grain) is None


# --- the cache: warm fits never push out a requested one ----------------------


class _SizedFit:
    def __init__(self, megabytes):
        nbytes = int(megabytes * 1024 * 1024)
        group = type("Group", (), {"nbytes": nbytes})()
        self.trace = type("Trace", (), {"posterior": group, "groups": lambda self: ["posterior"]})()


def test_a_warm_fit_goes_in_oldest_so_it_is_evicted_before_any_requested_fit():
    from breakdown.api.trees import TraceStore

    store = TraceStore(max_bytes=30 * 1024 * 1024)
    traces = store.view("tree")
    traces[("requested_a", "2026-01-01")] = _SizedFit(10)
    traces[("requested_b", "2026-01-01")] = _SizedFit(10)
    traces.put_oldest(("warm", "2026-01-01"), _SizedFit(10))
    assert len(traces) == 3, "room for all three: nothing evicted"

    # Over budget: the warm fit is the one that goes, not the requests.
    traces.put_oldest(("warm_2", "2026-01-01"), _SizedFit(10))
    assert ("requested_a", "2026-01-01") in traces
    assert ("requested_b", "2026-01-01") in traces
    assert ("warm_2", "2026-01-01") not in traces, "a warm write evicts only warm fits"

    # And a later request evicts warm fits first.
    traces[("requested_c", "2026-01-01")] = _SizedFit(10)
    assert ("warm", "2026-01-01") not in traces
    assert {k[0] for k in traces} == {"requested_a", "requested_b", "requested_c"}


# --- the mode -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, mode",
    [(None, "off"), ("", "off"), ("latest", "latest"), (" LATEST ", "latest"), ("yes", "off")],
)
def test_warm_mode_is_opt_in_and_refuses_unknown_values(monkeypatch, raw, mode):
    from breakdown.api.main import _warm_mode

    if raw is None:
        monkeypatch.delenv("BREAKDOWN_WARM", raising=False)
    else:
        monkeypatch.setenv("BREAKDOWN_WARM", raw)
    assert _warm_mode() == mode


def test_warm_is_off_by_default_and_meta_says_so(monkeypatch):
    monkeypatch.delenv("BREAKDOWN_WARM", raising=False)
    with TestClient(app) as client:
        assert client.get("/meta").json()["warm"] == {}


# --- the plan: keys from run_rca's own planner --------------------------------


def test_plan_covers_every_target_once_with_run_rca_keys(monkeypatch):
    """Planning fits nothing, so this is fast: it checks that the bundled
    tree's one learned node is planned once, at the date the UI's default
    window starts, even though several targets' defaults reach it."""
    from breakdown.engine.rca import plan_rca_fits
    from breakdown.engine.warm import default_analysis_for, plan_warm_fits

    monkeypatch.delenv("BREAKDOWN_WARM", raising=False)
    with TestClient(app):
        dag, data = app.state.parser.dag, app.state.data
        fits, windows = plan_warm_fits(dag, data, {})
        assert [(f.node, f.fit_end) for f in fits] == [("order_count", "2024-04-03")]
        assert windows["revenue"] == {"analysis_start": "2024-04-03", "analysis_end": "2024-04-09"}
        assert default_analysis_for(dag, data, "revenue") == ("2024-04-03", "2024-04-09")
        plan = plan_rca_fits(
            dag, data, {}, "revenue", analysis_start="2024-04-03", analysis_end="2024-04-09"
        )
        assert plan.fit_ends["order_count"] == "2024-04-03"
        # Already-cached keys are not planned again.
        cached = {("order_count", "2024-04-03"): object()}
        monkeypatch.setattr("breakdown.engine.rca.cached_fit_is_usable", lambda fit, m: True)
        assert plan_warm_fits(dag, data, cached)[0] == []


# --- end to end ---------------------------------------------------------------


@pytest.mark.slow
def test_the_default_analysis_is_a_cache_hit_after_the_warm(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_WARM", "latest")
    with TestClient(app) as client:
        deadline = time.monotonic() + 600
        while True:
            warm = client.get("/meta").json()["warm"]
            if warm.get("status") not in ("planning", "running"):
                break
            assert time.monotonic() < deadline, f"warm did not finish: {warm}"
            time.sleep(1)
        assert warm["status"] == "done", warm
        assert warm["total"] == 1 and warm["done"] == 1 and warm["failed"] == {}

        # Any fit now would be a cache miss. Make one impossible.
        def no_fit(*a, **k):
            raise AssertionError("the default analysis should not fit anything after a warm")

        monkeypatch.setattr("breakdown.engine.rca.fit_rca_node", no_fit)
        w = warm["windows"]["revenue"]
        r = client.post("/rca/revenue", params=w)
        assert r.status_code == 200, r.text
        assert r.json()["nodes"]["order_count"]["status"] != "fit_failed"

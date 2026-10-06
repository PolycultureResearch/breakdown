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


# --- the window, read out of the UI rather than typed beside it ---------------
#
# `test_default_analysis_window_matches_the_ui_presets` at the top of this file
# says it mirrors the UI and compares the engine against tuples typed into the
# test. Changing `DEFAULT_PRESET` in app.js, or what `lastFullWeeks` computes,
# leaves it green, and the warm pass then fits windows nobody requests: a
# wasted fit per metric on every boot and a cold first click, with nothing
# failing (grill 2026-10-05 L11). app.js cannot import from Python and there is
# no JS test runner (deliberately), so the file is read here, in the style of
# `test_every_rendered_sampler_budget_matches_the_engine`.

#: What `grains.default_analysis_window` implements, per grain: the preset id
#: the UI must select by default, and the expressions that preset's `compute`
#: must be made of. Change `default_analysis_window` and this table together.
_ENGINE_DEFAULT_WINDOW = {
    "day": ("last7", [r"addDays\(end, -6\)", r"daysInclusive\(start, end\) < 8"]),
    "week": ("last-full-week", [r"lastFullWeeks\(start, end, 1\)"]),
    "month": ("last-full-month", [r"lastFullMonths\(start, end, 1\)"]),
}


def _app_js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "breakdown" / "static" / "app.js").read_text()


def test_the_ui_default_preset_is_the_window_the_warm_pass_fits():
    import re

    source = _app_js()
    literal = re.search(r"^const DEFAULT_PRESET = \{([^}]*)\};", source, flags=re.M)
    assert literal, (
        "app.js no longer declares `const DEFAULT_PRESET = {...};` on one line. If it "
        "moved or changed shape, update this reader; do not let the warm pass stop "
        "being checked against the window the UI opens on."
    )
    default = dict(re.findall(r'(\w+):\s*"([^"]+)"', literal.group(1)))
    assumed = {grain: preset for grain, (preset, _) in _ENGINE_DEFAULT_WINDOW.items()}
    assert default == assumed, (
        f"app.js DEFAULT_PRESET is {default}, and `grains.default_analysis_window` "
        f"(what BREAKDOWN_WARM=latest fits ahead of the click) implements {assumed}. "
        "The warm pass would fit a window the UI never requests. Change the two together."
    )
    for grain, (preset, expressions) in _ENGINE_DEFAULT_WINDOW.items():
        block = re.search(
            r'id: "%s",.*?compute\(start, end\) \{(.*?)\n    \},' % re.escape(preset),
            source,
            flags=re.S,
        )
        assert block, f"app.js WINDOW_PRESETS has no `{preset}` entry with a compute(start, end)"
        for expression in expressions:
            assert re.search(expression, block.group(1)), (
                f"app.js preset `{preset}` (the {grain}-grain default) no longer computes "
                f"with `{expression}`: {block.group(1).strip()!r}. "
                "`grains.default_analysis_window` mirrors the old arithmetic."
            )


def test_the_ui_presets_and_the_engine_agree_on_every_date():
    """The same comparison by execution: the preset code is cut out of app.js
    and run under node against `default_analysis_window`, over every data edge
    in a 70-day span (every weekday, two month ends, a leap day) and data
    starts on both sides of each preset's "too short" rule. This is the half
    that sees a change *inside* `lastFullWeeks` or `lastFullMonths`, which the
    literal check above cannot. Skipped where node is not installed."""
    import json
    import shutil
    import subprocess

    import pandas as pd

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the literal check above still ran")
    source = _app_js()
    begin = source.index("/* ---------- date / window helpers")
    end = source.index("\n", source.index("const DEFAULT_PRESET = ")) + 1
    ends = [str(d.date()) for d in pd.date_range("2024-01-20", periods=70)]
    starts = ["2023-06-01", "2024-01-01", "2024-01-15", "2024-02-01", "2024-02-26", "2024-03-01"]
    cases = [(s, e, g) for g in ("day", "week", "month") for s in starts for e in ends if s < e]
    script = (
        source[begin:end]
        + "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        + "console.log(JSON.stringify(cases.map(([s, e, g]) => {\n"
        + "  const preset = WINDOW_PRESETS.find((p) => p.id === DEFAULT_PRESET[g]);\n"
        + "  const w = preset.compute(new Date(s), new Date(e));\n"
        + "  return w ? [w.anStart, w.anEnd] : null;\n"
        + "})));\n"
    )
    out = subprocess.run(
        [node, "-e", script], input=json.dumps(cases), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    differ = []
    for (s, e, g), from_ui in zip(cases, json.loads(out.stdout)):
        engine = default_analysis_window(s, e, g)
        if (None if engine is None else list(engine)) != from_ui:
            differ.append((s, e, g, from_ui, engine))
    assert not differ, (
        f"{len(differ)} of {len(cases)} (data start, data edge, grain) cases where app.js's "
        "default preset and `default_analysis_window` name different windows; first: "
        f"start={differ[0][0]} edge={differ[0][1]} grain={differ[0][2]} "
        f"ui={differ[0][3]} engine={differ[0][4]}"
    )

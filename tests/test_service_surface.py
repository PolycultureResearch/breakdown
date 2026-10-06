"""The service surface holds one policy per question, on both doors.

Grill 2026-10-05 found the same defect four times in `api/` and `mcp/`: a
policy applied on the HTTP side and not carried to the MCP side, or applied
on one route and not its neighbour — the engine guard, the refusal classes,
the `sql`/`bind` redaction, the raw `load_error`. Each fix here moved the
policy into one function both doors call, so most tests below come in pairs:
the behaviour that was wrong, and a structural scan that fails when a new
call site goes around the function.

Nothing here samples. Engine calls are stubbed at the seam the routes call
through, and the warm tests stub `fit_rca_node`.
"""

import ast
import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import breakdown.api.main as main  # noqa: E402
import breakdown.mcp.server as mcp_server  # noqa: E402
from breakdown.api.main import app  # noqa: E402
from breakdown.api.trees import (  # noqa: E402
    EngineBusy,
    SliceQueryFailed,
    WarmGate,
    is_sampling_failure,
    refusal_message,
)

PACKAGE = Path(main.__file__).resolve().parents[1]
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
TOKEN = {"Authorization": "Bearer s3cret"}

SLICE_WINDOWS = {
    "dimension": "region",
    "reference_start": "2025-03-01",
    "reference_end": "2025-03-14",
    "analysis_start": "2025-03-15",
    "analysis_end": "2025-03-28",
}

_EXPORT_TREE = """
provider: {{type: duckdb, data_dir: ./exports}}
metrics:
  - name: units
    source: x.units
    grain: day
    dimensions:
      region: {{source: region, top_k: 3}}
    bind:
      relation: orders
      grain_key: order_id
      time_column: created_at
      agg: sum
      measure: {measure}
      dimensions: {{region: {{column: {region_column}}}}}
"""


def _client():
    # The MCP transport's DNS-rebinding protection admits localhost hosts only.
    return TestClient(app, base_url="http://127.0.0.1:9090", raise_server_exceptions=False)


def _call_tool(client, name, arguments, headers=None):
    resp = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**MCP_HEADERS, **(headers or {})},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


@pytest.fixture
def export_env(tmp_path, monkeypatch):
    """A one-metric `duckdb` tree over a March 2025 CSV — the provider whose
    definitions carry a `bind` block and whose errors quote generated SQL.
    `use(...)` writes the tree and points the server at it."""
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    exports = tmp_path / "exports"
    exports.mkdir()
    rows = "".join(
        f"{i},2025-03-{d:02d},{'A' if i % 2 else 'B'},{i + 1}\n"
        for i, d in enumerate(range(1, 29), start=1)
    )
    (exports / "orders.csv").write_text("order_id,created_at,region,qty\n" + rows)
    path = tmp_path / "tree.yml"
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2025-03-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2025-03-28")
    monkeypatch.delenv("BREAKDOWN_API_TOKEN", raising=False)
    monkeypatch.delenv("BREAKDOWN_REQUIRE_AUTH", raising=False)

    def use(measure="qty", region_column="region"):
        path.write_text(_EXPORT_TREE.format(measure=measure, region_column=region_column))
        monkeypatch.setenv("BREAKDOWN_TREE", str(path))
        return path

    return use


# --- H6: one redaction, every route that serializes a definition --------------


def test_metric_route_redacts_the_bind_block_dag_redacts(export_env, monkeypatch):
    """`GET /metrics/{name}` returned `definition.bind` whole — relation,
    columns, dimensions — to the caller `/dag` had nulled it for and
    `/metrics/{name}/query` had refused with a 403."""
    export_env()
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        dag = dict(client.get("/dag").json()["nodes"])
        assert dag["units"]["bind"] is None and dag["units"]["sql"] is None
        assert client.get("/metrics/units/query").status_code == 403

        anonymous = client.get("/metrics/units").json()["definition"]
        assert anonymous["bind"] is None and anonymous["sql"] is None
        # Redacted to null, not dropped, and nothing else is touched.
        assert anonymous["dimensions"]["region"]["source"] == "region"

        presented = client.get("/metrics/units", headers=TOKEN).json()["definition"]
        assert presented["bind"]["relation"] == "orders"
        assert presented == dict(client.get("/dag", headers=TOKEN).json()["nodes"])["units"]


def test_without_a_token_nothing_is_redacted(export_env):
    """The laptop default: no token is configured, so there is nothing to
    protect anything with and every route behaves as it always did."""
    export_env()
    with _client() as client:
        assert client.get("/metrics/units").json()["definition"]["bind"]["relation"] == "orders"


def test_no_definition_is_serialized_around_the_redaction_helper():
    """Structural: a `.model_dump()` anywhere in `api/` or `mcp/` is either
    inside `_definition_payload` or on a model that is not a metric
    definition, named here. The H6 leak was a second `metric.model_dump()`
    one screen below the loop that redacted the first."""
    # (enclosing function, receiver) pairs that dump something else: the
    # index card's `tree.goal`, and the scenario echoed into a deep link.
    not_definitions = {("_tree_card", "goal"), ("run_whatif", "scenario")}
    offenders = []
    for path in sorted((PACKAGE / "api").glob("*.py")) + sorted((PACKAGE / "mcp").glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "model_dump"
                ):
                    receiver = ast.unparse(call.func.value)
                    if fn.name == "_definition_payload" or (fn.name, receiver) in not_definitions:
                        continue
                    offenders.append(
                        f"{path.name}:{call.lineno} {fn.name}: {receiver}.model_dump()"
                    )
    assert not offenders, (
        f"{offenders}: a pydantic model is serialized outside `_definition_payload`. "
        "If it is a MetricDefinition, route it through the helper — `sql` and `bind` "
        "are redacted behind BREAKDOWN_API_TOKEN there and nowhere else. If it is "
        "not, name it in `not_definitions` with the reason."
    )


def _leaks(text: str, tmp_path) -> list:
    return [needle for needle in ("qtyy", "bd_fact", "SUM(", str(tmp_path)) if needle in text]


def test_a_failed_load_does_not_put_the_driver_error_on_open_routes(
    export_env, monkeypatch, tmp_path
):
    """With a token configured, every data route's 503 and the `/trees` card
    carried the raw DuckDB error — the generated SQL fragment and the server's
    absolute path to the tree. C43 had scrubbed `/health` only."""
    export_env(measure="qtyy")
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        assert client.get("/health").json()["error_kind"] == "data_load_error"

        for path in ("/meta", "/dag", "/series", "/metrics/units", "/metrics/units/ppc"):
            resp = client.get(path)
            assert resp.status_code == 503, path
            detail = resp.json()["detail"]
            assert not _leaks(detail, tmp_path), (path, detail)
            assert "data_load_error" in detail and "BREAKDOWN_API_TOKEN" in detail

        card = client.get("/trees").json()["trees"][0]
        assert card["state"] == "error" and card["load_error_kind"] == "data_load_error"
        assert card["load_error"] and not _leaks(card["load_error"], tmp_path)
        assert not _leaks(client.post("/trees/tree/load").text, tmp_path)

        # The caller who presents the token gets the diagnostic, on both.
        assert "qtyy" in client.get("/meta", headers=TOKEN).json()["detail"]
        assert "qtyy" in client.get("/trees", headers=TOKEN).json()["trees"][0]["load_error"]
        # And `/health` stays the same body for everyone.
        assert not _leaks(client.get("/health", headers=TOKEN).text, tmp_path)


def test_a_failed_load_is_still_fully_diagnosed_when_no_token_is_set(export_env, tmp_path):
    export_env(measure="qtyy")
    with _client() as client:
        assert "qtyy" in client.get("/meta").json()["detail"]
        card = client.get("/trees").json()["trees"][0]
        assert "qtyy" in card["load_error"] and card["load_error_kind"] == "data_load_error"


def test_a_healthy_tree_card_has_a_null_error_kind():
    with _client() as client:
        card = client.get("/trees").json()["trees"][0]
        assert card["load_error"] is None and card["load_error_kind"] is None


def test_a_discovery_failure_does_not_put_the_tree_path_on_open_routes(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "tree.yml"
    monkeypatch.setenv("BREAKDOWN_TREE", str(missing))
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        index = client.get("/trees").json()
        assert index["discovery_error"] and str(tmp_path) not in index["discovery_error"]
        meta = client.get("/meta")
        assert meta.status_code == 503 and str(tmp_path) not in meta.text
        assert "discovery_error" in meta.json()["detail"]
        assert str(missing) in client.get("/trees", headers=TOKEN).json()["discovery_error"]
        assert str(missing) in client.get("/meta", headers=TOKEN).json()["detail"]


# --- H7: MCP tools run inside the engine guard --------------------------------


def _tool_calls():
    return [
        (
            "run_rca",
            "_engine_run_rca",
            {"target": "revenue", "analysis_start": "2024-03-27", "analysis_end": "2024-04-09"},
        ),
        (
            "run_whatif",
            "run_scenario",
            {
                "baseline_start": "2024-03-01",
                "baseline_end": "2024-04-09",
                "interventions": [{"metric": "daily_sessions", "mode": "pct", "value": 0.1}],
            },
        ),
    ]


@pytest.mark.parametrize("tool, engine_fn, arguments", _tool_calls())
def test_an_orphaned_run_refuses_mcp_tools_as_it_refuses_http(
    monkeypatch, tool, engine_fn, arguments
):
    """With the guard held (an orphaned engine thread, roadmap C41) HTTP
    answered 409 and the MCP tool ran straight through, at the same moment."""
    ran = []
    monkeypatch.setattr(mcp_server, engine_fn, lambda *a, **k: ran.append(tool))
    with _client() as client:
        tree = app.state.trees[app.state.default_tree]
        assert tree.engine_guard.acquire(blocking=False)
        try:
            res = _call_tool(client, tool, arguments)
        finally:
            tree.engine_guard.release()
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert "still finishing an analysis" in text and "Nothing was run" in text
        assert ran == [], "the engine was entered beside the orphan"


def test_an_orphaned_run_refuses_mcp_slice_metric(export_env):
    export_env()
    with _client() as client:
        tree = app.state.trees[app.state.default_tree]
        args = {"name": "units", **SLICE_WINDOWS}
        assert tree.engine_guard.acquire(blocking=False)
        try:
            http = client.post("/rca/units/slices", params=SLICE_WINDOWS)
            res = _call_tool(client, "slice_metric", args)
        finally:
            tree.engine_guard.release()
        assert http.status_code == 409
        assert res["isError"] is True and "still finishing" in res["content"][0]["text"]
        # Released, the same call runs.
        assert _call_tool(client, "slice_metric", args)["isError"] is False


def test_mcp_tools_hold_the_guard_while_the_engine_runs(monkeypatch):
    """The other half: the tool's own run takes the guard, so *its* orphan is
    one the next caller is refused for."""
    held = []

    def stub(*args, **kwargs):
        held.append(app.state.trees[app.state.default_tree].engine_guard.locked())
        raise ValueError("stop here")

    monkeypatch.setattr(mcp_server, "_engine_run_rca", stub)
    with _client() as client:
        _call_tool(client, *[_tool_calls()[0][i] for i in (0, 2)])
        assert held == [True]
        assert not app.state.trees[app.state.default_tree].engine_guard.locked()


def test_every_engine_thread_starts_inside_the_guard():
    """Structural: every `asyncio.to_thread(...)` in `api/main.py` and
    `mcp/server.py`, and every `_in_daemon_thread(...)`, hands its callee to
    `guarded`. The three MCP call sites that did not were found by reading;
    a fourth should be found by this."""
    # Not engine work on a tree's caches: the data load (which runs before
    # there is anything to guard), a memoized summary of an immutable fit,
    # and a provider metadata round-trip.
    unguarded_by_design = {"load_tree", "_fit_summary", "tree.fetcher.earliest_date"}
    offenders, seen = [], 0
    for rel in ("api/main.py", "mcp/server.py"):
        for call in ast.walk(ast.parse((PACKAGE / rel).read_text())):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            name = ast.unparse(call.func)
            if name not in ("asyncio.to_thread", "_in_daemon_thread"):
                continue
            seen += 1
            callee = ast.unparse(call.args[0])
            if callee != "guarded" and callee not in unguarded_by_design:
                offenders.append(f"{rel}:{call.lineno} {name}({callee}, …)")
    assert seen >= 8, "the thread-launch scan found too little — has the idiom moved?"
    assert not offenders, (
        f"{offenders}: engine work launched on a thread without the tree's engine "
        "guard (roadmap C41). Pass `guarded, tree, fn, …` — or, in mcp/server.py, "
        "call `_engine(state, fn, …)`."
    )


# --- M6 / L3: one judgement of what a refusal is ------------------------------


class ParallelSamplingError(Exception):
    """Stands in for `pymc.sampling.parallel.ParallelSamplingError`: same
    name, and like the real one neither a ValueError nor a RuntimeError."""


def test_the_refusal_classes_are_one_set_for_both_surfaces():
    """`_REFUSALS`' comment claimed parity with HTTP while missing
    `RuntimeError`; each member is now checked against the shared judgement."""
    for cls in mcp_server._REFUSALS:
        assert refusal_message(cls("why")) == "why", cls
    assert RuntimeError in mcp_server._REFUSALS
    assert refusal_message(KeyError("beta_raw")) is None, "a crash stays a crash"
    assert refusal_message(EngineBusy("busy")) is None, "busy is a 409, not a 422"
    assert "BinderException" in refusal_message(
        SliceQueryFailed("m", "d", "s", type("BinderException", (Exception,), {})("no column"))
    )


def test_a_failed_sampler_is_recognized_by_name_without_importing_pymc():
    err = ParallelSamplingError("Chain 2 failed")
    assert is_sampling_failure(err)
    assert not isinstance(err, (ValueError, RuntimeError))
    assert refusal_message(err) == (
        "The model could not be sampled (ParallelSamplingError): Chain 2 failed"
    )
    assert not is_sampling_failure(KeyError("x"))


def test_the_real_pymc_class_is_the_one_matched():
    parallel = pytest.importorskip("pymc.sampling.parallel")
    real = parallel.ParallelSamplingError
    assert not issubclass(real, (ValueError, RuntimeError)), (
        "PyMC changed its base class; the name match is now redundant, not wrong"
    )
    assert is_sampling_failure(real.__new__(real))


def test_mcp_surfaces_a_runtime_error_and_a_failed_sampler_and_not_a_crash():
    @mcp_server._surface_refusals
    async def raises(exc):
        raise exc

    with pytest.raises(ToolError, match="no whole month period"):
        asyncio.run(raises(RuntimeError("'signups' runs no whole month period")))
    with pytest.raises(ToolError, match="could not be sampled"):
        asyncio.run(raises(ParallelSamplingError("Chain 0 failed")))
    with pytest.raises(ToolError, match="Retry in a moment"):
        asyncio.run(raises(EngineBusy("Tree 't' is still finishing. Retry in a moment.")))
    with pytest.raises(KeyError):
        asyncio.run(raises(KeyError("beta_raw")))


def test_run_whatif_carries_the_message_http_carries(monkeypatch):
    """Over the wire: the per-metric-window refusal is a `RuntimeError`, which
    HTTP returned as a readable 422 and MCP as `Error executing tool
    run_whatif` and nothing else."""
    message = "No overlapping whole 'month' periods across 'mrr' and its parents ['signups']"

    def refuse(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_scenario", refuse)
    monkeypatch.setattr(main, "run_scenario", refuse)
    body = _tool_calls()[1][2]
    with _client() as client:
        http = client.post("/simulate", json=body)
        assert http.status_code == 422 and http.json()["detail"] == message
        res = _call_tool(client, "run_whatif", body)
        assert res["isError"] is True and message in res["content"][0]["text"]


_ANALYSIS = "analysis_start=2024-03-27&analysis_end=2024-04-09"
_ENGINE_ROUTES = [
    ("fit_metric", "post", "/analyze/order_count", None),
    ("shapley_attribution", "get", f"/shapley/revenue?{_ANALYSIS}", None),
    ("run_rca", "post", f"/rca/revenue?{_ANALYSIS}", None),
    ("run_scenario", "post", "/simulate", _tool_calls()[1][2]),
]


@pytest.mark.parametrize("engine_fn, method, url, body", _ENGINE_ROUTES)
@pytest.mark.parametrize(
    "error, status",
    [
        (ValueError("too little history"), 422),
        (RuntimeError("empty grain join"), 422),
        (ParallelSamplingError("Chain 1 failed"), 422),
        (KeyError("beta_raw"), 500),
    ],
)
def test_every_analysis_route_maps_refusals_to_422_and_crashes_to_500(
    monkeypatch, engine_fn, method, url, body, error, status
):
    """One table for the four routes. `/analyze` had no handler at all — a
    `ValueError` from `fit_metric` was a 500 — and none of them knew a
    `ParallelSamplingError`."""

    def raises(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, engine_fn, raises)
    with _client() as client:
        resp = client.request(method, url, json=body)
        assert resp.status_code == status, resp.text
        if status == 422:
            assert str(error.args[0]) in resp.json()["detail"]


# --- M7: a slice query the source refuses is a 422 that names the dimension ---


def test_a_mistyped_dimension_column_is_a_422_naming_the_dimension(export_env):
    export_env(region_column="regionn")
    with _client() as client:
        resp = client.post("/rca/units/slices", params=SLICE_WINDOWS)
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "Slicing 'units' by 'region' failed" in detail
        assert "BinderException" in detail and "breakdown doctor" in detail
        # No token configured: the driver's own words ride along.
        assert "Cause:" in detail and "regionn" in detail


def test_the_slice_failures_cause_follows_the_sql_redaction(export_env, monkeypatch):
    export_env(region_column="regionn")
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        anonymous = client.post("/rca/units/slices", params=SLICE_WINDOWS)
        assert anonymous.status_code == 422
        detail = anonymous.json()["detail"]
        assert "by 'region'" in detail and "BinderException" in detail
        assert "regionn" not in detail and "Cause:" not in detail

        presented = client.post("/rca/units/slices", params=SLICE_WINDOWS, headers=TOKEN)
        assert presented.status_code == 422 and "regionn" in presented.json()["detail"]

        # MCP is behind the token, so the tool carries the cause too.
        res = _call_tool(client, "slice_metric", {"name": "units", **SLICE_WINDOWS}, TOKEN)
        text = res["content"][0]["text"]
        assert res["isError"] is True and "by 'region'" in text and "regionn" in text


def test_a_runtime_error_inside_the_slice_engine_is_a_422(export_env, monkeypatch):
    """The route caught `ValueError` and `SliceNotSupported` only, a release
    after its neighbours learned `RuntimeError` (roadmap C38)."""
    export_env()

    def refuse(*args, **kwargs):
        raise RuntimeError("no whole period in the reference window")

    monkeypatch.setattr(main, "slice_attribution", refuse)
    with _client() as client:
        resp = client.post("/rca/units/slices", params=SLICE_WINDOWS)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "no whole period in the reference window"


def test_a_crash_inside_the_slice_engine_is_still_a_500(export_env, monkeypatch):
    export_env()

    def crash(*args, **kwargs):
        raise KeyError("slice")

    monkeypatch.setattr(main, "slice_attribution", crash)
    with _client() as client:
        assert client.post("/rca/units/slices", params=SLICE_WINDOWS).status_code == 500


# --- H3 / L12: what the open routes say ---------------------------------------


def _blank(tree, *names):
    """Overwrite metrics' loaded series with zeros: what an empty result
    looks like once the boundary has aligned it."""
    data = tree.data
    for grain, frame in list(data.frames.items()):
        frame = frame.copy()
        for name in names:
            if name in frame.columns:
                frame[name] = 0.0
        data.frames[grain] = frame


def test_health_does_not_invent_a_data_edge_from_the_requested_window():
    """`/health` promised "never a date invented from the requested window"
    and published exactly that for a load in which nothing was observed: every
    flow filled with zeros to the requested end date, `data_through` that date."""
    with _client() as client:
        tree = app.state.trees[app.state.default_tree]
        every = sorted(tree.data.grain_of)
        before = client.get("/health").json()
        assert before["data_through"] == "2024-04-09" and before["no_nonzero_data"] == []

        # One blank metric among live ones: named, and the edge is unmoved —
        # its own "edge" was the window's end, the latest any series can have.
        _blank(tree, "daily_sessions")
        one = client.get("/health").json()
        assert one["no_nonzero_data"] == ["daily_sessions"]
        assert one["data_through"] == "2024-04-09"

        _blank(tree, *every)
        none = client.get("/health").json()
        assert none["status"] == "ok" and none["no_nonzero_data"] == every
        assert none["data_through"] is None
        assert none["data_through_bounded_by"] == []


def test_a_window_that_misses_the_data_never_reports_a_data_edge(export_env, monkeypatch):
    """End to end, and true on either side of the loader's own refusal of an
    all-empty load: degraded has no `data_through`, and `ok` reports null."""
    export_env()
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")
    with _client() as client:
        body = client.get("/health").json()
        assert body.get("data_through") is None, body
        if body["status"] == "ok":
            assert body["no_nonzero_data"] == ["units"]


def test_open_routes_name_no_metric_to_a_caller_without_the_token(monkeypatch):
    """One policy for `/health` and `/manifest`: with a token configured, a
    caller who has not presented it reads counts, not names."""
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    for require_auth in ("", "1"):
        monkeypatch.setenv("BREAKDOWN_REQUIRE_AUTH", require_auth)
        with _client() as client:
            tree = app.state.trees[app.state.default_tree]
            _blank(tree, "daily_sessions")
            names = set(tree.data.grain_of)

            anonymous = client.get("/health")
            body = anonymous.json()
            assert body["status"] == "ok" and body["data_through"] == "2024-04-09"
            for key in ("data_through_bounded_by", "short_series", "sparse_fills"):
                assert body[key] is None, key
            assert body["no_nonzero_data"] is None
            assert body["withheld"] == {
                "data_through_bounded_by": 0,
                "short_series": 0,
                "sparse_fills": 0,
                "no_nonzero_data": 1,
            }
            assert not [n for n in names if n in anonymous.text]
            assert not [n for n in names if n in client.get("/manifest").text]

            presented = client.get("/health", headers=TOKEN).json()
            assert presented["no_nonzero_data"] == ["daily_sessions"]
            assert presented["short_series"] == {} and "withheld" not in presented


def test_the_withheld_count_of_short_series_counts_metrics():
    record = {
        "day": {
            "trailing": {"reach": "2024-04-09", "short": {"a": {}, "b": {}}},
            "leading": {"reach": "2024-01-01", "short": {"b": {}}},
        },
        "week": {"trailing": {"reach": "2024-04-07", "short": {"c": {}}}},
    }
    assert main._short_series_metrics(record) == {"a", "b", "c"}
    assert main._short_series_metrics({}) == set()


# --- M5: the warm pass --------------------------------------------------------

_WARM_TREE = """
provider:
  type: mock
metrics:
  - name: {prefix}_sessions
    source: w.{prefix}_sessions
  - name: {prefix}_orders
    source: w.{prefix}_orders
    parents: [{prefix}_sessions]
"""


@pytest.fixture
def warm_trees(tmp_path, monkeypatch):
    """Two mock trees in a directory (so both load lazily, on request), with
    the warm on and its `EngineBusy` backoff shortened."""
    for prefix in ("a", "b"):
        (tmp_path / f"{prefix}.yml").write_text(_WARM_TREE.format(prefix=prefix))
    monkeypatch.setenv("BREAKDOWN_TREE", str(tmp_path))
    monkeypatch.setenv("BREAKDOWN_WARM", "latest")
    monkeypatch.setattr(main, "_WARM_BUSY_BACKOFF", (0.02, 0.05))


def _wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _warm(client, tree_id):
    return client.get(f"/trees/{tree_id}/meta").json()["warm"]


def _settled(client, tree_id):
    return lambda: _warm(client, tree_id).get("status") not in ("planning", "running")


def test_warm_fits_run_one_at_a_time_across_trees(warm_trees, monkeypatch):
    """A task per tree behind a lock per tree is N samplers for N trees: two
    measured on two. The gate is process-wide, so the peak is one."""
    live, peak, lock = [0], [0], threading.Lock()

    def stub(dag, data, node, fit_end):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.3)
        with lock:
            live[0] -= 1
        raise ValueError("stub: nothing fitted")

    monkeypatch.setattr(main, "fit_rca_node", stub)
    with _client() as client:
        for tree_id in ("a", "b"):
            assert client.post(f"/trees/{tree_id}/load").status_code == 200
        for tree_id in ("a", "b"):
            _wait_for(_settled(client, tree_id), what=f"warm of {tree_id}")
            warm = _warm(client, tree_id)
            assert warm["status"] == "done" and warm["done"] == warm["total"] == 1
            # A refusal is one unfittable node: recorded, and the warm goes on.
            assert list(warm["failed"].values()) == ["stub: nothing fitted"]
        assert peak[0] == 1, f"{peak[0]} warm fits ran at once"


def test_the_warm_gate_is_process_state_not_tree_state():
    """The multi-tree agreement: what bounds the process does not live on a
    `TreeState`, where there would be one per tree and so no bound at all."""
    import dataclasses

    from breakdown.api.trees import TreeState

    assert not [f.name for f in dataclasses.fields(TreeState) if "gate" in f.name]
    with _client():
        assert isinstance(app.state.warm_gate, WarmGate)
        assert app.state.warm_gate.stopping is False


def test_an_unexpected_error_ends_the_warm_as_failed_and_is_logged(warm_trees, monkeypatch, caplog):
    """A `KeyError` killed the task; `/meta` reported `running, done: 0` for
    the life of the process and nothing was logged."""

    def stub(dag, data, node, fit_end):
        raise KeyError("some_internal_key")

    monkeypatch.setattr(main, "fit_rca_node", stub)
    with caplog.at_level(logging.ERROR, logger="breakdown.api.main"):
        with _client() as client:
            client.post("/trees/a/load")
            _wait_for(_settled(client, "a"), what="warm of a")
            warm = _warm(client, "a")
            assert warm["status"] == "failed"
            assert warm["error"] == "KeyError: 'some_internal_key'"
            assert warm["done"] == 0 and warm["total"] == 1
            assert app.state.trees["a"].warm_task.exception() is None
            # Serving is untouched.
            assert client.get("/trees/a/meta").status_code == 200
    logged = [r for r in caplog.records if "warm: tree 'a' failed" in r.getMessage()]
    assert logged and logged[0].exc_info, "the failure is logged with its traceback"


def test_a_failed_sampler_is_one_failed_fit_not_a_dead_warm(warm_trees, monkeypatch):
    def stub(dag, data, node, fit_end):
        raise ParallelSamplingError("Chain 3 failed")

    monkeypatch.setattr(main, "fit_rca_node", stub)
    with _client() as client:
        client.post("/trees/a/load")
        _wait_for(_settled(client, "a"), what="warm of a")
        warm = _warm(client, "a")
        assert warm["status"] == "done" and warm["done"] == 1
        assert "could not be sampled" in next(iter(warm["failed"].values()))


def test_the_warm_waits_out_an_orphaned_run_instead_of_counting_it(warm_trees, monkeypatch):
    """`EngineBusy` used to be recorded under `failed` and the fit counted as
    done — a fit reported as attempted that never started."""
    calls = []
    monkeypatch.setattr(main, "fit_rca_node", lambda dag, data, node, fit_end: calls.append(node))
    with _client() as client:
        tree = app.state.trees["a"]
        assert tree.engine_guard.acquire(blocking=False)  # the orphan
        try:
            client.post("/trees/a/load")
            time.sleep(0.4)  # several backoffs' worth
            warm = _warm(client, "a")
            assert warm["status"] == "planning" and "failed" not in warm
            assert calls == []
        finally:
            tree.engine_guard.release()
        _wait_for(_settled(client, "a"), what="warm of a")
        warm = _warm(client, "a")
        assert warm["status"] == "done" and warm["done"] == 1 and warm["failed"] == {}
        assert calls == ["a_orders"]


def test_shutdown_does_not_wait_for_a_warm_fit_in_flight(warm_trees, monkeypatch):
    """Shutdown waited out the whole in-flight fit (7.5s against an 8s stub;
    minutes for a real one). The fit's thread is a daemon now: abandoned, not
    joined."""
    started, release, threads = threading.Event(), threading.Event(), []

    def stub(dag, data, node, fit_end):
        threads.append(threading.current_thread())
        started.set()
        release.wait(30)
        raise ValueError("stub")

    monkeypatch.setattr(main, "fit_rca_node", stub)
    try:
        with _client() as client:
            client.post("/trees/a/load")
            assert started.wait(10), "the warm never reached its fit"
            tree = app.state.trees["a"]
            t0 = time.monotonic()
        elapsed = time.monotonic() - t0
        assert elapsed < 3, f"shutdown took {elapsed:.1f}s with a warm fit in flight"
        assert threads[0].daemon and threads[0].is_alive(), "abandoned, and still running"
        assert tree.warm["status"] == "cancelled"
        assert app.state.warm_gate.stopping is True
    finally:
        release.set()
        for thread in threads:
            thread.join(10)


def test_a_warm_told_to_stop_starts_nothing():
    """The flag is read before every step, so a warm between two fits ends
    there instead of beginning the next one."""
    gate = WarmGate()
    gate.stopping = True
    started = []

    async def step():
        started.append(1)

    async def run():
        await main._warm_step(None, gate, step)

    with pytest.raises(main._WarmStopped):
        asyncio.run(run())
    assert started == []

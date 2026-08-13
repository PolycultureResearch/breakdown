import asyncio
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from breakdown.api.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


CUSTOM_TREE = """
provider:
  type: mock

metrics:
  - name: signups
    source: my_project.metrics.signups
  - name: activations
    source: my_project.metrics.activations
    parents: [signups]
"""


def test_lifespan_fetches_data_via_provider():
    """Startup should populate app.state.data from the configured provider,
    with one column per metric in the tree."""
    with TestClient(app) as client:
        frame = app.state.data.frame("day")
        metric_names = [m.name for m in app.state.parser.config.metrics]
        assert list(frame.columns) == ["date"] + metric_names
        assert len(frame) == 100  # default window 2024-01-01..2024-04-09
        assert not frame.isna().any().any()

        resp = client.get("/dag")
        assert resp.status_code == 200
        assert len(resp.json()["nodes"]) == len(metric_names)

        resp = client.get("/metrics/revenue")
        assert resp.status_code == 200
        assert len(resp.json()["time_series"]) == 100


def test_lifespan_respects_env_config(tmp_path, monkeypatch):
    """BREAKDOWN_TREE / BREAKDOWN_*_DATE env vars should control which tree
    is loaded and which window is fetched."""
    tree_file = tmp_path / "custom_tree.yml"
    tree_file.write_text(CUSTOM_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-06-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-06-30")

    with TestClient(app) as client:
        frame = app.state.data.frame("day")
        assert list(frame.columns) == ["date", "signups", "activations"]
        assert len(frame) == 30

        resp = client.get("/metrics/signups")
        assert resp.status_code == 200
        assert resp.json()["definition"]["source"] == "my_project.metrics.signups"


def test_meta_endpoint():
    """GET /meta returns UI bootstrap info reflecting current state."""
    with TestClient(app) as client:
        resp = client.get("/meta")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "mock"
        assert body["metrics"] == [m.name for m in app.state.parser.config.metrics]
        assert body["date_start"] == "2024-01-01"
        assert body["date_end"] == "2024-04-09"
        assert body["grains"]["revenue"] == "day"
        assert body["kinds"]["revenue"] == "flow"
        # Mock data covers the full window, so every metric is fresh through
        # the window end.
        assert body["data_through"]["revenue"] == "2024-04-09"
        assert body["fitted"] == []


def test_series_endpoint_per_metric_grain():
    """GET /series returns per-metric {grain, dates, values} — mixed grains
    mean there is no single shared date axis."""
    with TestClient(app) as client:
        resp = client.get("/series")
        assert resp.status_code == 200
        metrics = resp.json()["metrics"]
        assert set(metrics) == {m.name for m in app.state.parser.config.metrics}
        rev = metrics["revenue"]
        assert rev["grain"] == "day"
        assert len(rev["dates"]) == len(rev["values"]) == 100
        assert rev["dates"][0] == "2024-01-01"


def test_metrics_summary_json_safe_after_advi():
    """ADVI traces have NaN r_hat (single chain); /metrics must still serialize."""
    with TestClient(app) as client:
        resp = client.post("/analyze/daily_sessions?inference_method=advi&draws=100")
        assert resp.status_code == 200

        resp = client.get("/metrics/daily_sessions")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary is not None
        assert "mean" in summary
        # NaN must have been converted to null, never leaked into the payload
        assert all(v is None or isinstance(v, float) for v in summary["r_hat"].values())


def test_analyze_accepts_fit_end_and_chains():
    """/analyze takes optional fit_end (exclusive cutoff) and chains; the fit is
    cached under (name, fit_end) and surfaces in /meta and /metrics."""
    with TestClient(app) as client:
        resp = client.post(
            "/analyze/daily_sessions?inference_method=advi&draws=100&fit_end=2024-03-01"
        )
        assert resp.status_code == 200

        resp = client.get("/meta")
        assert "daily_sessions" in resp.json()["fitted"]

        resp = client.get("/metrics/daily_sessions")
        assert resp.status_code == 200
        assert resp.json()["summary"] is not None

        # bad fit_end -> 422
        resp = client.post("/analyze/daily_sessions?fit_end=not-a-date")
        assert resp.status_code == 422


def test_rca_endpoint():
    """POST /rca/{name} returns the RCA contract; runs one ADVI fit."""
    windows = {
        "reference_start": "2024-01-01",
        "reference_end": "2024-02-15",
        "analysis_start": "2024-02-16",
        "analysis_end": "2024-04-09",
    }
    with TestClient(app) as client:
        resp = client.post("/rca/revenue", params=windows)
        assert resp.status_code == 200
        body = resp.json()
        assert {"target", "reference_window", "analysis_window", "nodes", "ranked_causes"} <= set(
            body
        )
        assert body["target"] == "revenue"
        assert body["nodes"]["revenue"]["attribution_method"] == "shapley"
        assert body["nodes"]["order_count"]["attribution_method"] == "posterior"
        assert len(body["ranked_causes"]) > 0

        # unknown metric -> 404
        resp = client.post("/rca/does_not_exist", params=windows)
        assert resp.status_code == 404

        # missing required window param -> 422
        resp = client.post("/rca/revenue")
        assert resp.status_code == 422


def test_simulate_endpoint():
    """POST /simulate returns the scenario contract; runs one ADVI fit on
    demand; identical requests give identical responses."""
    scenario = {
        "baseline_start": "2024-03-01",
        "baseline_end": "2024-04-09",
        "interventions": [{"metric": "daily_sessions", "mode": "pct", "value": 0.15}],
        "assumptions": [
            {
                "source": "discount_pct",
                "target": "average_order_value",
                "effect": {"kind": "relative", "low": -0.12, "high": -0.08},
                "note": "10% blanket discount",
            }
        ],
        "levers": [{"name": "discount_pct", "value": 10, "unit": "%"}],
    }
    with TestClient(app) as client:
        resp = client.post("/simulate", json=scenario)
        assert resp.status_code == 200
        body = resp.json()
        assert {"baseline_window", "sources", "nodes", "warnings", "caveats"} <= set(body)
        assert body["nodes"]["daily_sessions"]["status"] == "intervened"
        assert body["nodes"]["revenue"]["status"] == "affected"
        assert set(body["nodes"]) == {m.name for m in app.state.parser.config.metrics}
        assert [s["kind"] for s in body["sources"]] == ["intervention", "assumption"]

        # the on-demand fit is cached and visible in /meta
        assert "order_count" in client.get("/meta").json()["fitted"]

        # deterministic: same scenario, same response
        assert client.post("/simulate", json=scenario).json() == body


def test_simulate_validation_errors():
    with TestClient(app) as client:
        base = {"baseline_start": "2024-03-01", "baseline_end": "2024-04-09"}
        # unknown metric -> 422
        resp = client.post(
            "/simulate",
            json={**base, "interventions": [{"metric": "nope", "mode": "set", "value": 1.0}]},
        )
        assert resp.status_code == 422
        # empty scenario -> 422
        assert client.post("/simulate", json=base).status_code == 422
        # inverted effect range -> 422 (pydantic)
        resp = client.post(
            "/simulate",
            json={
                **base,
                "assumptions": [
                    {
                        "source": "x",
                        "target": "revenue",
                        "effect": {"kind": "absolute", "low": 2.0, "high": 1.0},
                    }
                ],
            },
        )
        assert resp.status_code == 422
        # inverted baseline window -> 422
        resp = client.post(
            "/simulate",
            json={
                "baseline_start": "2024-04-09",
                "baseline_end": "2024-03-01",
                "interventions": [{"metric": "revenue", "mode": "set", "value": 1.0}],
            },
        )
        assert resp.status_code == 422


@pytest.mark.anyio
async def test_analyze_does_not_block_event_loop():
    """Sampling runs in a worker thread, so a trivial request fired during an
    in-flight /analyze must complete before the sampling call returns."""
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            timings = {}

            async def do_analyze():
                await client.post(
                    "/analyze/daily_sessions?inference_method=advi&draws=50",
                    timeout=120,
                )
                timings["analyze"] = time.perf_counter()

            async def do_root():
                # Let /analyze acquire the lock and enter the worker thread first.
                await asyncio.sleep(0.1)
                resp = await client.get("/")
                timings["root"] = time.perf_counter()
                assert resp.status_code == 200

            await asyncio.gather(do_analyze(), do_root())

            # The root request must not have waited for sampling to finish.
            assert timings["root"] < timings["analyze"]


DEGRADED_TREE = """
provider:
  type: warehouse
  host: example.cloud.databricks.com
  token: tok
  http_path: /sql/1.0/warehouses/abc
metrics:
  - name: revenue
    source: cat.sch.revenue
"""


def test_health_ok():
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["provider"] == "mock"
        assert body["metrics"] == len(app.state.parser.config.metrics)


def test_degraded_startup_serves_instead_of_crashing(tmp_path, monkeypatch):
    """A bad provider config (here: warehouse metrics without `sql`, caught
    before any connection attempt) must not kill startup — the process
    serves, /health carries the error, data endpoints 503, the UI loads."""
    tree_file = tmp_path / "degraded_tree.yml"
    tree_file.write_text(DEGRADED_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))

    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert "sql" in health["error"]

        resp = client.get("/dag")
        assert resp.status_code == 503
        assert "doctor" in resp.json()["detail"]
        for path in ("/meta", "/series", "/metrics/revenue"):
            assert client.get(path).status_code == 503
        assert (
            client.post(
                "/rca/revenue?reference_start=2024-01-01&reference_end=2024-01-07&analysis_start=2024-01-08&analysis_end=2024-01-14"
            ).status_code
            == 503
        )

        assert client.get("/ui/").status_code == 200
        assert client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# Cold start mode: provider `none` — no data, what-if over declared beliefs

COLD_START_TREE = """
provider:
  type: none

metrics:
  - name: sessions
    source: assumed
    baseline: {low: 800, high: 1600}
    plausible: {min: 0}

  - name: signups
    source: assumed
    parents: [sessions]
    baseline: 40
    priors:
      sessions:
        distribution: "Normal"
        params: {mu: 0.03, sigma: 0.01}

  - name: aov
    source: assumed
    baseline: 50

  - name: revenue
    source: assumed
    parents: [signups, aov]
    formula: "signups * aov"
"""


@pytest.fixture
def cold_start_env(tmp_path, monkeypatch):
    tree_file = tmp_path / "cold_start_tree.yml"
    tree_file.write_text(COLD_START_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))


def test_cold_start_boots_ok_not_degraded(cold_start_env):
    """provider: none is a stated mode: startup fetches nothing, data stays
    None, and health reports ok — not the degraded path."""
    with TestClient(app) as client:
        assert app.state.startup_error is None
        assert app.state.data is None
        assert client.get("/health").json() == {
            "status": "ok",
            "provider": "none",
            "metrics": 4,
        }

        meta = client.get("/meta").json()
        assert meta["mode"] == "cold_start"
        assert meta["date_start"] is None and meta["date_end"] is None
        assert meta["fitted"] == [] and meta["data_through"] == {}
        assert meta["grains"]["sessions"] == "day"

        # the DAG and definitions still serve (they need no data)
        assert client.get("/dag").status_code == 200
        m = client.get("/metrics/revenue").json()
        assert m["time_series"] == [] and m["summary"] is None


def test_cold_start_guards_data_routes(cold_start_env):
    """Time-series endpoints are a stated impossibility on a dataless tree:
    422 pointing at /simulate, not a 500 or a degraded 503."""
    windows = {
        "reference_start": "2024-01-01",
        "reference_end": "2024-01-31",
        "analysis_start": "2024-02-01",
        "analysis_end": "2024-02-29",
    }
    with TestClient(app) as client:
        for resp in (
            client.get("/series"),
            client.post("/analyze/sessions"),
            client.get("/shapley/revenue", params=windows),
            client.post("/rca/revenue", params=windows),
        ):
            assert resp.status_code == 422
            assert "no data provider" in resp.json()["detail"]
            assert "/simulate" in resp.json()["detail"]


def test_cold_start_simulate(cold_start_env):
    """POST /simulate runs the cold-start engine: no baseline window, mode
    label, belief intervals; passing a window is rejected."""
    with TestClient(app) as client:
        resp = client.post(
            "/simulate",
            json={
                "interventions": [{"metric": "sessions", "mode": "pct", "value": 0.10}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "cold_start"
        assert body["baseline_window"] is None
        # the range-asserted root carries a belief interval; propagation reaches revenue
        assert body["nodes"]["sessions"]["baseline_ci_95"] is not None
        assert body["nodes"]["revenue"]["status"] == "affected"
        assert body["nodes"]["revenue"]["delta"]["estimate"] > 0

        resp = client.post(
            "/simulate",
            json={
                "baseline_start": "2024-01-01",
                "baseline_end": "2024-02-01",
                "interventions": [{"metric": "sessions", "mode": "pct", "value": 0.10}],
            },
        )
        assert resp.status_code == 422
        assert "baseline" in resp.json()["detail"]


def test_cold_start_not_ready_tree_serves_degraded(tmp_path, monkeypatch):
    """A provider-none tree missing declarations must fail loudly at startup
    with the full blocker list, not one 422 at a time on /simulate."""
    tree_file = tmp_path / "not_ready.yml"
    tree_file.write_text(COLD_START_TREE.replace("    baseline: {low: 800, high: 1600}\n", ""))
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert "not cold-start ready" in health["error"]
        assert "sessions" in health["error"]


# --- dimensional slicing endpoint ---

SLICED_TREE = """
provider:
  type: mock

metrics:
  - name: signups
    source: my_project.metrics.signups
    dimensions:
      region: customer__region
  - name: trial_starts
    source: my_project.metrics.trial_starts
    parents: [signups]
    dimensions:
      region: customer__region
  - name: conversion_rate
    source: my_project.metrics.conversion_rate
    kind: rate
    dimensions:
      region: { source: customer__region, weight: trial_starts }
"""

_SLICE_WINDOWS = {
    "dimension": "region",
    "reference_start": "2024-02-05",
    "reference_end": "2024-03-03",
    "analysis_start": "2024-03-04",
    "analysis_end": "2024-03-10",
}


@pytest.fixture
def sliced_env(tmp_path, monkeypatch):
    tree_file = tmp_path / "sliced_tree.yml"
    tree_file.write_text(SLICED_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))


def test_slice_endpoint_flow(sliced_env):
    with TestClient(app) as client:
        resp = client.post("/rca/signups/slices", params=_SLICE_WINDOWS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["attribution_method"] == "slice_sum"
        assert body["reconciliation"]["status"] == "ok"
        total = sum(r["contribution"] for r in body["slices"])
        assert abs(total - body["gap"]) < 1e-6
        # zero-sum excess: concentration is a reallocation, not new gap
        assert abs(sum(r["excess"] for r in body["slices"])) < 1e-6
        # the fetched frame landed in the slice cache
        assert len(app.state.slice_cache) == 1


def test_slice_endpoint_rate_blend(sliced_env):
    with TestClient(app) as client:
        resp = client.post("/rca/conversion_rate/slices", params=_SLICE_WINDOWS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["attribution_method"] == "slice_blend"
        assert body["reconciliation"]["status"] == "ok"
        assert body["mix_total"] is not None
        total = sum(r["within"] + r["mix"] for r in body["slices"])
        assert abs(total - body["gap"]) < 1e-6


def test_slice_endpoint_unknown_dimension_422(sliced_env):
    with TestClient(app) as client:
        resp = client.post("/rca/signups/slices", params={**_SLICE_WINDOWS, "dimension": "geo"})
        assert resp.status_code == 422
        assert "declares no dimension 'geo'" in resp.json()["detail"]


def test_slice_endpoint_unknown_metric_404(sliced_env):
    with TestClient(app) as client:
        resp = client.post("/rca/nope/slices", params=_SLICE_WINDOWS)
        assert resp.status_code == 404


def test_slice_endpoint_bad_date_422(sliced_env):
    with TestClient(app) as client:
        resp = client.post(
            "/rca/signups/slices",
            params={**_SLICE_WINDOWS, "analysis_end": "not-a-date"},
        )
        assert resp.status_code == 422
        assert "analysis_end" in resp.json()["detail"]


def test_slice_endpoint_provider_without_slicing_422(sliced_env):
    from breakdown.data_fetch import WarehouseDataFetcher

    with TestClient(app) as client:
        app.state.fetcher = WarehouseDataFetcher(host="h", http_path="p", token="t", metric_sql={})
        app.state.slice_cache.clear()
        resp = client.post("/rca/signups/slices", params=_SLICE_WINDOWS)
        assert resp.status_code == 422
        assert "does not support dimensional" in resp.json()["detail"]


def test_api_import_does_not_load_pymc():
    """Boot cost guard: `breakdown.api.main` must import without the inference
    stack (roadmap C14).

    pymc/arviz/pytensor are ~80% of the process's import time. On a shared-CPU
    VM that was ~27s of a ~43s startup — long enough that Fly's proxy gave up
    on the White Cube demo and served **503** to the first visitor after every
    idle period, which reads as a white screen. Deferring them is one
    `import pymc` at module scope away from silently regressing, and the symptom
    only shows up in production, so it is pinned here.

    A fresh interpreter is required: pytest has almost certainly imported pymc
    already via the engine tests.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import breakdown.api.main; "
        "print(','.join(m for m in ('pymc', 'arviz', 'pytensor') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = result.stdout.strip()
    assert leaked == "", (
        f"breakdown.api.main pulled in the inference stack ({leaked}). Import it "
        "inside the functions that use it — see the note at the top of "
        "breakdown/engine/model.py."
    )


def test_warm_inference_imports_actually_loads_the_stack():
    """The other half of the deferral: something must absorb the cost, or it
    just moves from startup onto the first *Run analysis* click."""
    import subprocess
    import sys

    probe = (
        "import sys; from breakdown.engine.model import warm_inference_imports; "
        "warm_inference_imports(); "
        "print(','.join(sorted(m for m in ('pymc', 'arviz', 'pytensor') if m in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "arviz,pymc,pytensor"


def test_lifespan_warms_the_inference_stack_in_the_background():
    """The deferral only helps if startup absorbs the cost it moved.

    Without this, the ~27s (shared-CPU) import lands on whoever clicks *Run
    analysis* first — a worse trade than the slow boot it replaced, and one
    that would show up only in a live demo. Runs in a subprocess because
    pytest has almost certainly imported pymc already.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
        import sys, time
        from fastapi.testclient import TestClient
        import breakdown.api.main as m
        assert "pymc" not in sys.modules, "pymc imported before lifespan ran"
        with TestClient(m.app) as c:
            c.get("/health")
            for _ in range(200):        # warm-up is a daemon thread; give it a moment
                if "pymc" in sys.modules:
                    break
                time.sleep(0.05)
        print("warmed=" + str("pymc" in sys.modules))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert "warmed=True" in result.stdout, (
        "lifespan did not warm the inference stack; the first fit will pay the "
        f"full import cost. stdout={result.stdout!r} stderr={result.stderr[-500:]!r}"
    )


# --- C8: one process, several viewers -------------------------------------


def test_meta_survives_concurrent_trace_mutation():
    """`/meta` used to iterate `app.state.traces` lazily while `run_rca`
    inserted into it from a worker thread — an intermittent 500 for one viewer
    exactly while another's analysis ran (C8).

    The race needs the GIL to switch *inside* the comprehension, so the switch
    interval is pinned tiny to make it near-certain rather than occasional:
    against the pre-fix code this reproduces ~95% of iterations, against the
    snapshot ~0%.
    """
    import sys
    import threading

    from breakdown.api.main import app

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    with TestClient(app) as client:
        traces = app.state.traces
        for i in range(400):
            traces[(f"synthetic_{i}", "2024-02-01")] = object()
        stop = threading.Event()

        def churn():
            i = 400
            while not stop.is_set():
                traces[(f"synthetic_{i}", "2024-02-01")] = object()
                traces.pop((f"synthetic_{i - 300}", "2024-02-01"), None)
                i += 1

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(40):
                assert client.get("/meta").status_code == 200
        finally:
            stop.set()
            t.join(timeout=5)
            sys.setswitchinterval(old_interval)
            for key in [k for k in list(traces) if k[0].startswith("synthetic_")]:
                traces.pop(key, None)


def test_a_cheap_refit_cannot_displace_a_better_cached_fit():
    """`/analyze` exposes `inference_method` and `draws`; the cache key carries
    neither. One viewer running a 50-draw ADVI fit would silently replace the
    NUTS fit another viewer's analysis had already paid for (C8)."""
    from breakdown.api.main import _remember_fit

    class Fit:
        def __init__(self, method, draws):
            self.inference_method = method
            self.trace = type("T", (), {"posterior": type("P", (), {"sizes": {"draw": draws}})()})()

    traces = {}
    good = Fit("nuts", 1000)
    _remember_fit(traces, ("sessions", None), good)
    _remember_fit(traces, ("sessions", None), Fit("advi", 50))
    assert traces[("sessions", None)] is good, "a 50-draw ADVI fit displaced NUTS"

    # The deliberate upgrade still works: confirming an ADVI fit with NUTS.
    traces2 = {}
    _remember_fit(traces2, ("sessions", None), Fit("advi", 500))
    better = Fit("nuts", 1000)
    _remember_fit(traces2, ("sessions", None), better)
    assert traces2[("sessions", None)] is better


def test_trace_cache_is_bounded():
    """Unbounded, each entry holding every posterior draw: on the public demo
    every visitor picks their own windows, so the process OOMs eventually with
    nobody doing anything wrong (C8)."""
    from breakdown.api.main import MAX_CACHED_TRACES, _remember_fit

    traces = {}
    for i in range(MAX_CACHED_TRACES + 25):
        _remember_fit(traces, (f"m{i}", None), object())
    assert len(traces) == MAX_CACHED_TRACES
    assert ("m0", None) not in traces, "eviction should drop the oldest key first"
    assert (f"m{MAX_CACHED_TRACES + 24}", None) in traces


def test_rca_endpoint_defaults_reference():
    """Omitting both reference params defaults to the matched adjacent block;
    passing exactly one is a 422."""
    analysis = {"analysis_start": "2024-02-16", "analysis_end": "2024-04-09"}
    with TestClient(app) as client:
        resp = client.post("/rca/revenue", params=analysis)
        assert resp.status_code == 200
        body = resp.json()
        assert body["reference_defaulted"] is True
        # 4x the 54-day analysis clamps to the loaded data start, then trims
        # to whole weeks (the example tree declares seasonality): 42 days.
        assert body["reference_window"] == {"start": "2024-01-05", "end": "2024-02-15"}
        assert body["nodes"]["revenue"]["gap"] is not None

        resp = client.post("/rca/revenue", params={**analysis, "reference_start": "2024-01-01"})
        assert resp.status_code == 422
        assert "both reference_start and reference_end" in resp.json()["detail"]


def test_shapley_endpoint_defaults_reference():
    analysis = {"analysis_start": "2024-02-16", "analysis_end": "2024-04-09"}
    with TestClient(app) as client:
        resp = client.get("/shapley/revenue", params=analysis)
        assert resp.status_code == 200
        body = resp.json()
        assert body["reference_defaulted"] is True
        assert body["reference_window"]["end"] == "2024-02-15"
        assert body["analysis_window"] == {
            "start": "2024-02-16",
            "end": "2024-04-09",
        }


def test_slice_endpoint_defaults_reference(sliced_env):
    params = {
        "dimension": "region",
        "analysis_start": "2024-03-04",
        "analysis_end": "2024-03-10",
    }
    with TestClient(app) as client:
        resp = client.post("/rca/signups/slices", params=params)
        assert resp.status_code == 200
        body = resp.json()
        assert body["reference_defaulted"] is True
        # 7-day analysis -> 28-day floor, ending the day before it.
        assert body["effective_windows"]["reference"]["n_periods"] == 28


def test_meta_reports_earliest_available():
    """Background discovery fills earliest_available; the mock provider
    answers with its epoch for every metric."""
    import time

    with TestClient(app) as client:
        meta = {}
        for _ in range(100):  # discovery is async; poll briefly
            meta = client.get("/meta").json()
            if len(meta.get("earliest_available", {})) == len(meta["metrics"]):
                break
            time.sleep(0.05)
        assert set(meta["earliest_available"]) == set(meta["metrics"])
        assert all(v == "2020-01-01" for v in meta["earliest_available"].values())


# --- bounds and gates (H4/H5/H6, L3) ---


def test_slice_window_outside_loaded_data_is_422_before_any_fetch(sliced_env):
    """`/rca/{name}/slices` fetched `min(starts)..max(ends)` from the provider
    with no check that those dates lie inside the loaded window (H6). A caller
    could ask for 1900..2100 and get a 73,000-day scan, held under the tree's
    lock, whose frame then sat in the slice cache forever — even though the
    request went on to 422 for having no data in it.

    The observable property is that nothing was fetched, so nothing was
    cached."""
    with TestClient(app) as client:
        resp = client.post(
            "/rca/signups/slices",
            params={
                "dimension": "region",
                "reference_start": "1900-01-01",
                "reference_end": "1901-01-01",
                "analysis_start": "2099-01-01",
                "analysis_end": "2100-12-31",
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "outside the loaded data window" in detail
        assert "2024-01-01..2024-04-09" in detail, detail
        assert len(app.state.slice_cache) == 0, "the rejected window was still fetched"


def test_slice_window_check_covers_a_defaulted_reference(sliced_env):
    """The reference window may be defaulted inside `_run_slice`, so the check
    has to look at the span actually about to be fetched."""
    with TestClient(app) as client:
        resp = client.post(
            "/rca/signups/slices",
            params={
                "dimension": "region",
                "analysis_start": "2024-04-01",
                "analysis_end": "2024-05-01",  # past the loaded window's end
            },
        )
        assert resp.status_code == 422
        assert "outside the loaded data window" in resp.json()["detail"]
        assert len(app.state.slice_cache) == 0


def test_slice_and_flow_caches_are_bounded():
    """`slice_cache` and `flow_cache` were plain dicts with no cap, no TTL and
    no eviction anywhere in the package — keyed by caller-chosen windows, so on
    a public deployment they grow until the process dies. C8 bounded `traces`
    and never looked at its two siblings."""
    from breakdown.api.trees import MAX_CACHED_FLOWS, MAX_CACHED_SLICES, TreeState

    tree = TreeState(id="t", path="t.yml")
    for i in range(MAX_CACHED_SLICES + 10):
        tree.slice_cache[("signups", "region", "day", "2024-01-01", i)] = i
    assert len(tree.slice_cache) == MAX_CACHED_SLICES
    assert list(tree.slice_cache.values()) == list(range(10, MAX_CACHED_SLICES + 10))

    for i in range(MAX_CACHED_FLOWS + 10):
        tree.flow_cache[("signups", "region", i)] = i
    assert len(tree.flow_cache) == MAX_CACHED_FLOWS
    assert list(tree.flow_cache.values())[0] == 10, "eviction drops the oldest key first"


class _SizedFit:
    """A stand-in for a `FitResult` whose trace reports a known size."""

    def __init__(self, megabytes):
        nbytes = int(megabytes * 1024 * 1024)
        group = type("Group", (), {"nbytes": nbytes})()
        self.trace = type("Trace", (), {"posterior": group, "groups": lambda self: ["posterior"]})()


def test_trace_store_evicts_on_a_byte_budget():
    """The cap was an entry count, but an entry's size scales with the loaded
    window: one ADVI fit of the demo tree over 830 days measures 13.4 MB, so
    256 of them is ~3.4 GB against fly.toml's 2 GB (H4). Tuning the count down
    just moves the cliff to a wider window."""
    from breakdown.api.trees import TraceStore

    budget = 40 * 1024 * 1024
    store = TraceStore(max_bytes=budget)
    traces = store.view("tree")
    for i in range(6):
        traces[(f"m{i}", None)] = _SizedFit(13.4)

    assert store.total_bytes <= budget
    assert len(traces) == 2, "13.4 MB entries under a 40 MB budget"
    assert ("m0", None) not in traces, "eviction should drop the oldest key first"
    assert ("m5", None) in traces


def test_trace_store_keeps_the_newest_fit_even_when_it_alone_busts_the_budget():
    """Degrade to a cache of one, never a cache of none: the caller is holding
    that fit and about to serve from it."""
    from breakdown.api.trees import TraceStore

    store = TraceStore(max_bytes=1024)
    traces = store.view("tree")
    traces[("m", None)] = _SizedFit(13.4)
    assert len(traces) == 1


def test_trace_store_entry_count_is_still_a_backstop():
    """Unmeasurable (or free) entries are bounded by the count as before."""
    from breakdown.api.trees import TraceStore

    store = TraceStore(max_entries=3, max_bytes=1024**3)
    traces = store.view("tree")
    for i in range(10):
        traces[(f"m{i}", None)] = object()
    assert len(traces) == 3
    assert store.total_bytes == 0


def test_trace_store_byte_budget_is_env_overridable(monkeypatch):
    from breakdown.api.trees import MAX_CACHED_TRACE_BYTES, TraceStore

    monkeypatch.setenv("BREAKDOWN_MAX_TRACE_BYTES", str(7 * 1024 * 1024))
    assert TraceStore().max_bytes == 7 * 1024 * 1024
    monkeypatch.setenv("BREAKDOWN_MAX_TRACE_BYTES", "not-a-number")
    assert TraceStore().max_bytes == MAX_CACHED_TRACE_BYTES


def test_trace_store_refit_does_not_double_count_bytes():
    """A refit re-inserts at the same key; the old entry's bytes go with it."""
    from breakdown.api.trees import TraceStore

    store = TraceStore()
    traces = store.view("tree")
    traces[("m", None)] = _SizedFit(10)
    traces[("m", None)] = _SizedFit(10)
    assert store.total_bytes == 10 * 1024 * 1024
    del traces[("m", None)]
    assert store.total_bytes == 0
    assert len(traces) == 0


def test_metric_summary_is_computed_once_per_fit(monkeypatch):
    """`az.summary` is the one heavy engine call on `GET /metrics/{name}`: 1.1s
    on an 830-day ADVI trace, scaling with `draws`, and nothing memoized it —
    so `clearRCA`'s re-fetch of every fitted metric paid it N times (H5)."""
    import breakdown.api.main as main

    calls = []
    real = main.summarize_trace

    def counting(trace):
        calls.append(trace)
        return real(trace)

    monkeypatch.setattr(main, "summarize_trace", counting)
    with TestClient(app) as client:
        assert client.post("/analyze/daily_sessions?inference_method=advi&draws=100").status_code
        first = client.get("/metrics/daily_sessions").json()["summary"]
        second = client.get("/metrics/daily_sessions").json()["summary"]

    assert first is not None and first == second
    assert len(calls) == 1, "the summary was recomputed on every GET"


def test_non_ascii_bearer_token_is_401_not_500(monkeypatch):
    """`hmac.compare_digest` raises TypeError comparing strs with non-ASCII, so
    a header of `Bearer sécret` was a 500 from inside the middleware — an
    error-page-vs-401 oracle anyone could trip (L3)."""
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with TestClient(app) as client:
        # Sent as bytes because httpx refuses to encode a non-ASCII str header
        # — which is exactly the point: only a hand-rolled client sends these,
        # and it must get a 401 like everyone else.
        for header in ("Bearer sécret", "Bearer s3crét", "Bearer ünicode"):
            resp = client.post("/mcp/", json={}, headers={"Authorization": header.encode("utf-8")})
            assert resp.status_code == 401, header
            assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_non_ascii_configured_token_still_authenticates(monkeypatch):
    """The bytes have to round-trip, not merely fail safely: a deployment whose
    secret happens to be non-ASCII must still be able to use it."""
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "sécret")
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Authorization": "Bearer sécret".encode("utf-8"),
                "Accept": "application/json, text/event-stream",
            },
        )
        # Past the gate is all this asserts — the MCP transport's own host
        # check is what tests/test_mcp.py covers.
        assert resp.status_code != 401, resp.text

        wrong = client.post("/mcp/", json={}, headers={"Authorization": "Bearer s3cret"})
        assert wrong.status_code == 401


# --- /dag does not publish bindings to unauthenticated callers ---

BOUND_TREE = """
provider:
  type: mock

metrics:
  - name: signups
    source: my_project.metrics.signups
    sql: SELECT ordered_at AS date, count(*) AS signups FROM analytics.fct_signups GROUP BY 1
  - name: activations
    source: my_project.metrics.activations
    parents: [signups]
    bind:
      relation: analytics.fct_activations
      grain_key: activation_id
      time_column: activated_at
      agg: sum
      measure: amount
"""


@pytest.fixture
def bound_env(tmp_path, monkeypatch):
    tree_file = tmp_path / "bound_tree.yml"
    tree_file.write_text(BOUND_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))
    monkeypatch.delenv("BREAKDOWN_API_TOKEN", raising=False)


def _dag_nodes(client, **kwargs):
    resp = client.get("/dag", **kwargs)
    assert resp.status_code == 200
    return {name: definition for name, definition in resp.json()["nodes"]}


def test_dag_publishes_bindings_when_no_token_is_configured(bound_env):
    """The laptop default is unchanged: no token, no redaction."""
    with TestClient(app) as client:
        nodes = _dag_nodes(client)
        assert nodes["signups"]["sql"].startswith("SELECT")
        assert nodes["activations"]["bind"]["relation"] == "analytics.fct_activations"


def test_dag_redacts_sql_and_bind_from_unauthenticated_callers(bound_env, monkeypatch):
    """`/dag` served the full definition — fully-qualified table names and
    WHERE-clause business logic — to anyone, on a deployment that had bothered
    to configure a token."""
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with TestClient(app) as client:
        nodes = _dag_nodes(client)
        assert nodes["signups"]["sql"] is None
        assert nodes["activations"]["bind"] is None
        # null, not a removed key: `def.sql` must not become a KeyError.
        assert "sql" in nodes["signups"] and "bind" in nodes["activations"]
        # Everything the UI draws with is untouched.
        assert nodes["activations"]["parents"] == ["signups"]
        assert nodes["signups"]["source"] == "my_project.metrics.signups"

        authed = _dag_nodes(client, headers={"Authorization": "Bearer s3cret"})
        assert authed["signups"]["sql"].startswith("SELECT")
        assert authed["activations"]["bind"]["relation"] == "analytics.fct_activations"

        # The per-tree mount is the same handler, so it redacts too.
        prefixed = client.get("/trees/bound_tree/dag").json()["nodes"]
        assert dict(prefixed)["signups"]["sql"] is None


# --- BREAKDOWN_REQUIRE_AUTH: the whole data surface behind the same token ---

# Every JSON data route, in both mounts. The router is included twice, so the
# risk this list guards is an alias gated differently from the route it aliases.
_DATA_ROUTES = [
    ("get", "/meta"),
    ("get", "/dag"),
    ("get", "/series"),
    ("get", "/metrics/revenue"),
    ("get", "/metrics/revenue/query"),
    ("post", "/analyze/revenue"),
    ("get", "/shapley/revenue?analysis_start=2024-03-01&analysis_end=2024-04-09"),
    ("post", "/rca/revenue?analysis_start=2024-03-01&analysis_end=2024-04-09"),
    (
        "post",
        "/rca/revenue/slices?dimension=region&analysis_start=2024-03-01&analysis_end=2024-04-09",
    ),
    ("post", "/simulate"),
    ("get", "/progress/whatever"),
    ("get", "/trees"),
    ("post", "/trees/jaffle_shop_tree/load"),
    ("get", "/trees/jaffle_shop_tree/meta"),
    ("get", "/trees/jaffle_shop_tree/dag"),
    ("get", "/trees/jaffle_shop_tree/metrics/revenue"),
]


@pytest.fixture
def require_auth_env(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    monkeypatch.setenv("BREAKDOWN_REQUIRE_AUTH", "1")


def test_require_auth_gates_every_data_route(require_auth_env):
    """The token alone gates /mcp only. BREAKDOWN_REQUIRE_AUTH=1 extends the
    same check to the whole JSON surface, both mounts."""
    with TestClient(app) as client:
        for method, path in _DATA_ROUTES:
            resp = getattr(client, method)(path)
            assert resp.status_code == 401, f"{method.upper()} {path} was open"
            assert resp.headers["WWW-Authenticate"] == "Bearer"

        headers = {"Authorization": "Bearer s3cret"}
        assert client.get("/meta", headers=headers).status_code == 200
        assert client.get("/trees", headers=headers).status_code == 200
        assert client.get("/trees/jaffle_shop_tree/dag", headers=headers).status_code == 200


def test_require_auth_leaves_health_ui_and_root_open(require_auth_env):
    """/health is what orchestrators and compose.yaml's healthcheck call with no
    credentials; /ui is a JS bundle, not data; / carries nothing."""
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/app.js").status_code == 200
        assert client.get("/").status_code == 200


def test_require_auth_is_off_by_default():
    """Existing deployments must not break: the token alone still gates /mcp
    and nothing else."""
    with TestClient(app) as client:
        assert client.get("/meta").status_code == 200
        assert client.get("/dag").status_code == 200


def test_token_without_require_auth_leaves_data_routes_open(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    monkeypatch.delenv("BREAKDOWN_REQUIRE_AUTH", raising=False)
    with TestClient(app) as client:
        assert client.get("/meta").status_code == 200
        assert client.get("/series").status_code == 200
        assert client.post("/mcp/", json={}).status_code == 401


def test_require_auth_without_a_token_refuses_to_serve(monkeypatch):
    """Otherwise every request is checked against an empty secret and passes —
    the one auth configuration that fails open."""
    monkeypatch.setenv("BREAKDOWN_REQUIRE_AUTH", "1")
    monkeypatch.delenv("BREAKDOWN_API_TOKEN", raising=False)
    with TestClient(app) as client:
        for method, path in _DATA_ROUTES:
            resp = getattr(client, method)(path)
            assert resp.status_code == 503, f"{method.upper()} {path} served"
            assert "BREAKDOWN_API_TOKEN" in resp.json()["detail"]
            assert "BREAKDOWN_REQUIRE_AUTH" in resp.json()["detail"]

        # /health still answers — degraded, naming the misconfiguration, so an
        # operator sees the reason instead of unexplained 503s.
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert "BREAKDOWN_REQUIRE_AUTH" in health["error"]


def test_require_auth_accepts_any_value_but_an_explicit_off(monkeypatch):
    """A typo must close the door, not open it."""
    from breakdown.api.main import _require_auth

    for value in ("1", "true", "TRUE", "yes", "on", "ture"):
        monkeypatch.setenv("BREAKDOWN_REQUIRE_AUTH", value)
        assert _require_auth() is True, value
    for value in ("", "0", "false", "no", "off", "Off"):
        monkeypatch.setenv("BREAKDOWN_REQUIRE_AUTH", value)
        assert _require_auth() is False, value
    monkeypatch.delenv("BREAKDOWN_REQUIRE_AUTH")
    assert _require_auth() is False


def test_gate_prefixes_match_on_path_segments():
    """`startswith("/mcp")` also matches `/mcphony`, and an open-list built the
    same way would hand out a future `/uiconfig`."""
    from breakdown.api.main import _open_path, _under

    assert _under("/mcp", "/mcp") and _under("/mcp/", "/mcp")
    assert not _under("/mcphony", "/mcp")
    assert _open_path("/ui") and _open_path("/ui/app.js")
    assert not _open_path("/uiconfig")
    assert _open_path("/health") and not _open_path("/healthz")
    assert _open_path("/") and not _open_path("/meta")

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
        assert {"target", "reference_window", "analysis_window", "nodes", "ranked_causes"} <= set(body)
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
        "assumptions": [{
            "source": "discount_pct",
            "target": "average_order_value",
            "effect": {"kind": "relative", "low": -0.12, "high": -0.08},
            "note": "10% blanket discount",
        }],
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
        resp = client.post("/simulate", json={**base, "interventions": [
            {"metric": "nope", "mode": "set", "value": 1.0}]})
        assert resp.status_code == 422
        # empty scenario -> 422
        assert client.post("/simulate", json=base).status_code == 422
        # inverted effect range -> 422 (pydantic)
        resp = client.post("/simulate", json={**base, "assumptions": [{
            "source": "x", "target": "revenue",
            "effect": {"kind": "absolute", "low": 2.0, "high": 1.0}}]})
        assert resp.status_code == 422
        # inverted baseline window -> 422
        resp = client.post("/simulate", json={
            "baseline_start": "2024-04-09", "baseline_end": "2024-03-01",
            "interventions": [{"metric": "revenue", "mode": "set", "value": 1.0}]})
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
        assert client.post("/rca/revenue?reference_start=2024-01-01&reference_end=2024-01-07&analysis_start=2024-01-08&analysis_end=2024-01-14").status_code == 503

        assert client.get("/ui/").status_code == 200
        assert client.get("/").status_code == 200

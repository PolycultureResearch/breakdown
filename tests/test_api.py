import asyncio
import os
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
        data = app.state.data
        metric_names = [m.name for m in app.state.parser.config.metrics]
        assert list(data.columns) == ["date"] + metric_names
        assert len(data) == 100  # default window 2024-01-01..2024-04-09
        assert not data.isna().any().any()

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
        assert list(app.state.data.columns) == ["date", "signups", "activations"]
        assert len(app.state.data) == 30

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
        assert body["fitted"] == []


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

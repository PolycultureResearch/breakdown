import os

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from breakdown.api.main import app

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

"""Integration tests for the MCP server mounted at /mcp.

These speak the streamable-HTTP wire protocol (stateless, JSON responses)
through the FastAPI TestClient — an external client running tools over MCP
is exactly the roadmap's Horizon-2 exit criterion. base_url matters: the
transport's DNS-rebinding protection only admits localhost hosts.
"""

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from breakdown.api.main import app

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

WINDOWS = {
    "reference_start": "2024-01-01",
    "reference_end": "2024-02-15",
    "analysis_start": "2024-02-16",
    "analysis_end": "2024-04-09",
}


def _client():
    return TestClient(app, base_url="http://127.0.0.1:9090")


def _rpc(client, method, params, id=1):
    resp = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": id, "method": method, "params": params},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _call_tool(client, name, arguments, id=2):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments}, id=id)


def test_mcp_open_by_default():
    """Unset token keeps the loopback workflow friction-free."""
    with _client() as client:
        assert client.post("/mcp/", json={}, headers=HEADERS).status_code != 401


def test_mcp_requires_bearer_token_when_set(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}):
            resp = client.post("/mcp/", json={}, headers={**HEADERS, **headers})
            assert resp.status_code == 401, headers
            assert resp.headers["WWW-Authenticate"] == "Bearer"

        # The right token gets through to the transport.
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**HEADERS, "Authorization": "Bearer s3cret"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["tools"]


def test_token_gate_leaves_ui_and_health_open(monkeypatch):
    """The token guards the machine-facing surface, not the demo itself —
    a public instance still has to serve its UI to anyone with the link."""
    monkeypatch.setenv("BREAKDOWN_API_TOKEN", "s3cret")
    with _client() as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ui/").status_code == 200
        assert client.get("/meta").status_code == 200


def test_list_tools():
    with _client() as client:
        result = _rpc(client, "tools/list", {})
        tools = {t["name"]: t for t in result["tools"]}
        assert set(tools) == {"get_tree", "explain_metric", "run_rca", "slice_metric", "run_whatif"}
        for tool in tools.values():
            assert tool["description"].strip()
        # window-selection guidance is part of the run_rca contract: it is
        # how a client turns "last week" into windows
        assert "reference window" in tools["run_rca"]["description"]


def test_get_tree():
    with _client() as client:
        res = _call_tool(client, "get_tree", {})
        assert res["isError"] is False
        tree = res["structuredContent"]["result"]
        names = {m["name"] for m in tree["metrics"]}
        assert names == {m.name for m in app.state.parser.config.metrics}
        assert ["order_count", "revenue"] in tree["edges"]
        # definitions are trimmed for token economy: modeling internals stay out
        for m in tree["metrics"]:
            assert "priors" not in m and "sql" not in m and "seasonality" not in m


def test_explain_metric():
    with _client() as client:
        res = _call_tool(client, "explain_metric", {"name": "revenue"})
        out = res["structuredContent"]["result"]
        assert out["definition"]["formula"] == "order_count * average_order_value"
        assert out["definition"]["parents"] == ["order_count", "average_order_value"]
        assert out["series_summary"]["n_periods"] == 100
        assert len(out["series_summary"]["recent"]) == 8
        assert out["fit"] == {"fitted": False}
        assert out["report_url"].endswith("/ui/#metric=revenue")

        # unknown metric -> tool error naming the valid metrics
        res = _call_tool(client, "explain_metric", {"name": "nope"}, id=3)
        assert res["isError"] is True
        assert "Known metrics" in res["content"][0]["text"]


def test_run_rca():
    """RCA over MCP: contract, determinism, and the shared trace cache."""
    with _client() as client:
        res = _call_tool(client, "run_rca", {"target": "revenue", **WINDOWS})
        assert res["isError"] is False
        out = res["structuredContent"]["result"]

        assert out["target"] == "revenue"
        assert out["nodes"]["revenue"]["attribution_method"] == "shapley"
        assert out["nodes"]["order_count"]["attribution_method"] == "posterior"
        assert len(out["ranked_causes"]) > 0
        assert "how_to_read" in out
        for param in WINDOWS:
            assert f"{param}={WINDOWS[param]}" in out["report_url"]

        # compaction: no decompositions or window detail in the payload
        contribs = out["nodes"]["revenue"]["contributions"]
        assert contribs and all("decomposition" not in c for c in contribs)
        assert "effective_windows" not in out["nodes"]["revenue"]

        # the on-demand fit landed in the shared cache (visible to the UI too)
        assert ("order_count", WINDOWS["analysis_start"]) in app.state.traces

        # seeded engine: identical call, identical answer
        res2 = _call_tool(client, "run_rca", {"target": "revenue", **WINDOWS}, id=4)
        assert res2["structuredContent"] == res["structuredContent"]

        # unknown target -> tool error
        res = _call_tool(client, "run_rca", {"target": "nope", **WINDOWS}, id=5)
        assert res["isError"] is True


def test_run_whatif():
    scenario = {
        "baseline_start": "2024-03-01",
        "baseline_end": "2024-04-09",
        "interventions": [{"metric": "daily_sessions", "mode": "pct", "value": 0.15}],
    }
    with _client() as client:
        res = _call_tool(client, "run_whatif", scenario)
        assert res["isError"] is False
        out = res["structuredContent"]["result"]

        assert out["nodes"]["daily_sessions"]["status"] == "intervened"
        revenue = out["nodes"]["revenue"]
        assert revenue["status"] == "affected"
        assert (
            revenue["delta"]["ci_95"][0]
            <= revenue["delta"]["estimate"]
            <= revenue["delta"]["ci_95"][1]
        )
        assert len(out["caveats"]) == 3
        assert "how_to_read" in out
        assert "#whatif=" in out["report_url"]
        # extrapolation stats collapse to a flag
        assert isinstance(revenue["extrapolation"], bool)

        # engine validation errors surface as tool errors the model can fix
        res = _call_tool(
            client,
            "run_whatif",
            {"baseline_start": "2024-03-01", "baseline_end": "2024-04-09"},
            id=6,
        )
        assert res["isError"] is True
        assert "at least one intervention" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# Cold start mode over the wire

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
"""


@pytest.fixture
def cold_start_env(tmp_path, monkeypatch):
    tree_file = tmp_path / "cold_start_tree.yml"
    tree_file.write_text(COLD_START_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))


def test_cold_start_get_tree_and_whatif(cold_start_env):
    with _client() as client:
        res = _call_tool(client, "get_tree", {})
        assert res["isError"] is False
        tree = res["structuredContent"]["result"]
        assert tree["mode"] == "cold_start"
        assert tree["date_start"] is None
        by_name = {m["name"]: m for m in tree["metrics"]}
        assert by_name["sessions"]["baseline"] == {"low": 800.0, "high": 1600.0}

        res = _call_tool(
            client,
            "run_whatif",
            {"interventions": [{"metric": "sessions", "mode": "pct", "value": 0.10}]},
            id=3,
        )
        assert res["isError"] is False
        out = res["structuredContent"]["result"]
        assert out["mode"] == "cold_start"
        assert out["nodes"]["sessions"]["baseline_ci_95"] is not None
        # the how_to_read gains the cold-start block so the model narrates
        # beliefs, not evidence
        assert "COLD START" in out["how_to_read"]


def test_cold_start_rca_refused_with_pointer(cold_start_env):
    with _client() as client:
        res = _call_tool(client, "run_rca", {"target": "signups", **WINDOWS})
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert "no data provider" in text
        assert "run_whatif" in text


SLICED_TREE = """
provider:
  type: mock

metrics:
  - name: signups
    source: my_project.metrics.signups
    dimensions:
      region: customer__region
  - name: activations
    source: my_project.metrics.activations
    parents: [signups]
"""


@pytest.fixture
def sliced_env(tmp_path, monkeypatch):
    tree_file = tmp_path / "sliced_tree.yml"
    tree_file.write_text(SLICED_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")


def test_slice_metric(sliced_env):
    """Traverse-then-slice over MCP: get_tree advertises dimensions, and
    slice_metric localizes with the excess/reconciliation contract."""
    with _client() as client:
        res = _call_tool(client, "get_tree", {})
        by_name = {m["name"]: m for m in res["structuredContent"]["result"]["metrics"]}
        assert by_name["signups"]["dimensions"] == ["region"]
        assert "dimensions" not in by_name["activations"]

        res = _call_tool(
            client,
            "slice_metric",
            {"name": "signups", "dimension": "region", **WINDOWS},
            id=3,
        )
        assert res["isError"] is False
        out = res["structuredContent"]["result"]
        assert out["dimension"] == "region"
        assert out["attribution_method"] == "slice_sum"
        assert out["reconciliation"]["status"] == "ok"
        assert out["slices"]
        assert "excess" in out["slices"][0]
        assert "how_to_read" in out
        assert "localize" in out["how_to_read"]

        # deterministic: identical call, identical answer
        res2 = _call_tool(
            client,
            "slice_metric",
            {"name": "signups", "dimension": "region", **WINDOWS},
            id=4,
        )
        assert res2["structuredContent"] == res["structuredContent"]

        # undeclared dimension -> tool error naming what is declared
        res = _call_tool(
            client,
            "slice_metric",
            {"name": "activations", "dimension": "region", **WINDOWS},
            id=5,
        )
        assert res["isError"] is True
        assert "declares no dimension" in res["content"][0]["text"]

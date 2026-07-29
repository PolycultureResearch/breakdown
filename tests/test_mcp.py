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


def test_list_tools():
    with _client() as client:
        result = _rpc(client, "tools/list", {})
        tools = {t["name"]: t for t in result["tools"]}
        assert set(tools) == {"get_tree", "explain_metric", "run_rca", "run_whatif"}
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

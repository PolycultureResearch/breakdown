"""Several metric trees in one process (roadmap 2.16).

Trees are peers — a wide revenue tree, a team's, a feature's, one standing
behind a target — of any lifetime, with a goal or without. So the properties
that matter here are: a tree with no `tree:` block at all is as first-class as
an annotated one, one bad file doesn't take the others down, nobody pays for a
tree they didn't open, and the index says when it doesn't know.
"""

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from breakdown.api.main import app  # noqa: E402

BUSINESS_TREE = """
tree:
  title: "The business"
  description: "How the company is wired"

provider:
  type: mock

metrics:
  - name: sessions
    source: w.sessions
  - name: orders
    source: w.orders
    parents: [sessions]
"""

# A team's tree: no `tree:` block at all, no goal, no period. It is a peer of
# the others and every part of the index has to treat it as one.
BARE_TREE = """
provider:
  type: mock

metrics:
  - name: paid_clicks
    source: w.paid_clicks
  - name: paid_signups
    source: w.paid_signups
    parents: [paid_clicks]
"""

GOAL_TREE = """
tree:
  title: "Q3 Pro member growth"
  description: "200 net-new paying Pro members by Sep 30"
  owner: "growth@acme.com"
  period: "2026-Q3"
  goal:
    metric: pro_members_net_new
    target: 200
    deadline: "2026-09-30"

provider:
  type: mock

metrics:
  - name: pro_trials
    source: w.pro_trials
  - name: pro_members_net_new
    source: w.pro_members_net_new
    parents: [pro_trials]
"""

BROKEN_TREE = """
provider:
  type: mock

metrics:
  - name: orphan
    source: w.orphan
    parents: [does_not_exist]
"""


@pytest.fixture
def tree_dir(tmp_path, monkeypatch):
    d = tmp_path / "breakdown"
    d.mkdir()
    (d / "business.yml").write_text(BUSINESS_TREE)
    (d / "marketing.yml").write_text(BARE_TREE)
    (d / "q3_pro_growth.yml").write_text(GOAL_TREE)
    (d / "broken.yml").write_text(BROKEN_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(d))
    return d


def test_directory_discovers_one_tree_per_file(tree_dir):
    with TestClient(app) as client:
        body = client.get("/trees").json()
        assert {t["id"] for t in body["trees"]} == {
            "business",
            "marketing",
            "q3_pro_growth",
            "broken",
        }
        # id is the filename stem; title falls back to it when undeclared
        card = next(t for t in body["trees"] if t["id"] == "q3_pro_growth")
        assert card["title"] == "Q3 Pro member growth"
        assert card["owner"] == "growth@acme.com"
        assert card["period"] == "2026-Q3"
        assert card["goal"]["target"] == 200
        assert card["metric_count"] == 2


def test_a_tree_with_no_tree_block_is_a_peer(tree_dir):
    """Most trees declare no goal and many declare nothing at all — a team's
    tree, the wide revenue tree. None of that is a deficiency, so the card
    carries no goal furniture and the tree opens like any other."""
    with TestClient(app) as client:
        card = next(t for t in client.get("/trees").json()["trees"] if t["id"] == "marketing")
        assert card["title"] == "marketing"  # falls back to the filename stem
        assert card["goal"] is None and card["progress"] is None
        assert card["period"] is None and card["owner"] is None
        assert card["metric_count"] == 2
        assert client.get("/trees/marketing/meta").status_code == 200


def test_a_goal_needs_neither_a_deadline_nor_a_period(tmp_path, monkeypatch):
    """breakdown takes no position on how long a tree lives: a target with no
    date is an ordinary thing to be held to."""
    d = tmp_path / "trees"
    d.mkdir()
    (d / "business.yml").write_text(BUSINESS_TREE)
    (d / "platform.yml").write_text("""
tree:
  title: "Platform reliability"
  goal:
    metric: orders
    target: 5000

provider:
  type: mock

metrics:
  - name: sessions
    source: w.sessions
  - name: orders
    source: w.orders
    parents: [sessions]
""")
    monkeypatch.setenv("BREAKDOWN_TREE", str(d))
    with TestClient(app) as client:
        card = client.post("/trees/platform/load").json()
        assert card["goal"] == {
            "metric": "orders",
            "target": 5000,
            "direction": "up",
            "deadline": None,
        }
        assert card["period"] is None
        assert card["progress"]["target"] == 5000


def test_default_is_the_alphabetically_first_tree(tree_dir):
    with TestClient(app) as client:
        assert client.get("/trees").json()["default"] == "broken"


def test_default_tree_env_selects_it(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        assert client.get("/trees").json()["default"] == "business"
        # the unprefixed routes are that tree
        assert client.get("/meta").json()["metrics"] == ["sessions", "orders"]


def test_unknown_default_tree_is_a_startup_error(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "nope")
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert "nope" in health["error"]


def test_one_broken_tree_does_not_take_down_the_others(tree_dir, monkeypatch):
    """A malformed YAML in the directory is a broken card on the index, not a
    dead process — the same degraded-startup discipline, scoped to one tree."""
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        cards = {t["id"]: t for t in client.get("/trees").json()["trees"]}
        assert cards["broken"]["state"] == "error"
        assert "does_not_exist" in cards["broken"]["load_error"]
        assert cards["broken"]["metric_count"] == 0
        # ...while its neighbours serve normally
        assert client.get("/trees/business/meta").status_code == 200
        assert client.get("/trees/q3_pro_growth/dag").status_code == 200
        # and the broken one answers 503 with its own reason
        resp = client.get("/trees/broken/meta")
        assert resp.status_code == 503
        assert "does_not_exist" in resp.json()["detail"]


def test_a_directory_of_trees_fetches_nothing_at_boot(tree_dir):
    """§5.1: boot parses every tree and loads none. Eight trees is eight sets
    of warehouse round-trips, and paying for the seven nobody opened is the
    difference between starting in three seconds and three minutes."""
    with TestClient(app) as client:
        states = {t["id"]: t["state"] for t in client.get("/trees").json()["trees"]}
        assert states["business"] == "not_loaded"
        assert states["q3_pro_growth"] == "not_loaded"
        # ...and /trees itself never triggers one
        assert app.state.trees["business"].data is None


def test_index_says_not_loaded_rather_than_zero(tree_dir):
    """§2.3: `progress: null` with `state: not_loaded` is *we haven't looked*.
    A blank that reads as zero would be a wrong number, not a missing one."""
    with TestClient(app) as client:
        card = next(t for t in client.get("/trees").json()["trees"] if t["id"] == "q3_pro_growth")
        assert card["state"] == "not_loaded"
        assert card["progress"] is None
        assert card["goal"]["target"] == 200  # the declared goal still shows


def test_explicit_load_then_progress(tree_dir):
    with TestClient(app) as client:
        card = client.post("/trees/q3_pro_growth/load").json()
        assert card["state"] == "loaded"
        progress = card["progress"]
        assert progress["target"] == 200
        assert isinstance(progress["current"], float)
        # `as_of` is the tree's own data edge, so the index agrees with the
        # number the tree itself shows
        meta = client.get("/trees/q3_pro_growth/meta").json()
        assert progress["as_of"] <= meta["date_end"]
        # a second load is a no-op, and other trees are untouched
        assert client.post("/trees/q3_pro_growth/load").json()["state"] == "loaded"
        assert app.state.trees["business"].data is None


def test_a_data_request_loads_the_tree_implicitly(tree_dir):
    with TestClient(app) as client:
        assert app.state.trees["business"].data is None
        assert client.get("/trees/business/series").status_code == 200
        assert app.state.trees["business"].data is not None


def test_a_single_file_still_loads_eagerly(tmp_path, monkeypatch):
    """The single-tree case must boot exactly as it did: the port is up, so is
    the data. Lazy loading buys nothing when there is one tree."""
    tree_file = tmp_path / "business.yml"
    tree_file.write_text(BUSINESS_TREE)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree_file))
    with TestClient(app) as client:
        assert app.state.data is not None
        body = client.get("/trees").json()
        assert body["default"] == "business"
        assert body["trees"][0]["state"] == "loaded"


def test_eager_flag_loads_the_default_tree_from_a_directory(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    monkeypatch.setenv("BREAKDOWN_EAGER", "1")
    with TestClient(app) as client:
        states = {t["id"]: t["state"] for t in client.get("/trees").json()["trees"]}
        assert states["business"] == "loaded"
        assert states["q3_pro_growth"] == "not_loaded"


def test_missing_tree_path_serves_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_TREE", str(tmp_path / "nowhere.yml"))
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "degraded"
        # a 503 naming the reason, not a 404 that reads as "wrong tree"
        resp = client.get("/meta")
        assert resp.status_code == 503
        assert "without a metric tree" in resp.json()["detail"]


def test_empty_directory_serves_degraded(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("BREAKDOWN_TREE", str(empty))
    with TestClient(app) as client:
        assert "No metric trees found" in client.get("/health").json()["error"]


def test_unknown_tree_id_is_404(tree_dir):
    with TestClient(app) as client:
        resp = client.get("/trees/not_a_tree/meta")
        assert resp.status_code == 404
        assert "business" in resp.json()["detail"]


# --- tree-scoped routes and their aliases ---------------------------------


def test_scoped_and_unprefixed_routes_agree_on_the_default_tree(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        # `earliest_available` fills in from a background task, so it can
        # legitimately differ between two consecutive requests.
        bare = client.get("/meta").json()
        scoped = client.get("/trees/business/meta").json()
        for body in (bare, scoped):
            body.pop("earliest_available")
        assert bare == scoped
        assert client.get("/dag").json() == client.get("/trees/business/dag").json()


def test_scoped_routes_address_their_own_tree(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        assert client.get("/trees/q3_pro_growth/meta").json()["metrics"] == [
            "pro_trials",
            "pro_members_net_new",
        ]
        # a metric of one tree is not a metric of the other
        assert client.get("/trees/business/metrics/pro_trials").status_code == 404
        assert client.get("/trees/q3_pro_growth/metrics/pro_trials").status_code == 200


def test_meta_names_its_tree(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        assert client.get("/trees/q3_pro_growth/meta").json()["tree"] == "q3_pro_growth"
        assert client.get("/meta").json()["tree"] == "business"


def test_trees_have_independent_caches_and_locks(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app) as client:
        client.post("/trees/business/load")
        client.post("/trees/q3_pro_growth/load")
        business = app.state.trees["business"]
        goal = app.state.trees["q3_pro_growth"]
        assert business.lock is not goal.lock
        assert business.slice_cache is not goal.slice_cache
        # one shared, capped store underneath, but disjoint views
        business.traces[("sessions", None)] = object()
        assert ("sessions", None) in business.traces
        assert ("sessions", None) not in goal.traces
        assert len(app.state.trace_store._entries) == 1


def test_the_trace_cap_is_global_not_per_tree(tree_dir):
    """256 per tree would be 256xN InferenceData objects, each holding every
    posterior draw."""
    from breakdown.api.trees import MAX_CACHED_TRACES

    with TestClient(app):
        a = app.state.trees["business"].traces
        b = app.state.trees["q3_pro_growth"].traces
        for i in range(MAX_CACHED_TRACES):
            a[(f"m{i}", None)] = object()
        for i in range(10):
            b[(f"n{i}", None)] = object()
        assert len(app.state.trace_store._entries) == MAX_CACHED_TRACES
        assert len(b) == 10  # the newcomers survive; the oldest entries went
        assert ("m0", None) not in a


# --- MCP ------------------------------------------------------------------

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _call_tool(client, name, arguments, id=2):
    resp = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_mcp_list_trees_then_scope_a_tool_to_one(tree_dir, monkeypatch):
    """An analyst asking about one part of the business has to be able to
    *find* the tree that models it before analysing it."""
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app, base_url="http://127.0.0.1:9090") as client:
        res = _call_tool(client, "list_trees", {})
        assert res["isError"] is False
        listing = res["structuredContent"]["result"]
        assert listing["default"] == "business"
        goal = next(t for t in listing["trees"] if t["id"] == "q3_pro_growth")
        assert goal["goal"]["metric"] == "pro_members_net_new"
        assert goal["state"] == "not_loaded" and goal["progress"] is None

        # ...and naming it scopes the tool, loading it on the way
        out = _call_tool(client, "get_tree", {"tree": "q3_pro_growth"})
        tree = out["structuredContent"]["result"]
        assert tree["tree"] == "q3_pro_growth"
        assert {m["name"] for m in tree["metrics"]} == {
            "pro_trials",
            "pro_members_net_new",
        }
        # omitting `tree` still means the default tree
        default = _call_tool(client, "get_tree", {})["structuredContent"]["result"]
        assert default["tree"] == "business"


def test_mcp_report_url_carries_the_tree(tree_dir, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_DEFAULT_TREE", "business")
    with TestClient(app, base_url="http://127.0.0.1:9090") as client:
        out = _call_tool(
            client,
            "explain_metric",
            {"name": "pro_trials", "tree": "q3_pro_growth"},
        )["structuredContent"]["result"]
        assert out["report_url"].endswith("#tree=q3_pro_growth&metric=pro_trials")


def test_mcp_unknown_tree_names_the_known_ones(tree_dir):
    with TestClient(app, base_url="http://127.0.0.1:9090") as client:
        res = _call_tool(client, "get_tree", {"tree": "nope"})
        assert res["isError"] is True
        assert "list_trees" in res["content"][0]["text"]

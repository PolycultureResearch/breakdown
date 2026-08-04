"""The White Cube demo instance still tells the stories it was built to tell.

This is the demo's real guarantee. Each planted anomaly in
`fake_companies/configs/white_cube_b2c_app.yaml` has a chain a metric-tree RCA
should recover, and a segment a slice should localize; these assert exactly
that, against the committed parquet snapshots and **no data provider at all**
(the tree's `project_path` is pointed at a directory that does not exist). So
the tests cover both halves of the deployment: the analyses are still correct,
and the shipped image really is hermetic.

Kept in sync with `knowledge/demo_guided_tour.md` (the windows a prospect is
walked through) and `fake_companies/scripts/verify_white_cube_stories.py` (the
same stories checked one layer down, in the warehouse).

Skipped when the snapshots are absent, so a clone that has not run
`demo/Makefile` is not a red suite.
"""

import os

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from breakdown.api.main import app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(REPO, "demo")
TREE = os.path.join(DEMO, "white_cube_tree.yml")
SNAPSHOTS = os.path.join(DEMO, ".breakdown", "snapshots")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(SNAPSHOTS) or not os.listdir(SNAPSHOTS),
    reason="demo snapshots not built — run `make -C demo snapshots`",
)

START, END = "2024-06-01", "2026-07-30"


@pytest.fixture(scope="module")
def client():
    """One instance for the module so fits are cached across stories.

    project_path points nowhere on purpose: if any of this reaches a provider
    instead of a snapshot, it fails loudly rather than quietly working on a
    machine that happens to have the dbt project."""
    prior = {k: os.environ.get(k) for k in
             ("BREAKDOWN_TREE", "BREAKDOWN_START_DATE", "BREAKDOWN_END_DATE",
              "BREAKDOWN_SNAPSHOT_DIR", "BREAKDOWN_REFRESH", "WHITE_CUBE_DBT_PROJECT")}
    os.environ.update(
        BREAKDOWN_TREE=TREE,
        BREAKDOWN_START_DATE=START,
        BREAKDOWN_END_DATE=END,
        BREAKDOWN_SNAPSHOT_DIR=SNAPSHOTS,
        WHITE_CUBE_DBT_PROJECT="/nonexistent/white-cube-has-no-provider",
    )
    os.environ.pop("BREAKDOWN_REFRESH", None)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def rca(client, target, ref, ana):
    r = client.post(
        f"/rca/{target}",
        params={
            "reference_start": ref[0], "reference_end": ref[1],
            "analysis_start": ana[0], "analysis_end": ana[1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def slices(client, metric, dimension, ref, ana):
    r = client.post(
        f"/rca/{metric}/slices",
        params={
            "dimension": dimension,
            "reference_start": ref[0], "reference_end": ref[1],
            "analysis_start": ana[0], "analysis_end": ana[1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_boots_hermetically(client):
    """No provider reachable, yet every metric has data."""
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["metrics"] == 18

    meta = client.get("/meta").json()
    assert meta["date_start"] == START and meta["date_end"] == END
    assert meta["grains"]["net_new_mrr"] == "week"
    assert meta["grains"]["signups"] == "day"
    assert meta["kinds"]["trial_conversion_rate"] == "rate"


def test_mrr_identity_is_exact(client):
    """net_new_mrr = new + expansion - contraction - churned, to float error.

    If this drifts, the semantic layer and the tree have disagreed about what
    the identity is, and every attribution below it is suspect."""
    d = rca(client, "net_new_mrr",
            ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07"))
    node = d["nodes"]["net_new_mrr"]
    assert node["attribution_method"] == "shapley"
    assert abs(node["unexplained"]) < 1e-6 * max(1.0, abs(node["gap"]))
    assert {c["parent"] for c in node["contributions"]} == {
        "new_mrr", "expansion_mrr", "contraction_mrr", "churned_mrr"
    }


def test_story_a_signup_regression_traverses_and_localizes(client):
    """A mobile signup CTA broke: new MRR falls, the tree walks back to signups,
    and the slice names mobile."""
    ref, ana = ("2026-01-05", "2026-02-01"), ("2026-02-09", "2026-03-08")
    d = rca(client, "new_mrr", ref, ana)

    assert d["nodes"]["new_mrr"]["gap"] < 0

    # The volume chain saturates the influence score, so which of its members
    # sorts first is a tie — what must hold is that the whole chain outranks
    # the alternatives the story is meant to rule out (price, and the demand
    # side that did not move).
    ranked = [c["metric"] for c in d["ranked_causes"]]
    chain = {"new_subscriptions", "trial_conversions", "trials_started", "signups"}
    assert ranked[0] in chain
    for alternative in ("new_arpu", "trial_conversion_rate"):
        assert max(ranked.index(m) for m in chain) < ranked.index(alternative)

    # The trial is modelled as a one-week lag, and the shifted parent window
    # must land on the anomaly, not on the calendar window.
    (contribution,) = d["nodes"]["new_subscriptions"]["contributions"]
    assert contribution["parent"] == "trial_conversions"
    assert contribution["lag"] == 1
    assert contribution["parent_windows"]["analysis"]["start"] == "2026-02-02"

    # The CTA broke volume, not conversion quality.
    conv = {c["parent"]: c["share_of_gap"]
            for c in d["nodes"]["trial_conversions"]["contributions"]}
    assert conv["trials_started"] > conv["trial_conversion_rate"]

    s = slices(client, "signups", "device",
               ("2025-12-29", "2026-01-25"), ("2026-02-02", "2026-03-01"))
    assert s["slices"][0]["value"] == "mobile"
    assert s["slices"][0]["baseline_share"] < abs(s["slices"][0]["share_of_gap"])
    assert s["reconciliation"]["status"] == "ok"
    assert not s["slices"][0]["noise_level"]


def test_story_b_churn_spike_is_offset_and_plan_localized(client):
    """Churn spiked on the professional tier: net new MRR falls on the churn
    branch while acquisition holds up and partly masks it."""
    ref, ana = ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07")
    d = rca(client, "net_new_mrr", ref, ana)

    assert d["nodes"]["net_new_mrr"]["gap"] < 0
    assert d["ranked_causes"][0]["metric"] == "churned_mrr"
    assert d["nodes"]["churned_mrr"]["gap"] > 0  # stored positive: more churn

    s = slices(client, "churned_mrr", "plan", ref, ana)
    assert s["slices"][0]["value"] == "professional"
    assert s["slices"][0]["excess"] > 0
    assert s["reconciliation"]["status"] == "ok"


def test_story_c_campaign_splits_volume_from_quality(client):
    """A Brazil campaign: signups rise on both traffic and conversion, and the
    conversion half is concentrated in Brazil."""
    ref, ana = ("2025-02-03", "2025-03-02"), ("2025-03-10", "2025-04-06")
    d = rca(client, "signups", ref, ana)

    node = d["nodes"]["signups"]
    assert node["gap"] > 0
    shares = {c["parent"]: c["share_of_gap"] for c in node["contributions"]}
    # both halves contribute — the identity separates traffic from conversion
    assert shares["sessions"] > 0 and shares["visit_signup_rate"] > 0

    s = slices(client, "signups", "country", ref, ana)
    top = s["slices"][0]
    assert top["value"] == "BR"
    # Brazil is ~9% of traffic but carries a multiple of that share of the gap
    assert top["share_of_gap"] > 3 * top["baseline_share"]


def test_story_d_onboarding_lift_beats_the_trend(client):
    """The revamp has to show up as conversion, not as volume — the business is
    growing underneath, and that growth must not be credited to the change."""
    ref, ana = ("2025-07-07", "2025-08-03"), ("2025-08-11", "2025-09-07")
    d = rca(client, "new_mrr", ref, ana)

    assert d["nodes"]["new_mrr"]["gap"] > 0
    conv = {c["parent"]: c["share_of_gap"]
            for c in d["nodes"]["trial_conversions"]["contributions"]}
    assert conv["trial_conversion_rate"] > conv["trials_started"]


def test_arbitrary_slice_window_is_served_without_a_provider(client):
    """A prospect picking their own dates must not fall through to a provider.

    Sliced snapshots are stored at the full loaded window and trimmed on read,
    so a window nobody warmed at build time still answers."""
    s = slices(client, "churned_mrr", "plan",
               ("2026-02-16", "2026-03-15"), ("2026-05-18", "2026-06-14"))
    assert s["slices"][0]["value"] == "professional"
    assert s["reconciliation"]["status"] == "ok"


def whatif(client, interventions):
    r = client.post(
        "/simulate",
        json={
            "baseline_start": "2026-06-29", "baseline_end": "2026-07-26",
            "interventions": interventions, "assumptions": [], "levers": [],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_what_if_cutting_churn_lifts_net_new_mrr(client):
    """The third demo surface. `pct` takes a fraction: -0.2 is -20%."""
    out = whatif(client, [{"metric": "customer_churn_rate", "mode": "pct", "value": -0.2}])

    churn = out["nodes"]["churned_mrr"]
    net = out["nodes"]["net_new_mrr"]
    assert churn["status"] == "affected" and net["status"] == "affected"

    # Less churn, so less MRR lost and more kept. The identity is linear in the
    # intervened rate, so the lift is ~20% of the churn the business was losing.
    assert churn["simulated"] < churn["baseline"]
    assert net["simulated"] > net["baseline"]
    assert net["delta"]["estimate"] == pytest.approx(
        0.2 * churn["baseline"], rel=0.02
    )
    assert net["prob_direction"] > 0.9


def test_what_if_undefined_history_does_not_break_the_response(client):
    """churn_arpu is undefined in the weeks before anyone had churned. NaN is
    not JSON, so a naive min/max over that history 500s the whole scenario."""
    out = whatif(client, [{"metric": "customer_churn_rate", "mode": "pct", "value": -0.2}])
    extrap = out["nodes"]["churn_arpu"]["extrapolation"]
    assert extrap["hist_min"] is not None
    assert extrap["hist_min"] == extrap["hist_min"]  # not NaN


def test_what_if_flags_a_physically_impossible_scenario(client):
    """Over-cutting churn drives the rate negative; the engine must say so
    rather than quietly reporting the arithmetic."""
    out = whatif(client, [{"metric": "customer_churn_rate", "mode": "pct", "value": -3.0}])
    kinds = {(w["kind"], w["metric"]) for w in out["warnings"]}
    assert ("non_physical", "customer_churn_rate") in kinds

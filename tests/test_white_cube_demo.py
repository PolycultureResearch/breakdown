"""The White Cube demo instance still tells the stories it was built to tell,
*with the numbers `knowledge/demo_guided_tour.md` says it does.*

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

Skipped when the snapshots are absent, so neither a clone that has not run
`demo/Makefile` nor an unpacked sdist (which excludes `demo/` entirely) is a
red suite.

Why the quoted values are pinned here, and how
----------------------------------------------
The tour is read aloud on a client call, and it told its reader that every
number in it was asserted here. That was false: this module pinned *properties*
— gap sign, which node ranks first, `lag == 1`, a share inequality — and no
percentage the tour actually quoted. Several drifted (C3 and C5 both changed
attribution after the tour was written) and nothing went red: one share was
wrong by 28 points and one had an inverted sign. So the values are pinned now,
**beside** the properties, never instead of them — the lesson recorded on
roadmap C3 is that a golden test pinning values without asserting the property
those values exist to protect will happily lock in a bug. Every `approx` below
sits next to the inequality or sign test it is the numeric witness for.

`TOUR` is `abs=0.001` — a tenth of a percentage point — for anything the UI
renders as a percentage, and `rel=1e-3` for money and counts. That is a
deliberate choice at both ends:

* **Not looser**, because the drift this suite failed to catch was 28 points,
  4.3 points on `new_arpu`, and a 1.6-point swing that inverted a sign. A
  tolerance anywhere near those is worth nothing. 0.001 is ~280x tighter than
  the worst of them, and tighter than the last digit the tour prints.
* **Not tighter**, because nearly every pinned figure is deterministic
  arithmetic over committed parquet — Shapley on an identity, or a ratio of
  window means — with no sampler in the path. Measured across separate
  processes these agree bit-for-bit, so the real noise floor is ~1e-13 and
  0.001 is ten orders of magnitude of headroom for a different BLAS or a
  summation reorder.

**Every fitted node in this suite has a sampler in its path**, and the
tolerance argument above does not cover them. Since roadmap S2's second half
the engine samples with NUTS by default — mean-field ADVI fails its PSIS check
on essentially every real node here, and moves point estimates by tens of
percent — so `sessions`, `trials_started`, `trial_conversion_rate` and
`customer_churn_rate` are all MCMC-derived. `run_rca` seeds (`random_seed=0`),
so a rerun on *this* machine reproduces bit-for-bit; a seed does **not** make a
NUTS chain reproducible across a different CPU, BLAS or PyMC build, and
measurement says how much it does not:

| quantity | this machine | CI |
|---|---|---|
| `share_of_gap`, non-collinear parents | −0.0768 | agrees within 1e-3 |
| `share_of_gap`, split of a collinear pair | 0.6804 | **0.6822** / **0.6967** |
| β (posterior mean) | −0.036 | agrees within 1e-3 |
| β HDI 2.5% (tail quantile) | −0.059 | **−0.061** |
| `P(direction)` | 0.838 | just under **0.835** |

So the two kinds of number get two treatments, and the line between them is
statistical rather than convenient. **Posterior means** are averages over
~5,000 effective draws; their Monte-Carlo error is small, they hold at TOUR's
tolerance, and `share_of_gap` is tied to the document with `prints()`. **Tail
quantiles** (an HDI bound) carry far more Monte-Carlo error than their centre,
so they get a band sized to the measured spread. And a value sitting on a
**rounding boundary** — `P(direction)` at 0.835 — is unsafe to `prints()` at
any precision, because the two machines print different second decimals from
the same seed; the tour writes it "≈0.84" and the test bands it. That one cost
a red CI run to learn, which is why it is written down.

Apply that split to **every** sampler-derived figure added here, not only the
four measured above. A `share_of_gap`, a contribution `estimate` and a `beta`
mean are posterior means; a `ci_95` bound, an HDI bound and a
`prob_same_direction` near a rounding boundary are tail quantiles and get bands.

**And "posterior means pin at TOUR" has one large exception, which CI found.**
The split of credit between two **collinear** parents is not a stable quantity
across numeric stacks, even seeded. Story D's `trial_activation_rate` share —
whose co-parent `trial_days_active` rides the same underlying engagement, on
purpose — measured, from the same `random_seed=0`:

| stack | share |
|---|---|
| macOS/arm64, py3.14 | 0.6804 |
| CI Linux, py3.12 | 0.6822 |
| CI Linux, py3.11 **and** py3.13 (bit-identical to each other) | **0.6967** |

A **1.6-point** spread, sixteen times TOUR, with no run-to-run variance inside
a stack. That is not Monte-Carlo noise in the ordinary sense; it is roadmap
**S4**'s failure shape reproduced by a BLAS difference. On a ridge the *sum* of
two collinear coefficients is well determined and the *split* is not, so a
chain that traverses the ridge slightly differently lands somewhere else along
it — which is exactly what this story exists to tell a prospect, now
demonstrated at the tolerance level.

So a share that is **one parent's half of a collinear pair** is banded to the
measured cross-stack range and is never `prints()`-ed: 68.0% and 69.7% are
different strings and both are true. A share on a node whose parents are not
collinear keeps TOUR — story B's −0.0768 agrees within 1e-3 across machines,
and story A's identities are deterministic.

What must never be relaxed is the row of properties beside these values — sign,
magnitude bound, the contribution interval straddling zero, `P(direction)`
under 0.9, and the coefficient HDI clear of zero. They are the beat; the values
are only its witnesses. When a PyMC or numpy bump moves a value, **re-measure
and re-pin**; widening is for cross-machine noise that has been measured, not
for a figure that genuinely moved.

Everywhere else the tour would have had to quote a sampler-derived number (the
`marketing_spend` what-if lever, which also runs through a learned edge), it
quotes the *contrast* instead — a wide interval against a degenerate one — and
that contrast is what is asserted, with a band rather than a point.

Two claims the tour makes are **product defects, not stale numbers**, and are
pinned here as such: `churn_arpu` has no declared `direction` so the UI colours
it green, and the RCA table renders no `lag`. Those tests exist to go red when
the defect is *fixed*, at which point the matching `⚠ known gap` note in the
tour should be deleted.
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

# Tolerance for a figure the tour prints. See the module docstring.
TOUR = dict(abs=0.001)

pytestmark = pytest.mark.skipif(
    not os.path.isdir(SNAPSHOTS) or not os.listdir(SNAPSHOTS),
    reason=(
        "demo snapshots not present — `demo/` is repo-only, not part of the distribution; "
        "in a checkout, run `make -C demo snapshots`"
    ),
)

START, END = "2024-06-01", "2026-07-30"


@pytest.fixture(scope="module")
def client():
    """One instance for the module so fits are cached across stories.

    project_path points nowhere on purpose: if any of this reaches a provider
    instead of a snapshot, it fails loudly rather than quietly working on a
    machine that happens to have the dbt project."""
    prior = {
        k: os.environ.get(k)
        for k in (
            "BREAKDOWN_TREE",
            "BREAKDOWN_START_DATE",
            "BREAKDOWN_END_DATE",
            "BREAKDOWN_SNAPSHOT_DIR",
            "BREAKDOWN_REFRESH",
            "WHITE_CUBE_DBT_PROJECT",
        )
    }
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
            "reference_start": ref[0],
            "reference_end": ref[1],
            "analysis_start": ana[0],
            "analysis_end": ana[1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def slices(client, metric, dimension, ref, ana):
    r = client.post(
        f"/rca/{metric}/slices",
        params={
            "dimension": dimension,
            "reference_start": ref[0],
            "reference_end": ref[1],
            "analysis_start": ana[0],
            "analysis_end": ana[1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------- the UI surface
#
# The tour is read off the screen, not off the payload, and the two differ on
# purpose: the RCA tab's *Headline* view (the default, `state.rcaView`) puts the
# co-movement term in its own row, so a driver's share there is its window-means
# part over the gap, while the payload's `share_of_gap` folds each parent's
# slice of co-movement back in. `new_subscriptions` in story A is 130.0% on
# screen and 1.2899 in the payload; both decompositions are complete.
#
# These three helpers are ports of `breakdown/static/app.js` — `shareOf` (~3062)
# over `decomposition.means` (~3130), and the `localized` rule (~2856). There is
# no JS test runner here (MVP-first, deliberately), so this is the seam: if that
# rendering changes, change these with it, and re-measure the tour.


TOUR_DOC = os.path.join(REPO, "knowledge", "demo_guided_tour.md")


def prints(value, spec="{:.1f}%", scale=100.0):
    """Assert the tour actually prints this *measured* figure.

    The assertions below pin the engine, which closes the direction that
    actually failed: attribution changed under the tour (C3, C5) and nothing
    went red. It leaves the other direction open — edit a percentage in the
    tour by hand and the literals here still match each other while the
    document is wrong.

    Deriving the string from the measured value rather than from a third copy
    closes it both ways at once. Engine drifts, the formatted string stops
    appearing, red. Tour edited, the string stops appearing, red. The tour's
    guarantee is only worth what the weaker direction is worth.
    """

    # The tour is prose and uses a typographic minus (U+2212); Python formats
    # an ASCII hyphen. Normalize both rather than making the document uglier.
    def ascii_minus(t):
        return t.replace("\u2212", "-").replace("\u2013", "-")

    printed = ascii_minus(spec.format(value * scale))
    with open(TOUR_DOC) as f:
        text = ascii_minus(f.read())
    assert printed in text, (
        f"the guided tour does not print {printed}, but this test pins it. "
        "Either the engine moved and the tour is stale, or the tour was edited "
        "without its pin — the document promises both are impossible."
    )


def ui_share(node, parent):
    """A driver's share as the Headline table prints it: means over gap."""
    (c,) = [c for c in node["contributions"] if c["parent"] == parent]
    return c["decomposition"]["means"]["estimate"] / node["gap"]


def ui_comovement(node):
    """The `co-movement shift` row's share, as the Headline table prints it."""
    return node["interaction"]["estimate"] / node["gap"]


def ui_localized(s):
    """Whether the slice panel prints the "<x> carries N% of the gap" verdict
    rather than one of the two restrained ones. Since C24 the verdict is
    *published* and the UI just reads it — this helper reads the same field, so
    the browser rule, this suite and MCP consumers cannot drift apart again.
    (The hand-mirrored recomputation this replaces is how the rate panels'
    always-false verdict went unnoticed: every rate assertion here was a
    negative one, passing for the wrong reason.)

    Since roadmap 2.21 there are three states, and this helper still answers
    the narrow question the panel's headline sentence asks — "may I name this
    slice?" — which `long_tail` answers no to. `ui_verdict` is the full one."""
    return bool(s.get("localized"))


def ui_verdict(s):
    """The verdict the panel renders, in the engine's own vocabulary
    (roadmap 2.21): `localized`, `long_tail` or `not_localized`."""
    return s["localization"]


def test_the_tree_is_the_size_the_script_says():
    """Section 0 of the tour is read aloud: "23 metrics in 376 lines, of which
    279 are actual configuration". Pinned exactly rather than as a band — if you
    edit the tree, the sentence a presenter says about it needs editing too, and
    that is precisely the drift this module exists to catch."""
    with open(TREE) as f:
        lines = f.read().splitlines()
    assert len(lines) == 376
    assert len([ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]) == 279
    assert len([ln for ln in lines if ln.startswith("  - name:")]) == 23


def test_boots_hermetically(client):
    """No provider reachable, yet every metric has data."""
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["metrics"] == 23

    meta = client.get("/meta").json()
    assert meta["date_start"] == START and meta["date_end"] == END
    assert meta["grains"]["net_new_mrr"] == "week"
    assert meta["grains"]["signups"] == "day"
    assert meta["kinds"]["trial_conversion_rate"] == "rate"


def test_mrr_identity_is_exact(client):
    """net_new_mrr = new + expansion - contraction - churned, to float error.

    If this drifts, the semantic layer and the tree have disagreed about what
    the identity is, and every attribution below it is suspect."""
    d = rca(client, "net_new_mrr", ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07"))
    node = d["nodes"]["net_new_mrr"]
    assert node["attribution_method"] == "shapley"
    assert abs(node["unexplained"]) < 1e-6 * max(1.0, abs(node["gap"]))
    assert {c["parent"] for c in node["contributions"]} == {
        "new_mrr",
        "expansion_mrr",
        "contraction_mrr",
        "churned_mrr",
    }


def test_story_a_signup_regression_traverses_and_localizes(client):
    """A mobile signup CTA broke: new MRR falls, the tree walks back to signups,
    and the slice names mobile."""
    ref, ana = ("2026-01-05", "2026-02-01"), ("2026-02-09", "2026-03-08")
    d = rca(client, "new_mrr", ref, ana)

    top = d["nodes"]["new_mrr"]
    assert top["gap"] < 0
    # "New MRR is down −16.7% (−$324/week)."
    assert top["relative_change"] == pytest.approx(-0.167, **TOUR)
    assert top["gap"] == pytest.approx(-324.0, rel=1e-3)

    # "...walks it back through new_subscriptions (−16.5%) and trial_conversions
    # (−18.9%) to signups (−11.2%)."
    for metric, quoted in (
        ("new_subscriptions", -0.165),
        ("trial_conversions", -0.189),
        ("signups", -0.112),
    ):
        assert d["nodes"][metric]["relative_change"] == pytest.approx(quoted, **TOUR), metric

    # The volume chain saturates the influence score, so which of its members
    # sorts first is a tie — what must hold is that the whole chain outranks
    # the alternatives the story is meant to rule out (price, and the demand
    # side that did not move).
    ranked = [c["metric"] for c in d["ranked_causes"]]
    chain = {"new_subscriptions", "trial_conversions", "trials_started", "signups"}
    assert ranked[0] in chain
    for alternative in ("new_arpu", "trial_conversion_rate"):
        assert max(ranked.index(m) for m in chain) < ranked.index(alternative)

    # "new_subscriptions carries 98.8% of the gap; average deal size is a
    # bystander (new_arpu 3.2%, co-movement −1.9%)." The property those values
    # protect: the price axis is *cleared*, not netted away — the count fell,
    # what each subscription was worth did not move the story. The tour also
    # quotes the payload's 0.978 for the same driver, to explain why the API
    # and the screen differ; pinned so that claim cannot drift either.
    assert ui_share(top, "new_subscriptions") > 0.9
    assert abs(ui_share(top, "new_arpu")) < 0.1
    assert ui_share(top, "new_subscriptions") == pytest.approx(0.988, **TOUR)
    assert ui_share(top, "new_arpu") == pytest.approx(0.032, **TOUR)
    assert ui_comovement(top) == pytest.approx(-0.019, **TOUR)
    # ...and the tour prints exactly these, so neither side can drift alone.
    prints(ui_share(top, "new_subscriptions"))
    prints(ui_share(top, "new_arpu"))
    prints(top["relative_change"])
    payload = {c["parent"]: c["share_of_gap"] for c in top["contributions"]}
    assert payload["new_subscriptions"] == pytest.approx(0.978, **TOUR)

    # "unexplained on new_mrr reads −2.27e-13. The identity is exact." The
    # digits are float noise, so the tour says so and this pins the magnitude.
    assert abs(top["unexplained"]) < 1e-9

    # new_subscriptions is a *measured* identity now — every way into a paid
    # plan is a term, the books close exactly, and the trial term still
    # carries the one-week lag whose shifted window must land on the anomaly,
    # not on the calendar window. The tour quotes the full shifted pair,
    # because that is what the slice panel's footer prints — see
    # test_known_gap_the_rca_table_cannot_show_the_lag.
    ns = d["nodes"]["new_subscriptions"]
    assert ns["attribution_method"] == "shapley"
    assert abs(ns["unexplained"]) < 1e-9
    assert {c["parent"] for c in ns["contributions"]} == {
        "trial_conversions",
        "reactivations",
        "direct_conversions",
    }
    # "trial conversions carry 86.4% of the drop, direct conversions 16.1%
    # (they fell too), and reactivations −2.5% (they ticked up against it)."
    ns_shares = {c["parent"]: c["share_of_gap"] for c in ns["contributions"]}
    assert ns_shares["trial_conversions"] == pytest.approx(0.864, **TOUR)
    assert ns_shares["direct_conversions"] == pytest.approx(0.161, **TOUR)
    assert ns_shares["reactivations"] == pytest.approx(-0.025, **TOUR)
    (contribution,) = [c for c in ns["contributions"] if c["parent"] == "trial_conversions"]
    assert contribution["lag"] == 1
    assert contribution["parent_windows"]["reference"] == {
        "start": "2025-12-29",
        "end": "2026-01-25",
    }
    assert contribution["parent_windows"]["analysis"] == {
        "start": "2026-02-02",
        "end": "2026-03-01",
    }

    # The CTA broke volume first: trials_started carries 68.7% against
    # conversion's 32.0%. Conversion is no longer a flat bystander — ambient
    # trial engagement wobbles it a few points in every window now that the
    # edge is real — so the property is dominance (volume ~2x quality), not a
    # sign, and the tour says "mostly volume" rather than "not quality".
    tc = d["nodes"]["trial_conversions"]
    conv = {c["parent"]: c["share_of_gap"] for c in tc["contributions"]}
    assert conv["trials_started"] > conv["trial_conversion_rate"]
    assert ui_share(tc, "trials_started") > 2 * ui_share(tc, "trial_conversion_rate")
    assert ui_share(tc, "trials_started") == pytest.approx(0.687, **TOUR)
    assert ui_share(tc, "trial_conversion_rate") == pytest.approx(0.320, **TOUR)
    assert ui_comovement(tc) == pytest.approx(-0.007, **TOUR)

    # The slice the tour scripts. `signups` sits below the lagged edge and is
    # not itself a lagged parent, so `sliceWindowsFor` (app.js ~2743) finds no
    # `parent_windows` for it and the UI slices over its own effective windows
    # — the calendar block. These are the numbers on the presenter's screen.
    s = slices(client, "signups", "device", ref, ana)
    assert s["slices"][0]["value"] == "mobile"
    assert s["slices"][0]["baseline_share"] < abs(s["slices"][0]["share_of_gap"])
    assert s["reconciliation"]["status"] == "ok"
    assert not s["slices"][0]["noise_level"]
    assert ui_verdict(s) == "localized"
    # "mobile carries 76.2% of the gap on a 51.1% baseline share."
    assert s["slices"][0]["share_of_gap"] == pytest.approx(0.762, **TOUR)
    assert s["slices"][0]["baseline_share"] == pytest.approx(0.511, **TOUR)

    # "Not localized by country" — the contrast that is the point of the demo.
    # It is a verdict, not a number, so the rule behind it is what gets pinned.
    c = slices(client, "signups", "country", ref, ana)
    # The exact state, not merely "not localized": since 2.21 the restrained
    # verdict has two forms, and the tour prints this one's sentence.
    assert ui_verdict(c) == "not_localized"
    assert abs(c["slices"][0]["excess"] / c["gap"]) < 0.25

    # Still asserted over the lag-shifted pair too: a prospect who slices from
    # the trial_conversions cause gets those windows, and they must also name
    # mobile rather than falling through to a provider.
    shifted = slices(
        client, "signups", "device", ("2025-12-29", "2026-01-25"), ("2026-02-02", "2026-03-01")
    )
    assert shifted["slices"][0]["value"] == "mobile"
    assert shifted["reconciliation"]["status"] == "ok"


def test_story_b_churn_spike_is_plan_localized_and_engagement_is_cleared(client):
    """Churn spiked on the professional tier: the whole net-new-MRR drop is the
    churn branch, acquisition is flat, and the tree's learned engagement edge
    correctly declines to take the blame."""
    ref, ana = ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07")
    d = rca(client, "net_new_mrr", ref, ana)

    assert d["nodes"]["net_new_mrr"]["gap"] < 0
    assert d["ranked_causes"][0]["metric"] == "churned_mrr"
    assert d["nodes"]["churned_mrr"]["gap"] > 0  # stored positive: more churn

    # The churn branch carries the whole story while acquisition sits it out —
    # these are the levels the presenter reads out.
    for metric, quoted in (
        ("net_new_mrr", -0.385),
        ("churned_mrr", 0.848),
        ("churned_subscriptions", 0.479),
        # Σnumerator / Σdenominator over the window (roadmap 1.11c), not an
        # average of the four weekly ratios. The check that this is the right
        # number, not merely a different one, is the identity assertion below.
        ("customer_churn_rate", 0.342),
        ("churn_arpu", 0.250),
    ):
        assert d["nodes"][metric]["relative_change"] == pytest.approx(quoted, **TOUR), metric
    assert d["nodes"]["churned_mrr"]["relative_change"] > 0
    # Acquisition is flat — the drop is not a demand story. (An earlier
    # edition of this data had acquisition *up* and masking the churn; the
    # engagement-edges regeneration flattened it, and the story is cleaner
    # for it: one branch moved.)
    assert abs(d["nodes"]["new_mrr"]["relative_change"]) < 0.05
    assert abs(d["nodes"]["new_subscriptions"]["relative_change"]) < 0.05

    # "Inside churned_mrr, the split is 63.6% / 36.3% (co-movement 0.1%)":
    # more cancellations, and the ones cancelling were worth more than average.
    cm = d["nodes"]["churned_mrr"]
    assert ui_share(cm, "churned_subscriptions") > ui_share(cm, "churn_arpu") > 0
    assert ui_share(cm, "churned_subscriptions") == pytest.approx(0.636, **TOUR)
    assert ui_share(cm, "churn_arpu") == pytest.approx(0.363, **TOUR)
    assert ui_comovement(cm) == pytest.approx(0.001, **TOUR)

    # The learned engagement edge is *cleared*, not silent — and since the
    # 2026-08-22 generator fidelity fix it is cleared on stronger evidence than
    # before, which is worth stating because it is the beat's whole point.
    #
    # Member activity moved **up** (+2.5%) in a window where churn spiked. The
    # tree declares the edge's sign negative ("disengaged members churn"), so an
    # activity *rise* predicts churn *falling*: the contribution runs against
    # the gap rather than explaining it, and the posterior is unsure of even
    # that sign. The tree examined the "members disengaged" story and declined
    # it, which is exactly what a planted pricing-tier spike requires of an
    # engagement edge.
    #
    # Why the bounds are unchanged and now mean more: `dau_over_active` used to
    # reach the event stream as an NHPP rate factor, so `member_activity_rate`
    # sat at ~93% against its ceiling with weekly corr(activity, the shared
    # `member_engagement` driver) of only +0.28 — the edge partly cleared itself
    # because it could barely see anything. At the configured ~25% level that
    # correlation is +0.74 and the mechanism is genuinely strong, so declining
    # it here is a statement about *this window*, not about a squashed driver.
    #
    # And the numbers below come from **NUTS**, which is what every fitted
    # node in this suite now runs (roadmap S2's second half). This node is the
    # one that made the case for that default: run the same window with
    # `?inference_method=advi` and it scores PSIS k̂ 1.26, reporting this
    # contribution as −4.9% of the gap instead of −7.7% — a point estimate a
    # third too small, with every property still holding, which is exactly why
    # properties alone were not enough. See the module docstring for what a
    # sampler in the path means for the tolerance.
    mar = d["nodes"]["member_activity_rate"]
    assert mar["relative_change"] > 0, "activity rose — the wrong way for the churn story"
    assert mar["relative_change"] == pytest.approx(0.025, **TOUR)
    # The tour quotes the levels too, because "+2.5%" of a saturated 93% and of
    # a 25% working range are different claims, and the presenter is standing in
    # front of the node card showing the level.
    assert mar["baseline"] == pytest.approx(0.254, **TOUR)
    assert mar["actual"] == pytest.approx(0.260, **TOUR)
    ccr = d["nodes"]["customer_churn_rate"]
    # The node the beat rests on must be exactly sampled, and must say so with
    # the *absence* of a k-hat rather than a good one: a `khat_status` here at
    # all would mean the tour is quoting an approximation, and `unusable`
    # would mean quoting one the engine says is not evidence about its own
    # interval width.
    assert ccr["inference_method"] == "nuts"
    assert ccr["khat_status"] is None and ccr["khat"] is None
    assert ccr["fit_quality"] == "ok"
    (eng,) = [c for c in ccr["contributions"] if c["parent"] == "member_activity_rate"]
    assert eng["share_of_gap"] < 0, "the edge pulls against the gap, not merely a little with it"
    assert abs(eng["share_of_gap"]) < 0.15
    assert eng["share_of_gap"] == pytest.approx(-0.077, **TOUR)
    assert eng["ci_95"][0] < 0 < eng["ci_95"][1]
    assert eng["prob_same_direction"] < 0.9
    # A band, not a pin: this is the figure here a PyMC/numpy bump could nudge
    # most, and the tour quotes it to two places. It is deliberately *not*
    # `prints()`-ed — see the module docstring: this machine measures 0.838 and
    # CI measures a hair under 0.835, so the two round to different second
    # decimals from the same seed. The tour writes it "≈0.84" for that reason.
    assert eng["prob_same_direction"] == pytest.approx(0.84, abs=0.04)
    # The headline the presenter reads, and the one figure here safe to tie to
    # the document: it is a posterior *mean* over ~5,000 effective draws, at
    # one decimal place of a percentage. See the module docstring for why that
    # is a different kind of number from the two above and below.
    prints(eng["share_of_gap"])

    # The two intervals the tour now separates, because a prospect who clicks
    # the node sees both and they say different things. The *contribution*
    # interval straddles zero (asserted above); the *coefficient* interval does
    # not. That distinction is the beat: the edge is real and its direction is
    # settled, and it still does not explain this window, because activity
    # barely moved. The NUTS default is what made it true — the same fit under
    # mean-field puts this HDI at [-0.0530, +0.0050], failing to exclude zero.
    m = client.get("/metrics/customer_churn_rate")
    assert m.status_code == 200, m.text
    summary = m.json()["summary"]
    assert m.json()["diagnostics"]["method"] == "nuts"
    beta, lo, hi = (
        summary["mean"]["beta_raw[0]"],
        summary["hdi_2.5%"]["beta_raw[0]"],
        summary["hdi_97.5%"]["beta_raw[0]"],
    )
    assert hi < 0, (
        "the coefficient's HDI must stay clear of zero — the tour tells the "
        "presenter to say the edge itself is settled, and only the "
        "contribution is unsure"
    )
    # The mean is a mean, so it holds at TOUR's tolerance. The HDI *bounds* are
    # tail quantiles and do not: this machine reads [-0.059, -0.013] and CI
    # reads [-0.061, -0.013] from the same seed, because a 2.5% quantile of
    # 2,000 draws carries far more Monte-Carlo error than their centre. Banded
    # to the measured spread with room, which is what the difference between
    # the two kinds of number actually warrants — the claim that has to hold
    # exactly is `hi < 0` above, and that is asserted hard.
    assert beta == pytest.approx(-0.036, abs=1e-3)
    assert lo == pytest.approx(-0.060, abs=0.004)
    assert hi == pytest.approx(-0.013, abs=0.004)

    s = slices(client, "churned_mrr", "plan", ref, ana)
    assert s["slices"][0]["value"] == "professional"
    assert s["slices"][0]["excess"] > 0
    assert s["reconciliation"]["status"] == "ok"
    assert ui_verdict(s) == "localized"
    # "professional carries 100.6% of the gap on a 44.0% baseline share."
    assert s["slices"][0]["share_of_gap"] == pytest.approx(1.006, **TOUR)
    assert s["slices"][0]["baseline_share"] == pytest.approx(0.440, **TOUR)

    # The contrast: a tier, not a geography — on the same node the plan slice
    # just localized, so the two verdicts are directly comparable. (The tour
    # scripts the contrast here rather than on customer_churn_rate, whose
    # country slice is the long-tail case pinned below: a third verdict makes
    # a muddier beat than two opposite ones on one node.)
    c = slices(client, "churned_mrr", "country", ref, ana)
    assert ui_verdict(c) == "not_localized"
    assert len(c["slices"]) == 9
    assert sum(1 for row in c["slices"] if row["noise_level"]) == 7


def test_story_b_the_churn_rate_by_country_is_a_long_tail_verdict(client):
    """Roadmap 2.21, on the window that produced it.

    `customer_churn_rate` by country concentrates 26.4% of the gap — a hair
    over the 25% bar — in `__other__`, the fold of the four countries outside
    this dimension's `top_k`. Every named country is either noise-level or
    carries a few percent. Before 2.21 this published `localized: true` and
    the panel printed "*everything else carries 42.6% of the gap*", naming as
    the culprit the one row that is not a segment: the reader's next move
    (go and look at that country) does not exist. It is now its own verdict,
    with the remedy on screen.

    Raising `top_k` to 20 on this exact window enumerates all twelve
    countries and the verdict falls to `not_localized` — the best named slice
    is AU at 13.8% — which is the check that `long_tail` was the honest
    reading and not a softened `localized`."""
    ref, ana = ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07")
    c = slices(client, "customer_churn_rate", "country", ref, ana)

    assert ui_verdict(c) == "long_tail"
    assert not ui_localized(c)  # the headline sentence stays unprintable
    top = c["slices"][0]
    assert top["value"] == "__other__"
    assert top["n_values"] == 4
    assert abs(top["excess"] / c["gap"]) == pytest.approx(0.264, **TOUR)
    assert not top["noise_level"]
    # Five of nine rows are noise-flagged: nothing named stands out either.
    assert sum(1 for row in c["slices"] if row["noise_level"]) == 5
    # The remedy travels with the verdict, not only in the browser: the panel
    # renders this exact sentence, and an MCP consumer gets the same one.
    assert "top_k" in c["localization_remedy"]


def test_known_gap_churn_arpu_is_undeclared_and_the_ui_colours_it_green(client):
    """A product defect the tour must not paper over, filed separately.

    Story B says the churn branch is red. `churn_arpu` rises here and carries
    36.3% of the damage, and it renders **green, with an up arrow**, on the
    presenter's screen. `demo/white_cube_tree.yml` declares no `direction` on it;
    `MetricDefinition.direction` defaults to `up_is_good` (parser.py ~648) and
    `/dag` serializes with `model_dump()`, so the default arrives at the UI
    indistinguishable from a declared value. The UI is not free to do better
    here — by the time it has the payload, silence and an explicit `up_is_good`
    look identical — which is why this is filed as a defect rather than fixed by
    editing the tree in passing.

    This pins the defect, not the fix. It goes red when someone declares a
    direction on `churn_arpu`, which is the moment the `⚠ known gap` note in
    `knowledge/demo_guided_tour.md` must be deleted."""
    d = rca(client, "net_new_mrr", ("2026-03-16", "2026-04-12"), ("2026-05-11", "2026-06-07"))
    node = d["nodes"]["churn_arpu"]
    assert node["gap"] > 0  # up, and up is bad for a churn ARPU
    # 0.250 — `churn_arpu` aggregates over the window as
    # Σchurned_mrr / Σchurned_subscriptions, weighting each week by the
    # cancellations it actually had, rather than averaging four weekly ARPUs.
    assert node["relative_change"] == pytest.approx(0.250, **TOUR)

    yaml = pytest.importorskip("yaml")
    with open(TREE) as f:
        declared = {m["name"]: m.get("direction") for m in yaml.safe_load(f)["metrics"]}
    assert declared["churn_arpu"] is None, (
        "churn_arpu now declares a direction — the tree is authored, so delete "
        "the remaining '⚠ known gap' note in knowledge/demo_guided_tour.md"
    )
    # The neighbours that *are* declared, so this reads as one metric missed
    # rather than a tree-wide omission.
    for correct in ("churned_mrr", "churned_subscriptions", "customer_churn_rate"):
        assert declared[correct] == "down_is_good"

    # ...and the silence now survives serialization, so the UI renders it
    # neutrally instead of claiming "improved". This assertion is the half that
    # changed: `direction` defaulted to `up_is_good` in the parser, so the
    # default arrived at the browser indistinguishable from a declaration and
    # app.js's own `|| "up_is_good"` fallback could never fire. The tree is
    # still under-authored — that is the known gap the tour flags — but an
    # absent declaration is no longer a confident wrong one.
    defs = dict(client.get("/dag").json()["nodes"])
    assert defs["churn_arpu"]["direction"] is None


def test_known_gap_the_rca_table_cannot_show_the_lag(client):
    """The other product defect, also filed separately.

    The tour used to script the presenter to point at a `lag 1` tag and a parent
    window in the RCA attribution table. That table renders neither (app.js
    ~3126-3183 has no lag column and no parent_windows); the lag reaches the
    screen only via the Metric tab's declared-lag chip and the slice panel's
    window footer.

    So: the payload carries both, which is where the tour's date comes from, and
    the slice panel really does print the shifted pair for the lagged parent.
    That second assertion is what makes the rewritten script runnable."""
    ref, ana = ("2026-01-05", "2026-02-01"), ("2026-02-09", "2026-03-08")
    d = rca(client, "new_mrr", ref, ana)
    (c,) = [
        k
        for k in d["nodes"]["new_subscriptions"]["contributions"]
        if k["parent"] == "trial_conversions"
    ]
    assert c["lag"] == 1 and c["parent_windows"] is not None

    # `sliceWindowsFor("trial_conversions")` (app.js ~2743) finds this
    # contribution and slices over its `parent_windows`, so the panel footer
    # reads "2025-12-29 → 2026-01-25 vs 2026-02-02 → 2026-03-01 · windows
    # shifted back 1 week for the lag" — the one place the shift is on screen.
    s = slices(
        client,
        "trial_conversions",
        "device",
        (c["parent_windows"]["reference"]["start"], c["parent_windows"]["reference"]["end"]),
        (c["parent_windows"]["analysis"]["start"], c["parent_windows"]["analysis"]["end"]),
    )
    assert s["effective_windows"]["reference"]["start"] == "2025-12-29"
    assert s["effective_windows"]["analysis"]["start"] == "2026-02-02"
    assert s["effective_windows"]["analysis"]["end"] == "2026-03-01"


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

    # "Signups up +8.5% ... sessions carries 60.0%, visit_signup_rate 42.8% ...
    # sessions themselves are only +5.0%." Both halves positive is the property;
    # the levels are what says it is not just volume.
    assert node["relative_change"] == pytest.approx(0.085, **TOUR)
    assert d["nodes"]["sessions"]["relative_change"] == pytest.approx(0.050, **TOUR)
    assert ui_share(node, "sessions") == pytest.approx(0.600, **TOUR)
    assert ui_share(node, "visit_signup_rate") == pytest.approx(0.428, **TOUR)
    # Co-movement is a small negative here (−2.8%): the two halves are
    # near-additive, which is why the tour reads the 60/43 split straight off
    # the table. (An earlier edition of the data had +7.8% and the tour read
    # it out; the sentence follows the number, so it is pinned either way.)
    assert ui_comovement(node) == pytest.approx(-0.028, **TOUR)

    s = slices(client, "signups", "country", ref, ana)
    top = s["slices"][0]
    assert top["value"] == "BR"
    # Brazil is ~8% of traffic but carries ten times that share of the gap
    assert top["share_of_gap"] > 3 * top["baseline_share"]
    assert ui_verdict(s) == "localized"
    # "BR carries 84.3% of the gap on an 8.4% baseline share."
    assert top["share_of_gap"] == pytest.approx(0.843, **TOUR)
    assert top["baseline_share"] == pytest.approx(0.084, **TOUR)


def test_story_d_onboarding_lift_beats_the_trend(client):
    """The revamp has to show up as conversion, not as volume — the business is
    growing underneath, and that growth must not be credited to the change."""
    ref, ana = ("2025-07-07", "2025-08-03"), ("2025-08-11", "2025-09-07")
    d = rca(client, "new_mrr", ref, ana)

    assert d["nodes"]["new_mrr"]["gap"] > 0
    conv = {
        c["parent"]: c["share_of_gap"] for c in d["nodes"]["trial_conversions"]["contributions"]
    }
    assert conv["trial_conversion_rate"] > conv["trials_started"]

    # "New MRR up +26.0% ... trial_conversion_rate carries 69.3% against
    # trials_started's 29.8% (co-movement 0.9%)."
    tc = d["nodes"]["trial_conversions"]
    assert d["nodes"]["new_mrr"]["relative_change"] == pytest.approx(0.260, **TOUR)
    assert ui_share(tc, "trial_conversion_rate") == pytest.approx(0.693, **TOUR)
    assert ui_share(tc, "trials_started") == pytest.approx(0.298, **TOUR)
    assert ui_comovement(tc) == pytest.approx(0.009, **TOUR)


def test_story_d_names_activation_as_the_mechanism(client):
    """The marquee beat of the rebuilt story D: the conversion lift itself has
    a cause the tree can name. The revamp was planted on the *engagement*
    driver — activation and days-active rose, conversion followed through the
    generator's per-user coupling — so the RCA on trial_conversion_rate must
    hand the gap to trial_activation_rate with a confident posterior.

    The second parent is the honesty half. trial_days_active rides the same
    underlying engagement (deliberately collinear — roadmap S4's failure shape,
    planted on purpose), so the fit sizes the pair's *sum* far more surely than
    the split: activation's interval excludes zero, days-active's does not.
    The tour scripts that contrast rather than hiding it, because "the tool is
    sure of the total and honest about the split" is the pitch."""
    ref, ana = ("2025-07-07", "2025-08-03"), ("2025-08-11", "2025-09-07")
    d = rca(client, "new_mrr", ref, ana)

    # The chain the presenter walks: activation +35.9%, days active +76.1%,
    # conversion +40.2% — against trial volume's +15.5% trend (see the
    # adjacency test below for why that number is the control).
    #
    # The two engagement figures moved on 2026-08-22 (activation +34.4% ->
    # +35.9%, days active +71.2% -> +76.1%) because both are recomputed in dbt
    # from `product.events`, and the generator stopped folding `dau_over_active`
    # into an event-rate intensity. Conversion is drawn from the lifecycle, not
    # from events, so it did not move at all — which is the check that the
    # regeneration hit the intended layer and nothing else.
    assert d["nodes"]["trial_activation_rate"]["relative_change"] == pytest.approx(0.359, **TOUR)
    assert d["nodes"]["trial_days_active"]["relative_change"] == pytest.approx(0.761, **TOUR)
    assert d["nodes"]["trial_conversion_rate"]["relative_change"] == pytest.approx(0.402, **TOUR)

    ccr = d["nodes"]["trial_conversion_rate"]
    assert ccr["status"] == "ok"
    by = {c["parent"]: c for c in ccr["contributions"]}
    act, days = by["trial_activation_rate"], by["trial_days_active"]
    # "trial_activation_rate carries 68.0% of the conversion gap, interval
    # clear of zero, P(direction) 0.998."
    #
    # 70.7% under mean-field, and this node is the counter-example worth
    # keeping: its ADVI k̂ is 0.497 — inside the *good* band, the one node in
    # the demo the approximation genuinely represents — and the share still
    # moved 2.7 points. A passing k̂ bounds the error; it does not zero it.
    #
    # Banded, not pinned at TOUR, and the module docstring says why: this is one
    # parent's half of a *collinear* pair, and the split between two collinear
    # parents is the quantity a ridge leaves undetermined. Seeded, it measures
    # 0.6804 (macOS/arm64), 0.6822 (Linux py3.12) and 0.6967 (Linux py3.11 and
    # py3.13) — a 1.6-point spread with no variance inside a stack. The tour
    # says "about 70%" rather than a decimal the stacks disagree on, and the
    # assertions that carry the beat are the properties below, not this number.
    assert act["share_of_gap"] == pytest.approx(0.689, abs=0.015)
    assert act["ci_95"][0] > 0
    assert act["prob_same_direction"] > 0.99
    # ...and the collinear twin is reported unsurely, as it should be.
    assert days["ci_95"][0] < 0 < days["ci_95"][1]
    assert days["prob_same_direction"] < 0.99
    assert act["share_of_gap"] > days["share_of_gap"]
    # Both causes rank above trial volume in the tree-wide ranking.
    ranked = [c["metric"] for c in d["ranked_causes"]]
    assert ranked.index("trial_activation_rate") < ranked.index("trials_started")


def test_story_d_a_non_adjacent_reference_would_credit_the_trend(client):
    """The tour's "worth saying" aside is a quantitative claim, so it is run.

    Measured: pushing the reference back eight weeks moves `trials_started`
    from +15.5% to +21.6% — six points of trend the tree would then credit to
    the onboarding revamp. The property is the inequality; the levels are what
    the tour prints."""
    ana = ("2025-08-11", "2025-09-07")
    adjacent = rca(client, "trials_started", ("2025-07-07", "2025-08-03"), ana)
    stale = rca(client, "trials_started", ("2025-05-12", "2025-06-08"), ana)

    a = adjacent["nodes"]["trials_started"]["relative_change"]
    s = stale["nodes"]["trials_started"]["relative_change"]
    assert s > a, "a further-back reference must inflate the apparent lift, or the aside is wrong"
    assert a == pytest.approx(0.155, **TOUR)
    assert s == pytest.approx(0.216, **TOUR)


def test_arbitrary_slice_window_is_served_without_a_provider(client):
    """A prospect picking their own dates must not fall through to a provider.

    Sliced snapshots are stored at the full loaded window and trimmed on read,
    so a window nobody warmed at build time still answers."""
    s = slices(
        client, "churned_mrr", "plan", ("2026-02-16", "2026-03-15"), ("2026-05-18", "2026-06-14")
    )
    assert s["slices"][0]["value"] == "professional"
    assert s["reconciliation"]["status"] == "ok"


def whatif(client, interventions):
    r = client.post(
        "/simulate",
        json={
            "baseline_start": "2026-06-29",
            "baseline_end": "2026-07-26",
            "interventions": interventions,
            "assumptions": [],
            "levers": [],
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
    assert net["delta"]["estimate"] == pytest.approx(0.2 * churn["baseline"], rel=0.02)
    assert net["prob_direction"] > 0.9

    # "churned_mrr falls from $804/week to $643/week (−20.0%), and net_new_mrr
    # rises by exactly that amount — +$161/week, +9.7% — with P(direction) 1.0
    # and a zero-width 95% interval."
    assert churn["baseline"] == pytest.approx(804.21, rel=1e-3)
    # 643.37 = 804.21 x 0.8, exactly — which is the point. The propagation
    # multiplies the intervened rate through two identities, and it lands on
    # the arithmetic answer only because each rate's baseline is its
    # denominator-weighted window value (roadmap 1.11c); an averaged-ratio
    # baseline would miss the linear response by a few tenths of a percent.
    assert churn["simulated"] == pytest.approx(643.37, rel=1e-3)
    assert churn["simulated"] == pytest.approx(0.8 * churn["baseline"], rel=1e-9)
    assert churn["relative_delta"] == pytest.approx(-0.200, **TOUR)
    assert net["delta"]["estimate"] == pytest.approx(160.84, rel=1e-3)
    assert net["relative_delta"] == pytest.approx(0.097, **TOUR)
    assert net["prob_direction"] == 1.0
    # The zero width is the claim the tour defends out loud ("not false
    # confidence — the churn edge is an identity"), so it is asserted, not
    # merely implied by the point estimate.
    lo, hi = net["delta"]["ci_95"]
    assert hi - lo < 1e-6


def test_what_if_the_spend_lever_is_the_uncertain_one(client):
    """The tour's second scenario points at a contrast, not a dollar figure.

    `marketing_spend` reaches `net_new_mrr` through learned edges, so its
    interval is wide where the churn lever's is degenerate. That contrast is the
    whole point being made on the call, and it is what gets asserted — a point
    estimate here would be sampler-derived, and pinning one would either flake
    or be so loose it asserted nothing. The band is deliberately generous: the
    claim is "tens of dollars wide against zero", and anything inside it
    supports that sentence while a collapse to zero or a blow-up to hundreds
    does not."""
    spend = whatif(client, [{"metric": "marketing_spend", "mode": "pct", "value": 0.3}])
    churn = whatif(client, [{"metric": "customer_churn_rate", "mode": "pct", "value": -0.2}])

    net = spend["nodes"]["net_new_mrr"]
    assert net["status"] == "affected"
    lo, hi = net["delta"]["ci_95"]
    churn_lo, churn_hi = churn["nodes"]["net_new_mrr"]["delta"]["ci_95"]

    assert hi - lo > churn_hi - churn_lo
    assert 20 < hi - lo < 400
    assert churn_hi - churn_lo < 1e-6
    # ...and it still arrives through the funnel: the acquisition side moves,
    # the churn side does not.
    assert spend["nodes"]["new_mrr"]["status"] == "affected"
    assert spend["nodes"]["churned_mrr"]["status"] == "baseline"


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
    # The tour names all three metrics the flag lands on, so all three are here.
    assert {m for k, m in kinds if k == "non_physical"} == {
        "customer_churn_rate",
        "churned_subscriptions",
        "churned_mrr",
    }

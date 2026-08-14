"""The worked reference tree (`knowledge/b2b_mrr_tree.yml`) is what a new author
copies, so its schema coverage is a property worth pinning rather than a state
that happened to be true once (roadmap C10).

Nothing loaded this file before C10, which is how it drifted to *zero* `grain`,
`kind`, `dimensions` and `expected_signs` declarations while remaining the
documented answer to "how hard is this to author".

Skipped when the tree is absent. `knowledge/` is repo scaffolding — design docs
and the roadmap — and the sdist excludes it, so the published suite would
otherwise fail here on a file the artifact never contained.
"""

import logging
import os

import pytest

from breakdown.data_fetch import MockDataFetcher
from breakdown.grains import build_grained
from breakdown.parser import Parser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(REPO, "knowledge", "b2b_mrr_tree.yml")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(TREE),
    reason=(
        "knowledge/b2b_mrr_tree.yml not present — it is repo-only, not part of the "
        "distribution; read it at "
        "https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/b2b_mrr_tree.yml"
    ),
)


@pytest.fixture(scope="module")
def parser():
    with open(TREE) as fh:
        return Parser(fh.read())


@pytest.fixture(scope="module")
def defs(parser):
    return {n: parser.dag.nodes[n]["definition"] for n in parser.dag}


def test_parses_as_a_dag(parser):
    """Covers the grain rules too: `_validate_grains` runs inside `_build_dag`."""
    assert parser.dag.number_of_nodes() == 106
    assert parser.dag.number_of_edges() == 112


def test_every_rate_is_declared_not_inherited(defs, caplog):
    """The C10 defect itself: 46 ratio-shaped metrics inheriting `kind: flow`.

    Asserted through the parse-time lint rather than a hand-maintained name
    list, so the tree and the lint can only drift apart in one direction.
    """
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        with open(TREE) as fh:
            Parser(fh.read())
    # Everything except the standing denominator inventory below, which is a
    # different lint about a different field and has its own test.
    assert [r.message for r in caplog.records if "denominator" not in r.message] == []


def test_the_rates_without_a_denominator_are_the_eight_that_cannot_have_one(parser, caplog):
    """Roadmap 1.11's "report the list", asserted rather than logged and lost.

    A rate with no `denominator` aggregates over a window as the plain average
    of its per-period ratios — the pre-1.11 number, which is wrong whenever the
    denominators differ. 40 of the 46 rates here declared none when 1.11b
    landed, so it warns rather than refuses.

    **The list is pinned by name, not by count, because it stopped being a
    backlog** — and it has now moved from the lint to the *answers*. 38 rates
    declare a denominator read off the tree's own arithmetic (`count = base *
    rate` makes `base` the denominator). The eight below are the ones where no
    denominator can be established: four mean durations whose cohort this tree
    does not carry, three ratios over a base that is not a metric here, and one
    median, which is not `Σnum / Σden` for any pair of series at all. Each
    states its reason in `no_denominator:` — in the field, where the payload
    and `doctor` can read it, rather than in a comment where only a person can.

    So a name appearing here is a claim about the metric, not a to-do. Adding
    one means someone decided a rate has no denominator — worth reading the
    argument for. Removing one means someone established one — worth reading
    the evidence for. A name appearing in `rates_denominator_unanswered`
    instead means the tree grew a rate nobody has said anything about, and that
    is the one that should fail: this tree answers for every rate it has.
    """
    rates = [
        n
        for n, d in ((n, parser.dag.nodes[n]["definition"]) for n in parser.dag)
        if d.kind == "rate"
    ]
    assert len(rates) == 46
    assert parser.rates_denominator_unanswered == [], (
        "every rate in this tree either declares a denominator or declares "
        "`no_denominator` with the reason. A name here is a new rate that has "
        "not been asked the question."
    )
    assert sorted(parser.rates_denominator_none) == [
        # Mean durations: the cohort averaged over is not a metric in the tree.
        "new_business_opportunity_sales_cycle",  # opportunities closed
        # A median — not Σnum / Σden for any pair of series.
        "page_speed",
        # Ratios whose base is not a metric in the tree.
        "prospect_coverage",  # the target prospect list
        "time_on_site",  # sessions
        "time_to_lead_response",  # leads responded to
        "time_to_sent_new_business_proposal",  # proposals sent
        "time_to_trial_activation",  # trials that activated
        "website_bounce_rate",  # sessions
    ], (
        "the reference tree's rate inventory moved. These eight are deliberate "
        "— a denominator that cannot be established from the tree, its "
        "descriptions or its formulas. See the DENOMINATORS block in the YAML "
        "header and roadmap 1.11 before changing this list."
    )
    assert set(parser.rates_denominator_none) <= set(rates)
    # Each reason is real prose, not a placeholder: the field exists so the
    # argument survives to the next reader, and `"n/a"` would defeat that while
    # passing every check above.
    for name, why in parser.rates_denominator_none.items():
        assert len(why.split()) >= 8, f"{name}'s `no_denominator` reason is too thin: {why!r}"
    # And nothing warns any more, because nothing is outstanding. The warning
    # naming these eight was, for `page_speed`, advice that could not be
    # followed. Re-parsed here because the module-scoped `parser` fixture warned
    # long before this test's caplog started listening.
    with caplog.at_level(logging.WARNING, logger="breakdown.parser"):
        with open(TREE) as fh:
            Parser(fh.read())
    assert not [r for r in caplog.records if "declare no `denominator`" in r.getMessage()]


def test_stocks_are_declared_as_stocks(defs):
    """`total_mrr` and `total_customers` were described as stocks in their own
    descriptions while declared as flows — they would have been SUMMED."""
    for name in (
        "total_mrr",
        "total_arr",
        "total_customers",
        "total_email_subscribers",
        "retained_mrr",
        "cumulative_churned_customers",
    ):
        assert defs[name].kind == "stock", name


def test_no_week_grain_anywhere(defs):
    """Every branch converges on `total_mrr`, and weeks do not nest in months,
    so a single weekly node would make the tree unparseable."""
    assert {d.grain for d in defs.values()} == {"day", "month"}


def test_every_learned_edge_declares_a_sign(defs):
    learned = {n: d.parents for n, d in defs.items() if d.parents and d.formula is None}
    assert len(learned) == 5
    assert sum(len(p) for p in learned.values()) == 12
    for name, parents in learned.items():
        assert set(defs[name].expected_signs) == set(parents), name


def test_no_level_drives_a_rate_on_a_learned_edge(defs):
    """The scale-confound shape README's `expected_signs` section warns about.

    `sdr_talk_time` is the one deliberate exception, documented in the tree.
    """
    offenders = [
        (n, p)
        for n, d in defs.items()
        if d.formula is None and d.parents and d.kind == "rate"
        for p in d.parents
        if defs[p].kind != "rate"
    ]
    assert offenders == [("sales_qualification_rate", "sdr_talk_time")]


def test_trial_conversion_collinearity_is_gone(defs):
    """`product_qualified_leads` is *defined* as `true_trials *
    trial_to_pql_rate`, so regressing on it alongside `trial_to_pql_rate` left
    the split of credit unidentified (roadmap S4/S17)."""
    assert defs["product_qualified_leads"].formula is not None
    assert "product_qualified_leads" not in defs["true_trial_conversion_rate"].parents


def test_email_subscriber_base_is_a_level_not_a_net_add(defs):
    """It was `new_email_subscribers - new_email_unsubscribes` — the period's
    net add — and was then multiplied by send frequency, a quantity that goes
    negative on any day with net unsubscribes."""
    subs = defs["total_email_subscribers"]
    assert subs.formula is None and subs.kind == "stock"
    assert "new_email_unsubscribes" not in defs


def test_slicing_workflow_is_reachable(defs):
    """Zero `dimensions` meant `POST /rca/{name}/slices` could not run at all."""
    sliceable = [n for n, d in defs.items() if d.dimensions]
    assert len(sliceable) >= 20
    assert {"total_mrr", "churned_mrr", "new_customers"} <= set(sliceable)


def test_rate_dimension_weights_share_the_rate_grain(parser, defs):
    """Enforced only at slice time (`api/main.py`), so a tree can parse clean
    and fail on the first slice request — pin it here until that check moves
    to parse time (roadmap C12)."""
    for name, d in defs.items():
        for dim, spec in d.dimensions.items():
            if d.kind == "rate":
                assert spec.weight is not None, (name, dim)
                assert defs[spec.weight].grain == d.grain, (name, dim)


def test_seasonality_periods_are_in_the_node_s_own_grain(defs):
    """`period: 7` on a monthly node means seven *months*. The weekly
    components belong on the daily traffic roots."""
    for name, d in defs.items():
        for s in d.seasonality:
            assert d.grain == "day" and s.period == 7, name


def test_lags_are_in_the_node_s_own_grain(defs):
    """Same trap as seasonality: the enterprise funnel's 3-day lead-response
    lag would have become three *months* when that branch moved to month."""
    assert defs["true_trial_conversion_rate"].lags == {"time_to_trial_activation": 7}
    assert defs["sales_qualification_rate"].lags == {}


def test_mixed_grain_data_builds_and_resamples(parser, defs):
    """The day -> month handoffs are the point of the tree's grain cut, so
    prove they actually align rather than merely parse."""
    fetcher = MockDataFetcher(dag=parser.dag)
    per_metric, grain_of, kind_of = {}, {}, {}
    for name, d in defs.items():
        per_metric[name] = fetcher.fetch_metric(
            name, "2022-01-01", "2023-12-31", grain=d.grain, kind=d.kind
        )
        grain_of[name], kind_of[name] = d.grain, d.kind
    data = build_grained(per_metric, grain_of, kind_of)

    assert len(data.frame("month")) == 24
    assert len(data.frame("day")) == 730

    # The day -> month handoff: `converted_trials` is a daily flow, so the month
    # `new_customers` is fitted at sees its within-month sum.
    frame = data.fit_frame("new_customers", ["converted_trials", "new_business_deals"], "month")
    assert len(frame) == 24
    daily = data.series("converted_trials")
    jan = daily[daily["date"] < "2022-02-01"]["converted_trials"].sum()
    assert frame.iloc[0]["converted_trials"] == pytest.approx(jan)

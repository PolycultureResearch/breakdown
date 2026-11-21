"""The evaluation runner (`knowledge/benchmarks/evalrunner`, evaluation spec
§10 step 2): reading a world set, loading a world as a tree's data, choosing
windows, mapping a planted root to the tree, and scoreboard arithmetic.

Everything but the last test runs without a sampler.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from tests.eval_worlds_fixture import (
    ARITHMETIC_TREE,
    E2E_ONSET,
    E2E_TREE,
    TICKET_COUNT,
    TICKET_SUM,
    arithmetic_world_set,
    e2e_world_set,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "knowledge" / "benchmarks"))

from evalrunner.attribution import eligibility, path_edges, score_items  # noqa: E402
from evalrunner.loader import (  # noqa: E402
    SeriesBinding,
    TreePlan,
    TreeSpec,
    UnservedNodes,
    WorldCannotAnswer,
    WorldFetcher,
    WorldSeries,
    load_world,
    plan_tree,
)
from evalrunner.runner import (  # noqa: E402
    RunRefused,
    plan_run,
    run_shard,
    select_seeds,
)
from evalrunner.scoreboard import (  # noqa: E402
    build_scoreboard,
    diff_scoreboards,
    empirical_tail,
    lag1_correlation,
    render_diff,
    render_markdown,
    summarize,
)
from evalrunner.truth import (  # noqa: E402
    Measured,
    analysis_window,
    map_root,
    onset_of,
    pick_target,
)
from evalrunner.windows import horizons_for, window_starts  # noqa: E402
from evalrunner.worldset import WorldSetError, load_world_set  # noqa: E402

from breakdown.parser import Parser  # noqa: E402

AB = {"visits_ab": SeriesBinding("visits", "channel", ("a", "b"))}


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------
def test_manifest_is_read_without_the_generator(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    assert ws.vertical == "fixture_vertical"
    assert ws.metrics["churn"].inputs == {"numerator": "churned", "denominator": "base"}
    assert ws.metrics["churn"].input_time_aggregation == {
        "numerator": "sum",
        "denominator": "end_of_period",
    }
    assert ws.metrics["net"].formula["op"] == "-"
    assert ws.metrics["ticket"].has_sides and not ws.metrics["orders"].has_sides
    assert ws.metrics["base"].measure_agg == "count_distinct"
    (world,) = ws.worlds
    assert world.kind == "null" and world.series.exists()
    assert ws.ground_truth(world) == []


def test_the_sets_verdicts_and_a_twins_slice_stats_are_read_from_their_files(tmp_path):
    ws = load_world_set(e2e_world_set(tmp_path))
    verdicts = ws.verdicts("baseline", "rate_drop")
    assert verdicts[("signups", None, None)] == "established_down"
    assert verdicts[("spend", None, None)] == "exactly_zero"
    assert ws.verdicts("baseline", "no_such_anomaly") == {}
    effects = ws.set_effects("baseline", "rate_drop")
    signups = effects[("signups", None, None)]
    assert (signups["rel_mean"], signups["rel_sd"], signups["n_seeds"]) == (-0.4, None, 1)
    single = ws.world("fixture.baseline.s0.only.rate_drop")
    assert single.effects_summary.exists()


def _set_version(root, version):
    raw = json.loads((root / "worlds.json").read_text())
    raw["schema_version"] = version
    (root / "worlds.json").write_text(json.dumps(raw))


def test_unknown_schema_version_is_refused_by_number(tmp_path):
    root = arithmetic_world_set(tmp_path)
    _set_version(root, 3)
    with pytest.raises(WorldSetError, match="schema_version 3"):
        load_world_set(root)


def test_a_version_1_set_is_refused_up_front_unless_asked_for_by_name(tmp_path):
    root = arithmetic_world_set(tmp_path)
    _set_version(root, 1)
    with pytest.raises(WorldSetError, match="schema_version 1.*needs schema_version 2"):
        load_world_set(root)
    assert load_world_set(root, allow_v1=True).schema_version == 1


def test_a_verdict_column_the_set_does_not_declare_is_refused(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    ws.check_verdict_column("verdict_10pct")
    with pytest.raises(WorldSetError, match="verdict_5pct"):
        ws.check_verdict_column("verdict_5pct")


def test_a_missing_key_is_refused_by_name(tmp_path):
    root = arithmetic_world_set(tmp_path)
    raw = json.loads((root / "worlds.json").read_text())
    del raw["metrics"][0]["time_aggregation"]
    (root / "worlds.json").write_text(json.dumps(raw))
    with pytest.raises(WorldSetError, match="time_aggregation"):
        load_world_set(root)


# ---------------------------------------------------------------------------
# The loader: day -> week, by how each metric aggregates
# ---------------------------------------------------------------------------
@pytest.fixture
def arithmetic(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    return ws, WorldSeries(ws.worlds[0], ws.metrics)


def test_a_sum_is_added_up_over_each_whole_week(arithmetic):
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("orders"), "week")
    assert list(weekly.index) == list(pd.date_range("2024-01-01", periods=4, freq="7D"))
    assert weekly.tolist() == [28.0, 77.0, 126.0, 175.0]  # 1..7, 8..14, ...


def test_a_stock_takes_the_last_day_of_the_week(arithmetic):
    _, series = arithmetic
    assert series.at_grain(SeriesBinding("base"), "week").tolist() == [106.0, 113.0, 120.0, 127.0]


def test_a_ratio_is_the_ratio_of_its_summed_sides_not_a_mean_of_daily_ratios(arithmetic):
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("conversion"), "week")
    assert weekly.tolist() == pytest.approx([42 / 420] * 4)


def test_a_ratio_over_a_stock_divides_by_the_stocks_last_day(arithmetic):
    """`churn = churned / base`: the week's churned over the base at the
    week's end, which is what makes `churned = base * churn` exact weekly."""
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("churn"), "week")
    assert weekly.tolist() == pytest.approx([14 / 106, 14 / 113, 14 / 120, 14 / 127])


def _edit_metric(tmp_path, name, **changes):
    root = arithmetic_world_set(tmp_path)
    raw = json.loads((root / "worlds.json").read_text())
    (m,) = [m for m in raw["metrics"] if m["name"] == name]
    m.update(changes)
    (root / "worlds.json").write_text(json.dumps(raw))
    ws = load_world_set(root)
    return WorldSeries(ws.worlds[0], ws.metrics)


def test_a_ratio_whose_sides_rules_are_not_stated_is_refused(tmp_path):
    """A set from before the generator stated each side's rule: summing a
    stock over a week is the factor-of-seven error, so nothing is assumed."""
    series = _edit_metric(tmp_path, "churn", input_time_aggregation={})
    with pytest.raises(WorldCannotAnswer, match="'churn'.*input_time_aggregation"):
        series.at_grain(SeriesBinding("churn"), "week")
    assert len(series.at_grain(SeriesBinding("churn"), "day")) == 28


def test_a_ratio_of_sums_over_a_stock_is_refused_by_the_stocks_own_rule(tmp_path):
    """How sets built before 83640ca described the churn rate: `ratio_of_sums`
    with no sides stated. The denominator's own entry says it is a stock."""
    series = _edit_metric(
        tmp_path, "churn", time_aggregation="ratio_of_sums", input_time_aggregation={}
    )
    with pytest.raises(WorldCannotAnswer, match="'base' aggregates as 'sum'.*'end_of_period'"):
        series.at_grain(SeriesBinding("churn"), "week")


def test_a_ratio_of_sums_with_a_side_that_is_not_a_sum_is_refused(tmp_path):
    series = _edit_metric(
        tmp_path,
        "churn",
        time_aggregation="ratio_of_sums",
        input_time_aggregation={"numerator": "sum", "denominator": "end_of_period"},
    )
    with pytest.raises(WorldCannotAnswer, match="ratio_of_sums.*end_of_period"):
        series.at_grain(SeriesBinding("churn"), "week")


def test_a_derived_metric_is_recomputed_from_its_aggregated_inputs(arithmetic):
    _, series = arithmetic
    assert series.at_grain(SeriesBinding("net"), "week").tolist() == [21.0, 70.0, 119.0, 168.0]


def test_a_derived_metric_without_a_structured_formula_is_refused(tmp_path):
    """The SQL `expr` is not parsed: the manifest's tree is the contract."""
    series = _edit_metric(tmp_path, "net", formula=None)
    with pytest.raises(WorldCannotAnswer, match="'net'.*structured `formula`"):
        series.at_grain(SeriesBinding("net"), "week")


def test_a_derived_input_whose_stated_rule_contradicts_its_own_is_refused(tmp_path):
    series = _edit_metric(
        tmp_path, "net", input_time_aggregation={"o": "end_of_period", "r": "sum"}
    )
    with pytest.raises(WorldCannotAnswer, match="'orders'.*'end_of_period'.*'sum'"):
        series.at_grain(SeriesBinding("net"), "week")


def test_a_formula_divisor_of_zero_leaves_the_period_undefined(tmp_path):
    series = _edit_metric(
        tmp_path,
        "net",
        formula={
            "op": "/",
            "args": [{"metric": "orders"}, {"op": "nullif_zero", "args": [{"const": 0}]}],
        },
    )
    assert series.at_grain(SeriesBinding("net"), "week").empty


def test_an_average_is_rebuilt_weekly_from_its_sum_and_row_count(arithmetic):
    """Not the mean of the daily means: a day with more rows weighs more."""
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("ticket"), "week")
    expected = [TICKET_SUM[w : w + 7].sum() / TICKET_COUNT[w : w + 7].sum() for w in (0, 7, 14, 21)]
    daily_means = (TICKET_SUM / TICKET_COUNT)[:7].mean()
    assert weekly.tolist() == pytest.approx(expected)
    assert weekly.iloc[0] != pytest.approx(daily_means)


def test_slice_members_are_summed(arithmetic):
    _, series = arithmetic
    daily = series.at_grain(AB["visits_ab"], "day")
    assert daily.tolist() == [30.0] * 28
    assert series.at_grain(AB["visits_ab"], "week").tolist() == [210.0] * 4


def test_a_running_total_is_a_stock(arithmetic):
    _, series = arithmetic
    binding = SeriesBinding("orders", cumulative=True)
    assert series.at_grain(binding, "day").iloc[:3].tolist() == [1.0, 3.0, 6.0]
    assert series.at_grain(binding, "week").iloc[0] == 28.0


@pytest.mark.parametrize(
    "binding, grain, names",
    [
        (SeriesBinding("revenue"), "day", ["'revenue'", "not in the series file"]),
        (SeriesBinding("orders", "channel", ("a",)), "day", ["'orders'", "'channel'"]),
        (SeriesBinding("visits", "channel", ("a", "z")), "day", ["channel='z'"]),
        (SeriesBinding("users"), "week", ["users", "'count_distinct'", "week"]),
    ],
)
def test_what_the_file_cannot_answer_is_refused_by_name(arithmetic, binding, grain, names):
    _, series = arithmetic
    with pytest.raises(WorldCannotAnswer) as exc:
        series.at_grain(binding, grain)
    for name in names:
        assert name in str(exc.value)


def test_a_distinct_count_is_served_at_day_grain_as_written(arithmetic):
    _, series = arithmetic
    assert series.at_grain(SeriesBinding("users"), "day").tolist() == [40.0] * 28


def test_a_distinct_count_with_week_values_is_served_from_the_weekly_file(arithmetic):
    """MetricFlow's week value of a distinct count (distinct over the week),
    whole weeks only: the partial fifth week is dropped."""
    _, series = arithmetic
    assert series.at_grain(SeriesBinding("members"), "week").tolist() == [60, 70, 80, 90]
    assert series.at_grain(SeriesBinding("members"), "day").iloc[0] == 50


def test_a_ratio_over_a_weekly_distinct_count_divides_by_its_week_value(arithmetic):
    """As MetricFlow aggregates each input at the grain asked for, which keeps
    `lapsed = members x lapse` exact against the weekly member count."""
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("lapse"), "week")
    assert weekly.tolist() == pytest.approx([14 / 60, 14 / 70, 14 / 80, 14 / 90])


def test_a_cumulative_metric_is_served_weekly_as_its_first_day_like_metricflow(arithmetic):
    _, series = arithmetic
    weekly = series.at_grain(SeriesBinding("signups_to_date"), "week")
    assert weekly.tolist() == [6.0, 48.0, 90.0, 132.0]  # days 1, 8, 15, 22


def test_a_week_node_is_served_as_breakdowns_providers_return_it(tmp_path):
    """Every provider asks its source at the node's grain, so a week node gets
    MetricFlow's week number where the set states it: a stock whose measure
    sums is seven days of its level added up, not the business number."""
    from evalrunner.loader import caveat_for

    series = _edit_metric(
        tmp_path, "base", metricflow_week="sum_of_days", matches_metricflow_week=False
    )
    assert series.at_grain(SeriesBinding("base"), "week").iloc[0] == sum(range(100, 107))
    assert series.at_grain(SeriesBinding("base"), "day").iloc[0] == 100  # days unchanged
    note = caveat_for(series.specs["base"], SeriesBinding("base"), "week")
    assert "sum_of_days" in note and "seven days" in note
    with pytest.raises(WorldCannotAnswer, match="week grain only"):
        series.at_grain(SeriesBinding("base"), "month")


def test_a_ratio_is_served_from_its_inputs_week_rules(tmp_path):
    series = _edit_metric(
        tmp_path,
        "lapse",
        metricflow_week="ratio_of_week_inputs",
        matches_metricflow_week=False,
        input_metricflow_week={"numerator": "sum_of_days", "denominator": "distinct_over_week"},
    )
    weekly = series.at_grain(SeriesBinding("lapse"), "week")
    assert weekly.tolist() == pytest.approx([14 / 60, 14 / 70, 14 / 80, 14 / 90])
    series = _edit_metric(
        tmp_path / "b",
        "lapse",
        metricflow_week="ratio_of_week_inputs",
        input_metricflow_week={"numerator": "sum_of_days", "denominator": "first_day"},
    )
    with pytest.raises(WorldCannotAnswer, match="denominator.*'first_day'"):
        series.at_grain(SeriesBinding("lapse"), "week")


def test_the_plan_names_where_a_week_value_departs_from_the_manifests_rule(arithmetic):
    from evalrunner.loader import caveat_for

    ws, _ = arithmetic
    first_day = caveat_for(ws.metrics["signups_to_date"], SeriesBinding("signups_to_date"), "week")
    assert "first day" in first_day and "MetricFlow" in first_day
    assert caveat_for(ws.metrics["members"], SeriesBinding("members"), "week") is None
    assert (
        caveat_for(ws.metrics["signups_to_date"], SeriesBinding("signups_to_date"), "day") is None
    )


def test_a_distinct_count_stock_is_served_weekly_with_a_caveat_on_the_plan(tmp_path):
    """Its last day's count is the stock's level, and not MetricFlow's weekly
    distinct count: served, and the plan says which number it is."""
    ws = load_world_set(arithmetic_world_set(tmp_path))
    tree = TreeSpec("fixture_tree.yml", ARITHMETIC_TREE, bindings=AB)
    plan = plan_tree(tree, WorldSeries(ws.worlds[0], ws.metrics))
    assert set(plan.caveats) == {"base"}  # `base` has no weekly series
    assert "last day's count" in plan.summary()["caveats"]["base"]


def test_the_fetcher_refuses_a_node_whose_kind_contradicts_the_generator(arithmetic):
    _, series = arithmetic
    fetcher = WorldFetcher(series, {"base": SeriesBinding("base")})
    with pytest.raises(WorldCannotAnswer, match="kind 'flow'.*end_of_period"):
        fetcher.fetch_metric("base", "2024-01-01", "2024-01-28", grain="week", kind="flow")
    with pytest.raises(WorldCannotAnswer, match="no binding"):
        fetcher.fetch_metric("nope", "2024-01-01", "2024-01-28")


def test_a_world_loads_as_the_trees_aligned_data(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    tree = TreeSpec("fixture_tree.yml", ARITHMETIC_TREE, bindings=AB)
    loaded = load_world(ws, ws.worlds[0], tree)
    week = loaded.data.frames["week"].set_index("date")
    assert week["orders"].tolist() == [28.0, 77.0, 126.0, 175.0]
    assert week["base"].tolist() == [106.0, 113.0, 120.0, 127.0]
    assert week["net"].tolist() == [21.0, 70.0, 119.0, 168.0]
    assert loaded.data.frames["day"]["visits_ab"].tolist() == [30.0] * 28
    assert loaded.plan.served["visits_ab"].describe() == "visits [channel in a, b]"


def test_every_unserved_node_is_reported_and_the_tree_is_not_loaded(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    raw = yaml.safe_load(ARITHMETIC_TREE)
    raw["metrics"] += [
        {
            "name": "users",
            "source": "fx.metrics.users",
            "grain": "week",
            "kind": "rate",
            "no_denominator": "fixture",
        },
        {"name": "ghost", "source": "fx.metrics.ghost", "parents": ["visits_ab"]},
    ]
    tree = TreeSpec("fixture_tree.yml", yaml.safe_dump(raw), bindings=AB)
    series = WorldSeries(ws.worlds[0], ws.metrics)
    with pytest.raises(UnservedNodes) as exc:
        plan_tree(tree, series)
    assert set(exc.value.unserved) == {"users", "ghost"}
    plan = plan_tree(tree, series, drop_unserved=True)
    assert sorted(plan.dropped) == ["ghost", "users"]
    assert "ghost" not in [m["name"] for m in plan.raw["metrics"]]
    Parser(yaml.safe_dump(plan.raw))  # the tree without them still parses


def test_a_node_a_formula_needs_cannot_be_dropped(tmp_path):
    ws = load_world_set(arithmetic_world_set(tmp_path))
    raw = yaml.safe_load(ARITHMETIC_TREE)
    raw["metrics"] += [
        {"name": "ghost", "source": "fx.metrics.ghost", "grain": "week"},
        {
            "name": "total",
            "grain": "week",
            "formula": "orders + ghost",
            "parents": ["orders", "ghost"],
        },
    ]
    tree = TreeSpec("fixture_tree.yml", yaml.safe_dump(raw), bindings=AB)
    with pytest.raises(UnservedNodes, match="formula of 'total' needs it"):
        plan_tree(tree, WorldSeries(ws.worlds[0], ws.metrics), drop_unserved=True)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def test_window_starts_are_mondays_evenly_spaced_and_deterministic():
    kw = dict(horizons=(1, 7, 28), spacing_days=28)
    starts, spacing = window_starts("2024-06-01", "2026-05-31", **kw)
    assert spacing == 28
    assert starts == window_starts("2024-06-01", "2026-05-31", **kw)[0]
    assert all(s.dayofweek == 0 for s in starts)
    assert starts[0] == pd.Timestamp("2024-12-02")  # 182 days of history, then the next Monday
    assert set(np.diff(starts)) == {pd.Timedelta(days=28)}


@pytest.mark.parametrize("spacing", [7, 14, 28])
@pytest.mark.parametrize("horizon", [1, 7, 28])
def test_windows_at_one_horizon_never_overlap(spacing, horizon):
    starts, _ = window_starts("2024-06-01", "2026-05-31", horizons=(horizon,), spacing_days=spacing)
    scored = [
        s
        for i, s in enumerate(starts)
        if horizons_for(i, s, "day", (horizon,), spacing, "2026-05-31")
    ]
    assert len(scored) > 3
    ends = [s + pd.Timedelta(days=horizon - 1) for s in scored]
    assert all(end < nxt for end, nxt in zip(ends, scored[1:]))
    assert ends[-1] <= pd.Timestamp("2026-05-31")


def test_max_windows_picks_the_smallest_whole_week_spacing_that_fits():
    starts, spacing = window_starts("2024-06-01", "2026-05-31", horizons=(7,), max_windows=8)
    assert len(starts) <= 8 and spacing % 7 == 0
    more, tighter = window_starts(
        "2024-06-01", "2026-05-31", horizons=(7,), spacing_days=spacing - 7
    )
    assert len(more) > 8 and tighter == spacing - 7


def test_a_week_grain_node_has_no_one_day_window():
    assert horizons_for(0, "2025-01-06", "week", (1, 7, 28), 28, "2026-05-31") == [7, 28]
    assert horizons_for(0, "2025-01-06", "day", (1, 7, 28), 28, "2026-05-31") == [1, 7, 28]
    assert horizons_for(0, "2025-01-06", "month", (7, 28), 28, "2026-05-31") == []


# ---------------------------------------------------------------------------
# Root -> node
# ---------------------------------------------------------------------------
MAPPING_TREE = """
provider: {type: mock}
metrics:
  - {name: paid_sessions, source: x.m.paid_sessions}
  - {name: other_sessions, source: x.m.other_sessions}
  - {name: sessions, source: x.m.sessions, formula: "paid_sessions + other_sessions",
     parents: [paid_sessions, other_sessions]}
  - {name: base, source: x.m.base, kind: stock}
  - {name: churn_rate, source: x.m.churn_rate, kind: rate, denominator: base}
  - {name: churned, source: x.m.churned, formula: "base * churn_rate",
     parents: [base, churn_rate]}
  - {name: revenue, source: x.m.revenue, parents: [sessions, churned]}
"""


@pytest.fixture
def mapping():
    dag = Parser(MAPPING_TREE).dag
    served = {n: SeriesBinding(n) for n in dag.nodes}
    served["paid_sessions"] = SeriesBinding("sessions", "channel", ("paid_search", "paid_social"))
    served["other_sessions"] = SeriesBinding("sessions", "channel", ("organic",))
    return dag, TreePlan(served=served, unserved={})


def record(root_metric, root_slice=None, moved=(), **extra):
    effects = {m: {"moved": True, "onset": "2025-02-10", "first_diff": "2025-02-03"} for m in moved}
    return {
        "id": "a",
        "kind": "rate",
        "start": "2025-02-03",
        "end": "2025-02-23",
        "root_metric": root_metric,
        "root_slice": root_slice or {},
        "measured_effects": effects,
        **extra,
    }


def test_a_root_slice_maps_to_the_node_bound_to_that_slice(mapping):
    dag, plan = mapping
    m = map_root(dag, plan, record("sessions", {"channel": "paid_social"}))
    assert (m.status, m.root_node, m.truth) == ("on_tree_slice", "paid_sessions", ["paid_sessions"])


def test_a_root_metric_maps_to_its_node(mapping):
    dag, plan = mapping
    m = map_root(dag, plan, record("revenue"))
    assert (m.status, m.truth) == ("on_tree", ["revenue"])


def test_a_root_that_is_not_a_node_is_off_tree_and_not_forced(mapping):
    dag, plan = mapping
    m = map_root(dag, plan, record("page_load_time"))
    assert m.status == "off_tree" and m.truth == [] and not m.on_tree
    assert "page_load_time" in m.note


def test_a_dropped_root_is_off_tree_and_says_why(mapping):
    dag, plan = mapping
    plan.dropped.append("discounted_orders")
    m = map_root(dag, plan, record("discounted_orders"))
    assert m.status == "off_tree" and "dropped" in m.note


def test_a_root_on_a_formula_node_is_scored_at_the_parent_that_carries_the_change(mapping):
    """The generator roots a churn-rate spike at the churned count; in the
    tree that count is an identity, and the rate is what changed. The base
    moves too (churned customers leave it), by far less."""
    dag, plan = mapping
    rec = record("churned", moved=["churned", "churn_rate", "base"])
    rec["measured_effects"]["churn_rate"]["rel_change"] = 0.36
    rec["measured_effects"]["base"]["rel_change"] = -0.017
    m = map_root(dag, plan, rec)
    assert (m.status, m.root_node, m.truth) == ("via_formula_parents", "churned", ["churn_rate"])


def test_a_root_on_a_rates_numerator_is_scored_at_the_rate_the_record_names(mapping):
    """`root_metric_rate` says which rate the count is the numerator of; no
    guess from which parent moved more."""
    dag, plan = mapping
    rec = record("churned", {"plan": "pro"}, moved=["churned"], root_metric_rate="churn_rate")
    m = map_root(dag, plan, rec)
    assert (m.status, m.root_node, m.truth) == ("on_tree_rate", "churn_rate", ["churn_rate"])
    # A tree without the rate falls back to the count's own node.
    rec["root_metric_rate"] = "not_in_tree"
    assert map_root(dag, plan, rec).root_node == "churned"


def test_the_sets_verdict_decides_what_moved_over_this_twins(mapping):
    """One verdict per anomaly and metric across seeds, so every seed gets
    the same target; a node the set has no row for falls back to the twin."""
    dag, plan = mapping
    rec = record("churned", moved=["churn_rate", "churned", "revenue"])
    verdicts = {
        ("revenue", None, None): "open",
        ("churned", None, None): "established_up",
    }
    measured = Measured(rec, verdicts=verdicts)
    assert pick_target(dag, plan, rec, ["churn_rate"], measured) == ("churned", "set:verdict")
    loose = Measured(rec, verdicts=verdicts, verdict_column="verdict_10pct")
    assert pick_target(dag, plan, rec, ["churn_rate"], loose)[1] == "set:verdict_10pct"
    assert pick_target(dag, plan, rec, ["churn_rate"]) == ("revenue", "twin_moved")
    # No set row for `churned` either: this twin's `moved` decides.
    measured = Measured(rec, verdicts={("revenue", None, None): "exactly_zero"})
    assert pick_target(dag, plan, rec, ["churn_rate"], measured) == ("churned", "twin_moved")


def test_a_node_bound_to_one_slice_reads_its_own_slices_stats(mapping):
    dag, plan = mapping
    plan.served["paid_sessions"] = SeriesBinding("sessions", "channel", ("paid_social",))
    rec = record("sessions", {"channel": "paid_social"})
    twin = {("sessions", "channel", "paid_social"): {"first_diff": "2025-02-05", "moved": True}}
    measured = Measured(rec, twin=twin)
    assert onset_of("paid_sessions", plan, rec, is_root=False, measured=measured) == (
        pd.Timestamp("2025-02-05"),
        "first_diff:node",
    )
    assert measured.moved("paid_sessions", plan) == (True, "twin_moved")
    assert measured.moved("other_sessions", plan) == (False, "twin_moved")


def test_a_formula_root_with_no_moved_parent_keeps_its_own_label(mapping):
    dag, plan = mapping
    m = map_root(dag, plan, record("churned", moved=["churned"]))
    assert m.truth == ["churned"] and "no parent" in m.note


def test_onset_is_the_exact_first_difference_then_the_noise_floor_then_the_declared_start(mapping):
    _, plan = mapping
    rec = record("revenue", moved=["revenue"])
    # The twins were identical until the anomaly's own start: that day is exact,
    # and a ramp's noise-floor onset a week later is not when it arrived.
    assert onset_of("revenue", plan, rec, is_root=True) == (
        pd.Timestamp("2025-02-03"),
        "first_diff:node",
    )
    # A first difference before the declared start is re-dealt draws, not arrival.
    rec["measured_effects"]["revenue"]["first_diff"] = "2024-06-01"
    assert onset_of("revenue", plan, rec, is_root=True) == (
        pd.Timestamp("2025-02-10"),
        "onset:node",
    )
    rec["measured_effects"]["revenue"]["onset"] = None
    assert onset_of("revenue", plan, rec, is_root=True) == (
        pd.Timestamp("2025-02-03"),
        "declared_start",
    )
    # The root's own slice is consulted for the root node only.
    rec["root_effect"] = {"onset": "2025-02-12", "first_diff": "2024-06-01"}
    assert onset_of("revenue", plan, rec, is_root=True)[1] == "onset:root_slice"
    assert onset_of("revenue", plan, rec, is_root=False)[1] == "declared_start"


def test_the_target_is_the_deepest_node_the_anomaly_is_measured_to_reach(mapping):
    dag, plan = mapping
    rec = record("churned", moved=["churn_rate", "churned"])
    assert pick_target(dag, plan, rec, ["churn_rate"]) == ("churned", "twin_moved")
    rec = record("churned", moved=["churn_rate", "churned", "revenue"])
    assert pick_target(dag, plan, rec, ["churn_rate"]) == ("revenue", "twin_moved")
    assert pick_target(dag, plan, record("x"), ["churn_rate"])[1] == "fallback_deepest_descendant"
    assert pick_target(dag, plan, rec, ["revenue"])[0] is None


def test_the_analysis_window_is_whole_periods_from_the_onsets_period():
    rec = {"end": "2025-02-23"}
    assert analysis_window("2025-02-05", rec, "week") == ("2025-02-03", "2025-02-23")
    assert analysis_window("2025-02-05", rec, "day") == ("2025-02-05", "2025-02-23")
    assert analysis_window("2025-02-05", {"end": "2025-12-31"}, "day")[1] == "2025-03-04"
    assert analysis_window("2025-02-05", {"end": "2025-02-06"}, "week") == (
        "2025-02-03",
        "2025-02-09",
    )


# ---------------------------------------------------------------------------
# Attribution (§5.4)
# ---------------------------------------------------------------------------
def test_attribution_scores_the_edges_on_the_path_from_root_to_target(mapping):
    dag, _ = mapping
    assert path_edges(dag, ["churn_rate"], "revenue") == [
        ("churn_rate", "churned"),
        ("churned", "revenue"),
    ]
    assert path_edges(dag, ["revenue"], "revenue") == []


SET = {"set_rel_mean": 0.10, "set_rel_sd": 0.01, "set_n_seeds": 10}  # ±7% at ten seeds


def _item(world, seed, measured, null=100.0, estimate=None, ci=None, **extra):
    """One world's row of one item. The set's cross-seed effect rides on it."""
    return {
        "world": world,
        "seed": seed,
        "measured_effect": measured,
        "world_value": null + measured,
        "null_value": null,
        "estimate": estimate,
        "ci_95": ci,
        "verdict": "established_up",
        "aligned": False,
        "withheld": None,
        **SET,
        **extra,
    }


def test_a_size_is_scored_only_when_established_and_precise_at_the_sets_seed_count():
    rows = [_item(f"w{s}", s, 10.0) for s in range(3)]
    v = eligibility(rows)
    assert v["eligible"] and v["n_seeds"] == 10 and v["precision"] < 0.1
    # The same spread from three seeds: t on two degrees of freedom is 4.30.
    few = [{**r, "set_n_seeds": 3} for r in rows]
    v = eligibility(few)
    assert not v["eligible"] and "precision" in v["why"] and "3 seeds" in v["why"]
    assert eligibility([{**r, "verdict": "open"} for r in rows])["why"] == "verdict open"
    missing = [{**r, "set_rel_sd": None} for r in rows]
    assert "no cross-seed mean and spread" in eligibility(missing)["why"]
    negative = [{**r, "set_null_mean": -4.0} for r in rows]  # a build that went negative
    assert "zero or negative" in eligibility(negative)["why"]
    refused = [{**r, "refused": "several_parents_moved: ['x']"} for r in rows]
    assert eligibility(refused)["why"].startswith("several_parents_moved")


def test_size_error_is_a_ratio_on_a_log_scale_with_interval_coverage_and_withholding():
    rows = [
        _item("w0", 0, 10.0, estimate=20.0, ci=[15.0, 25.0]),
        _item("w1", 1, 10.0, estimate=10.0, ci=[5.0, 15.0]),
        _item("w2", 2, 10.0, estimate=-5.0, ci=None),
        _item("w3", 3, 10.0, estimate=None, withheld="withheld:nonfinite_posterior (p)"),
    ]
    scored = {s["world"]: s for s in score_items(rows, eligibility(rows))}
    assert scored["w0"]["truth"] == pytest.approx(10.0)  # set mean 10% of a null of 100
    assert scored["w0"]["ratio"] == pytest.approx(2.0)
    assert scored["w0"]["log_error"] == pytest.approx(np.log(2.0))
    assert scored["w0"]["covered"] is False and scored["w1"]["covered"] is True
    assert scored["w2"]["wrong_sign"] and scored["w2"]["log_error"] is None
    assert scored["w2"]["covered"] is None  # no interval, no coverage
    assert "ratio" not in scored["w3"] and scored["w3"]["withheld"]  # withheld, not a zero


def test_truth_is_the_sets_mean_times_the_items_share_or_an_aligned_twins_own():
    rows = [_item(f"w{s}", s, 8.0, null=n) for s, n in enumerate([100.0, 110.0, 90.0])]
    for r in rows:  # a formula parent carrying 80% of its child's twin difference
        r["world_value"] = r["null_value"] + 10.0
    rows[0].update(estimate=8.5, aligned=True)
    rows[1].update(estimate=8.0)
    v = eligibility(rows)
    assert v["eligible"], v["why"]
    assert v["fraction"] == pytest.approx(0.8)
    scored = {s["world"]: s for s in score_items(rows, v)}
    assert scored["w0"]["basis"] == "this_twin" and scored["w0"]["truth"] == 8.0
    assert scored["w1"]["basis"] == "cross_seed_mean"
    assert scored["w1"]["truth"] == pytest.approx(0.8 * 0.10 * 110.0)


def test_error_is_also_a_share_of_the_targets_measured_counterfactual_change():
    from evalrunner.attribution import target_changes

    edge = [_item(f"w{s}", s, 10.0, estimate=14.0, child="t", target="t") for s in range(2)]
    v = eligibility(edge)
    changes = target_changes({("a", "x->t"): edge}, {("a", "x->t"): v})
    assert changes["w0"] == (pytest.approx(10.0), "cross_seed_mean")
    scored = score_items(edge, v, changes)
    assert scored[0]["error_share"] == pytest.approx(0.4)  # (14 − 10) / |10|


def test_an_imprecise_but_established_item_is_scored_on_direction_only():
    rows = [
        _item(f"w{s}", s, 10.0, estimate=e, set_rel_sd=0.2) for s, e in enumerate([5.0, -1.0, 3.0])
    ]
    scored = score_items(rows, eligibility(rows))
    assert [s.get("direction_agrees") for s in scored] == [True, False, True]
    assert all("ratio" not in s for s in scored)


# ---------------------------------------------------------------------------
# Tiers and seeds
# ---------------------------------------------------------------------------
def test_held_out_seeds_need_the_explicit_flag():
    available = range(10)
    assert select_seeds("smoke", available) == [0, 1, 2]
    assert select_seeds("standard", available) == [0, 1, 2, 3, 4]
    assert select_seeds("full", available, held_out=True) == list(range(10))
    with pytest.raises(RunRefused, match="held out"):
        select_seeds("standard", available, seeds=[0, 7])
    assert select_seeds("standard", available, seeds=[0, 7], held_out=True) == [0, 7]
    with pytest.raises(RunRefused, match="none of"):
        select_seeds("smoke", [8, 9])


# ---------------------------------------------------------------------------
# Scoreboard arithmetic
# ---------------------------------------------------------------------------
def test_spread_is_taken_across_seeds_not_across_worlds():
    seed_of = {"a0": 0, "b0": 0, "a1": 1, "b1": 1}
    out = summarize({"a0": 1.0, "b0": 3.0, "a1": 4.0, "b1": None}, seed_of)
    assert out["n_seeds"] == 2
    assert out["value"] == pytest.approx(3.0)  # mean of the seed means 2 and 4
    assert out["spread"] == pytest.approx(np.std([2.0, 4.0], ddof=1))
    assert summarize({"a0": 1.0}, seed_of)["spread"] is None


def test_empirical_tail_counts_quiet_windows_at_least_as_extreme():
    assert empirical_tail(-2.0, np.array([0.5, -2.5, 2.0, 1.0])) == 0.5
    assert empirical_tail(9.0, np.array([0.5, -2.5])) == 0.0
    assert empirical_tail(1.0, np.array([])) is None


def test_lag1_correlation_pairs_neighbouring_windows_within_a_node():
    z = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    frame = pd.DataFrame({"node": "a", "index": range(6), "z": z})
    value, pairs = lag1_correlation(frame)
    assert pairs == 5 and value == pytest.approx(-1.0)
    # A gap in the grid is not a neighbour.
    gapped = frame[frame["index"] != 3]
    assert lag1_correlation(gapped)[1] == 3


def _board(units_by_metric, worlds):
    rows = [
        {
            "question": "Q3 localization",
            "metric": metric,
            "horizon_days": None,
            "method": "innovation_contribution",
            "split": None,
            "role": "all",
            "vertical": "v",
            "units": units,
        }
        for metric, units in units_by_metric.items()
    ]
    return {
        "tier": "smoke",
        "engine_commit": "x",
        "generator_version": "g",
        "seeds": [0, 1],
        "held_out": False,
        "created": "now",
        "tree": {"path": "t", "effective_sha256": "h"},
        "worlds": {
            w: {"seed": s, "regime": "baseline", "kind": "single"} for w, s in worlds.items()
        },
        "rows": rows,
    }


def test_diff_is_paired_by_seed_on_the_shared_worlds():
    a = _board(
        {
            "top1": {
                "co.baseline.s0.x": 0.0,
                "co.baseline.s0.y": 1.0,
                "co.baseline.s1.x": 0.0,
                "co.baseline.s2.x": 1.0,
            }
        },
        {
            "co.baseline.s0.x": 0,
            "co.baseline.s0.y": 0,
            "co.baseline.s1.x": 1,
            "co.baseline.s2.x": 2,
        },
    )
    b = _board(
        {
            "top1": {"co.baseline.s0.x": 1.0, "co.baseline.s0.y": 1.0, "co.baseline.s1.x": 1.0},
            "top3": {"co.baseline.s0.x": 1.0},
        },
        {"co.baseline.s0.x": 0, "co.baseline.s0.y": 0, "co.baseline.s1.x": 1},
    )
    diff = diff_scoreboards(a, b)
    assert diff["only_in_a"] == ["co.baseline.s2.x"] and diff["rows_only_in_b"] == 1
    (row,) = diff["rows"]
    # Seed 0: 0.5 -> 1.0; seed 1: 0 -> 1. Seed 2 is not shared and is left out.
    assert (row["n_seeds"], row["n_worlds"]) == (2, 3)
    assert row["a"] == pytest.approx(0.25) and row["b"] == pytest.approx(1.0)
    assert row["difference"] == pytest.approx(0.75)
    assert row["spread"] == pytest.approx(np.std([0.5, 1.0], ddof=1))
    assert "+0.750 ± 0.354" in render_diff(diff)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_a_run_goes_from_manifest_to_scoreboard_and_resumes_without_refitting(tmp_path):
    worlds = e2e_world_set(tmp_path / "worlds")
    tree = tmp_path / "tree.yml"
    tree.write_text(E2E_TREE)
    run = plan_run(
        worlds, tree, tier="smoke", max_windows=2, engine={"commit": "test", "key": "test"}
    )
    kinds = [j["kind"] for j in run["jobs"]]
    assert kinds.count("single") == 1 and kinds.count("calibration") == 6  # 3 nodes x 2 windows
    assert run["singles"]["rate_drop"]["truth"] == ["signup_rate"]
    assert run["singles"]["rate_drop"]["set_verdicts"]

    out = tmp_path / "run"
    out.mkdir()
    (out / "run.json").write_text(json.dumps(run, default=str))
    assert run_shard(out, (0, 1), log=lambda *a, **k: None) == 7
    assert run_shard(out, (0, 1), log=lambda *a, **k: None) == 0  # every job's key is done

    board = build_scoreboard(out)
    assert board["coverage"]["jobs_done"] == 7
    assert board["coverage"]["single_worlds_ranked"] == 1
    (target,) = board["coverage"]["targets"].values()
    assert (target["target"], target["basis"]) == ("signups", "set:verdict")
    assert board["verdict_column"] == "verdict"
    # One seed: the attribution items are written, and none is size-eligible,
    # which the eligibility table says rather than leaving the question empty.
    section = board["counterfactual_attribution"]
    assert section["truth_seeds"] == [0] and not section["truth_includes_held_out_seeds"]
    table = {t["item"]: t for t in section["eligibility"]}
    assert set(table) == {"root:signup_rate", "signup_rate->signups"}
    assert not table["signup_rate->signups"]["eligible"]
    assert "no cross-seed mean and spread" in table["signup_rate->signups"]["why"]
    items = [json.loads(line) for line in (out / "rows" / "attribution.0of1.jsonl").open()]
    edge = next(i for i in items if i["item"] == "signup_rate->signups")
    assert edge["kind"] == "formula_edge" and edge["measured_effect"] < 0
    assert edge["estimate"] is not None and edge["estimate"] < 0
    rows = {
        (r["question"], r["metric"], r["method"], r["role"]): r
        for r in board["rows"]
        if r["horizon_days"] in (None, 7)
    }
    # The planted root is found, with evidence, by the innovation ranking.
    top1 = rows[("Q3 localization", "top1", "innovation_contribution", "all")]
    assert top1["value"] == 1.0 and top1["n_seeds"] == 1
    assert (
        rows[("Q3 localization", "top1_with_evidence", "innovation_evidence", "rate")]["value"]
        == 1.0
    )
    detection = rows[("Q2 detection", "abs_z_at_onset", None, "rate")]
    assert detection["value"] > 3
    assert rows[("Q2 detection", "empirical_tail", None, "rate")]["value"] == 0.0
    assert rows[("Q1 calibration", "mean_z", None, "all")]["n"] == 6
    assert rows[("Q8 cost", "fit_seconds_median", None, "all")]["value"] > 0
    onsets = [json.loads(line) for line in (out / "rows" / "detection.0of1.jsonl").open()]
    assert {r["onset"] for r in onsets} == {E2E_ONSET}
    for r in board["rows"]:  # nothing non-finite reaches the encoder (rule 3)
        assert r["value"] is None or np.isfinite(r["value"])
    json.dumps(board, allow_nan=False)
    text = render_markdown(board)
    assert "not a canonical evaluation tree" in text and "Q3 Localization" in text
    assert "Q5 Which counterfactual attribution sizes the world set can ground" in text

    # A different shard layout finds the same jobs done.
    assert run_shard(out, (1, 3), log=lambda *a, **k: None) == 0

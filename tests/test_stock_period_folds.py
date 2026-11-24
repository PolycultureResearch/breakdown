"""Roadmap C66: a `kind: stock` its provider adds up over each period.

`kind` only governs aggregation when breakdown resamples a finer parent up to a
child's grain. A node's own series is asked of the provider at the node's
grain, and the provider applies the *measure's* aggregation: a weekly
`SUM(mrr)` over a daily snapshot table is seven days of MRR. These tests
reproduce that through the real `duckdb` provider, then pin the refusal that
replaces it on every path that loads a tree (`fetch_all_metrics`, `breakdown
check`, `breakdown doctor`), and the per-provider way breakdown knows the fold.
"""

import json
import logging
import textwrap

import pandas as pd
import pytest

from breakdown.dbt_bridge import period_fold
from breakdown.dbt_manifest import parse_manifest
from breakdown.grains import STOCK_PERIOD_FOLDS, stock_period_refusal
from breakdown.loading import (
    StockFoldedOverPeriod,
    refuse_stock_period_folds,
    stock_period_problems,
)
from breakdown.parser import BindingSpec, Parser

# Four whole ISO weeks of one account's daily MRR: 1000, 1001, … 1027.
START, END = "2025-06-02", "2025-06-29"
DAYS = pd.date_range(START, END, freq="D")
LEVELS = [1000.0 + i for i in range(len(DAYS))]


@pytest.fixture
def exports(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    rows = ["snapshot_date,account_id,mrr"] + [
        f"{day.date()},1,{level}" for day, level in zip(DAYS, LEVELS)
    ]
    (d / "mrr_daily.csv").write_text("\n".join(rows) + "\n")
    return d


def _duckdb_tree(grain="week", bind=None, extra=""):
    bind = bind or textwrap.dedent(
        """\
        relation: mrr_daily
        grain_key: snapshot_date
        time_column: snapshot_date
        agg: sum
        measure: mrr
        """
    )
    return (
        textwrap.dedent(
            f"""\
            provider:
              type: duckdb
              data_dir: ./exports
            metrics:
              - name: mrr
                source: exports.mrr
                grain: {grain}
                kind: stock
                bind:
            """
        )
        + textwrap.indent(bind, " " * 6)
        + textwrap.indent(textwrap.dedent(extra), "  ")
    )


def _load(tmp_path, text):
    from breakdown.loading import build_fetcher, fetch_all_metrics

    path = tmp_path / "tree.yml"
    path.write_text(text)
    parser = Parser(text)
    fetcher = build_fetcher(
        parser.config.provider, parser.dag, parser.config.metrics, tree_path=str(path)
    )
    return parser, fetcher, fetch_all_metrics(parser, fetcher, "duckdb", START, END)


# --- the defect, reproduced ----------------------------------------------------


def test_the_provider_sums_a_week_of_daily_snapshots(tmp_path, exports):
    """The defect at the provider, before any refusal: what `SUM` grouped by
    week returns for a daily stock is seven levels added together."""
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    from breakdown.loading import build_fetcher

    path = tmp_path / "tree.yml"
    path.write_text(_duckdb_tree())
    parser = Parser(path.read_text())
    fetcher = build_fetcher(
        parser.config.provider, parser.dag, parser.config.metrics, tree_path=str(path)
    )
    df = fetcher.fetch_metric("mrr", START, END, grain="week", kind="stock")
    first_week = sum(LEVELS[:7])
    assert df["mrr"].iloc[0] == pytest.approx(first_week)
    assert df["mrr"].iloc[0] / LEVELS[6] == pytest.approx(7.0, rel=0.01)


def test_loading_refuses_it_by_name(tmp_path, exports):
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    with pytest.raises(StockFoldedOverPeriod) as e:
        _load(tmp_path, _duckdb_tree())
    msg = str(e.value)
    for needle in ("'mrr'", "`kind: stock`", "'week'", "`sum`", "duckdb", "grain 'day'"):
        assert needle in msg, f"{needle!r} missing from: {msg}"
    assert "time_grain: week" in msg  # the second remedy, in this provider's terms


def test_the_first_remedy_gives_the_level(tmp_path, exports):
    """Declared at its native grain, the stock loads, and a weekly formula
    reading it sees the last daily value — `resample_up` is kind-aware."""
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    extra = """\
      - name: arr
        grain: week
        kind: stock
        formula: "mrr * 12"
        parents: [mrr]
    """
    _, _, data = _load(tmp_path, _duckdb_tree(grain="day", extra=extra))
    arr = data.series("arr")
    assert arr["arr"].iloc[0] == pytest.approx(12 * LEVELS[6])


def test_the_second_remedy_gives_the_level(tmp_path, exports):
    """A `bind.sql` that already picks each week's closing snapshot, declared
    `time_grain: week`, is one row per period: the sum of one row is the row."""
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    bind = textwrap.dedent(
        """\
        sql: >-
          SELECT DATE_TRUNC('week', snapshot_date) AS week, mrr
          FROM mrr_daily
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY DATE_TRUNC('week', snapshot_date) ORDER BY snapshot_date DESC) = 1
        grain_key: week
        time_column: week
        time_grain: week
        agg: sum
        measure: mrr
        """
    )
    _, _, data = _load(tmp_path, _duckdb_tree(bind=bind))
    assert data.series("mrr")["mrr"].tolist() == [LEVELS[i] for i in (6, 13, 20, 27)]


def test_a_period_distinct_count_is_not_refused():
    """`count_distinct` counts each entity once however many snapshots it
    appears in, so it does not scale with the period (the White Cube demo's
    weekly `active_subscriptions`)."""
    assert (
        stock_period_refusal(
            "active",
            "week",
            "count_distinct",
            "day",
            measure="subscription_id",
            provider="duckdb",
            alternative="…",
        )
        is None
    )


@pytest.mark.parametrize("agg", ["sum", "count", "average"])
def test_every_snapshot_fold_is_refused_and_says_why(agg):
    msg = stock_period_refusal(
        "m", "month", agg, "day", measure="x", provider="dbt", alternative="do the other thing."
    )
    assert msg is not None and STOCK_PERIOD_FOLDS[agg] in msg and "do the other thing." in msg


def test_nothing_is_folded_at_or_above_the_source_grain():
    for agg in STOCK_PERIOD_FOLDS:
        assert (
            stock_period_refusal(
                "m", "week", agg, "week", measure="x", provider="duckdb", alternative="…"
            )
            is None
        )


def test_a_day_stock_is_never_checked():
    # The finest grain cannot fold anything finer, so no manifest is read.
    parser = Parser(_duckdb_tree(grain="day"))
    assert stock_period_problems(parser.config).refusals == []


def test_time_grain_is_validated():
    with pytest.raises(ValueError, match="time_grain"):
        BindingSpec(grain_key="k", time_column="t", agg="sum", measure="x", time_grain="quarter")


# --- how each provider knows ---------------------------------------------------


def _manifest(measure_extra=None, metric_type="simple"):
    """A one-model semantic manifest: a daily balance table and its metric."""
    measure = {"name": "balance", "agg": "sum", "expr": "amount"}
    measure.update(measure_extra or {})
    return {
        "semantic_models": [
            {
                "name": "balances",
                "node_relation": {"alias": "fct_balances", "schema_name": "marts"},
                "defaults": {"agg_time_dimension": "day"},
                "entities": [{"name": "account", "type": "primary", "expr": "account_id"}],
                "dimensions": [
                    {"name": "day", "type": "time", "type_params": {"time_granularity": "day"}}
                ],
                "measures": [measure],
            }
        ],
        "metrics": [
            {
                "name": "balance",
                "type": "simple",
                "type_params": {"measure": {"name": "balance"}},
            },
            {
                "name": "balance_x2",
                "type": "derived",
                "type_params": {"expr": "balance * 2", "metrics": [{"name": "balance"}]},
            },
        ],
    }


def test_metricflow_sum_measure_folds_by_sum():
    fold = period_fold(parse_manifest(_manifest()), "balance")
    assert (fold.agg, fold.source_grain, fold.measure) == ("sum", "day", "balance")


def test_metricflow_semi_additive_measure_folds_to_the_closing_snapshot():
    nad = {"non_additive_dimension": {"name": "day", "window_choice": "max"}}
    assert period_fold(parse_manifest(_manifest(nad)), "balance").agg == "last"
    nad["non_additive_dimension"]["window_choice"] = "min"
    assert period_fold(parse_manifest(_manifest(nad)), "balance").agg == "first"


def test_a_derived_metric_folds_as_badly_as_its_worst_input():
    fold = period_fold(parse_manifest(_manifest()), "balance_x2")
    assert fold.agg == "sum" and "balance_x2" in fold.measure


def test_the_classic_spec_semi_additive_measure_is_no_longer_bound_as_a_sum():
    """The bridge read `non_additive_dimension` off the new spec only, so a
    classic-spec balance measure bound as `agg: sum` (found with C66)."""
    pytest.importorskip("sqlglot")
    from breakdown.dbt_bridge import translate

    nad = {"non_additive_dimension": {"name": "day", "window_choice": "max"}}
    r = translate(parse_manifest(_manifest(nad)))
    assert "balance" not in r.bindings
    assert any(s.name == "balance" and "non_additive_dimension" in s.reason for s in r.skipped)


def test_the_bridge_records_the_source_grain_on_the_binding():
    pytest.importorskip("sqlglot")
    from breakdown.dbt_bridge import translate

    assert translate(parse_manifest(_manifest())).bindings["balance"].time_grain == "day"


def _local_tree(project_path, grain="week"):
    return textwrap.dedent(
        f"""\
        provider:
          type: local
          project_path: {project_path}
        metrics:
          - name: balance
            source: sl.metrics.balance
            grain: {grain}
            kind: stock
        """
    )


def test_local_reads_the_fold_off_the_projects_manifest(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "semantic_manifest.json").write_text(json.dumps(_manifest()))
    found = stock_period_problems(Parser(_local_tree(tmp_path)).config)
    assert len(found.refusals) == 1 and "non_additive_dimension" in found.refusals[0]


def test_local_accepts_a_semi_additive_measure(tmp_path):
    (tmp_path / "target").mkdir()
    nad = {"non_additive_dimension": {"name": "day", "window_choice": "max"}}
    (tmp_path / "target" / "semantic_manifest.json").write_text(json.dumps(_manifest(nad)))
    found = stock_period_problems(Parser(_local_tree(tmp_path)).config)
    assert found.refusals == [] and found.unverified == []


def test_local_without_a_manifest_names_the_node_as_unverified(tmp_path, caplog):
    config = Parser(_local_tree(tmp_path / "absent")).config
    caplog.set_level(logging.WARNING, logger="breakdown.loading")
    found = refuse_stock_period_folds(config)
    assert found.refusals == [] and len(found.unverified) == 1
    assert "no semantic manifest" in found.unverified[0]
    assert any("'balance'" in r.getMessage() for r in caplog.records)


def test_cloud_cannot_see_its_definitions_and_says_so():
    text = textwrap.dedent(
        """\
        provider:
          type: cloud
          environment_id: "1"
          host: semantic-layer.cloud.getdbt.com
          token: x
        metrics:
          - name: balance
            source: sl.metrics.balance
            grain: month
            kind: stock
        """
    )
    found = stock_period_problems(Parser(text).config)
    assert found.refusals == [] and "'balance'" in found.unverified[0]


# --- the gates: check and doctor say the same sentence ------------------------


def test_breakdown_check_refuses_it_before_any_fetch(tmp_path, exports):
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    from breakdown.check import run_check

    path = tmp_path / "tree.yml"
    path.write_text(_duckdb_tree())
    results = run_check(str(path))
    failed = [r for r in results if r.status == "fail"]
    assert failed and "`kind: stock`" in failed[0].detail


def test_breakdown_check_warns_on_an_unverified_stock(tmp_path, monkeypatch):
    from breakdown import check

    monkeypatch.setattr("breakdown.data_fetch.provider_extra_missing", lambda _p: None)
    path = tmp_path / "tree.yml"
    path.write_text(_local_tree(tmp_path / "absent"))
    results = check.run_check(str(path))
    assert [r.status for r in results] == ["pass", "warn"]
    assert "'balance'" in results[1].detail


def test_doctor_fails_it_as_a_tree_check(tmp_path, exports):
    pytest.importorskip("duckdb")
    pytest.importorskip("sqlglot")
    from breakdown.doctor import run_doctor

    path = tmp_path / "tree.yml"
    path.write_text(_duckdb_tree())
    results = run_doctor(str(path), START, END)
    stock = next(r for r in results if r.name == "stock period folds")
    assert stock.status == "fail" and "'mrr'" in stock.detail

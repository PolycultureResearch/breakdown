"""The `dbt` provider (roadmap 2.10).

The fetch tests run end to end against DuckDB — generate SQL, execute it, and
push the result through the same `_to_naive_dates` → `_floor_labels` →
`_align_to_spine` chain every provider shares. That covers the join between the
three pieces, which is where a provider actually goes wrong: each part can be
right while the frame handed between them has the wrong column names, the wrong
dtype, or an unaligned spine.
"""

import pandas as pd
import pytest
import yaml

from breakdown.dbt_provider import (
    DbtDataFetcher,
    DbtProfileError,
    connect_from_profile,
    fetcher_from_project,
    resolve_profile,
)
from breakdown.parser import BindingDimension, BindingSpec

pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")
duckdb = pytest.importorskip("duckdb")

from breakdown.data_fetch import SliceNotSupported  # noqa: E402

ORDERS = """
CREATE TABLE fct_orders AS SELECT * FROM (VALUES
    (1, DATE '2024-01-01', 10.0, 'EMEA'),
    (2, DATE '2024-01-02', 20.0, 'AMER'),
    (3, DATE '2024-01-02', 5.0,  'EMEA'),
    -- nothing on the 3rd: an interior gap the spine must fill, not the query
    (4, DATE '2024-01-04', 40.0, NULL)
) AS t(order_id, ordered_at, amount, region);
"""


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute(ORDERS)
    return c


@pytest.fixture
def fetcher(con):
    bind = BindingSpec(
        relation="fct_orders",
        grain_key="order_id",
        time_column="ordered_at",
        agg="sum",
        measure="amount",
        dimensions={"region": BindingDimension(column="region")},
    )
    return DbtDataFetcher({"revenue": bind}, connect=lambda: con, dialect="duckdb")


# --- the shared fetcher contract -------------------------------------------


def test_fetch_metric_returns_the_engine_contract(fetcher):
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04", grain="day")
    assert list(df.columns) == ["date", "revenue"]
    assert pd.DatetimeIndex(df["date"]).tz is None
    assert df["revenue"].tolist() == [10.0, 25.0, 0.0, 40.0]


def test_interior_gaps_are_filled_by_the_spine_not_the_query(fetcher):
    # The 3rd has no row at all. `_align_to_spine` fills a flow with 0 and logs
    # it; the SQL must not invent the period itself or it would be filled twice.
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04", grain="day")
    assert df.loc[df["date"] == "2024-01-03", "revenue"].item() == 0.0


def test_trailing_gaps_are_trimmed_not_zero_filled(fetcher):
    # Periods after the last row the source returned mean "not loaded yet".
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-10", grain="day")
    assert df["date"].max() == pd.Timestamp("2024-01-04")


def test_partial_edge_periods_are_dropped_at_week_grain(fetcher):
    # 2024-01-01 is a Monday, so [01-01, 01-04] contains no whole week.
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04", grain="week")
    assert df.empty


def test_a_rate_is_never_gap_filled(con):
    """A period the ratio has no rows for carries no value — not a zero, not
    the previous day's rate (roadmap 1.11). The refusal to *invent* is the
    invariant; refusing to serve the metric at all was the collateral damage."""
    bind = BindingSpec(
        relation="fct_orders",
        grain_key="order_id",
        time_column="ordered_at",
        agg="ratio",
        numerator="amount",
        denominator="amount",
    )
    f = DbtDataFetcher({"r": bind}, connect=lambda: con, dialect="duckdb")
    df = f.fetch_metric("r", "2024-01-01", "2024-01-04", grain="day", kind="rate")
    assert df["r"].isna().any()
    assert not (df["r"] == 0.0).any()


# --- slicing ----------------------------------------------------------------


def test_sliced_fetch_returns_long_format(fetcher):
    df = fetcher.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-04")
    assert list(df.columns) == ["date", "slice", "value"]
    assert df["value"].sum() == 75.0


def test_null_dimension_values_become_the_null_sentinel(fetcher):
    df = fetcher.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-04")
    assert "__null__" in set(df["slice"])


def test_slicing_an_undeclared_dimension_raises_the_typed_error(fetcher):
    # SliceNotSupported is what the API maps to a 422 naming the provider.
    with pytest.raises(SliceNotSupported, match="declares no dimension"):
        fetcher.fetch_metric_sliced("revenue", "plan", "2024-01-01", "2024-01-04")


def test_additive_slices_reconcile_with_the_total(fetcher):
    total = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04")["revenue"].sum()
    sliced = fetcher.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-04")[
        "value"
    ].sum()
    assert sliced == total == 75.0


def test_non_additive_slices_say_why_they_do_not_sum(con, caplog):
    # Real evidence for this: slicing `active_subscription_count` by status on a
    # live warehouse gave 2,106 against an unsliced 2,069. Not a defect — one
    # entity changing status inside a day is counted once in the total and once
    # per slice — but the slices are not a decomposition, so the gap has to read
    # as dedup overlap rather than an unexplained cause (roadmap 3.8).
    con.execute(
        "CREATE TABLE visits AS SELECT * FROM (VALUES "
        "(1, DATE '2024-01-01', 'u1', 'ios'), "
        "(2, DATE '2024-01-01', 'u1', 'web'), "
        "(3, DATE '2024-01-01', 'u2', 'ios')) AS t(id, d, user_id, platform)"
    )
    bind = BindingSpec(
        relation="visits",
        grain_key="id",
        time_column="d",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        dimensions={"platform": BindingDimension(column="platform")},
    )
    f = DbtDataFetcher({"dau": bind}, connect=lambda: con, dialect="duckdb")
    total = f.fetch_metric("dau", "2024-01-01", "2024-01-01")["dau"].sum()
    with caplog.at_level("WARNING"):
        sliced = f.fetch_metric_sliced("dau", "platform", "2024-01-01", "2024-01-01")["value"].sum()
    assert total == 2 and sliced == 3  # u1 is on both platforms
    assert "do not sum" in caplog.text and "overlap" in caplog.text


# --- diagnostics ------------------------------------------------------------


def test_unknown_metric_names_what_it_knows(fetcher):
    with pytest.raises(RuntimeError, match="No binding for 'nope'"):
        fetcher.fetch_metric("nope", "2024-01-01", "2024-01-04")


def test_the_grain_claim_is_checkable(fetcher, con):
    assert fetcher.check_grain("revenue") == (4, 4)

    con.execute("CREATE TABLE dup AS SELECT * FROM fct_orders UNION ALL SELECT * FROM fct_orders")
    fanned = BindingSpec(
        relation="dup", grain_key="order_id", time_column="ordered_at", agg="sum", measure="amount"
    )
    f = DbtDataFetcher({"revenue": fanned}, connect=lambda: con, dialect="duckdb")
    rows, distinct = f.check_grain("revenue")
    assert rows == 8 and distinct == 4  # every aggregate over this is doubled


def test_the_sql_behind_each_number_is_recorded(fetcher):
    # The hook 2.11 reads: principle 3 says a number must be defensible, and
    # this is what makes the query visible without re-deriving it.
    fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04")
    fetcher.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-04")
    assert "fct_orders" in fetcher.last_sql["revenue"]
    assert "region" in fetcher.last_sql["revenue::region"]


def test_construction_does_not_touch_the_warehouse():
    # A tree whose metrics all have snapshots must boot with the warehouse down,
    # so the connection is a callable, not a live handle.
    def explode():
        raise AssertionError("connected during construction")

    DbtDataFetcher({}, connect=explode, dialect="duckdb")


# --- filters, end to end (roadmap 2.17) --------------------------------------


def _filtered_fetcher(con):
    bind = BindingSpec(
        relation="fct_orders",
        grain_key="order_id",
        time_column="ordered_at",
        agg="sum",
        measure="amount",
        dimensions={"region": BindingDimension(column="region")},
        where=["region = 'EMEA'"],
    )
    return DbtDataFetcher({"revenue": bind}, connect=lambda: con, dialect="duckdb")


def test_a_filtered_binding_serves_a_smaller_series_through_the_whole_chain(fetcher, con):
    # Generate, execute, align — the join between the three pieces, which is
    # where a provider actually goes wrong. The unfiltered series is
    # [10, 25, 0, 40] = 75; EMEA is orders 1 and 3, so [10, 5, 0, 0] = 15.
    everything = fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-04")["revenue"]
    filtered = _filtered_fetcher(con).fetch_metric("revenue", "2024-01-01", "2024-01-04")["revenue"]
    assert everything.tolist() == [10.0, 25.0, 0.0, 40.0]
    assert filtered.tolist() == [10.0, 5.0]
    assert filtered.sum() < everything.sum()


def test_the_filtered_slices_still_reconcile_with_the_filtered_total(con):
    f = _filtered_fetcher(con)
    total = f.fetch_metric("revenue", "2024-01-01", "2024-01-04")["revenue"].sum()
    sliced = f.fetch_metric_sliced("revenue", "region", "2024-01-01", "2024-01-04")["value"].sum()
    assert total == sliced == 15.0


def test_the_predicate_is_visible_in_the_statement_provenance(con):
    # Principle 3: a number the engine cannot defend is not shipped. A filtered
    # node is smaller than the dashboard metric of the same name, and *show
    # query* is where a reader can see why.
    f = _filtered_fetcher(con)
    f.fetch_metric("revenue", "2024-01-01", "2024-01-04")
    assert "region = 'EMEA'" in f.query_provenance("revenue")


def test_the_filter_claim_is_checkable(con):
    f = _filtered_fetcher(con)
    assert f.check_filter("revenue") == (4, 2)
    assert "kept" in f.last_sql["revenue::filter"]


def test_the_filter_claim_can_be_bounded_to_a_window(con):
    f = _filtered_fetcher(con)
    assert f.check_filter("revenue", start_date="2024-01-01", end_date="2024-01-02") == (3, 2)


def test_an_empty_probe_window_reports_no_rows_rather_than_everything_dropped(con):
    # `SUM(...)` over no rows is NULL, not 0. Reporting that as `kept == 0` would
    # fail a healthy binding whose window simply has no data in it.
    f = _filtered_fetcher(con)
    assert f.check_filter("revenue", start_date="2023-01-01", end_date="2023-01-02") == (0, 0)


def test_the_grain_claim_over_a_filtered_binding_counts_the_filtered_rows(con):
    assert _filtered_fetcher(con).check_grain("revenue") == (2, 2)


# --- profile resolution -----------------------------------------------------


def _project(tmp_path, profiles, *, profile="default", project="proj"):
    (tmp_path / "dbt_project.yml").write_text(yaml.safe_dump({"name": project, "profile": profile}))
    (tmp_path / "profiles.yml").write_text(yaml.safe_dump(profiles))
    return str(tmp_path)


def test_profile_resolves_the_projects_own_target(tmp_path):
    path = _project(
        tmp_path,
        {
            "default": {
                "target": "dev",
                "outputs": {
                    "dev": {"type": "duckdb", "path": "dev.db"},
                    "prod": {"type": "duckdb", "path": "prod.db"},
                },
            }
        },
    )
    assert resolve_profile(path)["path"] == "dev.db"
    assert resolve_profile(path, target="prod")["path"] == "prod.db"


def test_env_var_in_a_profile_is_rendered(tmp_path, monkeypatch):
    monkeypatch.setenv("WH_TOKEN", "s3cret")
    path = _project(
        tmp_path,
        {
            "default": {
                "target": "dev",
                "outputs": {
                    "dev": {
                        "type": "duckdb",
                        "token": "{{ env_var('WH_TOKEN') }}",
                        "schema": "{{ env_var('WH_SCHEMA', 'fallback') }}",
                    }
                },
            }
        },
    )
    out = resolve_profile(path)
    assert out["token"] == "s3cret"
    assert out["schema"] == "fallback"


def test_unset_env_var_is_named_not_passed_through(tmp_path):
    # `{{ env_var('X') }}` handed to a driver as a literal password fails in a
    # way nobody can read, so it is caught here instead.
    path = _project(
        tmp_path,
        {
            "default": {
                "target": "dev",
                "outputs": {
                    "dev": {"type": "duckdb", "token": "{{ env_var('NOT_SET_ANYWHERE') }}"}
                },
            }
        },
    )
    with pytest.raises(DbtProfileError, match="NOT_SET_ANYWHERE"):
        resolve_profile(path)


def test_jinja_we_cannot_render_is_refused(tmp_path):
    path = _project(
        tmp_path,
        {
            "default": {
                "target": "dev",
                "outputs": {"dev": {"type": "duckdb", "token": "{{ var('nope') }}"}},
            }
        },
    )
    with pytest.raises(DbtProfileError, match="cannot render"):
        resolve_profile(path)


def test_missing_target_lists_what_exists(tmp_path):
    path = _project(
        tmp_path, {"default": {"target": "dev", "outputs": {"dev": {"type": "duckdb"}}}}
    )
    with pytest.raises(DbtProfileError, match=r"Available: \['dev'\]"):
        resolve_profile(path, target="staging")


def test_missing_profile_names_the_project_file(tmp_path):
    path = _project(tmp_path, {"other": {"target": "dev", "outputs": {"dev": {}}}})
    with pytest.raises(DbtProfileError, match="is not in"):
        resolve_profile(path)


def test_unsupported_adapter_says_so_and_offers_the_way_out(tmp_path):
    with pytest.raises(DbtProfileError, match="No connection support"):
        connect_from_profile({"type": "exasol"})


def test_fetcher_from_project_needs_a_manifest(tmp_path):
    path = _project(
        tmp_path, {"default": {"target": "dev", "outputs": {"dev": {"type": "duckdb"}}}}
    )
    with pytest.raises(FileNotFoundError, match="Run `dbt parse`"):
        fetcher_from_project(path)


# --- selecting the provider from a tree -------------------------------------


def test_dbt_is_a_selectable_provider_type():
    from breakdown.parser import Parser

    parser = Parser(
        """
provider:
  type: dbt
  project_path: /some/dbt/project
  target: prod
metrics:
  - name: revenue
    source: jaffle.metrics.revenue
"""
    )
    cfg = parser.config.provider
    assert (cfg.type, cfg.project_path, cfg.target) == ("dbt", "/some/dbt/project", "prod")


def test_dbt_provider_config_expands_env_refs(monkeypatch):
    from breakdown.parser import Parser

    monkeypatch.setenv("DBT_PROJECT", "/from/env")
    parser = Parser(
        """
provider:
  type: dbt
  project_path: ${DBT_PROJECT}
metrics:
  - name: revenue
    source: jaffle.metrics.revenue
"""
    )
    assert parser.config.provider.project_path == "/from/env"


def test_the_factory_routes_dbt_to_this_fetcher(tmp_path, monkeypatch):
    from breakdown.loading import build_fetcher
    from breakdown.parser import Parser

    parser = Parser(
        f"""
provider:
  type: dbt
  project_path: {tmp_path}
metrics:
  - name: revenue
    source: jaffle.metrics.revenue
"""
    )
    called = {}

    def fake(project_path, **kw):
        called.update(project_path=project_path, **kw)
        return "fetcher"

    monkeypatch.setattr("breakdown.dbt_provider.fetcher_from_project", fake)
    assert build_fetcher(parser.config.provider, parser.dag, parser.config.metrics) == "fetcher"
    assert called["project_path"] == str(tmp_path)


def test_a_nodes_own_bind_overrides_the_manifest(tmp_path, monkeypatch):
    # The tree corrects what dbt declares without editing the dbt project. The
    # override is keyed by the queried name (the last segment of `source`),
    # which is what the fetcher looks bindings up by.
    from breakdown.loading import build_fetcher
    from breakdown.parser import Parser

    parser = Parser(
        f"""
provider:
  type: dbt
  project_path: {tmp_path}
metrics:
  - name: revenue
    source: jaffle.metrics.revenue
    bind:
      relation: analytics.fct_orders
      grain_key: order_id
      time_column: ordered_at
      agg: sum
      measure: amount
"""
    )
    seen = {}

    def fake(project_path, **kw):
        seen.update(kw)
        return "fetcher"

    monkeypatch.setattr("breakdown.dbt_provider.fetcher_from_project", fake)
    build_fetcher(parser.config.provider, parser.dag, parser.config.metrics)
    assert set(seen["overrides"]) == {"revenue"}
    assert seen["overrides"]["revenue"].relation == "analytics.fct_orders"


def test_provider_query_name_is_one_function_not_three_ternaries():
    # It had three call sites — startup fetch, sliced fetch, doctor readiness —
    # and adding a provider meant editing all three, which is how a provider
    # ships working at startup and broken on slice.
    from breakdown.data_fetch import provider_query_name
    from breakdown.parser import MetricDefinition

    m = MetricDefinition(name="revenue", source="jaffle.metrics.rev_source")
    assert provider_query_name("mock", m) == "revenue"
    assert provider_query_name("warehouse", m) == "revenue"
    for sl in ("local", "cloud", "dbt"):
        assert provider_query_name(sl, m) == "rev_source"


# --- entity-grain resolution end to end (roadmap 3.8 §4) --------------------


EVENTS = """
CREATE TABLE ev AS SELECT * FROM (VALUES
    (1, 'u1', TIMESTAMP '2024-01-01 09:00', 'active'),
    (2, 'u1', TIMESTAMP '2024-01-01 18:00', 'cancelled'),
    (3, 'u2', TIMESTAMP '2024-01-01 10:00', 'active')
) AS t(row_id, user_id, seen_at, status);
"""


def _dau_fetcher(con, resolve=None):
    kw = dict(
        relation="ev",
        grain_key="row_id",
        time_column="seen_at",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        dimensions={"status": BindingDimension(column="status")},
    )
    if resolve is not None:
        kw["entity_grain"] = {"resolve": resolve}
    return DbtDataFetcher({"dau": BindingSpec(**kw)}, connect=lambda: con, dialect="duckdb")


@pytest.fixture
def events():
    c = duckdb.connect()
    c.execute(EVENTS)
    return c


def test_resolution_turns_overlapping_slices_into_exact_ones(events):
    assert _dau_fetcher(events).slice_additivity("dau", "status") == "overlapping"
    assert _dau_fetcher(events, "last").slice_additivity("dau", "status") == "exact"


def test_resolved_slices_reconcile_with_the_unsliced_metric(events):
    f = _dau_fetcher(events, "last")
    total = f.fetch_metric("dau", "2024-01-01", "2024-01-01")["dau"].sum()
    sliced = f.fetch_metric_sliced("dau", "status", "2024-01-01", "2024-01-01")["value"].sum()
    assert total == sliced == 2  # u1 and u2, each counted once

    naive = (
        _dau_fetcher(events)
        .fetch_metric_sliced("dau", "status", "2024-01-01", "2024-01-01")["value"]
        .sum()
    )
    assert naive == 3  # the overstatement resolution removes


def test_the_overlap_warning_stops_once_slices_are_exact(events, caplog):
    with caplog.at_level("WARNING"):
        _dau_fetcher(events, "last").fetch_metric_sliced(
            "dau", "status", "2024-01-01", "2024-01-01"
        )
    assert "do not sum" not in caplog.text


def test_provenance_shows_the_resolved_statement(events):
    f = _dau_fetcher(events, "first")
    f.fetch_metric_sliced("dau", "status", "2024-01-01", "2024-01-01")
    sql = f.query_provenance("dau", "status")
    assert "ROW_NUMBER" in sql and "ASC" in sql


# --- entity flows end to end (roadmap 3.8 §6) -------------------------------


FLOW_EVENTS = """
CREATE TABLE flows AS SELECT * FROM (VALUES
    -- reference window 2024-01-01..02
    (1, 'stay',   'ios', TIMESTAMP '2024-01-01 10:00'),
    (2, 'leaver', 'ios', TIMESTAMP '2024-01-01 10:00'),
    (3, 'mover',  'ios', TIMESTAMP '2024-01-01 10:00'),
    -- analysis window 2024-01-03..04
    (4, 'stay',   'ios', TIMESTAMP '2024-01-03 10:00'),
    (5, 'mover',  'web', TIMESTAMP '2024-01-03 10:00'),
    (6, 'joiner', 'web', TIMESTAMP '2024-01-03 10:00')
) AS t(row_id, user_id, platform, seen_at);
"""

FLOW_WINDOWS = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")


def _flow_fetcher(resolve="last"):
    con = duckdb.connect()
    con.execute(FLOW_EVENTS)
    bind = BindingSpec(
        relation="flows",
        grain_key="row_id",
        time_column="seen_at",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        entity_grain={"resolve": resolve},
        dimensions={"platform": BindingDimension(column="platform")},
    )
    return DbtDataFetcher({"dau": bind}, connect=lambda: con, dialect="duckdb")


def test_entity_flows_classify_a_real_transition_matrix():
    from breakdown.engine.slices import entity_flows

    f = _flow_fetcher()
    flows = entity_flows(f.fetch_entity_flows("dau", "platform", *FLOW_WINDOWS))
    assert flows["totals"] == {"new": 1, "churned": 1, "retained": 1, "migrated": 1}
    assert flows["migrations"] == [{"from": "ios", "to": "web", "entities": 1}]
    assert flows["migration_net"] == 0


def test_a_binding_without_entity_grain_cannot_classify_flows():
    from breakdown.dbt_sql import UnsupportedBinding

    con = duckdb.connect()
    con.execute(FLOW_EVENTS)
    bind = BindingSpec(
        relation="flows",
        grain_key="row_id",
        time_column="seen_at",
        agg="count_distinct",
        measure="user_id",
        entity_key="user_id",
        dimensions={"platform": BindingDimension(column="platform")},
    )
    f = DbtDataFetcher({"dau": bind}, connect=lambda: con, dialect="duckdb")
    with pytest.raises(UnsupportedBinding, match="need an `entity_grain`"):
        f.fetch_entity_flows("dau", "platform", *FLOW_WINDOWS)


def test_providers_that_cannot_classify_entities_raise_the_typed_error():
    from breakdown.data_fetch import MockDataFetcher

    with pytest.raises(SliceNotSupported, match="entity flows"):
        MockDataFetcher().fetch_entity_flows("m", "d", *FLOW_WINDOWS)


def test_the_flow_query_is_recorded_for_provenance():
    f = _flow_fetcher()
    f.fetch_entity_flows("dau", "platform", *FLOW_WINDOWS)
    assert "FULL OUTER JOIN" in f.last_sql["dau::platform::flows"]


def test_resolve_error_does_not_claim_exactness_it_has_not_earned(events):
    # `resolve: error` asserts single-valuedness without making it true — the
    # generated query is the naive one. Reporting `exact` put a false label on
    # the number and stopped the overlap from being withheld; `unknown` is the
    # honest answer at query time, and reconciliation still flags a real
    # residual while `doctor` settles the assertion.
    assert _dau_fetcher(events, "error").slice_additivity("dau", "status") == "unknown"
    assert _dau_fetcher(events, "last").slice_additivity("dau", "status") == "exact"
    assert _dau_fetcher(events).slice_additivity("dau", "status") == "overlapping"


def test_resolve_error_serves_the_naive_query_consistently_with_its_label(events):
    # The label and the query must agree: `error` does not resolve, so the
    # slices are whatever the data says — here, overstating by the overlap.
    f = _dau_fetcher(events, "error")
    total = f.fetch_metric("dau", "2024-01-01", "2024-01-01")["dau"].sum()
    sliced = f.fetch_metric_sliced("dau", "status", "2024-01-01", "2024-01-01")["value"].sum()
    assert total == 2 and sliced == 3


def test_flows_are_cached_so_a_second_click_costs_nothing():
    # Uncached, every slice request re-ran a FULL OUTER JOIN over two windows —
    # the same query, same windows, on every click.
    from breakdown.api.main import _fetch_flows_cached

    calls = []

    class _State:
        flow_cache = {}

        class fetcher:
            @staticmethod
            def fetch_entity_flows(*a):
                calls.append(a)
                return "transitions"

    class _Parser:
        class config:
            class provider:
                type = "dbt"

    metric = BindingSpec(relation="t", grain_key="r", time_column="d", agg="sum", measure="v")
    metric = type("M", (), {"name": "dau", "source": "w.dau", "grain": "day"})()
    args = (
        _State(),
        _Parser(),
        metric,
        "status",
        "2024-01-01",
        "2024-01-07",
        "2024-01-08",
        "2024-01-14",
    )
    assert _fetch_flows_cached(*args) == "transitions"
    assert _fetch_flows_cached(*args) == "transitions"
    assert len(calls) == 1


def test_the_grain_assertion_can_be_bounded_to_a_window(events):
    f = _dau_fetcher(events, "last")
    assert f.check_grain("dau") == (3, 3)
    assert f.check_grain("dau", start_date="2024-01-01", end_date="2024-01-01") == (3, 3)
    assert "WHERE" in f.last_sql["dau::grain"]


# --- the bigquery connector --------------------------------------------------
#
# The generator has emitted BigQuery SQL since 2.10 (`ADAPTER_DIALECTS`, and an
# ISOWEEK override for week bucketing); only the connection was missing, which
# left a BigQuery shop on the `local` provider and its `mf` subprocess. These
# cover the parts that are testable without a warehouse: which connector the
# profile selects, and the credential branch it picks inside it.


def test_bigquery_is_a_supported_adapter():
    from breakdown.dbt_provider import CONNECTORS

    assert "bigquery" in CONNECTORS


def test_bigquery_dialect_is_mapped_for_the_adapter():
    # The connector is only useful if the SQL it runs is BigQuery-shaped; this
    # pins the pair together so neither half can be removed alone.
    from breakdown.dbt_sql import dialect_for_adapter

    assert dialect_for_adapter("bigquery") == "bigquery"


def test_bigquery_unsupported_auth_method_is_named():
    pytest.importorskip("google.cloud.bigquery", reason="needs the bigquery extra")

    with pytest.raises(DbtProfileError, match="oauth"):
        connect_from_profile({"type": "bigquery", "method": "oauth-secrets", "project": "p"})


def test_bigquery_service_account_requires_a_keyfile():
    pytest.importorskip("google.cloud.bigquery", reason="needs the bigquery extra")

    with pytest.raises(DbtProfileError, match="keyfile"):
        connect_from_profile({"type": "bigquery", "method": "service-account", "project": "p"})


def test_bigquery_service_account_json_wants_an_inline_mapping():
    pytest.importorskip("google.cloud.bigquery", reason="needs the bigquery extra")

    with pytest.raises(DbtProfileError, match="keyfile_json"):
        connect_from_profile(
            {
                "type": "bigquery",
                "method": "service-account-json",
                "project": "p",
                "keyfile_json": "/not/a/mapping.json",
            }
        )


def test_bigquery_oauth_builds_a_client_from_the_profile(monkeypatch):
    """`oauth` means Application Default Credentials — the driver finds them, so
    breakdown must pass `credentials=None` rather than invent one, and must read
    `project`/`location` from the profile dbt itself would use."""
    pytest.importorskip("google.cloud.bigquery", reason="needs the bigquery extra")
    from google.cloud import bigquery

    seen = {}

    def fake_client(project=None, credentials=None, location=None):
        seen.update(project=project, credentials=credentials, location=location)
        return object()

    monkeypatch.setattr(bigquery, "Client", fake_client)
    monkeypatch.setattr("google.cloud.bigquery.dbapi.connect", lambda client: ("conn", client))

    conn = connect_from_profile(
        {"type": "bigquery", "method": "oauth", "project": "nn-prod", "location": "US"}
    )

    assert conn[0] == "conn"
    assert seen == {"project": "nn-prod", "credentials": None, "location": "US"}


def test_bigquery_reads_the_database_alias_for_project(monkeypatch):
    # Real profiles use both spellings; dbt accepts `database` as the alias.
    pytest.importorskip("google.cloud.bigquery", reason="needs the bigquery extra")
    from google.cloud import bigquery

    seen = {}
    monkeypatch.setattr(
        bigquery,
        "Client",
        lambda project=None, credentials=None, location=None: seen.update(project=project),
    )
    monkeypatch.setattr("google.cloud.bigquery.dbapi.connect", lambda client: "conn")

    connect_from_profile({"type": "bigquery", "database": "nn-prod"})

    assert seen["project"] == "nn-prod"

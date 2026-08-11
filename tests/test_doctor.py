"""Doctor checks, all offline: providers are exercised through stubs, never
a live warehouse or semantic layer."""

import pytest

from breakdown.data_fetch import WarehouseDataFetcher
from breakdown.doctor import (
    CheckResult,
    _probe_window,
    check_cloud,
    check_local,
    check_provider_extra,
    check_warehouse,
    print_report,
    run_doctor,
)
from breakdown.parser import Parser

MOCK_TREE = """
provider:
  type: mock
metrics:
  - name: revenue
    source: my.metrics.revenue
"""

WAREHOUSE_TREE = """
provider:
  type: warehouse
  host: example.cloud.databricks.com
  token: tok
  http_path: /sql/1.0/warehouses/abc
  catalog: cat
  schema: sch
metrics:
  - name: revenue
    source: cat.sch.revenue
    sql: "SELECT day AS date, SUM(x) AS value FROM t WHERE day BETWEEN :start_date AND :end_date GROUP BY day"
"""


def by_name(results):
    return {r.name: r for r in results}


def test_missing_tree_file():
    results = by_name(run_doctor("/nope/missing.yml"))
    assert results["tree file"].status == "fail"
    assert results["tree parses"].status == "skip"


def test_unset_env_var_reported_with_remediation(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCTOR_TEST_TOKEN", raising=False)
    tree = tmp_path / "tree.yml"
    tree.write_text(WAREHOUSE_TREE.replace("token: tok", "token: ${DOCTOR_TEST_TOKEN}"))
    results = by_name(run_doctor(str(tree)))
    env = results["provider env vars"]
    assert env.status == "fail"
    assert "DOCTOR_TEST_TOKEN" in env.detail
    assert "export DOCTOR_TEST_TOKEN=" in env.remediation
    assert results["tree parses"].status == "skip"


def test_invalid_yaml_fails_parse(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text("metrics: [unclosed")
    results = by_name(run_doctor(str(tree)))
    assert results["tree parses"].status == "fail"


def test_mock_tree_all_pass(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(MOCK_TREE)
    results = run_doctor(str(tree))
    # fit readiness skips without an explicit window; everything else passes
    assert all(r.status in ("pass", "skip") for r in results)
    assert any(r.status == "pass" for r in results)
    assert print_report(results) == 0


def test_print_report_exit_code_on_failure():
    assert print_report([CheckResult.fail("x", "boom")]) == 1


def test_probe_window_defaults_to_recent_week():
    import datetime

    start, end = _probe_window(None, None)
    assert end == str(datetime.date.today())
    assert start == str(datetime.date.today() - datetime.timedelta(days=7))
    assert _probe_window("2024-01-01", "2024-02-01") == ("2024-01-01", "2024-02-01")
    with pytest.raises(SystemExit, match="valid YYYY-MM-DD"):
        _probe_window("nope", None)


def test_warehouse_connection_failure_skips_sql_probes(monkeypatch):
    config = Parser(WAREHOUSE_TREE).config

    def boom(self):
        raise RuntimeError("could not reach warehouse")

    monkeypatch.setattr(WarehouseDataFetcher, "_connect", boom)
    results = by_name(check_warehouse(config, "2024-01-01", "2024-01-08"))
    assert results["auth configured"].status == "pass"
    assert results["warehouse connection"].status == "fail"
    assert "http_path" in results["warehouse connection"].remediation
    assert results["metric sql runs"].status == "skip"


def test_warehouse_missing_sql_fails(monkeypatch):
    config = Parser(
        WAREHOUSE_TREE.replace(
            '    sql: "SELECT day AS date, SUM(x) AS value FROM t WHERE day BETWEEN :start_date AND :end_date GROUP BY day"\n',
            "",
        )
    ).config
    monkeypatch.setattr(
        WarehouseDataFetcher, "_connect", lambda self: (_ for _ in ()).throw(RuntimeError("n/a"))
    )
    results = by_name(check_warehouse(config, "2024-01-01", "2024-01-08"))
    assert results["per-metric sql"].status == "fail"
    assert "revenue" in results["per-metric sql"].detail


def test_warehouse_no_auth_fails():
    config = Parser(WAREHOUSE_TREE.replace("  token: tok\n", "")).config
    results = by_name(check_warehouse(config, "2024-01-01", "2024-01-08"))
    assert results["auth configured"].status == "fail"
    assert results["warehouse connection"].status == "skip"


def test_cloud_missing_config_mentions_cell_host():
    config = Parser(MOCK_TREE.replace("type: mock", "type: cloud")).config
    results = by_name(check_cloud(config))
    assert results["cloud config"].status == "fail"
    assert "semantic-layer" in results["cloud config"].remediation
    assert results["semantic layer reachable"].status == "skip"


def test_local_missing_mf_cli(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = Parser(MOCK_TREE.replace("type: mock", "type: local")).config
    results = by_name(check_local(config))
    assert results["metricflow CLI"].status == "fail"
    assert "dbt-metricflow" in results["metricflow CLI"].remediation
    assert results["metrics listable"].status == "skip"


NONE_TREE = """
provider:
  type: none
metrics:
  - name: sessions
    source: assumed
    baseline: {low: 800, high: 1600}
  - name: signups
    source: assumed
    parents: [sessions]
    baseline: 40
    priors:
      sessions:
        distribution: "Normal"
        params: {mu: 0.03, sigma: 0.01}
"""


def test_doctor_cold_start_ready(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(NONE_TREE)
    results = {r.name: r for r in run_doctor(str(tree))}
    assert results["cold-start declarations"].status == "pass"


def test_doctor_cold_start_missing_declarations(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(NONE_TREE.replace("    baseline: 40\n", ""))
    results = {r.name: r for r in run_doctor(str(tree))}
    check = results["cold-start declarations"]
    assert check.status == "fail"
    assert "signups" in check.detail
    assert "baseline" in check.remediation


def test_doctor_fit_readiness_with_explicit_window(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(MOCK_TREE)
    # 31 whole day periods >= the fit minimum -> pass, with per-metric counts
    results = {r.name: r for r in run_doctor(str(tree), "2024-01-01", "2024-01-31")}
    check = results["fit readiness"]
    assert check.status == "pass"
    assert "revenue: 31/10 whole day periods" in check.detail

    # 5 days < minimum -> fail naming the short metric
    results = {r.name: r for r in run_doctor(str(tree), "2024-01-01", "2024-01-05")}
    check = results["fit readiness"]
    assert check.status == "fail"
    assert "revenue" in check.remediation
    assert "not fittable yet" in check.detail


def test_doctor_fit_readiness_skips_without_window(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(MOCK_TREE)
    results = {r.name: r for r in run_doctor(str(tree))}
    assert results["fit readiness"].status == "skip"
    assert "--start-date" in results["fit readiness"].detail


def test_doctor_fit_readiness_skips_cold_start(tmp_path):
    tree = tmp_path / "tree.yml"
    tree.write_text(NONE_TREE)
    results = {r.name: r for r in run_doctor(str(tree), "2024-01-01", "2024-01-31")}
    assert results["fit readiness"].status == "skip"
    assert "nothing is ever fitted" in results["fit readiness"].detail


# --- provider extras (packaging) ---


def test_provider_extra_present_is_a_pass():
    assert check_provider_extra("mock") is None
    assert check_provider_extra("none") is None


def test_missing_extra_reported_once_and_downstream_skipped(tmp_path, monkeypatch):
    """A missing extra must read as one fixable failure, not a cascade of
    connectivity failures whose remediations all point the wrong way."""
    import breakdown.data_fetch as data_fetch

    monkeypatch.setattr(
        data_fetch,
        "provider_extra_missing",
        lambda provider: (
            "provider type 'warehouse' needs the databricks extra: "
            "pip install 'metric-breakdown[databricks]'   (missing module 'databricks.sql')"
        ),
    )
    tree = tmp_path / "tree.yml"
    tree.write_text(WAREHOUSE_TREE)
    results = by_name(run_doctor(str(tree)))

    assert results["databricks extra installed"].status == "fail"
    assert "metric-breakdown[databricks]" in results["databricks extra installed"].remediation
    for name in ("auth configured", "warehouse connection", "metric sql runs"):
        assert results[name].status == "skip"
    # No lookalike failure from a check that never got to run.
    assert sum(1 for r in results.values() if r.status == "fail") == 1


def test_missing_dbt_extra_offers_the_standalone_cli_route(monkeypatch):
    import breakdown.data_fetch as data_fetch

    monkeypatch.setattr(data_fetch, "provider_extra_missing", lambda provider: "`mf` not found")
    result = check_provider_extra("local")
    assert result.status == "fail"
    assert "metric-breakdown[dbt]" in result.remediation
    # dbt-metricflow as a uv tool satisfies the local provider too, and keeps
    # dbt-core out of the analysis environment.
    assert "uv tool install dbt-metricflow" in result.remediation


# --- the `dbt` provider chain (roadmap 2.10) --------------------------------
#
# Offline like the rest of this file: the manifest and profile are written to
# tmp_path and the warehouse is a file-backed DuckDB, so the whole chain — the
# real connector included — runs in CI with no monkeypatching.

import json  # noqa: E402

from breakdown.doctor import check_dbt  # noqa: E402

pytest.importorskip("sqlglot", reason="needs the dbt-bridge extra")
duckdb = pytest.importorskip("duckdb")


def _dbt_project(tmp_path, *, metrics=None, rows="(1, DATE '2024-01-01', 5.0, 'EMEA')"):
    """A minimal dbt project on disk — dbt_project.yml, profiles.yml, a parsed
    semantic manifest — plus the DuckDB file its profile points at."""
    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE SCHEMA IF NOT EXISTS main; "
        f"CREATE TABLE main.fct_orders AS SELECT * FROM (VALUES {rows}) "
        "AS t(order_id, ordered_at, amount, region)"
    )
    con.close()

    (tmp_path / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    (tmp_path / "profiles.yml").write_text(
        f"demo:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: {db}\n"
    )
    model = {
        "name": "orders",
        # `database: null` so the relation is `main.fct_orders`, which resolves
        # in whichever DuckDB file the profile attaches.
        "node_relation": {
            "alias": "fct_orders",
            "schema_name": "main",
            "database": None,
            "relation_name": "x",
        },
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "region", "type": "categorical"},
        ],
        "measures": [{"name": "revenue", "agg": "sum", "expr": "amount"}],
    }
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    (target / "semantic_manifest.json").write_text(
        json.dumps(
            {
                "semantic_models": [model],
                "metrics": metrics
                if metrics is not None
                else [
                    {
                        "name": "revenue",
                        "type": "simple",
                        "type_params": {"measure": {"name": "revenue"}},
                    }
                ],
                "project_configuration": {"time_spine_table_configurations": [], "metadata": None},
            }
        )
    )
    return tmp_path


def _tree(project, extra="") -> str:
    return f"""
provider:
  type: dbt
  project_path: {project}
metrics:
  - name: revenue
    source: dbt.metrics.revenue
{extra}"""


def _results(tree_yaml):
    from breakdown.parser import Parser

    return {r.name: r for r in check_dbt(Parser(tree_yaml).config)}


def test_dbt_chain_passes_on_a_healthy_project(tmp_path):
    results = _results(_tree(_dbt_project(tmp_path)))
    assert [r.status for r in results.values()] == ["pass"] * 6
    assert "one row per grain" in results["grain claims hold"].detail
    assert "duckdb" in results["dbt profile"].detail


def test_missing_manifest_says_dbt_parse_and_names_the_fusion_caveat(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    results = _results(_tree(tmp_path))
    r = results["semantic manifest"]
    assert r.status == "fail"
    assert "dbt parse" in r.remediation and "legacy" in r.remediation
    # Everything downstream is skipped, not failed with the same root cause.
    assert all(results[n].status == "skip" for n in list(results)[1:])


def test_missing_project_stops_the_chain_immediately(tmp_path):
    results = _results(_tree(tmp_path / "nope"))
    assert results["semantic manifest"].status == "fail"
    assert "dbt_project.yml" in results["semantic manifest"].detail


def test_a_tree_metric_absent_from_the_manifest_is_named(tmp_path):
    project = _dbt_project(tmp_path)
    tree = _tree(project) + "  - name: ghost\n    source: dbt.metrics.not_a_metric\n"
    results = _results(tree)
    r = results["tree metrics bind"]
    assert r.status == "fail" and "not_a_metric" in r.detail
    assert results["grain claims hold"].status == "skip"


def test_a_declared_dimension_that_does_not_exist_fails_before_the_first_click(tmp_path):
    # Without this check the failure is a 500 the first time someone clicks
    # "slice by" — the same too-late class as C12.
    project = _dbt_project(tmp_path)
    tree = _tree(project, extra="    dimensions:\n      plan: subscription__tier\n")
    r = _results(tree)["declared dimensions exist"]
    assert r.status == "fail" and "revenue.plan" in r.detail


def test_a_declared_dimension_that_does_exist_passes(tmp_path):
    project = _dbt_project(tmp_path)
    tree = _tree(project, extra="    dimensions:\n      region: region\n")
    assert _results(tree)["declared dimensions exist"].status == "pass"


def test_fan_out_is_reported_as_a_failed_grain_claim(tmp_path):
    # The check MetricFlow and Cube structurally cannot make: they accept a
    # declared relationship on trust, so a relation that is not one row per
    # grain silently multiplies every aggregate over it.
    project = _dbt_project(
        tmp_path,
        rows="(1, DATE '2024-01-01', 5.0, 'EMEA'), (1, DATE '2024-01-01', 5.0, 'AMER')",
    )
    r = _results(_tree(project))["grain claims hold"]
    assert r.status == "fail"
    assert "2 rows / 1 distinct" in r.detail
    assert "silently multiplied" in r.remediation


def test_an_unresolvable_profile_stops_before_connecting(tmp_path):
    project = _dbt_project(tmp_path)
    (project / "profiles.yml").write_text(
        "demo:\n  target: dev\n  outputs:\n    dev:\n"
        "      type: duckdb\n      path: \"{{ env_var('NOT_SET_ANYWHERE') }}\"\n"
    )
    results = _results(_tree(project))
    assert results["dbt profile"].status == "fail"
    assert "NOT_SET_ANYWHERE" in results["dbt profile"].detail
    assert results["warehouse connection"].status == "skip"


def test_the_dbt_provider_reports_its_own_extra(monkeypatch):
    import breakdown.data_fetch as df
    from breakdown.doctor import check_provider_extra

    monkeypatch.setattr(df, "provider_extra_missing", lambda p: "missing module 'sqlglot'")
    r = check_provider_extra("dbt")
    assert r.status == "fail"
    assert "metric-breakdown[dbt-bridge]" in r.remediation

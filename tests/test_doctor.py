"""Doctor checks, all offline: providers are exercised through stubs, never
a live warehouse or semantic layer."""

import pytest

from breakdown.data_fetch import WarehouseDataFetcher
from breakdown.doctor import (
    CheckResult,
    _probe_window,
    check_cloud,
    check_local,
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
    assert all(r.status == "pass" for r in results)
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
    config = Parser(
        WAREHOUSE_TREE.replace("  token: tok\n", "")
    ).config
    results = by_name(check_warehouse(config, "2024-01-01", "2024-01-08"))
    assert results["auth configured"].status == "fail"
    assert results["warehouse connection"].status == "skip"


def test_cloud_missing_config_mentions_cell_host():
    config = Parser(
        MOCK_TREE.replace("type: mock", "type: cloud")
    ).config
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

"""The duckdb provider: per-metric SQL over a folder of CSV/Parquet exports."""
import textwrap

import pandas as pd
import pytest

pytest.importorskip("duckdb")

from breakdown.api.main import _build_fetcher  # noqa: E402
from breakdown.data_fetch import DuckDBDataFetcher, query_name_for  # noqa: E402
from breakdown.doctor import run_doctor  # noqa: E402
from breakdown.parser import Parser, resolve_relative_paths  # noqa: E402

ORDERS_CSV = """\
created_at,status,quantity
2025-06-02T10:00:00,paid,2
2025-06-02T11:30:00,paid,1
2025-06-02T12:00:00,test,99
2025-06-04T09:00:00,paid,5
"""

DAILY_SQL = """
SELECT CAST(created_at AS DATE) AS date, SUM(quantity) AS value
FROM orders
WHERE status != 'test'
  AND CAST(created_at AS DATE) BETWEEN :start_date AND :end_date
GROUP BY 1
"""


@pytest.fixture
def exports(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    (d / "orders.csv").write_text(ORDERS_CSV)
    return d


def test_daily_flow_zero_fills_interior_and_trims_trailing(exports):
    fetcher = DuckDBDataFetcher(str(exports), {"tickets": DAILY_SQL})
    df = fetcher.fetch_metric("tickets", "2025-06-01", "2025-06-05")
    # Jun 1 and 3 are interior gaps -> 0; Jun 5 is after the last row -> trimmed.
    assert list(df.columns) == ["date", "tickets"]
    assert df["tickets"].tolist() == [0.0, 3.0, 0.0, 5.0]
    assert fetcher.tables == {"orders": "orders.csv"}


def test_weekly_date_trunc_lands_on_monday(exports):
    sql = """
    SELECT DATE_TRUNC('week', CAST(created_at AS DATE)) AS date, SUM(quantity) AS value
    FROM orders WHERE status != 'test'
      AND CAST(created_at AS DATE) BETWEEN :start_date AND :end_date
    GROUP BY 1
    """
    df = DuckDBDataFetcher(str(exports), {"m": sql}).fetch_metric(
        "m", "2025-06-02", "2025-06-15", grain="week"
    )
    assert df["m"].tolist() == [8.0]
    assert all(d.dayofweek == 0 for d in df["date"])


def test_type_casts_are_not_mistaken_for_placeholders(exports):
    sql = """
    SELECT created_at::DATE AS date, SUM(quantity) AS value
    FROM orders WHERE status != 'test'
      AND created_at::DATE BETWEEN :start_date AND :end_date
    GROUP BY 1
    """
    df = DuckDBDataFetcher(str(exports), {"m": sql}).fetch_metric("m", "2025-06-01", "2025-06-05")
    assert df["m"].sum() == 8.0


def test_parquet_files_are_tables(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    pd.DataFrame(
        {"day": pd.to_datetime(["2025-06-02", "2025-06-03"]).date, "spend": [10.0, 20.0]}
    ).to_parquet(d / "ad_spend.parquet")
    sql = "SELECT day AS date, spend AS value FROM ad_spend WHERE day BETWEEN :start_date AND :end_date"
    df = DuckDBDataFetcher(str(d), {"spend": sql}).fetch_metric("spend", "2025-06-02", "2025-06-03")
    assert df["spend"].tolist() == [10.0, 20.0]


def test_missing_dir_and_empty_dir_fail_clearly(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        DuckDBDataFetcher(str(tmp_path / "nope"), {"m": DAILY_SQL}).fetch_metric(
            "m", "2025-06-01", "2025-06-05"
        )
    with pytest.raises(RuntimeError, match="No .csv or .parquet"):
        DuckDBDataFetcher(str(tmp_path), {"m": DAILY_SQL}).fetch_metric(
            "m", "2025-06-01", "2025-06-05"
        )


def test_stem_collision_is_rejected(exports):
    pd.DataFrame({"x": [1]}).to_parquet(exports / "orders.parquet")
    with pytest.raises(RuntimeError, match="map to table 'orders'"):
        DuckDBDataFetcher(str(exports), {"m": DAILY_SQL}).fetch_metric(
            "m", "2025-06-01", "2025-06-05"
        )


# --- config wiring ---

TREE = textwrap.dedent(
    """
    provider:
      type: duckdb
      data_dir: ./exports
    metrics:
      - name: tickets
        source: orders.tickets
        sql: |
    {sql}
    """
)


def _tree_yaml():
    return TREE.format(sql=textwrap.indent(DAILY_SQL.strip(), " " * 10))


def test_duckdb_requires_data_dir():
    with pytest.raises(ValueError, match="requires `data_dir`"):
        Parser("provider:\n  type: duckdb\nmetrics:\n  - name: a\n    source: a\n    sql: select 1\n")


def test_relative_data_dir_resolves_against_tree(tmp_path, exports):
    tree_path = tmp_path / "tree.yml"
    parser = Parser(_tree_yaml())
    resolve_relative_paths(parser.config, str(tree_path))
    assert parser.config.provider.data_dir == str(exports)

    fetcher = _build_fetcher(parser.config.provider, parser.dag, parser.config.metrics)
    assert isinstance(fetcher, DuckDBDataFetcher)
    # duckdb keys metrics by tree name, like warehouse — not by `source`.
    assert query_name_for(parser.config.metrics[0], "duckdb") == "tickets"


def test_doctor_end_to_end(tmp_path, exports):
    tree_path = tmp_path / "tree.yml"
    tree_path.write_text(_tree_yaml())
    results = {r.name: r for r in run_doctor(str(tree_path), "2025-06-01", "2025-06-05")}

    assert results["duckdb extra installed"].status == "pass"
    assert results["data files"].status == "pass"
    assert "orders (orders.csv)" in results["data files"].detail
    assert results["metric sql runs"].status == "pass"
    # Four days of data is below the fit minimum: doctor says so, honestly.
    assert results["fit readiness"].status == "fail"
    assert "tickets: 4/" in results["fit readiness"].detail


def test_doctor_reports_missing_folder(tmp_path):
    tree_path = tmp_path / "tree.yml"
    tree_path.write_text(_tree_yaml())  # no exports/ folder created
    results = {r.name: r for r in run_doctor(str(tree_path))}
    assert results["data files"].status == "fail"
    assert results["metric sql runs"].status == "skip"

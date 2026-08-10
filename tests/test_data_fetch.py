import numpy as np
import pandas as pd
import pytest

from breakdown.data_fetch import (
    CloudDataFetcher,
    LocalDataFetcher,
    MissingProviderExtra,
    MockDataFetcher,
    WarehouseDataFetcher,
    provider_extra_missing,
)
from breakdown.parser import Parser

# Provider SDKs are optional extras, so the handful of tests that need a real
# one skip on a base install rather than failing it. Everything else — the mock
# provider, the warehouse gap/alignment rules driven by stub cursors — runs
# without any extra installed.
needs_dbt_extra = pytest.mark.skipif(
    provider_extra_missing("cloud") is not None,
    reason="requires the `dbt` extra (pip install 'metric-breakdown[dbt]')",
)
needs_mf_cli = pytest.mark.skipif(
    provider_extra_missing("local") is not None,
    reason="requires the MetricFlow `mf` CLI on PATH",
)
needs_databricks_extra = pytest.mark.skipif(
    provider_extra_missing("warehouse") is not None,
    reason="requires the `databricks` extra (pip install 'metric-breakdown[databricks]')",
)

TREE_YAML = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
  - name: average_order_value
    source: dbt.metric.average_order_value
    # Explicitly flow, against its own name: this fixture exists to pin the
    # flow/stock generation path byte-for-byte, and declaring the kind is also
    # how an author silences the parse-time rate-name lint.
    kind: flow
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""


def test_mock_fetcher_returns_data():
    fetcher = MockDataFetcher()
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert not df.empty
    assert "revenue" in df.columns
    assert "date" in df.columns


def test_mock_fetcher_deterministic():
    fetcher = MockDataFetcher()
    df1 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    df2 = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert df1["revenue"].equals(df2["revenue"])


def test_mock_fetcher_invalid_date_range():
    fetcher = MockDataFetcher()
    with pytest.raises(ValueError, match="end_date"):
        fetcher.fetch_metric("revenue", "2024-03-31", "2024-01-01")


def test_mock_fetcher_tree_aware_formula_holds():
    """With a DAG, formula nodes should approximately satisfy their formula."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)

    revenue = fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")["revenue"]
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-03-31")["order_count"]
    aov = fetcher.fetch_metric("average_order_value", "2024-01-01", "2024-03-31")[
        "average_order_value"
    ]

    corr = np.corrcoef(revenue, orders * aov)[0, 1]
    assert corr > 0.99


def test_mock_fetcher_tree_aware_parent_child_correlated():
    """Probabilistic children should co-move with their parents."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)

    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-03-31")["daily_sessions"]
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-03-31")["order_count"]

    corr = np.corrcoef(sessions, orders)[0, 1]
    assert corr > 0.9


def test_mock_fetcher_tree_aware_deterministic():
    parser = Parser(TREE_YAML)
    df1 = MockDataFetcher(dag=parser.dag).fetch_metric("revenue", "2024-01-01", "2024-03-31")
    df2 = MockDataFetcher(dag=parser.dag).fetch_metric("revenue", "2024-01-01", "2024-03-31")
    assert df1["revenue"].equals(df2["revenue"])


def test_mock_fetcher_unknown_metric_falls_back_to_random_walk():
    """Metrics not in the DAG (or no DAG at all) still return data."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    df = fetcher.fetch_metric("not_in_tree", "2024-01-01", "2024-01-31")
    assert "not_in_tree" in df.columns
    assert len(df) == 31


def test_mock_fetcher_lagged_child_correlates_with_shifted_parent():
    """With a lagged edge, the generated child should correlate more strongly
    with the lag-shifted parent than with the contemporaneous parent."""
    yaml_content = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
    lags: { daily_sessions: 5 }
"""
    parser = Parser(yaml_content)
    fetcher = MockDataFetcher(dag=parser.dag)
    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-04-30")[
        "daily_sessions"
    ].values
    orders = fetcher.fetch_metric("order_count", "2024-01-01", "2024-04-30")["order_count"].values

    L = 5
    corr_lagged = np.corrcoef(orders[L:], sessions[:-L])[0, 1]
    corr_contemp = np.corrcoef(orders[L:], sessions[L:])[0, 1]
    assert corr_lagged > corr_contemp


@needs_dbt_extra
def test_cloud_fetcher_requires_credentials():
    """CloudDataFetcher.__init__ should raise when dbtsl client rejects bad credentials."""
    with pytest.raises(Exception):
        fetcher = CloudDataFetcher(environment_id="0", host="invalid.host", token="bad-token")
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")


@needs_mf_cli
def test_local_fetcher_raises_on_bad_project():
    """LocalDataFetcher should raise RuntimeError when mf fails on a non-existent project."""
    fetcher = LocalDataFetcher(project_path="/tmp/nonexistent_dbt_project")
    with pytest.raises(RuntimeError, match="mf query failed"):
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-03-31")


# --- warehouse provider ---

WAREHOUSE_TREE = """
provider:
  type: warehouse
  host: ${TEST_WH_HOST}
  http_path: /sql/1.0/warehouses/abc
  token: ${TEST_WH_TOKEN}
  catalog: narrative
  schema: default
metrics:
  - name: new_mrr
    source: narrative.default.fct_mrr_movements
    sql: "SELECT day AS date, SUM(new_mrr_usd) AS value FROM fct_mrr_movements WHERE day BETWEEN :start_date AND :end_date GROUP BY day"
"""


def test_provider_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_WH_HOST", "dbc-xyz.databricks.com")
    monkeypatch.setenv("TEST_WH_TOKEN", "dapi-secret")
    cfg = Parser(WAREHOUSE_TREE).config.provider
    assert cfg.type == "warehouse"
    assert cfg.host == "dbc-xyz.databricks.com"
    assert cfg.token == "dapi-secret"
    assert cfg.db_schema == "default"  # `schema` in YAML, aliased to avoid BaseModel clash


def test_provider_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("TEST_WH_HOST", raising=False)
    monkeypatch.setenv("TEST_WH_TOKEN", "dapi-secret")
    with pytest.raises(Exception, match="TEST_WH_HOST"):
        Parser(WAREHOUSE_TREE)


class _StubCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("date",), ("value",)]
        self.executed = None

    def execute(self, sql, parameters=None):
        self.executed = (sql, parameters)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def test_warehouse_fetcher_reindexes_and_zero_fills(monkeypatch):
    import datetime

    # Two movement days inside a five-day window: the interior gap becomes 0;
    # the trailing day after the last returned row is trimmed (not-yet-loaded,
    # not zero).
    rows = [
        (datetime.date(2025, 6, 2), 100.0),
        (datetime.date(2025, 6, 4), 250.0),
    ]
    cursor = _StubCursor(rows)
    fetcher = WarehouseDataFetcher(
        host="h",
        http_path="p",
        token="t",
        metric_sql={"new_mrr": "SELECT ... :start_date ... :end_date"},
    )
    monkeypatch.setattr(fetcher, "_cursor", lambda: cursor)

    df = fetcher.fetch_metric("new_mrr", "2025-06-01", "2025-06-05")

    assert list(df.columns) == ["date", "new_mrr"]
    assert len(df) == 4  # daily range through the last observed row
    assert df["new_mrr"].tolist() == [0.0, 100.0, 0.0, 250.0]
    # window bounds were bound as named parameters, not string-formatted
    assert cursor.executed[1] == {"start_date": "2025-06-01", "end_date": "2025-06-05"}


def test_warehouse_fetcher_unknown_metric_raises():
    fetcher = WarehouseDataFetcher(host="h", http_path="p", token="t", metric_sql={})
    with pytest.raises(RuntimeError, match="No `sql` defined"):
        fetcher.fetch_metric("missing", "2025-06-01", "2025-06-05")


def test_warehouse_fetcher_requires_auth():
    # Neither a PAT token nor an OAuth profile → construction must fail loudly.
    with pytest.raises(ValueError, match="token.*or.*profile|profile"):
        WarehouseDataFetcher(host="h", http_path="p", token=None, metric_sql={})


@needs_databricks_extra
def test_warehouse_fetcher_profile_uses_credentials_provider(monkeypatch):
    """With a `profile`, the connector is called with an OAuth credentials
    provider (not access_token), and host defaults to the profile's host."""
    captured = {}

    class _FakeConfig:
        def __init__(self, profile=None):
            captured["profile"] = profile
            self.host = "https://dbc-fake.cloud.databricks.com/"

        def authenticate(self):  # HeaderFactory stand-in
            return {}

    class _StubConnection:
        def cursor(self):
            return _StubCursor([])

    def _fake_connect(**kwargs):
        captured["connect_kwargs"] = kwargs
        return _StubConnection()

    import databricks.sql as dbsql_mod
    from databricks.sdk import core as sdk_core

    monkeypatch.setattr(sdk_core, "Config", _FakeConfig)
    monkeypatch.setattr(dbsql_mod, "connect", _fake_connect)

    fetcher = WarehouseDataFetcher(
        host=None,
        http_path="/sql/1.0/warehouses/x",
        token=None,
        metric_sql={},
        profile="narrative",
    )
    fetcher._cursor()

    assert captured["profile"] == "narrative"
    kw = captured["connect_kwargs"]
    assert "access_token" not in kw
    assert callable(kw["credentials_provider"])
    # host resolved from the profile, with scheme/trailing slash stripped
    assert kw["server_hostname"] == "dbc-fake.cloud.databricks.com"


# --- grain-aware providers (1.7) ---

MIXED_TREE = """
metrics:
  - name: trial_starts
    source: dbt.metric.trial_starts
  - name: trial_conversion_rate
    source: dbt.metric.trial_conversion_rate
    grain: week
    kind: rate
  - name: conversions
    source: dbt.metric.conversions
    grain: week
    formula: "trial_starts * trial_conversion_rate"
    parents: [trial_starts, trial_conversion_rate]
"""


def test_mock_all_day_tree_pinned_values():
    """Grain-aware generation must leave all-day trees byte-identical: golden
    values captured from the pre-grain generator."""
    parser = Parser(TREE_YAML)
    fetcher = MockDataFetcher(dag=parser.dag)
    df = fetcher.fetch_metric("revenue", "2024-01-01", "2024-04-09")
    assert len(df) == 100
    np.testing.assert_allclose(
        df["revenue"].head(3).to_numpy(),
        [793860.4244541493, 959272.2559016624, 1017094.0476589967],
        rtol=0,
        atol=1e-6,
    )
    np.testing.assert_allclose(df["revenue"].sum(), 79884063.93374, atol=1e-4)
    sessions = fetcher.fetch_metric("daily_sessions", "2024-01-01", "2024-04-09")
    np.testing.assert_allclose(
        sessions["daily_sessions"].head(3).to_numpy(),
        [1890.3967010755, 2019.680210048, 2028.7438671543],
        rtol=0,
        atol=1e-6,
    )


def test_mock_mixed_grain_identity_holds_at_node_grain():
    """A weekly formula node satisfies its formula against the daily flow
    parent summed to weeks and the native weekly rate."""
    parser = Parser(MIXED_TREE)
    fetcher = MockDataFetcher(dag=parser.dag)
    window = ("2024-01-01", "2024-03-31")

    conv = fetcher.fetch_metric("conversions", *window, grain="week")
    starts = fetcher.fetch_metric("trial_starts", *window)
    rate = fetcher.fetch_metric("trial_conversion_rate", *window, grain="week", kind="rate")

    weekly_starts = (
        starts.set_index("date")["trial_starts"]
        .resample("W-MON", label="left", closed="left")
        .sum()
    )
    joined = (
        conv.set_index("date")
        .join(weekly_starts, how="inner")
        .join(rate.set_index("date"), how="inner")
    )
    expected = joined["trial_starts"] * joined["trial_conversion_rate"]
    assert len(joined) >= 10
    # Generator adds 2% noise to the identity; with ~13 weekly points assert
    # a tight relative residual rather than a fragile correlation threshold.
    rel_err = np.abs(joined["conversions"] - expected) / np.abs(expected)
    assert rel_err.median() < 0.05
    assert np.corrcoef(joined["conversions"], expected)[0, 1] > 0.95
    # Weekly labels are period starts (Mondays).
    assert all(d.dayofweek == 0 for d in conv["date"])


def test_mock_fetcher_coarse_fallback_walk():
    """Non-DAG metrics generate at the requested grain."""
    fetcher = MockDataFetcher()
    df = fetcher.fetch_metric("anything", "2024-01-01", "2024-03-31", grain="week")
    assert all(d.dayofweek == 0 for d in df["date"])
    assert len(df) == 13  # whole weeks fully inside Mon Jan 1 .. Sun Mar 31


def _wh_fetcher(rows):
    import datetime  # noqa: F401

    cursor = _StubCursor(rows)
    fetcher = WarehouseDataFetcher(
        host="h",
        http_path="p",
        token="t",
        metric_sql={"m": "SELECT ... :start_date ... :end_date"},
    )
    fetcher._cursor = lambda: cursor
    return fetcher


def test_warehouse_weekly_flow_zero_fills_interior_trims_trailing():
    import datetime

    rows = [
        (datetime.date(2024, 1, 1), 100.0),  # Monday
        (datetime.date(2024, 1, 15), 250.0),  # Monday, gap week between
    ]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")
    # Interior gap (Jan 8) -> 0; trailing week (Jan 22) trimmed as unloaded.
    assert df["m"].tolist() == [100.0, 0.0, 250.0]
    assert all(d.dayofweek == 0 for d in df["date"])


def test_warehouse_weekly_stock_forward_fills():
    import datetime

    rows = [
        (datetime.date(2024, 1, 1), 100.0),
        (datetime.date(2024, 1, 15), 250.0),
    ]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="stock")
    assert df["m"].tolist() == [100.0, 100.0, 250.0]


def test_warehouse_empty_result_keeps_full_zero_fill_for_flow():
    """No rows at all is a legitimate all-quiet flow window (e.g. a coverage
    cliff) — keep the full zero spine rather than trimming to nothing."""
    df = _wh_fetcher([]).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")
    assert df["m"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_warehouse_stock_leading_gap_raises():
    import datetime

    rows = [(datetime.date(2024, 1, 15), 250.0)]
    with pytest.raises(RuntimeError, match="no value at or before the first week period"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="stock")


def test_warehouse_rate_missing_interior_period_raises():
    import datetime

    # Interior gap (Jan 8) between returned rows: a rate cannot be invented.
    # (A trailing gap is trimmed like any other kind — data not loaded yet.)
    rows = [
        (datetime.date(2024, 1, 1), 0.5),
        (datetime.date(2024, 1, 15), 0.6),
    ]
    with pytest.raises(RuntimeError, match="Rate metric 'm' is missing week periods"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week", kind="rate")


def test_warehouse_misaligned_labels_raise():
    import datetime

    rows = [(datetime.date(2024, 1, 3), 100.0)]  # a Wednesday at week grain
    with pytest.raises(RuntimeError, match="not aligned to period starts"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")


def test_warehouse_partial_period_rows_dropped():
    """Rows for periods only partially inside the window are dropped, not
    zero-filled into fake periods."""
    import datetime

    rows = [
        (datetime.date(2024, 1, 1), 100.0),
        (datetime.date(2024, 1, 8), 200.0),
    ]
    # Window ends Wednesday Jan 10: the Jan-8 week is partial.
    df = _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-10", grain="week")
    assert df["m"].tolist() == [100.0]


# --- provider date alignment (roadmap C1/C2) ---
#
# Every case below produced a *plausible wrong number* rather than an error.
# They are grouped here because they share one cause: the spine/trim/fill
# contract lived inside the warehouse fetcher, so the semantic-layer providers
# never got it, and the alignment guard that should have caught the tz case
# compared two values that were both tz-aware.


def test_warehouse_tz_aware_dates_do_not_become_zeros():
    """A tz-aware date column used to zero out the whole metric (C1).

    `floor_period` calls `.normalize()`, which preserves tzinfo, so a tz-aware
    midnight passed the alignment guard. It then matched nothing on the
    tz-naive spine, `notna().any()` was False so the trailing trim was skipped,
    and `fillna(0.0)` returned a full spine of zeros — silently, and then
    snapshotted.
    """
    rows = [
        (pd.Timestamp("2024-06-03", tz="UTC"), 100.0),
        (pd.Timestamp("2024-06-04", tz="UTC"), 250.0),
    ]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-06-03", "2024-06-04")

    assert df["m"].tolist() == [100.0, 250.0]
    assert df["date"].tolist() == [pd.Timestamp("2024-06-03"), pd.Timestamp("2024-06-04")]
    assert df["date"].dt.tz is None


def test_warehouse_tz_aware_offset_keeps_the_labelled_date():
    """A non-UTC session timezone must not shift the label. The warehouse means
    the wall-clock date it put on the row, so we drop the zone rather than
    converting through UTC (which would move a +09:00 midnight to the previous
    day)."""
    rows = [(pd.Timestamp("2024-06-03 00:00:00", tz="Asia/Tokyo"), 100.0)]
    df = _wh_fetcher(rows).fetch_metric("m", "2024-06-03", "2024-06-03")

    assert df["date"].tolist() == [pd.Timestamp("2024-06-03")]
    assert df["m"].tolist() == [100.0]


def test_warehouse_rows_entirely_off_the_spine_raise():
    """SQL that ignores its bound parameters returns rows for the wrong window;
    every row then falls off the spine and the old code zero-filled the lot.
    An empty *result* is legitimate (all-quiet flow window); rows that all miss
    is a bug in the query, and must say so."""
    import datetime

    rows = [
        (datetime.date(2024, 5, 1), 100.0),
        (datetime.date(2024, 5, 2), 250.0),
    ]
    with pytest.raises(RuntimeError, match="none of which fall on"):
        _wh_fetcher(rows).fetch_metric("m", "2024-06-01", "2024-06-05")


def test_warehouse_interior_gap_fill_warns(caplog):
    """Filling an interior hole is a judgement call, not a fact — say so. A
    three-day ETL outage is otherwise indistinguishable from a real collapse,
    and RCA will name the metric as the root cause."""
    import datetime
    import logging

    rows = [
        (datetime.date(2024, 1, 1), 100.0),
        (datetime.date(2024, 1, 15), 250.0),
    ]
    with caplog.at_level(logging.WARNING, logger="breakdown.data_fetch"):
        _wh_fetcher(rows).fetch_metric("m", "2024-01-01", "2024-01-28", grain="week")

    assert "interior" in caplog.text.lower()
    assert "2024-01-08" in caplog.text  # names the period it invented


# -- the semantic-layer providers never snapped at all (C2) --


def _sl_frame(dates, values, grain="week", name="m"):
    return pd.DataFrame({f"metric_time__{grain}": dates, name: values})


def _local_fetcher(frame, monkeypatch):
    fetcher = LocalDataFetcher(project_path="/unused")
    monkeypatch.setattr(fetcher, "_run_mf_query", lambda *a, **k: frame.copy())
    return fetcher


def _cloud_fetcher(frame):
    import contextlib

    class _StubTable:
        def to_pandas(self):
            return frame.copy()

    class _StubClient:
        def session(self):
            return contextlib.nullcontext()

        def query(self, **kwargs):
            return _StubTable()

    fetcher = object.__new__(CloudDataFetcher)  # bypass SDK construction
    fetcher.client = _StubClient()
    return fetcher


def test_local_fetcher_drops_partial_edge_periods(monkeypatch):
    """A window ending mid-week used to hand back a two-day partial week as a
    full row at ~2/7 normal volume, which `snap_window` then treated as whole —
    a manufactured −71% gap."""
    frame = _sl_frame([pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-08")], [100.0, 30.0])
    df = _local_fetcher(frame, monkeypatch).fetch_metric(
        "m", "2024-01-01", "2024-01-10", grain="week"
    )
    assert df["m"].tolist() == [100.0]


def test_cloud_fetcher_drops_partial_edge_periods():
    frame = _sl_frame([pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-08")], [100.0, 30.0])
    df = _cloud_fetcher(frame).fetch_metric("m", "2024-01-01", "2024-01-10", grain="week")
    assert df["m"].tolist() == [100.0]


def test_local_fetcher_fills_interior_gap_and_trims_trailing(monkeypatch):
    frame = _sl_frame([pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-15")], [100.0, 250.0])
    df = _local_fetcher(frame, monkeypatch).fetch_metric(
        "m", "2024-01-01", "2024-01-28", grain="week"
    )
    # Interior Jan-8 week -> 0; trailing Jan-22 week trimmed as not-yet-loaded.
    assert df["m"].tolist() == [100.0, 0.0, 250.0]


def test_cloud_fetcher_stock_forward_fills_interior_gap():
    frame = _sl_frame([pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-15")], [100.0, 250.0])
    df = _cloud_fetcher(frame).fetch_metric(
        "m", "2024-01-01", "2024-01-28", grain="week", kind="stock"
    )
    assert df["m"].tolist() == [100.0, 100.0, 250.0]


def test_local_fetcher_rate_missing_period_raises(monkeypatch):
    """A rate cannot be gap-filled — the same rule the warehouse path already
    enforced, now shared."""
    frame = _sl_frame([pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-15")], [0.5, 0.6])
    with pytest.raises(RuntimeError, match="Rate metric 'm' is missing week periods"):
        _local_fetcher(frame, monkeypatch).fetch_metric(
            "m", "2024-01-01", "2024-01-28", grain="week", kind="rate"
        )


def test_cloud_fetcher_tz_aware_dates_do_not_become_zeros():
    """The semantic layer returns tz-aware timestamps for some warehouses."""
    frame = _sl_frame(
        [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-08", tz="UTC")],
        [100.0, 250.0],
    )
    df = _cloud_fetcher(frame).fetch_metric("m", "2024-01-01", "2024-01-14", grain="week")
    assert df["m"].tolist() == [100.0, 250.0]
    assert df["date"].dt.tz is None


# --- sliced-fetch reshape (_sliced_long) ---


def test_sliced_long_reshapes_semantic_layer_frame():
    import pandas as pd

    from breakdown.data_fetch import _sliced_long

    raw = pd.DataFrame(
        {
            "METRIC_TIME__DAY": ["2026-01-05", "2026-01-05", "2026-01-06"],
            "CUSTOMER__REGION": ["emea", "amer", None],
            "signups": [10.0, 20.0, 5.0],
        }
    )
    out = _sliced_long(raw, "signups", "day")
    assert list(out.columns) == ["date", "slice", "value"]
    assert set(out["slice"]) == {"emea", "amer", "__null__"}
    assert out["value"].dtype == float
    assert out["date"].is_monotonic_increasing


def test_sliced_long_ambiguous_columns_raise():
    import pandas as pd

    from breakdown.data_fetch import _sliced_long

    raw = pd.DataFrame(
        {
            "METRIC_TIME__DAY": ["2026-01-05"],
            "CUSTOMER__REGION": ["emea"],
            "CUSTOMER__PLAN": ["pro"],
            "signups": [10.0],
        }
    )
    with pytest.raises(RuntimeError, match="exactly one dimension column"):
        _sliced_long(raw, "signups", "day")


# --- provider extras (packaging) ---
#
# Provider SDKs ship as `[dbt]` / `[databricks]` extras, so the base install
# has to fail with an instruction rather than an ImportError traceback.


def test_local_fetcher_without_mf_names_the_extra(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    fetcher = LocalDataFetcher(project_path="/tmp/whatever")
    with pytest.raises(MissingProviderExtra) as exc:
        fetcher.fetch_metric("revenue", "2024-01-01", "2024-01-31")
    message = str(exc.value)
    assert "provider type 'local' needs the dbt extra" in message
    assert "pip install 'metric-breakdown[dbt]'" in message


def test_cloud_fetcher_without_dbtsl_names_the_extra(monkeypatch):
    import sys

    # A None entry makes `import dbtsl` raise ImportError, which is what an
    # uninstalled extra looks like from inside the fetcher.
    monkeypatch.setitem(sys.modules, "dbtsl", None)
    with pytest.raises(MissingProviderExtra) as exc:
        CloudDataFetcher(environment_id="1", host="h", token="t")
    message = str(exc.value)
    assert "provider type 'cloud' needs the dbt extra" in message
    assert "pip install 'metric-breakdown[dbt]'" in message


def test_warehouse_fetcher_without_databricks_names_the_extra(monkeypatch):
    """Building the fetcher is pure config; the driver is needed to connect,
    which is where the base install has to explain itself."""
    import sys

    monkeypatch.setitem(sys.modules, "databricks.sql", None)
    fetcher = WarehouseDataFetcher(host="h", http_path="p", token="tok", metric_sql={})
    with pytest.raises(MissingProviderExtra) as exc:
        fetcher._connect()
    message = str(exc.value)
    assert "provider type 'warehouse' needs the databricks extra" in message
    assert "pip install 'metric-breakdown[databricks]'" in message


def test_missing_extra_is_not_a_bare_import_error():
    """MissingProviderExtra stays a RuntimeError so existing provider-error
    handling keeps working, but is distinguishable from a broken install."""
    assert issubclass(MissingProviderExtra, RuntimeError)
    assert not issubclass(MissingProviderExtra, ImportError)


def test_extra_lookup_is_none_for_providers_without_dependencies():
    assert provider_extra_missing("mock") is None
    assert provider_extra_missing("none") is None


def test_api_import_does_not_pull_provider_sdks():
    """The base install must be able to start the server. `breakdown.api.main`
    imports data_fetch, so any module-scope provider import there would make
    `pip install metric-breakdown` unusable without the extras."""
    import subprocess
    import sys

    code = (
        "import sys; import breakdown.api.main; "
        "leaked = [m for m in sys.modules "
        "if m == 'dbtsl' or m.split('.')[0] in ('databricks', 'dbt', 'metricflow')]; "
        "print(leaked)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", f"provider SDKs imported eagerly: {proc.stdout}"


def test_local_fetcher_constructs_without_mf_so_snapshots_still_serve():
    """A tree can name the `local` provider and never query it — the snapshot
    cache serves committed parquet with no dbt project or `mf` in sight. Only
    an actual query may demand the extra."""
    import shutil

    real_which = shutil.which
    try:
        shutil.which = lambda name: None
        LocalDataFetcher(project_path="/tmp/nonexistent")  # must not raise
    finally:
        shutil.which = real_which


# --- Kind-aware generation (roadmap C11) -------------------------------------

C11_TREE = """
metrics:
  - name: visitors
    source: mock.visitors
  - name: signup_rate
    source: mock.signup_rate
    kind: rate
  - name: signups
    source: mock.signups
    formula: "visitors * signup_rate"
    parents: [visitors, signup_rate]
  - name: trial_rate
    source: mock.trial_rate
    kind: rate
  - name: trials
    source: mock.trials
    formula: "signups * trial_rate"
    parents: [signups, trial_rate]
  - name: average_order_value
    source: mock.average_order_value
    kind: rate
  - name: time_to_first_response
    source: mock.time_to_first_response
    kind: rate
"""


def test_mock_rate_leaves_are_generated_on_a_rate_scale():
    """C11: a `kind: rate` leaf used to be drawn from `uniform(50, 5000)` — the
    same scale as an impression count."""
    parser = Parser(C11_TREE)
    fetcher = MockDataFetcher(dag=parser.dag)
    window = ("2022-01-01", "2024-12-31")

    for share in ("signup_rate", "trial_rate"):
        v = fetcher.fetch_metric(share, *window, kind="rate")[share]
        assert v.min() > 0, share
        assert v.max() < 1, share

    # Durations and averages are ratios too, but not shares — they only have to
    # stay positive and on a human scale.
    for magnitude in ("average_order_value", "time_to_first_response"):
        v = fetcher.fetch_metric(magnitude, *window, kind="rate")[magnitude]
        assert 0 < v.min()
        assert v.max() < 1e4, magnitude


def test_mock_count_times_rate_funnel_does_not_compound():
    """The C11 defect itself: each `count × rate` level multiplied the scale by
    ~10³, so the reference tree's apex reached 10²⁵."""
    parser = Parser(C11_TREE)
    fetcher = MockDataFetcher(dag=parser.dag)
    window = ("2022-01-01", "2024-12-31")

    visitors = fetcher.fetch_metric("visitors", *window)["visitors"]
    signups = fetcher.fetch_metric("signups", *window)["signups"]
    trials = fetcher.fetch_metric("trials", *window)["trials"]

    # Each level multiplies by a share, so the funnel narrows monotonically
    # instead of exploding.
    assert signups.mean() < visitors.mean()
    assert trials.mean() < signups.mean()
    assert trials.mean() > 0


PRIOR_SIGN_TREE = """
metrics:
  - name: page_load_seconds
    source: mock.page_load_seconds
    kind: rate
  - name: engaged_share
    source: mock.engaged_share
    kind: rate
  - name: conversion_rate
    source: mock.conversion_rate
    kind: rate
    parents: [page_load_seconds, engaged_share]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.0, sigma: 0.02 }
      page_load_seconds:
        distribution: "Normal"
        params: { mu: -0.01, sigma: 0.005 }
      engaged_share:
        distribution: "HalfNormal"
        params: { sigma: 0.05 }
    expected_signs:
      page_load_seconds: negative
      engaged_share: positive
"""


def test_mock_reads_per_parent_priors_and_can_generate_a_negative_edge():
    """C11: every coefficient came from `priors["coefficient"].mu` with a
    `uniform(0.1, 0.5)` fallback, so per-parent priors were never read and no
    generated edge was ever negative — a correctly declared
    `expected_signs: negative` was then guaranteed to warn.

    Here the shared `coefficient` prior is `mu: 0.0`; only the per-parent
    priors carry direction, so the old code produced a flat zero signal.
    """
    parser = Parser(PRIOR_SIGN_TREE)
    fetcher = MockDataFetcher(dag=parser.dag)
    window = ("2022-01-01", "2024-12-31")

    rate = fetcher.fetch_metric("conversion_rate", *window, kind="rate")["conversion_rate"]
    load = fetcher.fetch_metric("page_load_seconds", *window, kind="rate")["page_load_seconds"]
    engaged = fetcher.fetch_metric("engaged_share", *window, kind="rate")["engaged_share"]

    assert np.corrcoef(rate, load)[0, 1] < 0, "declared-negative edge generated positive"
    assert np.corrcoef(rate, engaged)[0, 1] > 0, "declared-positive edge generated negative"
    # The child is still a rate, not its regressors' scale (seconds).
    assert 0 < rate.min() and rate.max() < 1

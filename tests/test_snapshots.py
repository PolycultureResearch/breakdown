"""Snapshot store: parquet round-trips, read-through semantics, wiring."""

import json
import logging
import os

import pandas as pd
import pytest

from breakdown.api.main import _wrap_snapshots
from breakdown.data_fetch import BaseDataFetcher, MockDataFetcher
from breakdown.parser import BindingSpec
from breakdown.snapshots import MANIFEST, SnapshotFetcher, SnapshotStore, definition_sha


class CountingFetcher(BaseDataFetcher):
    """Deterministic inner fetcher that counts provider hits."""

    def __init__(self):
        self.calls = 0

    def fetch_metric(self, metric_name, start_date, end_date, grain="day", kind="flow"):
        self.calls += 1
        dates = pd.date_range(start_date, end_date, freq="D")
        return pd.DataFrame({"date": dates, metric_name: [float(i) for i in range(len(dates))]})


class ExplodingFetcher(BaseDataFetcher):
    def fetch_metric(self, *args, **kwargs):
        raise RuntimeError("warehouse is down")

    def fetch_metric_sliced(self, *args, **kwargs):
        raise RuntimeError("warehouse is down")


class CountingSlicedFetcher(BaseDataFetcher):
    """Inner fetcher for the sliced path; records the windows it was asked for."""

    def __init__(self):
        self.windows = []

    def fetch_metric(self, metric_name, start_date, end_date, grain="day", kind="flow"):
        raise AssertionError("sliced tests should not hit the unsliced path")

    def fetch_metric_sliced(
        self, metric_name, dimension_source, start_date, end_date, grain="day", kind="flow"
    ):
        self.windows.append((start_date, end_date))
        dates = pd.date_range(start_date, end_date, freq="D")
        return pd.DataFrame(
            {
                "date": list(dates) * 2,
                "slice": ["a"] * len(dates) + ["b"] * len(dates),
                "value": [float(i) for i in range(len(dates))] * 2,
            }
        )


# The C16 reproduction, verbatim: a query that includes refunds, and the same
# query after the author noticed and excluded them.
REFUNDS_IN = (
    "SELECT d AS date, amount AS value FROM orders\nWHERE :start_date <= d AND d <= :end_date"
)
REFUNDS_OUT = (
    "SELECT d AS date, amount AS value FROM orders\n"
    "WHERE is_refund = false AND :start_date <= d AND d <= :end_date"
)


class SqlFetcher(BaseDataFetcher):
    """A `warehouse`-shaped provider: each metric carries its own SQL, and the
    numbers follow the SQL text. Editing the query changes the answer, which is
    the whole of C16 — a cache keyed only on (metric, window, grain, kind)
    cannot tell the two apart."""

    def __init__(self, metric_sql):
        self.metric_sql = dict(metric_sql)
        self.calls = 0

    def _value(self, metric_name):
        return 60.0 if "is_refund" in self.metric_sql[metric_name] else 100.0

    def fetch_metric(self, metric_name, start_date, end_date, grain="day", kind="flow"):
        self.calls += 1
        dates = pd.date_range(start_date, end_date, freq="D")
        return pd.DataFrame({"date": dates, metric_name: [self._value(metric_name)] * len(dates)})

    def fetch_metric_sliced(
        self, metric_name, dimension_source, start_date, end_date, grain="day", kind="flow"
    ):
        self.calls += 1
        dates = pd.date_range(start_date, end_date, freq="D")
        return pd.DataFrame(
            {
                "date": list(dates),
                "slice": ["a"] * len(dates),
                "value": [self._value(metric_name)] * len(dates),
            }
        )

    def query_provenance(self, metric_name, dimension_source=None, **kw):
        return self.metric_sql.get(metric_name)


def _binding(measure: str) -> BindingSpec:
    return BindingSpec(
        relation="analytics.orders",
        grain_key="order_id",
        time_column="d",
        agg="sum",
        measure=measure,
    )


def test_store_round_trip(tmp_path):
    store = SnapshotStore(str(tmp_path))
    df = pd.DataFrame(
        {"date": pd.date_range("2024-01-01", periods=3, freq="D"), "revenue": [1.5, 2.0, 3.25]}
    )
    store.write("revenue", "2024-01-01", "2024-01-03", "day", "flow", df, provider="Test")
    back = store.read("revenue", "2024-01-01", "2024-01-03", "day", "flow")
    pd.testing.assert_frame_equal(back, df)


def test_store_miss_returns_none(tmp_path):
    assert SnapshotStore(str(tmp_path)).read("x", "2024-01-01", "2024-01-02", "day", "flow") is None


def test_store_key_includes_window_grain_kind(tmp_path):
    store = SnapshotStore(str(tmp_path))
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=1), "m": [1.0]})
    store.write("m", "2024-01-01", "2024-01-01", "day", "flow", df, provider="Test")
    assert store.read("m", "2024-01-01", "2024-01-02", "day", "flow") is None  # window
    assert store.read("m", "2024-01-01", "2024-01-01", "week", "flow") is None  # grain
    assert store.read("m", "2024-01-01", "2024-01-01", "day", "stock") is None  # kind


def test_manifest_records_provenance(tmp_path):
    store = SnapshotStore(str(tmp_path))
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=2), "m": [1.0, 2.0]})
    store.write("m", "2024-01-01", "2024-01-02", "day", "flow", df, provider="WarehouseDataFetcher")
    with open(tmp_path / MANIFEST) as f:
        manifest = json.load(f)
    (entry,) = manifest.values()
    assert entry["provider"] == "WarehouseDataFetcher"
    assert entry["rows"] == 2
    assert "fetched_at" in entry


def test_fetcher_reads_through_once(tmp_path):
    inner = CountingFetcher()
    fetcher = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    first = fetcher.fetch_metric("m", "2024-01-01", "2024-01-05")
    second = fetcher.fetch_metric("m", "2024-01-01", "2024-01-05")
    assert inner.calls == 1
    pd.testing.assert_frame_equal(first, second)


def test_refresh_refetches_and_overwrites(tmp_path):
    inner = CountingFetcher()
    store = SnapshotStore(str(tmp_path))
    SnapshotFetcher(inner, store).fetch_metric("m", "2024-01-01", "2024-01-05")
    SnapshotFetcher(inner, store, refresh=True).fetch_metric("m", "2024-01-01", "2024-01-05")
    assert inner.calls == 2
    # refresh still writes: a third, non-refresh fetcher hits the snapshot
    SnapshotFetcher(inner, store).fetch_metric("m", "2024-01-01", "2024-01-05")
    assert inner.calls == 2


def test_provider_outage_served_from_snapshots(tmp_path):
    """The resilience property: warehouse down + snapshots present → data."""
    store = SnapshotStore(str(tmp_path))
    good = SnapshotFetcher(CountingFetcher(), store)
    expected = good.fetch_metric("m", "2024-01-01", "2024-01-05")
    from_snapshot = SnapshotFetcher(ExplodingFetcher(), store).fetch_metric(
        "m", "2024-01-01", "2024-01-05"
    )
    pd.testing.assert_frame_equal(from_snapshot, expected)


def test_unwritable_dir_serves_uncached(tmp_path, caplog):
    """A snapshot dir the process cannot create degrades to serving uncached.

    The obstruction is a *file* where the store wants a directory, so
    `os.makedirs` raises ENOTDIR — which is true for every uid. Permission bits
    would not be: root ignores them, the write succeeds, and this test failed
    under Docker or any CI that runs as root. (The EACCES flavour of the same
    OSError is covered below, where it can actually be produced.)
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    inner = CountingFetcher()
    fetcher = SnapshotFetcher(inner, SnapshotStore(str(blocked / "snapshots")))
    df = fetcher.fetch_metric("m", "2024-01-01", "2024-01-03")
    assert len(df) == 3
    assert "snapshot write failed" in caplog.text


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores the permission bits this makes the directory unwritable with",
)
def test_read_only_dir_serves_uncached(tmp_path, caplog):
    """The realistic shape of the same failure: a snapshot dir mounted or
    chmod'd read-only. Only reproducible as an unprivileged user."""
    read_only = tmp_path / "ro"
    read_only.mkdir()
    os.chmod(read_only, 0o500)
    try:
        inner = CountingFetcher()
        fetcher = SnapshotFetcher(inner, SnapshotStore(str(read_only / "snapshots")))
        df = fetcher.fetch_metric("m", "2024-01-01", "2024-01-03")
        assert len(df) == 3
        assert "snapshot write failed" in caplog.text
    finally:
        os.chmod(read_only, 0o700)


def test_wrap_snapshots_skips_mock(tmp_path):
    inner = MockDataFetcher()
    assert _wrap_snapshots(inner, "mock", str(tmp_path / "t.yml")) is inner


def test_wrap_snapshots_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_SNAPSHOT_DIR", "off")
    inner = CountingFetcher()
    assert _wrap_snapshots(inner, "warehouse", str(tmp_path / "t.yml")) is inner


def test_wrap_snapshots_defaults_tree_adjacent(tmp_path, monkeypatch):
    monkeypatch.delenv("BREAKDOWN_SNAPSHOT_DIR", raising=False)
    monkeypatch.delenv("BREAKDOWN_REFRESH", raising=False)
    wrapped = _wrap_snapshots(CountingFetcher(), "warehouse", str(tmp_path / "sub" / "t.yml"))
    assert isinstance(wrapped, SnapshotFetcher)
    assert wrapped.store.directory == str(tmp_path / "sub" / ".breakdown" / "snapshots")
    assert wrapped.refresh is False


def test_wrap_snapshots_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKDOWN_SNAPSHOT_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BREAKDOWN_REFRESH", "1")
    wrapped = _wrap_snapshots(CountingFetcher(), "cloud", str(tmp_path / "t.yml"))
    assert wrapped.store.directory == str(tmp_path / "cache")
    assert wrapped.refresh is True


def test_sliced_reads_through_once(tmp_path):
    inner = CountingSlicedFetcher()
    fetcher = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    first = fetcher.fetch_metric_sliced("m", "customer__region", "2024-01-01", "2024-01-05")
    second = fetcher.fetch_metric_sliced("m", "customer__region", "2024-01-01", "2024-01-05")
    assert inner.windows == [("2024-01-01", "2024-01-05")]
    pd.testing.assert_frame_equal(first, second)


def test_sliced_span_serves_unseen_sub_windows(tmp_path):
    """The property the hermetic deployment needs: one stored span answers
    windows nobody ran at build time."""
    inner = CountingSlicedFetcher()
    fetcher = SnapshotFetcher(
        inner, SnapshotStore(str(tmp_path)), slice_span=("2024-01-01", "2024-01-31")
    )
    fetcher.fetch_metric_sliced("m", "customer__region", "2024-01-10", "2024-01-12")
    assert inner.windows == [("2024-01-01", "2024-01-31")]  # widened to the span

    # A different window, never requested before, is still a snapshot hit.
    offline = SnapshotFetcher(ExplodingFetcher(), SnapshotStore(str(tmp_path)))
    df = offline.fetch_metric_sliced("m", "customer__region", "2024-01-20", "2024-01-22")
    assert list(df["date"].dt.strftime("%Y-%m-%d").unique()) == [
        "2024-01-20",
        "2024-01-21",
        "2024-01-22",
    ]
    assert set(df["slice"]) == {"a", "b"}


def test_sliced_request_outside_span_is_not_widened(tmp_path):
    inner = CountingSlicedFetcher()
    fetcher = SnapshotFetcher(
        inner, SnapshotStore(str(tmp_path)), slice_span=("2024-01-01", "2024-01-31")
    )
    fetcher.fetch_metric_sliced("m", "customer__region", "2024-02-01", "2024-02-03")
    assert inner.windows == [("2024-02-01", "2024-02-03")]


def test_sliced_key_separates_dimension_and_metric(tmp_path):
    inner = CountingSlicedFetcher()
    fetcher = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    fetcher.fetch_metric_sliced("m", "customer__region", "2024-01-01", "2024-01-05")
    fetcher.fetch_metric_sliced("m", "customer__plan", "2024-01-01", "2024-01-05")
    fetcher.fetch_metric_sliced("other", "customer__region", "2024-01-01", "2024-01-05")
    assert len(inner.windows) == 3
    assert len(list(tmp_path.glob("*.parquet"))) == 3


def test_sliced_refresh_refetches(tmp_path):
    inner = CountingSlicedFetcher()
    store = SnapshotStore(str(tmp_path))
    SnapshotFetcher(inner, store).fetch_metric_sliced("m", "d", "2024-01-01", "2024-01-05")
    SnapshotFetcher(inner, store, refresh=True).fetch_metric_sliced(
        "m", "d", "2024-01-01", "2024-01-05"
    )
    assert len(inner.windows) == 2


@pytest.mark.parametrize("kind", ["flow", "stock"])
def test_kind_passed_through_to_inner(tmp_path, kind):
    """The wrapper must forward grain/kind — gap-fill semantics live inner."""
    seen = {}

    class Probe(BaseDataFetcher):
        def fetch_metric(self, metric_name, start_date, end_date, grain="day", kind="flow"):
            seen.update(grain=grain, kind=kind)
            return pd.DataFrame({"date": pd.date_range(start_date, periods=1), metric_name: [1.0]})

    SnapshotFetcher(Probe(), SnapshotStore(str(tmp_path))).fetch_metric(
        "m", "2024-01-01", "2024-01-07", grain="week", kind=kind
    )
    assert seen == {"grain": "week", "kind": kind}


def test_snapshot_fetcher_delegates_earliest_date(tmp_path):
    class EpochFetcher(CountingFetcher):
        def earliest_date(self, metric_name, grain="day"):
            return "2019-06-01"

    fetcher = SnapshotFetcher(EpochFetcher(), SnapshotStore(str(tmp_path)))
    assert fetcher.earliest_date("m") == "2019-06-01"


def test_snapshot_fetcher_earliest_date_never_raises(tmp_path):
    class RaisingFetcher(CountingFetcher):
        def earliest_date(self, metric_name, grain="day"):
            raise RuntimeError("no SDK in this deployment")

    fetcher = SnapshotFetcher(RaisingFetcher(), SnapshotStore(str(tmp_path)))
    assert fetcher.earliest_date("m") is None


# --- definition fingerprinting (roadmap C16) --------------------------------


def test_edited_sql_invalidates_the_snapshot(tmp_path, caplog):
    """The reproduction: snapshot the refund-including query, edit it to
    exclude refunds, restart. The server must not keep serving 100.0."""
    SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_IN}), SnapshotStore(str(tmp_path))
    ).fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")

    inner = SqlFetcher({"ticket_revenue": REFUNDS_OUT})
    restarted = SnapshotFetcher(inner, SnapshotStore(str(tmp_path)))
    with caplog.at_level(logging.WARNING):
        df = restarted.fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")

    assert df["ticket_revenue"].mean() == 60.0
    assert inner.calls == 1  # the stale snapshot was refused, not served
    assert "definition behind 'ticket_revenue' changed" in caplog.text


def test_edited_sql_keeps_provenance_and_number_in_agreement(tmp_path):
    """The worse half of C16: *show query* must never display the edited SQL
    beside the pre-edit number."""
    SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_IN}), SnapshotStore(str(tmp_path))
    ).fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")

    restarted = SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_OUT}), SnapshotStore(str(tmp_path))
    )
    df = restarted.fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")
    shown = restarted.query_provenance("ticket_revenue")

    assert shown == REFUNDS_OUT
    assert df["ticket_revenue"].mean() == 60.0  # what REFUNDS_OUT actually returns


def test_unchanged_sql_still_hits_the_snapshot(tmp_path):
    """Caching still works: an identical definition is a hit, not a refetch."""
    SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_IN}), SnapshotStore(str(tmp_path))
    ).fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")

    inner = SqlFetcher({"ticket_revenue": REFUNDS_IN})
    df = SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
        "ticket_revenue", "2024-01-01", "2024-01-05"
    )
    assert inner.calls == 0
    assert df["ticket_revenue"].mean() == 100.0


def test_reformatted_sql_is_the_same_definition(tmp_path):
    """Leading/trailing whitespace is not a change. Interior whitespace is left
    alone on purpose — collapsing it cannot tell a string literal apart."""
    padded = f"\n  {REFUNDS_IN}  \n"
    SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_IN}), SnapshotStore(str(tmp_path))
    ).fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")

    inner = SqlFetcher({"ticket_revenue": padded})
    SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
        "ticket_revenue", "2024-01-01", "2024-01-05"
    )
    assert inner.calls == 0


def test_manifest_records_definition_sha(tmp_path):
    store = SnapshotStore(str(tmp_path))
    SnapshotFetcher(SqlFetcher({"m": REFUNDS_IN}), store).fetch_metric(
        "m", "2024-01-01", "2024-01-02"
    )
    with open(tmp_path / MANIFEST) as f:
        (entry,) = json.load(f).values()
    assert entry["definition_sha"] == definition_sha(SqlFetcher({"m": REFUNDS_IN}), "m")


def test_legacy_snapshot_without_sha_is_served_with_a_warning(tmp_path, caplog):
    """Pre-C16 dirs keep working — refusing them would break every committed
    snapshot and every snapshot-only deployment — but say so out loud."""
    SnapshotFetcher(
        SqlFetcher({"ticket_revenue": REFUNDS_IN}), SnapshotStore(str(tmp_path))
    ).fetch_metric("ticket_revenue", "2024-01-01", "2024-01-05")
    # Age the manifest back to what the old writer produced.
    with open(tmp_path / MANIFEST) as f:
        manifest = json.load(f)
    for record in manifest.values():
        record.pop("definition_sha")
    with open(tmp_path / MANIFEST, "w") as f:
        json.dump(manifest, f)

    inner = SqlFetcher({"ticket_revenue": REFUNDS_OUT})
    with caplog.at_level(logging.WARNING):
        df = SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
            "ticket_revenue", "2024-01-01", "2024-01-05"
        )
    assert inner.calls == 0
    assert df["ticket_revenue"].mean() == 100.0
    assert "predates definition fingerprinting" in caplog.text
    assert "BREAKDOWN_REFRESH=1" in caplog.text


def test_refresh_stamps_a_legacy_snapshot(tmp_path, caplog):
    """And the disclosed remedy actually works: one refresh pass ends it."""
    store = SnapshotStore(str(tmp_path))
    inner = SqlFetcher({"m": REFUNDS_IN})
    SnapshotFetcher(inner, store, refresh=True).fetch_metric("m", "2024-01-01", "2024-01-05")
    with caplog.at_level(logging.WARNING):
        SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
            "m", "2024-01-01", "2024-01-05"
        )
    assert inner.calls == 1
    assert caplog.text == ""


def test_provider_without_definitions_neither_warns_nor_refetches(tmp_path, caplog):
    """`local`/`cloud` hold no per-metric definition — there is nothing to
    fingerprint, so there is nothing to warn about."""
    inner = CountingFetcher()
    SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
        "m", "2024-01-01", "2024-01-05"
    )
    with caplog.at_level(logging.WARNING):
        SnapshotFetcher(inner, SnapshotStore(str(tmp_path))).fetch_metric(
            "m", "2024-01-01", "2024-01-05"
        )
    assert inner.calls == 1
    assert caplog.text == ""


def test_edited_binding_invalidates_the_sliced_snapshot(tmp_path):
    """`read_sliced` matches any covering window, so it needs the same check —
    and the fingerprint must not mention a window for that to work."""
    store = SnapshotStore(str(tmp_path))
    SnapshotFetcher(
        SqlFetcher({"m": REFUNDS_IN}), store, slice_span=("2024-01-01", "2024-01-31")
    ).fetch_metric_sliced("m", "region", "2024-01-10", "2024-01-12")

    inner = SqlFetcher({"m": REFUNDS_OUT})
    df = SnapshotFetcher(
        inner, SnapshotStore(str(tmp_path)), slice_span=("2024-01-01", "2024-01-31")
    ).fetch_metric_sliced("m", "region", "2024-01-20", "2024-01-22")
    assert inner.calls == 1
    assert df["value"].mean() == 60.0


def test_unchanged_binding_still_hits_the_sliced_snapshot(tmp_path):
    SnapshotFetcher(
        SqlFetcher({"m": REFUNDS_IN}),
        SnapshotStore(str(tmp_path)),
        slice_span=("2024-01-01", "2024-01-31"),
    ).fetch_metric_sliced("m", "region", "2024-01-10", "2024-01-12")

    inner = SqlFetcher({"m": REFUNDS_IN})
    df = SnapshotFetcher(
        inner, SnapshotStore(str(tmp_path)), slice_span=("2024-01-01", "2024-01-31")
    ).fetch_metric_sliced("m", "region", "2024-01-20", "2024-01-22")
    assert inner.calls == 0
    assert len(df) == 3


def test_dbt_binding_is_fingerprinted(tmp_path):
    """The `dbt` provider's definition is a `BindingSpec`, not SQL: it must
    fingerprint by value, and by field rather than by YAML key order."""

    class BoundFetcher(BaseDataFetcher):
        def __init__(self, bindings):
            self.bindings = bindings

        def fetch_metric(self, *a, **kw):
            raise AssertionError("not called")

    revenue = definition_sha(BoundFetcher({"m": _binding("amount")}), "m")
    assert revenue == definition_sha(BoundFetcher({"m": _binding("amount")}), "m")
    assert revenue != definition_sha(BoundFetcher({"m": _binding("net_amount")}), "m")
    assert definition_sha(BoundFetcher({"m": _binding("amount")}), "other") is None


def test_snapshot_definition_hook_takes_precedence(tmp_path):
    """The extension point a future provider implements instead of being
    special-cased inside snapshots.py."""

    class HookedFetcher(SqlFetcher):
        def snapshot_definition(self, metric_name):
            return "pinned"

    hooked = definition_sha(HookedFetcher({"m": REFUNDS_IN}), "m")
    assert hooked == definition_sha(HookedFetcher({"m": REFUNDS_OUT}), "m")
    assert hooked != definition_sha(SqlFetcher({"m": REFUNDS_IN}), "m")

"""Snapshot store: parquet round-trips, read-through semantics, wiring."""

import json
import os

import pandas as pd
import pytest

from breakdown.api.main import _wrap_snapshots
from breakdown.data_fetch import BaseDataFetcher, MockDataFetcher
from breakdown.snapshots import MANIFEST, SnapshotFetcher, SnapshotStore


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

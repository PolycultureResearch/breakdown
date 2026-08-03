"""Snapshot store: a local parquet cache of fetched metric series (roadmap 2.4).

Each fetched series is written once per (metric, grain, kind, window) and
read back on every later startup, so a tree refits without touching the
warehouse: restarts are cheap, RCAs re-run reproducibly from a fresh clone
(snapshots are plain files a partner repo can commit), and a provider
migration is invisible as long as the snapshots agree. The cache wraps the
real fetcher at the `BaseDataFetcher` boundary — nothing downstream knows
snapshots exist.

Snapshotting is deliberately failure-soft: an unwritable directory (e.g. a
read-only /config mount in a container) logs one warning and the app runs
uncached, and a provider outage is survivable when every metric in the tree
already has a snapshot.
"""

import datetime
import json
import logging
import os
from typing import Optional

import pandas as pd

from breakdown.data_fetch import BaseDataFetcher

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"


class SnapshotStore:
    """One parquet file per (metric, grain, kind, window), plus a manifest
    recording provenance (provider, fetch time, row count) for humans."""

    def __init__(self, directory: str):
        self.directory = directory

    def _filename(self, metric: str, start: str, end: str, grain: str, kind: str) -> str:
        return f"{metric}__{grain}-{kind}__{start}__{end}.parquet"

    def read(
        self, metric: str, start: str, end: str, grain: str, kind: str
    ) -> Optional[pd.DataFrame]:
        path = os.path.join(self.directory, self._filename(metric, start, end, grain, kind))
        if not os.path.isfile(path):
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def write(
        self, metric: str, start: str, end: str, grain: str, kind: str,
        df: pd.DataFrame, provider: str,
    ) -> None:
        os.makedirs(self.directory, exist_ok=True)
        filename = self._filename(metric, start, end, grain, kind)
        df.to_parquet(os.path.join(self.directory, filename), index=False)
        self._record(filename, provider, len(df))

    def _record(self, filename: str, provider: str, rows: int) -> None:
        path = os.path.join(self.directory, MANIFEST)
        manifest = {}
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError):
                manifest = {}
        manifest[filename] = {
            "provider": provider,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "rows": rows,
        }
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)


class SnapshotFetcher(BaseDataFetcher):
    """Read-through cache over another fetcher.

    A hit returns the stored frame without touching the inner provider; a
    miss fetches, stores, and returns. `refresh=True` skips reads (but still
    writes), forcing one clean refetch pass — the knob for "the warehouse
    backfilled, my snapshots are stale"."""

    def __init__(self, inner: BaseDataFetcher, store: SnapshotStore, refresh: bool = False):
        self.inner = inner
        self.store = store
        self.refresh = refresh

    def fetch_metric(
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
    ) -> pd.DataFrame:
        if not self.refresh:
            df = self.store.read(metric_name, start_date, end_date, grain, kind)
            if df is not None:
                logger.info("snapshot hit: %s [%s, %s] %s", metric_name, start_date, end_date, grain)
                return df

        df = self.inner.fetch_metric(metric_name, start_date, end_date, grain=grain, kind=kind)
        try:
            self.store.write(
                metric_name, start_date, end_date, grain, kind,
                df, provider=type(self.inner).__name__,
            )
            logger.info("snapshot written: %s [%s, %s] %s", metric_name, start_date, end_date, grain)
        except OSError as e:
            logger.warning(
                "snapshot write failed for %s (%s); serving uncached. "
                "Set --snapshot-dir to a writable path or --no-snapshots to silence.",
                metric_name, e,
            )
        return df

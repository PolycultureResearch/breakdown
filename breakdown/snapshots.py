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

    def _sliced_filename(
        self, metric: str, dimension: str, start: str, end: str, grain: str, kind: str
    ) -> str:
        return f"{metric}__by-{dimension}__{grain}-{kind}__{start}__{end}.parquet"

    def read(
        self, metric: str, start: str, end: str, grain: str, kind: str
    ) -> Optional[pd.DataFrame]:
        path = os.path.join(self.directory, self._filename(metric, start, end, grain, kind))
        if not os.path.isfile(path):
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def read_sliced(
        self, metric: str, dimension: str, start: str, end: str, grain: str, kind: str
    ) -> Optional[pd.DataFrame]:
        """Return a sliced frame covering [start, end], trimmed from any stored
        window that contains it.

        Unlike `read`, this does not require an exact window match. Sliced
        frames are fetched per analysis, so keying on the requested window would
        only ever serve the windows someone happened to snapshot — anyone
        picking their own dates would fall through to the provider. Storing the
        widest window once and trimming makes every sub-window a hit."""
        for path, s, e in self._sliced_candidates(metric, dimension, grain, kind):
            if s <= start and e >= end:
                df = pd.read_parquet(path)
                df["date"] = pd.to_datetime(df["date"])
                window = df[(df["date"] >= start) & (df["date"] <= end)]
                return window.reset_index(drop=True)
        return None

    def _sliced_candidates(
        self, metric: str, dimension: str, grain: str, kind: str
    ) -> list[tuple[str, str, str]]:
        """Stored sliced windows for this key, widest first."""
        prefix = f"{metric}__by-{dimension}__{grain}-{kind}__"
        found = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        for name in names:
            if not name.startswith(prefix) or not name.endswith(".parquet"):
                continue
            span = name[len(prefix) : -len(".parquet")]
            start, _, end = span.partition("__")
            if start and end:
                found.append((os.path.join(self.directory, name), start, end))
        return sorted(found, key=lambda f: (f[1], f[2]))

    def write(
        self,
        metric: str,
        start: str,
        end: str,
        grain: str,
        kind: str,
        df: pd.DataFrame,
        provider: str,
    ) -> None:
        os.makedirs(self.directory, exist_ok=True)
        filename = self._filename(metric, start, end, grain, kind)
        df.to_parquet(os.path.join(self.directory, filename), index=False)
        self._record(filename, provider, len(df))

    def write_sliced(
        self,
        metric: str,
        dimension: str,
        start: str,
        end: str,
        grain: str,
        kind: str,
        df: pd.DataFrame,
        provider: str,
    ) -> None:
        os.makedirs(self.directory, exist_ok=True)
        filename = self._sliced_filename(metric, dimension, start, end, grain, kind)
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

    def fetch_entity_flows(self, *args, **kw):
        """Delegated, never cached. Flows are keyed by a *pair* of windows
        rather than one, so the snapshot store's (metric, grain, kind, window)
        key cannot address them — and they are analysis-time only, like slices
        were before sliced snapshots existed."""
        return self.inner.fetch_entity_flows(*args, **kw)

    def slice_additivity(self, metric_name, dimension_source):
        """Delegates: additivity is a property of the binding, not of whether
        the series came from cache."""
        return self.inner.slice_additivity(metric_name, dimension_source)

    def query_provenance(self, metric_name, dimension_source=None, **kw):
        """Delegates to the wrapped provider.

        A snapshot hit means no query ran for *this* call, but the statement
        that would produce the series is still what defends the number: the
        binding determines it exactly whether or not it executed just now. The
        caller labels which of the two it got.
        """
        return self.inner.query_provenance(metric_name, dimension_source, **kw)

    def __init__(
        self,
        inner: BaseDataFetcher,
        store: SnapshotStore,
        refresh: bool = False,
        slice_span: Optional[tuple[str, str]] = None,
    ):
        self.inner = inner
        self.store = store
        self.refresh = refresh
        # The window sliced fetches are widened to before storing. Slices are
        # fetched per analysis, so without this a snapshot only ever serves the
        # exact windows someone already ran; widening to the loaded data window
        # stores each (metric, dimension) once and serves every sub-window from
        # it — which is what lets a snapshot-only deployment answer slice
        # questions nobody anticipated.
        self.slice_span = slice_span

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        if not self.refresh:
            df = self.store.read(metric_name, start_date, end_date, grain, kind)
            if df is not None:
                logger.info(
                    "snapshot hit: %s [%s, %s] %s", metric_name, start_date, end_date, grain
                )
                return df

        df = self.inner.fetch_metric(metric_name, start_date, end_date, grain=grain, kind=kind)
        try:
            self.store.write(
                metric_name,
                start_date,
                end_date,
                grain,
                kind,
                df,
                provider=type(self.inner).__name__,
            )
            logger.info(
                "snapshot written: %s [%s, %s] %s", metric_name, start_date, end_date, grain
            )
        except OSError as e:
            logger.warning(
                "snapshot write failed for %s (%s); serving uncached. "
                "Set --snapshot-dir to a writable path or --no-snapshots to silence.",
                metric_name,
                e,
            )
        return df

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        if not self.refresh:
            df = self.store.read_sliced(
                metric_name, dimension_source, start_date, end_date, grain, kind
            )
            if df is not None:
                logger.info(
                    "sliced snapshot hit: %s by %s [%s, %s] %s",
                    metric_name,
                    dimension_source,
                    start_date,
                    end_date,
                    grain,
                )
                return df

        span_start, span_end = self._span(start_date, end_date)
        df = self.inner.fetch_metric_sliced(
            metric_name,
            dimension_source,
            span_start,
            span_end,
            grain=grain,
            kind=kind,
        )
        try:
            self.store.write_sliced(
                metric_name,
                dimension_source,
                span_start,
                span_end,
                grain,
                kind,
                df,
                provider=type(self.inner).__name__,
            )
            logger.info(
                "sliced snapshot written: %s by %s [%s, %s] %s",
                metric_name,
                dimension_source,
                span_start,
                span_end,
                grain,
            )
        except OSError as e:
            logger.warning(
                "sliced snapshot write failed for %s by %s (%s); serving uncached.",
                metric_name,
                dimension_source,
                e,
            )
        if (span_start, span_end) == (start_date, end_date):
            return df
        window = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        return window.reset_index(drop=True)

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        # Pure passthrough — snapshot-only deployments (no SDK, no warehouse)
        # must simply not know, never crash.
        try:
            return self.inner.earliest_date(metric_name, grain)
        except Exception as e:
            logger.info("earliest_date unavailable for '%s': %s", metric_name, e)
            return None

    def _span(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Widen a sliced fetch to the configured span when it covers the request."""
        if not self.slice_span:
            return start_date, end_date
        span_start, span_end = self.slice_span
        if span_start <= start_date and span_end >= end_date:
            return span_start, span_end
        return start_date, end_date

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

A snapshot is only as good as the question that produced it, so each record
also carries a `definition_sha` — a fingerprint of the provider-side
definition that determines the values (the `warehouse` metric's own `sql:`,
the `dbt` node's binding). The window, grain and kind are in the filename;
the definition is not, and without the fingerprint editing a metric's SQL to
exclude refunds and restarting would serve the refund-including numbers
forever (roadmap C16). Worse, `query_provenance` would then show the *new*
statement beside the *old* number, so the one panel that exists to let a
reader verify a number would certify a wrong one. Comparing the sha on read
and refetching on a mismatch repairs both: a hit can only be served when the
statement that would be shown is the statement that produced it.
"""

import datetime
import hashlib
import json
import logging
import os
from typing import Any, Optional

import pandas as pd

from breakdown.data_fetch import BaseDataFetcher, _align_to_spine

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"

# Attributes on a provider that hold its per-metric definitions, keyed by the
# same name the snapshot store is keyed by (`provider_query_name`): the
# `warehouse` fetcher's `metric_sql`, the `dbt` fetcher's `bindings`. The
# semantic-layer providers (`local`, `cloud`) hand a metric *name* to someone
# else's planner and hold no definition at all — for them the fingerprint is
# legitimately None, not a failure.
#
# A provider that carries its definition somewhere else should implement
# `snapshot_definition(metric_name) -> str | None`, which is checked first and
# is the intended extension point; this tuple is the fallback for the two
# providers that predate it.
_DEFINITION_ATTRS = ("metric_sql", "bindings")


def definition_sha(fetcher: BaseDataFetcher, metric_name: str) -> Optional[str]:
    """Fingerprint of what `fetcher` would ask for `metric_name`, or None when
    the provider has no per-metric definition to fingerprint.

    Deliberately *not* derived from `query_provenance`: for the `dbt` provider
    that string is generated per window and prefers the statement that last
    ran, so the same binding fingerprints differently depending on which
    windows this process happened to fetch — which would turn every hit into a
    miss. The definition itself is window-independent, which is what the
    sliced path needs too (`read_sliced` serves any stored window that covers
    the request, so its fingerprint cannot mention a window).
    """
    hook = getattr(fetcher, "snapshot_definition", None)
    if callable(hook):
        text = hook(metric_name)
    else:
        text = None
        for attr in _DEFINITION_ATTRS:
            definitions = getattr(fetcher, attr, None)
            if isinstance(definitions, dict) and metric_name in definitions:
                text = _definition_text(definitions[metric_name])
                break
    if not text:
        return None
    # 64 bits: collision-free for any plausible number of snapshots, and short
    # enough that a human scanning manifest.json can compare two by eye.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _definition_text(value: Any) -> Optional[str]:
    """Canonical text for one definition.

    Only `.strip()` is normalized away. Collapsing interior whitespace would
    make a reformat cheap, but it cannot tell whitespace inside a string
    literal from whitespace between tokens, and hashing two genuinely
    different queries to the same value serves a stale number silently — the
    exact failure this fingerprint exists to prevent. A needless refetch costs
    one query; a false hit costs a wrong answer nobody can see is wrong.

    Pydantic models (`BindingSpec`) serialize by field order rather than by
    the YAML author's key order, so re-ordering keys in a `bind:` block is
    free where re-ordering SQL is not.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    dump = getattr(value, "model_dump_json", None)
    if callable(dump):
        return str(dump())
    # Unknown definition object: `repr` may be unstable across processes, so
    # this degrades to refetching every startup. Never to a false hit.
    return repr(value)


class SnapshotStore:
    """One parquet file per (metric, grain, kind, window), plus a manifest
    recording provenance (provider, fetch time, row count, definition sha).

    The manifest is not only documentation: `definition_sha` is compared on
    every read, and a file whose definition has moved on is refused rather
    than served (roadmap C16)."""

    def __init__(self, directory: str):
        self.directory = directory
        # Parsed manifest, loaded once. Only this process writes it during a
        # run, and `_record` keeps the cache in step, so re-reading it once per
        # metric on a cold start buys nothing.
        self._manifest_cache: Optional[dict] = None
        # Files already warned about, so an unverifiable snapshot re-read on
        # every RCA does not repeat itself into noise.
        self._warned: set[str] = set()

    def _filename(self, metric: str, start: str, end: str, grain: str, kind: str) -> str:
        return f"{metric}__{grain}-{kind}__{start}__{end}.parquet"

    def _sliced_filename(
        self, metric: str, dimension: str, start: str, end: str, grain: str, kind: str
    ) -> str:
        return f"{metric}__by-{dimension}__{grain}-{kind}__{start}__{end}.parquet"

    def read(
        self,
        metric: str,
        start: str,
        end: str,
        grain: str,
        kind: str,
        definition_sha: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        filename = self._filename(metric, start, end, grain, kind)
        path = os.path.join(self.directory, filename)
        if not os.path.isfile(path):
            return None
        if not self._definition_matches(filename, metric, definition_sha):
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def read_sliced(
        self,
        metric: str,
        dimension: str,
        start: str,
        end: str,
        grain: str,
        kind: str,
        definition_sha: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        """Return a sliced frame covering [start, end], trimmed from any stored
        window that contains it.

        Unlike `read`, this does not require an exact window match. Sliced
        frames are fetched per analysis, so keying on the requested window would
        only ever serve the windows someone happened to snapshot — anyone
        picking their own dates would fall through to the provider. Storing the
        widest window once and trimming makes every sub-window a hit.

        The definition is checked per candidate, exactly as in `read`: the
        binding that produces a sliced frame is the same binding that produces
        the unsliced series, so an edited `bind:` block must invalidate both."""
        for path, s, e in self._sliced_candidates(metric, dimension, grain, kind):
            if s <= start and e >= end:
                if not self._definition_matches(os.path.basename(path), metric, definition_sha):
                    continue
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
        definition_sha: Optional[str] = None,
    ) -> None:
        os.makedirs(self.directory, exist_ok=True)
        filename = self._filename(metric, start, end, grain, kind)
        df.to_parquet(os.path.join(self.directory, filename), index=False)
        self._record(filename, provider, len(df), definition_sha)

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
        definition_sha: Optional[str] = None,
    ) -> None:
        os.makedirs(self.directory, exist_ok=True)
        filename = self._sliced_filename(metric, dimension, start, end, grain, kind)
        df.to_parquet(os.path.join(self.directory, filename), index=False)
        self._record(filename, provider, len(df), definition_sha)

    def _manifest(self) -> dict:
        """The parsed manifest, or `{}` when there isn't a readable one.

        Unreadable is not fatal: a snapshot dir can be a read-only mount, and
        losing the manifest must cost verification, never the data."""
        if self._manifest_cache is None:
            self._manifest_cache = self._read_manifest()
        return self._manifest_cache

    def _read_manifest(self) -> dict:
        path = os.path.join(self.directory, MANIFEST)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path) as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _definition_matches(
        self, filename: str, metric: str, definition_sha: Optional[str]
    ) -> bool:
        """Whether this file may be served for the current definition.

        Three cases, and the middle one is the judgement call:

        * The record carries a `definition_sha` — serve iff it is the same one.
          A mismatch (including `null` vs a sha, i.e. the provider gained a
          definition we can fingerprint) means the values on disk answer a
          question nobody is asking any more.
        * The record predates fingerprinting, or there is no record. Refusing
          would break every snapshot dir written before this change, including
          the committed ones a partner repo re-runs RCAs from and the
          snapshot-only deployments that have no provider to refetch from —
          the cure would be worse than the disease. So serve it, but say so:
          silence is what made C16 a trap. One `BREAKDOWN_REFRESH=1` pass
          rewrites the dir with fingerprints and the warning stops.
        * There is nothing to fingerprint (`local`/`cloud` hold no per-metric
          definition) — nothing to warn about either, then or now.
        """
        record = self._manifest().get(filename)
        if not isinstance(record, dict) or "definition_sha" not in record:
            if definition_sha is not None and filename not in self._warned:
                self._warned.add(filename)
                logger.warning(
                    "snapshot %s predates definition fingerprinting, so it cannot be "
                    "checked against the current definition of '%s'; serving it as-is. "
                    "If you have edited its sql:/bind: since it was written, re-run with "
                    "BREAKDOWN_REFRESH=1 to refetch and stamp it.",
                    filename,
                    metric,
                )
            return True
        stored = record.get("definition_sha")
        if stored == definition_sha:
            return True
        logger.warning(
            "snapshot ignored: the definition behind '%s' changed since %s was written "
            "(%s -> %s); refetching so the number and the query it is defended by agree.",
            metric,
            filename,
            stored,
            definition_sha,
        )
        return False

    def _record(
        self, filename: str, provider: str, rows: int, definition_sha: Optional[str] = None
    ) -> None:
        path = os.path.join(self.directory, MANIFEST)
        # Re-read rather than trusting the cache: another process (a `doctor`
        # run beside a live server) may own entries this one has never seen,
        # and the manifest is a merge, not a snapshot of one process's beliefs.
        manifest = self._read_manifest()
        manifest[filename] = {
            "provider": provider,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "rows": rows,
            "definition_sha": definition_sha,
        }
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        self._manifest_cache = manifest
        self._warned.discard(filename)


def _realign_snapshot(
    df: pd.DataFrame,
    metric_name: str,
    grain: str,
    kind: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Put a snapshot hit through the same date contract every provider obeys.

    A snapshot outlives the code that wrote it. `definition_sha` (C16)
    invalidates a record when the *metric's* definition changes, but nothing
    fingerprints the **engine's** alignment rules, so a file written before C2
    landed keeps its pre-C2 shape forever — `store.read` returned it verbatim
    and no provider contract ever touched it again.

    Not hypothetical: the committed White Cube demo snapshots predate C2 and
    carried a four-day partial trailing week as a whole one. `new_mrr` served
    5,032 against a trailing level near 1,900, the UI drew a terminal spike, and
    `/meta` reported coverage three days past the loaded window — verbatim the
    symptom C2's roadmap row predicts.

    Re-aligning on read is idempotent for anything written by current code (the
    write path already went through `_align_to_spine`, and reindexing an aligned
    frame onto the same spine is a no-op), so this costs correct snapshots
    nothing and repairs stale-shaped ones.
    """
    try:
        aligned = _align_to_spine(df, metric_name, grain, kind, start_date, end_date, metric_name)
    except RuntimeError as e:
        # The contract can *refuse* as well as reshape — a `rate` with a gap is
        # an error, because a rate cannot be invented. Refusing here would take
        # down a deployment that was serving, in order to repair a file the
        # operator may not be able to refetch (a snapshot-only deployment has no
        # provider behind it). So: say so, loudly, and serve what is stored. The
        # frame is no worse than it was a release ago; the difference is that it
        # is now disclosed rather than silent.
        logger.warning(
            "Metric '%s': snapshot could not be re-aligned on read and is served as "
            "stored — %s This file predates the shared date contract (roadmap C2); "
            "its edge periods may be partial.",
            metric_name,
            e,
        )
        return df
    if len(aligned) != len(df):
        logger.warning(
            "Metric '%s': snapshot held %d %s period(s), the window's spine holds %d — "
            "re-aligned on read. This file predates the shared date contract (roadmap "
            "C2); it was serving partial edge periods as whole ones. The numbers are "
            "correct now, but the file on disk is still the old shape: refetch with "
            "BREAKDOWN_REFRESH=1 to fix it at rest.",
            metric_name,
            len(df),
            grain,
            len(aligned),
        )
    return aligned


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

        What makes that argument sound rather than merely hopeful is the
        definition sha: a snapshot whose definition has moved on is refused,
        so the series being defended here was produced by the definition this
        statement is generated from. Without that check the panel showed the
        edited query beside the pre-edit number (roadmap C16).
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
        # metric -> definition sha, memoized: a provider's definitions are
        # loaded config and cannot change under a running process, and the dbt
        # binding dump is not free to re-serialize once per RCA fetch.
        self._sha_cache: dict[str, Optional[str]] = {}

    def _definition_sha(self, metric_name: str) -> Optional[str]:
        if metric_name not in self._sha_cache:
            self._sha_cache[metric_name] = definition_sha(self.inner, metric_name)
        return self._sha_cache[metric_name]

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        # Computed once per call, before the fetch, and used for both the
        # compare and the write, so a hit and the record it would be stored
        # under can never disagree about what the definition was.
        sha = self._definition_sha(metric_name)
        if not self.refresh:
            df = self.store.read(metric_name, start_date, end_date, grain, kind, sha)
            if df is not None:
                logger.info(
                    "snapshot hit: %s [%s, %s] %s", metric_name, start_date, end_date, grain
                )
                return _realign_snapshot(df, metric_name, grain, kind, start_date, end_date)

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
                definition_sha=sha,
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
        sha = self._definition_sha(metric_name)
        if not self.refresh:
            df = self.store.read_sliced(
                metric_name, dimension_source, start_date, end_date, grain, kind, sha
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
                definition_sha=sha,
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

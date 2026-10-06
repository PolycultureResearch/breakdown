"""Per-tree state for a process serving many metric trees (roadmap 2.16).

One `TreeState` per tree, held in `app.state.trees[id]`. Everything the old
single-tree app kept directly on `app.state` — parser, fetcher, data, the
caches, the lock — is per-tree and lives here; only `progress` stayed global,
because run ids are already unique.

Trees are peers. A company might keep one wide tree with revenue at the top, a
marketing tree detailing channels and campaigns, a product tree about feature
adoption and retention, and a tree standing behind a specific goal — each may
be durable or disposable, and each may declare a goal or not. Nothing here
ranks them or assumes a lifetime.

Two things are deliberately *not* per-tree:

- **The trace cap.** `MAX_CACHED_TRACES` entries per tree would be 256 x N
  InferenceData objects, each holding every posterior draw. One `TraceStore`
  keyed `(tree_id, metric, fit_end)` is shared by every tree; each tree gets a
  `TraceView` onto it that speaks the engine's own `(metric, fit_end)` key, so
  `run_rca` and `run_scenario` are untouched. The cap is a **byte budget**
  first and an entry count second — see `MAX_CACHED_TRACE_BYTES`.
- **The lock is per-tree**, which is the opposite move and for the same reason:
  two trees' caches are disjoint, so one global lock would park an RCA on the
  revenue tree behind a simulation on an unrelated marketing tree for no
  reason. (`waiting` progress now means "queued behind another run *on this
  tree*".)
"""

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, MutableMapping, Optional, Tuple

from breakdown.parser import Parser, TreeMeta

logger = logging.getLogger(__name__)

# Cap on cached fits, **process-wide** rather than per tree. Each entry is an
# InferenceData object holding every posterior draw, so an unbounded cache
# grows with distinct (tree, metric, analysis_start) triples until the process
# is OOM-killed — reachable without malice on the public demo, where each
# visitor picks their own windows (C8). Insertion-ordered eviction: dicts
# preserve order, so the oldest key is the first, and a refit re-inserts at the
# end. Generous enough that a normal session never evicts; a fit that is
# dropped is simply recomputed.
#
# The count alone cannot bound memory, because an entry's size scales with the
# loaded window *and* with the sampler: one NUTS fit at the engine's
# `NUTS_DRAWS` x `NUTS_CHAINS` (500 x 4) of the demo tree's `order_count` over
# an 830-day window measures **27.0 MB** of posterior, so 256 of them is
# ~6.9 GB against `demo/fly.toml`'s `memory = "2gb"`. Tuning the count down
# just moves the cliff to a wider window. So the real bound is a **byte
# budget**, with the count kept as a secondary backstop against a pathological
# number of tiny fits.
#
# Roadmap S25 (2026-09-30) roughly halved that figure without touching the
# budget: the NUTS posterior no longer carries the per-period `trend_z` latent
# beside `trend` (the level is integrated out and recovered after sampling),
# measured 23.1 MB -> 11.8 MB on the demo's 709-day `sessions`. The ADVI
# opt-in keeps the latent, and so keeps the larger size; the budget below is
# written for the larger of the two and is left there as headroom.
#
# Re-measured 2026-08-24 when NUTS became the default (roadmap S2); the figure
# here had been 13.4 MB, which was one 1000-draw *ADVI* fit — one chain, and
# the sampler no route runs by default any more. Roadmap C27 then fixed
# `NUTS_DRAWS` at 500 rather than `fit_metric`'s old 1000, which is what keeps
# 27.0 MB the right number: 1000 draws would roughly double it and halve the
# budget below.
MAX_CACHED_TRACES = 256

# 512 MiB, overridable with BREAKDOWN_MAX_TRACE_BYTES (bytes; 0 disables the
# byte bound and leaves only the count). The reasoning for the default: the
# smallest box this is expected to run on is the 2 GB demo VM, where the
# interpreter plus PyMC/ArviZ/PyTensor/pandas and one tree's frames sit around
# 0.5–0.7 GB resident, and a fit in flight transiently holds a sampler's
# working set plus the new trace on top of whatever is cached. Half a gigabyte
# of cache leaves roughly that much headroom again — about 19 of the 27.0 MB
# NUTS traces above, more than one session explores — while a wider window
# simply caches fewer fits instead of OOM-killing the process.
MAX_CACHED_TRACE_BYTES = 512 * 1024 * 1024

# Bounds for the two per-tree frame caches (see `TreeState`). Both count AND
# bytes (roadmap C32): the previous count-only bound was justified by one
# measurement of an ordinary frame (9,648 rows, 435 KB), and a frame scales
# with the dimension's *cardinality* — 830 days × 5,000 slice values is
# ~154 MB, so 64 of them is ~9.6 GB against the 2 GB demo box. 64 MiB per
# cache per tree holds ~150 ordinary frames (more than the count bound ever
# allowed) while a pathological dimension caches a handful, or none, instead
# of OOM-killing the process. The residual, named on the roadmap row: the
# *transient* fetch of one such frame still happens once per request until
# top_k + an `__other__` roll-up move into the generated SQL.
MAX_CACHED_SLICES = 64
MAX_CACHED_FLOWS = 64
MAX_CACHED_FRAME_BYTES = 64 * 1024 * 1024

_TraceKey = Tuple[str, Optional[str]]


class EngineBusy(Exception):
    """A cancelled request's engine thread is still finishing on this tree.

    The asyncio `tree.lock` released when its coroutine was cancelled (a
    middleware timeout, a shutdown, a client disconnect on Starlette versions
    that cancel), but threads are not cancellable and the orphan runs on. A
    second engine run beside it is the OOM rule 2 exists to prevent — two
    NUTS working sets on a 2 GB box — so the guard refuses with a 409 and a
    retry hint instead (roadmap C41).
    """


def guarded(tree: "TreeState", fn, *args, **kwargs):
    """Run one engine call under the tree's thread-side guard (roadmap C41).

    Non-blocking on purpose: the per-tree asyncio lock already serializes the
    ordinary flow, so any contention here means an orphaned run. Blocking
    would quietly queue engine work the caller believes it owns the lock for.

    Lives here, beside the guard it takes, rather than in `api/main.py` where
    it was written: `mcp/server.py` cannot import `api.main` at module scope
    (that module imports it, to mount the transport), and for a release the
    three MCP tools therefore called `asyncio.to_thread(engine_fn, …)` bare —
    the callers most likely to time out on a multi-minute analysis were the
    ones whose orphan held no guard (grill 2026-10-05 H7). Both surfaces
    import it from here now, and `tests/test_service_surface.py` enumerates
    every `to_thread` in both files.
    """
    if not tree.engine_guard.acquire(blocking=False):
        raise EngineBusy(
            f"Tree '{tree.id}' is still finishing an analysis whose caller "
            "went away (engine threads cannot be cancelled). Retry in a "
            "moment, once it completes."
        )
    try:
        return fn(*args, **kwargs)
    finally:
        tree.engine_guard.release()


def is_sampling_failure(exc: BaseException) -> bool:
    """Whether `exc` is PyMC's `ParallelSamplingError` (or a subclass).

    A chain that dies in a multi-process NUTS run raises
    `pymc.sampling.parallel.ParallelSamplingError`, which derives from
    `Exception` directly — neither `ValueError` nor `RuntimeError` — so C38's
    "a failed fit is a named refusal, not a 500" held only for single-process
    sampling (grill 2026-10-05 L3). Matched by class *name* along the MRO
    rather than by import: importing `pymc` here would put ~27s of
    PyTensor back on the boot path this package defers it off, and a name
    match keeps working if a PyMC release moves the class to another module.
    """
    return any(cls.__name__ == "ParallelSamplingError" for cls in type(exc).__mro__)


class SliceQueryFailed(Exception):
    """A provider could not run the sliced query for one declared dimension.

    Raised by `api.main._run_slice` around the provider fetch, so the slice
    route and the MCP tool can answer with a refusal that names the dimension
    instead of a bare 500 (grill 2026-10-05 M7): a mistyped `column:` in a
    binding surfaces as whatever the driver raises — `BinderException`,
    `ProgrammingError`, a DB-API error with no common base — and nothing but
    the call site knows that the failing query was *this dimension's*.

    Two texts, because the two halves have different audiences. `public`
    names the metric, the dimension and the error class, and is safe on a
    route auth leaves open. `cause` is the driver's own message, which quotes
    the generated SQL and so follows the `sql`/`bind` redaction (H6):
    `str(exc)` carries both, `public` only the first.
    """

    def __init__(self, metric: str, dimension: str, source: str, cause: BaseException):
        self.metric = metric
        self.dimension = dimension
        self.public = (
            f"Slicing '{metric}' by '{dimension}' failed: the provider could not run "
            f"the sliced query for dimension source '{source}' ({type(cause).__name__}). "
            "The usual cause is a `column:` in the metric's `bind.dimensions` that the "
            "relation does not have, or a dimension shape the provider cannot compile. "
            "`breakdown doctor --tree <path>` runs one sliced query per declared "
            "dimension and names the one that fails."
        )
        self.cause = f"{type(cause).__name__}: {cause}"
        super().__init__(f"{self.public} Cause: {self.cause}")


def refusal_message(exc: BaseException) -> Optional[str]:
    """The text to hand a caller when `exc` is a refusal; None when it is a crash.

    One judgement for both surfaces. HTTP turns the text into a 422 and MCP
    into a `ToolError`; before this lived in one place, HTTP had learned that
    `RuntimeError` is a refusal (roadmap C38) and MCP had not, so the
    per-metric-window message `/simulate` returned as a readable 422 reached
    an agent as `Error executing tool run_whatif` and nothing else (grill
    2026-10-05 M6).

    - `ValueError` / `RuntimeError` (which `SliceNotSupported` and
      `UnsupportedBinding` subclass): the engine's and providers' own named
      diagnoses, written for the caller.
    - `SliceQueryFailed`: a provider error on a sliced query, already worded.
    - A sampling failure (`is_sampling_failure`): the fit could not be
      sampled. The engine catches this at its fit sites and reports the node
      `fit_failed`; this is the backstop for a path that does not.

    Everything else — a `KeyError` on an engine internal, an `AttributeError`
    — is a crash, and stays one: there is nothing in it for a caller to act
    on. `EngineBusy` is deliberately not here; it is not a refusal of the
    request but of the moment, and each surface answers it on its own terms
    (a 409, a retryable `ToolError`).
    """
    if isinstance(exc, EngineBusy):
        return None
    if isinstance(exc, (ValueError, RuntimeError, SliceQueryFailed)):
        return str(exc)
    if is_sampling_failure(exc):
        return f"The model could not be sampled ({type(exc).__name__}): {exc}"
    return None


class WarmGate:
    """Process-wide state for the background warm (roadmap 3.10).

    Not on `TreeState`, for the reason the trace cap is not: the thing being
    bounded is the *process*. Each tree's warm is its own task behind its own
    per-tree lock, so N trees opened in a morning ran N NUTS fits at once —
    two measured on two trees, and each is a sampler's working set on a box
    sized for one (grill 2026-10-05 M5). `lock` admits one warm fit at a time
    across every tree. It is taken *before* the tree's own lock, so a tree
    waiting its turn holds nothing a person's request could queue behind.

    `stopping` is the shutdown flag, read between fits: a warm that is told to
    stop starts nothing new. One instance per lifespan (`app.state.warm_gate`)
    because an `asyncio.Lock` belongs to the loop that first awaits it.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.stopping = False


def _trace_nbytes(fit: Any) -> int:
    """Approximate resident size of one fit, cheaply and without copying it.

    `InferenceData` is xarray-backed, so summing `nbytes` across its groups
    reads the arrays' own shape/dtype metadata and touches no data. The honest
    alternative — `pickle.dumps(...)` — materializes a second full copy of the
    very object we are trying not to hold two of.

    Unknown shapes (a test double, a fit whose trace is not xarray-backed)
    measure 0 and are bounded by the entry count alone.

    The trace is not the only thing on a fit that scales with the loaded
    window, and until roadmap S10 it was the only thing counted. `dates` is one
    value per fitted period, and S10's `ppc_band` is six — the observed series
    plus five replicate quantiles. Both are small against the trace they ride
    with (measured together at 0.65% of it — 167 kB against a 24.6 MB trace —
    on the demo tree's `trials_started` over 790 days), and *small,
    therefore unmeasured* is precisely the argument the `slice_cache` defect
    was made of. A meter that cannot see a term under-reports every entry by a
    fixed fraction and stops being a bound on the thing that grows. Count them.

    One field is deliberately not counted, and it is named rather than
    forgotten: `summary_json` is filled lazily by `_fit_summary` on the first
    `GET /metrics/{name}`, which is *after* the store measured this entry, so
    at insert time there is nothing there to weigh.
    `test_every_window_scaled_field_on_a_fit_is_metered_or_named` holds the
    exception open.
    """
    total = 0
    trace = getattr(fit, "trace", None)
    groups = getattr(trace, "groups", None)
    if groups is not None:
        try:
            for name in groups():
                group = getattr(trace, name, None)
                nbytes = getattr(group, "nbytes", 0)
                total += int(nbytes)
        except Exception:  # pragma: no cover - never let sizing break a cache write
            return 0
    return total + _fit_series_nbytes(fit)


#: Bytes one `ppc_band` period costs. Six series (observed plus five
#: quantiles) of Python floats — 24 bytes for the float object, 8 for the list
#: slot pointing at it — plus ~11 for that period's `YYYY-MM-DD` string.
#: Analytic rather than `sys.getsizeof` (which does not recurse into a list)
#: or `pickle.dumps` (which is the second full copy `_trace_nbytes` exists to
#: avoid).
_PPC_BAND_BYTES_PER_PERIOD = 6 * 32 + 11


def _fit_series_nbytes(fit: Any) -> int:
    """The per-period arrays hanging off a `FitResult` outside its trace.

    O(1): both lengths are already known — `dates` reports its own, and the
    band carries `n_periods` — so nothing here walks a series.
    """
    total = 0
    dates = getattr(fit, "dates", None)
    try:
        # A DatetimeIndex is int64 nanoseconds behind the object wrapper.
        total += len(dates) * 8 if dates is not None else 0
    except TypeError:  # pragma: no cover - a test double with no length
        pass
    band = getattr(fit, "ppc_band", None)
    if isinstance(band, dict):
        try:
            total += max(int(band.get("n_periods") or 0), 0) * _PPC_BAND_BYTES_PER_PERIOD
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    return total


def _byte_budget() -> int:
    """`MAX_CACHED_TRACE_BYTES`, or BREAKDOWN_MAX_TRACE_BYTES when set."""
    raw = os.environ.get("BREAKDOWN_MAX_TRACE_BYTES")
    if not raw:
        return MAX_CACHED_TRACE_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "BREAKDOWN_MAX_TRACE_BYTES=%r is not an integer number of bytes; "
            "using the default of %d.",
            raw,
            MAX_CACHED_TRACE_BYTES,
        )
        return MAX_CACHED_TRACE_BYTES
    return max(value, 0)


class TraceStore:
    """Process-wide LRU of fitted models, keyed `(tree_id, metric, fit_end)`.

    Bounded by total bytes first and entry count second (see
    `MAX_CACHED_TRACE_BYTES`), evicting insertion-ordered — oldest first —
    until both fit. The newest entry is never evicted: the caller is holding it
    and about to serve from it, and a single fit larger than the whole budget
    should degrade to "cache of one", not to "cache of none".
    """

    def __init__(self, max_entries: int = MAX_CACHED_TRACES, max_bytes: Optional[int] = None):
        self.max_entries = max_entries
        self.max_bytes = _byte_budget() if max_bytes is None else max_bytes
        self._entries: Dict[Tuple[str, str, Optional[str]], Any] = {}
        self._sizes: Dict[Tuple[str, str, Optional[str]], int] = {}
        self.total_bytes = 0
        # One lock for every mutation (roadmap C41, grill M4). The module
        # docstring's "two trees' caches are disjoint" was true of the *keys*
        # and false of the store: every TreeState's TraceView writes into this
        # one `_entries` dict and this one `total_bytes`, from worker threads
        # the per-tree asyncio locks do not serialize against each other. Two
        # concurrent RCAs on two trees reproduce `dictionary changed size
        # during iteration` inside `_evict` (the C8 failure class, back on the
        # writer side) and drift `total_bytes` low — which silently disarms
        # the byte budget rule 2 exists for. Microseconds per fit; and on the
        # free-threaded 3.14 build the race window stops being "a few
        # bytecodes" and becomes routine.
        self._lock = threading.Lock()

    def view(self, tree_id: str) -> "TraceView":
        return TraceView(self, tree_id)

    def _forget(self, key: Tuple[str, str, Optional[str]]) -> None:
        self._entries.pop(key, None)
        self.total_bytes -= self._sizes.pop(key, 0)

    def _evict(self) -> None:
        while len(self._entries) > 1 and (
            len(self._entries) > self.max_entries
            or (self.max_bytes and self.total_bytes > self.max_bytes)
        ):
            self._forget(next(iter(self._entries)))


class TraceView(MutableMapping):
    """One tree's slice of the shared `TraceStore`, as the engine's own dict.

    The engine caches by `(metric, fit_end)` and writes into the mapping it was
    handed (`run_rca` adds on-demand fits in place), so the tree id is folded in
    here rather than in `engine/`: `fit_metric` stays a pure function of its
    arguments and nothing downstream learns that more than one tree exists.

    `__iter__` snapshots with `list(...)` before filtering, for the same reason
    `/meta` does (C8): a worker thread inserts into this dict while the event
    loop reads it, and a lazy generator over a live dict raises "dictionary
    changed size during iteration" — an intermittent 500 for one viewer exactly
    while another's analysis runs.
    """

    def __init__(self, store: TraceStore, tree_id: str):
        self._store = store
        self._tree_id = tree_id

    def __getitem__(self, key: _TraceKey) -> Any:
        try:
            return self._store._entries[(self._tree_id, *key)]
        except KeyError:
            raise KeyError(key)

    def __setitem__(self, key: _TraceKey, value: Any) -> None:
        store_key = (self._tree_id, *key)
        # A refit re-inserts at the end (insertion order is the eviction
        # order), so drop the old entry and its bytes first rather than
        # overwriting in place.
        store = self._store
        # Sizing outside the lock (it walks xarray metadata); mutation inside.
        size = _trace_nbytes(value)
        with store._lock:
            store._forget(store_key)
            store._entries[store_key] = value
            store._sizes[store_key] = size
            store.total_bytes += size
            store._evict()

    def put_oldest(self, key: _TraceKey, value: Any) -> None:
        """Insert as the *oldest* entry: first in line for eviction.

        For fits nobody asked for yet (roadmap 3.10's background warm). An
        ordinary write goes to the back and can push the oldest entries out;
        a warm write going to the back could evict a fit a person requested
        minutes ago, to make room for one they may never open. At the front,
        if the budget is short, the warm fit is the one that goes. The store's
        "never evict the newest" rule protects the back, so it does not stop
        this.
        """
        store_key = (self._tree_id, *key)
        store = self._store
        size = _trace_nbytes(value)
        with store._lock:
            store._forget(store_key)
            store._entries = {store_key: value, **store._entries}
            store._sizes[store_key] = size
            store.total_bytes += size
            store._evict()

    def __delitem__(self, key: _TraceKey) -> None:
        store_key = (self._tree_id, *key)
        with self._store._lock:
            if store_key not in self._store._entries:
                raise KeyError(key)
            self._store._forget(store_key)

    def __iter__(self) -> Iterator[_TraceKey]:
        return iter([k[1:] for k in list(self._store._entries) if k[0] == self._tree_id])

    def __len__(self) -> int:
        return sum(1 for k in list(self._store._entries) if k[0] == self._tree_id)


class BoundedCache(Dict[Any, Any]):
    """A dict that evicts oldest-first once it exceeds `max_entries` — or,
    when `max_bytes` is set, once its entries' measured bytes exceed it.

    The same insertion-ordered spirit as `TraceStore`, small enough to stay a
    dict: callers `.get`, `[]=`, `len()` and `.clear()` it exactly as before,
    and the only added behaviour is on write.

    The byte bound exists because the entry count was bounding the wrong
    quantity (roadmap C32, grill H3, AGENTS.md rule 2): a cached slice frame
    scales with the *dimension's cardinality* times the window, and 64 entries
    of an ordinary frame (9,648 rows measured 435 KB) is a few tens of MB
    while 64 frames of a 5,000-value dimension over 830 days is ~9.6 GB —
    against the demo box's 2 GB. Bound by the thing that actually grows.

    An entry larger than the whole budget is not cached at all (and the drop
    is logged): caching it would evict everything else to hold one frame that
    the next insert evicts anyway. The request still works — it just refetches
    — which is the same trade `TraceStore` makes when a wide window shrinks
    how many fits fit.
    """

    def __init__(self, max_entries: int, max_bytes: int = 0):
        super().__init__()
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self._sizes: Dict[Any, int] = {}

    @staticmethod
    def _nbytes(value: Any) -> int:
        """A DataFrame's resident size (deep: slice labels are object-dtype
        strings, which shallow counting misses entirely). Unknown shapes
        measure 0 and are bounded by the entry count alone."""
        try:
            return int(value.memory_usage(deep=True).sum())
        except Exception:
            return 0

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            del self[key]
        size = self._nbytes(value)
        if self.max_bytes and size > self.max_bytes:
            logger.warning(
                "Not caching a %.0f MB frame (budget %.0f MB): it would evict "
                "the whole cache and be evicted by the next insert. The "
                "request is served; repeats of it will refetch.",
                size / 1e6,
                self.max_bytes / 1e6,
            )
            return
        super().__setitem__(key, value)
        self._sizes[key] = size
        self.total_bytes += size
        while len(self) > self.max_entries or (
            self.max_bytes and self.total_bytes > self.max_bytes and len(self) > 1
        ):
            del self[next(iter(self))]

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        self.total_bytes -= self._sizes.pop(key, 0)

    def clear(self) -> None:
        super().clear()
        self._sizes.clear()
        self.total_bytes = 0


@dataclass
class TreeState:
    """Everything one tree owns. `app.state.trees[id]`."""

    id: str
    path: str
    # Parsed at boot for every tree (cheap, no I/O beyond the file) so the
    # index can answer instantly; `data` is fetched lazily.
    parser: Optional[Parser] = None
    meta: Optional[TreeMeta] = None
    # Why this tree can't serve: a YAML/parse failure found at boot, or a
    # provider failure found on load. Per-tree so one malformed file in a
    # directory doesn't take down the other seven — the same degraded-startup
    # discipline the single-tree app had, scoped down.
    load_error: Optional[str] = None
    # A stable, secret-free classification of `load_error` for the surfaces
    # auth deliberately leaves open (roadmap C43): "parse_error" |
    # "data_load_error". The full exception text stays on `load_error`, which
    # only the log and the auth-gated routes read — it can carry the tree's
    # SQL or a provider's hostnames and usernames, which is exactly what the
    # unauthenticated /health was echoing.
    load_error_kind: Optional[str] = None
    fetcher: Any = None
    data: Any = None
    loaded: bool = False
    loading: bool = False
    traces: MutableMapping = field(default_factory=dict)
    # Read-through provider caches, bounded for the same reason `traces` is:
    # both are keyed by caller-chosen windows, so on a public deployment where
    # every visitor picks their own they grow without limit and nothing in the
    # package ever evicted from them. Per tree rather than process-wide,
    # because unlike a trace a frame is small (see `MAX_CACHED_SLICES`) and two
    # trees naming the same metric are two independent nodes anyway.
    slice_cache: Dict[Any, Any] = field(
        default_factory=lambda: BoundedCache(MAX_CACHED_SLICES, MAX_CACHED_FRAME_BYTES)
    )
    flow_cache: Dict[Any, Any] = field(
        default_factory=lambda: BoundedCache(MAX_CACHED_FLOWS, MAX_CACHED_FRAME_BYTES)
    )
    # Thread-side twin of `lock` (roadmap C41, grill M11): the asyncio lock
    # releases when the awaiting coroutine is *cancelled*, but the engine
    # thread it launched keeps running — writing into `traces` — so a second
    # run can start beside the orphan and two NUTS fits side by side is an
    # OOM on the 2 GB box. Acquired non-blocking inside the worker function
    # itself (`guarded`, above): contention is impossible in the
    # ordinary flow, so hitting it *means* an orphan is still finishing, and
    # the honest answer is a 409 naming that, not a second sampler.
    engine_guard: threading.Lock = field(default_factory=threading.Lock)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    earliest: Dict[str, Optional[str]] = field(default_factory=dict)
    earliest_task: Optional[asyncio.Task] = None
    # Roadmap 3.10's background warm of each metric's default analysis (opt-in,
    # BREAKDOWN_WARM=latest). `warm` is the status `/meta` reports, replaced
    # whole rather than mutated, like `progress`: the task writes it from the
    # event loop and handlers read it there too. What is process-wide about
    # the warm — one fit at a time, the stop flag — is `WarmGate`, not here.
    warm: Dict[str, Any] = field(default_factory=dict)
    warm_task: Optional[asyncio.Task] = None

    @property
    def title(self) -> str:
        """Display name: `tree.title`, else the id (the filename stem)."""
        if self.meta is not None and self.meta.title:
            return self.meta.title
        return self.id

    @property
    def state(self) -> str:
        """`loaded` | `loading` | `not_loaded` | `error` — the field that keeps
        the index honest. `progress: null` with `not_loaded` means *we haven't
        looked*, which the UI must render differently from a real zero."""
        if self.load_error is not None:
            return "error"
        if self.loaded:
            return "loaded"
        if self.loading:
            return "loading"
        return "not_loaded"

    @property
    def provider_type(self) -> Optional[str]:
        return self.parser.config.provider.type if self.parser else None


def discover_trees(path: str) -> Dict[str, TreeState]:
    """Build the `TreeState` map from `--tree`: a file or a directory.

    A directory is globbed for `*.yml`/`*.yaml`, **non-recursively** — a
    `breakdown/` folder in a dbt repo, not a whole project tree. The id is
    always the filename stem: stable, greppable, obvious in logs, legible in a
    `#tree=` deep link, and impossible for two files to collide on (a `tree.id`
    key could not say that).

    A file argument keeps today's behavior exactly: one tree, its id its stem,
    and it is the default.
    """
    if os.path.isdir(path):
        names = sorted(
            entry
            for entry in os.listdir(path)
            if entry.endswith((".yml", ".yaml")) and not entry.startswith(".")
        )
        files = [os.path.join(path, name) for name in names]
        if not files:
            raise RuntimeError(
                f"No metric trees found in directory '{path}' "
                "(looked for *.yml / *.yaml, non-recursively)."
            )
    else:
        if not os.path.isfile(path):
            raise RuntimeError(f"Metric tree not found: {path}")
        files = [path]

    trees: Dict[str, TreeState] = {}
    for file_path in files:
        tree_id = os.path.splitext(os.path.basename(file_path))[0]
        trees[tree_id] = TreeState(id=tree_id, path=file_path)
    return trees


def parse_tree(tree: TreeState) -> None:
    """Parse one tree's YAML into its `Parser` + `TreeMeta`.

    Failure-soft **per tree**: one malformed file in a directory must not take
    down the other seven, so the error is recorded on the tree and shows as a
    broken card on the index rather than raising into the process."""
    try:
        with open(tree.path, "r") as f:
            parser = Parser(f.read())
    except Exception as e:
        tree.load_error = f"{type(e).__name__}: {e}"
        tree.load_error_kind = "parse_error"
        logger.error(
            "Failed to parse tree '%s' (%s); it will show as errored. %s",
            tree.id,
            tree.path,
            e,
        )
        return
    tree.parser = parser
    tree.meta = parser.config.tree


def resolve_default(trees: Dict[str, TreeState], requested: Optional[str]) -> str:
    """`--default-tree <id>`, else the single tree if there is one, else the
    alphabetically first. It backs the unprefixed route aliases and is what a
    bare `/ui` opens."""
    if requested:
        if requested not in trees:
            raise RuntimeError(
                f"--default-tree '{requested}' is not one of the discovered "
                f"trees: {', '.join(sorted(trees))}"
            )
        return requested
    return sorted(trees)[0]

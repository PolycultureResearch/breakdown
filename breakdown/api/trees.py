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
# loaded window: one ADVI fit (1000 draws) of the demo tree's `order_count`
# over an 830-day window measures **13.4 MB** of posterior, so 256 of them is
# ~3.4 GB against `demo/fly.toml`'s `memory = "2gb"`. Tuning the count down
# just moves the cliff to a wider window. So the real bound is a **byte
# budget**, with the count kept as a secondary backstop against a pathological
# number of tiny fits.
MAX_CACHED_TRACES = 256

# 512 MiB, overridable with BREAKDOWN_MAX_TRACE_BYTES (bytes; 0 disables the
# byte bound and leaves only the count). The reasoning for the default: the
# smallest box this is expected to run on is the 2 GB demo VM, where the
# interpreter plus PyMC/ArviZ/PyTensor/pandas and one tree's frames sit around
# 0.5–0.7 GB resident, and a fit in flight transiently holds a sampler's
# working set plus the new trace on top of whatever is cached. Half a gigabyte
# of cache leaves roughly that much headroom again — about 38 of the 13.4 MB
# traces above, far more than one session explores — while a wider window
# simply caches fewer fits instead of OOM-killing the process.
MAX_CACHED_TRACE_BYTES = 512 * 1024 * 1024

# Bound for the two per-tree frame caches (see `TreeState`). Counts, not bytes:
# a cached slice frame is a DataFrame of one metric by one dimension over one
# window — 9,648 rows measured 435 KB, two orders of magnitude under a trace —
# so 64 of each per tree is a few tens of MB at worst, and the count stays the
# simplest thing that can be reasoned about.
MAX_CACHED_SLICES = 64
MAX_CACHED_FLOWS = 64

_TraceKey = Tuple[str, Optional[str]]


def _trace_nbytes(fit: Any) -> int:
    """Approximate resident size of one fit, cheaply and without copying it.

    `InferenceData` is xarray-backed, so summing `nbytes` across its groups
    reads the arrays' own shape/dtype metadata and touches no data. The honest
    alternative — `pickle.dumps(...)` — materializes a second full copy of the
    very object we are trying not to hold two of.

    Unknown shapes (a test double, a fit whose trace is not xarray-backed)
    measure 0 and are bounded by the entry count alone.
    """
    trace = getattr(fit, "trace", None)
    groups = getattr(trace, "groups", None)
    if groups is None:
        return 0
    total = 0
    try:
        for name in groups():
            group = getattr(trace, name, None)
            nbytes = getattr(group, "nbytes", 0)
            total += int(nbytes)
    except Exception:  # pragma: no cover - never let sizing break a cache write
        return 0
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
        store._forget(store_key)
        store._entries[store_key] = value
        size = _trace_nbytes(value)
        store._sizes[store_key] = size
        store.total_bytes += size
        store._evict()

    def __delitem__(self, key: _TraceKey) -> None:
        store_key = (self._tree_id, *key)
        if store_key not in self._store._entries:
            raise KeyError(key)
        self._store._forget(store_key)

    def __iter__(self) -> Iterator[_TraceKey]:
        return iter([k[1:] for k in list(self._store._entries) if k[0] == self._tree_id])

    def __len__(self) -> int:
        return sum(1 for k in list(self._store._entries) if k[0] == self._tree_id)


class BoundedCache(Dict[Any, Any]):
    """A dict that evicts its oldest entry once it exceeds `max_entries`.

    The same insertion-ordered spirit as `TraceStore`, small enough to stay a
    dict: callers `.get`, `[]=`, `len()` and `.clear()` it exactly as before,
    and the only added behaviour is on write.
    """

    def __init__(self, max_entries: int):
        super().__init__()
        self.max_entries = max_entries

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        while len(self) > self.max_entries:
            del self[next(iter(self))]


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
    slice_cache: Dict[Any, Any] = field(default_factory=lambda: BoundedCache(MAX_CACHED_SLICES))
    flow_cache: Dict[Any, Any] = field(default_factory=lambda: BoundedCache(MAX_CACHED_FLOWS))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    earliest: Dict[str, Optional[str]] = field(default_factory=dict)
    earliest_task: Optional[asyncio.Task] = None

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

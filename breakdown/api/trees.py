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
  `run_rca` and `run_scenario` are untouched.
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
MAX_CACHED_TRACES = 256

_TraceKey = Tuple[str, Optional[str]]


class TraceStore:
    """Process-wide LRU of fitted models, keyed `(tree_id, metric, fit_end)`."""

    def __init__(self, max_entries: int = MAX_CACHED_TRACES):
        self.max_entries = max_entries
        self._entries: Dict[Tuple[str, str, Optional[str]], Any] = {}

    def view(self, tree_id: str) -> "TraceView":
        return TraceView(self, tree_id)

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))


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
        self._store._entries[(self._tree_id, *key)] = value
        self._store._evict()

    def __delitem__(self, key: _TraceKey) -> None:
        try:
            del self._store._entries[(self._tree_id, *key)]
        except KeyError:
            raise KeyError(key)

    def __iter__(self) -> Iterator[_TraceKey]:
        return iter([k[1:] for k in list(self._store._entries) if k[0] == self._tree_id])

    def __len__(self) -> int:
        return sum(1 for k in list(self._store._entries) if k[0] == self._tree_id)


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
    slice_cache: Dict[Any, Any] = field(default_factory=dict)
    flow_cache: Dict[Any, Any] = field(default_factory=dict)
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

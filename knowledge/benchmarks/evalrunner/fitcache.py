"""The fit cache: keyed by world, node, fit end and model configuration.

A fit is identified by everything that decides its posterior: which world's
data, which node, where the fit ends, the node's own definition, the series
it is regressed on, the sampler settings and seed, and the engine's code. Two
arms that leave a node's configuration alone therefore share its fit, and a
rerun refits nothing.

There are two things to keep per key, and they grow very differently:

- **The scored rows** a fit produced (`rows/*.jsonl`): a few hundred bytes
  each, kept for every fit, never evicted. This is what makes a rerun, or an
  arm that only changes the ranking, free. A null-world fit has exactly one
  consumer, its own calibration rows, so its row is its whole cache entry.
- **The trace** (`traces/<key>.pkl`, a pickled `FitResult`): megabytes each,
  growing with draws x periods because the recovered level is stored per
  period. Only a single world's fits are written, since those have several
  consumers (detection, both innovation rankings, the engine's own RCA). The
  directory is bounded in **bytes**, not entries (`AGENTS.md` rule 2: bound by
  the thing that grows), evicting least recently used first. Eviction costs a
  refit and never a wrong number.

Several shard processes share the directory. Writes are atomic (temp file,
then rename); an eviction racing a read is a miss.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
from pathlib import Path
from typing import Any, Callable

import networkx as nx

DEFAULT_TRACE_CACHE_BYTES = 4 * 1024**3


def model_config(dag: nx.DiGraph, node: str) -> dict[str, Any]:
    """What about the tree decides `node`'s fit: its own definition, and the
    grain and kind each parent is resampled by."""
    defn = dag.nodes[node]["definition"]
    return {
        "definition": defn.model_dump(mode="json", exclude={"description", "format", "direction"}),
        "parents": [
            {
                "name": p,
                "grain": dag.nodes[p]["definition"].grain,
                "kind": dag.nodes[p]["definition"].kind,
            }
            for p in dag.predecessors(node)
        ],
    }


def fit_key(world_key: str, dag: nx.DiGraph, node: str, fit_end: str, engine_key: str) -> str:
    payload = json.dumps(
        {
            "world": world_key,
            "node": node,
            "fit_end": fit_end,
            "model": model_config(dag, node),
            "engine": engine_key,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class FitStore:
    """The `fits` mapping the scoring code and `run_rca` read and write,
    keyed `(node, fit_end)` for one world, optionally backed by the trace
    directory.

    Holds a `FitResult` or the exception a fit raised. `get` (what `run_rca`
    calls) returns results only. Every new fit is reported to `on_fit` with
    its wall time, so cost is logged in one place whoever did the fitting.
    """

    def __init__(
        self,
        world_key: str,
        dag: nx.DiGraph,
        engine_key: str,
        *,
        trace_dir: Path | None = None,
        max_bytes: int = DEFAULT_TRACE_CACHE_BYTES,
        on_fit: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.world_key = world_key
        self.dag = dag
        self.engine_key = engine_key
        self.trace_dir = trace_dir if trace_dir and max_bytes > 0 else None
        self.max_bytes = max_bytes
        self.on_fit = on_fit
        self._mem: dict[tuple[str, str], Any] = {}
        self._clock = time.perf_counter()
        self.hits = 0
        if self.trace_dir:
            self.trace_dir.mkdir(parents=True, exist_ok=True)

    def key(self, node: str, fit_end: str) -> str:
        return fit_key(self.world_key, self.dag, node, fit_end, self.engine_key)

    def _path(self, k: tuple[str, str]) -> Path:
        return self.trace_dir / f"{self.key(*k)}.pkl"

    def _load(self, k: tuple[str, str]) -> bool:
        if k in self._mem:
            return True
        if self.trace_dir is None:
            return False
        path = self._path(k)
        try:
            with open(path, "rb") as f:
                self._mem[k] = pickle.load(f)
            os.utime(path)  # recently used
        except (FileNotFoundError, EOFError, pickle.UnpicklingError):
            return False
        self.hits += 1
        return True

    def __contains__(self, k) -> bool:
        found = self._load(k)
        if not found:
            self._clock = time.perf_counter()  # a fit is about to start
        return found

    def __getitem__(self, k):
        if not self._load(k):
            raise KeyError(k)
        return self._mem[k]

    def get(self, k, default=None):
        if not self._load(k) or isinstance(self._mem[k], Exception):
            self._clock = time.perf_counter()
            return default
        return self._mem[k]

    def __setitem__(self, k, fit) -> None:
        now = time.perf_counter()
        seconds, self._clock = now - self._clock, now
        self._mem[k] = fit
        size = None
        if self.trace_dir is not None:
            stored = RuntimeError(str(fit)) if isinstance(fit, Exception) else fit
            path = self._path(k)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                pickle.dump(stored, f, protocol=pickle.HIGHEST_PROTOCOL)
            size = tmp.stat().st_size
            os.replace(tmp, path)
            enforce_byte_cap(self.trace_dir, self.max_bytes)
        if self.on_fit:
            self.on_fit(fit_record(k, fit, seconds, self.key(*k), size))


def fit_record(k, fit, seconds: float, key: str, size: int | None) -> dict[str, Any]:
    node, fit_end = k
    out = {"node": node, "fit_end": fit_end, "fit_key": key, "fit_seconds": round(seconds, 2)}
    if isinstance(fit, Exception):
        return {**out, "ok": False, "error": str(fit)[:200]}
    d = fit.diagnostics
    return {
        **out,
        "ok": True,
        "trace_bytes": size,
        "n_periods": len(fit.dates),
        "likelihood": fit.likelihood,
        "trend": fit.trend_kind,
        "min_ess": d.get("min_ess_bulk"),
        "divergences": d.get("divergences"),
        "max_rhat": d.get("max_rhat"),
        "fit_quality": d.get("fit_quality"),
        "ppc_status": d.get("ppc_status"),
    }


def enforce_byte_cap(trace_dir: Path, max_bytes: int) -> int:
    """Delete least-recently-used traces until the directory fits. Returns the
    number removed. Safe against another process doing the same."""
    entries = []
    for path in trace_dir.glob("*.pkl"):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        entries.append((st.st_mtime, st.st_size, path))
    total = sum(size for _, size, _ in entries)
    removed = 0
    for _, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        total -= size
        removed += 1
    return removed

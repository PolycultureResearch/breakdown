"""Which fits to make ahead of time so the default analysis is a cache hit.

Roadmap 3.10, step 1 (`knowledge/speed_and_warm_analyses_design.md` §4.2).
An RCA's fits depend only on `analysis_start` (`rca._node_fit_end`), and the
trace cache is keyed `(metric, fit_end)`. So the analysis a person is most
likely to run first, the UI's default window for whichever metric they open,
can be fitted before they open it.

This module only *plans*. It is a pure function of the tree, its data and the
cache, like the rest of `engine/`; running the fits, under the tree's lock and
off the event loop, is the API's job (`api/main.py`, `_warm_latest`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import networkx as nx

from ..grains import coarsest, default_analysis_window, ensure_grained
from .rca import plan_rca_fits

logger = logging.getLogger(__name__)


class WarmFit(NamedTuple):
    """One fit to make: the cache key `(node, fit_end)`, and the first target
    whose default analysis asked for it (for the log and the status)."""

    node: str
    fit_end: str
    target: str


def default_analysis_for(dag: nx.DiGraph, data: Any, target: str) -> Optional[Tuple[str, str]]:
    """The UI's default analysis window for `target`, or None.

    The same inputs `app.js` reads from `/meta`: the coarsest grain across the
    target's ancestor scope, and the earliest `data_through` across it
    (`scopeDataEnd`), falling back to the loaded window's end.
    """
    data = ensure_grained(data)
    scope = nx.ancestors(dag, target) | {target}
    grain = coarsest(data.grain_of.get(n, "day") for n in scope)
    edge = data.date_end
    for n in scope:
        through = data.data_through(n) if n in data.grain_of else None
        if through is not None and through < edge:
            edge = through
    return default_analysis_window(data.date_start, edge, grain)


def plan_warm_fits(
    dag: nx.DiGraph,
    data: Any,
    traces: Dict[Tuple[str, Optional[str]], Any],
) -> Tuple[List[WarmFit], Dict[str, Dict[str, str]]]:
    """Every fit the default analysis of any metric would need, deduplicated.

    Returns `(fits, windows)`: the fits not already usable in `traces`, in a
    deterministic order, and the default window per target that produced
    them. A target whose default analysis cannot run (too little data, a
    window the engine refuses) is skipped and logged, because a warm that
    raises would take nothing down but would also warm nothing else.

    Every key comes from `rca.plan_rca_fits`, the function `run_rca` itself
    calls, so a warmed fit is the fit a real request looks up.
    """
    data = ensure_grained(data)
    fits: List[WarmFit] = []
    windows: Dict[str, Dict[str, str]] = {}
    seen = set()
    for target in sorted(dag.nodes):
        if target not in data.grain_of:
            continue
        window = default_analysis_for(dag, data, target)
        if window is None:
            continue
        try:
            plan = plan_rca_fits(
                dag, data, traces, target, analysis_start=window[0], analysis_end=window[1]
            )
        except (ValueError, RuntimeError) as e:
            logger.info("warm: skipping '%s' (%s → %s): %s", target, window[0], window[1], e)
            continue
        windows[target] = {"analysis_start": window[0], "analysis_end": window[1]}
        for node in plan.to_fit:
            key = (node, plan.fit_ends[node])
            if key in seen:
                continue
            seen.add(key)
            fits.append(WarmFit(node, plan.fit_ends[node], target))
    return fits, windows

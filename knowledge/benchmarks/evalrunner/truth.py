"""A single world's planted anomaly, in the tree's terms.

The generator labels a root as a metric and a slice (`root_metric`,
`root_slice`) and measures what moved and when from the twin difference
(`measured_effects`). A tree has nodes. This module is the translation, and it
is deliberately small and explicit, because every choice in it changes a
score:

- **Root -> node** (`map_root`). A node bound to the root metric at the root
  slice, else the node bound to the metric's total, else a derived node of the
  same name. None of those: the world is **off-tree** for this tree, and is
  recorded as such, never forced onto a neighbour.
- **A root that lands on a formula node.** The generator roots some anomalies
  at a count (a spike in the churn *rate* is rooted at `churned_subscriptions`,
  because that is the metric its semantic layer can slice). In a tree that
  count is often an exact identity of a stock and a rate, and an identity
  cannot be a root (innovation design §4.1). The truth is then the formula's
  parents that the measured effects say moved, followed up through formulas.
- **Onset** (`onset_of`). The date the node's own series first clears its
  noise floor in the twin difference (`onset`); failing that the first day
  the twins differ at all (`first_diff`) when that is not before the declared
  start (most entity builders re-deal earlier days, so an early `first_diff`
  is realization noise, not arrival); failing that the declared start. The
  source used is recorded on every row.
- **Target** (`pick_target`). The deepest node downstream of the truth whose
  metric the measured effects say moved: "the top metric the anomaly reaches".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import pandas as pd

from breakdown.grains import floor_period

from .loader import SeriesBinding, TreePlan

MAX_ANALYSIS_DAYS = 28


@dataclass
class RootMapping:
    status: str  # on_tree | on_tree_slice | via_formula_parents | off_tree
    root_node: str | None  # the node the generator's label lands on
    truth: list[str] = field(default_factory=list)  # the node(s) scored as the root
    note: str = ""

    @property
    def on_tree(self) -> bool:
        return bool(self.truth)


def metric_of(node: str, plan: TreePlan, metrics: set[str]) -> str | None:
    """The generator metric a node's movement is read from."""
    if node in plan.served:
        return plan.served[node].metric
    return node if node in metrics else None


def node_moved(node: str, plan: TreePlan, record: dict[str, Any]) -> bool:
    effects = record.get("measured_effects") or {}
    metric = metric_of(node, plan, set(effects))
    if metric is None:
        return False
    confirmed = record.get("declared_affected_metrics_confirmed") or {}
    return bool((effects.get(metric) or {}).get("moved")) or metric in (
        confirmed.get("moved_in_slice_only") or []
    )


def map_root(dag: nx.DiGraph, plan: TreePlan, record: dict[str, Any]) -> RootMapping:
    metric = record.get("root_metric")
    root_slice = record.get("root_slice") or {}
    if not metric:
        return RootMapping("off_tree", None, note="the record has no root_metric")

    def bound(match) -> str | None:
        for node, binding in plan.served.items():
            if binding.metric == metric and not binding.cumulative and match(binding):
                return node
        return None

    def at_slice(b: SeriesBinding) -> bool:
        return b.dimension in root_slice and root_slice[b.dimension] in b.values

    node = bound(at_slice)
    status = "on_tree_slice"
    if node is None:
        node, status = bound(lambda b: b.dimension is None), "on_tree"
    if node is None and metric in dag and metric not in plan.served:
        node = metric  # a derived node carrying the metric's name
    if node is None:
        why = "was dropped from the tree as unserved" if metric in plan.dropped else "is not a node"
        return RootMapping("off_tree", None, note=f"root metric '{metric}' {why}")

    truth, seen = [node], set()
    while any(_is_formula(dag, n) for n in truth):
        nxt = []
        for n in truth:
            if not _is_formula(dag, n):
                nxt.append(n)
                continue
            seen.add(n)
            moved = [
                p for p in dag.predecessors(n) if p not in seen and node_moved(p, plan, record)
            ]
            if not moved:
                # Nothing upstream is known to have moved: keep the identity
                # itself as the label and say so, instead of guessing a parent.
                return RootMapping(
                    status,
                    node,
                    [node],
                    note=f"'{node}' is a formula and no parent of '{n}' is measured as moved",
                )
            nxt.extend(moved)
        truth = list(dict.fromkeys(nxt))
        status = "via_formula_parents"
    return RootMapping(status, node, truth)


def _is_formula(dag: nx.DiGraph, node: str) -> bool:
    return bool(dag.nodes[node]["definition"].formula) and dag.in_degree(node) > 0


def onset_of(
    node: str, plan: TreePlan, record: dict[str, Any], *, is_root: bool
) -> tuple[pd.Timestamp, str]:
    """`(date, source)` for when `node`'s own series first moves."""
    declared = pd.Timestamp(record["start"])
    effects = record.get("measured_effects") or {}
    candidates = [("node", effects.get(metric_of(node, plan, set(effects)) or ""))]
    if is_root:
        candidates.append(("root_slice", record.get("root_effect")))
    for label, effect in candidates:
        if effect and effect.get("onset"):
            return pd.Timestamp(effect["onset"]), f"onset:{label}"
    for label, effect in candidates:
        if effect and effect.get("first_diff"):
            first = pd.Timestamp(effect["first_diff"])
            if first >= declared:
                return first, f"first_diff:{label}"
    return declared, "declared_start"


def pick_target(
    dag: nx.DiGraph, plan: TreePlan, record: dict[str, Any], truth: list[str]
) -> tuple[str | None, str]:
    """`(target, basis)`: the deepest moved node downstream of the truth."""
    downstream = set().union(*(nx.descendants(dag, t) for t in truth)) - set(truth)
    if not downstream:
        return None, "the root has no descendant in the tree"
    depth = {}
    for n in nx.topological_sort(dag):
        depth[n] = 1 + max((depth[p] for p in dag.predecessors(n)), default=-1)
    order = {n: i for i, n in enumerate(nx.topological_sort(dag))}

    def deepest(nodes):
        return max(nodes, key=lambda n: (depth[n], order[n]))

    moved = [n for n in downstream if node_moved(n, plan, record)]
    if moved:
        return deepest(moved), "measured"
    return deepest(downstream), "fallback_deepest_descendant"


def analysis_window(onset, record: dict[str, Any], grain: str) -> tuple[str, str]:
    """The target's window: from the period holding the onset, for as long as
    the anomaly lasts, at most `MAX_ANALYSIS_DAYS`, in whole periods."""
    start = floor_period(pd.Timestamp(onset), grain)
    last_day = min(pd.Timestamp(record["end"]), start + pd.Timedelta(days=MAX_ANALYSIS_DAYS - 1))
    if grain == "week":
        weeks = max(1, ((last_day - start).days + 1) // 7)
        end = start + pd.Timedelta(days=7 * weeks - 1)
    elif grain == "day":
        end = max(start, last_day)
    else:
        raise ValueError(f"a target at grain '{grain}' has no analysis window rule")
    return str(start.date()), str(end.date())

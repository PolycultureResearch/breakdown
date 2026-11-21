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
- **A root on a rate's numerator count.** The generator roots some rate
  anomalies at the count (a churn-rate spike at `churned_subscriptions`),
  because that is what its semantic layer can slice, and names the rate in
  `root_metric_rate`. A tree that holds the rate as a non-formula node is
  scored at the rate (`on_tree_rate`).
- **A root that still lands on a formula node** (a set without
  `root_metric_rate`, or a tree without the rate). An identity cannot be a
  root (innovation design §4.1). The truth is then the one parent of the
  formula, among those measured as moved, with the largest relative change.
- **Moved** (`Measured`). The set's cross-seed `verdict` when the set has one
  for the node's metric and slice (`established_up`/`_down` is moved), else
  this twin's `moved`. The basis is on every row.
- **Onset** (`onset_of`). The first day the twins differ at all
  (`first_diff`), when that is not before the declared start: the twins were
  then identical until the anomaly arrived at this metric, so it is the exact
  arrival date, ramps included. When it is earlier (most entity builders
  re-deal days long before the anomaly, so the twins differ from the start),
  the first day the difference clears the metric's noise floor (`onset`).
  Failing both, the declared start. A node bound to one slice member reads
  its own slice's stats from the twin's `effects_summary.parquet`. The
  source used is on every row.
- **Target** (`pick_target`). The deepest node downstream of the truth that
  moved: "the top metric the anomaly reaches".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

from breakdown.grains import floor_period

from .loader import TreePlan

MAX_ANALYSIS_DAYS = 28


ESTABLISHED = ("established_up", "established_down")


@dataclass
class Measured:
    """What the generator measured for one single world, in node terms.

    Three sources, each used for what it can answer:

    - the record's `measured_effects` and `declared_affected_metrics_confirmed`:
      this twin's stats for each metric's total;
    - the twin's `effects_summary.parquet` (`twin`): the same stats for every
      single-dimension slice, so a node bound to one slice member has its own;
    - the set's `effects_summary.parquet` (`verdicts`): one verdict per
      anomaly, metric and slice across the set's seeds. When it exists, it
      decides whether a node moved, so every seed of an anomaly gets the same
      target. When it has no row for a node, this twin's `moved` decides,
      and the basis says so.
    """

    record: dict[str, Any]
    verdicts: dict[tuple, str] = field(default_factory=dict)
    twin: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    # The set's cross-seed effect per key (`WorldSet.set_effects`): what an
    # attributed size is scored against.
    set_effects: dict[tuple, dict[str, Any]] = field(default_factory=dict)

    def key(self, node: str, plan: TreePlan) -> tuple | None:
        """`(metric, dimension, value)` a node's own series is measured as, or
        None when no single row measures it (several slice members summed, a
        running total)."""
        binding = plan.served.get(node)
        if binding is None:
            return (node, None, None)  # a derived node named after its metric
        if binding.cumulative:
            return None
        if binding.dimension is None:
            return (binding.metric, None, None)
        if len(binding.values) == 1:
            return (binding.metric, binding.dimension, binding.values[0])
        return None

    def effect(self, node: str, plan: TreePlan) -> dict[str, Any] | None:
        key = self.key(node, plan)
        if key is None:
            return None
        if key[1] is None:
            effect = (self.record.get("measured_effects") or {}).get(key[0])
            if effect is not None:
                return effect
        return self.twin.get(key)

    def moved(self, node: str, plan: TreePlan) -> tuple[bool, str]:
        """`(moved, basis)`: basis `set_verdict` or `twin_moved`."""
        key = self.key(node, plan)
        if key is not None and key in self.verdicts:
            return self.verdicts[key] in ESTABLISHED, "set_verdict"
        effect = self.effect(node, plan) or {}
        moved = bool(effect.get("moved"))
        if key is not None and key[1] is None and not moved:
            confirmed = self.record.get("declared_affected_metrics_confirmed") or {}
            moved = key[0] in (confirmed.get("moved_in_slice_only") or [])
        return moved, "twin_moved"

    def abs_rel_change(self, node: str, plan: TreePlan) -> float:
        effect = self.effect(node, plan) or {}
        try:
            value = abs(float(effect.get("rel_change") or 0.0))
        except (TypeError, ValueError):
            return 0.0
        return value if value == value else 0.0


def read_twin_summary(path) -> dict[tuple, dict[str, Any]]:
    """A twin's `effects_summary.parquet` as `(metric, dimension, value) -> stats`."""
    if path is None or not Path(path).exists():
        return {}
    frame = pd.read_parquet(path)
    out = {}
    for row in frame.to_dict("records"):
        key = (row["metric"], _none(row.get("dimension")), _none(row.get("value")))
        out[key] = {k: _none(v) for k, v in row.items()}
    return out


def _none(v):
    if v is None:
        return None
    try:
        return None if v != v else v
    except (TypeError, ValueError):
        return v


def as_measured(record: dict[str, Any], measured: Measured | None) -> Measured:
    return measured if measured is not None else Measured(record)


@dataclass
class RootMapping:
    status: str  # on_tree | on_tree_slice | on_tree_rate | via_formula_parents | off_tree
    root_node: str | None  # the node the generator's label lands on
    truth: list[str] = field(default_factory=list)  # the node scored as the root
    note: str = ""

    @property
    def on_tree(self) -> bool:
        return bool(self.truth)


def node_moved(node: str, plan: TreePlan, record: dict[str, Any], measured=None) -> bool:
    return as_measured(record, measured).moved(node, plan)[0]


def _bound(dag, plan: TreePlan, metric: str, root_slice: dict) -> tuple[str | None, str]:
    """The node a metric (at a slice, else its total) lands on, and how."""

    def bound(match) -> str | None:
        for node, binding in plan.served.items():
            if binding.metric == metric and not binding.cumulative and match(binding):
                return node
        return None

    node = bound(lambda b: b.dimension in root_slice and root_slice[b.dimension] in b.values)
    if node is not None:
        return node, "on_tree_slice"
    node = bound(lambda b: b.dimension is None)
    if node is not None:
        return node, "on_tree"
    if metric in dag and metric not in plan.served:
        return metric, "on_tree"  # a derived node carrying the metric's name
    return None, ""


def map_root(
    dag: nx.DiGraph, plan: TreePlan, record: dict[str, Any], measured: Measured | None = None
) -> RootMapping:
    measured = as_measured(record, measured)
    metric = record.get("root_metric")
    root_slice = record.get("root_slice") or {}
    if not metric:
        return RootMapping("off_tree", None, note="the record has no root_metric")

    # The generator roots a rate's anomaly at the rate's numerator count when
    # its semantic layer cannot slice the rate, and names the rate. A tree
    # that holds the rate is scored at it: the rate is what was changed.
    rate = record.get("root_metric_rate")
    if rate:
        node, _ = _bound(dag, plan, rate, root_slice)
        if node is not None and not _is_formula(dag, node):
            return RootMapping(
                "on_tree_rate", node, [node], note=f"root '{metric}' is the numerator of '{rate}'"
            )

    node, status = _bound(dag, plan, metric, root_slice)
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
            moved = [p for p in dag.predecessors(n) if p not in seen and measured.moved(p, plan)[0]]
            if not moved:
                # Nothing upstream is known to have moved: keep the identity
                # itself as the label and say so, instead of guessing a parent.
                return RootMapping(
                    status,
                    node,
                    [node],
                    note=f"'{node}' is a formula and no parent of '{n}' is measured as moved",
                )
            nxt.append(max(moved, key=lambda p: measured.abs_rel_change(p, plan)))
        truth = list(dict.fromkeys(nxt))
        status = "via_formula_parents"
    return RootMapping(status, node, truth)


def _is_formula(dag: nx.DiGraph, node: str) -> bool:
    return bool(dag.nodes[node]["definition"].formula) and dag.in_degree(node) > 0


def onset_of(
    node: str,
    plan: TreePlan,
    record: dict[str, Any],
    *,
    is_root: bool,
    measured: Measured | None = None,
) -> tuple[pd.Timestamp, str]:
    """`(date, source)` for when `node`'s own series first moves."""
    declared = pd.Timestamp(record["start"])
    measured = as_measured(record, measured)
    candidates = [("node", measured.effect(node, plan))]
    if is_root:
        candidates.append(("root_slice", record.get("root_effect")))
    for label, effect in candidates:
        if effect and effect.get("first_diff"):
            first = pd.Timestamp(effect["first_diff"])
            if first >= declared:
                return first, f"first_diff:{label}"
    for label, effect in candidates:
        if effect and effect.get("onset"):
            return pd.Timestamp(effect["onset"]), f"onset:{label}"
    return declared, "declared_start"


def pick_target(
    dag: nx.DiGraph,
    plan: TreePlan,
    record: dict[str, Any],
    truth: list[str],
    measured: Measured | None = None,
) -> tuple[str | None, str]:
    """`(target, basis)`: the deepest moved node downstream of the truth.
    The basis is `set_verdict` or `twin_moved` (which source said the target
    moved), or `fallback_deepest_descendant` when none is known to have."""
    measured = as_measured(record, measured)
    downstream = set().union(*(nx.descendants(dag, t) for t in truth)) - set(truth)
    if not downstream:
        return None, "the root has no descendant in the tree"
    depth = {}
    for n in nx.topological_sort(dag):
        depth[n] = 1 + max((depth[p] for p in dag.predecessors(n)), default=-1)
    order = {n: i for i, n in enumerate(nx.topological_sort(dag))}

    def deepest(nodes):
        return max(nodes, key=lambda n: (depth[n], order[n]))

    verdict = {n: measured.moved(n, plan) for n in downstream}
    moved = [n for n in downstream if verdict[n][0]]
    if moved:
        target = deepest(moved)
        return target, verdict[target][1]
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

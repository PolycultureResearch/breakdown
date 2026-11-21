"""Attribution (evaluation spec §5.4, Q5): the engine's attributed sizes
against the measured twin effect.

**What is compared.** For a single world, the engine's root-cause analysis
(`run_rca`) over the anomaly's declared window, as the whole periods of the
target that cover it (`attribution_window`): the window the set's
cross-seed effects were measured over, and the same in every seed. On every edge `p -> c` that
lies on a path from the truth root to the target, the engine reports `p`'s
contribution to `c`'s change, with a 95% credible interval. Its measured
counterpart is how much of `c`'s change in that window the anomaly caused
through `p`, from the single world and its null twin, each loaded through the
same loader and aggregated by the engine's own window rule
(`node_window_value`, over the node's effective analysis window):

- `c` a formula: the exact Shapley share of `p` in `formula(world parents) -
  formula(null parents)`, per period, then the mean over the window (the
  engine's flow rule). Formula edges are reported apart from learned ones;
- `c` learned: `V_world(c) - V_null(c)`, all of `c`'s twin difference, when
  `p` is the only parent of `c` the set says the anomaly moved. When another
  parent moved too the twins do not say how to split it, and the edge is
  refused by name (`several_parents_moved`);
- the root itself: its twin difference against the part of its change the
  engine leaves unexplained by its parents, trend and seasonality
  (`root_unexplained`), or, for a root with no parents, against its change
  itself (`root_gap`: the engine attributes nothing at a terminal cause and
  reports its movement). Neither has an interval, so neither has coverage.

The engine answers window-over-window (analysis against the adjacent
reference window); the truth is a counterfactual (world against its null
twin). The gap between the two questions, the reference window's own drift,
is part of the error, as it is part of what a reader of the engine's answer
gets.

**Which truth.** Spec of the generator, §9.7: a size is scored only where the
set's `verdict` for the child metric is established and the set's cross-seed
mean of the child's relative effect is precise: 95% half-width within
`MAX_REL_PRECISION` of the effect at the set's own seed count, `t · sd / √n /
|mean|` from `rel_change_sd` and `n_seeds` in its `effects_summary.parquet`,
with `t` the two-sided 95% point of Student's t on n − 1 degrees of freedom.
The generator's §9.2 rule uses 1.96; at its ten seeds t is 2.26, and at a
smoke set's three it is 4.30, where 1.96 would trust a spread taken from
three numbers.

Truth is then the set's cross-seed mean relative effect of the child, times
the item's share of it (1 for a learned edge and a root, the mean Shapley
fraction over the run's worlds for a formula parent), times this world's null
value of the child. Where this world's twins were aligned until the anomaly
(`pre_onset_rms_diff == 0` in the twin's summary for that metric), one
twin's difference is that world's own effect and is used instead. The set's
relative change is of the window mean of the daily series; the engine's
window rule takes a rate as Σnumerator / Σdenominator, so for a rate the two
can differ slightly, and that difference is in the error.

Everything here is computed at scoring time from rows the job writes, so a
world's rows never depend on which other seeds the run holds.
"""

from __future__ import annotations

import math
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.grains import floor_period

from .truth import ESTABLISHED, Measured

MAX_REL_PRECISION = 0.20


def t95(n: int) -> float:
    """Two-sided 95% point of Student's t with n − 1 degrees of freedom."""
    from scipy.stats import t

    return float(t.ppf(0.975, n - 1))


__all__ = [
    "MAX_REL_PRECISION",
    "attribution_window",
    "attribution_items",
    "eligibility",
    "score_items",
]


def attribution_window(record: dict[str, Any], grain: str) -> tuple[str, str]:
    """The anomaly's declared window, as the whole periods of the target that
    cover it: the window the set's cross-seed effect was measured over, and
    the same in every seed."""
    start = floor_period(pd.Timestamp(record["start"]), grain)
    end = pd.Timestamp(record["end"])
    if grain == "week":
        end = floor_period(end, grain) + pd.Timedelta(days=6)
    elif grain != "day":
        raise ValueError(f"a target at grain '{grain}' has no attribution window rule")
    return str(start.date()), str(end.date())


def path_edges(dag: nx.DiGraph, truth: list[str], target: str) -> list[tuple[str, str]]:
    """Every edge on a path from a truth node to the target."""
    on_path = set()
    for t in truth:
        if t == target or not nx.has_path(dag, t, target):
            continue
        on_path |= (nx.descendants(dag, t) & nx.ancestors(dag, target)) | {t, target}
    order = {n: i for i, n in enumerate(nx.topological_sort(dag))}
    return sorted(
        ((p, c) for p, c in dag.edges if p in on_path and c in on_path),
        key=lambda e: (order[e[1]], order[e[0]]),
    )


def _window(rca_node: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    eff = (rca_node or {}).get("effective_windows") or {}
    an = eff.get("analysis")
    if not an:
        return None
    return pd.Timestamp(an["start"]), pd.Timestamp(an["end"])


def _value(lw, node: str, window) -> float | None:
    from breakdown.engine.windows import node_window_value

    try:
        v = node_window_value(lw.data, node, window[0], window[1])
    except Exception:  # no period in the window at all
        return None
    return v if np.isfinite(v) else None


def _formula_share(lw, null_lw, dag, parent: str, child: str, window) -> float | None:
    """Mean over the window's periods of `parent`'s exact Shapley share in the
    child's twin difference."""
    from breakdown.engine.model import compute_shapley

    defn = dag.nodes[child]["definition"]
    parents = list(dag.predecessors(child))
    grain = lw.data.grain_of[child]
    lags = defn.lags

    def series(data, p):
        frame = data.series(p, grain).set_index("date")[p]
        return frame.shift(lags.get(p, 0))

    world = {p: series(lw.data, p) for p in parents}
    null = {p: series(null_lw.data, p) for p in parents}
    idx = pd.DatetimeIndex(world[parents[0]].index)
    idx = idx[(idx >= window[0]) & (idx <= window[1])]
    if not len(idx):
        return None
    w = {p: world[p].reindex(idx).to_numpy(float) for p in parents}
    n = {p: null[p].reindex(idx).to_numpy(float) for p in parents}
    if not all(np.isfinite(a).all() for a in [*w.values(), *n.values()]):
        return None
    shares = compute_shapley(defn.formula, parents, n, w, node=child)
    value = float(np.mean(shares[parent]))
    return value if np.isfinite(value) else None


def _verdict(measured: Measured, plan, node: str) -> str | None:
    key = measured.key(node, plan)
    return None if key is None else measured.verdicts.get(key)


def _aligned(measured: Measured, plan, node: str, stats: dict) -> tuple[bool | None, str]:
    """Whether this child's twins were identical before the start, and on
    what basis: the set's `aligned_before_start` (every seed; generator
    §10.5), or, where the set leaves it NULL, this twin's own
    `pre_onset_rms_diff` over its `pre_onset_days`, named as the fallback."""
    flag = stats.get("aligned_before_start")
    if flag is not None:
        return bool(flag), "set_flag"
    effect = measured.effect(node, plan) or {}
    rms, days = effect.get("pre_onset_rms_diff"), effect.get("pre_onset_days")
    if rms is None or (days is not None and not days):
        return None, "unknown: set flag NULL and no twin days before the start"
    try:
        return float(rms) == 0.0, "twin_pre_onset (set flag NULL)"
    except (TypeError, ValueError):
        return None, "unknown: set flag NULL"


def _truth_basis(lw, child: str, stats: dict) -> tuple[str, str | None]:
    """`(basis, refusal)`: which of the set's effects matches the engine's
    window value for this child, or why none does.

    The engine averages a stock's per-period levels over a window (the mean
    of daily values: `rel_change`) and takes a rate as Σnum/Σden and a flow
    as its sum (the metric's own rule: `rel_change_aggregated`, generator
    §10.6, where the set has it). A week node is served as breakdown's
    providers return it, MetricFlow's week rule; where that differs from the
    business rule the set measured by (`matches_metricflow_week` false), no
    set effect matches the engine's number, and the size is refused."""
    defn = lw.dag.nodes[child]["definition"]
    binding = lw.plan.served.get(child)
    spec = lw.specs.get(binding.metric if binding else child)
    grain = lw.data.grain_of.get(child)
    if grain == "week" and spec is not None and spec.matches_metricflow_week is False:
        return "none", (
            f"provider_rule_differs: '{spec.name}' is served at week grain as MetricFlow's "
            f"`{spec.metricflow_week}`, and the set measures it by its "
            f"'{spec.time_aggregation}' rule"
        )
    if defn.kind == "stock" or stats.get("rel_agg_mean") is None:
        return "rel_change", None
    return "rel_change_aggregated", None


def attribution_items(
    lw, null_lw, rca: dict[str, Any], truth: list[str], target: str, measured: Measured
) -> list[dict[str, Any]]:
    """One row per scored item of one world: the engine's number, its
    interval or why it was withheld, and the twin values the truth is built
    from. No eligibility here; that needs every seed's rows."""
    dag, plan = lw.dag, lw.plan
    nodes = rca.get("nodes") or {}
    items: list[dict[str, Any]] = []

    def base(kind, parent, child):
        node = nodes.get(child) or {}
        window = _window(node)
        v_world = v_null = None
        if window is not None:
            v_world, v_null = _value(lw, child, window), _value(null_lw, child, window)
        key = measured.key(child, plan)
        stats = (measured.set_effects.get(key) if key else None) or {}
        basis, refusal = _truth_basis(lw, child, stats)
        agg = basis == "rel_change_aggregated"
        aligned, aligned_basis = _aligned(measured, plan, child, stats)
        return {
            "truth_basis": basis,
            **({"refused": refusal} if refusal else {}),
            "set_rel_mean": _finite(stats.get("rel_agg_mean" if agg else "rel_mean")),
            "set_rel_sd": _finite(stats.get("rel_agg_sd" if agg else "rel_sd")),
            "set_n_seeds": stats.get("n_seeds"),
            "set_null_mean": _finite(stats.get("null_mean")),
            "item": f"{parent}->{child}" if parent else f"root:{child}",
            "kind": kind,
            "parent": parent,
            "child": child,
            "window_start": None if window is None else str(window[0].date()),
            "window_end": None if window is None else str(window[1].date()),
            "verdict": _verdict(measured, plan, child),
            "verdict_column": measured.verdict_column,
            "aligned": aligned,
            "aligned_basis": aligned_basis,
            "world_value": v_world,
            "null_value": v_null,
            "node_status": node.get("status"),
            "ci_status": node.get("ci_status"),
        }

    for t in truth:
        node = nodes.get(t) or {}
        # A root the engine fitted (it has parents): the part of its change
        # its parents, trend and seasonality do not explain. A root with no
        # parents is a terminal cause, and the engine reports its change
        # itself; that is not a withheld number, so it is not counted as one.
        attributed = dag.in_degree(t) > 0
        field = "unexplained" if attributed else "gap"
        row = base("root_unexplained" if attributed else "root_gap", None, t)
        row.update(
            measured_effect=_diff(row),
            estimate=_finite(node.get(field)),
            ci_95=None,
            withheld=_withheld(node, node.get(field), field),
        )
        items.append(row)

    for p, c in path_edges(dag, truth, target):
        node = nodes.get(c) or {}
        is_formula = bool(dag.nodes[c]["definition"].formula)
        row = base("formula_edge" if is_formula else "learned_edge", p, c)
        entry = next((e for e in node.get("contributions") or [] if e.get("parent") == p), None)
        row.update(
            estimate=_finite(None if entry is None else entry.get("estimate")),
            ci_95=_interval(None if entry is None else entry.get("ci_95")),
            withheld=_withheld(node, None if entry is None else entry.get("estimate"), p),
        )
        window = _window(node)
        if is_formula:
            share = None if window is None else _formula_share(lw, null_lw, dag, p, c, window)
            row["measured_effect"] = share
        else:
            others = [
                q
                for q in dag.predecessors(c)
                if q != p and (_verdict(measured, plan, q) in ESTABLISHED)
            ]
            if others:
                row["measured_effect"] = None
                row.setdefault("refused", f"several_parents_moved: {sorted(others)}")
            else:
                row["measured_effect"] = _diff(row)
        items.append(row)
    return items


def _diff(row) -> float | None:
    if row["world_value"] is None or row["null_value"] is None:
        return None
    return row["world_value"] - row["null_value"]


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _interval(ci) -> list[float] | None:
    if not ci or len(ci) != 2:
        return None
    lo, hi = _finite(ci[0]), _finite(ci[1])
    return None if lo is None or hi is None else [lo, hi]


def _withheld(node: dict[str, Any], estimate, what: str) -> str | None:
    """Why the engine gave no number, or None when it gave one. Never a zero."""
    if not node:
        return "node_not_in_rca"
    if node.get("status") != "ok":
        return f"node_status:{node.get('status')}"
    if _finite(estimate) is None:
        return f"withheld:{node.get('ci_status') or 'no_estimate'} ({what})"
    return None


# ---------------------------------------------------------------------------
# Scoring: needs every seed's rows of an item
# ---------------------------------------------------------------------------
def eligibility(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether one item's size can be scored: the set's verdict for its child
    established, and the set's cross-seed mean relative effect for the child
    precise to `MAX_REL_PRECISION` at the set's own seed count. Both come
    from the set's `effects_summary.parquet` (declared window, every seed the
    set holds); the rows only carry them. The item's share of its child's
    effect (1 for a learned edge and a root; a formula parent's Shapley
    fraction) is the mean over the run's worlds."""
    first = rows[0]
    verdicts = {r.get("verdict") for r in rows}
    fractions = [
        r["measured_effect"] / (r["world_value"] - r["null_value"])
        for r in rows
        if r.get("measured_effect") is not None
        and r.get("world_value") is not None
        and r.get("null_value") is not None
        and r["world_value"] != r["null_value"]
    ]
    out: dict[str, Any] = {
        "verdict": verdicts.pop() if len(verdicts) == 1 else sorted(map(str, verdicts)),
        "n_seeds": first.get("set_n_seeds"),
        "rel_mean": first.get("set_rel_mean"),
        "rel_sd": first.get("set_rel_sd"),
        "fraction": float(np.mean(fractions)) if fractions else None,
        "truth_basis": first.get("truth_basis"),
        "precision": None,
        "aligned_worlds": sum(1 for r in rows if r.get("aligned")),
    }
    refused = {r.get("refused") for r in rows if r.get("refused")}
    if refused:
        out["eligible"], out["why"] = False, sorted(refused)[0]
        return out
    if out["verdict"] not in ESTABLISHED:
        out["eligible"] = False
        out["why"] = f"verdict {out['verdict']}" if out["verdict"] is not None else "no set verdict"
        return out
    null_levels = [r.get("set_null_mean") for r in rows] + [r.get("null_value") for r in rows]
    if any(v is not None and v <= 0 for v in null_levels):
        # trade_inventory_build can be negative and discounted_orders is zero
        # outside a promotion: a relative change of those is not a size. The
        # generator says to read them in units (`mean_diff_mean`); scoring in
        # units is not built, so the size is refused by name.
        out["eligible"] = False
        out["why"] = "null level is zero or negative: a size in units is not built"
        return out
    n, mean, sd = out["n_seeds"], out["rel_mean"], out["rel_sd"]
    if mean is None or sd is None or not n or n < 2 or mean == 0:
        out["eligible"] = False
        out["why"] = "the set's summary gives no cross-seed mean and spread for this metric"
        return out
    if out["fraction"] is None:
        out["eligible"], out["why"] = False, "no world measured the item's share"
        return out
    out["precision"] = t95(int(n)) * sd / math.sqrt(n) / abs(mean)
    if out["precision"] > MAX_REL_PRECISION:
        out["eligible"] = False
        out["why"] = (
            f"precision ±{out['precision']:.0%} at the set's {int(n)} seeds "
            f"(needs ±{MAX_REL_PRECISION:.0%})"
        )
        return out
    out["eligible"], out["why"] = True, "established and precise"
    return out


def target_changes(groups: dict[tuple, list[dict]], verdicts: dict[tuple, dict]) -> dict:
    """`world -> (the target's measured counterfactual change, basis)`: the
    denominator of the error share (spec §5.4). Read off the item whose child
    is the target: the set's cross-seed mean times this world's null value
    where the set has one on a basis matching the engine's, else this
    world's own twin difference, named."""
    out: dict[str, tuple[float | None, str]] = {}
    for key, rows in groups.items():
        v = verdicts[key]
        for r in rows:
            if r.get("child") != r.get("target") or r["world"] in out:
                continue
            null = r.get("null_value")
            if v.get("rel_mean") is not None and null and not r.get("refused"):
                out[r["world"]] = (v["rel_mean"] * null, "cross_seed_mean")
            elif r.get("world_value") is not None and null is not None:
                out[r["world"]] = (r["world_value"] - null, "this_twin")
            else:
                out[r["world"]] = (None, "unmeasured")
    return out


def score_items(
    rows: list[dict[str, Any]], verdict: dict[str, Any], target_change: dict | None = None
) -> list[dict[str, Any]]:
    """Per world: truth, ratio, log error, coverage, withheld, and the error
    as a share of the target's measured change. For an ineligible item whose
    verdict is established, the direction only."""
    target_change = target_change or {}
    out = []
    for r in rows:
        s = {"world": r["world"], "seed": r["seed"], "withheld": r.get("withheld")}
        est = r.get("estimate")
        if verdict["eligible"]:
            if r.get("aligned") and r.get("measured_effect") is not None:
                truth, basis = r["measured_effect"], "this_twin"
            elif r.get("null_value") is not None:
                truth = verdict["fraction"] * verdict["rel_mean"] * r["null_value"]
                basis = "cross_seed_mean"
            else:
                truth, basis = None, None
            s.update(truth=truth, basis=basis)
            if est is not None and truth not in (None, 0):
                ratio = est / truth
                s["ratio"] = ratio
                s["wrong_sign"] = ratio <= 0
                s["log_error"] = math.log(ratio) if ratio > 0 else None
                ci = r.get("ci_95")
                s["covered"] = None if ci is None else bool(ci[0] <= truth <= ci[1])
                change, change_basis = target_change.get(r["world"], (None, None))
                if change:
                    s["error_share"] = (est - truth) / abs(change)
                    s["target_change_basis"] = change_basis
        elif verdict["verdict"] in ESTABLISHED and est is not None:
            # The item's own measured direction (a formula share can point
            # against its child: the stock's share in a churn spike), else
            # the child's verdict.
            if verdict.get("rel_mean") and verdict.get("fraction"):
                expected = np.sign(verdict["rel_mean"] * verdict["fraction"])
            else:
                expected = 1 if verdict["verdict"] == "established_up" else -1
            s["direction_agrees"] = bool(np.sign(est) == expected)
        out.append(s)
    return out

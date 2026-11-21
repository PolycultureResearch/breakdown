"""Attribution (evaluation spec §5.4, Q5): the engine's attributed sizes
against the measured twin effect.

**What is compared.** For a single world, the engine's root-cause analysis
(`run_rca`) over the anomaly's own window: the declared start's period, for
as long as the anomaly lasts, at most `MAX_ANALYSIS_DAYS`, in whole periods of
the target (`attribution_window`). The same window in every seed, so the
measured effect can be averaged across seeds. On every edge `p -> c` that
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
set's `verdict` for the child metric is established and the cross-seed mean
of the measured effect is precise, 95% half-width within `MAX_REL_PRECISION`
of the effect at the seeds the run scores: `t · sd / √n / |mean|`, with `t`
the two-sided 95% point of Student's t on n − 1 degrees of freedom. The
generator's §9.2 rule uses 1.96; at its ten seeds t is 2.26, and at a smoke
set's three it is 4.30, where 1.96 would trust a spread taken from three
numbers. That spread is computed here from the runner's own per-world rows,
so the precision is that of the window and the seeds actually scored. Truth is then the cross-seed mean relative effect times this
world's null value. Where this world's twins were aligned until the anomaly
(`pre_onset_rms_diff == 0` in the twin's summary for that metric), one twin's
difference is that world's own effect and is used instead.

Everything here is computed at scoring time from rows the job writes, so a
world's rows never depend on which other seeds the run holds.
"""

from __future__ import annotations

import math
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from .truth import ESTABLISHED, MAX_ANALYSIS_DAYS, Measured, analysis_window

MAX_REL_PRECISION = 0.20


def t95(n: int) -> float:
    """Two-sided 95% point of Student's t with n − 1 degrees of freedom."""
    from scipy.stats import t

    return float(t.ppf(0.975, n - 1))


__all__ = [
    "MAX_ANALYSIS_DAYS",
    "MAX_REL_PRECISION",
    "attribution_window",
    "attribution_items",
    "eligibility",
    "score_items",
]


def attribution_window(record: dict[str, Any], grain: str) -> tuple[str, str]:
    """The anomaly's own window at the target's grain, the same in every seed."""
    return analysis_window(pd.Timestamp(record["start"]), record, grain)


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


def _aligned(measured: Measured, plan, node: str) -> bool | None:
    effect = measured.effect(node, plan) or {}
    rms = effect.get("pre_onset_rms_diff")
    if rms is None:
        return None
    try:
        return float(rms) == 0.0
    except (TypeError, ValueError):
        return None


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
        return {
            "item": f"{parent}->{child}" if parent else f"root:{child}",
            "kind": kind,
            "parent": parent,
            "child": child,
            "window_start": None if window is None else str(window[0].date()),
            "window_end": None if window is None else str(window[1].date()),
            "verdict": _verdict(measured, plan, child),
            "aligned": _aligned(measured, plan, child),
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
                row["refused"] = f"several_parents_moved: {sorted(others)}"
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
    """Whether one item's size can be scored, from its rows across the run's
    seeds: the set's verdict must be established, and the cross-seed mean of
    the relative effect precise to `MAX_REL_PRECISION`."""
    rel = [
        r["measured_effect"] / r["null_value"]
        for r in rows
        if r.get("measured_effect") is not None and r.get("null_value") not in (None, 0)
    ]
    verdicts = {r.get("verdict") for r in rows}
    out: dict[str, Any] = {
        "n_seeds": len(rel),
        "verdict": verdicts.pop() if len(verdicts) == 1 else sorted(map(str, verdicts)),
        "rel_mean": float(np.mean(rel)) if rel else None,
        "rel_sd": float(np.std(rel, ddof=1)) if len(rel) > 1 else None,
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
    if len(rel) < 2:
        out["eligible"], out["why"] = False, f"{len(rel)} seed(s) with a measured effect"
        return out
    if out["rel_mean"] == 0:
        out["eligible"], out["why"] = False, "measured mean is zero"
        return out
    out["precision"] = t95(len(rel)) * out["rel_sd"] / math.sqrt(len(rel)) / abs(out["rel_mean"])
    if out["precision"] > MAX_REL_PRECISION:
        out["eligible"] = False
        out["why"] = (
            f"precision ±{out['precision']:.0%} at {len(rel)} seeds "
            f"(needs ±{MAX_REL_PRECISION:.0%})"
        )
        return out
    out["eligible"], out["why"] = True, "established and precise"
    return out


def score_items(rows: list[dict[str, Any]], verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Per world: truth, ratio, log error, coverage, withheld. For an
    ineligible item whose verdict is established, the direction only."""
    out = []
    for r in rows:
        s = {"world": r["world"], "seed": r["seed"], "withheld": r.get("withheld")}
        est = r.get("estimate")
        if verdict["eligible"]:
            if r.get("aligned") and r.get("measured_effect") is not None:
                truth, basis = r["measured_effect"], "this_twin"
            elif r.get("null_value") is not None:
                truth, basis = verdict["rel_mean"] * r["null_value"], "cross_seed_mean"
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
        elif verdict["verdict"] in ESTABLISHED and est is not None:
            # The item's own measured direction (a formula share can point
            # against its child: the stock's share in a churn spike), else
            # the child's verdict.
            if verdict.get("rel_mean"):
                expected = np.sign(verdict["rel_mean"])
            else:
                expected = 1 if verdict["verdict"] == "established_up" else -1
            s["direction_agrees"] = bool(np.sign(est) == expected)
        out.append(s)
    return out

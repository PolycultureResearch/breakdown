"""Root cause analysis over the ancestor DAG of a target metric.

`run_rca` walks the ancestors of an anomalous metric and attributes the change
between a reference window and an analysis window to upstream metrics. Two
attribution methods are used depending on the node type:

- **Formula nodes** (arithmetic identities): exact Shapley attribution over the
  parent window means. Contributions are point estimates with no uncertainty.
- **Probabilistic nodes** (learned BSTS regressions): the posterior over the
  raw-scale coefficient (`beta_raw`) times the parent's window-over-window
  change, giving an uncertainty-aware contribution (mean, 95% CI, and the
  posterior mass on the dominant side of zero).

Unfitted probabilistic nodes in scope are fit on demand with ADVI; the caller
passes its trace cache and new fits are added to it in place.

The `ranked_causes` list is a documented **heuristic**: it propagates an
influence score from the target up the ancestor tree, weighting each hop by the
parent's share of its child's gap (clamped to [0, 1]). It is meant as a triage
ordering, not a rigorous multi-hop uncertainty propagation.
"""

from typing import Any, Dict

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine.model import compute_shapley, fit_metric
from breakdown.formula import eval_formula


def window_mean(data: pd.DataFrame, col: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Mean of `col` over rows whose date is within [start, end] (inclusive).

    `data["date"]` must already be datetime. Raises if the window is empty.
    """
    mask = (data["date"] >= start) & (data["date"] <= end)
    if not mask.any():
        raise ValueError(
            f"No data for '{col}' in window [{start.date()}, {end.date()}]"
        )
    return float(data.loc[mask, col].mean())


def _eval_scalar(formula: str, values: Dict[str, float]) -> float:
    return float(eval_formula(formula, {k: np.array([v]) for k, v in values.items()})[0])


def shapley_attribution(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
) -> Dict[str, Any]:
    """Exact Shapley decomposition of a formula metric's window-over-window gap.

    `baseline`/`actual`/`gap` are computed from the formula applied to parent
    window means (not from the target's own column), so `attribution` sums to
    `gap` exactly. Returns the `GET /shapley` response shape.
    """
    defn = dag.nodes[target]["definition"]
    if not defn.formula:
        raise ValueError(
            f"Metric '{target}' has no formula — Shapley attribution requires a formula definition."
        )
    parents = list(dag.predecessors(target))
    missing = [p for p in parents if p not in data.columns]
    if missing:
        raise ValueError(f"Columns missing from data: {missing}")

    frame = data.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    ref_start, ref_end = pd.to_datetime(reference_start), pd.to_datetime(reference_end)
    an_start, an_end = pd.to_datetime(analysis_start), pd.to_datetime(analysis_end)

    baselines = {p: window_mean(frame, p, ref_start, ref_end) for p in parents}
    actuals = {p: window_mean(frame, p, an_start, an_end) for p in parents}

    baseline = _eval_scalar(defn.formula, baselines)
    actual = _eval_scalar(defn.formula, actuals)

    return {
        "target": target,
        "formula": defn.formula,
        "baseline": baseline,
        "actual": actual,
        "gap": actual - baseline,
        "attribution": compute_shapley(defn.formula, parents, baselines, actuals),
    }


def run_rca(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    traces: Dict[str, Any],
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    advi_draws: int = 500,
) -> Dict[str, Any]:
    """Attribute `target`'s window-over-window change to its ancestors.

    `traces` is the caller's cache (metric name -> InferenceData). Probabilistic
    nodes in scope without a cached trace are fit with ADVI and added to it.
    """
    if target not in dag:
        raise ValueError(f"Metric '{target}' not found in the metric tree.")

    frame = data.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    ref_start, ref_end = pd.to_datetime(reference_start), pd.to_datetime(reference_end)
    an_start, an_end = pd.to_datetime(analysis_start), pd.to_datetime(analysis_end)

    nodes_in_scope = nx.ancestors(dag, target) | {target}

    # Fit any probabilistic (non-formula, non-root) node in scope that lacks a
    # cached trace. Formula nodes and roots need no fit.
    for node in nodes_in_scope:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and node not in traces:
            traces[node] = fit_metric(dag, data, node, draws=advi_draws, inference_method="advi")

    nodes_out: Dict[str, Any] = {}
    for node in nodes_in_scope:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))

        baseline = window_mean(frame, node, ref_start, ref_end)
        actual = window_mean(frame, node, an_start, an_end)
        gap = actual - baseline
        relative_change = gap / baseline if abs(baseline) >= 1e-12 else None

        contributions = []
        if not parents:
            attribution_method = None
            unexplained = None
        elif defn.formula:
            attribution_method = "shapley"
            sh = shapley_attribution(
                dag, data, node, reference_start, reference_end, analysis_start, analysis_end
            )
            for p in parents:
                estimate = sh["attribution"][p]
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": None,
                    "prob_same_direction": None,
                })
            unexplained = gap - sh["gap"]
        else:
            attribution_method = "posterior"
            arr = traces[node].posterior["beta_raw"].values.reshape(-1, len(parents))
            estimate_sum = 0.0
            for i, p in enumerate(parents):
                lag = defn.lags.get(p, 0)
                if lag > 0:
                    # The parent values that influenced the analysis window are
                    # those `lag` days earlier, so shift both windows back.
                    shift = pd.Timedelta(days=lag)
                    p_baseline = window_mean(frame, p, ref_start - shift, ref_end - shift)
                    p_actual = window_mean(frame, p, an_start - shift, an_end - shift)
                else:
                    p_baseline = window_mean(frame, p, ref_start, ref_end)
                    p_actual = window_mean(frame, p, an_start, an_end)
                parent_gap = p_actual - p_baseline
                samples = arr[:, i] * parent_gap
                estimate = float(samples.mean())
                estimate_sum += estimate
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": [
                        float(np.percentile(samples, 2.5)),
                        float(np.percentile(samples, 97.5)),
                    ],
                    "prob_same_direction": float(
                        max((samples > 0).mean(), (samples < 0).mean())
                    ),
                })
            unexplained = gap - estimate_sum

        nodes_out[node] = {
            "baseline": baseline,
            "actual": actual,
            "gap": gap,
            "relative_change": relative_change,
            "attribution_method": attribution_method,
            "unexplained": unexplained,
            "contributions": contributions,
        }

    return {
        "target": target,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "nodes": nodes_out,
        "ranked_causes": _rank_causes(dag, target, nodes_in_scope, nodes_out),
    }


def _rank_causes(dag, target, nodes_in_scope, nodes_out):
    """Heuristic influence score: propagate 1.0 from the target up the ancestor
    tree, weighting each hop by the parent's clamped share of its child's gap.

    Processing in reverse topological order (target first) guarantees a child's
    score is complete before it is propagated to its parents.
    """
    score = {n: 0.0 for n in nodes_in_scope}
    score[target] = 1.0
    via: Dict[str, str] = {}
    best_term: Dict[str, float] = {n: float("-inf") for n in nodes_in_scope}

    topo_scope = [n for n in nx.topological_sort(dag) if n in nodes_in_scope]
    for child in reversed(topo_scope):
        for contrib in nodes_out[child]["contributions"]:
            parent = contrib["parent"]
            if parent not in score:
                continue
            share = contrib["share_of_gap"]
            weight = 0.0 if share is None else min(abs(share), 1.0)
            term = score[child] * weight
            score[parent] += term
            if term > best_term[parent]:
                best_term[parent] = term
                via[parent] = child

    ranked = [
        {"metric": n, "score": score[n], "via": via.get(n)}
        for n in nodes_in_scope
        if n != target
    ]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked

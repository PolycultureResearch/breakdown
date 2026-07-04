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

Unfitted probabilistic nodes in scope are fit on demand with ADVI (traces are
cached on the builder and reused on subsequent calls).

The `ranked_causes` list is a documented **heuristic**: it propagates an
influence score from the target up the ancestor tree, weighting each hop by the
parent's share of its child's gap (clamped to [0, 1]). It is meant as a triage
ordering, not a rigorous multi-hop uncertainty propagation.
"""

from typing import Any, Dict

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.engine.model import ModelBuilder, compute_shapley
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


def run_rca(
    builder: ModelBuilder,
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    advi_draws: int = 500,
) -> Dict[str, Any]:
    dag = builder.dag
    if target not in dag:
        raise ValueError(f"Metric '{target}' not found in the metric tree.")

    data = builder.data.copy()
    data["date"] = pd.to_datetime(data["date"])

    ref_start = pd.to_datetime(reference_start)
    ref_end = pd.to_datetime(reference_end)
    an_start = pd.to_datetime(analysis_start)
    an_end = pd.to_datetime(analysis_end)

    nodes_in_scope = nx.ancestors(dag, target) | {target}

    # Fit any probabilistic (non-formula, non-root) node in scope that lacks a
    # cached trace. Formula nodes and roots need no fit.
    for node in nodes_in_scope:
        node_data = dag.nodes[node]
        parents = list(dag.predecessors(node))
        if parents and not node_data.get("formula") and node not in builder.traces:
            builder.build_and_sample(
                node, draws=advi_draws, tune=50, inference_method="advi"
            )

    nodes_out: Dict[str, Any] = {}
    for node in nodes_in_scope:
        node_data = dag.nodes[node]
        parents = list(dag.predecessors(node))
        formula = node_data.get("formula")

        baseline = window_mean(data, node, ref_start, ref_end)
        actual = window_mean(data, node, an_start, an_end)
        gap = actual - baseline
        relative_change = gap / baseline if abs(baseline) >= 1e-12 else None

        contributions = []
        if not parents:
            attribution_method = None
            unexplained = None
        elif formula:
            attribution_method = "shapley"
            parent_baselines = {p: window_mean(data, p, ref_start, ref_end) for p in parents}
            parent_actuals = {p: window_mean(data, p, an_start, an_end) for p in parents}
            shapley_values = compute_shapley(formula, parents, parent_baselines, parent_actuals)
            for p in parents:
                estimate = shapley_values[p]
                contributions.append({
                    "parent": p,
                    "estimate": estimate,
                    "share_of_gap": (estimate / gap) if abs(gap) >= 1e-12 else None,
                    "ci_95": None,
                    "prob_same_direction": None,
                })
            formula_actual = float(
                eval_formula(formula, {p: np.array([parent_actuals[p]]) for p in parents})[0]
            )
            formula_baseline = float(
                eval_formula(formula, {p: np.array([parent_baselines[p]]) for p in parents})[0]
            )
            unexplained = gap - (formula_actual - formula_baseline)
        else:
            attribution_method = "posterior"
            lags = node_data.get("lags") or {}
            arr = builder.traces[node].posterior["beta_raw"].values.reshape(-1, len(parents))
            estimate_sum = 0.0
            for i, p in enumerate(parents):
                lag = lags.get(p, 0)
                if lag > 0:
                    shift = pd.Timedelta(days=lag)
                    p_baseline = window_mean(data, p, ref_start - shift, ref_end - shift)
                    p_actual = window_mean(data, p, an_start - shift, an_end - shift)
                else:
                    p_baseline = window_mean(data, p, ref_start, ref_end)
                    p_actual = window_mean(data, p, an_start, an_end)
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

    ranked_causes = _rank_causes(dag, target, nodes_in_scope, nodes_out)

    return {
        "target": target,
        "reference_window": {"start": reference_start, "end": reference_end},
        "analysis_window": {"start": analysis_start, "end": analysis_end},
        "nodes": nodes_out,
        "ranked_causes": ranked_causes,
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

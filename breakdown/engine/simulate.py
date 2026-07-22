"""Steady-state scenario simulation over the metric tree (the "what-if machine").

`run_scenario` takes a baseline window, a set of interventions ("set sessions
+15%") and user-asserted assumption links ("the discount cuts AOV by 8-12%"),
and propagates the implied deltas down the tree:

- **Intervened nodes** follow do-operator semantics: the node is severed from
  its own structural equation (its parents' deltas are ignored) and its delta
  is exact.
- **Formula nodes** propagate exactly through `eval_formula`.
- **Probabilistic nodes** propagate through their fitted `beta_raw` posterior
  (business units, d(child)/d(parent)). Trend and seasonality are unchanged by
  an intervention and cancel out of the delta. Lags are irrelevant under
  steady-state semantics: a lagged effect still fully arrives at equilibrium.
- **Assumption effects** are sampled from a Normal whose central 90% interval
  is the user's stated [low, high] range, added on top of structural
  propagation into the target node.

Uncertainty is draw-aligned Monte Carlo: every per-node delta is a vector of
`n_draws` samples and the draw index is preserved end-to-end, so an optimistic
coefficient draw at hop 1 feeds the same draw at hop 2 (uncertainty composes
through multi-hop paths, unlike interval arithmetic). One seeded generator per
call makes responses deterministic.

The per-source decomposition is an exact Shapley over sources (interventions +
assumptions): a point propagation (posterior-mean betas, mean effects) runs for
every subset of active sources, so per-source contributions sum exactly to the
node's point delta, with interactions through nonlinear formulas apportioned.

Like `run_rca`, this module is stateless: the caller passes its trace cache,
keyed by `(name, fit_end)`, and on-demand ADVI fits are added to it in place.
The fit window runs through `baseline_end` — the baseline is "current normal",
not an anomaly, so nothing is excluded.
"""
import math
from itertools import combinations
from typing import Any, Dict, List, Literal, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from breakdown.engine.model import fit_metric
from breakdown.engine.rca import window_mean
from breakdown.formula import eval_formula

_N_DRAWS = 2000
_MAX_SOURCES = 10
# The central 90% interval of a Normal spans mu +/- 1.645 sigma.
_Z90 = 1.6448536269514722

CAVEATS = [
    "Fitted coefficients are local slopes around the observed operating range; "
    "large moves may not extrapolate linearly.",
    "Learned edges are fitted associations, not experiments — unmodeled "
    "confounders can bias the propagated effects.",
    "Assumption-link effects are user-asserted beliefs, not fitted from data.",
]


class EffectRange(BaseModel):
    """User-stated effect range, read as the central 90% interval of a Normal.

    `relative` scales by the target metric's baseline; `absolute` is in the
    target's business units. `low == high` degenerates to a deterministic
    effect.
    """

    kind: Literal["absolute", "relative"]
    low: float
    high: float

    @model_validator(mode="after")
    def check_bounds(self) -> "EffectRange":
        if self.low > self.high:
            raise ValueError(f"effect low ({self.low}) must be <= high ({self.high})")
        return self


class Assumption(BaseModel):
    id: Optional[str] = None
    source: str
    target: str
    effect: EffectRange
    note: Optional[str] = None


class Intervention(BaseModel):
    metric: str
    mode: Literal["set", "delta", "pct"]
    value: float


class Lever(BaseModel):
    """Display metadata only: levers have no dynamics of their own in v1."""

    name: str
    value: Optional[float] = None
    unit: Optional[str] = None


class ScenarioRequest(BaseModel):
    baseline_start: str
    baseline_end: str
    interventions: List[Intervention] = Field(default_factory=list)
    assumptions: List[Assumption] = Field(default_factory=list)
    levers: List[Lever] = Field(default_factory=list)


def _intervention_label(iv: Intervention) -> str:
    if iv.mode == "pct":
        return f"{iv.metric} {iv.value:+.1%}"
    if iv.mode == "delta":
        return f"{iv.metric} {iv.value:+g}"
    return f"{iv.metric} = {iv.value:g}"


def _validate_scenario(
    dag: nx.DiGraph, scenario: ScenarioRequest
) -> List[Assumption]:
    """Cross-field validation; returns assumptions with ids filled in."""
    if not scenario.interventions and not scenario.assumptions:
        raise ValueError("Scenario must contain at least one intervention or assumption.")
    if len(scenario.interventions) + len(scenario.assumptions) > _MAX_SOURCES:
        raise ValueError(
            f"Scenario has too many sources: at most {_MAX_SOURCES} interventions "
            "+ assumptions are supported."
        )

    seen_metrics = set()
    for iv in scenario.interventions:
        if iv.metric not in dag:
            raise ValueError(f"Metric '{iv.metric}' not found in the metric tree.")
        if iv.metric in seen_metrics:
            raise ValueError(f"Metric '{iv.metric}' has more than one intervention.")
        seen_metrics.add(iv.metric)

    assumptions: List[Assumption] = []
    used_ids = {f"i:{m}" for m in seen_metrics}
    for i, a in enumerate(scenario.assumptions):
        if a.target not in dag:
            raise ValueError(f"Assumption target '{a.target}' not found in the metric tree.")
        aid = a.id or f"a{i}"
        if aid in used_ids:
            raise ValueError(f"Duplicate assumption id '{aid}'.")
        used_ids.add(aid)
        assumptions.append(a.model_copy(update={"id": aid}))
    return assumptions


def run_scenario(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    traces: Dict[Tuple[str, Optional[str]], Any],
    scenario: ScenarioRequest,
    advi_draws: int = 500,
    n_draws: int = _N_DRAWS,
) -> Dict[str, Any]:
    """Simulate a scenario and return the `POST /simulate` response shape.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` ->
    FitResult. Probabilistic nodes on affected paths without a cached trace
    are fit with ADVI on data through `baseline_end` and added to it.
    """
    assumptions = _validate_scenario(dag, scenario)

    frame = data.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    b_start = pd.to_datetime(scenario.baseline_start)
    b_end = pd.to_datetime(scenario.baseline_end)
    if b_end < b_start:
        raise ValueError(
            f"baseline_end '{scenario.baseline_end}' is before "
            f"baseline_start '{scenario.baseline_start}'."
        )

    rng = np.random.default_rng(0)
    baselines = {n: window_mean(frame, n, b_start, b_end) for n in dag.nodes}

    # Resolve interventions to absolute steady-state targets.
    targets: Dict[str, float] = {}
    for iv in scenario.interventions:
        base = baselines[iv.metric]
        if iv.mode == "set":
            targets[iv.metric] = iv.value
        elif iv.mode == "delta":
            targets[iv.metric] = base + iv.value
        else:
            targets[iv.metric] = base * (1.0 + iv.value)

    warnings: List[Dict[str, str]] = []
    for a in assumptions:
        if a.target in targets:
            warnings.append({
                "kind": "override",
                "metric": a.target,
                "detail": (
                    f"'{a.target}' is pinned by an intervention (do-operator), so "
                    f"assumption '{a.id}' has no effect while that intervention is active."
                ),
            })

    # Affected scope: seeds and everything downstream of them. Deltas never
    # flow parent-ward.
    seeds = set(targets) | {a.target for a in assumptions}
    affected = set(seeds)
    for s in seeds:
        affected |= nx.descendants(dag, s)
    order = [n for n in nx.topological_sort(dag) if n in affected]

    # Fit on demand: a probabilistic node needs its beta_raw posterior only if
    # a delta can actually reach it through a parent. fit_end is exclusive, so
    # baseline_end + 1 day fits through the baseline window; when the baseline
    # runs to the end of the data that is exactly the full-window fit.
    data_end = frame["date"].max()
    fit_end_key: Optional[str] = (
        None if b_end >= data_end else (b_end + pd.Timedelta(days=1)).date().isoformat()
    )
    needs_beta = set()
    for node in order:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and set(parents) & affected:
            needs_beta.add(node)
            if (node, fit_end_key) not in traces:
                traces[(node, fit_end_key)] = fit_metric(
                    dag, data, node, draws=advi_draws,
                    inference_method="advi", fit_end=fit_end_key,
                )

    # Draw posterior coefficients and assumption effects up front, in a fixed
    # order, so identical calls consume the rng identically.
    beta_draws: Dict[str, np.ndarray] = {}
    beta_means: Dict[str, np.ndarray] = {}
    for node in order:
        if node not in needs_beta:
            continue
        parents = list(dag.predecessors(node))
        arr = traces[(node, fit_end_key)].trace.posterior["beta_raw"].values.reshape(
            -1, len(parents)
        )
        beta_draws[node] = arr[rng.choice(arr.shape[0], size=n_draws)]
        beta_means[node] = arr.mean(axis=0)

    effect_draws: Dict[str, np.ndarray] = {}
    effect_means: Dict[str, float] = {}
    for a in assumptions:
        mu = (a.effect.low + a.effect.high) / 2.0
        sigma = (a.effect.high - a.effect.low) / (2.0 * _Z90)
        draws = rng.normal(mu, sigma, size=n_draws) if sigma > 0 else np.full(n_draws, mu)
        scale = baselines[a.target] if a.effect.kind == "relative" else 1.0
        effect_draws[a.id] = draws * scale
        effect_means[a.id] = mu * scale

    source_ids = [f"i:{iv.metric}" for iv in scenario.interventions] + [
        a.id for a in assumptions
    ]
    assumptions_by_target: Dict[str, List[Assumption]] = {}
    for a in assumptions:
        assumptions_by_target.setdefault(a.target, []).append(a)

    def propagate(active: frozenset, use_draws: bool) -> Dict[str, np.ndarray]:
        """One forward pass with the given sources active.

        With an intervention inactive (a Shapley subset), its node is *not*
        clamped and structural propagation flows through it — that is what
        makes marginal contributions well-defined.
        """
        size = n_draws if use_draws else 1
        deltas: Dict[str, np.ndarray] = {}
        for node in order:
            if node in targets and f"i:{node}" in active:
                deltas[node] = np.full(size, targets[node] - baselines[node])
                continue
            defn = dag.nodes[node]["definition"]
            parents = list(dag.predecessors(node))
            d = np.zeros(size)
            if defn.formula:
                base = {p: np.full(size, baselines[p]) for p in parents}
                shifted = {p: base[p] + deltas.get(p, 0.0) for p in parents}
                d = d + np.asarray(
                    eval_formula(defn.formula, shifted) - eval_formula(defn.formula, base),
                    dtype=float,
                )
            elif parents and any(p in deltas for p in parents):
                betas = beta_draws[node] if use_draws else beta_means[node][None, :]
                for i, p in enumerate(parents):
                    dp = deltas.get(p)
                    if dp is not None:
                        d = d + betas[:, i] * dp
            for a in assumptions_by_target.get(node, []):
                if a.id in active:
                    d = d + (effect_draws[a.id] if use_draws else effect_means[a.id])
            deltas[node] = d
        return deltas

    mc = propagate(frozenset(source_ids), use_draws=True)

    # Shapley over sources on point propagations; by efficiency the per-source
    # contributions sum exactly to the all-sources point delta at every node.
    subset_cache: Dict[frozenset, Dict[str, np.ndarray]] = {}

    def point_deltas(subset: frozenset) -> Dict[str, np.ndarray]:
        if subset not in subset_cache:
            subset_cache[subset] = propagate(subset, use_draws=False)
        return subset_cache[subset]

    n_sources = len(source_ids)
    contributions: Dict[str, Dict[str, float]] = {n: dict.fromkeys(source_ids, 0.0) for n in order}
    for sid in source_ids:
        others = [s for s in source_ids if s != sid]
        for r in range(n_sources):
            weight = (
                math.factorial(r) * math.factorial(n_sources - r - 1)
                / math.factorial(n_sources)
            )
            for coalition in combinations(others, r):
                without = point_deltas(frozenset(coalition))
                with_sid = point_deltas(frozenset(coalition) | {sid})
                for node in order:
                    contributions[node][sid] += weight * float(
                        with_sid[node][0] - without[node][0]
                    )

    hist_stats = {
        n: {
            "hist_min": float(np.min(frame[n].values)),
            "hist_max": float(np.max(frame[n].values)),
            "hist_mean": float(np.mean(frame[n].values)),
            "hist_std": float(np.std(frame[n].values)),
        }
        for n in dag.nodes
    }

    nodes_out: Dict[str, Any] = {}
    for node in dag.nodes:
        base = baselines[node]
        hist = hist_stats[node]
        if node not in affected:
            nodes_out[node] = {
                "status": "baseline",
                "baseline": base,
                "simulated": base,
                "delta": {"estimate": 0.0, "ci_95": [0.0, 0.0]},
                "relative_delta": 0.0,
                "prob_direction": None,
                "fit_quality": None,
                "extrapolation": {"flag": False, **hist},
                "contributions": [],
            }
            continue

        d = mc[node]
        estimate = float(d.mean())
        simulated = base + estimate
        outside_range = simulated < hist["hist_min"] or simulated > hist["hist_max"]
        outside_band = (
            hist["hist_std"] > 0
            and abs(simulated - hist["hist_mean"]) > 2 * hist["hist_std"]
        )
        flag = outside_range or outside_band
        if flag:
            if simulated > hist["hist_max"]:
                detail = (
                    f"Simulated value {simulated:.4g} for '{node}' is above the "
                    f"historical max {hist['hist_max']:.4g}."
                )
            elif simulated < hist["hist_min"]:
                detail = (
                    f"Simulated value {simulated:.4g} for '{node}' is below the "
                    f"historical min {hist['hist_min']:.4g}."
                )
            else:
                detail = (
                    f"Simulated value {simulated:.4g} for '{node}' is more than 2 "
                    f"standard deviations from the historical mean {hist['hist_mean']:.4g}."
                )
            warnings.append({"kind": "extrapolation", "metric": node, "detail": detail})
        if simulated < 0 and hist["hist_min"] >= 0:
            warnings.append({
                "kind": "non_physical",
                "metric": node,
                "detail": (
                    f"Simulated value {simulated:.4g} for '{node}' is negative but "
                    "the metric has never been negative historically."
                ),
            })

        fit_quality = None
        if node in needs_beta:
            fit_quality = traces[(node, fit_end_key)].diagnostics.get("fit_quality")

        contribs = [
            {"source": sid, "estimate": est}
            for sid, est in contributions[node].items()
            if abs(est) > 1e-12
        ]
        contribs.sort(key=lambda c: abs(c["estimate"]), reverse=True)

        nodes_out[node] = {
            "status": "intervened" if node in targets else "affected",
            "baseline": base,
            "simulated": simulated,
            "delta": {
                "estimate": estimate,
                "ci_95": [
                    float(np.percentile(d, 2.5)),
                    float(np.percentile(d, 97.5)),
                ],
            },
            "relative_delta": (estimate / base) if abs(base) >= 1e-12 else None,
            "prob_direction": float(max((d > 0).mean(), (d < 0).mean())),
            "fit_quality": fit_quality,
            "extrapolation": {"flag": bool(flag), **hist},
            "contributions": contribs,
        }

    sources = [
        {"id": f"i:{iv.metric}", "kind": "intervention", "label": _intervention_label(iv)}
        for iv in scenario.interventions
    ] + [
        {"id": a.id, "kind": "assumption", "label": f"{a.source} → {a.target}", "note": a.note}
        for a in assumptions
    ]

    return {
        "baseline_window": {"start": scenario.baseline_start, "end": scenario.baseline_end},
        "n_draws": n_draws,
        "seed": 0,
        "sources": sources,
        "nodes": nodes_out,
        "warnings": warnings,
        "caveats": CAVEATS,
    }

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

**Prior mode** (`data=None`): the same machinery with zero data — the minimum
sample size is zero. Baselines come from each node's asserted `baseline`
declaration (formula nodes derive theirs per-draw from parents, so the
identity holds under the stated beliefs), and `beta_raw` is sampled directly
from each edge's YAML prior — priors are already stated in business units;
the `x_std/y_std` rescaling exists only to reach normalized space for
fitting, so with nothing to fit the prior IS the coefficient distribution.
Extrapolation flags come from declared `plausible` bounds instead of history.
Propagation, do-operator semantics, draw alignment, and the Shapley source
decomposition are identical; the response is labeled `mode: "prior"` and
carries prior-specific caveats. See `knowledge/prior_mode_design.md`.
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
from breakdown.grains import ensure_grained, fit_grain, snap_window

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

PRIOR_CAVEATS = [
    "All coefficients and baselines are stated beliefs (priors), not estimates "
    "from data — results quantify the consequences of your assumptions, not "
    "evidence.",
    "Belief draws are sampled independently per edge and per baseline; "
    "correlated beliefs are not represented, so intervals may be too narrow "
    "or too wide where beliefs co-vary.",
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
    # Required in fitted mode (the window that defines "current normal");
    # rejected in prior mode, where operating points come from the tree's
    # `baseline` declarations. Presence is validated per mode in run_scenario.
    baseline_start: Optional[str] = None
    baseline_end: Optional[str] = None
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


def _sample_prior(prior: Any, size: int, rng: np.random.Generator) -> np.ndarray:
    """Draw from a YAML `Prior` directly in business units (prior-mode
    beta_raw). The distributions mirror `_PRIOR_DISTRIBUTIONS` in model.py and
    the parameter defaults mirror `scale_prior_params`."""
    d, p = prior.distribution, prior.params
    if d == "Normal":
        return rng.normal(p.get("mu", 0.0), p.get("sigma", 1.0), size)
    if d == "HalfNormal":
        return np.abs(rng.normal(0.0, p.get("sigma", 1.0), size))
    if d == "Exponential":
        return rng.exponential(1.0 / p.get("lam", 1.0), size)
    if d == "LogNormal":
        return rng.lognormal(p.get("mu", 0.0), p.get("sigma", 1.0), size)
    raise ValueError(
        f"Unsupported prior distribution: '{d}'. "
        "Must be one of: Normal, HalfNormal, Exponential, LogNormal"
    )


def _prior_mean(prior: Any) -> float:
    """Analytic mean of a YAML `Prior` — the point value the Shapley source
    decomposition propagates (the prior-mode analog of the posterior mean)."""
    d, p = prior.distribution, prior.params
    if d == "Normal":
        return float(p.get("mu", 0.0))
    if d == "HalfNormal":
        return float(p.get("sigma", 1.0)) * math.sqrt(2.0 / math.pi)
    if d == "Exponential":
        return 1.0 / float(p.get("lam", 1.0))
    if d == "LogNormal":
        return math.exp(float(p.get("mu", 0.0)) + float(p.get("sigma", 1.0)) ** 2 / 2.0)
    raise ValueError(
        f"Unsupported prior distribution: '{d}'. "
        "Must be one of: Normal, HalfNormal, Exponential, LogNormal"
    )


def validate_prior_mode(dag: nx.DiGraph) -> List[str]:
    """Every violation keeping a tree from running prior-mode scenarios.

    A prior-mode tree declares beliefs everywhere: an asserted `baseline` on
    every non-formula node (the response reports every node's baseline, and
    formula nodes derive theirs), and an explicit business-unit prior on every
    probabilistic edge — the fitted-mode fallback (Normal(0,1) in normalized
    space) is meaningless without data to define the scale. Returns [] when
    ready. `breakdown doctor` reuses this for a pre-flight check."""
    problems: List[str] = []
    for n in nx.topological_sort(dag):
        defn = dag.nodes[n]["definition"]
        parents = list(dag.predecessors(n))
        if defn.formula is None and defn.baseline is None:
            problems.append(
                f"metric '{n}' needs a `baseline` declaration "
                "(only formula nodes derive theirs from parents)"
            )
        if parents and not defn.formula:
            for p in parents:
                if not (defn.priors.get(p) or defn.priors.get("coefficient")):
                    problems.append(
                        f"edge '{p}' -> '{n}' needs an explicit business-unit "
                        "prior (parent-specific or shared `coefficient`)"
                    )
    return problems


def run_scenario(
    dag: nx.DiGraph,
    data: Optional[pd.DataFrame],
    traces: Dict[Tuple[str, Optional[str]], Any],
    scenario: ScenarioRequest,
    advi_draws: int = 500,
    n_draws: int = _N_DRAWS,
) -> Dict[str, Any]:
    """Simulate a scenario and return the `POST /simulate` response shape.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` ->
    FitResult. Probabilistic nodes on affected paths without a cached trace
    are fit with ADVI on data through `baseline_end` and added to it.

    **`data=None` selects prior mode** (a tree with no data): baselines come
    from the YAML `baseline` declarations, coefficients are sampled from the
    YAML priors directly in business units, extrapolation flags come from
    declared `plausible` bounds, and `traces` is ignored. The scenario must
    then omit `baseline_start`/`baseline_end`. The response gains
    `mode: "prior"`, per-node `baseline_ci_95`, and prior-specific caveats.
    """
    assumptions = _validate_scenario(dag, scenario)
    prior_mode = data is None
    rng = np.random.default_rng(0)

    if prior_mode:
        if scenario.baseline_start is not None or scenario.baseline_end is not None:
            raise ValueError(
                "Prior mode (no data) takes no baseline window: operating "
                "points come from the tree's `baseline` declarations."
            )
        problems = validate_prior_mode(dag)
        if problems:
            raise ValueError("Tree is not prior-mode ready: " + "; ".join(problems))
    else:
        if scenario.baseline_start is None or scenario.baseline_end is None:
            raise ValueError(
                "baseline_start and baseline_end are required when the tree has data."
            )
        data = ensure_grained(data)
        b_start = pd.to_datetime(scenario.baseline_start)
        b_end = pd.to_datetime(scenario.baseline_end)
        if b_end < b_start:
            raise ValueError(
                f"baseline_end '{scenario.baseline_end}' is before "
                f"baseline_start '{scenario.baseline_start}'."
            )

    # Steady-state deltas are in per-native-period units. A flow parent
    # feeding a coarser child was fitted against its *sum* per child period,
    # so its baseline/delta must be scaled by periods-per-child-period at
    # that edge; stocks (last-value) and same-grain edges pass through.
    _PERIODS_PER = {("day", "week"): 7.0, ("day", "month"): 365.25 / 12}
    edge_scale: Dict[Tuple[str, str], float] = {}
    for child in dag.nodes:
        cg = fit_grain(dag, child)
        for p in dag.predecessors(child):
            pdefn = dag.nodes[p]["definition"]
            pg = getattr(pdefn, "grain", "day")
            pkind = getattr(pdefn, "kind", "flow")
            if pg == cg or pkind != "flow":
                edge_scale[(p, child)] = 1.0
            else:
                edge_scale[(p, child)] = _PERIODS_PER[(pg, cg)]

    # Baselines: per-node point value (`base_mu`) plus a draw-aligned vector
    # (`base_draws`, length n_draws).
    #
    # Fitted mode: the observed window mean at each node's native grain over
    # the whole periods inside the baseline window (exact -> constant
    # vectors). Unlike RCA's per-node degrade, a node with no baseline cannot
    # be simulated at all, so this errors loudly.
    #
    # Prior mode: asserted Normals (the [low, high] central-90% convention)
    # for source/probabilistic nodes; formula nodes derive per-draw
    # f(scaled parent draws) in topological order, so the identity holds
    # under the stated beliefs and nonlinear composition (Jensen terms,
    # shared-parent co-movement) is exact per draw. A probabilistic node's
    # asserted baseline need not equal beta·parents — the intercept/trend
    # absorb the level in fitted mode, and only deltas propagate here.
    base_mu: Dict[str, float] = {}
    base_draws: Dict[str, np.ndarray] = {}
    if prior_mode:
        for n in nx.topological_sort(dag):
            defn = dag.nodes[n]["definition"]
            if defn.formula:
                parents = list(dag.predecessors(n))
                vals = {p: base_draws[p] * edge_scale[(p, n)] for p in parents}
                base_draws[n] = np.asarray(eval_formula(defn.formula, vals), dtype=float)
                base_mu[n] = float(base_draws[n].mean())
            else:
                b = defn.baseline
                sigma = (b.high - b.low) / (2.0 * _Z90)
                base_draws[n] = (
                    rng.normal(b.mu, sigma, n_draws) if sigma > 0 else np.full(n_draws, b.mu)
                )
                # The seeded MC mean, not the analytic mu: every reported
                # number is a statistic of the same draws, so e.g. a `set`
                # intervention's simulated level is exactly its pinned value.
                base_mu[n] = float(base_draws[n].mean())
    else:
        for n in dag.nodes:
            g = fit_grain(dag, n)
            snapped = snap_window(b_start, b_end, g)
            if snapped is None:
                raise ValueError(
                    f"Baseline window [{scenario.baseline_start}, "
                    f"{scenario.baseline_end}] contains no whole '{g}' period for "
                    f"metric '{n}' (grain '{g}')."
                )
            base_mu[n] = window_mean(
                data.series(n), n, snapped.first_start, snapped.last_start
            )
            base_draws[n] = np.full(n_draws, base_mu[n])

    # Resolve interventions to per-draw steady-state deltas, plus the point
    # deltas the Shapley decomposition propagates. With a certain baseline
    # these are the constants they always were; with an uncertain (prior-mode)
    # baseline, `set` pins the LEVEL exactly so its delta inherits baseline
    # uncertainty, `pct` scales the baseline draws, and `delta` stays exact.
    intervened = {iv.metric for iv in scenario.interventions}
    tgt_delta_draws: Dict[str, np.ndarray] = {}
    tgt_delta_point: Dict[str, float] = {}
    for iv in scenario.interventions:
        bd, bm = base_draws[iv.metric], base_mu[iv.metric]
        if iv.mode == "set":
            tgt_delta_draws[iv.metric] = iv.value - bd
            tgt_delta_point[iv.metric] = iv.value - bm
        elif iv.mode == "delta":
            tgt_delta_draws[iv.metric] = np.full(n_draws, iv.value)
            tgt_delta_point[iv.metric] = iv.value
        else:
            tgt_delta_draws[iv.metric] = bd * iv.value
            tgt_delta_point[iv.metric] = bm * iv.value

    warnings: List[Dict[str, str]] = []
    for a in assumptions:
        if a.target in intervened:
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
    seeds = set(intervened) | {a.target for a in assumptions}
    affected = set(seeds)
    for s in seeds:
        affected |= nx.descendants(dag, s)
    order = [n for n in nx.topological_sort(dag) if n in affected]

    # A probabilistic node needs a beta_raw distribution only if a delta can
    # actually reach it through a parent. Fitted mode fits on demand: fit_end
    # is exclusive, so baseline_end + 1 day fits through the baseline window;
    # when the baseline runs to the end of the data that is exactly the
    # full-window fit. Prior mode fits nothing — validate_prior_mode already
    # guaranteed an explicit prior on every probabilistic edge.
    fit_end_key: Optional[str] = None
    if not prior_mode:
        data_end = data.date_end
        fit_end_key = (
            None if b_end >= data_end else (b_end + pd.Timedelta(days=1)).date().isoformat()
        )
    needs_beta = set()
    for node in order:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and set(parents) & affected:
            needs_beta.add(node)
            if not prior_mode and (node, fit_end_key) not in traces:
                traces[(node, fit_end_key)] = fit_metric(
                    dag, data, node, draws=advi_draws,
                    inference_method="advi", fit_end=fit_end_key,
                    random_seed=0,
                )

    # Draw coefficients and assumption effects up front, in a fixed order, so
    # identical calls consume the rng identically. Fitted mode indexes the
    # beta_raw posterior; prior mode samples the YAML prior directly in
    # business units (independent across parents and nodes — priors carry no
    # cross-coefficient correlation) with the analytic prior mean as the
    # point value.
    beta_draws: Dict[str, np.ndarray] = {}
    beta_means: Dict[str, np.ndarray] = {}
    for node in order:
        if node not in needs_beta:
            continue
        parents = list(dag.predecessors(node))
        if prior_mode:
            defn = dag.nodes[node]["definition"]
            priors = [defn.priors.get(p) or defn.priors.get("coefficient") for p in parents]
            beta_draws[node] = np.column_stack(
                [_sample_prior(pr, n_draws, rng) for pr in priors]
            )
            beta_means[node] = np.array([_prior_mean(pr) for pr in priors])
        else:
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
        if a.effect.kind == "relative":
            # Scaled by the target's baseline draws — draw-aligned, so an
            # optimistic baseline draw scales the effect in the same world.
            # (Fitted baselines are constant vectors: identical to the old
            # scalar scaling.)
            effect_draws[a.id] = draws * base_draws[a.target]
            effect_means[a.id] = mu * base_mu[a.target]
        else:
            effect_draws[a.id] = draws
            effect_means[a.id] = mu

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

        def base_vec(p: str, node: str) -> np.ndarray:
            # Draw-aligned baselines (constant vectors in fitted mode; sampled
            # from asserted ranges in prior mode), scaled to the edge's grain.
            s = edge_scale[(p, node)]
            return (base_draws[p] if use_draws else np.array([base_mu[p]])) * s

        deltas: Dict[str, np.ndarray] = {}
        for node in order:
            if node in intervened and f"i:{node}" in active:
                deltas[node] = (
                    tgt_delta_draws[node] if use_draws
                    else np.array([tgt_delta_point[node]])
                )
                continue
            defn = dag.nodes[node]["definition"]
            parents = list(dag.predecessors(node))
            d = np.zeros(size)
            if defn.formula:
                # The identity holds at the node's grain: finer flow parents
                # enter as their per-child-period sum (baseline and delta
                # alike scaled by the edge's periods-per-period factor).
                base = {p: base_vec(p, node) for p in parents}
                shifted = {
                    p: base[p] + deltas.get(p, 0.0) * edge_scale[(p, node)]
                    for p in parents
                }
                d = d + np.asarray(
                    eval_formula(defn.formula, shifted) - eval_formula(defn.formula, base),
                    dtype=float,
                )
            elif parents and any(p in deltas for p in parents):
                betas = beta_draws[node] if use_draws else beta_means[node][None, :]
                for i, p in enumerate(parents):
                    dp = deltas.get(p)
                    if dp is not None:
                        # beta_raw was fitted against the parent aggregated to
                        # this node's grain — scale the per-native-period
                        # delta accordingly.
                        d = d + betas[:, i] * (dp * edge_scale[(p, node)])
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

    # Honesty stats: full-history bands in fitted mode; declared `plausible`
    # bounds in prior mode (no bounds -> no flag, and the block says so
    # rather than inventing a band).
    hist_stats: Dict[str, Dict[str, Any]] = {}
    if prior_mode:
        for n in dag.nodes:
            pl = dag.nodes[n]["definition"].plausible
            hist_stats[n] = {
                "plausible_min": pl.min if pl is not None else None,
                "plausible_max": pl.max if pl is not None else None,
            }
    else:
        for n in dag.nodes:
            vals = data.series(n)[n].to_numpy()
            hist_stats[n] = {
                "hist_min": float(np.min(vals)),
                "hist_max": float(np.max(vals)),
                "hist_mean": float(np.mean(vals)),
                "hist_std": float(np.std(vals)),
            }

    nodes_out: Dict[str, Any] = {}
    for node in dag.nodes:
        base = base_mu[node]
        hist = hist_stats[node]
        baseline_ci = None
        if prior_mode and float(base_draws[node].std()) > 0:
            baseline_ci = [
                float(np.percentile(base_draws[node], 2.5)),
                float(np.percentile(base_draws[node], 97.5)),
            ]
        if node not in affected:
            entry = {
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
            if prior_mode:
                entry["baseline_ci_95"] = baseline_ci
            nodes_out[node] = entry
            continue

        d = mc[node]
        estimate = float(d.mean())
        simulated = base + estimate
        flag = False
        if prior_mode:
            p_min, p_max = hist["plausible_min"], hist["plausible_max"]
            if p_max is not None and simulated > p_max:
                flag = True
                detail = (
                    f"Simulated value {simulated:.4g} for '{node}' is above the "
                    f"declared plausible max {p_max:.4g}."
                )
            elif p_min is not None and simulated < p_min:
                flag = True
                detail = (
                    f"Simulated value {simulated:.4g} for '{node}' is below the "
                    f"declared plausible min {p_min:.4g}."
                )
            if flag:
                warnings.append({"kind": "extrapolation", "metric": node, "detail": detail})
        else:
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
        if not prior_mode and node in needs_beta:
            fit_quality = traces[(node, fit_end_key)].diagnostics.get("fit_quality")

        contribs = [
            {"source": sid, "estimate": est}
            for sid, est in contributions[node].items()
            if abs(est) > 1e-12
        ]
        contribs.sort(key=lambda c: abs(c["estimate"]), reverse=True)

        entry = {
            "status": "intervened" if node in intervened else "affected",
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
        if prior_mode:
            entry["baseline_ci_95"] = baseline_ci
        nodes_out[node] = entry

    sources = [
        {"id": f"i:{iv.metric}", "kind": "intervention", "label": _intervention_label(iv)}
        for iv in scenario.interventions
    ] + [
        {"id": a.id, "kind": "assumption", "label": f"{a.source} → {a.target}", "note": a.note}
        for a in assumptions
    ]

    return {
        "mode": "prior" if prior_mode else "fitted",
        "baseline_window": (
            None if prior_mode
            else {"start": scenario.baseline_start, "end": scenario.baseline_end}
        ),
        "n_draws": n_draws,
        "seed": 0,
        "sources": sources,
        "nodes": nodes_out,
        "warnings": warnings,
        "caveats": PRIOR_CAVEATS if prior_mode else CAVEATS,
    }

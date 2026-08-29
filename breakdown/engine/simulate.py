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
keyed by `(name, fit_end)`, and on-demand fits are added to it in place.
The fit window runs through `baseline_end` — the baseline is "current normal",
not an anomaly, so nothing is excluded.

**Cold-start mode** (`data=None`): the same machinery with zero data — the minimum
sample size is zero. Baselines come from each node's asserted `baseline`
declaration (formula nodes derive theirs per-draw from parents, so the
identity holds under the stated beliefs), and `beta_raw` is sampled directly
from each edge's YAML prior — priors are already stated in business units;
the `x_std/y_std` rescaling exists only to reach normalized space for
fitting, so with nothing to fit the prior IS the coefficient distribution.
Extrapolation flags come from declared `plausible` bounds instead of history.
The `non_physical` flag is unchanged by the mode where the tree declares a
bound (`share: true`): a declared bound is a fact about the metric, not an
observation, so it holds with or without data.
Propagation, do-operator semantics, draw alignment, and the Shapley source
decomposition are identical; the response is labeled `mode: "cold_start"` and
carries cold-start caveats. See `knowledge/cold_start_design.md`.
"""

import datetime
import math
from itertools import combinations
from typing import Any, Dict, List, Literal, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from breakdown.engine.model import (
    FIT_RANDOM_SEED,
    NUTS_DRAWS,
    cached_fit_is_usable,
    fit_metric,
)
from breakdown.engine.progress import ProgressFn
from breakdown.engine.progress import report as _report
from breakdown.engine.rca import (
    direction_fields,
    node_window_value,
    rate_window_method,
    rate_window_method_reason,
)
from breakdown.formula import divisor_expressions, eval_formula
from breakdown.grains import ensure_grained, fit_grain, snap_window, to_date

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

COLD_START_CAVEATS = [
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
    # rejected in cold-start mode, where operating points come from the tree's
    # `baseline` declarations. Presence is validated per mode in run_scenario.
    baseline_start: Optional[str] = None
    baseline_end: Optional[str] = None
    interventions: List[Intervention] = Field(default_factory=list)
    assumptions: List[Assumption] = Field(default_factory=list)
    levers: List[Lever] = Field(default_factory=list)

    @field_validator("baseline_start", "baseline_end")
    @classmethod
    def check_baseline_date(cls, v: Optional[str]) -> Optional[str]:
        """A date-shaped field is validated as a date, not as a `str`.

        `pd.to_datetime("")` is `NaT`, and `NaT < NaT` is False — so an empty
        baseline window passed the "is it backwards?" check below and reached
        the fit as a not-a-date. Same defect as the query-parameter one
        (`api/main._iso_date`); this is the body-parameter half of it, and
        being on the model is what makes it a 422 rather than an exception.
        """
        if v is None:
            return None
        try:
            datetime.date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"must be a valid YYYY-MM-DD date, got '{v}'")
        return v


#: What a *declaration* in the tree says a node's value cannot leave, as
#: `(min, max)`. Structural, never empirical: no entry here may be derived from
#: an observation, because history is evidence about what has happened and a
#: bound of this kind is a fact about what the metric *is*.
#:
#: **Both ends or neither** (roadmap C26). The defect this table replaces was a
#: physical-bound check with one side: `simulated < 0 and hist_min >= 0` fired
#: on a negative churn rate and nothing at all fired on an activity rate
#: simulated to 1.025 — 102.5% of members active, reported as merely "above the
#: historical max". A bound that is a fact about the metric bounds it in both
#: directions, so an entry naming one end is a half-written entry, and
#: `tests/test_project_invariants.py` fails on one.
_STRUCTURAL_BOUNDS: Dict[str, Tuple[float, float]] = {
    # `share: true` — the node is a proportion of some whole. Nothing weaker
    # implies this; see `MetricDefinition.share` for why `denominator`,
    # `format.style: percent` and `plausible` were each rejected as a source.
    "share": (0.0, 1.0),
}


def _structural_bounds(defn: Any) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """The bounds the tree declares for this node, and the sentence for them.

    Returns `(min, max, why)`, all `None` when the tree declares nothing. The
    definition is the only input on purpose — a caller cannot pass history in.
    """
    if defn.share is True:
        lo, hi = _STRUCTURAL_BOUNDS["share"]
        return (
            lo,
            hi,
            "the metric is declared a share (`share: true`), so it is a "
            "proportion of a whole and cannot leave [0, 1] — whatever the "
            "history happens to contain",
        )
    return None, None, None


def _non_physical_warning(
    node: str,
    defn: Any,
    simulated: float,
    hist: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """The one place a simulated value is called impossible rather than unusual.

    Two sources, and the difference between them is the whole point of the
    split:

    - **A declared bound** (`_STRUCTURAL_BOUNDS`) is a fact about the metric's
      definition, so it applies in both directions, in both modes, and never
      reads `hist` — `_structural_bounds` takes only the definition, so it
      cannot.
    - **The historical floor** is a *conservative inference*: a metric that has
      never been negative is one that probably cannot be, which is why it is
      gated on `hist_min >= 0` — plenty of metrics (net new MRR, a difference
      of any two flows) go negative honestly. It has no ceiling counterpart and
      must not grow one: "never observed above X" is an argument about the
      sample, not about the quantity, and that is `extrapolation`'s job.

    A node with a declared bound reports the declaration rather than the
    inference when both apply. The declaration is the stronger claim and the
    reader can check it against the tree; "never seen negative" they cannot.

    `hist` is `None` in cold-start mode, where there is no history to infer
    from — a declared bound still holds there, which is half of why this is one
    function instead of a branch inside each mode.
    """
    lo, hi, why = _structural_bounds(defn)
    if hi is not None and simulated > hi:
        return {
            "kind": "non_physical",
            "metric": node,
            "detail": (f"Simulated value {simulated:.4g} for '{node}' is above {hi:g}: {why}."),
        }
    if lo is not None and simulated < lo:
        return {
            "kind": "non_physical",
            "metric": node,
            "detail": (f"Simulated value {simulated:.4g} for '{node}' is below {lo:g}: {why}."),
        }
    if (
        hist is not None
        and hist.get("hist_min") is not None
        and simulated < 0
        and hist["hist_min"] >= 0
    ):
        return {
            "kind": "non_physical",
            "metric": node,
            "detail": (
                f"Simulated value {simulated:.4g} for '{node}' is negative but "
                "the metric has never been negative historically."
            ),
        }
    return None


def _intervention_label(iv: Intervention) -> str:
    if iv.mode == "pct":
        return f"{iv.metric} {iv.value:+.1%}"
    if iv.mode == "delta":
        return f"{iv.metric} {iv.value:+g}"
    return f"{iv.metric} = {iv.value:g}"


def _validate_scenario(dag: nx.DiGraph, scenario: ScenarioRequest) -> List[Assumption]:
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
    """Draw from a YAML `Prior` directly in business units (cold-start
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
    decomposition propagates (the cold-start analog of the posterior mean)."""
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


def validate_cold_start(dag: nx.DiGraph) -> List[str]:
    """Every violation keeping a tree from running cold-start scenarios.

    A cold-start tree declares beliefs everywhere: an asserted `baseline` on
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
    inference_method: str = "nuts",
    draws: int = NUTS_DRAWS,
    n_draws: int = _N_DRAWS,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """Simulate a scenario and return the `POST /simulate` response shape.

    `traces` is the caller's cache, keyed by `(metric name, fit_end)` ->
    FitResult. Probabilistic nodes on affected paths without a usable cached
    trace are fitted on data through `baseline_end` and added to it.

    `inference_method` defaults to `"nuts"` and carries the same meaning and
    the same reasoning as it does on `run_rca` — a scenario propagates every
    fitted slope downstream, so an approximation that misstates one is a wrong
    number at every node below it. `"advi"` stays available for triage speed,
    with PSIS k-hat reported on each node it fits. `draws` is the posterior
    draw count (per chain under NUTS), forwarded here for the same reason it
    is on `run_rca` — this module resamples that posterior into `beta_draws` —
    and defaulting to the engine's one `NUTS_DRAWS`; `n_draws` is the separate
    count of Monte-Carlo propagation draws through the DAG. Warm-up and chains
    come from `fit_metric`'s `NUTS_TUNE` / `NUTS_CHAINS`, so a scenario and an
    RCA of the same node sample from the same adaptation (roadmap C27).

    **`data=None` selects cold-start mode** (a tree with no data): baselines come
    from the YAML `baseline` declarations, coefficients are sampled from the
    YAML priors directly in business units, extrapolation flags come from
    declared `plausible` bounds, and `traces` is ignored. The scenario must
    then omit `baseline_start`/`baseline_end`. The response gains
    `mode: "cold_start"`, per-node `baseline_ci_95`, and cold-start caveats.
    """
    assumptions = _validate_scenario(dag, scenario)
    cold_start = data is None
    rng = np.random.default_rng(0)

    if cold_start:
        if scenario.baseline_start is not None or scenario.baseline_end is not None:
            raise ValueError(
                "Cold-start mode (no data) takes no baseline window: operating "
                "points come from the tree's `baseline` declarations."
            )
        problems = validate_cold_start(dag)
        if problems:
            raise ValueError("Tree is not cold-start ready: " + "; ".join(problems))
    else:
        if scenario.baseline_start is None or scenario.baseline_end is None:
            raise ValueError("baseline_start and baseline_end are required when the tree has data.")
        data = ensure_grained(data)
        b_start = to_date(scenario.baseline_start, "baseline_start")
        b_end = to_date(scenario.baseline_end, "baseline_end")
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
    # Cold-start mode: asserted Normals (the [low, high] central-90% convention)
    # for source/probabilistic nodes; formula nodes derive per-draw
    # f(scaled parent draws) in topological order, so the identity holds
    # under the stated beliefs and nonlinear composition (Jensen terms,
    # shared-parent co-movement) is exact per draw. A probabilistic node's
    # asserted baseline need not equal beta·parents — the intercept/trend
    # absorb the level in fitted mode, and only deltas propagate here.
    base_mu: Dict[str, float] = {}
    base_draws: Dict[str, np.ndarray] = {}
    # (cold start) Draws come from `_baseline_draws`: the declared belief's
    # distribution, truncated to the declared `plausible` bounds (C7a) — the
    # bounds used to be consulted only *after* sampling, for extrapolation
    # flags, so the shipped example drew ~1% negative customer counts.
    # Which arithmetic formed a rate's baseline, said where the number is read
    # (roadmap C25a). RCA labels every rate it reports (`window_aggregate`,
    # 1.11d); this surface computed the same number through the same entry
    # point and published it bare — the labelling policy's first propagation
    # test, failed. Fitted mode only: a cold-start baseline is a declared
    # belief, which the cold-start caveats already label as such.
    window_basis: Dict[str, Optional[str]] = {}
    window_basis_reason: Dict[str, Optional[str]] = {}
    if cold_start:
        for n in nx.topological_sort(dag):
            defn = dag.nodes[n]["definition"]
            if defn.formula:
                parents = list(dag.predecessors(n))
                vals = {p: base_draws[p] * edge_scale[(p, n)] for p in parents}
                # A ratio whose divisor draws cross zero has a Cauchy-like
                # distribution: its mean does not exist, and the sample mean
                # below would be whatever the seed's nearest-zero denominator
                # made it — the shipped example was a $2.1M CAC from an
                # ordinary order-of-magnitude belief (roadmap C7). Refused
                # rather than summarized differently: keeping the mean is what
                # keeps per-source contributions summing exactly to the delta
                # (the mean is linear; a median is not), so the fix is to make
                # the mean exist — truncate the belief away from zero — not to
                # publish a different statistic that breaks that property.
                for div_src in divisor_expressions(defn.formula):
                    div = np.asarray(eval_formula(div_src, vals), dtype=float)
                    if float(div.min()) <= 0.0 <= float(div.max()):
                        raise ValueError(
                            f"Cold-start metric '{n}': the belief draws for "
                            f"divisor '{div_src}' in formula '{defn.formula}' "
                            "cross zero, so the ratio's Monte-Carlo mean does "
                            "not exist and its centre would be an artifact of "
                            "the seed. Declare `plausible: {min: ...}` (> 0) "
                            "on the divisor's metric(s) — draws are truncated "
                            "to plausible bounds — or use "
                            "`baseline: {distribution: LogNormal}`, whose "
                            "support excludes zero, or tighten the belief so "
                            "the divisor cannot cross zero."
                        )
                base_draws[n] = np.asarray(eval_formula(defn.formula, vals), dtype=float)
                base_mu[n] = float(base_draws[n].mean())
            else:
                base_draws[n] = _baseline_draws(n, defn, rng, n_draws)
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
            # Same window-aggregation rules as RCA, through the same entry
            # point: a rate's baseline is Σnumerator / Σdenominator over the
            # window, never the average of its per-period ratios (roadmap
            # 1.11c). A baseline that is undefined — every period of the window
            # undefined — cannot be simulated from, and unlike RCA's per-node
            # degrade every node here is needed, so it errors loudly.
            base_mu[n] = node_window_value(data, n, snapped.first_start, snapped.last_start, g)
            window_basis[n] = rate_window_method(
                data, n, snapped.first_start, snapped.last_start, g
            )
            window_basis_reason[n] = rate_window_method_reason(data, n, window_basis[n])
            if not np.isfinite(base_mu[n]):
                raise ValueError(
                    f"Metric '{n}' has no value over the baseline window "
                    f"[{scenario.baseline_start}, {scenario.baseline_end}]: every "
                    f"whole '{g}' period in it is undefined (a rate whose "
                    "denominator is zero has no rate). Choose a baseline window "
                    "containing at least one defined period."
                )
            base_draws[n] = np.full(n_draws, base_mu[n])

    # Resolve interventions to per-draw steady-state deltas, plus the point
    # deltas the Shapley decomposition propagates. With a certain baseline
    # these are the constants they always were; with an uncertain (cold-start)
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
            warnings.append(
                {
                    "kind": "override",
                    "metric": a.target,
                    "detail": (
                        f"'{a.target}' is pinned by an intervention (do-operator), so "
                        f"assumption '{a.id}' has no effect while that intervention is active."
                    ),
                }
            )

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
    # full-window fit. Cold-start mode fits nothing — validate_cold_start already
    # guaranteed an explicit prior on every probabilistic edge.
    fit_end_key: Optional[str] = None
    if not cold_start:
        data_end = data.date_end
        fit_end_key = (
            None if b_end >= data_end else (b_end + pd.Timedelta(days=1)).date().isoformat()
        )
    # Work list first, fits second, so `progress` can report a real denominator
    # rather than counting toward an unknown total.
    needs_beta = set()
    to_fit = []
    for node in order:
        defn = dag.nodes[node]["definition"]
        parents = list(dag.predecessors(node))
        if parents and not defn.formula and set(parents) & affected:
            needs_beta.add(node)
            if cold_start:
                continue
            cached = traces.get((node, fit_end_key))
            if cached is not None and cached_fit_is_usable(cached, inference_method):
                continue
            to_fit.append(node)

    # Same policy as `run_rca`, imported from the same helper so the two cannot
    # drift: the sampler the caller asked for is the sampler that runs, and a
    # cached fit is reused only when it is at least as good as the one the
    # request would produce.
    for i, node in enumerate(to_fit, 1):
        _report(progress, stage="fitting", metric=node, current=i, total=len(to_fit))
        try:
            fit = fit_metric(
                dag,
                data,
                node,
                draws=draws,
                inference_method=inference_method,
                fit_end=fit_end_key,
                random_seed=FIT_RANDOM_SEED,
            )
        except ValueError as e:
            # `run_rca` degrades this to a per-node `fit_failed` and answers for
            # the rest of the tree. A scenario cannot: it *propagates* along the
            # DAG, so a node with no estimable coefficient breaks the chain, and
            # every node downstream of it would silently report a delta missing
            # one of its parents' effects — a confident wrong number, which is
            # worse than no answer. So this refuses, but it refuses in terms of
            # the scenario the caller actually ran rather than leaking
            # `_normalize`'s "Column 'x' has zero variance".
            #
            # (The better answer is to mark this node *and its descendants*
            # un-simulated and simulate the rest, the way RCA degrades. That
            # needs a per-node status the scenario payload does not have yet.)
            reached = sorted(nx.descendants(dag, node) & set(order) | {node})
            raise ValueError(
                f"Cannot simulate this scenario: '{node}' lies on the path from "
                f"the intervention to the target, and its coefficient cannot be "
                f"estimated — {e} A constant series carries no information about "
                f"how its parents move it, so every downstream node "
                f"({', '.join(reached)}) would be simulated with that link "
                "missing. Widen the window so the node varies, or intervene "
                "somewhere that does not route through it."
            ) from e
        traces[(node, fit_end_key)] = fit

    _report(progress, stage="simulating", total=len(to_fit))

    # Draw coefficients and assumption effects up front, in a fixed order, so
    # identical calls consume the rng identically. Fitted mode indexes the
    # beta_raw posterior; cold-start mode samples the YAML prior directly in
    # business units (independent across parents and nodes — priors carry no
    # cross-coefficient correlation) with the analytic prior mean as the
    # point value.
    beta_draws: Dict[str, np.ndarray] = {}
    beta_means: Dict[str, np.ndarray] = {}
    # Resolution floor for `prob_direction`. Coefficient draws are *resampled
    # with replacement* from a fitted posterior, so drawing 2,000 of them from
    # a 500-draw ADVI fit adds no information about the sign: if all 500 are
    # positive, so are all 2,000. The direction probability can therefore not
    # resolve finer than the coarsest posterior behind any propagated
    # coefficient, and taking the minimum across the run under-claims where a
    # node happens to be driven by freshly sampled beliefs — which is the safe
    # side. Cold-start mode fits nothing and samples every belief at `n_draws`,
    # so it keeps `n_draws`.
    n_effective = n_draws
    for node in order:
        if node not in needs_beta:
            continue
        parents = list(dag.predecessors(node))
        if cold_start:
            defn = dag.nodes[node]["definition"]
            priors = [defn.priors.get(p) or defn.priors.get("coefficient") for p in parents]
            beta_draws[node] = np.column_stack([_sample_prior(pr, n_draws, rng) for pr in priors])
            beta_means[node] = np.array([_prior_mean(pr) for pr in priors])
        else:
            arr = (
                traces[(node, fit_end_key)]
                .trace.posterior["beta_raw"]
                .values.reshape(-1, len(parents))
            )
            beta_draws[node] = arr[rng.choice(arr.shape[0], size=n_draws)]
            beta_means[node] = arr.mean(axis=0)
            n_effective = min(n_effective, int(arr.shape[0]))

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

    source_ids = [f"i:{iv.metric}" for iv in scenario.interventions] + [a.id for a in assumptions]
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
            # from asserted ranges in cold-start mode), scaled to the edge's grain.
            s = edge_scale[(p, node)]
            return (base_draws[p] if use_draws else np.array([base_mu[p]])) * s

        deltas: Dict[str, np.ndarray] = {}
        for node in order:
            if node in intervened and f"i:{node}" in active:
                deltas[node] = (
                    tgt_delta_draws[node] if use_draws else np.array([tgt_delta_point[node]])
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
                shifted = {p: base[p] + deltas.get(p, 0.0) * edge_scale[(p, node)] for p in parents}
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
                math.factorial(r) * math.factorial(n_sources - r - 1) / math.factorial(n_sources)
            )
            for coalition in combinations(others, r):
                without = point_deltas(frozenset(coalition))
                with_sid = point_deltas(frozenset(coalition) | {sid})
                for node in order:
                    contributions[node][sid] += weight * float(with_sid[node][0] - without[node][0])

    # Honesty stats: full-history bands in fitted mode; declared `plausible`
    # bounds in cold-start mode (no bounds -> no flag, and the block says so
    # rather than inventing a band).
    hist_stats: Dict[str, Dict[str, Any]] = {}
    if cold_start:
        for n in dag.nodes:
            pl = dag.nodes[n]["definition"].plausible
            hist_stats[n] = {
                "plausible_min": pl.min if pl is not None else None,
                "plausible_max": pl.max if pl is not None else None,
            }
    else:
        for n in dag.nodes:
            vals = data.series(n)[n].to_numpy(dtype=float)
            # A metric can be genuinely undefined for part of its history — a
            # per-cancellation average in a week with no cancellations, any
            # ratio before its denominator exists. Reduce nan-safely so those
            # periods are skipped rather than poisoning the band (and, since
            # NaN is not JSON, turning the whole response into a 500).
            observed = vals[~np.isnan(vals)]
            if observed.size == 0:
                hist_stats[n] = {
                    "hist_min": None,
                    "hist_max": None,
                    "hist_mean": None,
                    "hist_std": None,
                }
                continue
            hist_stats[n] = {
                "hist_min": float(np.min(observed)),
                "hist_max": float(np.max(observed)),
                "hist_mean": float(np.mean(observed)),
                "hist_std": float(np.std(observed)),
            }

    nodes_out: Dict[str, Any] = {}
    for node in dag.nodes:
        base = base_mu[node]
        hist = hist_stats[node]
        baseline_ci = None
        if cold_start and float(base_draws[node].std()) > 0:
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
                "khat_status": None,
                "khat_borderline": None,
                "khat_warnings": None,
                "collinearity_status": None,
                "collinearity_warnings": None,
                "extrapolation": {"flag": False, **hist},
                # An unaffected node's simulated value *is* its baseline, so
                # this flag would be a statement about the loaded data rather
                # than about the scenario. The data side is checked once at
                # load (`api/main._check_declared_shares`), where it belongs.
                "non_physical": False,
                "contributions": [],
            }
            if cold_start:
                entry["baseline_ci_95"] = baseline_ci
            if window_basis.get(node) is not None:
                entry["window_aggregate"] = window_basis[node]
                entry["window_aggregate_reason"] = window_basis_reason[node]
            nodes_out[node] = entry
            continue

        d = mc[node]
        estimate = float(d.mean())
        simulated = base + estimate
        flag = False
        if cold_start:
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
        elif hist["hist_min"] is None:
            # Never observed (every period undefined): no band to be outside of.
            # Say nothing rather than invent a range.
            pass
        else:
            outside_range = simulated < hist["hist_min"] or simulated > hist["hist_max"]
            outside_band = (
                hist["hist_std"] > 0 and abs(simulated - hist["hist_mean"]) > 2 * hist["hist_std"]
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

        impossible = _non_physical_warning(
            node,
            dag.nodes[node]["definition"],
            simulated,
            None if cold_start else hist,
        )
        if impossible is not None:
            warnings.append(impossible)

        fit_quality = None
        khat_status = None
        khat_borderline = None
        khat_warnings = None
        collinearity_status = None
        collinearity_warnings = None
        if not cold_start and node in needs_beta:
            dx = traces[(node, fit_end_key)].diagnostics
            fit_quality = dx.get("fit_quality")
            # Roadmap S2's verdict on the approximation this node's slope came
            # from, and null on the NUTS default (NUTS is not an
            # approximation). See `_node_out` in rca.py for the vocabulary; the
            # numeric k-hat stays in the fit's own diagnostics
            # (GET /metrics/{name}) rather than on every scenario node, because
            # what a scenario reader needs is the verdict.
            khat_status = dx.get("khat_status")
            # And whether that verdict is one the estimate can support
            # (roadmap S22). This is part of the verdict rather than a number
            # beside it, which is why `khat_borderline` crosses here and
            # `khat_se` — the number itself — does not.
            khat_borderline = dx.get("khat_borderline")
            khat_warnings = dx.get("khat_warnings")
            # Roadmap S4, beside k-hat and for the reason the four rules
            # exist: a disclosure that rides the RCA node and not its what-if
            # neighbour is one policy applied twice with opposite answers. It
            # matters at least as much here — an intervention on one member of
            # a collinear pair moves the child through a coefficient the data
            # barely separates from its twin's, so the scenario's magnitude is
            # soft in a way its interval alone does not say. Same shape as
            # k-hat: the verdict and its sentences, with the numbers left on
            # the fit (`GET /metrics/{name}`), because what a scenario reader
            # needs is the verdict.
            collinearity_status = dx.get("collinearity_status")
            collinearity_warnings = dx.get("collinearity_warnings")

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
            # Same estimator, same resolution ceiling as RCA's
            # `prob_same_direction`: a proportion over `n_draws` draws has
            # nothing between 1 − 1/n and 1, so a saturated Monte Carlo
            # publishes the ceiling and flags itself censored. A propagation
            # with no spread at all — an exact identity downstream of a pinned
            # intervention — is not an estimate and keeps its honest 1.0; the
            # shared helper draws that line.
            **direction_fields(d, key="prob_direction", n_effective=n_effective),
            "fit_quality": fit_quality,
            "khat_status": khat_status,
            "khat_borderline": khat_borderline,
            "khat_warnings": khat_warnings,
            "collinearity_status": collinearity_status,
            "collinearity_warnings": collinearity_warnings,
            "extrapolation": {"flag": bool(flag), **hist},
            # Per node, beside the per-node `extrapolation` flag, because the
            # two are different claims and the surfaces that render one must be
            # able to render the other. The sentence lives once, in `warnings`
            # (keyed by `metric`), rather than being copied here to drift.
            "non_physical": impossible is not None,
            "contributions": contribs,
        }
        if cold_start:
            entry["baseline_ci_95"] = baseline_ci
        if window_basis.get(node) is not None:
            entry["window_aggregate"] = window_basis[node]
            entry["window_aggregate_reason"] = window_basis_reason[node]
        nodes_out[node] = entry

    _refuse_non_finite(nodes_out)

    sources = [
        {"id": f"i:{iv.metric}", "kind": "intervention", "label": _intervention_label(iv)}
        for iv in scenario.interventions
    ] + [
        {"id": a.id, "kind": "assumption", "label": f"{a.source} → {a.target}", "note": a.note}
        for a in assumptions
    ]

    result = {
        "mode": "cold_start" if cold_start else "fitted",
        "baseline_window": (
            None if cold_start else {"start": scenario.baseline_start, "end": scenario.baseline_end}
        ),
        "n_draws": n_draws,
        "seed": 0,
        "sources": sources,
        "nodes": nodes_out,
        "warnings": warnings,
        "caveats": COLD_START_CAVEATS if cold_start else CAVEATS,
    }
    return result


def _baseline_draws(name: str, defn, rng: np.random.Generator, n_draws: int) -> np.ndarray:
    """Sample a declared baseline belief, truncated to its `plausible` bounds.

    Three shapes (roadmap C7a/C7b):

    - a point belief (`low == high`) is a point mass, whatever the
      distribution — nothing to sample, nothing to truncate;
    - `Normal` reads `[low, high]` as the central 90% interval, as before;
    - `LogNormal` reads the same interval on the log scale — the natural
      elicitation for an order-of-magnitude belief about a positive quantity,
      with support excluding zero by construction.

    Truncation is **rejection resampling**, not clipping: clipping piles a
    point mass on the bound, which turns "customers cannot be negative" into
    "there is a 1% chance of exactly zero customers" — a different belief the
    author never stated. Rejection keeps the declared shape inside the bounds.
    A belief whose mass lies almost entirely outside its own plausible range
    cannot be resampled honestly, so it raises with both declarations named —
    the two came from the same author, and only the author knows which one is
    wrong.
    """
    b = defn.baseline
    if b.is_point:
        return np.full(n_draws, b.low)
    if b.distribution == "LogNormal":
        mu_log = (math.log(b.low) + math.log(b.high)) / 2.0
        sigma_log = (math.log(b.high) - math.log(b.low)) / (2.0 * _Z90)

        def sample(k: int) -> np.ndarray:
            return rng.lognormal(mu_log, sigma_log, k)
    else:
        sigma = (b.high - b.low) / (2.0 * _Z90)

        def sample(k: int) -> np.ndarray:
            return rng.normal(b.mu, sigma, k)

    draws = sample(n_draws)
    pl = defn.plausible
    if pl is None:
        return draws
    lo = pl.min if pl.min is not None else -math.inf
    hi = pl.max if pl.max is not None else math.inf
    for _ in range(1000):
        bad = (draws < lo) | (draws > hi)
        n_bad = int(bad.sum())
        if n_bad == 0:
            return draws
        draws[bad] = sample(n_bad)
    raise ValueError(
        f"Cold-start metric '{name}': the declared baseline "
        f"[{b.low}, {b.high}] ({b.distribution}) places almost all of its mass "
        f"outside the declared plausible range [{pl.min}, {pl.max}] — "
        "truncated resampling cannot converge. The two declarations "
        "contradict each other; reconcile them."
    )


def _refuse_non_finite(nodes_out: Dict[str, Any]) -> None:
    """Refuse a scenario whose arithmetic produced a non-finite number.

    Rule 3: no engine result reaches an encoder unsanitized. `slices.py`
    filters non-finite values before the encoder and `rca.py` learned the same
    lesson as C17; this surface never did (the 2026-08-12 review's L4, shipped
    as roadmap C25b) — so a NaN or inf here met Starlette's `allow_nan=False`
    as an unhandled 500, and over MCP `round_floats` would have turned it into
    `null`, a simulation of nothing. RCA degrades per node because a tree RCA
    has independent nodes to save; a scenario's deltas propagate, so one
    non-finite node means its whole downstream is contaminated and the honest
    unit of refusal is the scenario. Raises ValueError (the API's 422) naming
    the nodes, not a status — there is no partial result worth keeping.
    """

    def bad(value: Any) -> bool:
        if isinstance(value, float):
            return not math.isfinite(value)
        if isinstance(value, dict):
            return any(bad(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(bad(v) for v in value)
        return False

    offending = sorted(name for name, entry in nodes_out.items() if bad(entry))
    if offending:
        raise ValueError(
            "Scenario produced non-finite results for: "
            + ", ".join(offending)
            + ". This usually means a zero denominator inside a formula over "
            "the baseline window, or (cold start) baseline draws crossing zero "
            "on a ratio's denominator — declare tighter `baseline`/`plausible` "
            "bounds, or choose a baseline window with defined values."
        )

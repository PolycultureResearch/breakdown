"""MCP server: breakdown's engine as tools for AI assistants.

Mounted on the FastAPI app at /mcp (streamable HTTP), so tools read the
same app.state the endpoints do — one process, one warm trace cache, no
state of their own. Heavy engine calls follow the endpoint concurrency
pattern exactly: serialized under app.state.lock, off the event loop via
asyncio.to_thread.
"""

import asyncio
import functools
import math
from typing import Any, Callable, Dict, List, Optional, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from breakdown.data_fetch import SliceNotSupported
from breakdown.engine.rca import run_rca as _engine_run_rca
from breakdown.engine.simulate import Assumption, Intervention, ScenarioRequest, run_scenario
from breakdown.mcp.shaping import (
    RCA_HOW_TO_READ,
    SLICE_HOW_TO_READ,
    compact_rca,
    compact_scenario,
    compact_slice,
    metric_link,
    rca_link,
    round_floats,
    whatif_how_to_read,
    whatif_link,
)

_RECENT_PERIODS = 8

mcp = MCPServer(
    "breakdown",
    instructions=(
        "breakdown is a Bayesian metric-tree engine for root-cause analysis and "
        "what-if simulation over a business's metric DAG. A server may hold "
        "several trees, each a different lens on the same business — a wide one "
        "with revenue at the top, a team's (marketing channels and campaigns), a "
        "product area's (feature adoption and retention), one standing behind a "
        "specific goal — so call list_trees first when the question is about a "
        "particular area, and pass the tree id to every other tool. "
        "Call get_tree first to "
        "learn the metrics, their grains, and the loaded data window; then run_rca "
        "to explain why a metric moved, slice_metric to localize a gap within a "
        "metric's declared dimensions (geo, plan, …), or run_whatif to simulate "
        "an intervention. "
        "Every analysis response includes a how_to_read block — follow it when "
        "narrating results, and include the report_url so users can open the "
        "interactive analysis."
    ),
)


_ToolFn = TypeVar("_ToolFn", bound=Callable[..., Any])

#: The exceptions that mean "the caller asked for something this tree cannot
#: answer", as opposed to "breakdown broke". Not a new judgement: it is the one
#: `api/main.py` already makes one file over, where exactly these two become a
#: 422 carrying `str(e)` and everything else becomes a 500.
_REFUSALS = (ValueError, SliceNotSupported)


def _surface_refusals(fn: _ToolFn) -> _ToolFn:
    """Re-raise a deliberate refusal as the SDK's *anticipated*-failure type.

    An MCP tool has two ways to fail and the SDK tells them apart by exception
    type. `ToolError` means "I saw this coming": the caller gets `isError` with
    the message text, which is the whole point of writing messages that name the
    offending value and the remedy. Anything else is a crash: since **mcp
    2.1.0** the caller gets only `Error executing tool <name>` and the text
    stays in the server log. mcp 2.0.0 forwarded every exception's text, so
    every refusal here read as anticipated by accident, and the hardening turned
    six carefully-worded refusals opaque at once.

    Opaque is worse here than at an HTTP boundary because the caller is a model.
    A person reading `Error executing tool run_rca` opens the log; a model has
    no log, so it cannot recover, cannot explain, and cannot stop — it guesses.
    That is the failure mode this project exists to avoid, and it is the same
    instinct as the provider-boundary rule: refuse, and say what was refused.

    Guards in this module raise `ToolError` directly, at the point they decide.
    This wrapper covers what is raised deeper — the engine's window and scenario
    validation, a provider that cannot slice — where the raise site knows
    nothing about MCP and shouldn't.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except _REFUSALS as e:
            raise ToolError(str(e)) from e

    # `tests/test_project_invariants.py` enumerates the `@mcp.tool()`
    # decorations and requires this one beside each: a seventh tool added
    # without it is a refusal the caller will never see.
    return wrapper  # type: ignore[return-value]


async def _state(tree: Optional[str] = None):
    """The `TreeState` a tool addresses: the named tree, else the default.

    Every tool takes an optional `tree`, so an analyst asking "why did paid
    signups stall" can call `list_trees`, find the tree that models paid
    acquisition, and stay in it. Omitting it is the default tree, which is what
    keeps every existing client working. Loads the tree's data if this is the
    first call that needs it — the tools are the one caller with no page to
    show a `loading` state to.
    """
    # Imported lazily: breakdown.api.main imports this module to mount the
    # MCP app, so a module-level import back would be a cycle.
    from breakdown.api.main import _ensure_loaded, app

    state = app.state
    trees = state.trees
    if not trees:
        raise ToolError(
            f"breakdown started without a metric tree: {state.startup_error}. "
            "Check the --tree path and restart."
        )
    tree_id = tree or state.default_tree
    tree_state = trees.get(tree_id)
    if tree_state is None:
        raise ToolError(
            f"No tree '{tree_id}'. Known trees: {', '.join(sorted(trees))}. "
            "Call list_trees for their titles and goals."
        )
    await _ensure_loaded(tree_state)
    if tree_state.load_error is not None:
        raise ToolError(
            f"Tree '{tree_state.id}' started without data: {tree_state.load_error}. "
            f"Run `breakdown doctor --tree {tree_state.path}` to diagnose."
        )
    return tree_state


def _known_metric(state, name: str) -> None:
    if name not in state.parser.dag:
        known = ", ".join(sorted(state.parser.dag.nodes))
        raise ToolError(f"Metric '{name}' not found. Known metrics: {known}")


def _require_data(state) -> None:
    """Mirror of the API's cold-start guard: analyses that consume history
    cannot exist on a tree that declares no data provider."""
    if state.data is None:
        raise ToolError(
            "This tree declares no data provider (cold start mode); this tool "
            "needs time-series data. run_whatif works — it simulates over the "
            "tree's declared beliefs."
        )


@mcp.tool()
@_surface_refusals
async def list_trees() -> Dict[str, Any]:
    """List every metric tree this server holds: id, title, owner, and (when
    it declares them) a period and a goal. Trees are different lenses on the
    same business — a wide revenue tree, a marketing tree detailing channels, a
    product tree about feature adoption, a tree standing behind a target — and
    any of them may be long-lived or short-lived. Call this first whenever the
    question points at a particular area or target rather than the business as
    a whole, then pass the `id` as the `tree` argument to get_tree, run_rca,
    slice_metric or run_whatif.

    Answers from parsed YAML alone and never loads data, so it is instant.
    `state` is `loaded` | `not_loaded` | `loading` | `error`: `not_loaded`
    means nobody has opened that tree in this process yet, which is why its
    `progress` is null — it is "we haven't looked", not zero. A tree with no
    declared goal has no `progress` at all, which is normal rather than a gap.
    Any tool call naming the tree loads it."""
    from breakdown.api.main import _tree_card, app

    state = app.state
    if not state.trees:
        raise ToolError(f"breakdown started without a metric tree: {state.startup_error}.")
    return round_floats(
        {
            "default": state.default_tree,
            "trees": [_tree_card(t) for t in state.trees.values()],
        }
    )


@mcp.tool()
@_surface_refusals
async def get_tree(tree: Optional[str] = None) -> Dict[str, Any]:
    """Get the metric tree: every metric with its grain, kind, parents, and
    formula, plus the DAG edges and the loaded data window. Call this first,
    before run_rca or run_whatif, to learn valid metric names and dates.
    `mode` is "fitted" (data-backed) or "cold_start" (the tree declares no
    data provider: only run_whatif applies, simulating over declared
    beliefs — asserted baselines and YAML priors — with no dates involved).
    Kinds: 'flow' metrics sum over time, 'stock' metrics take the last value,
    'rate' metrics are recomputed from components. Formula metrics decompose
    exactly (Shapley); metrics with parents but no formula are learned
    probabilistic relationships (Bayesian time-series regression).

    `tree` names which metric tree to read when the server holds more than one
    (see list_trees); omit it for the default tree."""
    state = await _state(tree)
    parser, data = state.parser, state.data
    metrics: List[Dict[str, Any]] = []
    for m in parser.config.metrics:
        entry: Dict[str, Any] = {"name": m.name, "grain": m.grain, "kind": m.kind}
        if m.parents:
            entry["parents"] = m.parents
        if m.formula:
            entry["formula"] = m.formula
        if m.description:
            entry["description"] = m.description
        if m.lags:
            entry["lags"] = m.lags
        if m.dimensions:
            entry["dimensions"] = sorted(m.dimensions)
        if m.baseline is not None:
            entry["baseline"] = {"low": m.baseline.low, "high": m.baseline.high}
        if data is not None:
            through = data.data_through(m.name)
            if through is not None:
                entry["data_through"] = str(through.date())
        metrics.append(entry)
    return {
        "mode": "cold_start" if data is None else "fitted",
        "tree": state.id,
        "title": state.title,
        "provider": parser.config.provider.type,
        "date_start": None if data is None else str(data.date_start.date()),
        "date_end": None if data is None else str(data.date_end.date()),
        "metrics": metrics,
        "edges": [list(e) for e in parser.dag.edges()],
    }


@mcp.tool()
@_surface_refusals
async def explain_metric(name: str, tree: Optional[str] = None) -> Dict[str, Any]:
    """Explain one metric: its definition, place in the tree (parents and
    children), a summary of its recent series, and whether a Bayesian fit is
    cached for it. Use this to ground a narrative about a specific metric or
    to sanity-check names and date coverage before an analysis. `tree` names
    which metric tree the metric belongs to (see list_trees); omit it for the
    default tree."""
    state = await _state(tree)
    _known_metric(state, name)
    parser, data = state.parser, state.data
    metric = parser.dag.nodes[name]["definition"]

    definition: Dict[str, Any] = {"name": metric.name, "grain": metric.grain, "kind": metric.kind}
    if metric.description:
        definition["description"] = metric.description
    if metric.formula:
        definition["formula"] = metric.formula
    if metric.parents:
        definition["parents"] = metric.parents
    if metric.lags:
        definition["lags"] = metric.lags
    if metric.dimensions:
        definition["dimensions"] = sorted(metric.dimensions)
    if metric.baseline is not None:
        definition["baseline"] = {"low": metric.baseline.low, "high": metric.baseline.high}
    if metric.plausible is not None:
        definition["plausible"] = {"min": metric.plausible.min, "max": metric.plausible.max}
    # Present only when declared, like everything else here — and worth the
    # tokens because it is decision-relevant *before* a scenario runs: an agent
    # that knows this node is a proportion can pick a lever that stays inside
    # [0, 1] rather than proposing one `run_whatif` will call impossible.
    if metric.share is not None:
        definition["share"] = metric.share

    if data is None:
        # Cold-start tree: no series exists; the asserted baseline above is
        # the metric's operating point.
        series_summary = None
    else:
        s = data.series(name)
        values = [
            None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
            for v in s[name].tolist()
        ]
        finite = [v for v in values if v is not None]
        recent = [
            {"date": str(d.date()), "value": v}
            for d, v in list(zip(s["date"], values))[-_RECENT_PERIODS:]
        ]
        series_summary = {
            "grain": data.grain_of[name],
            "n_periods": len(values),
            "mean": sum(finite) / len(finite) if finite else None,
            "min": min(finite) if finite else None,
            "max": max(finite) if finite else None,
            "recent": recent,
        }

    from breakdown.api.main import _pick_fit

    fit = _pick_fit(state.traces, name)
    fit_info: Dict[str, Any] = {"fitted": fit is not None}
    if fit is not None:
        fit_info["inference_method"] = fit.inference_method
        fit_info["fit_quality"] = fit.diagnostics.get("fit_quality")
        # Roadmap S2: the PSIS verdict on a variational fit, and null on the
        # NUTS default. Carried here as well as on RCA nodes because
        # `explain_metric` is where an agent goes to decide whether one
        # metric's numbers are worth narrating, and `fit_quality: "suspect"`
        # alone does not say whether the model failed to converge or converged
        # somewhere far from the posterior. A cached fit may have come from any
        # route, including `POST /analyze?inference_method=advi`, so this
        # reports what is actually in the cache rather than assuming.
        fit_info["khat"] = fit.diagnostics.get("khat")
        fit_info["khat_status"] = fit.diagnostics.get("khat_status")
        fit_info["khat_warnings"] = fit.diagnostics.get("khat_warnings")
        fit_info["sign_warnings"] = fit.diagnostics.get("sign_warnings")

    return round_floats(
        {
            "definition": definition,
            "children": list(parser.dag.successors(name)),
            "series_summary": series_summary,
            "fit": fit_info,
            "report_url": metric_link(name, tree=state.id),
        }
    )


@mcp.tool()
@_surface_refusals
async def run_rca(
    target: str,
    analysis_start: str,
    analysis_end: str,
    reference_start: Optional[str] = None,
    reference_end: Optional[str] = None,
    tree: Optional[str] = None,
) -> Dict[str, Any]:
    """Root-cause analysis: explain why `target` moved between a reference
    (baseline) window and an analysis window. Returns per-node gaps with
    attributed contributions (Shapley for formula nodes, posterior for
    learned edges), 95% credible intervals, an `unexplained` remainder, and
    a ranked triage list — plus a how_to_read guide and a report_url deep
    link to the interactive analysis.

    The model always trains on **all loaded history before
    `analysis_start`** — the reference window is *not* the training window,
    only the comparison baseline the gap is measured against. **Usually omit
    `reference_start`/`reference_end`**: the engine defaults to the matched
    adjacent block — ~4× the analysis length (min 28 days, whole weeks when
    seasonality is in scope), ending the day before `analysis_start`. The
    response's `reference_window` and `reference_defaulted` say what was
    used. Override only for a deliberate baseline (e.g. the same fiscal
    period a quarter earlier) — and a non-adjacent reference absorbs
    underlying trend into the comparison on a growing metric, so say so when
    you narrate one.

    Dates are YYYY-MM-DD, inclusive, and must lie inside the loaded data
    window (see get_tree); the analysis window cannot start on the first
    loaded day when the reference is omitted (no room for a baseline).
    Windows snap to whole periods at each node's grain. Analysis window =
    the period in question. When you do pass an explicit reference shorter
    than a week, compare like with like: cover the same days of the week —
    a weekend vs. the prior weekend — or the gap will be dominated by
    weekday-mix seasonality rather than anything actionable. Trees with
    monthly metrics need windows covering whole months. The first call fits
    models on demand with exact MCMC and can take several minutes on a wide
    or day-grain tree; repeat calls on the same tree and window are fast
    (fits are cached). There is deliberately no fast-approximation switch
    here — the HTTP route has one, driven by a human who can see what it
    costs (see docs/mcp.md).

    Follow-up: when a top cause declares dimensions (see get_tree), call
    slice_metric on it to localize the gap within the metric — reuse the
    resolved windows from this response. For a lagged edge, the
    contribution's `parent_windows` are the windows to reuse.

    `tree` names which metric tree to analyse when the server holds more than
    one (see list_trees); omit it for the default tree. Metric names are
    per-tree — two trees naming the same metric are two independent nodes."""
    state = await _state(tree)
    _require_data(state)
    _known_metric(state, target)
    async with state.lock:
        result = await asyncio.to_thread(
            _engine_run_rca,
            state.parser.dag,
            state.data,
            state.traces,
            target,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            reference_start=reference_start,
            reference_end=reference_end,
        )
    out = round_floats(compact_rca(result))
    out["how_to_read"] = RCA_HOW_TO_READ
    # Deep link from the *resolved* windows, so a defaulted reference replays
    # identically even if the server later boots with a different data range.
    out["report_url"] = rca_link(
        target,
        result["reference_window"]["start"],
        result["reference_window"]["end"],
        analysis_start,
        analysis_end,
        tree=state.id,
    )
    return out


@mcp.tool()
@_surface_refusals
async def slice_metric(
    name: str,
    dimension: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    tree: Optional[str] = None,
) -> Dict[str, Any]:
    """Localize a metric's window-over-window gap within one of its declared
    dimensions (geo, plan tier, app version, …): the traverse-then-slice
    follow-up to run_rca. Tree RCA says which upstream metric moved; this
    says where inside it — which slices carry more of the gap than their
    size predicts (`excess`), with credible intervals from a window
    bootstrap. Flows/stocks decompose exactly as sums over slices; rates
    split each slice into `within` (its own rate moved) and `mix` (traffic
    shifted between slices).

    `dimension` must be declared on the metric (get_tree lists each metric's
    `dimensions`). Dates are YYYY-MM-DD, inclusive, snapped to whole periods
    at the metric's grain. Unlike run_rca, all four dates are required here:
    pass the resolved windows from the run_rca response that pointed here
    (its `reference_window`/`analysis_window`), and for a lagged parent use
    the `parent_windows` its run_rca contribution carries. Slices are
    fetched on demand from the provider; the first call for a (metric,
    dimension, window) queries the data source, repeats are cached. `tree`
    names which metric tree the metric belongs to (see list_trees); omit it
    for the default tree."""
    state = await _state(tree)
    _require_data(state)
    _known_metric(state, name)
    defn = state.parser.dag.nodes[name]["definition"]
    if dimension not in defn.dimensions:
        raise ToolError(
            f"Metric '{name}' declares no dimension '{dimension}' "
            f"(declared: {sorted(defn.dimensions) or 'none'})."
        )

    from breakdown.api.main import _run_slice

    async with state.lock:
        result = await asyncio.to_thread(
            _run_slice,
            state,
            state.parser,
            state.data,
            defn,
            dimension,
            reference_start,
            reference_end,
            analysis_start,
            analysis_end,
        )
    out = round_floats(compact_slice(result))
    out["how_to_read"] = SLICE_HOW_TO_READ
    return out


@mcp.tool()
@_surface_refusals
async def run_whatif(
    baseline_start: Optional[str] = None,
    baseline_end: Optional[str] = None,
    interventions: Optional[List[Intervention]] = None,
    assumptions: Optional[List[Assumption]] = None,
    tree: Optional[str] = None,
) -> Dict[str, Any]:
    """What-if simulation: pin one or more metrics to hypothetical values
    (do-operator: the metric's own drivers are severed) and propagate the
    effects downstream through the tree. Returns a posterior distribution
    over each affected metric's change (estimate + 95% credible interval +
    probability of direction), extrapolation warnings, and per-source
    Shapley contributions — plus a how_to_read guide and a report_url.

    `interventions`: [{metric, mode, value}] where mode 'pct' is a relative
    change (0.10 = +10%), 'delta' an absolute change, 'set' an absolute
    level. `assumptions` assert an unmodeled edge: {source, target, effect:
    {kind: absolute|relative, low, high}} where [low, high] is your central
    90% belief about the effect on target. `baseline_start`/`baseline_end`
    (YYYY-MM-DD, inside the loaded data window — see get_tree) define the
    baseline the scenario is compared against; required on a fitted tree,
    and must be OMITTED on a cold-start tree (get_tree says `mode:
    "cold_start"`), where operating points come from the tree's declared
    baselines. The first call may fit models on demand and take a minute;
    repeat calls are fast (fits are cached). `tree` names which metric tree to
    simulate over when the server holds more than one (see list_trees); omit
    it for the default tree."""
    state = await _state(tree)
    scenario = ScenarioRequest(
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        interventions=interventions or [],
        assumptions=assumptions or [],
    )
    async with state.lock:
        result = await asyncio.to_thread(
            run_scenario, state.parser.dag, state.data, state.traces, scenario
        )
    out = round_floats(compact_scenario(result))
    out["how_to_read"] = whatif_how_to_read(result["mode"])
    out["report_url"] = whatif_link(scenario.model_dump(exclude_defaults=True), tree=state.id)
    return out

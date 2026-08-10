"""MCP server: breakdown's engine as tools for AI assistants.

Mounted on the FastAPI app at /mcp (streamable HTTP), so tools read the
same app.state the endpoints do — one process, one warm trace cache, no
state of their own. Heavy engine calls follow the endpoint concurrency
pattern exactly: serialized under app.state.lock, off the event loop via
asyncio.to_thread.
"""

import asyncio
import math
from typing import Any, Dict, List, Optional

from mcp.server import MCPServer

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
        "what-if simulation over a business's metric DAG. Call get_tree first to "
        "learn the metrics, their grains, and the loaded data window; then run_rca "
        "to explain why a metric moved, slice_metric to localize a gap within a "
        "metric's declared dimensions (geo, plan, …), or run_whatif to simulate "
        "an intervention. "
        "Every analysis response includes a how_to_read block — follow it when "
        "narrating results, and include the report_url so users can open the "
        "interactive analysis."
    ),
)


def _state():
    # Imported lazily: breakdown.api.main imports this module to mount the
    # MCP app, so a module-level import back would be a cycle.
    from breakdown.api.main import app

    if app.state.startup_error is not None:
        raise RuntimeError(
            f"breakdown started without data: {app.state.startup_error}. "
            "Run `breakdown doctor --tree <tree.yml>` to diagnose."
        )
    return app.state


def _known_metric(state, name: str) -> None:
    if name not in state.parser.dag:
        known = ", ".join(sorted(state.parser.dag.nodes))
        raise ValueError(f"Metric '{name}' not found. Known metrics: {known}")


def _require_data(state) -> None:
    """Mirror of the API's cold-start guard: analyses that consume history
    cannot exist on a tree that declares no data provider."""
    if state.data is None:
        raise RuntimeError(
            "This tree declares no data provider (cold start mode); this tool "
            "needs time-series data. run_whatif works — it simulates over the "
            "tree's declared beliefs."
        )


@mcp.tool()
async def get_tree() -> Dict[str, Any]:
    """Get the metric tree: every metric with its grain, kind, parents, and
    formula, plus the DAG edges and the loaded data window. Call this first,
    before run_rca or run_whatif, to learn valid metric names and dates.
    `mode` is "fitted" (data-backed) or "cold_start" (the tree declares no
    data provider: only run_whatif applies, simulating over declared
    beliefs — asserted baselines and YAML priors — with no dates involved).
    Kinds: 'flow' metrics sum over time, 'stock' metrics take the last value,
    'rate' metrics are recomputed from components. Formula metrics decompose
    exactly (Shapley); metrics with parents but no formula are learned
    probabilistic relationships (Bayesian time-series regression)."""
    state = _state()
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
        "provider": parser.config.provider.type,
        "date_start": None if data is None else str(data.date_start.date()),
        "date_end": None if data is None else str(data.date_end.date()),
        "metrics": metrics,
        "edges": [list(e) for e in parser.dag.edges()],
    }


@mcp.tool()
async def explain_metric(name: str) -> Dict[str, Any]:
    """Explain one metric: its definition, place in the tree (parents and
    children), a summary of its recent series, and whether a Bayesian fit is
    cached for it. Use this to ground a narrative about a specific metric or
    to sanity-check names and date coverage before an analysis."""
    state = _state()
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
        fit_info["sign_warnings"] = fit.diagnostics.get("sign_warnings")

    return round_floats(
        {
            "definition": definition,
            "children": list(parser.dag.successors(name)),
            "series_summary": series_summary,
            "fit": fit_info,
            "report_url": metric_link(name),
        }
    )


@mcp.tool()
async def run_rca(
    target: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
) -> Dict[str, Any]:
    """Root-cause analysis: explain why `target` moved between a reference
    (baseline) window and an analysis window. Returns per-node gaps with
    attributed contributions (Shapley for formula nodes, posterior for
    learned edges), 95% credible intervals, an `unexplained` remainder, and
    a ranked triage list — plus a how_to_read guide and a report_url deep
    link to the interactive analysis.

    Dates are YYYY-MM-DD, inclusive, and must lie inside the loaded data
    window (see get_tree). Windows snap to whole periods at each node's
    grain. Choosing windows from a phrase like 'last week': analysis window
    = the period in question; reference window = an equal-length window
    immediately before it (or a comparable earlier period). Prefer
    whole-week spans (7/14/28 days) so weekday mix doesn't manufacture
    seasonal gaps. For windows shorter than a week, compare like with
    like: pick a reference covering the same days of the week — a weekend
    vs. the prior weekend, a Monday vs. recent Mondays — or the gap will
    be dominated by weekday-mix seasonality rather than anything
    actionable. Trees with monthly metrics need windows covering whole
    months. The first call fits models on demand and can take a minute or
    two; repeat calls on the same tree are fast (fits are cached).

    Follow-up: when a top cause declares dimensions (see get_tree), call
    slice_metric on it to localize the gap within the metric. For a lagged
    edge, the contribution's `parent_windows` are the windows to reuse."""
    state = _state()
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
    out["report_url"] = rca_link(
        target, reference_start, reference_end, analysis_start, analysis_end
    )
    return out


@mcp.tool()
async def slice_metric(
    name: str,
    dimension: str,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
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
    at the metric's grain — normally the same windows as the run_rca that
    pointed here, but for a lagged parent use the `parent_windows` its
    run_rca contribution carries. Slices are fetched on demand from the
    provider; the first call for a (metric, dimension, window) queries the
    data source, repeats are cached."""
    state = _state()
    _require_data(state)
    _known_metric(state, name)
    defn = state.parser.dag.nodes[name]["definition"]
    if dimension not in defn.dimensions:
        raise ValueError(
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
async def run_whatif(
    baseline_start: Optional[str] = None,
    baseline_end: Optional[str] = None,
    interventions: Optional[List[Intervention]] = None,
    assumptions: Optional[List[Assumption]] = None,
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
    repeat calls are fast (fits are cached)."""
    state = _state()
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
    out["report_url"] = whatif_link(scenario.model_dump(exclude_defaults=True))
    return out

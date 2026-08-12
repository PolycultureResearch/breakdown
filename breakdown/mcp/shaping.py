"""Shape engine results for MCP tool responses.

LLM clients pay per token and narrate what they see, so responses are
compacted (rounded floats, decompositions dropped, null skeletons trimmed)
and every analysis carries a `how_to_read` block distilled from
docs/model.md — the interpretation caveats must be in-context at the moment
the model writes its story, not in a resource it may never fetch.
"""

import json
import math
import os
from typing import Any, Dict
from urllib.parse import quote

_SIG_FIGS = 4

# Cap ranked_causes: it is a triage order, and past the first handful the
# scores are noise a narrator should not be tempted to rank-order.
_MAX_RANKED_CAUSES = 10

RCA_HOW_TO_READ = (
    "How to read this result:\n"
    "- The metric tree is the analyst's causal hypothesis, not discovered causality; "
    "unmodeled factors land in `unexplained` or get misattributed.\n"
    "- `unexplained` is a first-class finding: when it is large, the honest story is "
    "'the modeled drivers don't account for this move' — do not force the gap onto the parents.\n"
    "- `ranked_causes` is a triage order ('look here first'), not a probability that a "
    "metric is the cause.\n"
    "- `share_of_gap` is unclamped: opposing parents can legitimately sum past 100% or go negative.\n"
    "- `ci_95` is a 95% credible interval; `prob_same_direction` near 1.0 means the sign is "
    "near-certain, near 0.5 means it could go either way. A null `ci_95` means the interval "
    "was honestly withheld (see `ci_status`), not that it is zero.\n"
    "- `gap` is mean-per-period at each node's own grain — never compare raw gaps across "
    "nodes with different grains; compare shares and ranked-cause scores instead.\n"
    "- A contribution carrying `lag`/`parent_windows` was measured against the parent's "
    "own earlier windows — the periods that actually influenced the child. Narrate the "
    "parent using *its* dates ('trial starts Jul 11-17 explain conversions Jul 25-31'), "
    "and reuse those windows for any follow-up analysis of that parent (drill-down "
    "run_rca, slice_metric).\n"
    "- `components` (trend/seasonal) are model structure, not causes: a seasonal gap from an "
    "uneven weekday mix is nobody's fault.\n"
    "- `sign_warnings` mean a fitted slope contradicts its declared sign, the classic mark of "
    "scale confounding — do not narrate that edge causally.\n"
    "- `seasonality_warnings` mean a declared seasonal component could not be identified "
    "from the fitted history (`fit_periods` says how much there was); its share of the gap "
    "may be misallocated between `components.seasonal`, trend, and `unexplained` — load "
    "more history rather than narrating that split as precise.\n"
    "- Fits use ADVI, which can understate uncertainty; treat this as triage and confirm "
    "load-bearing findings with a NUTS fit before big decisions."
)

SLICE_HOW_TO_READ = (
    "How to read this result:\n"
    "- Slices localize; the tree explains. A concentrated slice says where to "
    "look next, not why the metric moved — follow up with run_rca upstream or "
    "with domain facts.\n"
    "- `excess` is the localization signal: the slice's contribution minus its "
    "baseline share of the gap. The biggest slice usually has the biggest raw "
    "`contribution` — that alone is not news. Excesses sum to zero: "
    "concentration is a reallocation of the gap, not extra gap.\n"
    "- `prob_concentrated` near 1.0 means the concentration direction is "
    "near-certain; `noise_level: true` means the bootstrap cannot distinguish "
    "this slice from a proportional move — do not narrate it as localized.\n"
    "- `__other__` folds the non-top slices (`n_values` of them) and can "
    "itself be the finding — a long-tail move is real.\n"
    "- For rates: `within` is the slice's own rate moving, `mix` is traffic "
    "shifting between slices. `mix_total` is a composition effect — nobody's "
    "fault, and often the whole story.\n"
    "- `reconciliation.status: discrepant` means the slices do not sum/blend "
    "back to the metric: the dimension does not cleanly partition it. Treat "
    "attributions as approximate and say so.\n"
    "- When slicing a lagged parent surfaced by run_rca, use the parent's own "
    "lag-shifted windows: its run_rca contribution carries them as "
    "`parent_windows` — those are the periods that actually influenced the "
    "child."
)

WHATIF_HOW_TO_READ = (
    "How to read this result:\n"
    "- `delta` is a posterior over the change: an estimate with a 95% credible interval. "
    "`prob_direction` near 0.5 means the sign is genuinely uncertain — narrate the odds "
    "(e.g. 'a 20% chance this loses money'), not just the point estimate.\n"
    "- Interventions are do-operator: the metric is pinned, its usual drivers are severed, "
    "and effects propagate only downstream through the tree.\n"
    "- Fitted slopes are local to the observed operating range; `extrapolation: true` "
    "(detail in `warnings`) means the scenario leaves that range — call the result speculative.\n"
    "- Assumption edges are user-asserted beliefs sampled from the stated 90% range, not "
    "fitted from data; say so when they drive the answer.\n"
    "- Per-source `contributions` are exact Shapley shares and sum to the node's delta estimate.\n"
    "- The `caveats` list applies to every number here; weave it into the narrative rather "
    "than dropping it."
)

COLD_START_HOW_TO_READ = (
    '- This tree runs in COLD START mode (`mode: "cold_start"`): it has no data provider. '
    "Every baseline and slope is a stated belief (asserted operating points, YAML priors) — "
    "present results as consequences of the assumptions, never as evidence. "
    "`baseline_ci_95` is the belief interval around a node's operating point; extrapolation "
    "flags compare against the tree's declared `plausible` bounds, not history. "
    "The most useful narrative is sensitivity: which beliefs drive the answer, and are "
    "therefore worth measuring first."
)


def whatif_how_to_read(mode: str) -> str:
    """The what-if how_to_read block; cold-start results gain the caveat
    block that reframes every number as a stated belief."""
    if mode == "cold_start":
        return WHATIF_HOW_TO_READ + "\n" + COLD_START_HOW_TO_READ
    return WHATIF_HOW_TO_READ


def round_floats(obj: Any, sig: int = _SIG_FIGS) -> Any:
    """Recursively round floats to `sig` significant figures; non-finite -> None."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        if obj == 0.0:
            return 0.0
        return round(obj, -int(math.floor(math.log10(abs(obj)))) + (sig - 1))
    if isinstance(obj, dict):
        return {k: round_floats(v, sig) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, sig) for v in obj]
    return obj


def _base_url() -> str:
    url = os.environ.get("BREAKDOWN_PUBLIC_URL")
    if not url:
        url = f"http://127.0.0.1:{os.environ.get('BREAKDOWN_PORT', '9090')}"
    return url.rstrip("/")


def rca_link(target, reference_start, reference_end, analysis_start, analysis_end) -> str:
    """UI deep link replaying this exact RCA (the engine is seeded, so the
    link reproduces the numbers). Param names match applyDeepLink() in
    static/app.js."""
    return (
        f"{_base_url()}/ui/#rca={quote(target)}"
        f"&reference_start={reference_start}&reference_end={reference_end}"
        f"&analysis_start={analysis_start}&analysis_end={analysis_end}"
    )


def whatif_link(scenario: Dict[str, Any]) -> str:
    return f"{_base_url()}/ui/#whatif={quote(json.dumps(scenario, separators=(',', ':')))}"


def metric_link(name: str) -> str:
    return f"{_base_url()}/ui/#metric={quote(name)}"


def compact_rca(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact a run_rca result: drop per-contribution decompositions and
    effective-window detail, collapse components to point estimates, shrink
    skipped nodes to their status, and omit null node fields."""
    nodes: Dict[str, Any] = {}
    for name, node in result["nodes"].items():
        if node["status"] != "ok":
            nodes[name] = {"status": node["status"], "grain": node["grain"]}
            continue
        windows = node["effective_windows"]
        compact = {
            "status": node["status"],
            "grain": node["grain"],
            "n_periods": {
                "reference": windows["reference"]["n_periods"],
                "analysis": windows["analysis"]["n_periods"],
            },
            "baseline": node["baseline"],
            "actual": node["actual"],
            "gap": node["gap"],
            "relative_change": node["relative_change"],
            "attribution_method": node["attribution_method"],
            "fit_quality": node["fit_quality"],
            "sign_warnings": node["sign_warnings"],
            # fit_window start/end are dropped for token economy; n_periods is
            # the decision-relevant number.
            "fit_periods": (node.get("fit_window") or {}).get("n_periods"),
            "seasonality_warnings": node.get("seasonality_warnings"),
            "ci_status": node["ci_status"],
            "unexplained": node["unexplained"],
            "components": (
                {k: v["estimate"] for k, v in node["components"].items()}
                if node["components"]
                else None
            ),
            "interaction": node["interaction"],
            "contributions": [
                {
                    # ci_95: null is meaningful (withheld interval) and stays;
                    # lag/parent_windows appear only on lagged contributions.
                    **{
                        k: c.get(k)
                        for k in (
                            "parent",
                            "estimate",
                            "share_of_gap",
                            "ci_95",
                            "prob_same_direction",
                        )
                    },
                    **{k: c[k] for k in ("lag", "parent_windows") if c.get(k) is not None},
                }
                for c in node["contributions"]
            ],
        }
        # ci_95: null inside contributions is meaningful (withheld interval)
        # and stays; node-level nulls are just absent features.
        nodes[name] = {k: v for k, v in compact.items() if v is not None}
    return {
        "target": result["target"],
        "reference_window": result["reference_window"],
        "analysis_window": result["analysis_window"],
        "reference_defaulted": result.get("reference_defaulted"),
        "nodes": nodes,
        "ranked_causes": result["ranked_causes"][:_MAX_RANKED_CAUSES],
    }


def compact_slice(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact a slice_attribution result: window detail collapses to period
    counts, per-slice nulls are trimmed (a null CI on a single-period window
    is conveyed once by ci_status), empty caveats are dropped."""
    windows = result["effective_windows"]
    out = {
        "metric": result["metric"],
        "dimension": result["dimension"],
        "grain": result["grain"],
        "kind": result["kind"],
        "n_periods": {
            "reference": windows["reference"]["n_periods"],
            "analysis": windows["analysis"]["n_periods"],
        },
        "baseline": result["baseline"],
        "actual": result["actual"],
        "gap": result["gap"],
        "attribution_method": result["attribution_method"],
        "mix_total": result["mix_total"],
        "slices": [{k: v for k, v in row.items() if v is not None} for row in result["slices"]],
        "reconciliation": {
            "status": result["reconciliation"]["status"],
            "residual_share_of_baseline": result["reconciliation"]["residual_share_of_baseline"],
        },
        "ci_status": result["ci_status"],
        "caveats": result["caveats"] or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def compact_scenario(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact a run_scenario result: unaffected nodes shrink to their
    baseline, extrapolation stats collapse to the flag (detail already
    lives in `warnings`)."""
    nodes: Dict[str, Any] = {}
    for name, node in result["nodes"].items():
        if node["status"] == "baseline":
            nodes[name] = {"status": "baseline", "baseline": node["baseline"]}
        else:
            nodes[name] = {
                "status": node["status"],
                "baseline": node["baseline"],
                "simulated": node["simulated"],
                "delta": node["delta"],
                "relative_delta": node["relative_delta"],
                "prob_direction": node["prob_direction"],
                "fit_quality": node["fit_quality"],
                "extrapolation": node["extrapolation"]["flag"],
                "contributions": node["contributions"],
            }
        # Cold-start results carry the belief interval around each operating
        # point; a null (point baseline) stays omitted like other null fields.
        if node.get("baseline_ci_95") is not None:
            nodes[name]["baseline_ci_95"] = node["baseline_ci_95"]
    return {
        "mode": result["mode"],
        "baseline_window": result["baseline_window"],
        "n_draws": result["n_draws"],
        "sources": result["sources"],
        "nodes": nodes,
        "warnings": result["warnings"],
        "caveats": result["caveats"],
    }

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
from typing import Any, Dict, Optional
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
    "- `unexplained_status`: `measured` (the node's own fetched series was compared, so zero "
    "means it reconciled) or `definitional` (the node is derived — its series *is* the "
    "formula — so zero means nothing was checked; never narrate that as 'the identity "
    "holds').\n"
    "- `window_aggregate` (rate nodes only): `components` = the window value is "
    "Σnumerator / Σdenominator; any `period_mean_*` = the mean of the per-period ratios "
    "instead, with the why: `_none_exists` (no denominator exists — a median, say; that "
    "mean is the only number there is, not a misconfiguration), `_undeclared` (nobody has "
    "declared one), `_weights_unavailable` (declared, unusable here). "
    "`window_aggregate_reason` gives the author's words.\n"
    "- `ranked_causes` is a triage order ('look here first'), not a probability that a "
    "metric is the cause.\n"
    "- `share_of_gap` is unclamped: opposing parents can legitimately sum past 100% or go negative.\n"
    "- `ci_95` is a 95% credible interval; `prob_same_direction` near 1.0 means the sign is "
    "near-certain, near 0.5 means it could go either way. A null `ci_95` means the interval "
    "was honestly withheld (see `ci_status`), not that it is zero. `*_censored: true` means "
    "it saturated at the estimator's ceiling — a bound ('>99.8%'), not certainty.\n"
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
    "- `collinearity_status` says whether that node's parents move together over the window it "
    "was fitted on: `high` = the split of the gap between them is not a determined quantity, "
    "`moderate` = their total is sound and the split is soft, `ok` = checked and separable, "
    "`unavailable` = could not be checked (unchecked, not clean), absent = nothing to check. "
    "On `moderate` or `high`, `collinearity_warnings` names the parents: narrate them as one "
    "cause and do not rank them against each other — that ordering is the part the data does "
    "not support, however different their `share_of_gap` look.\n"
    "- `ppc_status` says whether the node's fitted model can generate the data it was fitted "
    "on: `severe` = it cannot, so that node's coefficients and share of the gap rest on a "
    "likelihood the data argues against — narrate the direction, not the magnitude, and name "
    "the model as what needs fixing. `moderate` = a caveat, not a verdict. `ok` = checked and "
    "it reproduces its data, `unavailable` = unchecked (not clean), absent = nothing to "
    "check. `ppc_warnings` names the failing statistic. `severe` also sets `fit_quality: "
    "suspect`, and on a NUTS fit is the only thing that can have.\n"
    "- `seasonality_warnings` mean a declared seasonal component could not be identified "
    "from the fitted history (`fit_periods` says how much there was); its share of the gap "
    "may be misallocated between `components.seasonal`, trend, and `unexplained` — load "
    "more history rather than narrating that split as precise.\n"
    "- A node whose `status` is not `ok` was **not analyzed** — never narrate it as a "
    "metric that held steady; say it is a gap in the analysis and give `status_reason`. "
    "An `attribution_failed` node still reports a real `gap`: the move is measured, only "
    "the split is missing. Nothing upstream of such a node is attributed either, so "
    "`ranked_causes` is incomplete whenever one is present.\n"
    "- Every fitted node ran exact MCMC (NUTS) unless it carries `inference_method: advi`, "
    "which appears only when the caller asked for the fast approximation. A node with no "
    "`inference_method` and no `khat_status` was sampled exactly — that absence is not a "
    "missing check. Where `advi` does appear, `khat_status` says how far it landed from the "
    "true posterior (PSIS k̂). `unusable` = not close and not correctable: "
    "its `ci_95` is not evidence about the real interval, so narrate the point estimate as "
    "provisional and the uncertainty as unmeasured — never as precise, and say the analysis "
    "should be re-run without the approximation. `suspect` = measurably off; approximate. "
    "`unavailable` = unchecked, which is not clean. `khat_warnings` gives the reason and the "
    "remedy worth naming. k̂ is itself an estimate: `khat_se` is its Monte-Carlo standard "
    "error, and `khat_borderline: true` means k̂ is within one of those of a band edge, so "
    "the band beside it is the side it happened to land on rather than a settled verdict — "
    "narrate the worse of the two adjacent bands, and say an exact re-fit is what resolves it."
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
    "this slice from a proportional move — do not narrate it as localized. "
    "`prob_concentrated_censored: true` means the estimate saturated at the "
    "bootstrap's resolution ceiling — narrate it as a bound ('>99.8%'), not a "
    "measured value.\n"
    "- `__other__` folds the non-top slices (`n_values` of them) and can "
    "itself be the finding — a long-tail move is real. It is still not a "
    "segment: when it tops the ranking the verdict is `long_tail`, below.\n"
    "- For rates: `within` is the slice's own rate moving, `mix` is traffic "
    "shifting between slices. `mix_total` is a composition effect — nobody's "
    "fault, and often the whole story.\n"
    "- `localization` is the verdict, in three states. `localized`: the leader "
    "clears `localization_threshold` of the gap with its evidence intact — "
    "name it. `not_localized`: no slice carries enough of the gap beyond its "
    "own size (the leader's `excess` is below the threshold, or it is "
    "noise-level) — narrate the gap as spread across slices, and do not name "
    "the top slice as the cause. `long_tail`: the leader is `__other__`, the "
    "roll-up of the values outside `top_k` — the tail really did move, but "
    "there is no segment to act on, so say the concentration is in the long "
    "tail and hand back `localization_remedy`, which names what to change "
    "to see inside it. Never narrate `long_tail` by naming `__other__` as the "
    "culprit. `localized: <bool>` rides along as the older two-state form of "
    "the same verdict, and is `false` under `long_tail`.\n"
    "- `additivity: overlapping` means these slices share entities and "
    "overstate the metric by `overlap` — arithmetic, not a defect. Per-slice "
    "`share_of_gap` is withheld (absent) because the slices do not sum to the "
    "total, and `reconciliation.status` is `not_applicable` rather than "
    "`discrepant`.\n"
    "- `entity_flows`, when present, sits beside the attribution, never inside "
    "it: new/churned/retained/migrated entity counts across the two windows "
    "(`reconciles_to_gap: false`). A migration nets to zero across slices — "
    "narrate it as the metric moving *between* slices, not changing size; "
    "naive slicing reads the same event as two large offsetting causes.\n"
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
    "(e.g. 'a 20% chance this loses money'), not just the point estimate. "
    "`prob_direction_censored: true` means every draw landed on one side, so the published "
    "number is the resolution ceiling of `n_draws` — a lower bound, not a measurement.\n"
    "- Interventions are do-operator: the metric is pinned, its usual drivers are severed, "
    "and effects propagate only downstream through the tree.\n"
    "- `khat_status` is the PSIS check on the fit behind a node's slope, and is absent on "
    "the NUTS default (exact MCMC has nothing to approximate). When present, the scenario "
    "was run with the fast approximation: `unusable` = the slope driving this outcome is "
    "not close to its posterior — narrate the direction, not the interval, and say a re-run "
    "without the approximation is what would settle it. `suspect` = approximate. "
    "`unavailable` = unchecked. `khat_borderline: true` means the check could not separate "
    "that band from the next one down — read the worse of the two.\n"
    "- `collinearity_status: moderate` or `high` on a node means the fit behind its slope "
    "could not cleanly separate that parent from a sibling that moves with it "
    "(`collinearity_warnings` names them). The scenario's *direction* stands; its magnitude "
    "rests on a coefficient the data splits poorly, so intervening on one member of the pair "
    "is not a clean lever — say so rather than quoting the size as measured.\n"
    "- `ppc_status: severe` on a node means the model behind its slope does not reproduce "
    "the data it was fitted on (`ppc_warnings` says which statistic and why). The scenario "
    "still propagates, but its magnitude is conditional on a likelihood the data argues "
    "against — treat the size as illustrative and say the node needs a better model before "
    "the number is worth acting on.\n"
    "- Fitted slopes are local to the observed operating range; `extrapolation: true` "
    "(detail in `warnings`) means the scenario leaves that range — call the result speculative.\n"
    "- `non_physical: true` is the stronger claim and a different one: the value cannot exist, "
    "because the tree declares a bound it breaks (a `share` outside [0, 1]) or the metric has "
    "never been negative and this scenario made it so. Do not narrate such a node's number — "
    "say the scenario is impossible as posed and what a possible version would look like.\n"
    "- Assumption edges are user-asserted beliefs sampled from the stated 90% range, not "
    "fitted from data; say so when they drive the answer.\n"
    "- Per-source `contributions` are exact Shapley shares of the node's *point* delta: they "
    "sum to `delta.estimate`, not to the posterior draws or the interval.\n"
    "- A rate node's `window_aggregate` says which arithmetic formed its baseline: "
    "`components` is Σnumerator/Σdenominator (the real window rate); any `period_mean_*` "
    "value means the plain average of per-period ratios, with `window_aggregate_reason` "
    "saying why — narrate such a baseline as approximate, not as the component aggregate.\n"
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


def _hash_prefix(tree: Optional[str]) -> str:
    """`#tree=<id>&` when a tree is named, else `#`.

    The UI parses `#tree=` first and gates everything else on it, since
    `#metric=`/`#rca=` are meaningless without knowing whose metric names they
    refer to. Omitting it means the default tree, so links minted before this
    existed still resolve."""
    return f"#tree={quote(tree)}&" if tree else "#"


def rca_link(
    target, reference_start, reference_end, analysis_start, analysis_end, tree=None
) -> str:
    """UI deep link replaying this exact RCA (the engine is seeded, so the
    link reproduces the numbers). Param names match applyDeepLink() in
    static/app.js."""
    return (
        f"{_base_url()}/ui/{_hash_prefix(tree)}rca={quote(target)}"
        f"&reference_start={reference_start}&reference_end={reference_end}"
        f"&analysis_start={analysis_start}&analysis_end={analysis_end}"
    )


def whatif_link(scenario: Dict[str, Any], tree=None) -> str:
    payload = quote(json.dumps(scenario, separators=(",", ":")))
    return f"{_base_url()}/ui/{_hash_prefix(tree)}whatif={payload}"


def metric_link(name: str, tree=None) -> str:
    return f"{_base_url()}/ui/{_hash_prefix(tree)}metric={quote(name)}"


def _khat_figure_fields(node: Dict[str, Any]) -> Dict[str, Any]:
    """`khat` / `khat_se` / `khat_borderline`, present only where they decide something.

    The number is spent only where an agent has a decision to make with it: a
    flagged band, or (roadmap S22) an `ok` band the estimate cannot separate
    from `suspect`. On a clean `ok` fit the bare status is the whole message
    and the digits are tokens. `khat_borderline` is emitted only when true —
    an absent flag on a node that has no k-hat at all would otherwise assert
    something about a check that never ran.
    """
    if node.get("khat_status") == "ok" and not node.get("khat_borderline"):
        return {}
    fields: Dict[str, Any] = {"khat": node.get("khat"), "khat_se": node.get("khat_se")}
    if node.get("khat_borderline"):
        fields["khat_borderline"] = True
    return fields


def compact_rca(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact a run_rca result: drop per-contribution decompositions (and the
    node-level `interaction` that summarizes them) and effective-window detail,
    trim components to `{estimate, ci_95}`, shrink skipped nodes to their
    status, and omit null node fields.

    A non-`ok` node keeps `status_reason`, and keeps its own `baseline`,
    `actual` and `gap` when it has them. Both matter more here than in the HTTP
    response, not less. `fit_failed` and `attribution_failed` mean *the engine
    could not analyze this node* — which an assistant must not narrate as
    "nothing happened here" — and the reason names the offending parent and
    dates, so dropping it leaves a bare label the assistant can only guess at.
    An `attribution_failed` node's gap is read off the data rather than the
    model, so it is a real movement with no split; withholding it while showing
    the label invites exactly the wrong reading. (`window_shorter_than_grain`
    carries neither, and correctly compacts to almost nothing.)"""
    nodes: Dict[str, Any] = {}
    for name, node in result["nodes"].items():
        if node["status"] != "ok":
            degraded = {"status": node["status"], "grain": node["grain"]}
            if node.get("status_reason"):
                degraded["status_reason"] = node["status_reason"]
            for field in (
                "baseline",
                "actual",
                "gap",
                "relative_change",
                # A degraded rate still says how its numbers were formed — and
                # `undefined_over_window`, the status a rate reaches most often,
                # is precisely where "there is no denominator, by nature" and
                # "nobody declared one" suggest different next moves.
                "window_aggregate",
                "window_aggregate_reason",
            ):
                if node.get(field) is not None:
                    degraded[field] = node[field]
            nodes[name] = degraded
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
            # Which sampler produced these numbers, stated only when it is not
            # the exact one. NUTS is the default on every fitted node, so
            # repeating `"nuts"` across a wide tree spends tokens to say
            # "nothing to report"; `"advi"` is the fact an agent has a decision
            # to make with, and it arrives with the k-hat below.
            "inference_method": (
                node.get("inference_method")
                if node.get("inference_method") not in (None, "nuts")
                else None
            ),
            # Roadmap S2. The verdict rides along on every *approximated* node,
            # and the number only where it is bad news: `khat` on an `ok` fit
            # is a token an agent has no decision to make with, while on a
            # flagged one it is the difference between "slightly off" and "not
            # evidence". Never dropped when non-null-and-not-ok, for the same
            # reason `unexplained_status` is never dropped.
            #
            # Roadmap S22 widens "bad news" by one case: an `ok` k-hat that is
            # within its own standard error of the 0.5 edge is *not* a clean
            # verdict, and an agent given the bare `ok` would narrate the
            # interval as sound. So the number and its error travel there too,
            # and `khat_borderline` is what says why they are present.
            "khat_status": node.get("khat_status"),
            **_khat_figure_fields(node),
            "khat_warnings": node.get("khat_warnings"),
            "sign_warnings": node["sign_warnings"],
            # Roadmap S4. Same economy as k-hat above, for the same reason: the
            # verdict rides on every node that had two or more parents to
            # check — an absent `collinearity_status` on a multi-parent node
            # would read as "separable" when it means "not checked" — and the
            # evidence only where it is bad news. `collinearity_warnings` says
            # which parents, in words, which is the whole point of S4.
            "collinearity_status": node.get("collinearity_status"),
            "collinearity": (
                node.get("collinearity")
                if node.get("collinearity_status") not in (None, "ok")
                else None
            ),
            "collinearity_warnings": node.get("collinearity_warnings"),
            # Roadmap S3, on the same economy as S4 and k-hat: the verdict on
            # every node that was fitted (an absent `ppc_status` on a fitted
            # node would read as "the model checks out" when it means "not
            # checked"), and the per-statistic evidence only where it is bad
            # news. The p-values are what a reader would need to argue with
            # the verdict, and only a bad verdict invites that.
            "ppc_status": node.get("ppc_status"),
            "ppc": (node.get("ppc") if node.get("ppc_status") not in (None, "ok") else None),
            "ppc_warnings": node.get("ppc_warnings"),
            # fit_window start/end are dropped for token economy; n_periods is
            # the decision-relevant number.
            "fit_periods": (node.get("fit_window") or {}).get("n_periods"),
            "seasonality_warnings": node.get("seasonality_warnings"),
            # Carried whole rather than summarized in RCA_HOW_TO_READ: each
            # warning string is self-contained ("...read its intervals and
            # components as approximate"), so the guidance travels exactly on
            # the nodes it applies to and costs nothing on the ones it doesn't.
            "likelihood_warnings": node.get("likelihood_warnings"),
            "ci_status": node["ci_status"],
            "unexplained": node["unexplained"],
            # Never dropped for token economy: `unexplained: 0` means two
            # opposite things (a reconciliation, or a derived node nobody
            # checked), and an agent reading the first as the second reports a
            # verified identity that was never verified. One field, five bytes.
            "unexplained_status": node.get("unexplained_status"),
            # Rate nodes only (null elsewhere, and null fields are dropped
            # below): how `baseline`/`actual` were formed, and why, when the
            # answer is not the real component aggregate.
            "window_aggregate": node.get("window_aggregate"),
            "window_aggregate_reason": node.get("window_aggregate_reason"),
            # Components keep their interval: the trend estimate is a fitted
            # quantity, and a narrator handed only its point value states model
            # structure with certainty the model never claimed. A null ci_95
            # stays null — withheld is information (see ci_status).
            "components": (
                {
                    k: {"estimate": v["estimate"], "ci_95": v["ci_95"]}
                    for k, v in node["components"].items()
                }
                if node["components"]
                else None
            ),
            # `interaction` is deliberately absent. It is a *readout* of the
            # co-movement already inside each contribution's `estimate`
            # (estimate = means + comovement; interaction = Σ comovement), and
            # this compaction drops the `decomposition` that says so. A summary
            # of a dropped detail, shipped without the detail, reads as one
            # more term — and an agent that sums the contributions and adds it
            # double-counts the entire co-movement shift. The full HTTP
            # payload keeps both halves together.
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
                    **{
                        k: c[k]
                        for k in (
                            "lag",
                            "parent_windows",
                            "prob_same_direction_censored",
                        )
                        if c.get(k) is not None
                    },
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
        # The verdict travels with the payload (C24): the UI applies exactly
        # this gate before naming a top slice, and an agent holding only
        # `prob_concentrated` — which answers a different question — would
        # confidently name one exactly where the UI declines to. Both forms
        # travel (roadmap 2.21): `localization` carries the third state, and
        # `localized` stays the narrower "may I name this slice?" boolean it
        # has always been, so an older consumer reading only the boolean is
        # restrained rather than misled.
        "localization": result.get("localization"),
        "localization_remedy": result.get("localization_remedy"),
        "localized": result.get("localized"),
        "localization_threshold": result.get("localization_threshold"),
        # The 3.8 trio. Dropping these was the C9 failure mode through a new
        # door: `additivity: overlapping` is *why* every slice is missing
        # `share_of_gap`, and the migration line in `entity_flows` is the
        # difference between "a user switched platform" and two large
        # offsetting causes — the misreading an LLM narrator is most likely
        # to produce is the one these fields exist to prevent.
        "additivity": (result.get("additivity") if result.get("additivity") != "unknown" else None),
        "overlap": result.get("overlap"),
        "entity_flows": result.get("entity_flows"),
        "slices": [{k: v for k, v in row.items() if v is not None} for row in result["slices"]],
        "reconciliation": {
            "status": result["reconciliation"]["status"],
            "residual_share_of_baseline": result["reconciliation"]["residual_share_of_baseline"],
        },
        "ci_status": result["ci_status"],
        "caveats": result["caveats"] or None,
    }
    # `localized: False` is a verdict, not a null — the trim below drops only
    # absent facts, never negative ones.
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
                **{k: node[k] for k in ("prob_direction_censored",) if node.get(k) is not None},
                "fit_quality": node["fit_quality"],
                # Roadmap S2, same vocabulary as the RCA payload. A scenario
                # propagates a fitted slope downstream, so which fit produced
                # it is decision-relevant here too.
                "khat_status": node.get("khat_status"),
                # Roadmap S22, and only the flag: the scenario payload carries
                # the verdict rather than the number (see `run_scenario`), and
                # "this verdict is not resolved" is part of the verdict.
                **({"khat_borderline": True} if node.get("khat_borderline") else {}),
                "khat_warnings": node.get("khat_warnings"),
                # Roadmap S4, and it lands harder here than on an RCA node: a
                # scenario is a *lever*, and a lever the fit cannot separate
                # from its neighbour is not one you can pull on its own.
                "collinearity_status": node.get("collinearity_status"),
                "collinearity_warnings": node.get("collinearity_warnings"),
                # Roadmap S3, and it lands here for the reason S4 does: a
                # lever whose model cannot reproduce its own history is not a
                # lever whose magnitude means anything.
                "ppc_status": node.get("ppc_status"),
                "ppc_warnings": node.get("ppc_warnings"),
                "extrapolation": node["extrapolation"]["flag"],
                # The stronger claim beside the weaker one (roadmap C26). An
                # agent reading `extrapolation: true` alone cannot tell "far
                # outside what we have seen" from "cannot exist"; the two want
                # different next moves, so both flags travel and the sentence
                # that separates them is in `warnings`.
                "non_physical": node["non_physical"],
                "contributions": node["contributions"],
            }
        # Cold-start results carry the belief interval around each operating
        # point; a null (point baseline) stays omitted like other null fields.
        if node.get("baseline_ci_95") is not None:
            nodes[name]["baseline_ci_95"] = node["baseline_ci_95"]
        # A rate's baseline says which arithmetic formed it (C25a) — the same
        # `window_aggregate` label RCA payloads carry, on both the shrunken
        # and the full node shape: an unaffected rate still shows a baseline.
        for k in ("window_aggregate", "window_aggregate_reason"):
            if node.get(k) is not None:
                nodes[name][k] = node[k]
    return {
        "mode": result["mode"],
        "baseline_window": result["baseline_window"],
        "n_draws": result["n_draws"],
        "sources": result["sources"],
        "nodes": nodes,
        "warnings": result["warnings"],
        "caveats": result["caveats"],
    }

# The What-If Machine — design spec

Status: v1 (steady-state MVP). Companion to `statistical_improvement_plan.md` (engine)
and `ui_design_spec.md` (UI). Roadmap item 3.4.

## 1. Product framing

Breakdown's RCA answers the backward question: *why did this metric move?* The
What-If Machine answers the forward one: *if I could move this metric, what
would happen to the business?* — using the same tree, the same fitted
relationships, and the same honesty standards.

The user flow: real data establishes a **baseline** (per-metric window means)
for every node in the tree. The user **adjusts one or more metrics** — usually
the influenceable drivers far from the north-star metric — and optionally
asserts **assumption links**: effects the fitted tree doesn't know about. The
canonical example is a discount to new subscribers: it changes ARPU
*deterministically* (−10%, exactly), is *assumed* to lift trial→member
conversion (+1–3%, a belief), and *may* cut renewal rate (−0.5–2%, a risk).
The engine propagates all of it through the tree — exactly through formula
edges, probabilistically through fitted influence edges, and by sampling the
stated ranges for assumptions — and reports every downstream metric's new
value with a credible interval.

A scenario is therefore a small, explicit, shareable statement of a theory of
the business: *these are the levers, these are my beliefs about their effects,
and this is what the tree implies.* That is the product: not a forecast oracle,
but a place where the theory is written down, quantified, and confronted with
the fitted structure.

Two personas, as everywhere in breakdown (`ui_design_spec.md`):

- **The operator** builds scenarios: picks the baseline, adjusts nodes, states
  assumptions, reads the uncertainty.
- **The reader** (the CFO) receives a deep link. They see the scenario story —
  what was assumed, what it implies for the north star, how sure we are —
  without the builder chrome in the way.

## 2. Scenario contract

### 2.1 Baseline

A date window `[baseline_start, baseline_end]` inside the loaded data range.
The baseline value of every metric is its **observed window mean**
(`window_mean`, `breakdown/engine/rca.py`). The baseline window represents
"current normal", not an anomaly — so fits may use data through the end of it.

### 2.2 Interventions

`{metric, mode, value}` with `mode ∈ {set, delta, pct}`, resolved to an
absolute steady-state target `v*`:

| mode  | v*                        |
|-------|---------------------------|
| set   | `value`                   |
| delta | `baseline + value`        |
| pct   | `baseline · (1 + value)`  |

Intervention semantics are the **do-operator**: setting a metric severs it from
its own structural equation. Its parents' deltas do not flow into it; its own
delta is exact (`v* − baseline`, zero variance) and flows only downstream.
Interventions never propagate parent-ward.

### 2.3 Assumption links

`{source, target, effect: {kind, low, high}, note?}` — a user-asserted effect
on a tree metric, scoped to the scenario (never persisted to the tree YAML).

- `target` must be a tree metric. The effect is **additive on top of**
  structural propagation into that node.
- `source` is a tree metric or a free-text **lever** name (any source not in
  the tree is implicitly a lever). In v1 the source is provenance/labeling
  only — the effect magnitude is the stated range, not scaled by the source's
  value. (Per-unit scaling is future work; totals cover the motivating cases
  and avoid a second unit system.)
- `effect.kind ∈ {absolute, relative}`; `relative` multiplies the target's
  baseline.
- **The range `[low, high]` is read as the central 90% interval of a Normal**:
  `mu = (low+high)/2`, `sigma = (high−low)/(2·1.645)`. Rationale: a person
  stating "+1–3%" means a credible band, not hard walls; Normal tails admit
  "slightly outside" honestly; Normal composes smoothly through downstream
  CIs (a Uniform would produce edge artifacts); and `low == high` degenerates
  cleanly to a deterministic effect — which is exactly how deterministic lever
  effects ("10% discount ⇒ AOV −10%") are expressed, with no separate
  override machinery.

If a node is both intervened and an assumption target, the intervention wins
and a warning is emitted (the do-operator clamps the node).

### 2.4 Levers

`{name, value?, unit?}` — display metadata for the scenario summary and
waterfall grouping. Levers have no independent dynamics in v1.

## 3. Statistical design

### 3.1 Propagation

**Scope.** Affected set = intervened metrics ∪ assumption targets ∪ all
`nx.descendants` thereof. Every other node stays at baseline (`status:
"baseline"`, delta 0).

**Draw-aligned Monte Carlo.** `n_draws = 2000` per scenario, one seeded
`np.random.default_rng(0)` per call (identical requests → identical responses,
mirroring `run_rca`). Every per-node delta is a length-`n_draws` vector and
**draw index j is preserved end-to-end**: an optimistic β draw at hop 1 feeds
the same draw at hop 2, so uncertainty composes correctly through multi-hop
paths (Monte Carlo composition, not interval arithmetic). Posteriors of
different nodes come from independent fits, so aligning index j across nodes
is valid; each node's posterior is indexed via `rng.choice` to break ordering
artifacts.

Per node, in topological order over the affected set (unaffected parents
contribute the zero vector):

1. **Intervened**: `delta = v* − baseline` (constant vector). Parents ignored
   (do-operator).
2. **Formula node**: `delta = f(base + delta_parents) − f(base)`, evaluated on
   draw vectors via `eval_formula`. Exact per draw; nonlinear cross-terms
   (Δorders·Δaov) are captured because parent deltas move jointly per draw.
3. **Probabilistic node**: `delta = Σᵢ beta_raw[j,i] · delta_parentᵢ[j]` using
   the node's posterior draws of `beta_raw` (business units, d(child)/d(parent)).
   Trend and seasonality are unchanged by an intervention and cancel out of
   the delta. **Lags are irrelevant under steady-state semantics**: an effect
   lagged k days still fully arrives at the new equilibrium — deltas are
   "eventual" effects.
4. **Assumption effects** targeting the node are added after the structural
   step, sampled from their Normal (relative kind × the node's baseline).

**Mean-propagation policy.** Deltas are computed at window-mean scalars, and
each node reports `simulated = observed_baseline + mean(delta)`. For a
constant steady-state shift through multilinear formulas this is *exactly*
equal to propagating daily arrays and averaging — the covariance/Jensen terms
cancel: `mean((o_t+Δo)(a_t+Δa)) − mean(o_t·a_t) = ō·Δa + ā·Δo + Δo·Δa`.
Because each node's baseline is its own **observed** mean (never
`f(parent means)`), the historical Jensen gap never contaminates the output;
the scenario only ever moves deltas.

### 3.2 Fits on demand

Every probabilistic node in the affected set with at least one affected parent
needs a `beta_raw` posterior. Fits resolve from the caller's trace cache with
the existing key convention `(name, fit_end)`:

- `fit_end = None` (full-window fit) when `baseline_end` is the last loaded
  date, else the ISO date of `baseline_end + 1 day` (`fit_end` is exclusive,
  so the fit uses data through the baseline window).
- Cache miss → ADVI `fit_metric` (the `run_rca` pattern), added to the cache
  in place.

Unlike RCA there is no anomaly to exclude: the baseline window is "current
normal", so fitting through it is correct.

### 3.3 Decomposition (the waterfall)

Sources = each intervention + each assumption (K total, capped at 10). The
per-node decomposition is an **exact Shapley over sources**: run a *point*
propagation (posterior-mean betas, mean assumption effects — no draws) for
each of the 2^K subsets of active sources, then apply standard Shapley
weights. By efficiency the per-source contributions sum exactly to the node's
point delta — no dangling "interaction" row; interactions through nonlinear
formulas are apportioned. On a purely linear path this reduces to the plain
per-source deltas. Point-level (not per-draw) is deliberate: the waterfall is
a point-estimate story; uncertainty lives on the totals. Cost: ≤ 1024 scalar
tree traversals — negligible.

Subset semantics: when an intervention is inactive in a subset, its node is
*not* clamped — structural propagation flows through it. This is what makes
the marginal contributions well-defined.

### 3.4 Honesty

Operating principle: *never ship a number the engine can't defend.*

- **Extrapolation flags.** Every non-baseline node carries full-history stats
  (`hist_min/max/mean/std` over all loaded data). A node is flagged when its
  simulated value falls outside `[hist_min, hist_max]` or more than 2σ from
  the historical mean; a "non-physical" warning fires when a nonnegative
  metric goes negative. The UI shows the historical band *while the user drags
  the slider* — the warning arrives before the run, not after.
- **Always-present caveats** in the response (fixed strings): fitted
  coefficients are local slopes (large moves may not extrapolate); learned
  edges are fitted associations, not experiments (unmodeled confounders);
  assumption effects are user-asserted, not fitted.
- **Per-node `fit_quality`** (from ADVI/NUTS diagnostics) surfaces "suspect"
  fits inline.
- `prob_direction` = max(P(delta>0), P(delta<0)) — the same certainty channel
  RCA uses, rendered as opacity.

## 4. API

### 4.1 `POST /simulate`

Stateless: the client owns the scenario; the server computes. Deep links carry
the scenario as `#whatif=<encodeURIComponent(JSON.stringify(scenario))>`
(URI-encoded JSON, debuggable, no helper code). File-based scenario save/load
is future work.

Request:

```json
{
  "baseline_start": "2024-03-13",
  "baseline_end": "2024-04-09",
  "interventions": [
    {"metric": "daily_sessions", "mode": "pct", "value": 0.15}
  ],
  "assumptions": [
    {"id": "a0",
     "source": "discount_pct",
     "target": "average_order_value",
     "effect": {"kind": "relative", "low": -0.12, "high": -0.08},
     "note": "10% blanket discount"}
  ],
  "levers": [{"name": "discount_pct", "value": 10, "unit": "%"}]
}
```

Response (every tree node present so the UI overlay loop is uniform):

```json
{
  "baseline_window": {"start": "2024-03-13", "end": "2024-04-09"},
  "n_draws": 2000,
  "seed": 0,
  "sources": [
    {"id": "i:daily_sessions", "kind": "intervention", "label": "daily_sessions +15%"},
    {"id": "a0", "kind": "assumption", "label": "discount_pct → average_order_value"}
  ],
  "nodes": {
    "revenue": {
      "status": "affected",
      "baseline": 1234.5,
      "simulated": 1301.2,
      "delta": {"estimate": 66.7, "ci_95": [31.0, 101.9]},
      "relative_delta": 0.054,
      "prob_direction": 0.982,
      "fit_quality": null,
      "extrapolation": {"flag": false, "hist_min": 900.1, "hist_max": 1500.7,
                        "hist_mean": 1210.0, "hist_std": 120.3},
      "contributions": [
        {"source": "i:daily_sessions", "estimate": 84.2},
        {"source": "a0", "estimate": -17.5}
      ]
    },
    "daily_sessions": {"status": "intervened", "...": "..."},
    "average_order_value": {"status": "affected", "...": "..."},
    "unrelated_metric": {"status": "baseline", "...": "delta 0, no contributions"}
  },
  "warnings": [
    {"kind": "extrapolation", "metric": "daily_sessions",
     "detail": "Simulated value 5980.0 is above the historical max 5714.2."}
  ],
  "caveats": ["..."]
}
```

Errors: unknown metrics, bad/empty windows, `low > high`, empty scenario, or
more than 10 sources → 422 with detail.

### 4.2 Engine

`breakdown/engine/simulate.py` — `run_scenario(dag, data, traces, scenario,
advi_draws=500, n_draws=2000) -> dict`, a pure function in the `run_rca`
mold: the caller owns `traces`; on-demand fits are added in place; a seeded
rng per call makes responses deterministic. Pydantic request models live in
the same module and are imported by the API layer.

## 5. UI

### 5.1 Placement

A third sidebar tab, **What-if**, alongside Metric and Root cause. The
builder stacks vertically in the 360px sidebar; the header remains RCA-owned.
The what-if baseline window gets its own two date inputs inside the tab
(prefilled to the last 28 days of loaded data).

**Overlay exclusivity:** the active tab owns the canvas. Switching to Root
cause re-applies the RCA overlay (if a result exists); switching to What-if
re-applies the scenario overlay (if a result exists); both go through a shared
`clearOverlays()`. Both results persist in client state — nothing refetches on
tab switch. The Metric tab keeps whichever overlay was last active.

### 5.2 Building a scenario

1. With the What-if tab active, tapping a node opens the **adjust panel**
   instead of the Metric tab: baseline window mean; a slider (−50%…+50%)
   two-way bound with absolute-value and % inputs (plus a "set" mode); and a
   **historical range strip** — a pure-CSS bar showing full-history min→max,
   a shaded ±2σ band, a baseline tick, and a live marker for the current
   setting that turns amber outside the band. The extrapolation signal
   arrives *before* the run.
2. **"+ Add assumption"**: source input with a datalist of metrics + existing
   levers (free text creates a lever), target select, effect kind toggle
   (% of baseline / absolute), low/high inputs, optional note.
3. **Scenario summary**: one row per intervention/assumption/lever with a
   remove control; Run simulation; Clear. Running posts `/simulate`, writes
   the deep-link hash, and shows fit-progress status ("fitting N models…").

### 5.3 Reading the result

On the graph (mirrors the RCA overlay grammar — sign=hue, magnitude=label,
certainty=opacity):

- Non-baseline nodes tint green/red by delta direction with a `▲ +3.4%`
  label line; background opacity maps from `prob_direction`.
- **Intervened nodes** get a heavy solid indigo border and a `⊙` marker —
  visibly pinned, distinct from computed effects.
- **Assumption links are drawn on the graph** as temporary dotted amber
  edges; lever sources appear as temporary amber-dashed nodes placed near
  their target (no re-layout). Removed on clear/tab-switch.
- Extrapolation-flagged nodes get the dashed amber border + ⚠.
- Baseline nodes and edges fade.

In the sidebar:

1. **North-star card** per sink node: `revenue: 1,234 → 1,301 (+5.4%)
   [CI +2.5%…+8.3%]`.
2. **Waterfall**: a signed bar per source (its Shapley contribution to the
   sink's delta), captioned "sums exactly (Shapley)".
3. **Per-node table**: baseline → simulated, Δ%, 95% CI, P(dir), ⚠ flags —
   sinks first, then topological order.
4. Warnings inline (amber); caveats as a persistent muted footer.

### 5.4 The reader

`#whatif=…` deep links replay the scenario on load: parse, auto-run, switch
to the What-if tab. When entered via deep link, the builder collapses into a
`<details>Edit scenario</details>` block below the results — the reader sees
the story first; the edit surface is one click away, never removed.

Constraints inherited from the existing UI: vanilla JS, no build step, no new
libraries (the range strip is CSS, not Plotly); template-string HTML with
`esc()`; null-tolerant rendering; CSS tokens in `:root`
(`docs/ai-context/frontend-ui.md`).

## 6. Non-goals (v1)

- **Trajectory mode** — per-date paths via T11's posterior-predictive forward
  simulation. The contract reserves a future `"mode": "trajectory"` request
  field; steady-state is the v1 semantics.
- **Tree editing** — assumptions are scenario-scoped and never persisted to
  the tree YAML. The DAG stays the analyst's committed hypothesis.
- **Causal discovery** — deliberately absent, as everywhere in breakdown.
- **Goal-seek / optimization** ("what do sessions need to be for +10%
  revenue?") — the natural next feature: the pipeline is mostly linear and
  invertible. Named here so the contract doesn't preclude it.
- **Per-unit lever scaling** (effect sized by Δsource) and **server-side
  scenario storage** (a `scenarios/` YAML dir) — future work.

## 7. Build plan

- **W1** — this document.
- **W2** — engine: `breakdown/engine/simulate.py` (scenario models,
  resolution, affected set, draw-aligned propagation, do-operator clamping,
  assumption sampling, Shapley source decomposition, extrapolation stats,
  fit-on-demand) + `tests/test_simulate.py`.
- **W3** — API: `POST /simulate` in `breakdown/api/main.py` + `tests/test_api.py`.
- **W4** — UI builder: third tab, baseline inputs, node-tap routing, adjust
  panel with range strip, assumption form, summary list, run wiring, hash.
- **W5** — UI results: scenario overlay + temporary assumption elements,
  `clearOverlays()` refactor and tab exclusivity, cards/waterfall/table,
  deep-link reader mode.
- **W6** — docs (`docs/ai-context/frontend-ui.md` API surface + deep links)
  and an end-to-end pass.

W2/W3 and W4 are parallelizable after W1; W5 needs W3+W4.

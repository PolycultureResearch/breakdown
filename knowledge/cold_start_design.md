# Cold Start Mode — what-if with zero data (design spec)

Status: draft (v1 scope: engine + parser; API/UI phased behind it). Companion to
`what_if_design.md` (the fitted what-if machine this extends) and
`cold_start_founder_intro.md` (the audience-facing pitch).

## 1. Product framing

The what-if machine currently requires a fitted tree, which requires data. But
its propagation core never touches a time series: it consumes **baselines**
(operating points), **beta_raw draws** (edge slopes in business units), and
**assumption effects** (user-stated Normal ranges). Two of those three are
derived from data today; none of them has to be.

Cold-start mode replaces the data-derived inputs with declared ones, so a
**pre-revenue company can use the what-if machine before the first row of data
exists**. The Bayesian slogan is literal: the minimum sample size is zero. A
founder states a tree, operating-point beliefs, and edge-slope beliefs as
ranges; the engine propagates them with exactly the same draw-aligned Monte
Carlo, do-operator semantics, and Shapley source decomposition as fitted mode
— and reports honestly wide intervals instead of a spreadsheet's false point
estimates.

What the user gets that a spreadsheet can't give them:

- **Coherent uncertainty.** Ranges compose through multi-hop paths by Monte
  Carlo, not by hand-waving. Wide output intervals are the truth about a
  business that doesn't exist yet.
- **Scenario comparison under uncertainty.** Two monetization trees (or two
  scenarios on one tree) compare as distributions, not midpoints.
- **Sensitivity = what to test first.** The source waterfall already
  attributes each outcome to the interventions/assumptions that drove it;
  pre-data, that reads as "these two beliefs control the answer — measure them
  first."
- **A smooth on-ramp to fitted mode.** The same YAML priors feed `fit_metric`.
  When data arrives, fitting turns priors into posteriors with zero config
  changes; what-if flips from prior draws to posterior draws per node.

Same two personas as everywhere: the **operator** (here often the
founder/advisor authoring beliefs) and the **reader** receiving a deep link.

## 2. Tree contract (YAML)

Cold-start mode is a property of the *tree*, not the scenario: a tree with no data
provider and fully declared beliefs is a cold-start tree. Three additions to
`MetricDefinition`:

### 2.1 `baseline` — asserted operating point

```yaml
- name: site_sessions
  source: assumed              # provenance label; no provider is queried
  grain: day
  baseline: 1200               # shorthand: point value
- name: signup_rate
  source: assumed
  kind: rate
  baseline: {low: 0.01, high: 0.05}   # central 90% interval of a Normal
```

- Required (in cold-start mode) on every **non-formula** node: sources and
  probabilistic nodes. Formula nodes may **not** declare one — their baseline
  is derived per-draw as `f(parent baselines)`, so the identity holds by
  construction (parse-time error otherwise).
- Units are **mean per native grain period** — the same thing a fitted
  baseline (`window_mean`) is. A daily flow's baseline is "per day"; the
  existing `edge_scale` machinery handles finer-flow-parent-into-coarser-child
  edges unchanged.
- The `[low, high]` range is read exactly like an assumption effect: the
  central 90% interval of a Normal, `low == high` degenerating to a point.
  One elicitation convention everywhere.

### 2.2 `plausible` — declared honesty band

```yaml
  plausible: {min: 0, max: 10000}
```

Optional on any node; either bound may be omitted. Fitted mode flags
extrapolation against history (`hist_min/max`, ±2σ); cold-start mode has no
history, so `plausible` is the declared substitute: a simulated value outside
the bounds flags the node and emits a warning, and `min: 0` recovers the
non-physical (negative) check. No bounds → no flag (the response says so
rather than pretending confidence). Also the natural data source for the UI's
range strip. (Fitted mode may *also* use `plausible` later to tighten the
historical band; out of scope here.)

### 2.3 Priors become load-bearing

Every probabilistic edge on an affected path must carry an **explicit
business-unit prior** — the parent-specific entry or the shared
`coefficient` entry. The fitted-mode fallback (`Normal(0,1)` in normalized
space) is meaningless without data to define the scale, so cold-start mode errors
loudly, listing every missing edge.

Two prior-authoring notes that differ from fitted mode:

- **Sign-constrained priors are appropriate here.** `docs/model.md` argues
  against clamping signs when data could contradict them; with no data there
  is nothing to fight. `HalfNormal`/`LogNormal`/`Exponential` (all already
  supported by `scale_prior_params`) encode "surely positive" beliefs cleanly.
- **The prior IS `beta_raw`.** YAML priors are already stated in business
  units; the data-dependent rescaling (`scale = x_std/y_std`) exists only to
  move them into z-space for fitting. Cold-start mode skips it and samples the
  declared distribution directly.

## 3. Statistical design

### 3.1 Inputs

`n_draws = 2000`, one seeded `np.random.default_rng(0)` per call — identical
requests return identical responses, as in fitted mode. Three draw families,
all draw-aligned end-to-end:

1. **Baseline draws** `B_n[j]`, shape `(n_draws,)` per node. Asserted nodes
   sample their Normal (point baselines are constant vectors); formula nodes
   derive `B_f[j] = f(B_parents[j])` in topological order — per-draw, so
   nonlinear composition (Jensen terms, parent co-movement under shared
   uncertainty) is exact under the stated beliefs. This is the honest
   counterpart of fitted mode's "baseline = own observed mean": with no
   observation, the identity applied to beliefs is the only defensible value,
   and per-draw evaluation keeps it coherent.
2. **Beta draws** `beta_raw[j, i]` per probabilistic node, sampled directly
   from each parent's YAML prior. Draws are independent across parents and
   across nodes (fitted-mode posteriors correlate parents within a node;
   priors carry no such information — see §3.4 honesty).
3. **Assumption effect draws** — unchanged from fitted mode; `relative`
   effects scale by the target's baseline *draws*, keeping the worldview
   consistent within each draw j.

### 3.2 Propagation

Identical to fitted mode (`what_if_design.md` §3.1) with baselines as vectors:

- Interventions resolve per draw: `set` → `delta[j] = v* − B[j]` (the pinned
  *level* is exact; the *delta* inherits baseline uncertainty), `delta` →
  constant, `pct` → `B[j]·value`.
- Formula nodes: `delta[j] = f(B_parents[j] + delta_parents[j]) −
  f(B_parents[j])`.
- Probabilistic nodes: `delta[j] = Σᵢ beta_raw[j,i] · delta_parentᵢ[j]`
  (edge grain-scaling unchanged).
- Do-operator, assumption additivity, override warnings: unchanged.

Trend and seasonality never appear: they cancel out of fitted-mode deltas and
simply don't exist pre-data. Steady-state semantics only — cold-start mode changes
*what the inputs are*, not what a scenario means.

### 3.3 Decomposition

The Shapley-over-sources waterfall runs on point propagations exactly as in
fitted mode, using **analytic prior means** (Normal → μ; HalfNormal →
σ·√(2/π); Exponential → 1/λ; LogNormal → exp(μ+σ²/2)) and **baseline means**.
Efficiency holds: contributions sum to the point delta per node. Pre-data,
the waterfall doubles as the sensitivity report ("which belief moves the
answer"), which is arguably its highest-value reading.

### 3.4 Honesty

- **Response is labeled.** Top-level `"mode": "cold_start"`; per-node `fit_quality`
  is `null`; `baseline_ci_95` accompanies each node's baseline point estimate
  when the baseline is uncertain. Nothing prior-derived masquerades as fitted.
- **Extrapolation** flags against `plausible` bounds (absent bounds → no flag,
  and the extrapolation block says `"bounds": null` rather than inventing a
  band).
- **Prior-mode caveats** replace two of the three fitted caveats:
  - "All coefficients and baselines are stated beliefs (priors), not
    estimates from data — results quantify the consequences of your
    assumptions, not evidence."
  - "Belief draws are sampled independently per edge and per baseline;
    correlated beliefs ('if CAC is high, conversion is low') are not
    represented and intervals may be too narrow or too wide where beliefs
    co-vary."
  - (kept) "Assumption-link effects are user-asserted beliefs, not fitted
    from data."
- **Wide is right.** Multi-hop products of wide priors produce wide output
  intervals. That is the feature; the UI should present interval shrinkage
  over time (as data arrives and mode flips) as the product working.

## 4. Engine contract

`run_scenario` keeps one signature; **`data=None` selects cold-start mode**:

```python
run_scenario(dag, data=None, traces=None, scenario, n_draws=2000)
```

- `scenario.baseline_start/end` become optional: required in fitted mode
  (unchanged behavior), rejected in cold-start mode (there is no data window to
  mean over; the operating point comes from the YAML).
- `traces` is ignored in cold-start mode (no fits exist or are created).
- Prior-readiness validation is a public helper —
  `validate_cold_start(dag) -> list[str]` — returning every violation
  (non-formula node without `baseline`; probabilistic edge without an explicit
  prior), so `run_scenario` can raise a single aggregate `ValueError` and
  `breakdown doctor` can reuse it verbatim for a pre-flight check.
- Response shape is the fitted shape plus `"mode"`, per-node
  `baseline_ci_95` (nullable), and the cold-start extrapolation block; minus
  nothing. The UI overlay loop stays uniform.

## 5. Phasing beyond the engine

### 5.1 API (next after engine)

- `provider: none` (alias `assumed`): `lifespan` skips fetching entirely;
  `app.state.data = None`, not a degraded startup. Data routes (`/series`,
  `/rca/*`, `/analyze/*`, `/shapley/*`) return 422 "this tree declares no data
  provider" — a stated mode, not an error banner.
- `POST /simulate` branches on `app.state.data is None`. `/meta` carries
  `"mode": "cold_start"` so the UI boots into the right surface.
- MCP `run_whatif` passes through unchanged; `WHATIF_HOW_TO_READ` gains the
  cold-start caveat block.

### 5.2 UI (after API)

- Boot in what-if-first layout when `/meta` says cold-start mode: cards show
  asserted baseline (± range) instead of sparklines; probabilistic edges label
  with the prior ("β ~ 0.02 [0.01, 0.03] · belief" chip) instead of fitted β.
- The adjust panel's range strip renders from `plausible` bounds.
- Reader mode, deep links, waterfall, per-node table: unchanged.

### 5.3 Hybrid mode (~~future, the real payoff~~ — removed 2026-08-05)

> **Not scheduled.** This was roadmap item 2.7 and was removed; see
> [Deliberately not on the roadmap](roadmap.md#deliberately-not-on-the-roadmap)
> for the reasoning. Cold start is now a demo mode rather than a supported
> persona, so deepening it is not where engine surface goes. The section is kept
> because the design is sound and someone may want it later — read it as a
> proposal, not a plan.

Per-node graceful upgrade once *some* data exists: a node with ≥ 10 whole
periods uses its posterior and measured baseline; every other node falls back
to prior draws and asserted baselines; each node's response says which it
used (`"basis": "posterior" | "prior"`). This turns the current 10-period
cliff into a ramp and matches breakdown's per-node-degradation philosophy
(per-node grains, per-node `window_shorter_than_grain`). Deliberately not in
v1: it needs per-node basis bookkeeping in the response, UI badges, and a
policy for partially-fitted paths — worth its own small spec.

## 6. Non-goals (v1)

- **Correlated beliefs** — joint elicitation / copulas across edges or
  baselines. Named in the caveats instead.
- **Prior-predictive series generation** — simulating fake time series from
  the priors so RCA/cards run pre-data. `MockDataFetcher` already covers the
  demo need (DAG-respecting series from `coefficient` prior means); note that
  *fitting* mock data yields spuriously tight pseudo-posteriors, which is
  exactly what cold-start mode exists to avoid.
- **Trajectory mode** — steady-state only, as in fitted what-if v1.
- **Scenario-level baseline/prior overrides** — beliefs live in the tree
  YAML (the committed hypothesis), not the request. Revisit if the UI wants
  belief-tweaking without YAML edits.
- **Elicitation tooling** — helpers that turn "somewhere between 1-in-20 and
  1-in-100" into YAML. High leverage, separate surface.

## 7. Build plan

- **P1** — this document.
- **P2** — parser: `baseline` (shorthand + range), `plausible`, formula-node
  baseline rejection + tests.
- **P3** — engine: cold-start branch in `run_scenario` (baseline draws, prior
  beta sampling with analytic means, plausible-bounds flags, labeled
  response), `validate_cold_start`, + `tests/test_simulate_cold_start.py`
  (fitted-mode suite must pass untouched).
- **P4** — API: `provider: none`, dataless lifespan, route guards, `/simulate`
  branch, `/meta.mode`, MCP caveats + tests.
- **P5** — UI: cold-start boot surface, belief edge labels, plausible range
  strips, docs (`docs/model.md` "reading cold-start output" section,
  `docs/ai-context/*` updates).

P2+P3 ship together (this draft); P4 and P5 are sequential behind them.

# The model, its assumptions, and how to read the results

This page is for the person interpreting breakdown's output. It states exactly
what is fitted, the assumptions behind it, and the caveats that matter when you
read an attribution. No PyMC knowledge required.

## What gets fitted

Every metric in the tree can be fitted with a Bayesian structural time series
(BSTS). All series are z-scored (mean 0, sd 1) before fitting; results shown in
business units are converted back afterward. In normalized space:

```
y[t] = α + trend[t] + seasonal[t] + regression[t] + ε[t]
```

| Term | What it is | Prior |
|---|---|---|
| `α` | intercept | Normal(0, 1) — the data is z-scored, so its mean is exactly 0 |
| `trend[t]` | local level: a non-centered random walk (`cumsum(σ_trend · z)`) that absorbs slow drift | step size σ_trend ~ HalfNormal(0.05) by default, set by the YAML `trend.sigma` |
| `seasonal[t]` | 2 sin/cos Fourier pairs per `seasonality` entry | coefficients ~ Normal(0, 1) |
| `regression[t]` | `Σᵢ βᵢ · xᵢ[t]` over the metric's parents | from your YAML `priors`, else Normal(0, 1) in normalized space |
| `ε[t]` | observation noise | sd ~ HalfNormal(1) |

The trend uses a **non-centered** parameterization (unit normals scaled by
`σ_trend`) to avoid the funnel geometry that makes a centered random walk hard
for NUTS and unreliable for ADVI. The default step-size prior is deliberately
**tight** (HalfNormal(0.05)): the level is expected to drift slowly, leaving the
movement to be explained by parents and seasonality rather than absorbed into a
flexible trend. Loosen it per metric with `trend: {sigma: ...}` when a node
genuinely has fast level changes the parents don't capture.

The three node types use this differently:

- **Source metrics** (no parents): trend + seasonality only. Fitting one is
  useful mainly to see its decomposition.
- **Probabilistic metrics**: parents enter as regressors. Each parent's series
  is shifted back by its `lags` entry (if any) before fitting, and the first
  `max(lags)` rows are dropped so everything aligns.
- **Formula metrics**: the formula is treated as exact, so there are no β's to
  learn. The BSTS is fitted to the **residual** `observed − formula(parents)`,
  which captures whatever the identity doesn't explain (data noise, definition
  drift). With `lags`, the identity is cohort-aligned — `A[t] = f(parents
  shifted back by their lags)` — and both the residual and the Shapley
  attribution read each lagged parent from correspondingly shifted windows.

## Grain: what one observation is

Every node declares a natural `grain` (`day`, `week`, `month`; default day)
and a `kind` (`flow` sums over time, `stock` takes the last value, `rate`
recomputes from components). **A node is fetched, fitted, and attributed at
its own grain, never below it.** This is a statistical statement, not a
formatting one: a monthly snapshot forced onto a daily spine is 30 identical
rows carrying one observation of information — the posterior it produces is
spuriously tight, and per-day ratios on low-volume days are noise the
bootstrap then has to paper over. At the natural grain, fewer observations
per fit is the *honest* posterior width.

Consequences to keep in mind when reading results:

- `t`, lags, and seasonality periods are all **grain steps** of the node
  (`period: 7` is weekly on a daily node and seven months on a monthly one).
- Finer **flow/stock parents are resampled up** to the node's grain (sum /
  last) before fitting or attribution; a weekly identity over a daily flow
  uses the weekly *sum*. Rates never auto-resample — declare them at the
  grain they're consumed at.
- **Windows snap per node** to the whole periods fully inside the requested
  dates; each node reports its `effective_windows`, and a node whose window
  holds no whole period is reported with `status: "window_shorter_than_grain"`
  rather than failing the analysis. Fits need ≥ 10 whole periods, so monthly
  nodes want roughly a year of history.
- **Gaps are mean-per-period at each node's own grain.** In a mixed-grain
  tree, raw gaps of different-grain nodes are not comparable — compare
  `share_of_gap` and `ranked_causes` scores instead.
- Partial edge periods are dropped, never zero-filled, so a coarse metric's
  series can end before the raw data window does; the trend's flat forecast
  for a monthly node sits at the last *whole month* before the analysis
  window, which can be weeks before the anomaly.

## What data the fit sees

RCA fits each node on data **strictly before the analysis window** (`fit_end =
analysis_start`, an exclusive cutoff; only whole periods that *end* by the
cutoff are used, so a coarse period straddling the anomaly can't train the
model). This matters: if the anomalous window were
included in the training data, the flexible trend could absorb the anomaly as
"drift" and the parent coefficients would be dragged toward a compromise between
the normal regime and the incident. Fitting on the pre-anomaly period only means
the βs encode the *normal-regime* relationship, so `beta_raw × Δparent` answers
the question you actually asked — "given how these metrics normally relate, how
much of this change do the parents explain?" (This mirrors the CausalImpact
methodology of fitting on the pre-period and treating the post-period as a
counterfactual.)

Two consequences worth knowing:

- **Normalization follows the fit window.** z-scoring and prior rescaling use
  only the pre-anomaly rows, so the anomaly no longer inflates the scale used to
  normalize it. Because priors are rescaled with sample statistics
  (`scale = x_std / y_std`), the effective prior in business units is mildly
  data-dependent — a pragmatic, empirical-Bayes-adjacent choice that is what
  makes business-unit priors possible.
- **`/analyze` defaults to the full window.** The exploratory `POST /analyze/{name}`
  endpoint fits on all loaded data unless you pass `?fit_end=<date>`. To confirm
  an RCA node with NUTS on exactly the data RCA used, pass
  `?fit_end=<analysis_start>&inference_method=nuts`.

## Declared signs and scale confounding

`expected_signs` on a probabilistic node declares the direction you believe an
effect runs. It is deliberately **not** a prior: the fit stays unconstrained,
and when the `beta_raw` posterior puts less than 10% of its mass on the
declared side, the fit carries a `sign_warnings` diagnostic instead of quietly
shipping a coefficient that means something else.

Take a contradicted sign seriously as a *modeling* signal, not a fitting
error. The most common cause is **scale confounding** in a level-on-level
edge: a dollar flow regressed on a user count, where both series grow with
the business. The dominant covariance is "bigger base → more of both," and
the learned coefficient answers that question — not the per-user propensity
question the author meant. What-if simulations then move the child in the
"wrong" direction with full statistical justification. The cure is to make
both sides scale-free (regress the churn *rate* on the active *share*, at a
grain where those ratios are stable) or to control for the base explicitly —
not to clamp the sign with a constrained prior, which just forces the model
to fight data that genuinely contradicts the edge as defined.

## Reading coefficients: `beta` vs `beta_raw`

The posterior contains both. `beta` is in z-scored units ("a 1-sd move in the
parent moves the child this many sds") and is what the sampler actually works
with. **`beta_raw` is the one to report**: it is the same coefficient in
business units — d(child) per unit change of the parent. Priors in the YAML
are stated in business units and translated internally, so `mu: 0.1` on
`sessions → orders` really means "one extra session is worth ~0.1 orders".

## How RCA attributes a change

Given a reference window and an analysis window, each metric's change is its
**window-mean difference**: `gap = mean(analysis) − mean(reference)`.

- **Formula nodes** get exact **symmetric per-day** Shapley attribution. Both
  windows are evaluated day by day, and each parent's contribution is the sum
  of three exact Shapley games: a **window-means bridge** (reference means →
  analysis means), plus the parent's share of the **within-analysis-window
  co-movement term**, minus its share of the **within-reference-window**
  counterpart. The parts telescope, so contributions sum exactly to
  `mean over analysis days of formula(parents that day) − mean over reference
  days of formula(parents that day)`. Compared to Shapley on window means,
  this attributes *shifts* in the parents' within-window covariance to the
  parents — for `revenue = orders × aov`, "the big orders disappeared" is
  exactly an orders–aov covariance shift, and it shows up in the attribution
  instead of in `unexplained` — while a covariance that is merely *present*
  but unchanged in both windows contributes nothing. (For non-product
  formulas the co-movement terms are each window's full Jensen gap
  `mean f(daily) − f(means)`.)
- **Probabilistic nodes** get posterior attribution: contribution of parent i
  is the distribution `beta_raw[i] × (parent's gap)`. For lagged parents, the
  parent's gap is measured over windows shifted back by the lag — the parent
  values that actually influenced the analysis window. Every lagged
  contribution (both attribution methods) reports which windows those were:
  `lag` and `parent_windows` `{reference, analysis}`, the node's snapped
  windows shifted back by the lag. Narrate a lagged parent with *its* dates,
  and reuse them as the windows for any follow-up analysis of that parent
  (drill-down RCA, slicing). Unlagged contributions omit both keys.
- **Probabilistic nodes also report a `components` block** — the fitted model's
  own trend and seasonal terms, as window-over-window deltas with credible
  intervals (see below).

**Reading the two views (formula nodes).** The three games surface as two
presentations. The **headline** is the window-aggregate bridge — each parent's
means-bridge contribution plus one explicit *co-movement shift* row (the
summed interaction term across parents, `interaction` in the response). This
is the standard price/volume/mix decomposition, with the interaction shown as
its own labeled line rather than silently split among the factors — a large
co-movement row ("price and volume started moving together differently") is a
finding, not a nuisance. The **detailed** view splits each parent into
`means + comovement = total` (the `decomposition` on each contribution). For
a product the co-movement term is exactly the parents' covariance delta,
split evenly; for ratios and other formulas it is each window's full
within-window Jensen term, so read it as "co-movement", not strictly
"covariance".

Every contribution is summarized as an `estimate` (mean), a 95% interval
(`ci_95`), and `prob_same_direction` (the probability mass on the dominant side
of zero). These intervals reflect **two** sources of uncertainty:

1. **Coefficient uncertainty** (probabilistic nodes): the `beta_raw` posterior.
2. **Window-sampling uncertainty** (all nodes): a window mean over a handful of
   periods is itself a noisy estimate (its sd shrinks only like 1/√periods —
   brutal for a 2–3 day "what happened this weekend?" window). RCA resamples
   each window's rows with a **circular moving-block bootstrap** (block length
   per grain: up to 7 days, 4 weeks, or 2 months, resampled jointly across the
   node's parents so cross-metric correlation within the window is preserved)
   and composes the resampled window-mean differences with the coefficient
   posterior. Formula-node contributions get their CIs entirely from this
   bootstrap — the *relationship* is exact, but the window means feeding it
   are not.

The bootstrap assumes the series is roughly stationary within each window with
serial dependence of at most about a block. Replicates are seeded per RCA
call, so identical requests return identical numbers. A window that snaps to
a **single period** degenerates the bootstrap to identical replicates, so the
CI is withheld instead of reported as zero-width: formula nodes get
`ci_status: "degenerate_single_period"` with `ci_95: null` on their
contributions; posterior nodes keep their coefficient-posterior CI but are
flagged `"posterior_only_single_period"` because the window-sampling
component is absent.

### `components`: trend and seasonal, made explicit

For a fitted (probabilistic) node the model already decomposed the series into
trend, seasonality, and regression — so RCA reports the first two instead of
lumping them into `unexplained`:

- **`seasonal`** is the window-over-window change in the fitted Fourier
  component. It is parametric in time, so it evaluates in both windows exactly.
  This is where **window composition bias** becomes visible: a reference window
  with 1.4 weekends compared against an analysis window with 2 creates a real
  seasonal gap that is *not* anyone's fault — prefer whole-week windows (7, 14,
  28 days) to avoid manufacturing it.
- **`trend`** is the fitted level's forecast change: the analysis window lies
  after the fit period (RCA fits strictly before it), and a random-walk local
  level forecasts flat at its last fitted state — so the analysis-window trend
  is `trend[last fitted day]`, compared against the reference-window mean of
  the fitted trend. Its CI comes from the posterior of that last state, not
  from forward simulation of new steps.

Formula nodes and roots have `components: null`.

### `unexplained`

Contributions generally do **not** sum to the node's observed gap; the
remainder is reported as `unexplained`.

- For **probabilistic** nodes,
  `unexplained = gap − Σ parent contributions − trend − seasonal`: with the
  model's own components broken out, what remains is observation noise and
  genuine model misfit — an unmeasured driver, a wrong lag, a nonlinearity.
- For **formula** nodes it is only the target's own measurement noise around
  the identity: both windows are reconstructed per-day from the parents, so an
  exact identity has `unexplained = 0` up to floating point, and anything
  nonzero means the target's own series genuinely disagrees with
  `formula(parents)` inside one of the windows.

A large `unexplained` is a finding, not an error — it says "neither the parents
you modeled nor the fitted trend/seasonality account for this move."

### `share_of_gap` can exceed 100%

Shares are `contribution / gap` and are not clamped. Two parents can push in
opposite directions (one +145%, one −45%), which is exactly what happened and
worth seeing. The UI clamps only the *edge width*, never the numbers.

### `ranked_causes` is a heuristic

The ranking propagates a score from the target upward, weighting each hop by
the parent's |share| (clamped to 1). It is a triage ordering — "look here
first" — not a probability. For rigor, read the per-node contributions and
their credible intervals.

## Reading cold-start output

A tree with `provider: none` runs what-if simulations with **zero data** —
every input is a stated belief, and the output must be read that way.

**Where the numbers come from.** Each non-formula node's operating point is
its asserted `baseline` — `[low, high]` read as the central 90% interval of a
Normal, sampled per draw (formula nodes derive theirs from parents per draw,
so identities hold exactly under the stated beliefs). Each probabilistic
edge's slope is sampled directly from its YAML prior in business units: with
nothing to fit, the prior *is* the coefficient distribution. Propagation,
do-operator semantics, and the Shapley source decomposition are identical to
fitted mode; the response says `mode: "cold_start"`.

**What the intervals mean.** A fitted 95% CI summarizes a posterior — belief
disciplined by data. A cold-start 95% CI summarizes *only your stated
beliefs composed coherently through the tree*. It answers "if my ranges are
honest, where does the outcome land?", never "what does the evidence say?".
Wide intervals are the feature: they are the truth about a business with no
history, made explicit instead of hidden behind a spreadsheet's single
confident number. Per-node `baseline_ci_95` is the belief interval around
that node's operating point.

**How to use it.** Comparing scenarios (two pricing trees, two growth
assumptions) compares belief distributions — a valid, useful comparison. The
source waterfall doubles as sensitivity analysis: the beliefs that dominate
the outcome's spread are the ones worth measuring first. What cold-start
output can never do is confirm a belief — only data can, and when it arrives
the same YAML priors feed `fit_metric` and posteriors take over with no
config changes.

**Honesty flags.** Extrapolation warnings come from the tree's declared
`plausible` bounds (there is no history to compare against); a node with no
bounds is never flagged, which means *unchecked*, not *safe*. Belief draws
are sampled independently per edge and per baseline — correlated beliefs
("if price lands high, conversion lands low") are not represented, so
intervals may be too narrow or too wide where beliefs co-vary. Both caveats
ship in every cold-start response.

## Assumptions and limitations to keep in mind

1. **The DAG is your hypothesis.** breakdown quantifies relationships along
   the edges you declared; it does not discover edges, detect confounders, or
   prove causality. An effect routed through an unmodeled path will show up as
   `unexplained` or get misattributed to a correlated parent.
2. **Relationships are linear and (unless lagged) contemporaneous.** A parent
   whose true effect is nonlinear or delayed by an unmodeled lag will look
   weaker than it is.
3. **Trend can absorb signal.** A flexible random-walk trend can explain a
   child's slow drift as "trend" instead of "parent", widening β's credible
   interval. breakdown now defaults to a **tight** step-size prior
   (HalfNormal(0.05) in z-scored space) precisely to avoid this, so β stays
   sharply identified in the common case. If a node genuinely has fast level
   changes its parents don't capture, loosen the prior for that node with
   `trend: {sigma: ...}` in the YAML — and watch for a β CI that straddles zero
   as the sign that the trend is now competing with a parent.
4. **Seasonality needs data to be identified.** A `period: 365` component on
   100 days of data is unidentifiable and will soak up degrees of freedom —
   only declare seasonality your window can actually see (≥2 full periods).
5. **Window means hide within-window shape.** A spike-and-recover pattern and
   a level shift can have the same window mean. Choose windows that isolate
   the regime you care about, and look at the time-series panel.
6. **ADVI vs NUTS.** ADVI (the RCA default) is a fast approximation that can
   understate uncertainty; NUTS is the gold standard and reports convergence
   diagnostics (R̂ < 1.05 is healthy). Triage with ADVI, confirm with NUTS.

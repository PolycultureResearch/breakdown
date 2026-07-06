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
  drift).

## What data the fit sees

RCA fits each node on data **strictly before the analysis window** (`fit_end =
analysis_start`, an exclusive cutoff). This matters: if the anomalous window were
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

- **Formula nodes** get exact Shapley attribution computed on the parents'
  window means. The parent contributions sum exactly to
  `formula(analysis means) − formula(reference means)`. No uncertainty — the
  relationship is an identity.
- **Probabilistic nodes** get posterior attribution: contribution of parent i
  is the distribution `beta_raw[i] × (parent's gap)`, summarized as a mean, a
  95% credible interval, and `prob_same_direction` (the posterior probability
  that the contribution's sign is what the point estimate says). For lagged
  parents, the parent's gap is measured over windows shifted back by the lag —
  the parent values that actually influenced the analysis window.

### `unexplained`

Contributions generally do **not** sum to the node's observed gap; the
remainder is reported as `unexplained`. It is large when:

- the node's own data is noisy around the formula (formula nodes), or
- the linear-in-parents model misses part of the story: trend, seasonality, an
  unmeasured driver, a wrong lag (probabilistic nodes).

A large `unexplained` is a finding, not an error — it says "the parents you
modeled don't account for this move."

### `share_of_gap` can exceed 100%

Shares are `contribution / gap` and are not clamped. Two parents can push in
opposite directions (one +145%, one −45%), which is exactly what happened and
worth seeing. The UI clamps only the *edge width*, never the numbers.

### `ranked_causes` is a heuristic

The ranking propagates a score from the target upward, weighting each hop by
the parent's |share| (clamped to 1). It is a triage ordering — "look here
first" — not a probability. For rigor, read the per-node contributions and
their credible intervals.

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

# The model, its assumptions, and how to read the results

This page is for the person interpreting breakdown's output. It states exactly
what is fitted, the assumptions behind it, and the caveats that matter when you
read an attribution. No PyMC knowledge required.

For the *why* underneath — why each model was chosen, its strengths and
weaknesses, cited sources, and an assessment of the engine's overall statistical
rigor — see the
[statistics white paper](../knowledge/statistics_whitepaper.md).

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
recomputes from its components — `Σnumerator / Σdenominator`, never an average
of per-period ratios). **A node is fetched, fitted, and attributed at
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
  rather than failing the analysis. Two further statuses say a node was *not
  analyzed*, and both carry a `status_reason` naming the cause — read them as
  gaps in the analysis, never as "nothing happened here":
  `"fit_failed"` (the node's model could not be fitted, in practice a series
  with no variance across the window — an unlaunched feature, a stock held flat,
  a seasonal business's off-season) and `"attribution_failed"` (a formula node
  whose exact decomposition is not a finite number, in practice a zero
  denominator on some period in the window). An `attribution_failed` node still
  reports its own `baseline`, `actual` and `gap`, because those come from the
  data rather than the model — what is missing is the *split*, not the movement.
  Neither takes the rest of the tree down. Fits need ≥ 10 whole periods, so monthly
  nodes want roughly a year of history. **Treat that as a hard floor, not a
  recommendation.** A monthly node fit on ~12 periods is where every weakness in
  this document lands at once: the model carries one latent trend state per
  observation, so it is estimating more latent parameters than it has data
  points; a 2–3 month analysis window sits in the bootstrap's least reliable
  regime (see "Uncertainty" below); the block length for month grain is already
  the smallest it can be; and the trend is held flat across months rather than
  days. Monthly nodes are supported and the numbers are real, but read their
  intervals as the loosest in the tree, not the tightest.
- **Gaps are mean-per-period at each node's own grain.** In a mixed-grain
  tree, raw gaps of different-grain nodes are not comparable — compare
  `share_of_gap` and `ranked_causes` scores instead.
- Partial edge periods are dropped, never zero-filled, so a coarse metric's
  series can end before the raw data window does; the trend's flat forecast
  for a monthly node sits at the last *whole month* before the analysis
  window, which can be weeks before the anomaly.
- **A `flow` metric that starts partway into the loaded window is filled with
  zeros before its first row**, and those zeros are indistinguishable from
  measured ones once they reach the fit. This is the one fill the engine
  performs that is a *judgement* rather than a fact — a metric that did not
  exist yet is not a metric that was zero — so it is logged with the periods it
  invented, and you should read that warning. The fit trains on the invented
  periods, which gives the node a level shift and a trend it never had, and RCA
  can then rank it as a cause. If a node launched mid-window, the honest fix is
  a later `--start-date` for the tree, or a window that begins where the metric
  does. (`stock` refuses instead — it has nothing to carry backwards. `rate`
  neither fills nor refuses: see below.)

### Rates over undefined periods

A rate whose denominator is genuinely zero in a period — nobody churned that
week, nothing was on sale that day — has **no value** there. It is not low, and
it is not zero: `0/0` is undefined, and the canonical semantic-layer idiom
(`num / nullif(den, 0)`) returns NULL for exactly this reason. breakdown carries
that through rather than inventing a number, and the same representation stands
for a period the source simply never returned, because a provider cannot tell
the two apart. What it *can* do, once the rate declares its `denominator`, is
classify them after the fetch — denominator zero is a fact, denominator
non-zero is a missing value — and it says which in the startup log.

The policy at each consumer, stated once:

| Consumer | Policy |
|---|---|
| **The window aggregate** | A window's rate is `Σnumerator / Σdenominator`, which equals the *denominator-weighted* mean of the per-period rates. A period with a zero denominator contributes to neither sum, so it drops out and the window rate stays well-defined where the per-period rate is not. This is the same rule `resample_up` has always enforced in the time direction ("recompute from components, never average daily ratios"), now applied to the window. |
| **A rate with no declared `denominator`** | Falls back to the plain average of its *defined* periods. That is the pre-1.11 number and it is wrong whenever the denominators differ, so declare one wherever you can establish it. It stays a fallback rather than a parse error because some rates genuinely have none — a *median* load time is not `Σnum / Σden` for any pair of series, and a mean duration whose cohort is not a metric in the tree has no weight to offer. A wrong denominator publishes a confident wrong number; no denominator publishes this, disclosed. Every such node is named in the startup log and by `breakdown doctor`. |
| **Which of those two a number is** | Said in the payload, not left to be inferred. Every rate node carries `window_aggregate`: `components` (the real `Σnumerator / Σdenominator`), or one of `period_mean_none_exists` / `period_mean_undeclared` / `period_mean_weights_unavailable` — the same arithmetic in all three, three different facts about the world. A metric that *has* no denominator ([`no_denominator:`](yaml-reference.md#grains)) is reporting the only number that exists for it, and must not read as a tree somebody forgot to finish; `window_aggregate_reason` carries the author's own words for why. The UI prints the distinction as a label rather than a tooltip ("period means — no component aggregate exists"), because the exported report has no hover. This holds on every surface that reads the number: RCA and `/shapley` responses, and a fitted what-if's rate baselines (`POST /simulate`), which are the same window arithmetic. |
| **The what-if baseline** | Computed by the same rule (`Σnumerator / Σdenominator` over the baseline window), labelled the same way — and **refused loudly** when every period of the baseline window is undefined, because unlike RCA there is no per-node degrade worth having: a scenario's deltas propagate, so a baseline that does not exist poisons everything downstream of it. The same reasoning refuses any scenario whose arithmetic produces a non-finite number (422 naming the nodes) rather than encoding it. |
| **A window with no defined period at all** | The node has no value: `status: "undefined_over_window"`, with `baseline`/`actual`/`gap` all null rather than a fabricated number. |
| **The fit** | **Refused.** A period with no value cannot be trained on, and every imputation of it (zero, forward-fill, interpolation) is a fabricated observation the posterior would not know to widen for. Dropping the row is worse — model time, lags and the seasonal design are positional, so deleting a period silently re-dates every later one. The node reports `fit_failed` with the periods named, and only that node's attribution is lost. |
| **A formula node reading the rate** | Refused over any window containing an undefined period, with `attribution_failed` and the dates: a factor with no value has no decomposition. |
| **`_check_contiguous`** | Unaffected. Contiguity is a property of the *dates*, and an undefined period keeps its row — which is precisely why the value is carried in place rather than dropped. |
| **The block bootstrap** | Nothing new. Replicates that come out non-finite are dropped and the interval is withheld or computed from the survivors, under the existing `ci_status: "nonfinite_bootstrap_replicates"`. Where the rate's own aggregate is decomposed (`aggregation: "components"`), the resampling runs over the window's *defined* periods, so replicates and the exact value agree about which periods exist. |
| **`snap_window` / coverage validation** | Unaffected. Both ask whether the window's periods lie inside the node's data; an undefined period is inside it. |

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

### The reference window is not the training window

A natural misreading of RCA's four dates is that the model is fitted on the
reference window — and that a longer reference therefore buys a better model.
Neither is true. The fit window is **all loaded history before
`analysis_start`**, regardless of the reference dates; the reference window
only defines the **comparison baseline** the gap is measured against
(`gap = mean(analysis) − mean(reference)`). Widening the reference does
nothing for fit quality — widening `--start-date` does.

That baseline role is why "as long as possible" is the wrong instinct for the
reference. On a metric with any underlying trend, a very long reference turns
the gap into "current level vs long-run average", and the attribution dutifully
hands most of it to trend — coherent, and useless for incident analysis. The
model's trend component already measures drift explicitly; the reference should
isolate the *regime the analysis window departed from*.

When you omit both reference dates, RCA supplies the **matched adjacent
block**: the window ending the day before `analysis_start`, sized 4× the
analysis length with a floor of 28 days, rounded to a whole-week length when
any node in the target's ancestor scope declares seasonality (so the weekday
mix stays balanced), extended to hold at least one whole period at the
coarsest grain in scope, and clamped to the loaded data. Adjacent, so no trend
accumulates between the windows; a few multiples long, so the baseline mean is
stable without reaching into a different regime. The response echoes the
resolved window and sets `reference_defaulted: true`. Override it when you
have a deliberate baseline in mind — and if you pick a non-adjacent one on a
growing metric, expect trend to absorb part of the gap.

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
  days of formula(parents that day)`. This holds for the numbers RCA publishes,
  not only for `GET /shapley`: each contribution's `estimate` **is** the exact
  Shapley value, and the bootstrap supplies only its interval. Together with
  `unexplained` (below, computed from the same exact decomposition), the
  contributions reconcile with the node's own gap to machine precision.
  Compared to Shapley on window means,
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

**Closed form for a 2-parent product.** The three games above are general —
they hold for any formula, evaluated per day — but for the common case of a
formula `A × B` with exactly two parents, they collapse to a closed form
worth having memorized:

```
φ(A) = Δmean(A) × (mean_ref(B) + mean_an(B)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
φ(B) = Δmean(B) × (mean_ref(A) + mean_an(A)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
```

Each parent's share is its own mean shift, valued at the *average* of the
other parent's reference- and analysis-window means (the means-bridge term),
plus half of however much the two parents' within-window covariance shifted
between the two windows (the co-movement term, split evenly since a product is
symmetric in its two factors). The two `φ`s sum exactly to
`mean(A×B over analysis) − mean(A×B over reference)`, with no remainder —
which is also why a real behavioral change like "the large orders disappeared"
(an orders–AOV covariance shift) shows up in the attribution instead of
draining into `unexplained`.

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
component is absent. A third value, `"nonfinite_bootstrap_replicates"`, means
enough replicates evaluated to a non-finite number — again, a near-zero
denominator, this time hit only by some resamples — that the interval was
computed from the survivors or withheld entirely. Treat it the same way: the
point estimate is exact, the interval around it is not something the engine is
willing to claim.

A **constant parent** — an unlaunched feature, a stock held flat, a seasonal
business's off-season — makes every replicate resample the same number, so the
interval would come out exactly zero-width. That is not certainty, it is the
absence of information, and it is withheld: `ci_95: null` and
`prob_same_direction: null` with `ci_status: "degenerate_bootstrap_spread"`.
More generally, **no published `ci_95` is ever zero-width**; if you see one,
it is a bug.

**And no published direction probability is ever exactly 1.** It is a
proportion over 500 bootstrap replicates, so the only values it can take are
`k/500`: there is nothing between **0.998** and 1, and a count that saturates
— every replicate on one side — is the estimator running out of resolution,
not a measurement of certainty. Saturation is most likely exactly where the
evidence is thinnest, so it is published as the ceiling with a companion
`prob_same_direction_censored: true`, and every surface renders it as the
bound it is: **`>99.8%`**, never `100.0%`. Read that as "no replicate crossed
zero", which is a real and useful statement, and not as "the sign is certain",
which is not one this estimator can make. The same rule and the same flag
apply to `prob_concentrated` on slices and to `prob_direction` in what-if,
where the ceiling is set by the Monte Carlo's draw count instead. The one
exception is a what-if delta with *no spread at all* — an exact propagation
through an identity from a pinned intervention — which really is known and
keeps `1.0`.

> **Short windows are still short.** The block cap that used to halve the
> resampled variance is fixed, but two limits remain and both live in
> [S6](../knowledge/roadmap.md#statistical-rigor-s--a-standing-workstream),
> which will estimate block length from the data rather than taking it from a
> per-grain constant:
>
> - Under serial dependence, a short window simply carries little information
>   about the series' long-run variance — measured at 0.17–0.61 of the true
>   sampling variance across every block length tried, on an AR(1) at ρ=0.6.
>   That is inherent to the window, not a defect in the estimator.
> - The daily block constant of 7 **resonates with a weekly cycle**: a 7-day
>   block contains each weekday exactly once, so a weekly seasonal component
>   cancels identically in every replicate and the interval comes out narrower
>   than at neighbouring block lengths. Measured on the bundled demo tree, the
>   published interval width at block 7 is roughly a third of its width at
>   block 3. Read a daily-grain interval on a weekday-seasonal metric as
>   optimistic until S6 lands.

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
  the fitted trend.

  Its CI comes from the posterior of that last state, not from forward
  simulation of new steps. **That is an understatement, not just a design
  note** (open, roadmap S16): a random walk accumulates variance with every step
  past the fit end, so the honest interval should widen the further the analysis
  window sits from the fit, and this one does not — a one-day analysis window
  and a ninety-day one starting the same date report the identical trend
  estimate *and* the identical interval. The flat *point* forecast is correct
  for a local level (roadmap S8 / 3.4 address nodes with genuine momentum); the
  flat *interval width* is not. `components.trend` is usually the number that
  gets narrated, so treat it as the least conservative figure in the response.

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
- For a **derived** node — a formula node with no `source` of its own, whose
  series *is* `formula(parents)` — `unexplained` is `0` **by construction**, and
  the response says so with `unexplained_status: "definitional"`. That zero is
  not a reconciliation: nothing was compared, because there is no independently
  measured series to compare against. Read it as *we did not check*, and if you
  want the check, give the node a `source`. (breakdown then verifies the
  identity against the fetched series at **load**, over the whole window, and
  warns with the worst periods — so drift is reported even where no RCA looks.)

`unexplained_status` is `"measured"` everywhere else, including on every
probabilistic node. The distinction exists because a zero that was measured and
a zero that was assumed look identical on a screen, and reading the second as
the first reports a verified identity nobody verified.

A large `unexplained` is a finding, not an error — it says "neither the parents
you modeled nor the fitted trend/seasonality account for this move."

### `share_of_gap` can exceed 100%

Shares are `contribution / gap` and are not clamped. Two parents can push in
opposite directions (one +145%, one −45%), which is exactly what happened and
worth seeing. The UI clamps only the *edge width*, never the numbers.

### `ranked_causes` is a heuristic

The ranking propagates a score from the target upward. Each hop weights a
parent by `min(|share|, 1)` divided by the **total** gross share its siblings
had to move — so a node whose parents largely cancel passes on less influence
than one whose parents agree, and a parent explaining 165% of a gap scores
*below* one explaining a clean 80%. A share far past 100% means the
decomposition needed a lot of offsetting movement to land on a small net gap,
which makes the split *less* well-identified, not more. Because a node's
parents' weights sum to at most 1, a hop can never inflate influence.

It remains a triage ordering — "look here first" — not a probability. For
rigor, read the per-node contributions and their credible intervals.

Only paths that actually carry influence appear: a node no hop ever reached is
absent from `ranked_causes` entirely (`via` is never `null`), while a node that
*was* reached and explains nothing stays, scored 0.0, with the child it was
reached through. The full inventory of what was in scope — including nodes that
could not be analyzed at all — is `nodes`, not this ranking.

### Multiplicity: a ranking is a search

One RCA on a mid-sized tree produces on the order of thirty intervals and
`prob_same_direction` values, and then sorts them. Two consequences worth
holding onto:

- **The winner's interval was computed before it was selected.** A
  pre-specified node's `ci_95` means what this document says it means. The
  interval on the *top of a ranking* does not carry the same guarantee — the
  maximum of many noisy quantities is biased upward, and some intervals will
  exclude zero even when nothing is happening.
- **Re-running with different windows is a search.** The window pair is a free
  choice with no cost to retrying, so trying several and reporting the one that
  produced the clearest story is the same failure mode this engine exists to
  avoid. Pick the window from the incident, not from the result.

The engine does not currently correct for this (roadmap S15). The practical
defence is to state your target node and windows before you look.

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
   only declare seasonality your window can actually see (≥2 full periods
   inside the *fit* window, which for RCA ends at `analysis_start`). The fit
   reports both failure modes in `seasonality_warnings`: too little data for
   the period, and harmonics the period itself cannot support (a cycle needs
   more than two grain steps to be told apart from the level, so `period` must
   be ≥ 3 and the second harmonic is dropped below `period: 5`).
5. **Window means hide within-window shape.** A spike-and-recover pattern and
   a level shift can have the same window mean. Choose windows that isolate
   the regime you care about, and look at the time-series panel.
6. **ADVI vs NUTS.** ADVI (the RCA default) is a fast approximation that can
   understate uncertainty; NUTS is the gold standard and reports convergence
   diagnostics (R̂ < 1.05 is healthy). Triage with ADVI, confirm with NUTS.

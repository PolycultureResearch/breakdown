# The Statistics of Breakdown

**A white paper on the models behind Bayesian metric trees, why each was chosen,
and where each one stops being trustworthy.**

> **Written:** 2026-08-04 · **Last updated:** 2026-08-30 ·
> **Engine version:** 0.1.0
>
> **This is a living document.** The assessment in §3 and the improvements in §4
> describe the engine *as it stands on the last-updated date above*. Both
> sections carry status markers so you can tell a known current issue from one
> that has since been fixed:
>
> **○ open** · **◑ in progress** · **✅ shipped**
>
> Items are tagged with a roadmap ID — `S1`, `S2`, … for the
> [statistical rigor](roadmap.md#statistical-rigor-s--a-standing-workstream)
> workstream, and `C1`, `C2`, … for
> [Horizon 0](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend),
> the correctness gate. The difference is worth knowing: an **S** item is a
> disclosed limitation, a **C** item is a defect where the engine's behavior and
> its documentation disagree. **The [roadmap](roadmap.md) is the single source of
> truth for status and sequencing** — this paper holds the *statistical
> rationale* (why the gap matters, what the literature says, what "fixed" would
> mean) and does not duplicate the schedule. If the two ever disagree, the
> roadmap is right and this paper is stale. See the
> [revision history](#revision-history).

---

## Who this is for

You work with data. You know what a confidence interval is, you have opinions
about A/B tests, and you have shipped a regression or two. You are not
necessarily a Bayesian statistician, and you should not have to become one to
decide whether to trust a number breakdown puts in front of you.

This paper describes every statistical model in breakdown: what it does, why it
is the right tool for that job, and — the part most tool documentation
skips — where it breaks. It closes with an honest assessment of the engine's
current rigor and a prioritized list of what would make it better.

Two companion documents sit next to this one. [`docs/model.md`](../docs/model.md)
is the practitioner's guide to *reading output* — what a specific field means
when you are staring at it. This paper is the *why* underneath that. The
[roadmap](roadmap.md) is where the improvements identified in §4 get scheduled.

---

## 1. The approach

### 1.1 The problem

A business metric moved. Revenue is down 8% week over week. Somebody has to
explain why, and the honest answer is almost never a single cause — it is a
combination of several drivers, some of which moved a lot and matter little,
some of which moved a little and matter enormously, and some of which did not
move at all but are being blamed anyway.

The standard tooling response is anomaly detection ("revenue is 2.3σ below
trend") or flat dimensional slicing ("revenue fell most in Brazil"). Both are
useful and both stop short of the question. Anomaly detection tells you
*that* something happened. Flat slicing tells you *where* it showed up. Neither
tells you *which lever moved*, because neither knows that revenue is orders ×
average order value, that orders come from sessions via a conversion rate, and
that the conversion rate has been sliding for three weeks.

breakdown's premise is that the analyst already knows that structure, and that
writing it down is what makes the question tractable.

### 1.2 Five commitments

Everything downstream follows from five choices, each of which trades some
convenience for some honesty.

**1. The DAG is a declared hypothesis, not a discovery.** You write down the
metric graph. breakdown quantifies the edges you declared; it never invents
one. This is a real constraint — an unmodeled driver cannot be found — but it is
also what makes attribution meaningful. Causal *discovery* from observational
business time series is not a solved problem, and a tool that pretended
otherwise would be selling confidence it does not have. The DAG is the premise,
not a gap. (See §2.7 for what the DAG does and does not license you to claim.)

**2. Probabilistic, never frequentist.** Every relationship breakdown measures
comes out as a posterior distribution with a credible interval. There are no
p-values, no significance stars, no Pearson correlations. This is not
aesthetics. The question a stakeholder asks is "how much of the drop did
conversion cause, and how sure are you?" — which is a question about the
*probability of a parameter given the data*. That is exactly what a posterior
is, and exactly what a p-value is not. A 95% credible interval means there is a
95% probability the value lies in that range given the model and data; a
confidence interval means something considerably more contorted that almost
nobody reports correctly.

**3. Exact where exactness exists.** Some edges in a metric tree are
arithmetic identities: `revenue = orders × aov` is not a statistical
relationship to be estimated, it is a definition. Fitting a regression to an
identity would be both wasteful and misleading — it would put a credible
interval around a fact. breakdown splits the graph: **deterministic edges get
exact combinatorial attribution** (§2.3), **probabilistic edges get Bayesian
inference** (§2.1). Mixing the two in one tree is the central design decision,
and the reason attribution can sum correctly.

**4. Never ship a number the engine cannot defend.** Three mechanisms enforce
this. Every attribution carries a credible interval. Every node reports an
explicit **`unexplained`** term rather than silently distributing the remainder
across whatever happened to be modeled. Every fit carries convergence
diagnostics, and a fit whose geometry the sampler struggled with is flagged
`suspect` in the response rather than quietly returned. A large `unexplained`
is a *finding* — "neither your declared parents nor the fitted trend account
for this" — not an embarrassment to be hidden.

**5. Fit on the normal regime; measure the departure.** RCA fits every node on
data strictly *before* the analysis window. If the anomaly were in the training
data, a flexible trend would absorb it as drift and the coefficients would be
dragged toward a compromise between "normal" and "incident." Excluding it means
the coefficients encode the normal-regime relationship, so `β × Δparent`
answers the question actually asked. This mirrors the pre-period/post-period
structure of CausalImpact (Brodersen et al., 2015).

### 1.3 What comes out

For a target metric and two time windows, breakdown returns: a per-node gap
(the window-mean difference), a per-parent contribution with a credible interval
and a direction probability, an explicit `unexplained` remainder, a
trend/seasonal decomposition for fitted nodes, and a heuristic ranking of causes
for triage. The rest of this paper is how each of those is computed.

---

## 2. The models

### 2.1 Bayesian Structural Time Series

**Where it is used.** Every probabilistic edge in the tree — any node with
parents but no `formula`. Also source nodes (no parents), where it decomposes a
series into trend and seasonality.

**What it is.** A structural time series model decomposes an observed series
into interpretable additive components, each with its own dynamics. In
breakdown's normalized (z-scored) space:

```
y[t] = α + trend[t] + seasonal[t] + Σᵢ βᵢ·xᵢ[t] + ε[t]
```

- **`trend[t]`** is a *local level*: a random walk, `cumsum(σ_trend · z[t])`
  with `z[t] ~ Normal(0,1)`. It absorbs slow drift that no parent explains.
- **`seasonal[t]`** is a Fourier expansion — sin/cos harmonic pairs at each
  declared period.
- **`βᵢ`** are the regression coefficients on the node's parents, each parent
  optionally shifted back by a declared lag.
- **`ε[t]`** is observation noise.

The lineage runs from classical state-space structural models (Harvey, 1989;
Durbin & Koopman, 2012) through the Bayesian treatment popularized by Google
in **Scott & Varian (2014)**, "Predicting the Present with Bayesian Structural
Time Series," and applied to causal inference in **Brodersen et al. (2015)**,
"Inferring causal impact using Bayesian structural time-series models" — the
paper behind the CausalImpact package. breakdown's use is closest in spirit to
the latter: fit the normal regime, then ask what the departure means.

**Why it fits.** Four properties make BSTS the right backbone here:

1. **The components are the explanation.** A business stakeholder does not want
   a black-box forecast; they want "this much was seasonality, this much was the
   underlying trend, this much was your parents moving." A structural model
   hands you that decomposition directly, because those components are literally
   its parameters. A gradient-boosted forecaster with equivalent accuracy would
   answer none of the questions RCA asks.
2. **Uncertainty composes.** Because the coefficients are posteriors rather than
   point estimates, a contribution `β × Δparent` is a distribution, and
   multi-hop propagation through the tree (§2.6) can carry draw-aligned
   uncertainty from one hop to the next.
3. **Priors are how domain knowledge enters.** An analyst who knows a session is
   worth roughly 0.1 orders can say so. On short series — the common case in
   business analytics, where you may have 60 useful observations — that prior is
   doing real work that no amount of clever fitting substitutes for.
4. **The trend is a regularizer with a knob.** The step-size prior
   (`σ_trend ~ HalfNormal(0.05)` by default) is deliberately tight, so the level
   drifts slowly and the *parents* are forced to carry the movement. Loosening
   it per node is an explicit statement that this node has fast level changes
   its parents do not capture.

**Implementation notes that matter statistically.**

- The trend uses a **non-centered parameterization** (sampling unit normals and
  scaling by `σ_trend`) rather than a centered one. A centered hierarchical
  random walk produces the "funnel" geometry (Neal, 2003) that Hamiltonian
  samplers handle badly and mean-field variational inference fails on outright.
  The non-centered reparameterization is standard practice for exactly this
  reason (Papaspiliopoulos, Roberts & Sköld, 2007; Betancourt & Girolami, 2015).
- **Priors are stated in business units and rescaled internally.** You write
  `mu: 0.1` meaning "0.1 orders per session"; the engine converts to z-scored
  space via `scale = x_std / y_std`. Because that scale uses sample statistics
  from the fit window, the effective prior is mildly data-dependent — an
  empirical-Bayes-adjacent compromise, and an acknowledged one. It is what makes
  business-unit priors possible at all, and it is a place where a purist would
  object.
- **Formula nodes still get a BSTS**, fitted to the *residual*
  `observed − formula(parents)`. The identity is treated as exact, so there are
  no coefficients to learn; the fit captures what the identity does not — data
  noise, definition drift, upstream pipeline changes.

**Limitations.**

- **Linearity.** Effects are linear in the parents. A genuinely nonlinear or
  saturating relationship (diminishing returns on ad spend is the canonical
  case) is approximated by a line through the observed range and will look
  weaker than it is outside that range.
- **Trend/parent competition.** The trend and a slow-moving parent are partially
  confounded — both explain gradual drift. The tight default prior tilts the
  contest toward parents deliberately, which is a *choice*, not a neutral
  default: if a node truly has autonomous drift, that drift will be pushed onto
  the parents. A β credible interval that straddles zero after loosening
  `trend.sigma` is the diagnostic.
- **Scale confounding.** Regressing a dollar flow on a user count when both grow
  with the business learns "bigger base → more of both," not the per-user effect
  the author meant. breakdown ships an `expected_signs` diagnostic that flags
  when the posterior contradicts a declared direction, precisely because this
  failure is common and silent. The cure is remodeling the edge as rates on
  rates, not clamping the sign.
- **Short series.** Business metric trees routinely offer 30–90 usable
  observations per node. The posteriors are correspondingly wide, and they
  should be. This is honesty, not a defect — but it does mean many real
  questions come back with intervals too wide to act on, and no statistical
  method fixes a data shortage.
- **Local level only.** The trend has no slope ("local linear trend") or
  damping component. A node with genuine momentum is modeled as a level that
  happened to move.

### 2.2 Inference: NUTS and ADVI

**Where they are used.** **NUTS is the default everywhere** — `POST /analyze`,
`POST /rca/{name}`, `POST /simulate` and the MCP tools alike. ADVI is an
explicit `?inference_method=advi` opt-in on the three HTTP routes, for a tree
wide or fine-grained enough that exact sampling is impractical. That was not
always so: until 2026-08-24 the on-demand fits inside RCA and simulation used
ADVI, and §3.2 #1 records what the PSIS measurement said about it.

**What they are.** **NUTS** — the No-U-Turn Sampler (**Hoffman & Gelman, 2014**)
— is an adaptive form of Hamiltonian Monte Carlo that explores the posterior by
simulating physical trajectories, automatically tuning path length. It is
asymptotically exact: run it long enough and you have the true posterior.
**ADVI** — Automatic Differentiation Variational Inference (**Kucukelbir et
al., 2017**) — instead *optimizes*: it fits the closest tractable distribution
(here, a mean-field Gaussian, i.e. independent per parameter) to the posterior
by maximizing a lower bound on the evidence (the ELBO).

**Why both, and why the default is the exact one.** The obvious posture is
*triage with ADVI, confirm with NUTS*, and that is what this engine did. Two
measurements retired it (2026-08-22 to -24, §3.2 #1). First, mean-field fails
its PSIS k̂ check on essentially every real node here — a factorized Gaussian
cannot hold one correlated local-level latent per period — and the damage
reaches published point estimates, in both directions, by tens of percent.
Second, the latency it was bought with is small: the 106-metric reference tree
holds exactly five probabilistic fits, and fitting all five measured 31.3s under
ADVI against 28.0s under NUTS — the exact sampler was the *faster* of the two.
Where the approximation does still save time (an RCA's shorter fit windows) it
is tens of seconds, and it does not pay for coefficients that move by 57%.

So ADVI stays, as a stated opt-in for the case where the trade really does
bite, and every fit it produces reports its k̂. `?inference_method=advi` is a
one-parameter change on `/rca`, `/simulate` and `/analyze` alike, and
`?inference_method=nuts&fit_end=<analysis_start>` still re-runs a single node
exactly.

**Diagnostics.** Every fit returns `fit_quality`, and nothing is silently
returned as trustworthy:

- **NUTS** is flagged `suspect` when divergences exceed 1% of draws, any R̂
  exceeds 1.05, or any bulk effective sample size falls below 100. R̂ compares
  within-chain to between-chain variance (Gelman & Rubin, 1992; the modern
  rank-normalized form in Vehtari et al., 2021); divergences indicate the
  sampler hit curvature it could not integrate through — usually a geometry
  problem, and the reason the trend is non-centered.
- **ADVI** is flagged `suspect` when the ELBO is still moving at the end of
  optimization by more than half its recent noise level — i.e. it had not
  converged.

**Limitations.**

- **Mean-field ADVI misstates uncertainty, and misplaces point estimates.**
  This is the important one, and it is why ADVI is no longer a default. By
  assuming parameters are independent it cannot represent posterior
  correlation, so on this engine's β-vs-trend ridge it collapses the trend
  delta toward zero and lets β absorb the difference — in whichever direction
  that node's ridge runs, which is why the error is not correctable from the
  payload. Yao et al. (2018) is the standard treatment.
  [`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md) works through
  the mechanism and a decision it would send the wrong way; §3.2 #1 carries the
  measurements. **A response is only exposed to this when it was asked for**,
  and it then reports the k̂ that says how far off it is.
- **The ADVI diagnostic used to be weak, and is not any more.** An
  ELBO-convergence check confirms the optimizer stopped moving; it does *not*
  confirm the approximation is close to the true posterior, and a
  well-converged bad approximation passes. Every variational fit now also
  carries **PSIS k̂** (Yao et al., 2018) in three bands, and `fit_quality` goes
  `suspect` when either check fails (§3.2 #4).
- **R̂/ESS need multiple chains.** They are NaN on single-chain traces, and
  missing values do not flag.

### 2.3 Exact Shapley attribution on deterministic identities

**Where it is used.** Every `formula` node — arithmetic identities like
`revenue = orders × aov` or `signups = organic + paid`.

**The problem it solves.** When `revenue = orders × aov` and both factors
moved, how much of the revenue change belongs to each? This is genuinely
ambiguous, because the change includes an interaction term: some of the revenue
gain happened because *more* orders were each worth *more*. Every business
intelligence tool that has ever shipped a "price/volume/mix" report has picked
some convention for splitting it, usually without saying which.

**What breakdown does.** It uses the **Shapley value** (**Shapley, 1953**), the
unique allocation satisfying four axioms — efficiency (contributions sum exactly
to the total), symmetry (identical factors get identical credit), null player
(a factor that did not move gets nothing), and additivity. Young (1985) gives
the alternative monotonicity-based characterization. Shorrocks (2013) is the
canonical treatment of applying it to *decomposition* problems rather than
cooperative games, which is what this is.

The concrete computation enumerates all `2ⁿ` subsets of parents, computes the
formula with each subset at analysis-window values and the rest at
reference-window values, and averages each parent's marginal contribution over
all orderings.

**The three-game refinement.** A subtlety worth stating, because it was a real
bug. Attributing on *window means* alone throws away within-window co-movement:
for `revenue = orders × aov`, the pattern "the big orders disappeared" is a
shift in the orders–aov covariance, and a means-only decomposition dumps it into
`unexplained`. breakdown therefore evaluates **both windows period-by-period**
and sums three exact Shapley games:

```
φ_parent = φ_means + φ_covariance_analysis − φ_covariance_reference
```

The parts telescope, so contributions sum to the true gap exactly, for windows
of any (even unequal) lengths. A covariance that is merely *present* but
unchanged cancels and contributes nothing; a covariance that *shifted* is
attributed to the parents. `unexplained` on a formula node is then measurement
residual only — an exact identity yields zero up to floating point.

The window-aggregate form of this is the **Bennet indicator decomposition**
(Bennet, 1920; see Balk, 2008 for modern index-number treatment), which for a
two-factor product is exactly the two-player Shapley value. breakdown surfaces
that as the "headline" view — the familiar price/volume bridge, with the
interaction as its own labeled row rather than silently split.

**Why it fits.** It is *exact*. There is no estimation, no interval, no model
risk: given the parent series, these numbers are arithmetic. For the large
fraction of a real metric tree that is definitional, this is strictly better
than fitting anything. The axioms also give a defensible answer to "why did you
split the interaction that way?" — a question every price/volume report invites
and few can answer.

**Limitations.**

- **Exponential cost.** `2ⁿ` subsets means a formula node with many parents is
  expensive. In practice metric-tree identities have 2–4 parents, so this is
  theoretical, but a wide sum node would hurt.
- **Shapley is a *convention*, not a truth.** The axioms are appealing and the
  answer is unique *given* them, but "how much of the interaction belongs to
  price versus volume" has no physical fact of the matter. Alternative
  decompositions (Fisher, Törnqvist, Sun) split it differently and are not
  wrong. breakdown's choice is defensible and disclosed; it is not the only one.
- **It attributes, it does not explain.** Shapley says orders account for 80% of
  the revenue gap. It says nothing about *why* orders moved — that is the next
  hop up the tree, which is the whole point of having a tree.
- **Not the same as SHAP.** Lundberg & Lee (2017) apply Shapley values to
  *machine-learning feature attribution*, a different problem with different
  baseline semantics. Shared mathematics, unrelated interpretation.

### 2.4 Posterior attribution on probabilistic edges

**Where it is used.** Every fitted (non-formula) node during RCA.

**What it is.** Parent *i*'s contribution is the distribution
`beta_raw[i] × (parent's window gap)`, where `beta_raw` is the coefficient in
business units — d(child)/d(parent) — and the gap is the parent's own
window-over-window change. For lagged parents the gap is measured over windows
shifted back by the lag, so the parent values that actually influenced the
analysis window are the ones read. The result is summarized as a mean, a 95%
interval, and `prob_same_direction`.

Because the model already decomposed the series, RCA reports the fitted
**trend** and **seasonal** deltas explicitly rather than lumping them into the
remainder. `unexplained = gap − Σ contributions − trend − seasonal` is then
observation noise plus genuine model misfit.

**Why it fits.** It is the direct posterior answer to the question asked, it
inherits full uncertainty from the coefficient posterior, and separating out
trend and seasonality means a stakeholder is not told "conversion caused it"
when the truth is "your reference window had 1.4 weekends and your analysis
window had 2."

**Limitations.**

- **Linear extrapolation.** `β × Δparent` assumes the coefficient learned on the
  normal regime holds at the anomaly's magnitude. For a large excursion this is
  exactly where linearity is least safe.
- **Correlated parents split credit unstably.** Two collinear parents will have
  individually wide, negatively correlated coefficient posteriors. Their *sum*
  is well determined; the split between them is not. Nothing currently flags
  parent collinearity, and this is a real gap (§4.2).
- **The trend forecast is flat.** The analysis window lies outside the fit
  period, and a random-walk local level forecasts flat at its last fitted state.
  So the reported analysis-window trend is `trend[last fitted period]`, with a CI
  from the posterior of that state rather than forward simulation. For a node
  with real momentum this understates the counterfactual — and it is precisely
  what a full posterior-predictive counterfactual (§4.1) would fix.

### 2.5 The block bootstrap for window uncertainty

**Where it is used.** Every RCA node, both attribution methods, and slice
attribution.

**The problem it solves.** A window mean over a handful of periods is itself a
noisy estimate. "What happened this weekend?" is a two-day window; its mean has
a standard error that shrinks only as 1/√n. If attribution intervals reflected
only *coefficient* uncertainty, a two-day RCA would look as confident as a
two-month one, which is absurd.

**What breakdown does.** A **circular moving-block bootstrap**: resample
contiguous blocks of periods (up to 7 days, 4 weeks, or 2 months by grain),
wrapping circularly, and recompute the window means per replicate. The block
structure preserves serial dependence that an i.i.d. bootstrap would destroy.
Blocks are resampled **jointly across the node's parents**, preserving
cross-metric correlation within the window. The method traces to **Künsch
(1989)** and the circular variant to **Politis & Romano (1992)**; Lahiri (2003)
is the standard reference.

For formula nodes, this is the *entire* source of CI — the relationship is
exact, but the window means feeding it are not. For probabilistic nodes it
composes with the coefficient posterior.

**Why it fits.** It is assumption-light (no distributional form required),
it handles the serial correlation that business time series always have, and it
is the right answer to a question users actually ask ("is this just noise?").

**Limitations.**

- **Fixed block lengths.** Block length is a constant per grain, not estimated
  from the data. Data-driven selection exists (Politis & White, 2004) and is not
  implemented. Too-short blocks understate uncertainty; too-long overstate.
- **Within-window stationarity is assumed.** A window containing a step change
  mid-way violates it.
- **Single-period windows degenerate.** With one period, every replicate is
  identical and the resampled variance collapses to zero. breakdown detects this
  and **withholds** the interval (`ci_status: "degenerate_single_period"`)
  rather than reporting a spurious zero-width CI — the right call, and worth
  noting as an example of the "never ship an indefensible number" commitment.
- **It is not a posterior.** The bootstrap interval is a frequentist resampling
  construct composed with a Bayesian posterior. The composition is pragmatic and
  common, but it is not a coherent joint posterior, and a purist would want the
  window-sampling uncertainty inside the model instead (§4.3).

### 2.6 Steady-state simulation and the do-operator

**Where it is used.** The what-if machine — `POST /simulate`.

**What it is.** Given a baseline window and a set of interventions ("sessions
+15%"), breakdown propagates implied deltas down the tree. **Intervened nodes
follow do-operator semantics** (**Pearl, 2009**): the node is severed from its
own structural equation, its parents' deltas are ignored, and its value is set
exactly. This is the formal difference between "sessions *are* 15% higher"
(conditioning) and "we *made* sessions 15% higher" (intervening), and it is the
difference that makes the answer actionable.

Formula nodes propagate exactly; probabilistic nodes propagate through their
`beta_raw` posterior. Uncertainty is **draw-aligned Monte Carlo**: each node's
delta is a vector of draws and the draw index is preserved end to end, so an
optimistic coefficient at hop 1 feeds the same draw at hop 2. This composes
uncertainty correctly through multi-hop paths, which interval arithmetic
notoriously does not. Per-source decomposition is again an exact Shapley over
the active sources.

**Why it fits.** The DAG is exactly the structure the do-operator needs, so
intervention semantics come almost free — and getting this *wrong* (propagating
an intervention backwards into parents) is a classic and costly error.

**Limitations.**

- **Steady-state only.** The answer is "where does this land at equilibrium,"
  with no transition dynamics. Lags are deliberately ignored, on the argument
  that a lagged effect still fully arrives eventually. If you need "what does
  next Tuesday look like," this is the wrong tool.
- **The causal claim is only as good as the DAG.** Do-operator semantics are
  valid *given* the graph. If a confounder is missing, the intervention estimate
  is biased, and no amount of correct propagation fixes that (Pearl et al.,
  2016; Hernán & Robins, 2020). The math is right; the premise is yours.
- **Trend and seasonality cancel.** They are assumed unchanged by an
  intervention, which is reasonable for a level shift and wrong for an
  intervention that changes the *shape* of demand.

### 2.7 Cold start: reasoning from priors alone

**Where it is used.** Trees declaring `provider: none` — a business with no
history yet.

**What it is.** The full what-if machine with zero data. Each node's operating
point comes from an asserted `baseline: [low, high]`, read as the central 90%
interval of a Normal — or of a LogNormal (`distribution: LogNormal`, the
natural shape for an order-of-magnitude belief about a positive quantity) —
sampled per draw and truncated to the node's declared `plausible` bounds by
rejection resampling (C7): the belief keeps its shape inside the bounds, with
no mass piling up on them. Each edge's slope is sampled directly from its YAML
prior. With nothing to fit, **the prior *is* the coefficient distribution** —
which is not a workaround but the correct Bayesian statement of the situation.
One guard follows from taking the arithmetic seriously: a formula dividing by
a belief whose draws cross zero is refused, because the resulting ratio is
Cauchy-like and its Monte-Carlo mean does not exist — a summary of it would be
a seed artifact, not a statistic.

**Why it fits.** This is arguably the purest expression of the Bayesian stance
in the product. A founder with no data still has beliefs, and those beliefs have
uncertainty. Composing them coherently through a DAG and propagating the
uncertainty is strictly better than the spreadsheet alternative, which is a
single confident number with the uncertainty deleted. The source waterfall
doubles as **sensitivity analysis**: whichever beliefs dominate the outcome's
spread are the ones worth spending money to measure first. This is prior
predictive reasoning in the sense of Gelman et al. (2020).

**Limitations** — all of which ship in every cold-start response:

- **A cold-start interval is not evidence.** It answers "if my stated ranges are
  honest, where does this land," never "what does the data say." It can compare
  scenarios; it can never confirm a belief.
- **Beliefs are sampled independently.** Correlated beliefs ("if price lands
  high, conversion lands low") are not representable, so intervals are too
  narrow or too wide wherever beliefs genuinely co-vary. This is the most
  significant modeling gap in cold start.
- **Unflagged means unchecked.** Extrapolation warnings come from declared
  `plausible` bounds; a node without them is never flagged, which means
  unchecked, not safe.

### 2.8 Dimensional slice attribution

**Where it is used.** `POST /rca/{name}/slices` — "the gap is in `orders`; where
inside `orders`?"

**What it is.** Closed-form attribution of a node's gap across the values of one
declared dimension, in the same mathematical family as §2.3:

- **Flows and stocks** are exact sum identities (`signups = Σ_g signups_g`).
  Linearity collapses the Shapley decomposition: each slice's attribution is
  exactly its own window-mean change, summing to the gap with zero remainder.
- **Rates** blend (`r = Σ_g s_g·r_g`, with shares from a declared weight
  metric). The exact symmetric split is again the **Bennet decomposition**:
  `within_g = s̄_g·Δr_g` (this slice's rate moved) and `mix_g = r̄_g·Δs_g`
  (traffic shifted toward or away from it). Since `Σ_g Δs_g = 0`, the mix terms
  are a pure reallocation signal.

Ranking is by **excess concentration**, not raw size:
`excess_g = contribution_g − baseline_share_g × gap`. The biggest slice always
has the biggest raw contribution, which makes raw ranking useless; excess asks
how much *more* of the gap a slice carries than its size predicts, and sums to
zero across slices. Uncertainty comes from the same block bootstrap (§2.5),
resampled jointly across slices — no per-slice model fits.

**Why it fits.** Flat slicing is a commodity; *tree × slice* is not. Separating
"which metric moved" from "where inside it" keeps both questions answerable, and
excess-based ranking is what stops the tool from breathlessly reporting that
your largest market is also your largest mover.

**Limitations.**

- **Localization is not explanation.** A concentrated slice says where to look
  next. It does not say why.
- **One dimension at a time.** No automated tree × slice search, so a cause
  visible only in a *combination* of dimensions (mobile × Brazil) is not found.
- **Rate slicing needs a declared weight metric,** and a wrong one silently
  produces a wrong mix/within split.

### 2.9 Grain: what counts as one observation

Not a model, but a statistical decision that shapes every model above, and one
most metric tools get wrong.

Every node declares a natural `grain` (day/week/month) and a `kind` (flow
sums, stock takes last, rate recomputes from components). **A node is fetched,
fitted, and attributed at its own grain, never below it.**

This is a statement about information, not formatting. A monthly snapshot
forced onto a daily spine is thirty identical rows carrying *one* observation —
and a model fitted to it will produce a posterior roughly √30 too narrow.
Equally, a conversion rate computed per-day on low-volume days is mostly
division noise, and per-day attribution to volume-versus-rate becomes wild
enough that the bootstrap has to paper over it. Fitting at the natural grain
yields fewer observations and *honestly wider* posteriors, which is the correct
trade.

Consequences: `t`, lags, and seasonality periods are all grain steps; finer
flow/stock parents resample up (rates never auto-resample); windows snap per
node to whole periods; and raw gaps across different grains are not comparable
(compare `share_of_gap` instead).

**A window's rate is `Σnum / Σden`, not the mean of per-period ratios**
*(roadmap 1.11, shipped 2026-08-13 — this changed published numbers).* "Kind:
rate recomputes from components" now extends from the grain resample to the
window aggregate itself. When a rate declares its `denominator`, the value
reported for a window is the component aggregate — equivalently, the
denominator-weighted mean of the per-period rates — rather than the unweighted
average of daily ratios, which overweights exactly the low-volume periods §2.9
identifies as division noise. Measured on the White Cube tree, the two
arithmetics differ by 0.02% to 10.9% per node, so every rate's `baseline`,
`actual` and `relative_change` moved when this landed. Three consequences with
stated policies. *(i)* A period whose denominator is zero has **no** rate — it
drops out of both sums, so the window value stays defined where the per-period
ratio is not; a window with no defined period at all reports
`undefined_over_window` rather than a number. *(ii)* The **fit refuses** a
series with undefined periods (`fit_failed`, periods named) rather than
imputing — filling asserts an observation that does not exist, and dropping the
row re-dates every later period against the positional spine §2.10 defends;
`S21` in §4 holds the candidate third option (mask the likelihood). *(iii)* A
rate that declares **no** denominator still gets a number — the mean of defined
per-period ratios — but the payload labels which arithmetic ran
(`window_aggregate`: `components` or one of three `period_mean_*` statuses,
with the reason), because the two aggregates genuinely differ and an unlabelled
fallback would have the payload misdescribe its own arithmetic. Rates that
*legitimately* have no denominator (a median, a duration over an uncarried
cohort) declare `no_denominator: "<reason>"`; the case for keeping the field
optional and gating on `doctor` instead of the parser is
[`rate_denominator_policy.md`](rate_denominator_policy.md).

### 2.10 Guarding the inputs

The most dangerous statistical failure is not a wide interval — it is a
plausible number that is quietly wrong. breakdown validates the inputs that
would produce one:

- **Window ordering and overlap.** Overlapping reference and analysis windows
  are rejected outright: a shared period would count as both the normal regime
  and the departure from it.
- **Window coverage.** A window only *partly* covered by the data would silently
  average whichever periods happened to exist — a reference mean over 30
  requested days computed from the 4 that loaded. Both windows must lie fully
  inside the node's data, and lagged parents are checked on their *shifted*
  windows, with errors naming the parent, the lag, and the shifted dates.
- **A gap-free date spine.** Model time is positional (`t = arange(n)`), lags
  shift by rows, and bootstrap blocks are contiguous runs. A hole in the date
  spine compresses the calendar so that a lag of 7 rows stops meaning 7 days —
  and nothing would raise. Frames with gaps are rejected, naming the missing
  periods.
- **Seasonality identifiability.** Two distinct checks. A component needs ≥2
  full periods inside the *fit* window to be learnable at all, which is a data
  shortage and warns. Separately, a Fourier harmonic *k* carries `k/period`
  cycles per step, so the Nyquist limit (Shannon, 1949) requires `2k < period`;
  below it the design column is identically zero or collinear with another, and
  the parameter is **pure prior** — sampled but never informed by data. Fitting
  two harmonics unconditionally meant `period: 2` fit three such parameters,
  `period: 3` two, and `period: 4` one, letting a model report seasonal
  structure it invented rather than learned. Harmonics are now Nyquist-filtered
  and periods below 3 rejected at parse time.

---

## 3. How rigorous is breakdown today?

An honest assessment, in the spirit of the fourth commitment.

### 3.1 What is genuinely solid

- **The deterministic half of the engine is exact and axiomatically
  defensible.** Shapley attribution on identities is not an approximation. The
  three-game symmetric decomposition provably sums to the gap for unequal
  windows, and `unexplained` on an exact identity is zero to floating point.
  This covers a large fraction of a typical metric tree.
- **Uncertainty is never dropped.** Every attribution carries an interval; every
  fit carries convergence diagnostics; `unexplained` is explicit and
  first-class. Degenerate cases withhold intervals rather than report zero-width
  ones. There is no point-estimate-only path through the system.
- **Leakage is correctly handled.** Fitting strictly before the analysis window,
  with normalization and prior rescaling following the fit window, is the right
  design and is easy to get wrong.
- **The engine is deterministic and reproducible.** Seeded bootstrap and seeded
  on-demand fits mean identical requests return identical numbers, and with the
  snapshot store an analysis re-runs from a fresh clone. Reproducibility is a
  precondition for rigor and is frequently missing in this product category.
- **There is a calibration suite.** `tests/test_calibration.py` tests recovery
  of planted causes (contemporaneous, lagged, and identity), *restraint* on null
  cases and unrelated parents, and interval coverage across 20 independently
  generated worlds. Testing that a method declines to find a cause when there
  isn't one is the test most analytics tools skip.
- **Known-incident validation.** The engine recovered a real historical
  anomaly — a subscription migration — on real governed data, at two grains.

### 3.2 What is weak

Ordered roughly by how much it should worry you. Each carries its status and the
roadmap item that addresses it — check the [roadmap](roadmap.md) for current
state before assuming a weakness listed here is still open. Statuses read
**○ open**, **◐ substantially closed** (the item shipped, and a named residual
survives it — read the entry for what) and **✅ fixed**.

Two kinds of item appear below, and the difference matters when you are deciding
how much to trust a number today. An **S** item is a known limitation that is
*disclosed*: the engine does what this paper says, and the honest description of
what it does is less than you might want. A **C** item is a
[Horizon 0](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
**correctness defect** — behavior this paper or [`docs/model.md`](../docs/model.md)
describes wrongly, or a number the engine cannot defend at all. C items block the
S track. Several weaknesses below were found or sharpened by a hostile external
review of the engine, docs and tests conducted 2026-08-05 against 0.1.0.

1. **The default inference method misstates uncertainty.** — ✅ **fixed
   2026-08-24** ([S1](roadmap.md#statistical-rigor-s--a-standing-workstream) ✅
   benchmarked 2026-08-18;
   [S2](roadmap.md#statistical-rigor-s--a-standing-workstream) ✅ shipped
   2026-08-22 and closed 2026-08-24). **The default is now exact MCMC, so
   there is no approximation error on it to state or misstate.**

   RCA defaulted to mean-field ADVI, which cannot represent posterior
   correlation and produces systematically *wrong* intervals — narrow on the
   short ridge, over-wide at long windows, unpredictable even in direction. The
   escape hatch (re-run with NUTS) existed and was documented, but **the
   default path was the optimistic one**, and most users would never leave it.
   Worked through in detail in
   [`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md).
   S1's benchmark ([`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md))
   put numbers on it: on drifting-parent worlds — the β-vs-trend ridge — the
   mean-field 95% interval is **~0.8× the NUTS width** at this engine's own
   settings, and the candidate fix was rejected: full-rank ADVI reproduces the
   NUTS interval on the synthetic ridge but, on the real White Cube nodes,
   costs more than NUTS itself while landing **7.8× too wide** with a clean
   ELBO.

   **What S2 measured, and what it forced.** Every variational fit is now
   scored with PSIS k̂ (Yao et al., 2018). On real trees the diagnostic does not
   flag *some* nodes — it flags **nearly every one**: k̂ on White Cube's four is
   10.18, 1.07, 0.85, 1.36 at full window (`random_seed=0`), 1.26 / 0.88 / 1.11
   / 10.43 on a second window, and 1.04 on the bundled demo's `order_count`.
   The engine's own geometry is why — one latent trend state per fitted period,
   strongly correlated between neighbours, is the shape a factorized Gaussian
   cannot hold — and it is not an artifact of dimension: the same code returns
   0.31 on a 30-dimensional posterior mean-field *can* represent, 0.95 on
   Neal's funnel and 4.1 on a collinear ridge.

   The first cut of S2 answered that with automatic escalation — re-fit a
   rejected node with NUTS, capped at four per analysis. **That was the wrong
   shape once the measurement was in.** A rescue that fires on essentially
   every node is not a rescue, it is a default in disguise, and it was
   *slower* than the default it disguised: an escalated node pays for the
   approximation and then the exact fit. And the speed the approximation was
   being kept for turned out not to be there. The 106-metric B2B reference
   tree — the widest in this repo — contains exactly five probabilistic fits,
   and fitting all five over the full window measured **31.3s under ADVI
   against 28.0s under NUTS**: the exact sampler was the *faster* of the two,
   on four of the five nodes, and ADVI flagged all five `suspect` where NUTS
   returned four of five `ok`. The approximation does still save time on the
   shorter windows an RCA fits (`total_mrr` over its ancestors: 10.4s against
   19.4s), but that is tens of seconds against every published coefficient on
   the tree.

   **So the default became NUTS everywhere** — `run_rca`, `run_scenario` and
   `POST /analyze/{name}` — and ADVI became an explicit `?inference_method=advi`
   opt-in on all three, with k̂ reported on every node it fits. Escalation was
   deleted: on the default path there is nothing to escalate, and on the opt-in
   path escalating spends 2–20× the cost the caller just declined.

   **The size of what was being missed.** Across all four demo stories, the
   `sessions ← marketing_spend` contribution moves −108.2 → −68.8 (0.678 →
   0.431 of the gap), 178.1 → 114.0, 88.4 → 62.7 and 202.0 → 129.0. Those are
   *point estimates* off by tens of percent, not intervals off by 20%.

   **And the bias has no fixed sign**, which is what ruled out documenting it
   instead of fixing it. On story B the learned
   `member_activity_rate → customer_churn_rate` contribution moves the other
   way — −0.049 to −0.077 of the gap, 57% *larger* under NUTS. What is
   consistent is the **trend** delta, which mean-field collapses toward zero;
   β then absorbs the discrepancy in whichever direction the ridge happens to
   run at that node. A reader holding only the payload cannot tell which way
   their number is wrong, or by how much, so no caveat short of a re-fit
   recovers the estimate. On that same edge the coefficient's interval changes
   *verdict*, not just width: mean-field's 95% HDI on β is [−0.053, +0.005] and
   the exact fit's is [−0.059, −0.013] — the approximation was
   **under**-confident about an effect that is really there, the reverse of the
   under-dispersion this weakness is usually stated as. And the flip runs both
   ways: on the same story's `trial_conversion_rate ← trial_days_active`, ADVI's
   interval *excludes* zero and NUTS's straddles it. §4.1 carries all of it.

   **What is left, and why it is not this weakness.** Two things survive — a
   third closed as S22 on 2026-08-27 — and each is a property of a path the
   caller explicitly chose rather than of the default. A `suspect` node (0.5 < k̂ ≤ 0.7) on the opt-in path is
   published from its approximation with a warning. k̂ is a verdict on the
   **joint** posterior, so it can say the approximation is not close and cannot
   say how wrong any particular marginal is. k̂ was also a Monte-Carlo estimate
   published as a bare number, on a route that did not seed it — two
   consecutive `POST /analyze` calls measured 1.23 and 0.79 for the same node —
   and **that one closed on 2026-08-27**
   ([S22](roadmap.md#statistical-rigor-s--a-standing-workstream)): all three
   fitting paths now pass one seed, and k̂ travels with its own Monte-Carlo
   standard error (`khat_se`, ~0.15 near the 0.5 bar and ~0.2 near k̂ = 1) plus
   a `khat_borderline` flag where the estimate cannot separate the band it
   landed in from the next one along. The residual there is what the error's
   *size* says rather than that it is unstated: at 1000 draws it is comparable
   to the whole width of the `suspect` band, so that band is rarely resolvable,
   and one of the demo tree's four nodes (`trial_conversion_rate`, 0.854 ±
   0.172) is borderline in practice.

   One further caveat is honest to state: a **passing** k̂ bounds the
   approximation error but does not zero it. Story D's `trial_conversion_rate`
   is the one demo node mean-field genuinely represents (k̂ 0.497, `ok`), and
   its published share still moved 70.7% → 68.0% between the two samplers.

   None of that is "the default inference method misstates uncertainty". The
   default states it exactly, subject to the ordinary MCMC diagnostics it
   reports (R̂, divergences, ESS); the approximation is available, and it says
   how far off it is. That is the weakness closed.
2. **The short-window block bootstrap is attenuated by construction.** — ✅ the
   two named defects are fixed
   ([C4](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend),
   shipped 2026-08-13); **a measured residual is now disclosed under
   [S6](roadmap.md#statistical-rigor-s--a-standing-workstream)**.
   `_block_bootstrap_indices` capped the effective block at `n // 2`, which
   landed on the midpoint of the very degeneracy curve its docstring reasoned
   about. Measured rather than argued this time: the resampled-to-true variance
   ratio was **0.46–0.63 for every n ≤ 16** and non-monotone (0.55 at n=13,
   0.50 at n=14). The cap is now `n // 4` — 0.74–0.90 across all n, monotone,
   and within 0.03 of the optimum on an AR(1) at ρ=0.6. The degeneracy guard no
   longer keys on `n_periods == 1`: a *constant parent* is detected on the
   resampled spread and its interval is withheld rather than shipped
   zero-width, and no published `ci_95` is zero-width by any route.
   **What the fix did not retire, and what taking the measurement exposed:**
   under serial dependence a short window is weakly informative *whatever* the
   block — 0.17–0.61 of the true sampling variance at ρ=0.6, which is a property
   of the window, not the estimator. And `BOOT_BLOCK["day"] = 7` **resonates
   with a weekly cycle**: a 7-day block holds each weekday exactly once, so a
   weekly seasonal component cancels identically in every replicate, and the
   shipped default sits at a *local minimum* of the honest interval width —
   measured on the demo tree at roughly a third of the width at block 3. Every
   daily-grain interval on a weekday-seasonal metric has therefore been
   optimistic, by a factor no cap can correct, because the cap chooses how far
   down from the constant and the constant is what is wrong. That is S6's, and
   it is why "read a short-window formula CI as a lower bound" survives C4 in a
   narrower form rather than disappearing.
3. **Nothing accounts for multiplicity or selection.** — ○ open
   ([S15](roadmap.md#statistical-rigor-s--a-standing-workstream))
   One `run_rca` on a 15-node tree emits 25–30 intervals plus a
   `prob_same_direction` each, then sorts by effect size and presents the top
   one. Under a global null, ~1.5 of those intervals exclude zero by
   construction; the selected top cause is the maximum of many noisy quantities
   and is upward-biased, and its `ci_95` was computed **before** that selection.
   The window pair is a free user choice with no cost to retrying. This paper
   argues elsewhere that an unconstrained search over metrics × slices
   manufactures spurious findings; breakdown runs a bounded version of the same
   search and, until S15, says so nowhere. The interval on a *pre-specified*
   node is unaffected — the problem is specific to reading the winner of a
   ranking as though it had been the question.
4. **The ADVI quality check is not a real approximation diagnostic.** — ✅
   **fixed 2026-08-22**
   ([S2](roadmap.md#statistical-rigor-s--a-standing-workstream))
   ELBO convergence says the optimizer stopped; it says nothing about closeness
   to the true posterior. Worse than neutral: `_advi_diagnostics` thresholded
   the ELBO drift against the standard deviation of the stochastic ELBO trace
   itself — a quantity dominated by Monte-Carlo noise — so a `fit_quality: "ok"`
   conveyed very little, and it was the field the MCP payload kept and placed
   beside the intervals. S1's benchmark (2026-08-18) turned this from an
   argument into a measurement, in both directions at once: a full-rank fit on
   a real node passed the ELBO check while emitting an interval **7.8× the
   NUTS width**, and mean-field at its shipped step count came back `suspect`
   on **all three** real White Cube probabilistic nodes — so the check could
   pass a bad approximation and flag a usable one.

   There is now a real one beside it. `khat` / `khat_status` report PSIS k̂ per
   fit in three bands plus `unavailable`, and `fit_quality`
   goes `suspect` when *either* check fails, so the two-valued gate every
   consumer already branches on cannot come back clean on an approximation the
   engine knows is far from the posterior. The confirming case is measured, not
   argued: two White Cube nodes pass the ELBO check with k̂ ≈ 1.0, and their
   ADVI intervals are roughly half the NUTS width. An uncomputable k̂ is
   withheld under `unavailable` with its reason rather than emitted as a
   number, so the payload can say *unchecked* — which the ELBO-only era could
   not say about anything.
5. **Posterior predictive checks, and the engine now asks the question.**
   — ✅ closed 2026-08-29
   ([S3](roadmap.md#statistical-rigor-s--a-standing-workstream))
   The engine used never to ask "could this fitted model have generated data
   that looks like what I saw?" — the single most informative Bayesian model
   check there is (Gelman et al., 2020; Gabry et al., 2019) — so a badly
   misspecified node passed silently as long as it converged. Every fit is now
   scored on four test statistics (`min`, `max`, `resid_max`, `resid_acf1`) as
   two-sided mid-p posterior predictive p-values in two bands, and a `severe`
   verdict sets `fit_quality: "suspect"`. On a NUTS fit it is the only thing
   that can, which changes what that bit *means*: `suspect` on an exact
   sampler now says the model is wrong for the data rather than that the
   sampler struggled, and the two want opposite responses.

   Two residuals, stated rather than implied. It is an **in-sample** check
   against the fitted window, so it grades the likelihood and the mean
   function, not the DAG, not a confounder, and not out-of-sample behavior —
   §3.3's assumption is untouched by it. And four statistics is not every way
   a model can be wrong: an `ok` is the absence of four specific failures. The
   check's power also depends on the trend prior that limits how much the
   local level can absorb, so a node with a deliberately loosened
   `trend: {sigma: …}` should have its verdict read as weaker evidence. Power
   also grows with the fitted window, as it does for any goodness-of-fit check:
   the demo's `trials_started` is a count series at every window and scores
   `min` at p = 0.076 / 0.012 / 0.016 over 219 / 401 / 618 fitted days —
   `moderate`, then `severe`, then `severe`, with nothing about the metric
   having changed. An `ok` on a short window is *not yet detectable*, not
   *clean*.
6. **Correlated parents give an undetermined split, and the engine now says
   which parents.** — ✅ closed 2026-08-27
   ([S4](roadmap.md#statistical-rigor-s--a-standing-workstream))
   Correlated parents produce a well-determined *sum* and an unstable *split*,
   and the split is exactly what RCA reports. Since the default became exact
   MCMC (#1) the *interval* on each parent has been honest — wide, as it
   should be on a ridge — so this stopped compounding with #1 on the default
   path; on the `advi` opt-in it still does, and k̂ catches the ridge (4.1 on
   the collinear fixture) without saying which pair causes it. What was left
   was the diagnosis, and that is what shipped.

   **How unstable, measured 2026-08-24.** The demo tree carries a deliberately
   collinear pair (`trial_activation_rate` and `trial_days_active`, which ride
   the same underlying engagement). Their split of one gap comes out **0.680,
   0.682 and 0.697** on three different numeric stacks *from the same seed* —
   a 1.6-point spread, with no run-to-run variance inside a stack, so it is the
   linear-algebra library rather than the sampler's randomness. The pair's
   *sum* is the determined quantity; which of the two gets the credit is not.

   **The fix, 2026-08-27.** Every fit with two or more regressors now scores
   its own design matrix and publishes `collinearity_status` — `ok`,
   `moderate`, `high` or `unavailable` — with the flagged pairs and their
   correlations, the flagged parents and their VIFs, and a sentence naming
   them, on the fit, on every RCA node, over MCP and in the UI. Two bands
   rather than one bar, and the pair above is why: over the 62 weeks an RCA of
   that story fits it on it measures **|r| = 0.863**, so a single 0.9
   trip-wire would have passed in silence the exact node this weakness was
   written about. `moderate` says the pair's total is the sound number and the
   division of it is the soft one; `high` (|r| ≥ 0.9, VIF ≥ 10) says the split
   is not a measured quantity at all. It deliberately does not move
   `fit_quality`: a collinear fit is a *correct* fit that is properly unsure,
   and what is unsafe is reading one parent's share alone. An uncheckable
   design is withheld as `unavailable` rather than reported as zero
   correlation.

   Two things this does not fix, and they are the residual. No diagnostic can
   recover a division of credit the data does not contain — the warning tells
   you to read the pair together, it does not produce the per-parent number —
   and the durable remedy is authorial (merge the metrics, drop one, redefine
   one), which is why the warning names the tree rather than the fit. And the
   check is on the fitted design matrix, so it sees co-movement over the fit
   window only; two parents that are structurally redundant but happened to
   move apart over that window pass it.

   Until
   2026-08-08 the reference tree in `knowledge/` contained the structure itself
   — a conversion rate regressed on a parent that was *defined* as a product
   involving another of its parents — which is how easily it is authored
   without noticing. It was removed there
   ([C10](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend))
   rather than kept as a specimen, since that file is what new authors copy;
   the purpose-built fixture S4 and S17 needed lives in `tests/test_engine.py`
   instead.
7. **Interval calibration is tested by a test that is structurally unable to
   fail.** — ○ open
   ([S17](roadmap.md#statistical-rigor-s--a-standing-workstream),
   [S5](roadmap.md#statistical-rigor-s--a-standing-workstream))
   This one is a correction to an earlier edition of this paper, which offered
   the 20-world coverage test as its headline defence. The test computes the
   true contribution from the **realized** parent series and then checks it
   against percentiles of `beta_samples × a bootstrap of that same realized
   series` — so the window-sampling term is pure added width around a point that
   already carries the truth's parent factor. If β were known exactly, coverage
   would be ≈ 1 by construction, and the added width is precisely what would
   mask a too-narrow ADVI posterior. The pass bar (80% for a nominal 95%
   interval) has no power to reject true coverage of 0.85; all 20 worlds share
   one data-generating process varying only the noise seed. Two things are
   consequently **untested anywhere**: formula-node CIs, which come 100% from
   the bootstrap in #2, and the collinear-parent case in #6 — which since
   2026-08-08 has no live example to borrow from either, so the fixture has to
   be built. The *design* of the
   test suite — including its null-case restraint tests, which remain the most
   valuable part — is right; this particular implementation proves less than it
   appears to.
8. **`ranked_causes` is a heuristic and reads like a result.** — ◑ the inversion
   is fixed ([C5](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend),
   shipped 2026-08-13); **the framing problem is open**
   ([S12](roadmap.md#statistical-rigor-s--a-standing-workstream)).
   The score propagates share products up the tree. It is explicitly
   documented as triage rather than probability, but it is also the most
   prominent number in the UI, and prominence implies rigor whatever the docs
   say — that half is untouched and is S12's.
   The *defect* half is closed. The near-zero-gap guard was an absolute `1e-12`
   rather than relative to node scale, and the weight saturated at
   `min(|share|, 1)`, so a node that barely moved with two large offsetting
   parents handed its full influence score upward. Both are fixed: the guard is
   relative, and a hop now weights a parent by `min(|s_p|, 1)` divided by the
   node's **cancellation factor** — the total gross share its parents had to
   move to net out to the gap — so a share far past 100% lowers the score, which
   is what it always should have meant. A node's parents' weights sum to at most
   1, so a hop can no longer inflate influence at all.
   **One correction this paper owes its own reader**, because the entry above
   framed the defect as a near-zero-gap edge case and that framing was too
   narrow: it was reproduced on the **bundled demo tree**, over an ordinary
   fortnight, on a gap of +$596/day against a $26.4K baseline — nowhere near
   zero. Shares exceed 100% whenever two parents oppose, which is the common
   case the unclamped `share_of_gap` design exists to express. Two hostile
   reviews and a triage pass all had the mechanism in hand and none ran the demo
   and looked at the top number.
9. **The trend interval does not grow with the analysis horizon.** — ○ open
   ([S16](roadmap.md#statistical-rigor-s--a-standing-workstream))
   `components.trend` is reported as the last fitted level's posterior, so a
   one-period analysis window and a ninety-period one starting the same date
   return the identical estimate *and* the identical interval. The flat *point*
   forecast is a correct property of a local-level random walk and is documented
   as such (§2.1); the *interval* silently omits the forward-simulation variance
   that accumulates with every step past the fit end. This is distinct from #11
   below, which is about the point estimate. The size of the understatement
   scales with `σ_trend` and has not been measured — S16 measures it.
10. **The bootstrap is bolted on, not integrated.** — ○ open, partially addressed
    ([S6](roadmap.md#statistical-rigor-s--a-standing-workstream),
    [C4](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend))
    Composing a frequentist resampling interval with a Bayesian posterior is
    pragmatic and defensible but not a coherent joint posterior; block length is
    still fixed rather than estimated. C4 fixed the block *cap* (a defect,
    shipped 2026-08-13); S6 estimates the block *length* from the data and now
    carries a measured reason to hurry — see #2. The deeper composition question
    is not currently scheduled, and would mean moving window-sampling
    uncertainty inside the model.
11. **The flat trend forecast understates counterfactual movement** for nodes
    with genuine momentum (§2.4). — ○ open
    ([3.4](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you),
    [S8](roadmap.md#statistical-rigor-s--a-standing-workstream))
12. **Cold-start beliefs are sampled independently,** so correlated beliefs are
    misrepresented in either direction. — ○ open
    ([S7](roadmap.md#statistical-rigor-s--a-standing-workstream),
    [C7](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend))
    The bias is not symmetric in practice: a founder's beliefs about their own
    funnel are substantially one latent variable (how optimistic they are), and
    treating them as independent systematically *understates* interval width.
    Two related defects were C7 rather than S7, and **C7 shipped 2026-08-17**:
    baseline draws are now truncated to the declared `plausible` bounds by
    rejection resampling (a `min: 0` belief cannot draw a negative customer
    count, and nothing piles up on the bound), a `LogNormal` baseline exists
    for order-of-magnitude beliefs about positive quantities, and a formula
    dividing by a belief whose draws cross zero is **refused** with the
    remedies named — the Monte-Carlo mean of such a ratio does not exist, and
    the centre stays a mean rather than switching to a median because the
    mean's linearity is what keeps per-source contributions summing exactly to
    the delta. What remains here is S7's correlation gap. Cold start is a demo
    mode as of 2026-08-05; read its numbers as illustrations of a belief, not
    as forecasts.
13. **Causal language rests entirely on the declared DAG.** — ○ open
    ([S14](roadmap.md#statistical-rigor-s--a-standing-workstream))
    This is disclosed everywhere and remains the assumption most likely to be
    violated in practice — not because users are careless, but because business
    metric graphs have confounders (a pricing change that moves traffic *and*
    conversion) that are invisible unless someone thought to declare them. S14
    would put a *number* on the assumption; note that removing it entirely would
    mean causal discovery, which is deliberately off the roadmap.

### 3.3 The fair summary

breakdown is **more rigorous than the analytics tools it competes with and less
rigorous than a careful bespoke analysis by a statistician.** Its exact
half is genuinely exact. Its probabilistic half is well-specified and honestly
reported, but the default inference approximation is optimistic and the model-
checking layer is thin. The engine's real achievement is *structural*: it is
built so that adding rigor does not require re-architecting anything, because
uncertainty already flows everywhere it needs to.

Most of the failure modes above are **documented rather than hidden** — which is
the difference between a tool with known limitations and a tool that is quietly
wrong.

An earlier edition of this paper made that claim without qualification. The
2026-08-05 review found that it was not fully earned: a handful of behaviors were
quietly wrong rather than disclosed, including two that could silently turn real
data into fabricated movement at the provider boundary, and one — the coverage
test in #7 — where this document's own headline evidence proved less than it
appeared to. Those are enumerated as
[Horizon 0](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) and
they gate everything else.

**The two provider-boundary defects that review found are fixed** (`C1`/`C2`,
shipped 2026-08-05).
A timezone-aware date column used to leave a metric identically zero — the
alignment guard compared two tz-aware values and passed, and the zeros were then
written to snapshots and served from there. And only two of the four providers
snapped to a period spine, so a window ending mid-week could return a partial
week as a whole one. Both now go through a single shared contract, and both have
tests that fail against the previous code. If you ran an analysis on the `cloud`
or `local` provider at a non-period-aligned window boundary before that date, or
against a warehouse returning `TIMESTAMP`, re-run it — and delete any snapshot
written from it, because a snapshot of the old behavior is still wrong.

**A second review, on 2026-08-12, found two more at the same boundary. Both are
now fixed** (`C15`/`C16`, shipped the same day). A dbt metric's `filter` was
silently dropped, so a node served the *unfiltered* measure under the governed
metric's name — the manifest models never declared the field and unknown keys
are ignored, so nothing anywhere reported it and `doctor` stayed green. And a
snapshot was not invalidated when the metric's own `sql:`/`bind:` block changed,
so an edited query kept serving pre-edit numbers while the *show query* panel
displayed the new statement beside them — the one feature that exists to let a
reader verify a number was certifying a wrong one. Filtered metrics were
refused by name until the binding language could carry a predicate — since
roadmap 2.17 a filter is *resolved* into the binding when every reference in it
lands on the measure's own relation, and refused by name when any does not, so
the failure mode at this seam is still refusal and never a quiet substitution.
And every snapshot record is fingerprinted against the definition that produced
it — including its filter.

Neither was a statistical limitation; both were engineering defects at the seam
where this engine meets someone else's data.

**A third finding at the same seam came out of re-checking that review against
current code, and it is the sharpest of the set** (`C18`, shipped 2026-08-12). A
metric whose source has no rows before some date — a product launched in March,
a channel switched on in week 3 — was **zero-filled back to the start of the
loaded window with no warning of any kind**. Nineteen invented zeros in the
reproduction, and the model then trains on them, so a node that did not exist
yet acquires a manufactured level shift and a manufactured trend that RCA can
rank as a cause. What makes it worth reading twice is *where* it was: inside
`_align_to_spine`, the single shared date-alignment contract that `C1`/`C2`
created **in order to end this class of defect**, and which warns correctly about
interior gaps in the very next branch. The fix is a warning naming the fabricated
periods; trimming them was rejected because per-grain frames inner-join, so
trimming one late-starting node would delete those periods for every metric at
that grain.

That seam has now produced **five of the six** silent-wrong-number defects this
project had found by mid-August, and no other part of the system had produced
more than one.
The count is not the point; the location of the fifth is. A shared contract
written to close a class of defect can still carry an instance of it, and this
one survived being read by two hostile reviews — the second filed it as
*medium*, because a silent fill reads as a nicety until you notice what the fit
does with it. Treat a number's *provenance* as the least-tested thing in this
system, prefer a provider path with a `doctor` check behind it over one without,
and do not assume a boundary is sound because the last defect there was fixed.

**A fourth pass, on 2026-08-17, tested that advice deliberately and found the
class in a new place** — the
[milestone-readiness audit](milestone_readiness_2026_08_17.md) traced four
recently-decided policies (null handling, rate denominators, non-additive
slicing, the concentration verdict) from decision to code to tests. The
decisions themselves all held. What leaked was *propagation*: the sliced fetch
path never received the date contract the metric path got from C1/C2 — a
tz-aware sliced frame silently becomes an all-zero panel (roadmap C23) — the
slice panel's "localized" verdict is structurally unreachable for every rate
because one field is never emitted (C24), and `/simulate` publishes a rate's
window aggregate unlabelled and unsanitized where the RCA surface labels and
sweeps it (C25). The pattern across all fifteen findings: the honesty machinery
is implemented and largely correct at the engine, and its guarantees thin out
at the three boundaries without a structural test — engine→MCP compaction,
engine→UI rendering, and metric-path→slice-path. That is a better problem than
the 2026-08-05 review found, and a mechanically closable one; it is also the
current best answer to "where would this system lie to me": not in a model,
but in a surface a policy forgot to reach.

The remaining Horizon 0 items are open. Until they close, the honest statement
is: the *statistical* limitations are documented; a short, named list of
*correctness* defects is documented, and is being worked down — though it grew
by two on 2026-08-12 before shrinking again the same day, because looking harder
still finds things. We would rather you read that
list than trust the claim it replaced.

---

## 4. Where to go next

Every item below is tracked in the roadmap's
[**Statistical rigor (S) workstream**](roadmap.md#statistical-rigor-s--a-standing-workstream),
which is the source of truth for status and sequencing. This section holds the
*rationale* — why each gap matters and what "fixed" would mean. The IDs are
stable; the statuses here are a snapshot as of the last-updated date.

**Current state (2026-08-27):** the workstream has started, and three of its
items have closed. **S1 — benchmarking full-rank ADVI — ran 2026-08-18 and the
decision is not to adopt** (measurement in
[`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md); rationale in
§4.1). **S2 shipped 2026-08-22 and closed 2026-08-24**: PSIS k̂ on every
variational fit — and, because it rejects essentially every real node, **NUTS
as the default sampler on every path**, with ADVI a stated opt-in that reports
its k̂. It closes §3.2 #4 and §3.2 #1 outright. **S4 shipped 2026-08-27**, closing
§3.2 #6: a fit with two or more parents now names the ones whose split of
credit the data does not determine, in two bands the demo's own collinear pair
(|r| = 0.863 over the window an RCA fits it on) is what calibrated. **S3
shipped 2026-08-29**, closing §3.2 #5: every fit is now graded against its own
posterior predictive distribution on four statistics, and a `severe` verdict
moves `fit_quality` — so on the NUTS default that bit finally distinguishes a
model that is wrong from a sampler that struggled. **S10 shipped 2026-08-30**,
putting that check on screen: the Metric tab now draws the fitted series
against the quantile bands of the replicates, so the verdict can be *seen*
rather than only read. One other item has shipped its first half: S20's
*disclosure* (2026-08-17) — a fit whose window is ≥25% exact zeros now carries
`likelihood_warnings` on every surface that shows its numbers, because the
misspecification was silent and that failed this track's own
disclosed-limitation bar; the likelihood itself stays here.

**Ahead of all of it:**
[**Horizon 0**](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend),
the correctness gate — **closed 2026-08-17: every row is ✅.** Two of its items
were the defect halves of S items listed below — C4 of S6 (shipped
2026-08-13, which unblocked S17; an earlier edition of this paragraph said C4
"blocks any honest reading of S17's rebuilt coverage test," and that stopped
being true then) and C7 of S7 (shipped 2026-08-17). With the gate closed, this
track's sequencing condition is met on the correctness side; S1 remains the
scheduled start.

| ID | Item | Status |
|---|---|---|
| S1 | Benchmark full-rank ADVI as the RCA default | ✅ benchmarked 2026-08-18 — **not adopted** |
| S2 | PSIS k̂ approximation diagnostic; NUTS by default, ADVI by request | ✅ closed 2026-08-24 |
| S3 | Posterior predictive checks on every fit | ✅ shipped 2026-08-29 |
| S4 | Parent collinearity diagnostic | ✅ shipped 2026-08-27 |
| S5 | Simulation-based calibration | ○ open |
| S6 | Data-driven bootstrap block length | ○ open |
| S7 | Correlated cold-start beliefs | ○ open |
| S8 | Local linear trend (opt-in) | ○ open |
| S9 | Narrow nonlinear edges | ○ open |
| S10 | Posterior predictive plot in the UI | ✅ shipped 2026-08-30 |
| S11 | Prior-vs-posterior visualization | ○ open |
| S12 | `ranked_causes` visibly a heuristic | ○ open |
| S13 | Methods appendix in the exported report | ○ open |
| S14 | Quantify the DAG assumption | ○ open |
| S15 | Multiplicity and selection-aware reporting | ○ open |
| S16 | Forward-simulation variance in the trend interval | ○ open |
| S17 | Rebuild the calibration suite's coverage test | ○ open |
| S18 | Right-censored metrics — series whose past values restate | ○ open |
| S19 | Partial pooling across a repeated-cycle grouping | ○ open |
| S20 | Zero-inflated and count likelihoods | ○ open — disclosure half scheduled first |
| S21 | Fit through undefined periods by masking the likelihood | ○ optional — build on demand |
| S22 | k̂'s own Monte-Carlo error, and a seeded manual-fit path | ✅ closed 2026-08-27 |
| 3.4 | Counterfactual RCA (Horizon 3, not the S track) | ○ open |

Below, the reasoning behind each — ordered by value per unit of effort.

### 4.1 Highest value

**Benchmark full-rank ADVI** — `S1`, ✅ benchmarked 2026-08-18, **not
adopted**. The premise held and the conclusion did not follow. PyMC's
`fullrank_advi` *can* represent the β-vs-trend ridge that mean-field destroys:
at convergence it reproduced the NUTS interval on drifting-parent worlds to
within 4%, where mean-field was ~20% narrow. But "slower than mean-field and
far faster than NUTS" — this paragraph's own prior — was **false on the real
tree**: on the White Cube seasonal nodes full-rank took ~230s against NUTS's
11–66s on the same nodes, and its `ok`-ELBO interval on one of them came out
7.8× the NUTS width. The O(d²) covariance is the mechanism on both axes — the
model carries one latent trend state per fitted period, so real windows put
the variational Gaussian in hundreds of dimensions, where the optimizer is
slow and the ELBO check cannot see how wrong it landed. Full measurement,
reproduction script and decision:
[`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md). The engine
keeps `inference_method="fullrank_advi"` as a benchmarked experimental option;
no default changed.

**A real ADVI approximation diagnostic** — `S2`, ✅ **shipped 2026-08-22,
closed 2026-08-24**. The PSIS-based k̂ diagnostic of Yao et al. (2018) now
scores every variational fit, so a bad approximation is *detected* rather than
assumed away. Implementation note worth keeping: both terms of the log ratio
come from PyMC's own symbolic graph (`sized_symbolic_logp`, `symbolic_logq`)
cloned against a single shared sample, so the ratio is between comparable
densities in the unconstrained space and the same code serves mean-field and
full-rank without a per-family branch.

**The measurement did to this item roughly what S1's did to S1** — it held the
premise and broke the framing. The premise was that k̂ would flag *some* nodes
and re-fitting them would be an occasional rescue. It flags nearly all of them:
10.18, 1.07, 0.85 and 1.36 on the White Cube tree's four probabilistic nodes at
full window (`random_seed=0`), 1.26 / 0.88 / 1.11 / 10.43 on a second window,
1.04 on the bundled demo's `order_count`. The cause is this engine's own model
— one latent trend state per fitted period, strongly correlated between
neighbours, which is exactly the geometry a factorized Gaussian cannot
represent — and it is not a high-dimension artifact: the same code returns 0.31
on a 30-dimensional posterior mean-field can represent, 0.95 on Neal's funnel
and 4.1 on a collinear ridge.

**So the item's own "or" resolved the other way, and it took two more
measurements to see it.** The first cut escalated: `run_rca` and `run_scenario`
discarded an `unusable` approximation and re-fitted that node with NUTS, capped
at four per analysis. A rescue firing on essentially every node is a default in
disguise — and a *slower* one, because an escalated node pays for the
approximation and then the exact fit (the demo suite ran 225s escalating and
runs 175s now). Then the latency case fell over. The 106-metric B2B reference
tree, the widest here, holds exactly five probabilistic fits, and fitting all
five measured **31.3s under ADVI against 28.0s under NUTS** — the exact sampler
was the *faster* of the two, on four of the five nodes, and ADVI flagged all
five `suspect` where NUTS returned four of five `ok`.

**NUTS is therefore the default on every path**, ADVI an explicit
`?inference_method=advi` opt-in on `/rca`, `/simulate` and `/analyze` alike,
and escalation is gone: on the default path there is nothing to escalate, and
on the opt-in path escalating spends 2–20× the cost the caller just declined.
k̂ is now a disclosure on a chosen path rather than a trigger. S1's own closing
note — "NUTS-by-default for small trees is worth a look" — turns out to
describe where this landed, by a longer route.

What exactness buys is larger than interval width. Across the demo's four
stories the `sessions ← marketing_spend` contribution moves −108.2 → −68.8
(0.678 → 0.431 of the gap), 178.1 → 114.0, 88.4 → 62.7 and 202.0 → 129.0. The
ridge failure was distorting published *point estimates* by tens of percent,
and nothing before this could see it. **Nor does it shrink consistently.** On
story B the learned `member_activity_rate → customer_churn_rate` contribution
moves −0.049 → −0.077 of the gap, 57% larger in the other direction. The trend
delta is the term that behaves consistently — mean-field collapses it toward
zero in every case measured — and β takes up the slack in whichever direction
that node's ridge runs. A reader cannot correct for this by hand, which is the
argument for re-fitting rather than disclosing.

**One edge also changes a verdict, not just a number.** On
`member_activity_rate → customer_churn_rate`, mean-field put β at −0.0233 with
a 95% HDI of [−0.053, **+0.005**] — an interval that fails to exclude zero. The
exact fit puts it at −0.0361, [−0.059, **−0.013**], clear of zero. The
approximation was *under-confident about a real effect*, the opposite of the
under-dispersion mean-field is usually charged with, and it happened on the one
edge the demo exists to show the engine recovering. §3.2 #1's complaint has
always been phrased as intervals that are too narrow; on a ridge, mean-field
does not simply shrink an interval — it puts it in the wrong place, and which
way is a property of the geometry rather than of the method. **And the flip
runs both ways**: on the same story's `trial_conversion_rate ←
trial_days_active`, ADVI's interval *excludes* zero ([3.1e−5, 0.0084], P(dir)
0.98) and NUTS's straddles it ([−0.00035, 0.0104], 0.949). Two edges, two
directions, no rule a reader could apply.

**A passing k̂ bounds the error; it does not zero it.** Story D's
`trial_conversion_rate` is the one demo node mean-field genuinely represents —
k̂ 0.497, inside the good band — and its published activation share still moved
70.7% → 68.0% between the samplers. Worth stating, because "k̂ came back `ok`"
is the strongest thing the opt-in path can say about itself.

What it did not buy: k̂ is a verdict on the joint posterior, so it cannot say
which marginal is wrong; and a `suspect` (0.5–0.7) node on the opt-in path is
still published from its approximation with a warning. The third residual — k̂
carrying no error term of its own — closed as **S22** on 2026-08-27; see below.
All are recorded on §3.2 #1.

**k̂'s own Monte-Carlo error, and the seed that stopped it wandering** — `S22`,
✅ closed 2026-08-27. Two independent defects behind one number. `POST
/analyze/{name}` passed no `random_seed` while both analysis routes passed
`0`, so the manual-fit route answered the same request differently each time:
re-measured on the White Cube tree at full window, `customer_churn_rate` came
back **1.23 then 1.91** and `trials_started` **0.94 then 1.19** from two
consecutive calls (1.23/0.79 and 0.89/0.77 on the same nodes five days
earlier — the spread is itself unstable, which is the point). All three
fitting paths now read one `FIT_RANDOM_SEED`, enforced by an AST-walking
invariant test rather than by three call sites agreeing; the four figures this
paper quotes for the demo tree reproduce **exactly** from the route now
(10.18 / 1.07 / 0.85 / 1.36).

The second half is the one that matters here. k̂ is fitted to the tail of 1000
sampled importance ratios — 95 tail points — so it is an estimate, and this
project's whole argument for reporting it is that a number without a stated
uncertainty is not evidence. The engine now publishes `khat_se`, the
generalized Pareto shape parameter's asymptotic standard error over that tail
(Smith, 1985; Hosking & Wallis, 1987), corrected for the shrinkage in ArviZ's
empirical-Bayes estimator. It was checked against what it claims to describe:
holding one approximation fixed and re-estimating k̂ over 60 independent draws,
published-SE ÷ empirical-SD came out **1.06 / 1.11 / 0.90 / 1.04** on four
posteriors spanning k̂ 0.03 to 1.15.

**The measured size of that error is the finding.** It is ~0.15 near the 0.5
bar and ~0.2 near k̂ = 1 — comparable to the entire width of the `suspect`
band, which is 0.2 wide. So at 1000 draws the `ok`/`suspect` and
`suspect`/`unusable` distinctions are frequently not resolvable, and one of the
demo tree's four learned nodes is a live case: `trial_conversion_rate` scores
**0.854 ± 0.172**, `unusable` on the point estimate and unable to separate
`unusable` from `suspect`. Where that happens the fit carries
`khat_borderline: true` and the warning names the bands the estimate cannot
tell apart — all three of them where k̂ is within one error of *both* edges.
`fit_quality` goes `suspect` there too, including from an `ok` k̂, because
"not shown to be far from the posterior" is not the same claim as "shown to be
close to it". `khat_status` deliberately keeps meaning *which band
the point estimate is in*, so no consumer had to reinterpret it. Shrinking the
error was considered and rejected on cost: the Pareto tail grows as √n, so the
error falls as n^(−1/4) and halving it costs 16× the draws — against a
diagnostic that today costs ~0.2s beside a 1.4–6s fit. Reporting the error
costs nothing and is what lets a reader see when a band is a coin flip.

**Posterior predictive checks on every fit** — `S3`, ✅ **shipped
2026-08-29**. Every fit now simulates replicated series from its own posterior
and compares four summaries against the observed series: `min`, `max`,
`resid_max` and `resid_acf1`, each a two-sided mid-p posterior predictive
p-value. Two bands — `moderate` at p < 0.10, `severe` at p < 0.02 — reported as
`ppc_status` with every statistic's p-value beside it, on the fit, on every RCA
and what-if node, over MCP and in the UI.

Three things are worth recording, because each was decided by a measurement
rather than by preference.

*The obvious statistic is vacuous, and leaving it out is the finding.* The
first summary anyone reaches for is the series' standard deviation. But
`_prepare_series` z-scores `y` and `sigma_obs` is free, so the replicated
spread matches the observed spread **by construction**: measured across a
well-specified world, a t(3) world and a Poisson-count world it returned
p = 0.479 / 0.499 / 0.521. A statistic that cannot fail is worse than no
statistic, because a reader scores it as a check that passed. The same argument
retires `resid_sd` (0.511 / 0.526 / 0.482). What survives measures what the
local-level trend cannot absorb — and the worry that a per-period latent would
make the whole check vacuous did not survive contact with the data either: the
tight HalfNormal(0.05) step-size prior keeps the level from chasing noise, so
residual structure is still there to be measured.

*A discrete statistic needs mid-p, or it false-alarms on the good node.* Scored
with a plain `≥`, a statistic with a point mass — a count series whose minimum
is 0 in most replicates — returns p = 1.000 on a **correctly** specified node.
In the calibration probe that put the only false alarm on the well-specified
world. Splitting the tied mass removes it.

*The bands are wider than convention, deliberately.* Four statistics per node
means a node with four honest checks crosses a 0.05 bar 19% of the time by
chance. These are disclosure triggers on a payload a reader scans, not
hypothesis tests, and the cost of spraying is that the findings that matter
stop being read. On the trees this repo ships the separation is clean: five of
six probabilistic nodes come back `ok` with p-values between 0.30 and 0.96, and
one `moderate`.

The `fit_quality` question was the item's real design decision, and it is
answered asymmetrically with S4 on purpose. A collinear design gives a
*correct* fit that is properly unsure about the split, so S4 leaves the gate
alone. A model that cannot generate its own data is not correct, so `severe`
moves it — the roadmap row's "surface through the existing `fit_quality`
channel", honored for the strong band only. `moderate` stays out (wiring it
there would make `suspect` the common verdict) and `unavailable` stays out, for
the reason S22 kept k̂'s borderline band out: an unchecked model is not a failed
one.

**Counterfactual RCA via posterior predictive forecast** — roadmap `3.4` / T11,
○ open. Lives in Horizon 3 rather than the S track, since it is a headline
feature as much as a rigor fix. Instead of comparing window means, forecast the
analysis window from the
normal-regime model and report the *departure*: "revenue came in 12,400 below
what the normal regime predicts (95% CI …)." This is the full Brodersen et al.
(2015) pattern, it replaces the flat-trend approximation (§2.4), and it produces
a considerably stronger headline number than a window-mean difference.

**A parent collinearity diagnostic** — `S4`, ✅ **shipped 2026-08-27**
(promoted 2026-08-05). Every fit with two or more regressors now scores its own
design matrix — pairwise |r| among the parents, and VIF as well wherever there
are three or more of them — and publishes `collinearity_status` with the
parents it flagged, on the fit, on every RCA node, over MCP and in the UI.

The item was promoted because it compounded with weakness #1: correlated
parents make the split unstable, and mean-field ADVI then reported a *narrow*
interval around whichever split it landed on. S2 removed that half by making
the default sampler exact, which left the split honestly wide and still
anonymous — a reader holding two wide intervals had no way to know they were
one ridge measured twice.

Two design choices are worth recording. The bands (`moderate` at |r| ≥ 0.7 or
VIF ≥ 5, `high` at |r| ≥ 0.9 or VIF ≥ 10) exist because the demo's own
collinear pair measures |r| = 0.863 over the window an RCA fits it on: a
single 0.9 bar would have been silent on the one node this item was written
about. And the verdict deliberately leaves `fit_quality` alone — the fit is
correct and correspondingly unsure, so flagging the *node* would misdirect the
reader away from the actual hazard, which is reading one parent's
`share_of_gap` on its own. Both channels report: pairwise |r| names the pair,
VIF catches the multi-way degeneracy (`x4 = x1 + x2 + x3`) that no pair shows.
The residual is that no diagnostic recovers a split the data does not contain —
the remedy is authorial, and the warning says so — and that co-movement is
measured over the fit window, so a structurally redundant pair that happened to
move apart there passes. S17 still owes the collinear *coverage* case.

**Multiplicity and selection-aware reporting** — `S15`, ○ open. Disclose before
modeling. A `run_rca` result is a *ranking over many intervals*, and the top of
that ranking is a selected maximum whose interval was computed pre-selection.
Step one is that the `how_to_read` payload, [`docs/model.md`](../docs/model.md)
and the UI say so — that a pre-specified node's interval means what it says while
the *winner's* does not, and that re-running with a different window pair is a
search. Step two, only if step one proves insufficient, is a selection-aware
interval for the reported top cause.

*Considered and not adopted as the fix:* hierarchical pooling of `beta_raw`
across a node's parents, which would shrink winners and give selection-aware
intervals cheaply. It is rejected on substantive grounds, not cost: a node's
parents are heterogeneous quantities in different units — ad spend, sessions, a
conversion rate — and partial pooling toward a common mean encodes a belief that
they are exchangeable draws from one population, which is false. The independent
`Normal(0, 1)` on the *rescaled* coefficient (§2.1) is a deliberate choice, not
an oversight. If pooling is revisited, the case has to be made per tree shape.

**Forward-simulation variance in the trend interval** — `S16`, ○ open. The
reported `components.trend` is the posterior of the last fitted level, and its
interval does not widen as the analysis window moves further past the fit end.
For a random walk the forward variance accumulates with each step, so the correct
interval for a window `H` periods out is strictly wider — and today's is flat in
`H`. The fix is mechanical; the *sizing* is not, because it scales with
`σ_trend`, which this model prioritizes tightly by design. So S16 is a
measurement first: simulate forward from fitted posteriors across grains and
horizons, report the actual understatement, then correct it. This is separate
from S8 and 3.4, which concern the point forecast.

### 4.2 Substantial but well-scoped

**Rebuild the coverage test so it can fail** — `S17`, ○ open. Before S5's
expensive machinery, fix the cheap test that already exists. Three changes.
Draw the true contribution from the **data-generating process** rather than from
the realized parent series, so the interval is not being checked against a
quantity it was partly built from. Vary the world properly — a different seed per
world, and more than one DGP — instead of resampling noise around one scenario
with a fixed inference seed. Raise the pass bar to something with power against
the alternative that actually worries us (true coverage ≈ 0.85). Then add the two
uncovered cases: a **formula node**, whose interval comes entirely from the block
bootstrap and has no coverage test at all today, and a **collinear-parent** tree,
which is S4's failure mode and the shape real trees take. Expect this to fail on
first run — that is the point, and it is why C4 should land first so the failure
is attributable.

**Simulation-based calibration** — `S5`, ○ open (Talts et al., 2018). Draw
parameters from the prior, simulate data, refit, and check that the rank of the
true parameter within the posterior is uniform. This is the definitive test that
the inference is calibrated, and it would turn the current single-scenario
coverage test into a real guarantee. Expensive to run — a nightly or
release-gate job, not a per-commit one. S17 is the cheap complement, not a
substitute: it fixes the test we already rely on, while S5 replaces the class of
guarantee.

**Data-driven bootstrap block length** — `S6`, ○ open (Politis & White, 2004),
replacing the fixed per-grain constants.

**Correlated cold-start beliefs** — `S7`, ○ open. Let authors declare
correlations between priors (or specify a joint distribution over a small set of
beliefs), so "if price lands high, conversion lands low" is representable. This
is the largest modeling gap in cold start.

**Local linear trend as an opt-in** — `S8`, ○ open. A trend with a slope
component, for nodes with genuine momentum, chosen per node in the YAML. Keep
the local level as the default — the tight prior is doing deliberate work — but
stop forcing momentum onto the parents where it does not belong.

**Nonlinear edges, narrowly** — `S9`, ○ open. A declared log or saturating transform on a
specific edge (`response: log` on ad spend → conversions) would cover the most
common nonlinearity without opening the door to arbitrary model complexity.
This fits the MVP-first posture: one named transform, not a modeling language.

**Three gaps the seasonal, event-clocked business exposes** — `S18`, `S19`,
`S20`, all ○ open. Grouped because they were found together, in one external
deployment (a music festival, 2026-08-11) whose shape inverts most of what the
engine's own reference tree assumes: one product cycle a year, five editions of
history in total, months-long true-zero windows between cycles, and revenue that
restates backwards as payment plans settle.

*Right-censored metrics* (`S18`) is the one with no statistical content and the
widest reach. The engine assumes a fetched series is final. A metric whose past
values change — settlement, late-arriving conversions, refunds, chargebacks —
violates that silently, and because the snapshot cache keys on
`(metric, grain, kind, window)` with no content hash, a refit reproduces the
first fetch rather than the warehouse. There is no way to declare that a number
is provisional, so today the only defense is authoring discipline: fit a basis
that never restates, and keep the restating one as a reporting leaf rather than
an RCA target. That is now written down, which is what makes this a disclosed
limitation rather than a trap.

*Partial pooling across cycles* (`S19`) is the thin-panel case, and it is worth
being precise about why it is not the pooling this paper already declined. `S15`
rejected pooling `beta_raw` **across a node's parents**: they are heterogeneous
quantities in different units, and shrinking them toward a common mean has no
substantive justification. Pooling a single node's coefficient **across repeated
instances of its own cycle** is the opposite situation — the instances are the
same quantity measured again, which is the textbook justification for a
hierarchical prior. With five observations, complete pooling and no pooling are
both defensible and partial pooling is almost certainly better than either; the
engine currently offers only the second.

*Zero-inflated and count likelihoods* (`S20`) follows from the dark windows. The
observation model is Gaussian everywhere, and a Gaussian fit to a series that is
exactly zero for a third of its length puts posterior mass on negative counts
and mis-states the variance in the periods that are real. Note that the same
series also triggers `C4`, and the two are genuinely separate: `C4` is a
degenerate *bootstrap* over a constant window, this is a misspecified
*likelihood*. Fixing either alone leaves the other in place — which is the
useful lesson, because the symptom (an implausibly tight interval on a spiky,
mostly-zero parent) looks identical from the outside. **A sequencing note
(2026-08-17):** until the likelihood lands, this misspecification was the one
weakness in this paper's orbit that was *silent* rather than disclosed — such
a fit converges and reports `fit_quality: "ok"`, since the ELBO check (§3.2
#4) says only that the optimizer stopped. The disclosure half shipped the
same day, ahead of the first client deployment and separately from the model
work: a fit whose window is ≥25% exact zeros carries `likelihood_warnings` on
the payload, the UI, the export and MCP, with a `how_to_read` clause beside
it. Closing S20 proper deletes the disclosure.

**Fit through undefined periods by masking the likelihood** — `S21`, ○
optional, build on demand. Roadmap 1.11 settled how a rate behaves when its
denominator is legitimately zero: the fit *refuses* rather than imputes
(`fit_failed`, periods named), because imputing fabricates an observation and
dropping the row re-dates every later period. That refusal is honest, and there
is a third option it declines to take: a missing observation is the case a
state-space model is built for. The latent trend keeps a state at every `t`
whether or not `t` was observed, so the likelihood can simply not condition on
the undefined periods — mechanically, `pm.Normal(..., mu=mu[mask],
observed=y[mask])` — leaving trend, seasonality and regressors untouched. The
node keeps its posterior instead of losing it, and the posterior widens on its
own over the unobserved stretch, which is the honest answer *computed* rather
than stated. It stays optional because refusal degrades gracefully (the node's
own gap still reports, and no shipped tree has a node that is both
probabilistic and gappy), and because two questions must be settled first:
`fit_quality` should carry the observed-period count (a fit on 60 of 100
periods is not a fit on 100), and `MIN_FIT_PERIODS` must count *observed*
periods, or the floor is not a floor.

### 4.3 Explainability

These do not add rigor; they add the ability to *see* it, which is often what
determines whether a correct number gets believed.

**Ship the posterior predictive plot** — `S10`, ✅ **shipped 2026-08-30**. The
Metric tab now carries a *Posterior predictive check* panel: the series the
node was fitted on, against the 50% and 95% quantile bands of the replicates
S3 draws, the median replicate, and the periods the 95% band misses. It is the
same check as S3 with the answer left in its original shape — "`min` failed at
p = 0.016" does not say *where*, and on the demo's `trials_started` the picture
does: a count floored at 6, whose model's 95% band reaches −2.4, drawn against
a zero line.

*The item was scheduled as "nearly free once S3 computes it", and that was
wrong in a way worth recording.* S3 builds its `(500 × n_periods)` replicate
array inside the `pm.Model()` context and **discards** it, deliberately: on
White Cube's `trials_started` at the full window that array is 3.16 MB and its
mean function another 3.16 MB, on every fit, in a cache budgeted in gigabytes.
What persists is four p-values. So S10's real content was choosing what to
keep. It keeps five quantiles and the observed series, computed while the draws
are still in scope — 31.6 kB as numpy, 88 kB as JSON, 0.6% of the 24.6 MB
trace it rides with, and metered rather than assumed. Recomputing behind a
route was rejected outright: `sample_posterior_predictive` needs the model
*graph*, not the trace, so a route would have to refit the node — a minute of
sampling to redraw a chart, and a second posterior that is not the one the
published p-values came from.

*And it deliberately reports no second number.* The panel says how many periods
fall outside the 95% band, without a threshold, a band or a status of its own.
Across four demo nodes that count reads 5.6 / 4.6 / 4.5 / 3.6% while their PPC
verdicts run `severe` / `moderate` / `ok` / `moderate` — it does not separate
them, and publishing it as a verdict would be the same diagnostic answering
twice (§4's S22 entry).

**Prior-versus-posterior visualization** — `S11`, ○ open. Per coefficient: "you
believed 0.1 ± 0.02; the data says 0.08 ± 0.01" makes the Bayesian update
concrete and teaches the model at the same time.

**Make `ranked_causes` visibly a heuristic in the UI** — `S12`, ○ open. Not just
in the docs. Distinguish "ranked by triage score" from "ranked by evidence," or
attach the underlying interval to the ranking so a wide-interval cause cannot
outrank a tight one on a point estimate alone.

**A methods appendix in the exported report** — `S13`, ○ open. The HTML export
already carries a methods footnote; a linkable expansion stating fit window,
inference method, diagnostics, and the specific caveats that applied to *that*
analysis would make an exported RCA self-defending when it circulates without
its author.

**Quantify the DAG assumption** — `S14`, ○ open. A sensitivity statement — "if
an unmodeled confounder explained X% of the parent's movement, this attribution
would change by Y" — would put a number on the assumption everything rests on.
Related work exists under sensitivity analysis for unmeasured confounding;
adapting it to metric trees is a research-flavored project, and the
highest-ceiling idea here. Note this quantifies the assumption rather than
removing it: removing it would mean causal discovery, which is deliberately off
the roadmap (§1.2).

---

## Revision history

Newest first. Material changes only — typo and wording fixes are not logged.

| Date | Change |
|---|---|
| 2026-08-30 | **S10 shipped — the posterior predictive check is now something you can look at.** The Metric tab gains a *Posterior predictive check* panel: the series the node was fitted on, against the 50% and 95% quantile bands of the replicates S3 draws, the median replicate, and the periods the 95% band misses. Same check, answer left in its original shape — *"`min` failed at p = 0.016"* does not say **where**, and on the demo's `trials_started` the picture does: a count floored at 6 whose model's 95% band reaches −2.4, read against a zero line the chart draws deliberately. **The roadmap's "nearly free once S3 computes it" was wrong, and that was the item's real content.** S3 builds its `(500 × n_periods)` replicate array inside the `pm.Model()` context and *discards* it under rule 2 — 3.16 MB for the replicates and 3.16 MB for the mean function on `trials_started`'s 790-day window, on every fit, in a cache budgeted in gigabytes — so what persists is four p-values and no per-period series at all. S10 persists **five quantiles plus the observed series**, computed while the draws are still in scope: 31.6 kB as numpy, 160 kB as the lists that ride on the fit, 88 kB as JSON, **0.6% of the 24.6 MB trace beside it**, and `_trace_nbytes` now counts it (and `dates`, which it had never counted) rather than trusting that ratio to hold. Recomputing on demand was rejected outright: `sample_posterior_predictive` needs the model *graph*, so a route would refit the node — a minute of NUTS to redraw a chart, against a second posterior the published p-values did not come from. It lives on `FitResult` behind `GET /metrics/{name}/ppc`, not in `diagnostics`, because that dict is copied whole onto `GET /metrics/{name}` and its `ppc` block onto every RCA node and every MCP payload — 88 kB × 106 nodes of chart data handed to an agent that cannot see a chart; `test_no_per_node_payload_carries_a_series` makes that structural rather than remembered. **The rendering pass found two defects the payload could not have had.** Plotly's default SI exponent format labels a formula node's identity residual of 4.5e-13 as `400f`, which reads as four hundred of something rather than as machine epsilon; and a five-entry Plotly legend in a 360 px sidebar wraps to five rows drawn *inside* the plot, over the densest part of the series. Fixed with powers of ten on the axis, the UI's own `fmt()` on every hovered number, and an HTML key beneath the chart. A third was a sentence: the caption asserted that fewer than 5% of periods fall outside an in-sample band "here", which is false on the very node the feature was built for (5.6%). Nothing is tinted by verdict — the geometry is the evidence, and a band drawn red on a node already labelled `severe` would be an illustration of a label rather than the thing behind it. |
| 2026-08-29 | **S3 shipped — every fit is now graded against its own posterior predictive distribution, and `fit_quality` finally distinguishes a wrong model from a struggling sampler.** Replicated series are simulated from each fitted posterior and compared with the fitted series on four statistics — `min`, `max`, `resid_max`, `resid_acf1` — as two-sided **mid-p** p-values in two bands (`moderate` p < 0.10, `severe` p < 0.02), published as `ppc_status` / `ppc` / `ppc_warnings` on the fit, on every RCA and what-if node, over MCP with its own `how_to_read` entry and on `explain_metric`, and in the UI. **§3.2 #5 is closed.** `severe` sets `fit_quality: "suspect"`, and because it is the only thing that can do so on a NUTS fit, the meaning of that bit changed: `suspect` on the exact sampler now says the *model* is wrong for the data, where before it could only say the sampler struggled — two failures that want opposite responses, and the UI's suspect-explanation branch had to become conditional so it would stop naming a cause that did not happen. Three measurements shaped the design. **The obvious statistic is vacuous and was cut:** the series' standard deviation matches its replicates by construction, because `y` is z-scored and `sigma_obs` is free — p = 0.479 / 0.499 / 0.521 across a well-specified, a t(3) and a Poisson world, and `resid_sd` the same. A statistic that cannot fail reads as a check that passed, so it is deliberately absent and a test pins the absence. **Mid-p is not a refinement but a correctness fix:** scored with a plain `≥`, a statistic with a point mass returns p = 1.000 on a *correctly* specified node, which put the probe's only false alarm on the good world. **And the bands are wider than convention on purpose** — four statistics per node cross a 0.05 bar 19% of the time by chance, and these are disclosure triggers rather than tests. The worry that motivated the whole calibration — that a local-level trend with one latent per period would absorb every misspecification and make the check vacuous — was measured and did not hold: the tight HalfNormal(0.05) step-size prior keeps the level from chasing noise. On the trees this repo ships the separation is clean (five of six probabilistic nodes `ok` at p = 0.30–0.96, one `moderate`), and the residuals are stated rather than implied: it is an in-sample check, so it grades the likelihood and the mean function and not the DAG, and four statistics is not every way a model can be wrong. **S10 is unblocked** and is the track's next item. |
| 2026-08-27 | **S4 closed: the engine now names the parents whose split of credit the data does not determine.** Correlated parents were already reported honestly — since S2 made NUTS the default, each gets a wide interval and their total a narrow one — but nothing said the two widths were *one ridge measured twice*, and RCA publishes the split per parent. Every fit with two or more regressors now scores its own design matrix (`X` as the model sees it: z-scored, lag-shifted, in `list(dag.predecessors(...))` order) and publishes `collinearity_status` / `collinearity` / `collinearity_warnings` on the fit, on every RCA node, over MCP with a `how_to_read` entry, and in the UI. **The demo set the thresholds, and that is the finding worth recording.** The pair this paper cites in §3.2 #6 — `trial_activation_rate` and `trial_days_active`, whose split lands at 0.680 / 0.682 / 0.697 across BLAS implementations from one seed — measures **\|r\| = 0.863** over the 62 weeks an RCA of that story fits it on (0.941 over the full 112-week window). A single conventional 0.9 bar would therefore have passed *in silence* the exact node the weakness was written about, so the diagnostic carries two bands: `moderate` (\|r\| ≥ 0.7, Dormann et al. 2013; VIF ≥ 5) — their total is sound, the split is soft — and `high` (\|r\| ≥ 0.9; VIF ≥ 10) — the split is not a measured quantity. On the 106-metric reference tree the five multi-parent nodes separate cleanly across those bands: 0.146 and 0.504 `ok`, 0.761 and 0.841 `moderate`, 0.909 with VIF 10.6 `high`. VIF is reported only for three or more parents (with two it is identically 1/(1−r²)) and earns its place on the multi-way degeneracy no pair shows — `x4 = x1+x2+x3` has every pairwise r near 0.58 and every VIF above 100. Two deliberate non-choices: it does **not** move `fit_quality` (a collinear fit is a correct fit that is properly unsure; the hazard is reading one parent's share alone, not the node), and it does **not** attempt to repair the split, because no diagnostic can recover a division of credit the data does not contain — the remedy named in the warning is authorial. §3.2 #6 moves from ○ to ✅ with its residual stated, §4.1's S4 entry carries the shipped design, and `docs/model.md` gains a *Parents that move together* section. S17 still owes the collinear coverage case; the fixture it needs now exists (`tests/test_engine.py`'s `_ridge_frame` / `_separable_frame` / `_multiway_frame`). |
| 2026-08-27 | **S22 closed: k̂ now reports its own error, and the route that publishes it is seeded.** Two independent defects behind one diagnostic. (a) `POST /analyze/{name}` passed no `random_seed` while `run_rca` and `run_scenario` both passed `0`, so the manual-fit route — the one the UI's Analyze button and the "confirm this with NUTS" workflow use — returned a different posterior, and a different k̂, from two identical requests. Re-measured on the White Cube tree at full window before the fix: `customer_churn_rate` **1.23 then 1.91**, `trials_started` **0.94 then 1.19** (the same nodes had given 1.23/0.79 and 0.89/0.77 five days earlier — the spread is itself unstable, which is the argument). All three fitting paths now read one `FIT_RANDOM_SEED`, enforced by an AST-walking invariant test rather than by three call sites happening to agree, and the four demo figures this paper quotes reproduce exactly from the route: 10.18 / 1.07 / 0.85 / 1.36. (b) k̂ is fitted to the tail of 1000 sampled importance ratios (95 tail points), so it is an estimate; `khat_se` now publishes the generalized Pareto shape parameter's asymptotic standard error over that tail, corrected for the shrinkage in ArviZ's empirical-Bayes estimator, and it was validated against the thing it describes — holding one approximation fixed and re-estimating k̂ over 60 draws, published-SE ÷ empirical-SD came out **1.06 / 1.11 / 0.90 / 1.04** across k̂ 0.03 to 1.15. **The size of that error is the finding:** ~0.15 near the 0.5 bar and ~0.2 near k̂ = 1, against a `suspect` band that is 0.2 wide — so that band is frequently not resolvable, and `trial_conversion_rate` on the demo tree is a live case at **0.854 ± 0.172**. Such a fit now carries `khat_borderline: true`, a warning naming the two bands the estimate cannot separate, and `fit_quality: "suspect"` — including from an `ok` k̂, because "not shown to be far from the posterior" is a weaker claim than "close to it". `khat_status` keeps meaning which band the point estimate is in, so nothing downstream had to reinterpret it. Shrinking the error was rejected on cost: the tail grows as √n, so halving the error costs 16× the draws. §3.2 #1's third residual and §4.1's S2 entry are updated; §4 gains an S22 row. |
| 2026-08-24 | **S2 closed by reversing its own second half: NUTS is now the default sampler, ADVI an opt-in that reports its k̂.** The escalation shipped on 2026-08-22 — re-fit a k̂-rejected node with NUTS, capped at four per analysis — was the wrong shape once its own measurement was in. k̂ rejects essentially every real node (10.18 / 1.07 / 0.85 / 1.36 on White Cube at full window, 1.26 / 0.88 / 1.11 / 10.43 on a second, 1.04 on the bundled demo), so a rescue that fires on almost everything is a default in disguise — and a **slower** one, since an escalated node pays for the approximation and then the exact fit (the demo suite ran 225s escalating and runs 175s now). Then the latency case fell over: the 106-metric reference tree, the widest here, contains exactly **five** probabilistic fits, and fitting all five measured **31.3s under ADVI against 28.0s under NUTS** — the exact sampler was the *faster* of the two, on four of the five nodes, and ADVI flagged all five `suspect` where NUTS returned four of five `ok`. So `run_rca`, `run_scenario` and `POST /analyze/{name}` all default to exact MCMC; `?inference_method=advi` is available on all three and reports k̂ on every node it fits; and escalation, its cap, the `escalated` status and the "re-fitted with NUTS" UI badge were deleted. k̂ is a disclosure on a chosen path, not a trigger. **§3.2 #1 moves from ◐ to ✅** — "the default inference method misstates uncertainty" cannot be true of a default with no approximation in it — and its residuals are restated as properties of the opt-in path. §2.2 is rewritten (the *triage with ADVI, confirm with NUTS* posture is retired), §4's current state and §4.1's S2 entry carry the new measurements, and §3.2 #6 no longer compounds with #1 on the default path. Three figures worth keeping. Across the four demo stories `sessions ← marketing_spend` moves −108.2 → −68.8, 178.1 → 114.0, 88.4 → 62.7 and 202.0 → 129.0 between the samplers; the story B engagement edge moves the *other* way, −0.049 → −0.077; and the verdict flips in both directions — that edge's β HDI goes from straddling zero to excluding it, while `trial_conversion_rate ← trial_days_active` goes from excluding it to straddling it. A fourth is the honest limit of the opt-in: story D's `trial_conversion_rate` has k̂ **0.497**, inside the good band, and its published share still moved 70.7% → ~68%. A passing k̂ bounds the error; it does not zero it. **One thing measured on the way and recorded on §3.2 #6:** that same node's two parents are deliberately collinear, and their split of the gap comes out 0.680 / 0.682 / 0.697 on three numeric stacks *from the same seed*, with no variance inside a stack — a 1.6-point spread that is the BLAS rather than the sampler. The pair's sum is determined; the split is not. Evidence for **S4**, and the reason that figure is banded in the demo suite and written "about 70%" in the tour rather than pinned to a decimal. |
| 2026-08-22 | **S2 shipped — the engine can now tell a good approximation from a bad one, and the answer on real trees is "bad".** Every variational fit is scored with PSIS k̂ (Yao et al., 2018), computed from PyMC's own `sized_symbolic_logp` / `symbolic_logq` cloned against one shared sample and smoothed with `arviz.psislw`; three bands (`ok` ≤ 0.5, `suspect` ≤ 0.7, `unusable` above) plus `escalated` and `unavailable`, on the fit's diagnostics, on every RCA and what-if node, over MCP with its own `how_to_read` entry, and in the UI beside R̂. `run_rca` and `run_scenario` re-fit an `unusable` node with NUTS, capped at four per analysis. **§3.2 #4 is closed outright** — the ELBO-only check is no longer the only thing standing between an approximation and its published intervals — and **§3.2 #1 moves from ○ to ◐**, because the default path is no longer the optimistic one. It is not ✅, and the paper says why: the escalation cap, the un-escalated `suspect` band, the fact that k̂ judges the *joint* posterior and cannot say which marginal is wrong, and k̂'s own unreported Monte-Carlo error (new **S22**). The measurement is the part worth recording. k̂ flags **nearly every probabilistic node on every tree this repo ships** — 10.18 / 1.07 / 0.85 / 1.36 on White Cube at full window (`random_seed=0`), 1.04 on the bundled demo — including the two whose ELBO check said `ok`, which is exactly the failure S2 was specified to catch. The cause is the engine's own geometry (one correlated trend latent per period), not the diagnostic: the same code returns 0.31 on a 30-dimensional posterior mean-field can represent and 0.95 on Neal's funnel. So S2 did not become an occasional rescue; it made RCA on a small tree mostly-NUTS, at 3–6× the wall clock. What that buys is bigger than interval width: on the demo's story A, `sessions ← marketing_spend` moves from −108.2 to −68.8 (0.68 → 0.43 of the gap), and on story B the learned `member_activity_rate → customer_churn_rate` contribution moves −0.049 → −0.077 — two independent published **point estimates**, each off by 57%, and in *opposite* directions. The trend delta is what moves consistently (mean-field collapses it toward zero every time); β absorbs the rest in whichever direction that node's ridge runs, so the sign of the error is not recoverable from the payload. That is the β-vs-trend ridge S1 predicted, and it rules out disclosing the bias in place of fixing it. §4.1's S2 entry carries the full account and §4's current-state paragraph moves to S3. |
| 2026-08-18 | **S1 ran — the S track's first item closed with a rejection, and the paper is more precise for it.** Full-rank ADVI was benchmarked against mean-field and NUTS on the calibration DGP, a drifting-parent (β-vs-trend ridge) suite, and the White Cube tree's three probabilistic nodes ([`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md)). §3.2 #1 now carries measured numbers instead of theory — mean-field ≈0.8× the NUTS width on the ridge — and stays open; §3.2 #4 gained its strongest evidence yet, in both directions: a full-rank fit passed the ELBO check while 7.8× over-dispersed on a real node, and mean-field at its shipped step count is `suspect` on all three real nodes. §4.1's S1 entry records why the candidate fix failed on the real tree (the O(d²) covariance over per-period trend latents is slow *and* mis-fit at real window sizes — the same paragraph's "far faster than NUTS" prior was false there), and S2 — detection plus escalation — is next, repriced by S1's timing data: NUTS at 11s on an 830-day daily fit and 11–66s per real weekly node makes escalation affordable. The measurement's sharpest single fact: across the suites, VI error on this model is unpredictable even in *direction* — mean-field 0.8× the NUTS width on the short ridge, 2.1× over at 830 periods (unconverged at its shipped step count, and flagged), full-rank 7.8–12× over at realistic sizes (converged, and not flagged) — which is the case for measuring distance-to-posterior rather than optimizer convergence, i.e. for S2 as specified. |
| 2026-08-17 | **C7 shipped — and with it Horizon 0 closed, every row ✅.** §2.7 and §3.2 #12 updated: cold-start baseline draws are truncated to declared `plausible` bounds by rejection resampling (not clipping — clipping piles a point mass on the bound, a belief the author never stated), a `LogNormal` baseline reads `[low, high]` on the log scale for order-of-magnitude beliefs, and a formula dividing by a belief whose draws cross zero is refused with the remedies named. The decision worth recording: the roadmap row said "stop reporting the Monte-Carlo mean as the central number on ratio nodes," and the shipped fix deliberately does not — it makes the mean *exist* instead (truncation + refusal), because the mean's linearity is what keeps per-source Shapley contributions summing exactly to the delta; a median centre was considered and rejected for breaking that property on the one surface whose numbers are already the least evidential. §4's gate paragraph now records the correctness gate as closed; what remains at cold start is S7's correlation gap, unchanged. |
| 2026-08-17 | **Caught the paper up to four days of shipped work it had missed, and recorded a fourth audit.** Three corrections this paper owed its reader: §4's "current state (2026-08-05): all items open" was twelve days stale; its claim that "C4 blocks any honest reading of S17's rebuilt coverage test" had been false since C4 shipped on 2026-08-13 (S17 is unblocked; a reader planning the S track off that paragraph would have deferred it for a dead reason); and roadmap 1.11 — the largest *statistical* change since this paper was written — appeared nowhere: §2.9 now carries it (a window's rate is `Σnum/Σden`, which moved every rate's published `baseline`/`actual`/`relative_change` by a measured 0.02%–10.9%; undefined periods drop out of both sums; the fit refuses rather than imputes; an undeclared denominator's fallback is labelled in the payload). §4 gained `S21` (mask the likelihood over undefined periods — the state-space option 1.11's refusal declined) and S20 gained a scheduled disclosure half ahead of the first client deployment, because a Gaussian fit to a mostly-zero series currently reports `fit_quality: "ok"` with nothing anywhere saying otherwise — the one weakness in this paper's orbit that was silent rather than disclosed. §3.3 records the [2026-08-17 milestone-readiness audit](milestone_readiness_2026_08_17.md): the four recent policy decisions all held; what leaked was propagation, at the three boundaries without a structural test (engine→MCP, engine→UI, metric-path→slice-path), filed as roadmap C23–C25. No §3.2 weakness changed status. |
| 2026-08-13 | **Filter support shipped (roadmap 2.17), and §3.3 needed one clause rather than a rewrite.** That section recorded C15's fix as *"filtered metrics are now refused by name until the binding language can carry a predicate"*, which was true when written and would now tell a reader that every filtered dbt metric is skipped. It is not: a filter whose every reference resolves to a categorical dimension on the measure's own relation is compiled into the generated SQL, and one that reaches across a join, into a time dimension, or into a `Metric()` call is still refused by name. No weakness changed status and none was added — this is a capability, not a statistical position — but the sentence mattered because the whole point of §3.3 is what this engine does at the seam where it meets someone else's data, and *resolve-totally-or-refuse* is a different answer from *always refuse*. The property that has not changed is the one worth stating: the only failure mode at this seam is still refusal. |
| 2026-08-13 | **C4, C5 and C6 shipped — and taking C4's measurement moved a weakness rather than closing it.** §3.2 #2 (short-window bootstrap) and #8 (`ranked_causes`) both changed status: #2's two named defects are fixed, #8's inversion is fixed and only its S12 framing half remains. But measuring the block cap in order to justify it turned up something nobody had looked for: **`BOOT_BLOCK["day"] = 7` resonates with a weekly cycle** — a 7-day block holds each weekday exactly once, so a weekly seasonal component cancels identically in every replicate and the shipped default sits at a *local minimum* of the honest interval width, roughly a third of the width at block 3 on the demo tree. Every daily-grain interval on a weekday-seasonal metric has been optimistic by a factor no cap can correct, and that now sits under [S6](roadmap.md#statistical-rigor-s--a-standing-workstream), whose row was rewritten from "the current values are reasonable guesses" to a measured indictment of one of them. So the honest summary of C4 is that it closed two defects and *promoted* a third from unsuspected to disclosed — which is what measurement is for, and is why "read a short-window formula CI as a lower bound" survives in a narrower form instead of disappearing. #8 also carries a correction to this paper's own framing: it called the `ranked_causes` inversion a near-zero-gap edge case, and it reproduces on the bundled demo tree over an ordinary fortnight on a gap nowhere near zero. |
| 2026-08-12 | **A fifth defect at the provider boundary, found by re-checking the review rather than by a new one** — the 2026-08-12 review was frozen at `c18d150`; re-verifying all 33 of its findings against `e433daa` confirmed 28 still live and promoted two into [Horizon 0](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) that it had filed lower. `C18` is the one this paper owes text: a `flow` metric whose source starts partway into the loaded window was zero-filled back to the window's start **silently**, and the fit trained on the invented periods. §3.3 is updated, and its counting claim with it — this seam has produced five of six, not four of five. The location is the finding: `_align_to_spine` is the shared contract `C1`/`C2` built to end this class, and it warns correctly about *interior* gaps three lines away. `C17` (a zero denominator in a formula reaching the encoder as a NaN, and an agent payload as `null`) is the other promotion; it is an engine defect rather than a boundary one and adds no §3.2 weakness. No weakness changed status. Recorded because a reader comparing editions should see that §3.3's "the failure modes are documented rather than hidden" survived a second audit only after two more exceptions were fixed — and that both were found by re-reading a report, not by a third review. |
| 2026-08-12 | **Two new provider-boundary defects, found and fixed the same day** — a second hostile review (against `c18d150`, scoped to the first client deployment and PyPI publication) found that a dbt metric's `filter` was silently dropped (`C15`) and that a snapshot survived an edit to the metric's own `sql:`/`bind:` block (`C16`), with `query_provenance` then attesting the new statement for the old numbers. Both shipped. No weakness changed status and none was added to §3.2 — both are engineering defects rather than statistical ones. What changed is §3.3, which said flatly that *the* two provider-boundary defects were fixed; that was true of C1/C2 and would have told a reader this boundary was sound. It now records four of the project's five silent-wrong-number defects at this one seam and draws the inference — treat a number's provenance as the least-tested thing here — rather than reporting each instance as a surprise. C15 also falsified a **roadmap** claim rather than a paper one: 2.10 had listed `filters` among what the dbt binding shipped, and they had never worked; that row now says so and real support is filed as 2.17. |
| 2026-08-11 | **An outside deployment on a shape this paper had not considered** — a music festival: one product cycle a year, five editions of history, a demand clock in days-to-event, months-long true-zero windows, and revenue that restates backwards. §4 gained `S18` (right-censored metrics), `S19` (partial pooling across cycles) and `S20` (zero-inflated/count likelihoods), with a §4.2 entry explaining why `S19` is not the pooling `S15` declined — S15's objection was to pooling across a node's *heterogeneous parents*, which does not apply to pooling one node's coefficient across repeated instances of its own cycle. No weakness changed status and none was added: `C4`'s degenerate-bootstrap failure was **confirmed in production** rather than newly found, on a parent held identically at zero across a whole reference window, and the roadmap row now records the measured instance. Worth logging that the deployment's own workarounds were sound — an expected-pacing *regressor* is the correct encoding of a non-repeating event clock, not a second-best one, since this engine's seasonality is Fourier in integer time and therefore strictly periodic. |
| 2026-08-08 | **C10 shipped** — no weakness changed status, but §3.2 #6 (parent collinearity) and #7 (the coverage test) each made a factual claim that C10 falsified: both cited the reference tree as containing a live collinear structure. It was removed there rather than preserved as a specimen, since that file is the one new authors copy — so #6 now records that the structure *was* there and how easily it was authored, and #7 records that S17's collinear fixture has to be built rather than borrowed. The diagnostic itself is still missing; nothing about the engine's statistical position improved. |
| 2026-08-05 | **C3 shipped** — no text changed. §2.3 already claimed that contributions "sum to the true gap exactly" and that `unexplained` on a formula node is "measurement residual only"; that was true of `GET /shapley` and false of what RCA published, which reported a bootstrap mean of a nonlinear decomposition instead. The code now matches the paper rather than the paper being softened to match the code. Logged because a reader comparing editions should be able to see that this section's meaning changed even though its words did not. |
| 2026-08-05 | **C1/C2 shipped** — the two provider-boundary correctness defects are fixed, and §3.3 now says so, including what to re-run. Every provider shares one date-alignment contract (tz coercion, period spine, trailing trim, kind-aware interior fill). No §3.2 weakness changed status: neither defect was ever a numbered statistical weakness, which is exactly why §3.3 had to carry them. |
| 2026-08-05 | **Acted on a hostile external review** of the engine, docs and tests (against 0.1.0). §3.2 gained four weaknesses it had not named — the short-window bootstrap attenuation (#2, `C4`), unacknowledged multiplicity and selection (#3, `S15`), the horizon-invariant trend interval (#9, `S16`), and the structural bias in the coverage test this paper had offered as its headline calibration evidence (#7, `S17`) — and #6, #8, #10 and #12 were amended with the specific defects behind them. §3.3's unqualified claim that the failure modes are "documented rather than hidden" was **corrected**: it was not fully earned, and the exceptions are now enumerated as the roadmap's [Horizon 0](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) correctness gate, which runs ahead of the S track. §4 gained `S15`/`S16`/`S17` with rationale, and `S4` (parent collinearity) was promoted. |
| 2026-08-05 | §3.2 and §4 given status markers and roadmap IDs; §4 items registered as the roadmap's [Statistical rigor (S) workstream](roadmap.md#statistical-rigor-s--a-standing-workstream), sequenced to start after the 0.1.0 release with S1 first. Added full-rank ADVI (S1) as a §4.1 item, split out of the ADVI diagnostic. Cross-linked [`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md) from §2.2 and §3.2. |
| 2026-08-04 | First version, against engine 0.1.0. Written after the roadmap 1.1 statistical hardening work (window validation, date-spine contiguity, Nyquist harmonic filtering) — §2.10 describes those guards as shipped. |

---

## References

Balk, B. M. (2008). *Price and Quantity Index Numbers: Models for Measuring
Aggregate Change and Difference.* Cambridge University Press.

Bennet, T. L. (1920). The theory of measurement of changes in cost of living.
*Journal of the Royal Statistical Society*, 83(3), 455–462.

Betancourt, M. (2017). A conceptual introduction to Hamiltonian Monte Carlo.
arXiv:1701.02434.

Betancourt, M., & Girolami, M. (2015). Hamiltonian Monte Carlo for hierarchical
models. In *Current Trends in Bayesian Methodology with Applications.*
arXiv:1312.0906.

Blei, D. M., Kucukelbir, A., & McAuliffe, J. D. (2017). Variational inference: A
review for statisticians. *Journal of the American Statistical Association*,
112(518), 859–877.

Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015).
Inferring causal impact using Bayesian structural time-series models. *The
Annals of Applied Statistics*, 9(1), 247–274.

Cinelli, C., Forney, A., & Pearl, J. (2022). A crash course in good and bad
controls. *Sociological Methods & Research.*

Durbin, J., & Koopman, S. J. (2012). *Time Series Analysis by State Space
Methods* (2nd ed.). Oxford University Press.

Gabry, J., Simpson, D., Vehtari, A., Betancourt, M., & Gelman, A. (2019).
Visualization in Bayesian workflow. *Journal of the Royal Statistical Society:
Series A*, 182(2), 389–402.

Gelman, A. (2006). Prior distributions for variance parameters in hierarchical
models. *Bayesian Analysis*, 1(3), 515–534.

Gelman, A., & Rubin, D. B. (1992). Inference from iterative simulation using
multiple sequences. *Statistical Science*, 7(4), 457–472.

Gelman, A., Carlin, J. B., Stern, H. S., Dunson, D. B., Vehtari, A., & Rubin,
D. B. (2013). *Bayesian Data Analysis* (3rd ed.). CRC Press.

Gelman, A., Vehtari, A., Simpson, D., et al. (2020). Bayesian workflow.
arXiv:2011.01808.

Harvey, A. C. (1989). *Forecasting, Structural Time Series Models and the Kalman
Filter.* Cambridge University Press.

Hernán, M. A., & Robins, J. M. (2020). *Causal Inference: What If.* Chapman &
Hall/CRC.

Hoffman, M. D., & Gelman, A. (2014). The No-U-Turn Sampler: Adaptively setting
path lengths in Hamiltonian Monte Carlo. *Journal of Machine Learning Research*,
15, 1593–1623.

Kucukelbir, A., Tran, D., Ranganath, R., Gelman, A., & Blei, D. M. (2017).
Automatic differentiation variational inference. *Journal of Machine Learning
Research*, 18(14), 1–45.

Kumar, R., Carroll, C., Hartikainen, A., & Martin, O. (2019). ArviZ: a unified
library for exploratory analysis of Bayesian models in Python. *Journal of Open
Source Software*, 4(33), 1143.

Künsch, H. R. (1989). The jackknife and the bootstrap for general stationary
observations. *The Annals of Statistics*, 17(3), 1217–1241.

Lahiri, S. N. (2003). *Resampling Methods for Dependent Data.* Springer.

Lundberg, S. M., & Lee, S.-I. (2017). A unified approach to interpreting model
predictions. *Advances in Neural Information Processing Systems*, 30.

Neal, R. M. (2003). Slice sampling. *The Annals of Statistics*, 31(3), 705–767.

Papaspiliopoulos, O., Roberts, G. O., & Sköld, M. (2007). A general framework
for the parametrization of hierarchical models. *Statistical Science*, 22(1),
59–73.

Pearl, J. (2009). *Causality: Models, Reasoning, and Inference* (2nd ed.).
Cambridge University Press.

Pearl, J., Glymour, M., & Jewell, N. P. (2016). *Causal Inference in Statistics:
A Primer.* Wiley.

Politis, D. N., & Romano, J. P. (1992). A circular block-resampling procedure
for stationary data. In *Exploring the Limits of Bootstrap.* Wiley.

Politis, D. N., & White, H. (2004). Automatic block-length selection for the
dependent bootstrap. *Econometric Reviews*, 23(1), 53–70.

Salvatier, J., Wiecki, T. V., & Fonnesbeck, C. (2016). Probabilistic programming
in Python using PyMC3. *PeerJ Computer Science*, 2, e55.

Scott, S. L., & Varian, H. R. (2014). Predicting the present with Bayesian
structural time series. *International Journal of Mathematical Modelling and
Numerical Optimisation*, 5(1–2), 4–23.

Shannon, C. E. (1949). Communication in the presence of noise. *Proceedings of
the IRE*, 37(1), 10–21.

Shapley, L. S. (1953). A value for n-person games. In *Contributions to the
Theory of Games II*, Annals of Mathematics Studies 28, 307–317.

Shorrocks, A. F. (2013). Decomposition procedures for distributional analysis: a
unified framework based on the Shapley value. *Journal of Economic Inequality*,
11, 99–126.

Simpson, D., Rue, H., Riebler, A., Martins, T. G., & Sørbye, S. H. (2017).
Penalising model component complexity: A principled, practical approach to
constructing priors. *Statistical Science*, 32(1), 1–28.

Talts, S., Betancourt, M., Simpson, D., Vehtari, A., & Gelman, A. (2018).
Validating Bayesian inference algorithms with simulation-based calibration.
arXiv:1804.06788.

Taylor, S. J., & Letham, B. (2018). Forecasting at scale. *The American
Statistician*, 72(1), 37–45.

Vehtari, A., Gelman, A., & Gabry, J. (2017). Practical Bayesian model evaluation
using leave-one-out cross-validation and WAIC. *Statistics and Computing*, 27,
1413–1432.

Vehtari, A., Gelman, A., Simpson, D., Carpenter, B., & Bürkner, P.-C. (2021).
Rank-normalization, folding, and localization: An improved R̂ for assessing
convergence of MCMC. *Bayesian Analysis*, 16(2), 667–718.

Yao, Y., Vehtari, A., Simpson, D., & Gelman, A. (2018). Yes, but did it work?:
Evaluating variational inference. *International Conference on Machine
Learning*, 35.

Young, H. P. (1985). Monotonic solutions of cooperative games. *International
Journal of Game Theory*, 14, 65–72.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

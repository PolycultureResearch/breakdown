# The Statistics of Breakdown

**A white paper on the models behind Bayesian metric trees, why each was chosen,
and where each one stops being trustworthy.**

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

**Where they are used.** NUTS is the default for `POST /analyze` — the
"I want the real answer" path. ADVI is the default for on-demand fits inside
RCA and simulation, where a tree may need a dozen fits to answer one question.

**What they are.** **NUTS** — the No-U-Turn Sampler (**Hoffman & Gelman, 2014**)
— is an adaptive form of Hamiltonian Monte Carlo that explores the posterior by
simulating physical trajectories, automatically tuning path length. It is
asymptotically exact: run it long enough and you have the true posterior.
**ADVI** — Automatic Differentiation Variational Inference (**Kucukelbir et
al., 2017**) — instead *optimizes*: it fits the closest tractable distribution
(here, a mean-field Gaussian, i.e. independent per parameter) to the posterior
by maximizing a lower bound on the evidence (the ELBO).

**Why both.** This is a straightforward accuracy/latency trade, made explicit
rather than hidden. An RCA over a ten-node subtree needs a fit per probabilistic
node; NUTS everywhere would make the interaction unusable, and ADVI is roughly
5–10× faster. The engine's posture is **triage with ADVI, confirm with NUTS** —
and the API makes confirming a one-parameter change
(`?inference_method=nuts&fit_end=<analysis_start>`), so a finding that matters
can always be re-run exactly.

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

- **Mean-field ADVI understates uncertainty.** This is the important one. By
  assuming parameters are independent, it cannot represent posterior
  correlations, and it systematically produces *too-narrow* intervals. Since
  ADVI is the RCA default, **the credible intervals in a default RCA response
  are, if anything, optimistic.** Yao et al. (2018) is the standard treatment of
  how badly this can go and how little the ELBO tells you about it.
- **The ADVI diagnostic is weak.** An ELBO-convergence check confirms the
  optimizer stopped moving. It does *not* confirm the approximation is close to
  the true posterior — a well-converged bad approximation passes. Stronger
  diagnostics exist (PSIS-based k̂, Yao et al. 2018) and are not implemented.
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
interval of a Normal and sampled per draw. Each edge's slope is sampled directly
from its YAML prior. With nothing to fit, **the prior *is* the coefficient
distribution** — which is not a workaround but the correct Bayesian statement of
the situation.

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

Ordered roughly by how much it should worry you.

1. **The default inference method understates uncertainty.** RCA defaults to
   mean-field ADVI, which cannot represent posterior correlation and produces
   systematically narrow intervals. The escape hatch (re-run with NUTS) exists
   and is documented, but **the default path is the optimistic one**, and most
   users will never leave it. This is the single largest gap between what the
   intervals claim and what they deliver.
2. **The ADVI quality check is not a real approximation diagnostic.** ELBO
   convergence says the optimizer stopped; it says nothing about closeness to
   the true posterior.
3. **No posterior predictive checks.** The engine never asks "could this fitted
   model have generated data that looks like what I saw?" — the single most
   informative Bayesian model check there is (Gelman et al., 2020; Gabry et al.,
   2019). A badly misspecified node currently passes silently as long as it
   converges.
4. **No collinearity diagnostic on parents.** Correlated parents produce a
   well-determined *sum* and an unstable *split*, and the split is exactly what
   RCA reports. Nothing warns.
5. **Interval calibration is tested at one point, coarsely.** The 20-world
   coverage test is real and valuable, but it is a single scenario with an 80%
   pass bar for a nominal 95% interval. There is no simulation-based calibration
   (Talts et al., 2018) across the prior, and no coverage testing across grains,
   window lengths, or tree shapes.
6. **The bootstrap is bolted on, not integrated.** Composing a frequentist
   resampling interval with a Bayesian posterior is pragmatic and defensible but
   not a coherent joint posterior; block length is fixed rather than estimated.
7. **`ranked_causes` is a heuristic and reads like a result.** The score
   propagates |share| products up the tree. It is explicitly documented as
   triage rather than probability, but it is also the most prominent number in
   the UI, and prominence implies rigor whatever the docs say.
8. **The flat trend forecast understates counterfactual movement** for nodes
   with genuine momentum (§2.4).
9. **Cold-start beliefs are sampled independently,** so correlated beliefs are
   misrepresented in either direction.
10. **Causal language rests entirely on the declared DAG.** This is disclosed
    everywhere and remains the assumption most likely to be violated in
    practice — not because users are careless, but because business metric
    graphs have confounders (a pricing change that moves traffic *and*
    conversion) that are invisible unless someone thought to declare them.

### 3.3 The fair summary

breakdown is **more rigorous than the analytics tools it competes with and less
rigorous than a careful bespoke analysis by a statistician.** Its exact
half is genuinely exact. Its probabilistic half is well-specified and honestly
reported, but the default inference approximation is optimistic and the model-
checking layer is thin. The engine's real achievement is *structural*: it is
built so that adding rigor does not require re-architecting anything, because
uncertainty already flows everywhere it needs to.

Most importantly, the failure modes above are **documented rather than
hidden** — which is the difference between a tool with known limitations and a
tool that is quietly wrong.

---

## 4. Where to go next

Ordered by value per unit of effort.

### 4.1 Highest value

**Posterior predictive checks on every fit.** For each fitted node, simulate
replicated series from the posterior and compare summary statistics against the
observed series; flag nodes whose observed data sits in the tail. This is the
standard Bayesian workflow check (Gelman et al., 2020), it needs no new
modeling, and it would catch misspecification that convergence diagnostics
cannot see. It composes with the existing `fit_quality` channel.

**A real ADVI approximation diagnostic.** Implement the PSIS-based k̂ diagnostic
of Yao et al. (2018) so a bad variational approximation is *detected* rather
than assumed away. Where k̂ is poor, either auto-escalate that node to NUTS or
mark its intervals as unreliable. This directly addresses weakness #1 without
paying NUTS cost everywhere.

**Counterfactual RCA via posterior predictive forecast** (already roadmap 3.4 /
T11). Instead of comparing window means, forecast the analysis window from the
normal-regime model and report the *departure*: "revenue came in 12,400 below
what the normal regime predicts (95% CI …)." This is the full Brodersen et al.
(2015) pattern, it replaces the flat-trend approximation (§2.4), and it produces
a considerably stronger headline number than a window-mean difference.

**A parent collinearity diagnostic.** Compute pairwise correlation (or VIF) among
a node's parent regressors over the fit window and warn when the split of credit
is unstable. Cheap, and it addresses a silent failure that directly corrupts
what RCA reports.

### 4.2 Substantial but well-scoped

**Simulation-based calibration** (Talts et al., 2018). Draw parameters from the
prior, simulate data, refit, and check that the rank of the true parameter
within the posterior is uniform. This is the definitive test that the inference
is calibrated, and it would turn the current single-scenario coverage test into
a real guarantee. Expensive to run — a nightly or release-gate job, not a
per-commit one.

**Data-driven bootstrap block length** (Politis & White, 2004), replacing the
fixed per-grain constants.

**Correlated cold-start beliefs.** Let authors declare correlations between
priors (or specify a joint distribution over a small set of beliefs), so
"if price lands high, conversion lands low" is representable. This is the
largest modeling gap in cold start.

**Local linear trend as an opt-in.** A trend with a slope component, for nodes
with genuine momentum, chosen per node in the YAML. Keep the local level as the
default — the tight prior is doing deliberate work — but stop forcing momentum
onto the parents where it does not belong.

**Nonlinear edges, narrowly.** A declared log or saturating transform on a
specific edge (`response: log` on ad spend → conversions) would cover the most
common nonlinearity without opening the door to arbitrary model complexity.
This fits the MVP-first posture: one named transform, not a modeling language.

### 4.3 Explainability

These do not add rigor; they add the ability to *see* it, which is often what
determines whether a correct number gets believed.

**Ship the posterior predictive plot.** Once §4.1 computes it, showing observed
versus replicated series per node is the most persuasive single visual a
Bayesian tool can offer.

**Prior-versus-posterior visualization** per coefficient — "you believed 0.1
± 0.02; the data says 0.08 ± 0.01" makes the Bayesian update concrete and
teaches the model at the same time.

**Make `ranked_causes` visibly a heuristic in the UI,** not just in the docs.
Distinguish "ranked by triage score" from "ranked by evidence," or attach the
underlying interval to the ranking so a wide-interval cause cannot outrank a
tight one on a point estimate alone.

**Quantify the DAG assumption.** A sensitivity statement — "if an unmodeled
confounder explained X% of the parent's movement, this attribution would change
by Y" — would put a number on the assumption everything rests on. Related work
exists under sensitivity analysis for unmeasured confounding; adapting it to
metric trees is a research-flavored project, and the highest-ceiling idea here.

**A methods appendix in the exported report.** The HTML export already carries a
methods footnote; a linkable expansion stating fit window, inference method,
diagnostics, and the specific caveats that applied to *that* analysis would make
an exported RCA self-defending when it circulates without its author.

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

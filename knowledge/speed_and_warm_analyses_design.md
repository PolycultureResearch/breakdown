# A faster fit, a visible one, and an analysis ready before it is asked for

**Status:** design, not implementation. Opened 2026-09-30. Roadmap
[S25](roadmap.md#statistical-rigor-s--a-standing-workstream) (the Kalman
trend), [1.13](roadmap.md#horizon-1--prove-it-a-trustworthy-reproducible-rca)
(showing the work) and
[3.10](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you)
(warm analyses and suggested windows), all ○.

**The ask.** A cold RCA is slow enough to hurt the product. On the demo it is
worse than slow: every "Deploy demo" run since 2026-08-24 has failed because
story B's prewarm passes 45 minutes on Fly's `shared-cpu-2x`
([PR #142](https://github.com/PolycultureResearch/breakdown/pull/142) moves the
demo off it). The question was: can anything be made faster? If not, can the
wait be made legible, and can the fit happen before the analyst asks?

Something can be made faster, and it is most of the wait. The other two
questions have answers anyway, and they compound with the speed-up rather than
replace it.

---

## 1. Where the time goes

Measured 2026-09-30 on an Apple M2 Max (12 cores), `main` at `a2c85c8`, the
White Cube demo tree from its committed snapshots, story B
(`net_new_mrr`, analysis 2026-05-11 → 2026-06-07).

**A cold RCA takes 66.4s and a warm one 0.7s.** Everything that is not a fit
(attribution, bootstrap, Shapley, reference sensitivity) costs under a second.
The wait is the four NUTS fits, run one after another behind the tree's lock.

| node | grain | periods | leapfrog steps / draw | fit |
|---|---|---|---|---|
| `sessions` | day | ~710 | **262.6** | **32.5s** |
| `trials_started` | week | ~100 | 31.5 | 6.8s |
| `trial_conversion_rate` | week | ~100 | 31.0 | 3.6s |
| `customer_churn_rate` | week | ~100 | 63.0 | 3.4s |

One node is 70% of the story. `sessions` has seven times the periods of the
others, and NUTS needs 8× the steps per draw on it: trajectories of about 2⁸
leapfrog steps, where the weekly nodes need about 2⁵.

**Why.** The local level is written non-centered,
`trend = cumsum(σ_trend · z)` with one `z[t] ~ N(0,1)` per period. Each `z[t]`
moves every later point, so on an informative series the posterior over the
`z` is a long, tightly correlated ridge. The non-centered form was chosen to
avoid the centered walk's funnel (`fit_metric`'s docstring), and it does. The
cost is that the sampler integrates a 700-dimensional coupled latent with tiny
steps. This is a known weakness of both parameterizations of a random walk
when there are many observations per unit of level variance.

**Ruled out, measured:**

| lever | result |
|---|---|
| `progressbar=False` | no change on 12 cores (47.3s vs 47.6s). On a 4-core box the main process's rendering competes with 4 chains, so it is still worth doing, and free |
| chains in sequence (`cores=1`) | 3.1× slower (147.6s) |
| nutpie on the current model | 2.7× faster on the clock (17.0s), but minimum bulk ESS falls (sessions 460 → 197) and `trial_conversion_rate` goes `suspect` (ESS 92, R̂ 1.06, 17 divergences). It likely does not receive our `target_accept=0.9`. Per effective sample, perhaps 1.3×. Not a win on its own |
| fewer draws / shorter tune | not tried, and deliberately not proposed: it buys time with Monte-Carlo error, which is the wrong currency for this engine |

**A small free win, independent of everything below.** `_nuts_diagnostics`
calls `az.summary(trace)` over every posterior variable, including the
710-long `trend` deterministic: about 1.5s per fit (6s of the 66s profile). It
also means today's `min_ess_bulk` is partly the ESS of trend *states*, not of
parameters. S25 changes what is in the posterior anyway (§2.3), so that
question is settled there.

---

## 2. S25 — integrate the level out with a Kalman filter

### 2.1 The model does not change

With every other term fixed, the model is a linear Gaussian state-space model:

```
y[t]     = μ[t] + level[t] + ε[t],        ε[t] ~ N(0, σ_obs²)
level[t] = level[t-1] + η[t],             η[t] ~ N(0, σ_trend²),  level[-1] = 0
μ[t]     = α + seasonal[t] + (X β)[t] + (I β_iv)[t]
```

That is today's model exactly: `cumsum(σ_trend · z)` *is* this random walk,
with `level[0] ~ N(0, σ_trend²)`. For a linear Gaussian state-space model the
likelihood with the level integrated out, `p(y | α, β, σ_trend, σ_obs, …)`, is
computed exactly by the Kalman filter's prediction-error decomposition
(Harvey, 1989; Durbin & Koopman, 2012, ch. 7). NUTS then samples the handful
of parameters, and the level posterior is recovered afterwards by exact
conditional draws. **The posterior is the same posterior.** What changes is
only how it is computed. Published numbers move within Monte-Carlo error, and
that error shrinks.

### 2.2 Measured (prototype)

A scratch prototype replaced the two trend lines and the observation node with
a `pm.Potential` holding a scalar Kalman filter written as a `pytensor.scan`,
and stopped after sampling. Same data, same `fit_end`, same seed, same priors.

`sessions`:

| | today | Kalman, C backend | Kalman, nutpie | **Kalman, PyMC NUTS + `mode="NUMBA"`** |
|---|---|---|---|---|
| fit | 32.5s | 91.8s | 13.1s | **10.0s** |
| steps / draw | 262.6 | 6.8 | 6.6 | 6.9 |
| min bulk ESS | 460 | 1971 | 1860 | 1954 |
| divergences | 0 | 0 | 0 | 0 |
| β (mean, 94% HDI) | 0.599 | 0.600 [0.560, 0.639] | 0.600 | — |

All four story-B nodes, one process, PyMC NUTS with `compile_kwargs={"mode": "NUMBA"}`:

| node | today | Kalman | quality today → Kalman |
|---|---|---|---|
| `sessions` | 32.5s | 8.7s | ok → ok (ESS 460 → 2072) |
| `trials_started` | 6.8s | 6.4s | **suspect (10 div) → ok (0 div)** |
| `trial_conversion_rate` | 3.6s | 2.4s | ok (6 div) → ok (0 div) |
| `customer_churn_rate` | 3.4s | 1.6s | ok → ok |
| **total** | **~46s** | **19.0s** | |

β agrees to the third decimal on every node where it was printed
(`trials_started` 0.931 → 0.931, `trial_conversion_rate` [0.427, 0.305] →
[0.420, 0.307], `customer_churn_rate` −0.173 → −0.174).

**Where the remaining time goes.** A standalone Kalman model timed separately:
numba compile 4.8s and sampling 2.8s at T = 710; 3.2s and 0.5s at T = 105. The
fit is now compile-bound. Numba-compiled functions are shape-generic, so a
compiled model reused across `fit_end` values (data passed as `pm.Data`, not
baked in as constants) would make every fit of a node after its first cost
about the sampling time. That is §2.6, a follow-up, not part of S25.

The C backend is 3× *slower* than today, not faster. The scan's per-step
overhead dominates a scalar recursion. The numba backend is not an
optimization on top of S25; S25 does not work without it.

### 2.3 What has to be built

1. **The filter.** Scalar local level: predict `P += σ_trend²`, innovation
   `v = r[t] − a`, `F = P + σ_obs²`, gain `K = P/F`, update `a += K·v`,
   `P *= 1 − K`, `logp += −½(log 2πF + v²/F)`, with `a₀ = 0, P₀ = 0` so the
   first prediction is `N(0, σ_trend²)`, matching `level[0]`. It enters as
   `pm.Potential` on `r = y − μ`. The observation node disappears from the
   graph, so step 3 matters.
2. **The level posterior.** `rca.py:1913` reads
   `fit.trace.posterior["trend"]` (the trend delta for attribution), and
   `model.py:1641` adds it into `mu` for the posterior predictive. Both must
   keep working unchanged. So, after sampling, draw `level | y, θ` once per
   posterior draw with forward-filtering backward-sampling (Frühwirth-Schnatter,
   1994; Carter & Kohn, 1994), vectorized across draws in numpy, and write it
   into `posterior["trend"]` with today's shape. These are exact draws from the
   same joint posterior as today's `trend`, not an approximation to it.
3. **Posterior predictive (S3).** `y_rep` today comes from the observed node.
   With a `Potential` there is none, so replicates are drawn as
   `μ + trend + N(0, σ_obs²)` from the joint draws. The four S3 statistics and
   their thresholds are unchanged.
4. **Diagnostics.** Compute R̂ and ESS over the sampled parameters. The
   recovered `trend` is a deterministic function of them plus exact
   conditional noise, so its "ESS" measures nothing new. This changes what
   `min_ess_bulk` covers, and the white paper's §2.2 diagnostics paragraph
   must say so.
5. **Backend.** `pm.sample(..., compile_kwargs={"mode": "NUMBA"})`. numba is
   already a PyMC dependency, not a new one. Check it is present in the
   base-only install (`pip install metric-breakdown`), which CI's no-extras
   job will prove either way.
6. **Scope.** NUTS only. The ADVI opt-in keeps today's parameterization,
   because PSIS k̂ is measured against it. Every current node qualifies: the
   likelihood is Gaussian throughout (`model.py`, §2.1 of the white paper).
   **S20** (count likelihoods) would end that, and a non-Gaussian node would
   keep the explicit latent. The dispatch is by likelihood, not by node.

### 2.4 How it is accepted

- **Same posterior:** on the calibration suite's worlds and on the demo's
  story-B nodes, every parameter's posterior mean and 94% HDI endpoints under
  the old and new paths agree within a few Monte-Carlo standard errors, and
  the recovered `trend` agrees likewise at the first, middle and last period.
  This is the test that says S25 is a computation change and not a model
  change.
- **The calibration suite passes unchanged**, and the demo tests pass. Any
  `TOUR` re-pin is recorded with old → new values. A re-pin outside MC error
  is a bug, not a re-pin.
- **Faster where it matters:** story B cold, measured the same way as §1,
  reported in the PR.
- **`/read-the-numbers`** on the demo before merge: the UI's trend band and
  PPC panel are the surfaces that read the recovered level.

### 2.5 What it unlocks

- **S21** (fit through undefined periods): a Kalman filter skips the update
  on a missing observation and carries the prediction, which is exactly
  "masking the likelihood". The item becomes a few lines.
- **S8** (local linear trend): a two-state filter, same machinery.
- **Wider trees within the lock.** A daily node stops being the thing that
  makes a tree impractical under exact MCMC, which is the case §2.2 of the
  white paper keeps ADVI around for.

### 2.6 Follow-up, not S25: reuse the compiled model

The proposal: build each node's model once with its data as `pm.Data`, cache
the compiled logp/dlogp on `TreeState` beside `traces`, and swap data for each
`fit_end`, saving "the 3–5s compile on every fit after a node's first".

**Evaluated 2026-10-02, after S25 shipped: not worth building.** PyTensor
already does most of it. Its numba backend keeps an on-disk cache
(`numba__cache`, on by default, under `base_compiledir`) keyed on the graph
and not on the data, so the expensive part of the compile is shared across
`fit_end`s, across fits in one process, and across processes and restarts.
What §2.6 would add on top is the part the disk cache cannot skip, which is
building and rewriting the logp/dlogp graph in the parent process. That
part costs about 0.6–0.7s.

Measured on an M2 Max with
[`benchmarks/s25_compile_reuse.py`](benchmarks/s25_compile_reuse.py). A
"cold" cache is a fresh `base_compiledir`, which is what a new container
sees:

| | cold disk cache | warm disk cache |
|---|---|---|
| `sessions`, first fit in a process | 18.4s | 8.2s |
| `customer_churn_rate`, first fit | 10.5s | 2.5s |
| `trial_conversion_rate`, first fit | 6.9s | 3.2s |
| `trials_started`, first fit | 9.1s | 7.2s |
| 3.10's warm pass on White Cube (6 fits) | **60.1s** | **35.5s** |

- **Numba compiles lazily, inside the first sampling call.** On a cold cache
  the timed `logp_dlogp_function` is only 1.3–2.3s; the other ~8s of a cold
  `sessions` fit is jitting during `pm.sample`, with one chain as well as
  four, so it is compile and not process spawn. Each new graph structure
  (parent count, seasonality, interventions) pays this once per cache dir.
- **The disk cache does not care about the data.** A brand-new process
  fitting `sessions` at a `fit_end` it had never seen ran in 8.8s, the same
  as an in-process refit.
- **The prototype saves ~0.7s a fit on a warm cache.** On a synthetic
  `sessions`-shaped node, rebuilding per fit took 6.3–6.5s and reusing one
  `pm.Data` model and `pm.NUTS` step took 5.5–5.7s. That is ~10% on a
  long daily node and ~30% on a ~100-period weekly node (0.6s of 1.7–2.2s).
  Across the six warm-pass fits it is ~4s, against the 25s the warm disk
  cache already saved.

Why the remaining ~0.7s is not worth building for:

- **It changes `fit_metric` throughout.** `y`, `X`, `t`, `X_iv` and the
  seasonal terms all change length with `fit_end`, so every one becomes
  `pm.Data`. The cache key is the graph structure, and the cache is new
  per-tree state under Rule 2.
- **A reused step can break reproducibility.** A `pm.NUTS` step carries
  adaptation state between `pm.sample` calls. A fit could then depend on
  what the cache held before it, which breaks 3.10's invariant that a warmed
  fit is the fit `run_rca` would have made. Making it safe means resetting
  that state and proving the reset with a seeded equality test.
- **Multi-chain sampling unpickles the step into each worker anyway**, and
  that cost stays whatever the parent caches.

**What to do instead.**

1. **Persist `base_compiledir` wherever breakdown runs in a container.**
   This is the measured 41% on a cold warm pass, and about 8s per node
   structure on the first RCA after a deploy. Neither `Dockerfile` nor
   `demo/hetzner/compose.yaml` keeps `~/.pytensor`, so every deploy and
   restart compiles every structure again. A named volume, or
   `PYTENSOR_FLAGS=base_compiledir=` pointed at one, is a one-line change.
   Do not bake the cache into the image: numba's cache is specific to the
   CPU it was compiled on, and the build machine is not the host.
2. **What is left on a warm cache is sampling.** That is ~7s of `sessions`
   and ~6s of `trials_started` at 709 daily periods. If fits need to get
   faster again, measure there first, starting with the tune/draw budget or
   a different NUTS implementation. Compile reuse comes after that.

Revisit §2.6 only if a profile on a warm cache shows the parent-side compile
dominating, for example a tree of many short weekly nodes refitted at many
`fit_end`s, as 3.10 steps 2–3 might produce.

---

## 3. 1.13 — show the work

The UI already refuses to lie while it waits: `PROGRESS_PHRASES` in `app.js`
rotates phrases that are literally true of the current stage, and the node
being fitted pulses on the canvas. What it does not show is *how much*: how
far through the sampling it is, and how much evidence went into the answer.
Both are real numbers the engine already has.

### 3.1 Live draw progress

`pm.sample` accepts a `callback(trace, draw)` invoked in the main process on
every draw. Report `draws_done` / `draws_total`
(`chains × (tune + draws)`) through the existing `progress` channel. The
status line becomes something like

> exploring the typical set · sessions (1/4) · draw 3,412 / 6,000 · 0:14

and the fitting stage gets a real fraction, not a spinner. The callback must
stay cheap: throttle to about 10 updates per second, because it runs on the
process that also receives every draw. `progress.py`'s rules hold unchanged:
the callback is passed in explicitly, and a broken consumer cannot fail a fit.

### 3.2 "What went into this answer"

A `work` block on the RCA payload, rendered as a short panel beside the
methods footnote and in the export (S13's neighbour):

- per fitted node: chains × draws, **effective samples** (min bulk ESS),
  R̂ max, divergences, and the fit's `fit_quality`
- bootstrap replicates per interval (`N_BOOT` = 500) and the number of
  intervals they fed
- Shapley coalitions enumerated on each formula node (2ⁿ)
- the reference-sensitivity re-attributions (S23)
- wall time, and which fits were cache hits

**The honesty constraint, which is the design.** The obvious version,
"6,000 simulations per node!", is exactly what S12 warns about: prominence
reads as rigor whatever the diagnostics say. So:

1. **Effective samples lead, raw draws follow.** "2,072 effective samples
   from 4 chains that agree (R̂ 1.00)" is both more impressive and true.
   "6,000 draws" on a `suspect` fit is a boast the fit did not earn.
2. **A `suspect` or `severe` fit is shown in the same panel, in the same
   type.** The panel never renders a count without the verdict beside it.
3. **The wording lives in `disclosures.js`**, per the frontend rule, so the
   panel and the export cannot drift into two phrasings.

This goes in Horizon 1 beside 1.4 (UI trust finish) because its audience is
the same: a reader deciding whether to believe the number.

---

## 4. 3.10 — warm analyses, and windows worth warming

### 4.1 The fact that makes this cheap

**A node's RCA fit depends only on `analysis_start`.** `_node_fit_end`
(`rca.py`) returns `analysis_start` for every node, and the trace cache is
keyed `(metric, fit_end)`. The one exception is a node with a
`learn_from: window` intervention, whose fit is extended through the event
(S24). The reference window and the analysis end enter attribution, not the
fit. So:

- changing the reference window after an RCA is already instant, and
- warming an analysis means predicting **one date**, not a window pair.

### 4.2 Step 1: always warm the latest period

On tree load, and after each data refresh, fit every probabilistic node with
`analysis_start` = the start of the latest complete period at that node's
grain. "What happened last week?" is then always a cache hit, and it needs no
detection at all.

- **Server-side, opt-in:** `BREAKDOWN_WARM=latest`, or `breakdown serve --warm
  latest`. The demo's external `prewarm.py` stays for its tour stories.
- **Never blocks a person.** A background warm that holds the tree lock
  through four fits would park the analyst's own RCA behind it. It takes the
  lock per node, not per run, so a user request waits at most one fit, and it
  yields when a request is queued.
- **Bounded:** warm fits go into the same process-wide trace cap (Rule 2),
  and a warm never evicts a fit a person asked for more recently.
- This is the first mechanical piece of 3.1 (scheduled evaluation), which is
  ungated. It is the same loop that will later produce the digest.

### 4.3 Step 2: suggest an analysis window

The scenario: MRR at White Cube moves. The Metric tab's line chart shows a
marker where the series changed, labelled with the change's posterior
probability. The analyst looks, agrees that is the break, clicks *Analyze from
here*, and the RCA runs on the right window the first time.

**Detector.** Bayesian online changepoint detection (Adams & MacKay, 2007): a
posterior over "periods since the last change" per period, with a
conjugate Normal model per segment and a constant hazard. It is closed form
and O(T·R) in numpy, milliseconds on any series here. No MCMC, and so no
wait. The output is a posterior probability of a change at each period,
which is a probabilistic statement the engine's stance allows. Nothing in it
is a p-value or a test at a significance level.

- **Which series:** by default the apex and any node the tree marks as a
  headline. The analyst can ask for any node from its Metric tab.
- **What is suggested:** the top few periods by posterior change
  probability above a threshold, each as a candidate `analysis_start`
  snapped to the node's grain, with a default `analysis_end` of one matched
  block and the reference defaulted by 1.10's existing rule.
- **Where:** `GET /suggest_windows/{name}` (API), markers on the Metric-tab
  chart (UI), and a field on `explain_metric` (MCP), so an agent can propose
  the same window a person would see.

### 4.4 Squaring it with "automatic changepoint detection is deliberately out"

`docs/model.md` and [`step_change_design.md`](step_change_design.md) §5 rule
changepoint detection out, and that ruling stands. What it rules out is the
**model** placing steps: a sparse or heavy-tailed prior on the level
increments, which lets the fit reproduce anything and turns every step it
places into a finding the author never asserted. The line there is
"the author dates the step; the engine sizes it."

A window suggestion is on the other side of that line:

| | changepoint in the model (out) | window suggestion (this) |
|---|---|---|
| where it lives | inside `fit_metric`, as a model term | outside every fit, on the raw series |
| what it changes | the posterior, the PPC, the attribution | nothing but which window is proposed |
| who dates the change | the engine | the analyst, who accepts or ignores the marker |
| appears in the gap decomposition | yes, as a term | never |

`docs/model.md`'s sentence therefore gets a clause when this ships, not a
reversal: detection stays out of the model; a detector may *propose* a window,
and the analyst chooses it.

**The real statistical cost is selection (S15).** A window chosen because the
series moved most there makes the gap look larger than a randomly chosen
window would. RCA explains a gap; it does not test whether one exists, so the
attribution itself is not biased by where the window came from. But a reader
comparing "how big was this" across analyses is exposed. So the payload
records `window_source: "suggested"` with the change probability, the UI says
so beside the window, and S15's disclosure names it.

### 4.5 Step 3: warm the suggestions, then run them

Once suggestions exist, warming their `analysis_start` dates in the background
(§4.2's machinery, same lock discipline and cap) means the analyst who clicks
a marker lands on a cache hit. The step after that is the user's full
scenario: a change above the threshold triggers the RCA itself through the
existing API, and the result waits in the cache, and eventually in 3.1's
digest, before anyone opens the page. That is 3.1 and 3.5 (hosted mode)
territory and is sequenced there. This design only fixes the contract they
build on: suggestions are an engine function, warming is a queue of dates,
and the RCA an automation triggers is the same `run_rca` a person gets.

---

## 5. Sequencing

1. **S25** first. It is the only item that makes a fit cheaper; everything
   else hides or moves the cost. It also shrinks what §4 has to warm, so
   warming is cheaper too.
2. **3.10 step 1** (warm the latest period). Small, and it serves the Monday
   question with no new statistics.
3. **1.13** (live progress and the work panel). Independent; it can run in
   parallel with either of the above.
4. **3.10 steps 2–3** (suggestions, then warming them), with the
   `docs/model.md` clause and the S15 disclosure in the same change.
5. ~~§2.6 (compiled-model reuse)~~ evaluated 2026-10-02 and not built:
   PyTensor's disk cache already covers most of it. Persisting that cache in
   containers is the cheaper win (§2.6).

## 6. Open questions

- **Do the three lower-traffic learned nodes need the Kalman path at all?**
  On weekly nodes the gain is modest (6.8s → 6.4s on `trials_started`) and
  compile-bound. But the divergences disappear, and one code path is simpler
  than two. The recommendation is to apply it to every Gaussian node; the PR
  should report the per-node table so the choice is visible.
- **Hazard rate for BOCPD.** A per-grain default (a change every ~26 weeks,
  every ~12 months) is a prior, and it should be declared and overridable in
  YAML like any other prior, not a hidden constant.
- **Which nodes get suggestions by default.** The apex alone may be too
  narrow for a wide tree. There may be a case for a `headline: true` marker,
  but that is a schema addition, so it waits for a tree that needs it.

## References

- Adams, R. P. & MacKay, D. J. C. (2007). Bayesian online changepoint detection. arXiv:0710.3742.
- Carter, C. K. & Kohn, R. (1994). On Gibbs sampling for state space models. *Biometrika* 81(3).
- Durbin, J. & Koopman, S. J. (2012). *Time Series Analysis by State Space Methods*, 2nd ed. Oxford.
- Frühwirth-Schnatter, S. (1994). Data augmentation and dynamic linear models. *J. Time Series Analysis* 15(2).
- Harvey, A. C. (1989). *Forecasting, Structural Time Series Models and the Kalman Filter*. Cambridge.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

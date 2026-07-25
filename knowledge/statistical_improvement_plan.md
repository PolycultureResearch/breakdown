# Statistical Review & Improvement Plan

*A Bayesian statistician's review of breakdown — 2026-07-06*

**Scope.** This reviews the statistical model (`breakdown/engine/model.py`), the RCA
attribution logic (`breakdown/engine/rca.py`), the surrounding data plumbing, and the
documentation's claims about what the outputs mean. The guiding constraint, per the
project's goals: keep breakdown **simple but rigorous** at one job — rapidly localizing
and identifying the drivers of an observed change in a key metric. Recommendations
below favor fixes that *remove* statistical error without adding modeling machinery.

**How to use this document.** Part 1 is the critique (why each change matters). Part 2
records alternatives considered and rejected. Part 3 is the plan, written as
self-contained implementation tickets (T1–T12) with file paths, signatures, code
sketches, and acceptance criteria — specific enough to hand to a code assistant one
ticket at a time, in order. Design decisions are made in the tickets; do not re-open
them during implementation.

**Overall assessment.** The architecture is sound and unusually honest: the
deterministic/probabilistic node split is the right decomposition, exact Shapley on
formula nodes is correct and well-tested, priors-in-business-units with internal
rescaling is a genuinely good UX decision, `unexplained` is surfaced rather than hidden,
and `docs/model.md` states caveats most tools bury. The problems are concentrated in a
few places: (1) the model is fit on data that *includes* the anomaly being explained,
(2) the trend specification makes the causal coefficients weakly identified and the
sampler geometry pathological, (3) the RCA gap decomposition ignores components the
model has already estimated, and (4) uncertainty is reported where it's cheap (betas)
and omitted where it's equally real (window means, Shapley contributions, ADVI error).
None of these require a redesign. Most are targeted changes to `fit_metric` and
`run_rca`.

---

## Part 1 — Issues and pitfalls

Ordered roughly by severity: how much each one can distort the answer to "what drove
this change?"

### 1.1 The model is fit on the anomaly it's asked to explain (major)

`fit_metric` fits on the **entire** loaded data window (`model.py:212-276`), and RCA's
on-demand fits do the same (`rca.py:124-128`). The analysis window — the anomalous
period under investigation — is inside the training data. Two consequences:

- **The trend absorbs the anomaly.** The Gaussian random walk will happily explain a
  level shift in the analysis window as "trend", stealing signal from the parent
  regressors and shrinking their apparent contributions. `unexplained` then looks large
  and the true driver looks weak.
- **The betas are contaminated.** If the anomaly is precisely a period where the usual
  relationship broke (a regime change — often the actual root cause), including it in
  the fit drags `beta` toward a compromise value that describes neither regime. The
  attribution `beta_raw × Δparent` is then wrong in both windows.

This is also a departure from the methodology breakdown cites: Brodersen et al. (2015)
fit the structural model on the **pre-period only** and treat the post-period as a
counterfactual forecast. That's not an incidental detail of CausalImpact — it's what
makes the inference causal-flavored rather than curve-fitting.

**Fix:** T2 — fit each RCA node on data strictly before `analysis_start`, so the betas
encode the normal-regime relationship and `beta_raw × Δparent` answers the right
question. A fuller counterfactual mode is T11.

### 1.2 Trend flexibility makes β weakly identified, and the parameterization fights the sampler (major)

In z-scored space the trend is `GaussianRandomWalk(sigma_trend)` with
`sigma_trend ~ HalfNormal(1)` (`model.py:257-258`). A random walk whose *per-step* sd
can comfortably sit near 1 — on data whose *total* sd is 1 — can interpolate the series
almost pointwise. The likelihood then barely distinguishes "the parent did it" from
"the trend did it", and the β posterior is driven by the prior far more than users
will realize. `docs/model.md` §3 names this failure mode, but the default prior makes
it the *expected* behavior, not an edge case.

Three compounding technical problems:

- **Funnel geometry.** The centered parameterization (`trend` sampled directly, scaled
  by `sigma_trend`) produces the classic Neal's-funnel pathology. NUTS at
  `target_accept=0.9` will emit divergences that nothing currently checks (see 1.6),
  and mean-field ADVI is *known* to fail on funnels — precisely the method RCA uses by
  default.
- **Additive confounding with the intercept.** `alpha ~ Normal(0, 10)` (`model.py:262`)
  and the random walk's level are only jointly identified through the (vague) init
  distribution of the walk. On z-scored data (mean exactly 0), `sigma=10` is ~10 total
  sds of slack. This creates a flat ridge in the posterior — harmless for point
  attribution, costly for sampling and for ADVI's Gaussian approximation.
- **Slow-moving parents are absorbed.** Any parent whose movement over the window looks
  like a smooth drift is nearly collinear with the random walk. Its CI widens toward
  the prior and RCA under-attributes to it.

**Fix:** T3 — non-centered trend, tight default step prior, tightened intercept, and a
YAML knob wired to the currently dead `trend:` field. This single change will do more
for the calibration of every credible interval breakdown reports than anything else on
this list.

### 1.3 RCA discards components the model already estimated (major, cheap win)

For a probabilistic node, `run_rca` decomposes the gap as
`gap = Σᵢ beta_raw[i]·Δxᵢ + unexplained` (`rca.py:159-190`). But the fitted model is
`y = α + trend + seasonal + Σ βx + ε`, and the trace contains posterior samples of
`trend[t]` and the seasonal coefficients at every time point. The window-over-window
change in the fitted trend and seasonal components is directly computable from the
trace — yet it's all lumped into `unexplained`.

This matters for triage: "the gap is 60% seasonal composition and 30% sessions" is a
very different Monday-morning story from "sessions explain 30%, origin of the rest
unknown", and the model already knows the difference.

It also fixes a concrete pitfall: **window composition bias**. Nothing checks that the
reference and analysis windows have comparable weekday mixes. With weekly seasonality,
a 10-day reference window (1.43 weekends) vs. a 14-day analysis window (2 weekends)
produces a spurious "gap" that currently lands in `unexplained` or, worse, gets
partially attributed to a parent that also has weekly structure. Reporting an explicit
`seasonal` term in the decomposition makes this visible; a docs note telling users to
prefer whole-week windows makes it avoidable.

**Fix:** T5. Note one subtlety created by T2: once the fit excludes the analysis
window, the trend has no fitted value *inside* that window. The random-walk forecast
of a local level is flat at its last fitted state, so the analysis-window trend is the
last fitted trend state, per posterior sample; the Fourier seasonal component is
parametric in `t` and evaluates anywhere. T5 specifies both.

### 1.4 Shapley on window means conflates within-window covariance with "noise" (moderate)

`shapley_attribution` computes baseline/actual as **formula(window means of parents)**
(`rca.py:80-92`), while the node's `gap` in RCA uses the window mean of the target's
own column. For any nonlinear formula the two differ by a Jensen gap. For
`revenue = order_count × aov`:

```
mean(orders·aov) − mean(orders)·mean(aov) = cov_within_window(orders, aov)
```

So even with *zero* measurement noise, `unexplained` on a formula node is the change in
within-window covariance of the parents. `docs/model.md` calls this term "data noise"
— that's incomplete and will mislead: a real behavioral change (e.g., "big orders
disappeared", which shows up exactly as an orders–AOV covariance shift) is currently
reported as noise. Ratio formulas (`conversion_rate = orders / sessions`) make it
worse: the mean of a daily ratio can differ substantially from the ratio of means, and
the discrepancy scales with the variance of the denominator.

**Fix:** T6 — per-day Shapley over the analysis window. Contributions then sum to
`mean_analysis(formula(x_t)) − formula(reference means)`, so the analysis-window
covariance shift (the behaviorally interesting part) is attributed to parents instead
of dumped in `unexplained`.

### 1.5 Point-mass treatment of window means; formula nodes get no uncertainty at all (moderate)

Every window mean — the target's, each parent's, the lag-shifted parents' — is treated
as an exactly known constant (`rca.py:33-43`, `135-137`, `164-174`). The only
uncertainty in the entire RCA output comes from the β posterior. Consequences:

- **`ci_95` on posterior contributions is too narrow**, because `Δparent` is itself a
  noisy estimate (sd ≈ series sd / √window_days; brutal for short windows, which is
  exactly the "what happened this weekend?" use case — a 2-day analysis window).
- **`prob_same_direction` inherits false confidence.** With `Δparent` fixed, sign
  certainty is often just sign certainty of the window-mean difference. It can read
  0.99 when a 3-day window mean is well within noise of zero change.
- **Formula-node contributions have no CI by fiat** (`ci_95: null`). The *relationship*
  is deterministic, but the *inputs* (window means) are not. "Exact Shapley" is exact
  attribution of an uncertain quantity — the current output presents it as certainty
  about the world.

**Fix:** T7 — block-bootstrap the window means, compose the resampled Δ's with the β
posterior for probabilistic nodes, and push resampled parent means through
`compute_shapley` for formula nodes. One mechanism repairs all three symptoms.

### 1.6 No convergence gating anywhere (moderate)

`POST /analyze` returns `"status": "success"` unconditionally (`api/main.py:189-193`);
`run_rca` consumes ADVI fits with no check that the optimization converged; nothing
inspects `r_hat`, ESS, or divergence counts even though NUTS produces them and
`summarize_trace` computes them. Combined with 1.2 (a geometry that *will* produce
divergences and ADVI failures), users can receive confidently formatted credible
intervals from a fit that did not converge. **Fix:** T8. Do **not** block responses —
triage speed matters — but never present an unconverged CI as clean.

### 1.7 ADVI as the silent RCA default (moderate — interacts with 1.2 and 1.6)

RCA's on-demand fits use mean-field ADVI (`rca.py:128`), which systematically
underestimates posterior variance and handles the current centered random walk
especially poorly. The docs say "triage with ADVI, confirm with NUTS", which is the
right workflow — but RCA responses don't record *which* method produced each node's
numbers, so a user reading the JSON (or the UI sidebar) can't tell tentative CIs from
confirmed ones. **Fix:** T3 makes the posterior far more ADVI-friendly (closer to
Gaussian); T8 tags every RCA node with the inference method that produced it; T12
(later) considers full-rank ADVI for the on-demand path.

### 1.8 Data-dependent priors and full-sample normalization (minor but should be documented)

Two related subtleties in `_prepare_series` / `scale_prior_params`:

- **Priors are rescaled using sample statistics** (`scale = x_std / y_std`,
  `model.py:27-48, 192-209`). The prior therefore depends on the observed data —
  including the anomaly window (until T2 lands). If the analysis window doubles
  `y_std`, the effective prior on β in raw units changes between "before the incident"
  and "during the incident". This is a pragmatic and defensible choice (it's what makes
  business-unit priors possible), but it is empirical-Bayes-adjacent and should be
  stated in `docs/model.md`. T2 (fit and normalize on pre-anomaly data only) removes
  the worst version of it.
- **z-scoring uses the full sample** including the anomalous period, so the anomaly
  inflates the very scale used to normalize it. Same remedy.
- Small robustness note: `_normalize` raises on exactly zero variance but a
  *near*-constant series (e.g., a rounded rate) produces exploding z-scores; consider a
  relative-variance floor with a clear error.

### 1.9 `fit_metric`'s contract loses information the rest of the system needs (structural)

`fit_metric` returns only the InferenceData. The normalization constants
(`y_mean`, `y_std`, per-parent stds), the effective time index after lag-trimming, and
the fit configuration (method, cutoff, draws) are all discarded — which is *why* RCA
can't decompose trend/seasonal contributions (1.3), why traces can't be cache-keyed by
fit window (needed for 1.1), and why `/metrics/{name}` can't show the fitted
decomposition in business units. **Fix:** T1 — a `FitResult` dataclass. Pure refactor,
no new statistics, unblocks T2/T5/T8.

### 1.10 Assorted smaller issues

- **The example tree contradicts the docs.** `examples/jaffle_shop_tree.yml:35-38`
  declares `period: 365` seasonality on a default data window of ~100 days
  (`api/main.py:22-23`). `docs/model.md` §4 explicitly warns this is unidentifiable
  (≥ 2 full periods needed). The flagship example ships the documented pitfall — 8
  Fourier coefficients soaking up degrees of freedom. Fix in T9: remove the annual
  entry and **validate at fit time**.
- **`trend:` YAML field is parsed and silently ignored.** `parser.py:49` accepts it;
  `fit_metric` never reads it. T3 wires it to `sigma_trend` configurability — silently
  accepted no-op config erodes trust in the rest of the YAML.
- **Irregular date grids are treated as regular.** `t = np.arange(len(y))`
  (`model.py:254`) and `GaussianRandomWalk` both assume evenly spaced observations, but
  `_fetch_all_metrics` inner-joins on date (`api/main.py:52`) and can silently drop
  days present in one metric but not another (e.g., a metric not recorded on weekends
  deletes weekends for the whole tree). A period-7 Fourier basis on a grid with holes
  is misaligned with actual weekdays. Fix in T9.
- **Multi-hop ranking double-counts shared paths.** `_rank_causes` adds a parent's
  score across all its children's paths (`rca.py:224-233`), so an ancestor reached by
  two paths through a diamond accumulates score twice, and |share| clamping means
  offsetting +145%/−45% contributions both propagate at full weight. It's documented as
  a heuristic, and as triage it's acceptable — but a *principled* replacement exists at
  similar complexity once T7's sampled-Δ machinery lands: see T10.
- **`prob_same_direction` degenerates when Δparent ≈ 0.** All contribution samples are
  ≈ 0 and the max of the two tail masses is an arbitrary coin flip near 0.5 presented
  with three digits. T7's bootstrap makes near-zero Δ produce a wide sign-split
  automatically.
- **Windows are unvalidated.** Nothing checks reference/analysis order, overlap, or
  (for lagged parents) that the back-shifted window still lies inside the data
  (`rca.py:167-170` will raise a confusing "No data in window" from a date the user
  never typed). Fix in T9.
- **Formula-node residual fitting can amplify noise.** For a near-exact identity the
  residual sd is tiny; z-scoring it (`model.py:145-149`) inflates pure noise to unit
  variance and the BSTS then "finds" trend and seasonality in it. Harmless for RCA
  (which never uses these fits) but confusing in `/analyze` output. Consider reporting
  the residual's share of the target's variance so users can see "this identity
  explains 99.7% of movement; the fitted decomposition below concerns the 0.3%".
- **Two chains, and `chains=2` only for NUTS** (`model.py:274`). Fine for speed, but
  four short chains give a much better-behaved `r_hat`; T3 makes chain count a
  parameter.

### 1.11 What the docs get right (keep it that way)

Worth saying explicitly because it should be protected during any refactor:
the DAG-as-hypothesis framing, the refusal to clamp `share_of_gap`, `unexplained` as a
first-class output, business-unit priors, and the ADVI/NUTS triage/confirm distinction
are all correct decisions. The fixes above make the numbers live up to the
documentation, not change the philosophy.

---

## Part 2 — Alternatives considered and rejected (to keep it simple)

- **Full joint model of the tree** (one PyMC model over all nodes, coherent uncertainty
  propagation): statistically superior, operationally heavy — slow to fit, fragile to
  one bad node, hard to cache. The per-node fit + explicit-decomposition approach is
  the right trade-off for triage speed. Revisit only if multi-hop uncertainty becomes
  a core user demand.
- **Structure learning / automatic edge discovery:** explicitly out of scope, and
  rightly so — the a-priori DAG is the product's premise.
- **Nonlinear/interaction terms in the regression:** the linear-in-parents assumption
  is documented; nonlinearity lands in `unexplained`, which after T5 becomes a
  reliable "look here" signal. Adding splines/GPs would blow the simplicity budget for
  marginal triage value.
- **Changepoint detection for automatic window selection:** attractive, but window
  choice is where analyst judgment belongs. A within-window sparkline warning (docs §5)
  is enough for now.
- **Spike-and-slab priors on β (the full Brodersen et al. treatment):** valuable for
  many-parent nodes, but metric-tree nodes rarely exceed a handful of parents, and it
  complicates the ADVI path. Not now.

---

## Part 3 — Implementation plan (tickets T1–T12)

**Conventions for every ticket.**

- Implement tickets **in order**; each assumes the previous ones have landed.
- Verify with `uv run pytest tests/ -v`. PyMC tests are slow — keep new tests at
  `draws=100–300` like the existing ones, and mark anything heavier
  `@pytest.mark.slow`.
- All existing tests must pass unmodified unless a ticket's **Test changes** section
  explicitly says to update them.
- Update `README.md` and `docs/model.md` wherever a ticket changes behavior or output
  schema; each ticket lists its doc touchpoints.
- Keep the API response shapes backward-compatible except where a ticket specifies a
  schema change (new keys are fine; renaming/removing existing keys is not, unless
  stated).

### T1 — `FitResult`: make `fit_metric` return what the rest of the system needs

**Priority: P0 (do first — unblocks T2, T5, T8).** Pure refactor, no statistical change.

**Files:** `breakdown/engine/model.py`, `breakdown/engine/rca.py`,
`breakdown/api/main.py`, `tests/test_engine.py`, `tests/test_rca.py`,
`tests/test_api.py`.

**Changes.**

1. In `model.py`, add:

   ```python
   from dataclasses import dataclass, field

   @dataclass
   class FitResult:
       trace: Any                      # arviz.InferenceData
       target: str
       parents: List[str]              # regressor parents ([] for roots/formula nodes)
       y_mean: float                   # of the fitted y series (residual for formula nodes)
       y_std: float
       x_stds: Optional[np.ndarray]    # per-parent stds of the (lag-shifted) regressors, None if no X
       dates: pd.DatetimeIndex         # dates actually used in the fit (after lag trim; after fit_end cut in T2)
       inference_method: str           # "nuts" | "advi"
       fit_end: Optional[str] = None   # populated by T2; None = fit on full window
       diagnostics: Dict[str, Any] = field(default_factory=dict)  # populated by T8
   ```

2. Change `_prepare_series` to also return the pieces it currently discards. New
   return type: `Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray], float,
   float, Optional[np.ndarray], pd.DatetimeIndex]` =
   `(y, X, scale, y_mean, y_std, x_stds, dates)`. `dates` is
   `pd.DatetimeIndex(pd.to_datetime(data["date"]).iloc[max_lag:])` (or the full date
   column when `max_lag == 0`; for formula nodes it's the full column). `_normalize`
   already computes mean/std — just stop throwing them away.

3. `fit_metric` returns `FitResult` instead of the bare trace, filling every field
   (`fit_end=None`, `diagnostics={}` for now).

4. Update all callers:
   - `rca.py`: `traces[node]` now holds a `FitResult`; read
     `traces[node].trace.posterior["beta_raw"]`.
   - `api/main.py`: `/analyze` stores the `FitResult`; `/metrics/{name}` calls
     `summarize_trace(traces[name].trace)`; `/meta` is unchanged (keys are still
     metric names until T2).
   - Tests that call `fit_metric` directly: access `.trace.posterior` instead of
     `.posterior`. This is the one blanket permitted test edit.

**Acceptance criteria.**

- Full test suite passes after mechanical updates.
- New test: `fit_metric` on `order_count` (SIMPLE_YAML, 50 days) returns a `FitResult`
  with `y_std > 0`, `parents == ["daily_sessions"]`, `len(dates) == 50`, and
  `x_stds.shape == (1,)`.
- New test: with `lags: {daily_sessions: 5}` and 50 days, `len(result.dates) == 45`
  and `result.dates[0] == data["date"].iloc[5]`.

### T2 — Fit on pre-anomaly data

**Priority: P0.** Fixes 1.1 and the worst of 1.8.

**Files:** `breakdown/engine/model.py`, `breakdown/engine/rca.py`,
`breakdown/api/main.py`, `docs/model.md`, `tests/test_rca.py`, `tests/test_api.py`.

**Design decisions (fixed — do not revisit):**

- `fit_end` is an **exclusive** upper bound: the fit uses rows with
  `date < fit_end`. RCA passes `fit_end = analysis_start`, so the anomalous window is
  fully excluded and the reference window (which must end before `analysis_start`,
  enforced in T9) is fully included.
- Normalization (z-scoring) and prior scaling use **only** the filtered rows, so the
  anomaly no longer influences the scale or the effective prior.
- The trace cache is re-keyed from `name` to the tuple `(name, fit_end)` with
  `fit_end: Optional[str]` (`None` = full-window fit from `/analyze`). RCA **never**
  reuses a fit with a different `fit_end` — a full-window NUTS fit is contaminated for
  RCA purposes (that is the whole point of this ticket) and must not shadow it.

**Changes.**

1. `fit_metric(dag, data, target, draws=1000, tune=1000, inference_method="nuts",
   fit_end: Optional[str] = None)`. At the top:

   ```python
   if fit_end is not None:
       dates = pd.to_datetime(data["date"])
       data = data.loc[dates < pd.to_datetime(fit_end)].reset_index(drop=True)
       if len(data) < 10:
           raise ValueError(
               f"Only {len(data)} rows before fit_end={fit_end} for '{target}' (need >= 10)."
           )
   ```

   Store `fit_end` on the returned `FitResult`.

2. `run_rca`: the cache parameter becomes
   `traces: Dict[Tuple[str, Optional[str]], FitResult]`. The on-demand fit loop keys on
   `(node, analysis_start)` and calls
   `fit_metric(..., inference_method="advi", fit_end=analysis_start)`. The posterior
   read becomes `traces[(node, analysis_start)].trace.posterior["beta_raw"]`.

3. `api/main.py`:
   - `app.state.traces` uses the tuple keys. `/analyze` stores under `(name, fit_end)`
     and gains an optional query param `fit_end: Optional[str] = Query(default=None)`
     (validated as ISO date), so the "confirm with NUTS" workflow can reproduce
     exactly what RCA fitted.
   - `/meta` `fitted`: `sorted({name for (name, _) in traces})`.
   - `/metrics/{name}` summary: prefer key `(name, None)`; else the entry with the
     latest `fit_end`; else `None`.

4. `docs/model.md`: new short section "What data the fit sees" — RCA fits on data
   strictly before the analysis window (with the rationale from 1.1); `/analyze`
   defaults to the full window unless `fit_end` is passed; normalization and prior
   scaling follow the fit window (the 1.8 caveat, now stated).

**Acceptance criteria.**

- New test (regression for 1.1): build a 120-day dataset where
  `y = 0.5 * x + Normal(0, 0.5)` for days 0–89 and `y` drops by an additional constant
  −20 for days 90–119 (a driver *not* in the tree). Fit `y` on `x` twice: full window
  vs `fit_end = day 90`. Assert the `fit_end` posterior mean of `beta_raw` is within
  [0.4, 0.6]; assert the full-window fit's is measurably pulled away (or simply assert
  the pre-period fit, which is the contract). Assert `len(result.dates) == 90`.
- New test: `run_rca` populates `traces[("order_count", AN[0])]` (not
  `traces["order_count"]`), and a second call reuses it
  (`is` identity, mirroring `test_rca_trace_reuse`).
- Updated tests: `test_rca_on_demand_fitting_minimal`, `test_rca_trace_reuse` — key
  shape changes only.

### T3 — Fix the trend: non-centered, tight, configurable

**Priority: P0.** Fixes 1.2; makes 1.7 far less severe.

**Files:** `breakdown/engine/model.py`, `breakdown/parser.py`, `docs/model.md`,
`README.md` (YAML reference table), `tests/test_parser.py`, `tests/test_engine.py`.

**Design decisions (fixed):**

- Default per-step trend sd prior: `sigma_trend ~ HalfNormal(0.05)` in z-scored space
  ("the level drifts slowly; parents and seasonality explain the movement").
- Non-centered parameterization via cumulative sum of unit normals.
- Intercept tightened to `alpha ~ Normal(0, 1)` (data is z-scored; mean is exactly 0).
- YAML `trend:` field becomes functional. Accepted forms:
  `trend: linear` (back-compat, default sigma) or
  `trend: {type: linear, sigma: 0.1}`. Any other `type` is a parse error.
- `chains` becomes a `fit_metric` parameter, default 4.

**Changes.**

1. `parser.py`:

   ```python
   class TrendConfig(BaseModel):
       type: str = "linear"
       sigma: float = 0.05

       @field_validator("type")
       @classmethod
       def validate_type(cls, v: str) -> str:
           if v != "linear":
               raise ValueError(f"Unsupported trend type: {v}. Must be 'linear'.")
           return v

       @field_validator("sigma")
       @classmethod
       def validate_sigma(cls, v: float) -> float:
           if v <= 0:
               raise ValueError("trend sigma must be > 0")
           return v
   ```

   In `MetricDefinition`, replace `trend: Optional[str] = None` with
   `trend: Optional[TrendConfig] = None` plus a `mode="before"` validator that maps the
   string `"linear"` to `{"type": "linear"}` (any other string: error).

2. `model.py`, inside the model block — replace lines 257–258 and 262 with:

   ```python
   import pytensor.tensor as pt  # top of file

   trend_sigma_prior = defn.trend.sigma if defn.trend else 0.05
   sigma_trend = pm.HalfNormal("sigma_trend", trend_sigma_prior)
   trend_z = pm.Normal("trend_z", 0.0, 1.0, shape=len(y))
   trend = pm.Deterministic("trend", pt.cumsum(sigma_trend * trend_z))
   ...
   alpha = pm.Normal("alpha", mu=0, sigma=1.0)
   ```

   The `pm.Deterministic("trend", ...)` keeps the variable name `"trend"` in the
   posterior so existing tests and T5 read it unchanged. Note `HalfNormal`'s parameter
   is the scale `sigma` — passing `trend_sigma_prior` sets the prior scale of the
   *step size*, which is the intended knob.

3. NUTS call: `pm.sample(draws=draws, tune=tune, target_accept=0.9, chains=chains)`
   with `chains: int = 4` as a new `fit_metric` parameter; expose
   `chains` on `/analyze` as `Query(default=4, ge=1, le=8)`.

4. Docs: update the prior table in `docs/model.md` (§ "What gets fitted"), rewrite
   limitation §3 (trend absorption) to say the default is now tight and point at the
   YAML knob; add `trend` row semantics to the README YAML reference.

**Acceptance criteria.**

- Existing tests pass, including `test_lagged_model_trims_rows` (the Deterministic
  preserves `trend`'s shape) and `test_beta_raw_recovers_business_units`.
- New parser tests: `trend: linear` parses with `sigma == 0.05`;
  `trend: {sigma: 0.2}` parses; `trend: quadratic` and `trend: {sigma: -1}` raise.
- New engine test: `trend: {sigma: 0.2}` in YAML shows up as a wider `sigma_trend`
  posterior than the default on the same data (assert
  `posterior sigma_trend mean(sigma=0.2 fit) > mean(default fit)` on
  `generate_mock_data(50)`, seed fixed, draws=200 — this is a weak but stable check).
- New regression test for identifiability (the point of the ticket): generate
  `x = 100 + cumsum(Normal(0, 3, 120))`, `y = 0.5 * x + Normal(0, 1, 120)` (no
  injected trend in y beyond x). Fit y on x with NUTS, draws=300. Assert the 95% HDI
  of `beta_raw` (via `az.summary`) has width < 0.4 and contains 0.5. Under the old
  HalfNormal(1) trend this interval is dramatically wider; pin the new behavior.

### T4 — (folded into T1–T3; number reserved to keep later references stable)

No action. T1–T3 together are the P0 block.

### T5 — Explicit trend & seasonal contributions in RCA

**Priority: P1.** Fixes 1.3.

**Files:** `breakdown/engine/rca.py`, `breakdown/engine/model.py` (one small helper),
`docs/model.md`, `tests/test_rca.py`.

**Design decisions (fixed):**

- Only **probabilistic** nodes (fitted, non-formula, non-root) get a `components`
  block. Formula nodes and roots get `components: null`.
- Time index convention: for a fitted node, integer time `t(d) = (d − fit.dates[0]).days`
  for any date `d` (valid because T9 enforces a contiguous daily grid; before T9 lands
  this is already the de-facto assumption).
- **Seasonal contribution** (parametric, defined for all `t`): per posterior sample,
  `seasonal(t) = Σ_s Σ_{k=1,2} a_{s,k} sin(2πkt/P_s) + b_{s,k} cos(2πkt/P_s)`, using
  the trace variables `sin_{name}_h{k}` / `cos_{name}_h{k}`. Contribution =
  `(mean_{d∈analysis} seasonal(t(d)) − mean_{d∈reference} seasonal(t(d))) * y_std`.
- **Trend contribution**: per posterior sample, the reference-window trend is
  `mean of trend[t(d)] over reference dates d` (all inside the fit period after T2/T9
  validation); the analysis-window trend is the random-walk forecast, which for a
  local level is flat at the **last fitted state** `trend[-1]`. Contribution =
  `(trend[-1] − mean_ref(trend)) * y_std`. Document explicitly that this is the
  forecast mean and its uncertainty comes from the posterior of the last state, not
  from forward simulation of new steps (that refinement belongs to T11).
- Both are reported as `{"estimate": float, "ci_95": [lo, hi]}` using the same
  mean/percentile summaries as parent contributions.
- New `unexplained` definition for probabilistic nodes:
  `gap − Σ parent estimates − trend.estimate − seasonal.estimate`.

**Changes.**

1. In `model.py`, add a pure helper (unit-testable without sampling):

   ```python
   def seasonal_window_delta(
       trace, seasonality: List[Any],
       t_ref: np.ndarray, t_an: np.ndarray,
   ) -> np.ndarray:
       """Per-posterior-sample (analysis − reference) window mean of the Fourier
       seasonal component, in normalized units. Returns shape (n_samples,).
       Zero-length seasonality returns zeros."""
   ```

   Implementation: flatten each coefficient to shape `(n_samples,)`, build the sin/cos
   design for `t_ref` and `t_an`, compute the two window means per sample, subtract.

2. In `rca.py`'s posterior branch, after the parent loop:

   ```python
   fit = traces[(node, analysis_start)]
   t = lambda d: (d - fit.dates[0]).days
   t_ref = np.array([t(d) for d in frame_dates_in(ref_start, ref_end)])
   t_an  = np.array([t(d) for d in frame_dates_in(an_start, an_end)])

   trend_samples = fit.trace.posterior["trend"].values.reshape(n_samples, -1)
   trend_delta = (trend_samples[:, -1] - trend_samples[:, t_ref].mean(axis=1)) * fit.y_std
   seasonal_delta = seasonal_window_delta(fit.trace, defn.seasonality, t_ref, t_an) * fit.y_std
   ```

   Guard: if any reference date maps to `t < 0` or `t >= trend_samples.shape[1]`,
   raise `ValueError` telling the user the reference window must lie inside the
   fitted period.

3. Node output gains:

   ```json
   "components": {
     "trend":    {"estimate": ..., "ci_95": [...]},
     "seasonal": {"estimate": ..., "ci_95": [...]}
   }
   ```

   and `unexplained` uses the new definition. Formula/root nodes: `"components": null`.

4. `docs/model.md`: rewrite the `unexplained` section — it is now residual + model
   misfit only, with trend and seasonal reported explicitly; add the whole-week-windows
   recommendation from 1.3.

**Acceptance criteria.**

- Unit test for `seasonal_window_delta` with a hand-built fake trace (numpy arrays
  wrapped in an `arviz.from_dict` posterior): one weekly component with known
  coefficients `a=1, b=0` for h1 and zeros for h2 → assert the returned delta matches a
  direct numpy computation for chosen windows, per sample, to 1e-10.
- Integration test: dataset where `y = 0.3 * x + 5·sin(2πt/7) + Normal(0, 0.3)`,
  x flat; reference window = days 56–83 (4 whole weeks), analysis window chosen as a
  weekday-skewed 10-day span. With `seasonality: [{period: 7, name: weekly}]`,
  assert the `seasonal` component's estimate has the same sign as the true seasonal
  window-mean difference and `|unexplained|` is smaller than it would be without the
  component (compute both from the same RCA response: assert
  `|unexplained| < |unexplained + seasonal.estimate|`).
- Existing RCA tests updated only to tolerate the new `components` key.

### T6 — Per-day Shapley on formula nodes

**Priority: P1.** Fixes 1.4.

**Files:** `breakdown/engine/model.py` (`compute_shapley`), `breakdown/engine/rca.py`
(`shapley_attribution`), `docs/model.md`, `README.md` (Shapley section),
`tests/test_engine.py`, `tests/test_rca.py`.

**Design decisions (fixed):**

- Value function: for coalition `S`,
  `v(S) = mean over analysis-window days t of formula(xᵢ(t) for i∈S; reference-window
  mean of xᵢ for i∉S)`. By Shapley efficiency, contributions sum to
  `v(all) − v(∅) = mean_analysis(formula(x_t)) − formula(reference means)`.
- Response fields: `actual` becomes `mean_analysis(formula(x_t))` (changed);
  `baseline` remains `formula(reference means)` (unchanged); `gap = actual − baseline`.
- What remains outside the attribution (goes to `unexplained` at the RCA level): the
  target's measurement noise around the formula, plus the *reference*-window Jensen
  term (within-reference covariance). Document this asymmetry: the analysis-window
  covariance shift — the behaviorally interesting part — is now attributed; the
  reference window is the stable regime where the term is small and roughly constant.
- Cost: `n_analysis_days × 2ⁿ` formula evaluations, vectorized to `2ⁿ` array
  evaluations. Negligible for realistic n ≤ 5.

**Changes.**

1. Generalize `compute_shapley` so `baselines` and `actuals` values may be either
   scalars or equal-length `np.ndarray`s (the existing scalar path already wraps into
   1-element arrays; lift that): evaluate `eval_formula` once per coalition on full
   arrays, keep `phi` as an array, and return
   `Dict[str, np.ndarray]` when inputs are arrays / `Dict[str, float]` for scalars.
   Simplest implementation: always operate on arrays internally; if all inputs were
   scalars, return floats. The per-day case passes `actuals[p] =` the parent's daily
   values over the analysis window and `baselines[p] = np.full(n_days, ref_mean_p)`,
   then the caller averages each `phi` array.
2. `shapley_attribution`: build daily parent arrays for the analysis window
   (`frame.loc[analysis mask, p].values`), reference means as now; call the vectorized
   `compute_shapley`; `attribution[p] = float(phi[p].mean())`;
   `actual = float(mean of eval_formula over analysis days)`.
3. RCA's shapley branch needs no change beyond what flows through
   `shapley_attribution` (it already consumes `sh["attribution"]` and `sh["gap"]`).
4. Docs: update the Shapley section (README) and the formula-node `unexplained`
   paragraph (`docs/model.md`) per the design decision above.

**Acceptance criteria.**

- Existing pure-function tests (`test_compute_shapley_*`) pass unchanged (scalar path).
- New unit test (vectorized path): `formula = "a * b"`, analysis arrays
  `a = [110, 90]`, `b = [55, 45]`, reference means `a=100, b=50`. Then
  `v(all) = mean(110·55, 90·45) = 5050`, `v(∅) = 5000`; assert the two per-day-averaged
  Shapley values sum to `50.0 ± 1e-9`.
- New regression test for the covariance pitfall: build 60 days where `orders` and
  `aov` each have **unchanged marginal window means** between reference (days 0–29)
  and analysis (days 30–59), but their within-window correlation flips from +0.8 to
  −0.8 (construct via a shared factor with sign flip; add revenue = orders·aov
  exactly). Old behavior: gap ≈ 0 attributed, everything in unexplained. New behavior:
  assert `abs(sum(attribution.values()) − (result["actual"] − result["baseline"])) <
  1e-6` and `result["actual"] − result["baseline"]` reflects the true
  mean-revenue change (nonzero). Assert sum of attributions is that nonzero gap.

### T7 — Window-mean uncertainty via block bootstrap

**Priority: P1.** Fixes 1.5; repairs `prob_same_direction`; gives formula nodes CIs.

**Files:** `breakdown/engine/rca.py`, `docs/model.md`, `tests/test_rca.py`.

**Design decisions (fixed):**

- Method: **circular moving-block bootstrap over window rows**, block length
  `min(7, window_length)`, `n_boot = 500`, `rng = np.random.default_rng(0)` created
  once per `run_rca` call (deterministic API responses).
- Rows are resampled **jointly across all columns** (one set of day-indices per
  replicate, applied to every metric), preserving cross-metric correlation within the
  window — required for formula nodes.
- Reference and analysis windows are resampled independently of each other.
- Probabilistic nodes: contribution samples become
  `beta_raw_samples[i] * delta_samples[i mod n_boot]` where
  `delta_samples[b] = actual_mean_boot[b] − baseline_mean_boot[b]` for that parent
  (lag-shifted windows use the same machinery on the shifted window's rows). To avoid
  index-alignment artifacts, shuffle `delta_samples` once (with the same rng) before
  pairing. `estimate`, `ci_95`, `prob_same_direction` computed from these composed
  samples exactly as today.
- Formula nodes: for each bootstrap replicate `b`, compute reference means from the
  resampled reference rows and per-day Shapley (T6) over the resampled analysis-day
  rows → a `phi_b` per parent. `estimate = mean_b(phi_b)`,
  `ci_95 = [pct 2.5, pct 97.5]`, `prob_same_direction = max(P(φ>0), P(φ<0))` — formula
  contributions now populate all three fields (schema-compatible: previously-null
  fields gain values).
- Node-level `baseline`/`actual`/`gap` stay point estimates (they are descriptive);
  only contributions carry uncertainty.

**Changes.**

1. Add to `rca.py`:

   ```python
   def _block_bootstrap_indices(n: int, n_boot: int, rng, block: int = 7) -> np.ndarray:
       """(n_boot, n) integer index array; circular moving-block bootstrap."""
   ```

   Implementation: `block = min(block, n)`; for each replicate draw
   `ceil(n / block)` uniform start positions in `[0, n)`, expand each to
   `start, start+1, ..., start+block-1` mod `n`, concatenate, truncate to `n`.

2. Precompute, per window per node evaluation, the resampled row-index arrays once and
   reuse across that node's parents (joint resampling).

3. Rewire the posterior branch and the shapley branch per the design decisions. Keep
   the existing point-estimate code path callable for T6's `GET /shapley` endpoint
   response (`attribution` there stays the point estimate; do not bootstrap in
   `shapley_attribution` itself — the bootstrap lives in `run_rca`).

4. `docs/model.md`: update the "How RCA attributes a change" section — contributions
   now reflect both coefficient uncertainty and window-sampling uncertainty; formula
   contributions have CIs; note the bootstrap's assumption (within-window
   stationarity, ≤ 7-day dependence).

**Acceptance criteria.**

- Unit test for `_block_bootstrap_indices`: shape `(n_boot, n)`; all indices in
  `[0, n)`; with `n < block` it degenerates gracefully; deterministic given the rng
  seed.
- New test (short-window honesty, the point of the ticket): 100-day noisy series,
  analysis window of 3 days vs analysis window of 28 days for the same node; assert
  the parent contribution's `ci_95` width is strictly larger for the 3-day window.
- New test: formula-node contributions in RCA output now have non-null `ci_95` and
  `prob_same_direction ∈ [0.5, 1.0]`.
- Updated test: `test_rca_formula_attribution` — drop the "ci is None" assertions,
  keep the sum-to-gap assertion against the contribution `estimate`s with a loosened
  tolerance (`1e-6` → the estimates are bootstrap means; assert
  `abs(total − (gap − unexplained)) < 0.05 * max(1, abs(gap))` instead).
- Determinism test: two identical `run_rca` calls return identical contribution
  numbers.

### T8 — Convergence diagnostics, surfaced everywhere

**Priority: P2.** Fixes 1.6 and the reporting half of 1.7.

**Files:** `breakdown/engine/model.py`, `breakdown/api/main.py`,
`breakdown/engine/rca.py`, `docs/model.md`, `tests/test_engine.py`, `tests/test_api.py`.

**Design decisions (fixed):**

- NUTS diagnostics: `divergences = int(trace.sample_stats.diverging.sum())`;
  `max_rhat = float(az.summary(trace)["r_hat"].max())` (guard NaN);
  `min_ess_bulk = float(az.summary(trace)["ess_bulk"].min())`.
  `fit_quality = "suspect"` if `divergences > 0.01 * (draws * chains)` **or**
  `max_rhat > 1.05` **or** `min_ess_bulk < 100`; else `"ok"`.
- ADVI diagnostics: capture `approx.hist` (the loss history, −ELBO) inside
  `fit_metric`. Let `w = len(hist) // 10`; `last = hist[-w:]`,
  `prev = hist[-2*w:-w]`. `fit_quality = "suspect"` if
  `abs(mean(last) − mean(prev)) > 0.5 * std(last)` (loss still moving), else `"ok"`.
  Record `elbo_drop = float(mean(prev) − mean(last))`.
- Diagnostics live on `FitResult.diagnostics` as a plain dict:
  `{"fit_quality": "ok"|"suspect", "method": ..., plus the numbers above}`.
- Nothing blocks. `"suspect"` is information, not an error.

**Changes.**

1. `fit_metric` populates `FitResult.diagnostics` per the rules above (compute inside
   the function; for ADVI, `approx` is in scope; for NUTS, the trace is).
2. `/analyze` response gains `"diagnostics": fit_result.diagnostics`.
3. `run_rca` node output gains, for posterior nodes:
   `"inference_method"` and `"fit_quality"` copied from the node's `FitResult`
   (formula/root nodes: `null`).
4. `docs/model.md` §6 (ADVI vs NUTS): document `fit_quality` and its thresholds; state
   plainly that a `suspect` CI should be re-run with NUTS
   (`POST /analyze/{name}?fit_end=<analysis_start>&inference_method=nuts`).

**Acceptance criteria.**

- New test: NUTS fit on well-behaved synthetic data (post-T3 model) yields
  `fit_quality == "ok"`, `divergences` present and an int, `max_rhat < 1.05`.
- New test: ADVI `FitResult.diagnostics` contains `fit_quality` and `elbo_drop`.
- API test: `/analyze` response contains `diagnostics.fit_quality`; RCA response's
  posterior nodes contain `inference_method == "advi"` and a `fit_quality`.

### T9 — Input validation: date grid, seasonality identifiability, window sanity

**Priority: P2.** Fixes the 1.10 items that silently corrupt results, and the example.

**Files:** `breakdown/api/main.py`, `breakdown/engine/model.py`,
`breakdown/engine/rca.py`, `examples/jaffle_shop_tree.yml`, `docs/model.md`,
`tests/test_api.py`, `tests/test_rca.py`, `tests/test_engine.py`.

**Design decisions (fixed):**

- **Date grid** (in `_fetch_all_metrics`, after the inner join): compute
  `missing = pd.date_range(data.date.min(), data.date.max()).difference(data["date"])`.
  If non-empty, raise `RuntimeError` listing up to 10 missing dates and the count.
  Also log `n_dropped = (per-metric max row count) − len(data)` as a warning when > 0
  ("inner join dropped N dates present in only some metrics").
- **Seasonality identifiability** (in `fit_metric`, after `_prepare_series` so `len(y)`
  reflects lag trimming and `fit_end`): for each component, require
  `4 <= period <= len(y) / 2`; else raise
  `ValueError("Seasonality '{name}' (period {P}) is not identifiable on {n} fitted "
  "rows; need at least 2 full periods (and period >= 4).")`.
- **Window sanity** (new `_validate_windows` in `rca.py`, called by both
  `shapley_attribution` and `run_rca`): require
  `reference_start <= reference_end < analysis_start <= analysis_end` (strict `<`
  between the windows — overlap is an error, not a warning); require all four dates
  inside `[data.date.min(), data.date.max()]`. For each lagged parent, pre-check that
  the shifted windows lie inside the data range and raise a message that names the
  parent, the lag, and the *shifted* dates, so the user isn't confronted with a window
  they never typed.
- `examples/jaffle_shop_tree.yml`: delete the `period: 365` annual entry (keep
  weekly).

**Acceptance criteria.**

- Test: a metrics frame with a hole (drop one mid-range date) makes startup fetch
  raise with the missing date in the message (test `_fetch_all_metrics` directly with
  a stub fetcher).
- Test: `seasonality: [{period: 365, name: annual}]` on 100 days raises the
  identifiability error; `period: 7` on 100 days does not; `period: 3` raises.
- Test: RCA with `analysis_start <= reference_end` raises; RCA with a lagged parent
  whose shifted reference window precedes the data start raises with the parent's name
  and shifted dates in the message.
- The example tree loads and the default server startup path (mock provider, default
  window) fits `revenue` without the identifiability error.

### T10 — Calibration test suite

**Priority: P2.** This is what "rigorous" means operationally; it guards T1–T9 against
regression. New file: `tests/test_calibration.py`. Mark the coverage test
`@pytest.mark.slow` and register the marker in `pyproject.toml`.

**Test 1 — known root cause is found.** Generator: 120 days.
`sessions = 5000 + cumsum(Normal(0, 50)) + 300·sin(2πt/7)`; inject a level drop of
−25% on days 90–119. `order_count = 0.1·sessions + Normal(0, 10)`;
`aov = 50 + Normal(0, 2)` (no shift); `revenue = order_count · aov` exactly. Tree =
the JAFFLE_YAML from `tests/test_rca.py` plus weekly seasonality on sessions'
children where declared. RCA on `revenue`, reference = days 62–89 (4 whole weeks),
analysis = days 90–117 (4 whole weeks). Assert:
(a) at the `revenue` node, `order_count`'s `|share_of_gap|` > 0.6 and >
`average_order_value`'s;
(b) `ranked_causes[0]["metric"] ∈ {"order_count", "daily_sessions"}`;
(c) at the `order_count` node, `daily_sessions`' `prob_same_direction > 0.9`.
Seed everything; use `advi_draws=300`.

**Test 2 — null case manufactures no cause.** Same generator, no injected shift,
same windows. Assert no contribution anywhere in the response has both
`prob_same_direction > 0.95` **and** `|share_of_gap| > 0.5`. Run over 3 fixed seeds to
guard against a lucky pass.

**Test 3 — CI coverage (slow).** 20 replicates: `x = 100 + cumsum(Normal(0, 3, 90))`,
`y = 0.5·x + Normal(0, 1)`, new seed per replicate. Fit with NUTS
(`draws=300, tune=300, chains=2` for speed), count how often the 95% HDI of
`beta_raw` contains 0.5. Assert count ≥ 15 (binomial slack below the nominal 19;
this catches gross miscalibration, which is the goal). Do the same count for the RCA
contribution CI containing the true contribution `0.5 · Δx̄` — after T7 this must
also pass; before T7 it demonstrably fails for short windows, which is why this
ticket follows T7.

### T11 — Counterfactual mode (posterior-predictive forecast)

**Priority: P3 — only after T1–T9.** The full Brodersen et al. pattern: from the
pre-period fit (T2), forward-simulate the analysis window — extend the random walk
`analysis_length` steps per posterior sample (`trend[-1] + cumsum(sigma_trend·z_new)`),
evaluate the Fourier terms at the analysis `t`'s, add `Σ β·x_observed` and observation
noise — and report `gap_vs_counterfactual = mean_analysis(actual) −
mean_analysis(forecast)` with a posterior CI, per node. This upgrades T5's flat-trend
approximation and gives the headline number "the drop was X units below what the
normal regime predicts (95% CI …)". Ship as a new response block, not a change to the
existing decomposition. Specify fully when picked up; it reuses `FitResult` and the
T5 time-index convention as-is.

### T12 — Later improvements (specify when picked up)

- **Posterior path attribution to replace `ranked_causes`:** per posterior/bootstrap
  sample (T7 machinery), chain each hop's contribution share along every
  target-to-ancestor path, sum across paths (fixes diamond double-counting), and
  report `P(ancestor accounts for > 25% of the target gap)`. Keep the current
  heuristic until this lands; it is documented as a heuristic.
- **Full-rank ADVI or Pathfinder for the on-demand RCA path:** with T3's geometry,
  mean-field is much better; full-rank (`pm.fit(method="fullrank_advi")`) captures
  β–trend posterior correlation at these small parameter counts. Benchmark against
  NUTS on the T10 suite before switching the default.
- **Residual-share reporting for formula-node fits** (1.10): report
  `1 − var(residual)/var(target)` in `/analyze` output so users see how much the
  identity explains before reading the residual decomposition.

---

## Appendix — file/line index for the issues above

Line numbers refer to the tree at the time of this review (commit `688285a`).

| Issue | Location |
|---|---|
| Fit includes anomaly window | `engine/model.py:212-276`, `engine/rca.py:124-128` |
| Trend prior / centered GRW / wide α | `engine/model.py:257-262` |
| Gap decomposition drops trend/seasonal | `engine/rca.py:159-190` |
| Shapley on window means (Jensen/covariance) | `engine/rca.py:80-92` |
| Window means as point masses; no formula CIs | `engine/rca.py:33-43,135-137,150-157,164-174` |
| No convergence gating | `api/main.py:189-193`, `engine/rca.py:128` |
| ADVI default, method not echoed | `engine/rca.py:128`, `engine/model.py:271` |
| Data-dependent prior scaling / full-sample z-scoring | `engine/model.py:27-48,116-121,192-209` |
| Trace-only return contract | `engine/model.py:212-276` |
| Example annual seasonality on 100 days | `examples/jaffle_shop_tree.yml:35-38`, `api/main.py:22-23` |
| Dead `trend:` config field | `parser.py:49` |
| Inner join → irregular grid, `t = arange` | `api/main.py:52`, `engine/model.py:254` |
| Ranked-causes double counting | `engine/rca.py:211-243` |

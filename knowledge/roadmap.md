# Breakdown — Product Roadmap

A prioritized list of what to build, grounded in what's already shipped. Horizons,
not dates: each gates on its exit criteria. This is the product/engineering roadmap
only — no go-to-market.

A blocking correctness gate
([**Horizon 0**](#horizon-0--correctness-numbers-the-engine-cant-defend)), three
horizons, plus one standing workstream:
[**Statistical rigor (S)**](#statistical-rigor-s--a-standing-workstream), which
runs alongside them and holds the improvements identified in the
[statistics white paper](statistics_whitepaper.md).

Legend: ✅ shipped · ◑ partially shipped · ○ not started.

---

## Product principles

1. **Build against real need.** Prefer the item a concrete use case is blocked on
   over the speculatively useful one. The roadmap is a priority order, not a promise.
2. **The engine is the open core.** Statistical inference, attribution, and RCA are
   the differentiated, open part. Operational surfaces (scheduling, alerting,
   hosting, multi-user) are a separate layer built on top, not woven into the engine.
3. **Never ship a number the engine can't defend.** Credible intervals, a
   first-class `unexplained` term, and suspect-fit flags are the brand. Every new
   surface inherits this honesty posture — no bare point estimates.
4. **Optimize time-to-first-trusted-RCA.** Breakdown should be easy to set up and get value from. The product's north-star metric: elapsed
   time from "here are my credentials" to "breakdown correctly explained an incident
   I already understood." 

---

## Current status (what's built)

**Engine.** Per-node Bayesian Structural Time Series with a non-centered, tightly-
prioritized local-level trend; contemporaneous and lagged regressors; business-unit
priors with automatic rescaling. RCA over the ancestor DAG combining exact **per-day
Shapley** on formula nodes and **posterior attribution** on probabilistic nodes,
with explicit **trend/seasonal component** decomposition and **block-bootstrap**
uncertainty on window means. Fits run on data strictly **before** the analysis window
(uncontaminated by the anomaly). Convergence diagnostics (`fit_quality`) on every fit.
Steady-state **what-if simulation** (interventions, assumption links, Shapley source
attribution). **Cold start mode** (`provider: none`, a *demo* mode — see the note
below): the full what-if machine on a tree with zero data — asserted `baseline`
operating points, coefficients sampled directly from the YAML priors, declared
`plausible` honesty bounds; served end to end (API mode label, guarded routes,
MCP caveats, belief-first UI surface, bundled `cold_start_tree.yml` example). The
same priors feed `fit_metric` when data arrives, so graduation is a provider
swap. *(statistical plan T1–T8; what-if design spec; cold_start_design.md
P1–P5.)*

> **Cold start is a demo mode, not a supported persona (2026-08-05).** It ships
> and stays correct — [C7](#horizon-0--correctness-numbers-the-engine-cant-defend)
> bounded its baseline draws (shipped 2026-08-06) and
> [S7](#statistical-rigor-s--a-standing-workstream) stays scheduled — but it
> stops earning new roadmap surface. The persona it was built for (a pre-revenue
> founder) has **no incident to explain**, which is the product's actual moment
> of value, and what it produces is sensitivity analysis on stated assumptions —
> a category with incumbents. The follow-on hybrid mode was removed; see
> [Deliberately not on the roadmap](#deliberately-not-on-the-roadmap).

**Providers.** `mock`, `local` (MetricFlow), `cloud` (dbt Cloud Semantic Layer),
`warehouse` (direct SQL). Provider config supports `${ENV}` secret references.

**UI.** Cytoscape DAG served at `/ui`; per-metric time series + posterior inspection;
full point-and-click RCA workflow (window presets, client-side validation, target
strip, certainty channels, component rows, unexplained badges); what-if tab; node
**stat cards** (big number + delta + sparkline, configurable, format-aware);
deep-linkable RCA / metric / what-if views. *(UI plan U1–U4 + node cards.)*

**MCP server.** Four tools (`get_tree`, `explain_metric`, `run_rca`, `run_whatif`)
over streamable HTTP at `/mcp`, sharing the API process and trace cache; analysis
responses carry `how_to_read` caveats and deep links back into the UI. *(2.5.)*

**Input validation.** Window ordering and overlap, per-node window coverage
(including lagged parents' shifted windows), a gap-free date spine per grain, and
seasonality identifiability (Nyquist harmonic filter + fit-length warnings). *(T9.)*

**Not yet built** (the roadmap below): the rest of the Horizon 0 correctness gate
(C1–C7 shipped — every provider now shares one date-alignment contract, RCA
publishes the exact Shapley value, the window bootstrap's finite-sample
attenuation is corrected, `ranked_causes` no longer inverts on offsetting
noise, duplicate metric names are rejected at parse time, cold-start belief
draws respect their declared bounds, and the trace cache is bounded, thread-safe
and keyed by fit identity; **C9–C10 remain**), remaining
statistical rigor (T11), UI trust finish
(U5–U6), the connectivity kit, and the market-driven items (scheduled
monitoring, the rest of dimensional slicing).

---

## Horizon 0 — Correctness: numbers the engine can't defend

Gate: **blocks everything below.** Principle 3 is not satisfied while these ship.

Every item here is a confirmed defect that produces a *plausible wrong number*
rather than an error — the same failure class Horizon 1.1 was built to close,
found in the places 1.1 didn't look: the provider boundary, the published
statistic, and the agent-facing payload. They came out of a hostile external
review of the engine, docs, and tests conducted **2026-08-05** against 0.1.0;
each was traced to a specific code path, and the `file:line` references are the
starting point for whoever picks the item up.

This is not a standing workstream. It is a punch list with an end.

| # | Item | Status | Why |
|---|------|--------|-----|
| C1 | **tz-aware dates must fail loudly, not silently zero** — `pd.to_datetime` on a `TIMESTAMP` column preserves tzinfo; `floor_period` calls `.normalize()`, which preserves it too, so the alignment guard at `data_fetch.py:414` passes. The tz-aware index then matches nothing in the tz-naive `period_spine`, `notna().any()` is False so the trailing trim is skipped, and `fillna(0.0)` returns a **full spine of zeros**. Coerce to tz-naive (warning when tz was present) and raise when the spine join drops every row. Shipped as two shared module-level helpers in `data_fetch.py`: `_to_naive_dates` (drops the zone, keeping the labelled wall-clock date — converting through UTC would move a `+09:00` midnight back a day) runs before anything else touches the dates, and rows that *all* miss the spine now raise instead of zero-filling, since a query ignoring its bound window is a bug rather than an all-quiet series | ✅ | **Ship first.** Any SQL returning `TIMESTAMP` rather than `DATE` (Databricks attaches UTC) turns a whole metric into zeros. `_check_contiguous` passes because the spine is complete, nothing logs, RCA reports a −100% gap and confidently attributes it — and then `snapshots.py` writes the zeros to parquet and serves them forever. A transient connector setting becomes committed data |
| C2 | **Snap `cloud` and `local` to the period spine** — `CloudDataFetcher.fetch_metric` (`data_fetch.py:176`) and `LocalDataFetcher.fetch_metric` (`:261`) floor the labels and return; only `warehouse` and `mock` call `period_spine` and drop partial edge periods. Lift the warehouse fetcher's spine/trim/kind-fill block into a shared `BaseDataFetcher` helper and call it from all three. Same change: **warn** on interior gap-fill instead of silently zeroing a flow. Shipped: `_align_to_spine` holds the spine/trim/kind-fill contract and all three providers call it, so partial edge periods are dropped everywhere and interior gap-fills log the periods they invented. Label policy stays per-provider by design — warehouse errors on a misaligned label (the SQL author owns the aggregation), the semantic-layer providers floor with a warning (dbt may use non-Monday weeks) | ✅ | A window ending Tuesday on a `grain: week` metric returns a two-day partial week as a full row at ~2/7 normal volume, and `snap_window` treats it as whole — a manufactured −71% gap. `/meta`'s `data_through` then reports coverage through days that were never fetched. A three-day ETL outage becomes three zero days, indistinguishable from a real collapse. The mock provider snaps correctly, which is exactly why the test suite never sees this |
| C3 | **Publish the exact Shapley value as `estimate`** — `rca.py:553` reports `float(phi_b.mean())`, the bootstrap mean of a *nonlinear* function of jointly-resampled window means, while `unexplained` (`:586`) comes from the separate **exact** call. `shapley_attribution` already returns exact per-parent values in `result["attribution"]`; use them, and keep the bootstrap for the interval only. Flip `test_rca_formula_attribution` from its 5% tolerance to exact and re-pin the golden. Shipped: `estimate` is now `sh["attribution"][p]` and the two-level `decomposition` parts are exact too, so the split sums to the estimate as exact values rather than in expectation over replicates; the bootstrap supplies `ci_95` and `prob_same_direction` only. The golden test was re-pinned (`gap` and `unexplained` unchanged — they always came from the exact call) and now also asserts the reconciliation property, which is what let the bug survive a golden test in the first place. Residual on the pinned tree: 3.78 → 4.5e-13 | ✅ | Joint resampling induces `Cov(x̄,ȳ) ≠ 0` and the means-bridge is bilinear, so `E[φ_boot] ≠ φ_exact` — contributions don't reconcile with `gap`, contrary to [`docs/model.md`](../docs/model.md). The golden test encodes the discrepancy today (`gap − unexplained` off by 3.78). We compute both numbers and publish the wrong one; on a ratio with a noisy denominator the bias is unbounded in principle |
| C4 | **Bootstrap degeneracy guard and short-window attenuation** — two parts. (a) `single_period` (`rca.py:465`) keys on `n_periods == 1`, so a window in which a parent is *constant* (unlaunched feature, zero-inflated series) collapses every replicate to the same value and ships a **zero-width `ci_95` with `ci_status: "ok"`**; key the guard on the resampled spread instead. (b) `_block_bootstrap_indices` (`:188`) caps the block at `n // 2`, which lands on the midpoint of the degeneracy curve it is reasoning about — the resampled variance of the window mean is attenuated, worst on short windows, and non-monotone in `n`. *Data-driven block length stays [S6](#statistical-rigor-s--a-standing-workstream)*. Shipped, but **not as scoped** — the cap was the wrong target. Any `l < n` carries the `(1 - l/n)` attenuation; the cap only decides how large it is, so no choice of constant fixes it. `_window_mean_correction` instead rescales the replicate spread by `1/sqrt((1 - l/n)(1 - 1/n))`, the second factor being the ddof gap of the empirical distribution, both derived exactly for the iid case and confirmed by measurement. Applied to the means bridge (not the co-movement games, whose replicate pairing would break), to the window half of a posterior contribution (not the coefficient posterior, which carries no such attenuation), and to slice attribution, which shares the estimator. The guard now keys on resampled spread per contribution, with a new `ci_status: "degenerate_constant_window"`; the UI matches `degenerate_*` by prefix so a future status is never silently rendered as a normal result. **Coverage is improved, not fixed** — measured in `docs/model.md`; the residual is S6 and the new S18 | ✅ | Formula-node CIs come **entirely** from this path with no offsetting term, and [`docs/model.md`](../docs/model.md) sells the bootstrap as the honesty mechanism for exactly the short "what happened this weekend?" windows where the attenuation is worst. A too-narrow interval on the engine's most-quoted number violates principle 3 twice over |
| C5 | **`ranked_causes` inverts on near-zero gaps** — `rca.py:751` uses `min(abs(share), 1.0)`, and `share_of_gap` is `None` only when `abs(gap) < 1e-12` (absolute, not relative to node scale). A node with `gap = 1e-6` and two parents at `+0.5` / `−0.5` yields shares of ±5×10⁵, both clamped to **1.0**. Make the guard relative to node scale and penalize `\|share\| ≫ 1` rather than saturating at it. Shipped as `_hop_weight`: `min(|s|, 1/|s|)`, peaked where a parent explains its child exactly and decaying either side — symmetric in log-space, so explaining 10% and explaining 1000% are both weak evidence, for opposite reasons. The `share_of_gap` guard is now `_GAP_EPS * max(|baseline|, |actual|, 1)` rather than an absolute `1e-12`, so a node at 1e6 with a gap of 1e-6 reports no share at all and nothing upstream can inherit influence for a movement that did not happen. The two fixes are independent and both were needed: the relative guard alone would still saturate on a node that moved 1% between parents that moved ±50% | ✅ | The metric that most conclusively did *not* move hands its full influence score to everything above it, and scores accumulate across children — so a well-connected node with several quiet children can top the ranking on pure offsetting noise. `ranked_causes` is the most prominent number in the UI. "Documented as a heuristic" does not cover "inverted on a specific, common input" |
| C6 | **Reject duplicate metric names** — `parser.py:479` adds nodes then edges in two passes with no uniqueness check, and `MetricTreeConfig` (`:466`) has no validator. The second definition wins `nodes[name]["definition"]` while **both** metrics' edges are added. Shipped as `MetricTreeConfig.check_unique_names`, a parse-time validator naming every offender with its repeat count — on a 107-node tree "there is a duplicate" is not actionable. Placed on the config model rather than in `_build_dag` so it fails where every other schema violation does, before anything is fetched. A companion test asserts the invariant itself — that `list(dag.predecessors(n))` equals the declared parents for every node — since that is the property the validator exists to protect | ✅ | `list(dag.predecessors(name))` is the axis order of `beta_raw` — the load-bearing invariant AGENTS.md names. With a duplicate, `predecessors` returns the union while `priors`, `lags` and `expected_signs` are validated against the winning `defn.parents`, so every declared prior lands on the wrong axis or silently falls back to `Normal(0,1)`, and `_fetch_all_metrics` overwrites the data too. Nothing raises. One validator fixes it |
| C7 | **Bound cold-start baseline draws** — `simulate.py:335` computes `sigma = (high − low)/(2·z90)` and calls `rng.normal(mu, sigma, n_draws)` unconditionally; declared `plausible` bounds are never consulted at sampling time. Truncate at `plausible` min/max, add a **LogNormal** baseline option (`LogNormal` already exists for edge priors), and stop reporting the Monte-Carlo **mean** as the central number on ratio nodes. Shipped as three measured decisions. (a) `_sample_baseline` truncates draws to declared `plausible` bounds by **rejection**, not clipping — clipping would pile a fake mode on the boundary. (b) `distribution: lognormal` on a baseline fits the stated interval exactly while staying positive; **opt-in**, because it moves the reported centre (`[20, 120]` reports ~57, not 70) and that re-reads the author's intent. (c) The central statistic switches to the median **only where the mean has demonstrably not converged**, detected by fold stability and disclosed in a caveat naming the node. A global switch was measured and rejected: reconciliation error mean-vs-median is 0.00%/0.43% on `a+b`, 0.25%/5.43% on `a*b`, 13.2%/0.75% on `a/b` — neither statistic dominates, so swapping everywhere would have made the *common* cold-start node worse | ✅ | On the shipped `cold_start_tree.yml`, `paying_customers` draws ~1% **negative customer counts** and `mrr` inherits them. On ratio formula nodes `base_mu[n] = base_draws[n].mean()` takes the mean of a Cauchy-like ratio, which does not exist: a founder saying "somewhere between 2 and 40 signups a month" — an ordinary order-of-magnitude belief — gets a **$2.1M CAC** with a negative lower bound. Cold start is a demo mode now, and a demo that prints a negative customer count is worse than no demo |
| C8 | **Trace cache, concurrency, and the NaN 500** — `app.state.traces` has no eviction, no TTL and no size bound, and its key `(name, fit_end)` (`api/main.py:504`) ignores inference method and `draws`, so `POST /analyze/{name}?draws=50` poisons the key `run_rca` later reuses. `/meta` (`:393`) and `_pick_fit` (`:155`) iterate the dict outside `app.state.lock` while `run_rca` mutates it from a worker thread. `slices.py:326` writes `np.nan` into `share_b` on near-zero reference totals, which reaches `np.percentile` (`:268`) and then Starlette's `allow_nan=False` as an unhandled **500**. Shipped as `FitKey` + `TraceCache` in `engine/model.py`. The key is now the fit's full *identity* (method, draws, tune, chains, seed), so a manual `/analyze` can no longer land on the entry RCA reuses; defaults match the engine's own on-demand fit so call sites stay terse. The cache is LRU-bounded (256) and holds a **`threading.Lock`** — the pre-existing `asyncio.Lock` could never have fixed the iteration crash, since the mutation happens off-loop in a worker thread, and making readers take the app lock would have queued `/meta` behind a multi-minute fit. Slice replicates with an undefined baseline share are dropped, and the interval withheld when too few survive, instead of reaching `allow_nan=False` as a 500 | ✅ | The reference tree has 107 metrics; one RCA adds ~100 `InferenceData` objects and a second `analysis_start` adds 100 more — the user OOMs their own process. The poisoned key falsifies `shaping.py:133`'s promise that the deep link reproduces the numbers. The unlocked iteration raises `dictionary changed size during iteration` precisely while the UI polls a long RCA. *(The wider auth surface is deliberately **not** here — the default bind is loopback and `BREAKDOWN_API_TOKEN` gates `/mcp`; that belongs to [3.5](#horizon-3--make-it-findable-and-sticky-it-comes-to-you).)* |
| C9 | **The agent payload double-counts the interaction** — `compact_rca` (`mcp/shaping.py:182`) drops each contribution's `decomposition` but **keeps** the node-level `interaction`. Contribution `estimate` is already `means + comovement` (`rca.py:549`) and `interaction` is the sum of exactly those comovement parts. Drop or relabel it. Same item: `components` is collapsed to `{k: v["estimate"]}`, so the trend number reaches the narrator with no uncertainty at all, and `WHATIF_HOW_TO_READ` (`:133`) says contributions sum to the delta *estimate* when they sum to the *point* delta | ○ | An LLM narrating "parents contributed X and Y, plus an interaction of Z" double-counts the entire co-movement term. The MCP surface is the strategic asset — `how_to_read` caveats travelling with the payload is the idea nobody else has — and a payload that invites arithmetic error undoes it. Cheap to fix, and the failure is invisible in review because the tree used to demo it is multilinear |
| C10 | **Bring `b2b_mrr_tree.yml` up to the schema it is the reference for** — audited: 107 metrics, 47 formula nodes, **6 probabilistic**, 114 edges of which 100 are deterministic; and **zero** `grain`, `kind`, `dimensions`, `expected_signs`. All 31 rate-shaped metrics inherit `grain: day, kind: flow`, violating the guidance the README sets in bold; `total_mrr` and `total_customers` are described as stocks and declared as flows; `total_email_subscribers` is defined as the period's *net add* and then multiplied by send frequency. Add `grain`/`kind` throughout, `dimensions` on sliceable nodes, `expected_signs` on the 14 learned edges, fix the net-add definition and the `product_qualified_leads` collinearity. Consider a parse-time lint for rate-shaped names inheriting `kind: flow` | ○ | It is cited as the worked reference tree, so it is the answer to "how hard is this to author" and the shape a new author copies. Today it would mis-model 31 rates, mis-aggregate 3 stocks, carry a definitional error and a structural collinearity, and — with zero `dimensions` — cannot run the slicing workflow at all. Largest item here, and the one most likely to surface further engine gaps while doing it |

**Order:** C1 → C2 → C3 first. C1 and C2 are the two that commit wrong numbers to
snapshots, and C3 makes an existing documentation claim true for one lookup.

**C1 and C2 shipped together (2026-08-05)** — they were one fix wearing two hats.
C2's shared `_align_to_spine` helper is where C1's tz coercion belongs, so doing
C1 alone would have meant writing the guard twice and deleting one. Both failure
modes have tests that fail against the previous code.

**C3 shipped (2026-08-05).** Worth recording what it cost, because it was
advertised as a one-line fix and the line itself was: the work was re-pinning a
golden test that had pinned the *wrong* numbers, and noticing that the same bias
sat in the two-level `decomposition` and the `interaction` row. A golden test
that pins values without also asserting the property those values exist to
protect will happily lock in a bug — the re-pinned test now asserts the
reconciliation directly.

**C4 shipped (2026-08-05), and changed shape on contact.** The item said "correct
the `n // 2` block cap"; the cap turned out to be the wrong target, because *any*
block shorter than the window carries the attenuation and the cap only sets its
size. The fix is an analytic correction factor, not a different constant.

More importantly, **C4 does not close the weakness it belongs to.** Measuring
coverage rather than assuming it showed the correction is a strict improvement
that still leaves the nominal 95% interval covering 0.84–0.92 (iid) and 0.57–0.83
(AR(1) ρ=0.7). The remainder splits cleanly into two scheduled items — S6 for the
fixed block length, and **S18, added by this work**, for the normal-shaped tail on
short windows. The measured table lives in [`docs/model.md`](../docs/model.md) and
is quoted rather than summarized, because "improved" and "honest" are not the same
claim.

**C5 shipped (2026-08-05).** Two independent fixes, both needed: a scale-relative
`share_of_gap` guard, and a hop weight that *decays* above 1 instead of
saturating. Either alone leaves the inversion reachable — the relative guard does
not help a node that moved 1% between parents that moved ±50%, and the decaying
weight does not help a node whose gap is numerically nonzero but substantively
zero. [S12](#statistical-rigor-s--a-standing-workstream) remains open — C5 fixed
the defect, S12 is about the prominence.

**C6 shipped (2026-08-06)**, folded into the same change as C5 because it really
was one validator. Its test suite is the interesting part: alongside the two
rejection tests there is one asserting the *invariant* — that
`list(dag.predecessors(n))` equals the declared parents for every node. C3's
golden test taught us that pinning behaviour without pinning the property behind
it is how a defect survives a green suite. **C7 is next**, and unlike C5/C6 it
carries real design content (a bounded distribution choice), so it gets its own
change.

**C7 shipped (2026-08-06).** The first item where the *measurement* changed the
design rather than confirming it. The obvious fix — report the median instead of
the mean on ratio nodes — was measured and **rejected as a global rule**: mean
and median reconciliation error against the per-draw identity runs 0.00%/0.43%
on `a+b`, 0.25%/5.43% on `a*b`, 13.2%/0.75% on `a/b`. Neither dominates, and
cold-start trees are mostly multiplicative, so swapping everywhere would have
made the common node worse to fix the rare one. The shipped design keeps the
mean where it is sound and switches only where fold-stability shows it has not
converged — the same "withhold rather than publish an indefensible number"
posture as C4's degeneracy guard, except here a defensible alternative exists,
so it is substituted and disclosed rather than withheld.

**C8 shipped (2026-08-06).** Four sub-defects, and the concurrency one was misdiagnosed in the original write-up — including by me when I scheduled it. The row said `/meta` and `_pick_fit` "iterate the dict outside `app.state.lock`", implying the fix was to take that lock. It is not: the mutation happens in a **worker thread** via `asyncio.to_thread`, and an `asyncio.Lock` serializes coroutines only — it never protected the dict at all, and making readers acquire it would have queued `/meta` behind a multi-minute fit. The cache owns a `threading.Lock` instead. **C9 is next**, and is small; C10 (the 107-node reference tree) is the last and largest.

**Exit:** every row ✅, and no statement in [`docs/model.md`](../docs/model.md) or
the [white paper](statistics_whitepaper.md) describes behavior the code does not
have.

---

## Horizon 1 — Prove it: a trustworthy, reproducible RCA

Goal: an RCA a stakeholder believes, on governed metrics, that re-runs deterministically.

| # | Item | Status | Why |
|---|------|--------|-----|
| 1.1 | **Statistical hardening finish** (T9) — input validation, seasonality identifiability, and the example's documented pitfall. Shipped: `_validate_windows` (ordering; overlap is an error) + `_validate_coverage` (snapped windows must lie fully inside the node's own data; lagged parents checked on their *shifted* windows and reported with parent/lag/shifted dates) on both `run_rca` and `shapley_attribution`; a gap-free-spine check in `build_grained` naming up to 10 missing periods, plus an inner-join drop warning; Nyquist-filtered Fourier harmonics (`identifiable_harmonics`: keep harmonic `k` only when `2k < period`, so periods 3–4 keep one and `period < 3` is rejected at parse time) with dropped harmonics reported in `seasonality_warnings`; annual component removed from the jaffle example and the B2B MRR reference tree | ✅ | Silent-corruption guards. Every case fixed here produced a *plausible wrong number* rather than an error: a partly-covered window averaged whichever periods existed, a hole in the date spine shifted every downstream date (positional `t`, lags, bootstrap blocks), and periods 2–4 fit rank-deficient seasonal designs whose extra parameters were pure prior |
| 1.2 | **Calibration test suite** (T10) — known-root-cause recovery, null-case restraint, CI coverage against synthetic ground truth. Shipped as `tests/test_calibration.py` (contemporaneous/lagged/identity recovery, null + unrelated-parent restraint, 20-world CI coverage), made deterministic by the sampler seeding | ✅ | The moat made testable; guards T1–T9 against regression |
| 1.3 | **Config hardening** — per-metric grain floors, `kind` (flow/stock/rate) and sign-convention metadata. `grain` + `kind` shipped with 1.7; `expected_signs` (declared coefficient direction + contradiction diagnostic) shipped after a live wrong-sign what-if on the Net-New-MRR tree; display sign conventions shipped as `direction: up_is_good|down_is_good|neutral` (goodness-aware UI coloring; arrows stay directional) | ✅ | Table stakes before config lands in an external repo; prevents cumulative-vs-flow and sign traps |
| 1.4 | **UI trust finish** — fit provenance in the Metric tab, name-keyed coefficients, fit-window controls (U5); accessibility & keyboard pass (U6) | ○ | The reader/reviewer persona is the audience these features serve |
| 1.5 | **Exportable RCA report** — one click → self-contained HTML (printable to PDF): target strip, tree snapshot, ranked causes, attribution tables, methods footnote. Shipped client-side from the Share menu (embedded PNGs, zero external requests) | ✅ | The shareable artifact; the thing an analysis becomes when it leaves the app |
| 1.6 | **Validate against a known incident** — replay a historical anomaly on real governed data end-to-end. Done on the Net-New-MRR tree: the May→June 2025 credit-pack→unified-subscription migration recovered live (expansion MRR ≈ the whole swing), on both the daily and weekly engines | ✅ | The validation moment: recovering a known answer earns trust on the unknown ones |
| 1.7 | **Per-node aggregation grain** — let a node declare its natural grain (daily flows, weekly/monthly cohort rates); resample each node to its grain before fit/attribution instead of forcing daily. Extends 1.3's `kind`/grain metadata; pairs with a two-level attribution view (window-aggregate identity as headline, per-day covariance as drill-down). Spec below; design + research in [`grain_design.md`](grain_design.md) / [`grain_research.md`](grain_research.md). Shipped in full: `grain`/`kind` schema, native-grain providers, per-grain storage, grain-aware fit/RCA/simulate with window snapping, cohort-aligned lagged identities (`formula` + `lags`), and the two-level attribution view (headline means-bridge with explicit co-movement row as the RCA default; per-parent detailed split as drill-down) | ✅ | Ratio/cohort nodes are degenerate at daily grain (ARPU on a 1-member day; conversion on a low-volume day), producing noise the bootstrap then papers over. Motivated by the New-MRR sub-tree build — see [`authoring_deterministic_decompositions.md`](authoring_deterministic_decompositions.md) |
| 1.8 | **Covariance-asymmetry test + fix** — per-day Shapley drops reference-window covariance from the reconstruction baseline, so `unexplained` absorbs `−cov_ref` for co-moving multiplicative factors even on exact identities. Add a characterization test, then symmetrize the baseline. Spec below. Shipped as a three-part symmetric decomposition (`φ = φ_means + φ_cov_an − φ_cov_ref`, the roadmap's option (b)): both windows evaluated per-day, `unexplained` = measurement residual only, per-parent parts exposed as `decomposition` in `GET /shapley` | ✅ | Attributions on multiplicative nodes with correlated factors under-explain by exactly the factors' reference covariance — violates "never ship a number the engine can't defend" (principle 3) |

**Exit:** a stakeholder accepts an RCA finding; the same RCA re-runs deterministically from a fresh clone; report export exists.

### Specs for 1.7–1.8

Both came out of building a deterministic `new_mrr = new_members × new_member_arpu → new_members = non_trial_conversions + conversions_from_trial → conversions_from_trial = trial_starts × trial_to_member_conversion_rate` sub-tree against the `warehouse` provider (the [authoring lessons](authoring_deterministic_decompositions.md) doc is the companion).

#### 1.7 — Per-node aggregation grain

**Problem.** Grain is hardcoded daily: `data_fetch.py` reindexes every series onto a daily spine (`reindex(full, fill_value=0.0)`) and `MockDataFetcher` notes "Only daily grain is supported." Two consequences:

- **Ratio/cohort nodes are degenerate at daily grain.** A per-day rate factor (ARPU = daily_mrr/daily_members, conversion = daily_converts/daily_starts) swings wildly on low-volume days; the *product* survives (the count factor is also small) but the per-day *attribution to volume vs rate* is high-variance, which is why the block bootstrap is load-bearing rather than a nicety.
- **Deterministic lagged identities are disallowed.** The parser rejects `formula` + `lags` ("a formula is contemporaneous"). But `conversions[t] = trial_starts[t−k] × cohort_rate` is an exact, Shapley-decomposable identity; forbidding it forces either a contemporaneous *blended* (cross-cohort) rate or a fully probabilistic BSTS edge, with no exact deterministic middle.

**Design sketch.**

- Add an optional per-node `grain: day|week|month` (or fold into 1.3's node metadata). Each `BaseDataFetcher` resamples its series to the node's grain (aggregate-up only; downward disaggregation is undefined and should be rejected). Counts/flows sum; declared rates recompute from their components at the coarser grain, not by averaging daily ratios.
- For a formula node, all parents must share — or be resampled to — the node's grain so the identity holds *at that grain*. RCA aligns nodes in scope to a common comparison grain (coarsest in scope, or explicit) before the Shapley/posterior step.
- **Two-level attribution.** Make the window-aggregate identity the default headline (means/totals with an *explicit* interaction term — the standard price/volume/mix bridge), and keep the current per-day within-window covariance capture as an opt-in drill-down. Today that covariance detail is baked into the headline whether or not the reader wants it (and see 1.8).
- Optionally relax `formula` + `lags` to allow **cohort-aligned deterministic identities** (lagged products), so trial→member conversion has an exact deterministic form.

**Open questions.** Seasonality periods are grain-relative (`period: 7` is weekly on a daily series, meaningless monthly) — validate period against grain. Interacts with the snapshot store (2.4), which already keys on `(metric, window, grain)`. Mixed-grain trees (daily flow under a monthly rate) need a clear resample-up contract.

#### 1.8 — Covariance-asymmetry test + fix

**Diagnosis (in `engine/rca.py`).** `shapley_attribution` builds the reconstruction baseline from reference-window **means**:

- `ref_means[p] = window_mean(frame, p, ref)` (scalar per parent); `baseline = _eval_scalar(formula, ref_means)`.
- `daily_actuals[p] = _window_values(frame, p, an)` (daily); `actual = eval_formula(formula, daily_actuals).mean()`.
- `compute_shapley` is fed `{p: full(n_days, ref_means[p])}` as baselines, so `Σφ = actual − baseline`. The bootstrap path (`boot_baselines = ref_vals[p][ref_idx].mean(axis=1)`) shares the same means-only baseline, so CIs inherit the asymmetry.

`run_rca` then takes the node's **own** series gap and subtracts the formula gap: `unexplained = gap − sh["gap"]`, with `gap = window_mean(node, an) − window_mean(node, ref)`.

For an exact per-day product node `target_d = B_d·C_d` (population covariance `cov = mean(B·C) − mean(B)·mean(C)`, i.e. `ddof=0`):

```
gap        = (B̄_an·C̄_an + cov_an) − (B̄_ref·C̄_ref + cov_ref)      # node's own series, both windows carry cov
sh["gap"]  = (B̄_an·C̄_an + cov_an) − (B̄_ref·C̄_ref)                 # baseline uses means only → cov_ref dropped
unexplained = gap − sh["gap"] = −cov_ref(B, C)
```

So a mathematically exact identity yields `unexplained = −cov_ref` and attributions summing to `sh["gap"]`, which overshoots the true gap by `cov_ref`. The intended feature — attributing *analysis*-window covariance to the parents — is fine; the bug is that the *reference* window is collapsed to means, breaking efficiency against the node's own gap.

**Characterization test** (calibration suite / new `tests/test_shapley_covariance_asymmetry.py`; no provider — build the frame in memory):

1. 60 daily rows: reference = first 30, analysis = last 30. Fix means `μB, μC` (e.g. 10, 5). Construct each window as `B = μB + s·d`, `C = μC + s·d` where `d` is a fixed **zero-mean** pattern (e.g. `[+1,−1]*15`), so adding `s·d` leaves the window mean exactly `μ` while giving `cov = s²·var(d)`, tunable via `s` independently per window. Set `A = B * C` elementwise (clean identity — no grain leakage confound).
2. DAG: leaves `B`, `C`; formula node `A = "B * C"`, `parents: [B, C]`. `data` = DataFrame(date, A, B, C); `traces = {}`. Build via the existing test DAG helper (small YAML through `Parser`, or `MetricDefinition` objects directly — follow current test conventions).
3. Assertions against **current** behavior:
   - **Efficiency vs formula gap:** call `shapley_attribution(...)` directly; assert `abs(sum(φ_p.mean() for p) − sh["gap"]) < 1e−9`.
   - **Leak equals reference covariance:** `cov_ref = np.mean(B_ref*C_ref) − np.mean(B_ref)*np.mean(C_ref)`; from `run_rca`'s node-A output assert `abs(unexplained − (−cov_ref)) < 1e−6`.
   - **Zero-gap-nonzero-attribution:** set `cov_an == cov_ref == K ≠ 0`, means equal. Assert `abs(node_gap) < 1e−9` while `abs(sum(attributions) − K) < 1e−3` and `abs(unexplained + K) < 1e−3` — the node didn't move yet each factor gets a nonzero attribution, cancelled by an equal-and-opposite `unexplained`.
   - **Linearity sweep:** for `K_ref ∈ {0, 0.5, 1, 2, 4}` (means and `cov_an` fixed), assert `unexplained ≈ −K_ref` (fit slope ≈ −1, intercept ≈ 0 within tol).

**Fix + acceptance.** Symmetrize the baseline so the reference window is treated per-day like the analysis window — e.g. `baseline = eval_formula(formula, {p: _window_values(frame, p, ref)}).mean()`, and give `compute_shapley`'s non-members their reference **daily** values rather than the scalar mean. Windows of unequal length have no positional pairing, so either (a) require equal-length windows for exact per-day pairing, or (b) reframe the game to attribute the covariance **delta** `(cov_an − cov_ref)` and route only genuine model error to `unexplained`. Preserve the analysis-window covariance-capture feature; keep determinism (`rng` seed 0) and the bootstrap CI structure. After the fix, flip the tests to the target behavior:

- means equal & `cov_an == cov_ref`: `sum(attributions) ≈ 0` and `unexplained ≈ 0` (nothing moved, nothing attributed);
- means move, cov equal: attributions recover the mean-driven contributions, `unexplained ≈ 0`;
- cov moves, means equal: `sum(attributions) ≈ cov_an − cov_ref` (the honest covariance-shift signal), `unexplained ≈ 0`.

---

## Horizon 2 — Make it repeatable: a stranger can onboard

Goal: onboarding a new tree costs a day, not a week.

| # | Item | Status | Why |
|---|------|--------|-----|
| 2.1 | **Connection doctor** for dbt Cloud SL — walk the auth chain (token → host cell → environment → SL config → credential → mapping), name the missing link, emit a copy-paste remediation page for the admin steps outside our control. Shipped as `breakdown doctor --tree …`, broader than scoped: covers warehouse (auth mode → CLI → profile → connection → per-metric SQL via the real `fetch_metric`), cloud (one `client.metrics()` call proves the whole SL chain), and local; all checks run in one pass with copy-paste remediation. Landed with the deployment kit (installable CLI, Docker image, degraded startup + `/health`) | ✅ | Turns days of provisioning archaeology into minutes; is itself the onboarding demo |
| 2.2 | **CSV ingest + per-metric provider mixing** — a tree where some nodes come from the SL and some from direct SQL/CSV is a normal migration state (`source:` already carries a provider-qualified path) | ◑ | direct-SQL/warehouse provider exists; CSV ingest and per-metric mixing remain. The zero-integration on-ramp: "send a CSV, get an RCA," then migrate node-by-node to governed metrics |
| 2.3 | **Tree scaffolder** — enumerate SL metrics; turn `derived`/`ratio` `input_metrics` into formula edges; LLM-assisted import of latent trees (canvas exports, metric docs → draft YAML) | ○ | Blank-YAML is the adoption killer; trees already exist in fragments |
| 2.4 | **Snapshot store** (parquet/DuckDB) — fetch once per (metric, window, grain), refit from snapshots. Shipped as a parquet read-through cache at the fetcher boundary (`snapshots.py`): tree-adjacent `.breakdown/snapshots/` (committable → RCAs re-run from a fresh clone), `--refresh`/`--no-snapshots`/`--snapshot-dir` controls, failure-soft on read-only mounts, and a warehouse outage is survivable when every metric has a snapshot. DuckDB deferred until scheduling (3.1) needs cross-snapshot queries | ✅ | Reproducibility, provider-migration invisibility, warehouse politeness, and the foundation for scheduling |
| 2.5 | **MCP server** — expose `run_rca`, `get_tree`, `explain_metric` as tools | ✅ | AI analysts guess at "why"; breakdown is the grounded causal tool they should call. Cheap (endpoints exist), differentiating, and meets users where they already ask why-questions. Shipped as streamable HTTP at `/mcp` with a fourth tool beyond the original scope (`run_whatif`); analysis responses carry `how_to_read` caveats and `report_url` deep links into the UI |
| 2.6 | **Outsider docs pass** — install guide + first-tree tutorial on public data | ○ | First impressions for anyone arriving cold |
| 2.8 | **Dimensional slicing on the `warehouse` provider** — the SQL contract for a sliced fetch (`fetch_metric_sliced` returning `[date, slice, value]`), `doctor` checks for it, and the docs. Promoted out of [3.2](#horizon-3--make-it-findable-and-sticky-it-comes-to-you)'s "Remaining" because it is not a finishing touch — it is the feature carrying the thesis, missing on the one provider that needs no dbt SL | ○ | Slicing is the bridge from *metric* to *event*: "AOV fell" is a narrowing, "AOV fell, concentrated in EMEA on iOS 18.4" is a diagnosis — and the diagnosis is what the customer was going to spend the next three hours on. `tree × slice` is the biggest pure-product differentiator (3.2), and it currently works on `mock` and dbt SL only, while 3.3 exists precisely as a hedge against thin dbt-SL adoption. That gap is worth more than the rest of this horizon |

*(2.7 — hybrid cold-start→fitted mode — was removed 2026-08-05; see
[Deliberately not on the roadmap](#deliberately-not-on-the-roadmap). IDs are not
reused.)*

**Exit:** a new tree onboards in < 1 day; an external tool runs an RCA against a demo tree via MCP.

---

## Horizon 3 — Make it findable and sticky: it comes to you

Gate: real, recurring usage asking for these — **except 3.1**, whose gate was
removed (2026-08-05) as circular. "Wait for recurring usage" cannot gate the item
whose entire purpose is to *create* recurring usage. A tree exercised only during
incidents is an insurance artifact, and insurance artifacts rot: nobody owns the
tree, no test fails when a dbt model is renamed or re-grained, and the decay is
invisible until the week someone needs it. Declarative semantic assets survive
when they sit on the critical path of something a person reads on a schedule.
3.1 is what puts the tree there.

| # | Item | Status | Why |
|---|------|--------|-----|
| 3.1 | **Scheduled evaluation + anomaly flagging + digest** — "revenue moved; order_count explains ~80%, CI attached — link to full RCA" | ○ | Converts breakdown from pull to push; the retention feature and the seed of the ops layer. **Ungated** (see above): a tree that produces the Monday business review gets fixed when it breaks, and nothing else on this roadmap creates that obligation |
| 3.2 | **Dimensional slicing inside the tree** — attribute a node's gap across a declared dimension (geo, plan, channel), warehouse-side | ◑ | Completes the traverse + slice workflow. Flat slicing is commoditized; *tree × slice* is not — the biggest pure-product differentiator. v1 shipped per [`dimensional_slicing_design.md`](dimensional_slicing_design.md): `dimensions:` schema, exact sum/Bennet slice attribution with excess ranking, mock + SL providers, `POST /rca/{name}/slices`, MCP `slice_metric`. **UI** and **sliced snapshots** landed with the White Cube demo (3.7): a slice panel per ranked cause, lag-correct windows, and a concentration threshold so the panel says "not localized" rather than naming whatever sorted first. Remaining: automated tree×slice. **The warehouse SQL contract and its doctor checks moved to [2.8](#horizon-2--make-it-repeatable-a-stranger-can-onboard)** — they gate onboarding, not stickiness |
| 3.3 | **Native metric-view connectors** (e.g. Databricks metric views) | ○ | Hedge against thin dbt-SL adoption; slots in as another `BaseDataFetcher` + scaffolder |
| 3.4 | **Counterfactual RCA** (T11: posterior-predictive forecast) — "the drop was X units below what the normal regime predicts (95% CI …)" | ○ | Upgrades the flat-trend approximation; strong headline number. Distinct from the existing steady-state what-if |
| 3.5 | **Hosted mode** — auth, scheduled refresh, fit queue + warm cache | ○ | The operational product layer; PyMC fits are CPU-heavy, so a queue + cache is required |
| 3.6 | **Domain template packs** — worked example trees + methodology for specific domains (e.g. emissions/impact driver-tree decomposition) | ○ | Content that doubles as onboarding examples and demonstrates breadth |
| 3.7 | **Deployable demo instance** — a hosted Breakdown over synthetic B2C SaaS data ("White Cube") with planted, ground-truth-labeled anomalies, per [`white_cube_demo_plan.md`](white_cube_demo_plan.md) | ◑ | The pitch artifact: a link a prospect can actually use. Pulls three engine items forward as a side effect — the slicing **UI** and **sliced snapshots** (both 3.2 "Remaining"), and a bearer-token gate on `/mcp` (a down payment on 3.5) |

---

## Statistical rigor (S) — a standing workstream

Not a horizon: a parallel, priority-ordered track that runs alongside them. The
items come from §4 of the [statistics white paper](statistics_whitepaper.md),
which holds the *statistical rationale* for each — why the gap matters, what the
literature says, what "fixed" would mean. **This table is the source of truth
for status and sequencing**; the white paper cites these IDs and does not
duplicate the schedule.

**Sequencing decision (2026-08-05):** this track runs **immediately after the
0.1.0 release**, ahead of the adoption items (2.2, 2.6). The reasoning is that
the cheapest moment to change what a credible interval *means* is while the user
base is still small — once people have RCAs in circulation, widening intervals
means telling them their past analyses were overconfident. It does not block the
release itself: 0.1.0's limitations are documented rather than hidden, which is
the bar principle 3 sets.

**Amended (2026-08-05, same day):**
[**Horizon 0**](#horizon-0--correctness-numbers-the-engine-cant-defend) runs
**ahead of this track**. The distinction is the one a reader most needs: an S
item is a *known limitation that is disclosed*, which principle 3 permits; a C
item is behavior the docs describe wrongly or a number the engine cannot defend
at all, which it does not. Fix the second class before improving the first. Two
C items have S counterparts and the split is deliberate — C4 fixes the block-cap
*bug*, S6 estimates the block *length*; C7 bounds cold-start draws, S7 correlates
the beliefs behind them.

| # | Item | Status | Why |
|---|------|--------|-----|
| S1 | **Benchmark full-rank ADVI** as the RCA default — `pm.fit(method="fullrank_advi")` fits a full covariance matrix instead of a diagonal one. Measure fit time and interval width against both mean-field ADVI and NUTS on the calibration suite and the White Cube tree; adopt if the cost is acceptable | ○ | **First up.** The cheapest possible attack on the engine's #1 statistical weakness — a config change plus a benchmark, no new machinery. Mean-field cannot represent the β-vs-trend posterior ridge (see [`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md)); full-rank *can*. If it is fast enough to default to, it may make S2 unnecessary — which is exactly why it should be measured before S2 is built |
| S4 | **Parent collinearity diagnostic** — pairwise correlation (or VIF) among a node's parent regressors over the fit window; warn when the split of credit is unstable | ○ | **Promoted (2026-08-05)** from below S3 to here. It sits on the shortest path to a wrong decision: correlated parents produce a well-determined *sum* and an unstable *split*, and the split is exactly what RCA reports — while mean-field ADVI reports a *narrow* interval around whatever arbitrary split it landed on. Nothing warns, and no test covers it (see S17). The reference tree contains the structure itself: `true_trial_conversion_rate` regresses on parents including `trial_to_pql_rate` while `product_qualified_leads` is *defined* as `true_trials × trial_to_pql_rate`. Cheap, self-contained, composes with the existing `sign_warnings` channel |
| S2 | **A real ADVI approximation diagnostic** — PSIS-based k̂ (Yao et al., 2018) reported per fit; where k̂ is poor, auto-escalate that node to NUTS or mark its intervals unreliable in the response | ○ | Today's `fit_quality` for ADVI checks only that the ELBO stopped moving, so a well-converged *bad* approximation passes as `"ok"`. This detects the failure instead of assuming it away, without paying NUTS cost on every node. Scope depends on S1's result |
| S3 | **Posterior predictive checks on every fit** — simulate replicated series from the posterior, compare summary statistics against the observed series, flag nodes whose data sits in the tail; surface through the existing `fit_quality` channel | ○ | The single most informative Bayesian model check there is (Gelman et al., 2020) and the one the engine most conspicuously lacks. Needs no new modeling. A badly misspecified node currently passes silently as long as it converges |
| S5 | **Simulation-based calibration** (Talts et al., 2018) — draw parameters from the prior, simulate, refit, check that the rank of the true parameter within the posterior is uniform | ○ | The definitive test that inference is calibrated. Turns 1.2's single-scenario coverage test into a real guarantee. Expensive: a release-gate or nightly job, not per-commit |
| S6 | **Data-driven bootstrap block length** (Politis & White, 2004), replacing the fixed per-grain constants in `BOOT_BLOCK` | ○ | Block length currently is not estimated from the data at all. Too short understates uncertainty, too long overstates it — and the current values are reasonable guesses, not measurements |
| S7 | **Correlated cold-start beliefs** — let authors declare correlations between priors (or a joint distribution over a small set of beliefs) | ○ | The largest modeling gap in cold start: beliefs are sampled independently today, so "if price lands high, conversion lands low" is unrepresentable and intervals are wrong in either direction wherever beliefs genuinely co-vary. Already disclosed in every cold-start response |
| S8 | **Local linear trend as an opt-in** — a trend with a slope component, chosen per node in the YAML; local level stays the default | ○ | A node with genuine momentum is currently modeled as a level that happened to move, which pushes momentum onto the parents. Keep the tight-prior default (it does deliberate work) and give the exception an escape hatch |
| S9 | **Narrow nonlinear edges** — a declared transform on a specific edge (`response: log` on ad spend → conversions), not a modeling language | ○ | Covers the most common nonlinearity (diminishing returns) without opening the door to arbitrary model complexity. MVP-first: one named transform |
| S10 | **Posterior predictive plot in the UI** — observed vs replicated series per node | ○ | The most persuasive single visual a Bayesian tool can offer, and nearly free once S3 computes it. *Blocked on S3* |
| S11 | **Prior-vs-posterior visualization** per coefficient — "you believed 0.1 ± 0.02; the data says 0.08 ± 0.01" | ○ | Makes the Bayesian update concrete and teaches the model while it informs. Directly serves the reader/reviewer persona 1.4 targets |
| S12 | **Make `ranked_causes` visibly a heuristic in the UI** — distinguish "ranked by triage score" from "ranked by evidence", or attach the underlying interval so a wide-interval cause cannot outrank a tight one on a point estimate | ○ | It is documented as triage and rendered as the most prominent number in the UI. Prominence implies rigor whatever the docs say |
| S13 | **Methods appendix in the exported report** — a linkable expansion of the existing methods footnote stating fit window, inference method, diagnostics, and the caveats that applied to *that* analysis | ○ | Makes an exported RCA self-defending when it circulates without its author — the whole point of 1.5's export |
| S14 | **Quantify the DAG assumption** — a sensitivity statement: "if an unmodeled confounder explained X% of this parent's movement, the attribution would change by Y" | ○ | Puts a number on the assumption everything rests on. Highest ceiling and least defined item here; adapting unmeasured-confounding sensitivity analysis to metric trees is research-flavored. Note this is *not* causal discovery, which stays off the roadmap |
| S15 | **Multiplicity and selection-aware reporting** — disclose first (`how_to_read`, [`docs/model.md`](../docs/model.md), the UI), then evaluate whether the reported interval on the *selected* top cause can be made selection-aware | ○ | A single `run_rca` on a 15-node tree emits 25–30 intervals plus a `prob_same_direction` each, sorts by effect size, and presents the top one — whose `ci_95` was computed **pre-selection**. Under a global null ~1.5 of those intervals exclude zero by construction, and the window pair is a free user choice with no cost to retrying. The README's own essay makes this argument against unconstrained agent search; we have a bounded version of the same problem and disclose it nowhere. *Considered and not chosen as the fix: hierarchical pooling of `beta_raw` across a node's parents. Parents are heterogeneous quantities in different units — pooling them toward a common mean has no substantive justification, and the independent `Normal(0,1)` on the **rescaled** coefficient is deliberate. Disclosure is the MVP; pooling stays an option to argue for, not a plan* |
| S16 | **Forward-simulation variance in the trend interval** — `rca.py:622` computes `trend_delta` from `trend_samples[:, -1]`; `t_an` is computed three lines earlier and feeds only the seasonal term, so a one-day analysis window and a ninety-day one starting the same date return the **identical** trend estimate *and* the identical CI. Add the omitted forward-simulation variance so the interval grows with the horizon | ○ | The flat *point* forecast is a deliberate, documented property of a local-level random walk (and S8/3.4 address it). The horizon-invariant *interval* is a distinct and more mechanical defect: the random walk's forward variance accumulates with each step past the fit end and is simply absent. **Measure the magnitude before quoting one** — it scales with `σ_trend`, which this model prioritizes tightly, so the size of the understatement is an open empirical question and the current external estimate is asserted rather than measured. `components.trend` is the number that gets narrated |
| S18 | **A t-shaped tail for short-window intervals** — the percentile bootstrap takes 2.5/97.5 quantiles of the replicate distribution, which behaves like a normal quantile; a window of 5 periods needs a t-shaped one. Measure whether scaling the replicate spread by `t(0.975, df) / z(0.975)` is defensible, and what `df` should be under block dependence (`n − 1` is right only if the periods are independent, which is the assumption the block bootstrap exists to deny) | ○ | **Added by C4 (2026-08-05)**, which measured the residual rather than assuming it away. After C4's variance correction, iid coverage of the nominal 95% interval is ~0.84 at n=3 and ~0.90 at n=7; a t-shaped tail with `df = n − 1` takes both to ~0.95 in the same measurement. That is the largest remaining piece of the short-window gap and the cheapest to close — but it is a modeling choice about interval *shape*, not an exactly-derived bias correction, so it was deliberately kept out of a correctness fix. Composes with S6: the block length determines the effective `df` |
| S17 | **Rebuild the calibration suite's coverage test** — draw truth from the DGP rather than the realized series; add the two cases with **zero** coverage today; vary the seed per world; raise the pass bar | ○ | `_planted_step_world` computes `truth = beta * (x[an].mean() − x[ref].mean())` from the **realized** `x`, and checks it against percentiles of `beta_samples × bootstrap of that same realized x` — so the window-sampling term is pure added width around a point already equal to the truth's x-factor, and the inflation is precisely what would hide a narrow ADVI posterior. Layered on: an 80% bar for a nominal 95% interval has no power to reject true coverage of 0.85, all 20 worlds share one DGP varying only the noise seed, and `random_seed=0` is fixed across them. Two things are consequently untested anywhere — **formula-node CIs**, which come 100% from C4's bootstrap, and **collinear parents** (S4's failure, present in the reference tree). The design of 1.2 is right; this implementation cannot fail. Cheap complement to S5, not a replacement |

**Related, already scheduled elsewhere:** [3.4](#horizon-3--make-it-findable-and-sticky-it-comes-to-you)
(counterfactual RCA via posterior-predictive forecast) is the white paper's
fourth §4.1 item — it upgrades the flat-trend approximation and shares
machinery with S3. It stays in Horizon 3 rather than being duplicated here.

**Exit:** no fixed exit — this track is maintenance of a property, not a
milestone. The nearest thing to a bar: intervals that pass SBC (S5) *and a
coverage test that can fail* (S17), a default inference path whose approximation
error is *measured* rather than assumed (S1/S2), and no weakness in white paper
§3.2 left without either a fix or a disclosed caveat.

---

## Deliberately not on the roadmap

- Real-time / streaming grains.
- Our own metric definition language (we ride dbt's).
- Causal **discovery** — the DAG stays the analyst's hypothesis; that's the premise, not a gap.
- Per-seat anything.
- Dark mode (for now).
- **Hybrid cold-start → fitted mode** (was 2.7; removed 2026-08-05). Per-node
  graduation — a node with ≥ `MIN_FIT_PERIODS` whole periods uses its posterior,
  every other node falls back to prior draws, each response saying which basis it
  used. The idea is genuinely appealing (watching intervals shrink node by node
  *is* the early learning of a company, made visible) and it is removed anyway,
  for two reasons. It needs a policy for partially-fitted paths that the roadmap
  itself admitted was "worth its own small spec" — real design cost. And it
  extends cold start, now a demo mode rather than a supported persona: it would
  spend engine surface deepening the one mode whose users have no incident to
  explain. `MIN_FIT_PERIODS = 10` on a model carrying one latent trend state per
  observation is ≥ 14 latent parameters on 10 data points, so the cliff it was
  meant to soften is partly a cliff we should not be encouraging anyone over.

---

*Ticket-level detail and the rationale behind shipped work live in
[`archive/`](archive/) (statistical review T1–T12, UI plan U1–U6, connectivity
analysis). This roadmap absorbs their open items.*

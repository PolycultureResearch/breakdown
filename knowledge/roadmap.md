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

Rows are one line each: ID, status, a one-sentence statement, and a link into
[`roadmap_log.md`](roadmap_log.md) where the row had accumulated an account
(compressed 2026-08-31, per the third grill: 31,000 words and one 9,321-char
line had stopped being a roadmap). Statistical measurements are canonical in
the [white paper](statistics_whitepaper.md); a closing row states its outcome
in a sentence and puts the account in the log.

---

## Product principles

1. **Build against real need.** Prefer the item a concrete use case is blocked on
   over the speculatively useful one. The roadmap is a priority order, not a promise.
2. **The engine is the core, and it is developed in the open.** The whole
   engine is source-available under [FSL-1.1-ALv2](../LICENSE). Read it, run
   it, self-host it, fork it, build on it, commercially or otherwise. The one
   thing the license excludes is selling breakdown as a competing product, and
   every release automatically becomes Apache-2.0 open source two years after
   it ships. That conversion is an irrevocable grant written into the license
   itself, not a promise. The license points at competitors, not at users.
   Operational surfaces (scheduling, alerting, hosting, multi-user) are a
   separate layer built on top, not woven into the engine.
3. **Never ship a number the engine can't defend.** Credible intervals, a
   first-class `unexplained` term, and suspect-fit flags are the brand. Every new
   surface inherits this honesty posture — no bare point estimates.
4. **Optimize time-to-first-trusted-RCA.** Breakdown should be easy to set up and get value from. The product's north-star metric: elapsed
   time from "here are my credentials" to "breakdown correctly explained an incident
   I already understood." 
5. **Own the last mile; depend on standards, not vendors.** breakdown reads other
   people's semantic definitions, and that is a strength — but a *runtime*
   dependency on a vendor's Python package inherits their release cadence, their
   support window and their Python ceiling. Prefer reading a versioned artifact
   over importing the library that writes it, and keep that library as a
   development-time oracle so drift fails a test rather than a user. The line is
   at *parsing a resolved artifact* (fine) versus *reimplementing a parser* —
   refs, inheritance, Jinja (not fine, and why Sidemantic was rejected). The
   `dbt-bridge` extra depends on nothing from dbt Labs
   ([2.15](#horizon-2--make-it-repeatable-a-stranger-can-onboard)); the engine's
   own definition language stays ours.

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
> fixes its unbounded baseline draws and
> [S7](#statistical-rigor-s--a-standing-workstream) stays scheduled — but it
> stops earning new roadmap surface. The persona it was built for (a pre-revenue
> founder) has **no incident to explain**, which is the product's actual moment
> of value, and what it produces is sensitivity analysis on stated assumptions —
> a category with incumbents. The follow-on hybrid mode was removed; see
> [Deliberately not on the roadmap](#deliberately-not-on-the-roadmap).

**Providers.** `mock`, `dbt` (a dbt project's own semantic manifest — no dbt
Cloud, no `mf`, and **no runtime dependency on dbt Labs**), `local` (MetricFlow
CLI, superseded per tree by 2.13), `cloud` (dbt Cloud Semantic Layer),
`warehouse` (direct SQL). Provider config supports `${ENV}` secret references.
Per-node `bind:` blocks (2.9) let a tree mix sources, or use none at all.

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

**Not yet built** (the roadmap below) — and one thing newly finished: **the
Horizon 0 correctness gate closed on 2026-08-17, every row then in it ✅.** C4, C5 and C6
closed on 2026-08-13 (and C19–C22 the same day, from a reviewer reading the
output); the [milestone-readiness audit](milestone_readiness_2026_08_17.md)
added C23–C25 and closed them the same day, along with C12 and 2.20; C9, C13
and finally C7 (the author's option-1 call on the ratio-centre decision)
closed later that day. The punch list with an end has ended; per the
sequencing decision below, the [S track](#statistical-rigor-s--a-standing-workstream)
started after the 0.1.0 release with S1, which closed 2026-08-18 (benchmarked;
full-rank not adopted; S2 promoted to next), and S2, which closed 2026-08-24
(PSIS k̂ on every variational fit — and, because it fails on essentially every
real node, **NUTS as the default sampler everywhere** with ADVI as an explicit
opt-in). **Two C rows were filed after that close, and both came from running the
engine rather than reading it:** C26 (`non_physical` is one-sided, so a rate can
simulate above 1.0 unflagged) surfaced from a what-if during the 3.7 re-pin, and
C27 (`/analyze` and the analysis routes disagree on NUTS `tune`) from S2 putting
every route on the same sampler. **Both are closed** — C27 and C26 both on
2026-08-27 — so the table is all-✅ again. C26 is worth reading rather than
counting: the row's own scope named the wrong field, and the implementation
found out by checking it against the shipped trees rather than against the row.

Still outstanding: remaining
statistical rigor (T11), UI trust finish (U5–U6), an outsider docs pass before
publication (2.6), and the market-driven items (scheduled monitoring, the rest
of dimensional slicing).

---

## Horizon 0 — Correctness: numbers the engine can't defend

Gate: **blocks everything below.** Principle 3 is not satisfied while these ship.

Every item here is a confirmed defect that produces a *plausible wrong number*
rather than an error — the same failure class Horizon 1.1 was built to close,
found in the places 1.1 didn't look: the provider boundary, the published
statistic, and the agent-facing payload. Most came out of a hostile external
review of the engine, docs, and tests conducted **2026-08-05** against 0.1.0;
**C15 and C16 came out of a second review on 2026-08-12** against `c18d150`,
scoped to the first client deployment and PyPI publication; **C19–C22 came out
of a reviewer running the demo trees and reading the output (2026-08-13,
PR #66)**; and **C23–C25 came out of the
[milestone-readiness audit](milestone_readiness_2026_08_17.md) (2026-08-17)**
against `212b53c`, which traced four recently-decided policies from decision to
code to tests; and **C28 came from CI itself (2026-08-24)** — a dependency
released that morning changed shipped behavior with nothing in this repo
changing, and the two jobs that resolve dependencies fresh rather than from
`uv.lock` are what noticed; and **C29–C44 came out of a third hostile review
(2026-08-29)** against `6531a02` —
[grill_2026_08_29.md](grill_2026_08_29.md), three reviewers sweeping the whole
repo, whose headline is that the meta-defect the four rules were written down
for is back unchanged: seven of its nine top findings are a policy chosen
carefully in one file and not propagated to its neighbour, and two are fixes
from a previous grill that stopped one function short. Its structural and
hygiene findings (M8/M9's module extractions, the disclosure-layer split, the
L-list, and this file's own compression) are being executed alongside the
C-rows and are deliberately not numbered — they change no published number.
Verification against the working tree (2026-08-30) confirmed every High and
Medium finding, with two corrections recorded in the affected rows below (C35,
C41). Each was traced to a specific code path, and the
`file:line` references are the starting point for whoever picks the item up.
*(**C27 landed after C28 and the sequence is intact.** C28 was numbered around
a C27 that was still on an in-flight branch, on the reasoning that renumbering
a row someone already cites is worse than a temporary gap; that branch has
since merged, so both rows exist and the gap closed on its own.)* *(Line refs
re-anchored 2026-08-17 — the 2.16 multi-tree refactor had moved `api/main.py`
by ~1,100 lines and every open row's reference with it.)*

This is not a standing workstream. It is a punch list with an end.

| # | Item | Status | Why |
|---|------|--------|-----|
| C1 | **tz-aware dates must fail loudly, not silently zero** — `pd.to_datetime` on a `TIMESTAMP` column preserves tzinfo; `floor_period` calls `.normalize()`, which preserves it too, so the alignment guard at `data_fetch.py:414` passes. *(full history: [C1](roadmap_log.md#c1))* | ✅ | **Ship first.** Any SQL returning `TIMESTAMP` rather than `DATE` (Databricks attaches UTC) turns a whole metric into zeros. |
| C2 | **Snap `cloud` and `local` to the period spine** — `CloudDataFetcher.fetch_metric` (`data_fetch.py:176`) and `LocalDataFetcher.fetch_metric` (`:261`) floor the labels and return; only `warehouse` and `mock` call `period_spine` and drop partial edge periods. *(full history: [C2](roadmap_log.md#c2))* | ✅ | A window ending Tuesday on a `grain: week` metric returns a two-day partial week as a full row at ~2/7 normal volume, and `snap_window` treats it as whole — a manufactured −71% gap. |
| C3 | **Publish the exact Shapley value as `estimate`** — `rca.py:553` reports `float(phi_b.mean())`, the bootstrap mean of a *nonlinear* function of jointly-resampled window means, while `unexplained` (`:586`) comes from the separate **exact** call. *(full history: [C3](roadmap_log.md#c3))* | ✅ | Joint resampling induces `Cov(x̄,ȳ) ≠ 0` and the means-bridge is bilinear, so `E[φ_boot] ≠ φ_exact` — contributions don't reconcile with `gap`, contrary to [`docs/model.md`](../docs/model.md). |
| C4 | **Bootstrap degeneracy guard and short-window attenuation** — two parts. *(full history: [C4](roadmap_log.md#c4))* | ✅ | Formula-node CIs come **entirely** from this path with no offsetting term, and [`docs/model.md`](../docs/model.md) sells the bootstrap as the honesty mechanism for exactly the short "what happened this weekend?" windows where the attenuation is worst. |
| C5 | **`ranked_causes` inverts on near-zero gaps** — `rca.py:751` uses `min(abs(share), 1.0)`, and `share_of_gap` is `None` only when `abs(gap) < 1e-12` (absolute, not relative to node scale). *(full history: [C5](roadmap_log.md#c5))* | ✅ | The metric that most conclusively did *not* move hands its full influence score to everything above it, and scores accumulate across children — so a well-connected node with several quiet children can top the ranking on pure offsetting noise. |
| C6 | **Reject duplicate metric names** — `parser.py:479` adds nodes then edges in two passes with no uniqueness check, and `MetricTreeConfig` (`:466`) has no validator. *(full history: [C6](roadmap_log.md#c6))* | ✅ | `list(dag.predecessors(name))` is the axis order of `beta_raw` — the load-bearing invariant AGENTS.md names. |
| C7 | **Bound cold-start baseline draws** — `engine/simulate.py` computed `sigma = (high − low)/(2·z90)` and called `rng.normal(mu, sigma, n_draws)` unconditionally; declared `plausible` bounds were loaded but consulted only *post hoc* for extrapolation flags, never at sampling time. *(full history: [C7](roadmap_log.md#c7))* | ✅ | On the shipped `cold_start_tree.yml`, `paying_customers` draws ~1% **negative customer counts** and `mrr` inherits them. |
| C8 | **Trace cache, concurrency, and the NaN 500** — `app.state.traces` has no eviction, no TTL and no size bound, and its key `(name, fit_end)` (`api/main.py:504`) ignores inference method and `draws`, so `POST /analyze/{name}?draws=50` poisons the key `run_rca` later reuses. *(full history: [C8](roadmap_log.md#c8))* | ✅ | The reference tree has 107 metrics; one RCA adds ~100 `InferenceData` objects and a second `analysis_start` adds 100 more — the user OOMs their own process. |
| C9 | **The agent payload double-counts the interaction** — `compact_rca` (`mcp/shaping.py:257-262`) drops each contribution's `decomposition` but **keeps** the node-level `interaction`. *(full history: [C9](roadmap_log.md#c9))* | ✅ | An LLM narrating "parents contributed X and Y, plus an interaction of Z" double-counts the entire co-movement term. |
| C10 | **Bring `b2b_mrr_tree.yml` up to the schema it is the reference for** — audited: 107 metrics, 47 formula nodes, **6 probabilistic**, 114 edges of which 100 are deterministic; and **zero** `grain`, `kind`, `dimensions`, `expected_signs`. *(full history: [C10](roadmap_log.md#c10))* | ✅ | It is cited as the worked reference tree, so it is the answer to "how hard is this to author" and the shape a new author copies. |
| C11 | **The mock provider ignores the schema it is generating for** — `MockDataFetcher._tree_data` (`data_fetch.py:648`) draws every root from `rng.uniform(50, 5000)` regardless of `kind`, so a `kind: rate` leaf is generated on the same scale as an impression count. *(full history: [C11](roadmap_log.md#c11))* | ✅ | The mock is the demo, the tutorial, and most of the test suite. |
| C13 | **The mock generates every leaf independently, so a difference identity goes negative** — `MockDataFetcher._tree_data` (`data_fetch.py:1088-1180`): `controllable_attrition = cancel_requests − saved_cancel_requests` on the reference tree draws both leaves as independent random walks with nothing holding `saved ≤ requests`. *(full history: [C13](roadmap_log.md#c13))* | ✅ | Smaller blast radius than C11 — 3 nodes on the reference tree, bounded magnitudes, no compounding — but the same class of defect: the demo shows a number the business it is modelling cannot produce, and a reader cannot tell the fixture's limitation from an engine bug. |
| C12 | **A rate's slicing `weight` must share its grain, but nothing checks until slice time** — `_run_slice` (`api/main.py:1731-1739`) raises when `weight_defn.grain != defn.grain`, which is the first moment anyone finds out. *(full history: [C12](roadmap_log.md#c12))* | ✅ | A tree parses clean, boots clean, serves `/meta` and the whole tree RCA clean, and then fails the first time a user clicks a rate node's **slice by** row. |
| C14 | **The deployed demo served 503 to every first visitor** — three compounding causes, found by timing the public instance. *(full history: [C14](roadmap_log.md#c14))* | ✅ | The demo URL ships in the v0.1.0 release notes and the PyPI sidebar — the two places a stranger's first click lands — so this was the product's first impression failing closed. |
| C15 | **A dbt metric's `filter` is silently dropped — the node serves the unfiltered measure** — `dbt_manifest.py:130`. *(full history: [C15](roadmap_log.md#c15))* | ✅ | **Inverts the product's core promise.** The pitch is that breakdown computes over the semantic layer the client already governs; serving a different number *under the governed metric's name* is the one failure that cannot be argued down as a modelling caveat. |
| C16 | **Editing a metric's `sql:`/`bind:` doesn't invalidate its snapshot — and provenance then attests the new SQL for the old numbers** — `snapshots.py:39`. *(full history: [C16](roadmap_log.md#c16))* | ✅ | **The provenance inversion is worse than the staleness.** `SnapshotFetcher` delegates `query_provenance` straight through (`snapshots.py:163-173`), so the UI's *show query* panel displays the refund-excluding SQL beside the refund-including number — … |
| C17 | **A zero denominator in a formula is an unhandled 500 — and a NaN the agent payload turns into `null`** — `rca.py`. *(full history: [C17](roadmap_log.md#c17))* | ✅ | The trigger is not exotic: `formula: "revenue / order_count"` is the README's own documented shape, `_align_to_spine` *manufactures* the zero by design for a `kind: flow` denominator, and a seasonal business produces off-season zeros naturally. |
| C18 | **A metric that started mid-window is fabricated as zeros, silently** — `data_fetch.py`. *(full history: [C18](roadmap_log.md#c18))* | ✅ | The **fifth** silent-wrong-number defect at the provider boundary, and it was sitting inside the shared contract that [C1](#horizon-0--correctness-numbers-the-engine-cant-defend)/[C2](#horizon-0--correctness-numbers-the-engine-cant-defend) built to end exactly this class. |
| C19 | **`prob_same_direction` saturated at 1.0 and rendered as certainty** — the statistic is `max((x>0).mean(), (x<0).mean())` over 500 bootstrap replicates, so its representable values are k/500 and there is nothing between 0.998 and 1; it published the saturated 1.0 and rendered `P(dir) 100.0%` on 21 of the reference tree's 105 nodes — most likely exactly where the evidence is … *(full history: [C19](roadmap_log.md#c19))* | ✅ | C5's "a saturated clamp reads as certainty," on the probability side. |
| C20 | **The export was missing the two facts its reader most needs** — the static report omitted the fit window on the one node flagged *suspect fit* (the live header shows it; the export is where the reader cannot hover), and a contribution carrying `lag`/`parent_windows` was measured over a *different window* than the section header names, silently, on both surfaces — lagged edges being the demo's signature feature. *(full history: [C20](roadmap_log.md#c20))* | ✅ | 1.5's export is "the thing an analysis becomes when it leaves the app" — it circulates without its author, so a fact present only on hover is absent from the artifact that gets acted on |
| C21 | **An absent `direction` became a claim** — the parser defaulted `direction` to `up_is_good` and `/dag` serializes with `model_dump()`, so the default reached the browser indistinguishable from a declaration and `app.js`'s own fallback could never fire: `churn_arpu` rose 18.5% and rendered **green — "improved"** — while carrying 27.3% of the damage on a node down 32.1%. *(full history: [C21](roadmap_log.md#c21))* | ✅ | The honesty posture inverted in the one surface a client looks at, from an honest payload — the fifth-rule class. |
| C22 | **A cleared date field was an unhandled 500** — `pd.Timestamp("")` is `NaT`, which satisfies a `str` annotation, survives every `is None` guard and reaches `snap_window`, where `NaT.normalize()` is an `AttributeError`; `analysis_start=banana` 422'd correctly, which is why the empty string went unnoticed — two routes ran the ISO check inline and their four siblings had not. *(full history: [C22](roadmap_log.md#c22))* | ✅ | The same policy-at-one-call-site shape as everything else in this table, on the input boundary rather than the output one |
| C23 | **The sliced fetch path never got the shared date contract — a tz-aware sliced frame becomes an all-zero panel** — `_sliced_long` (`data_fetch.py:521-551`), used by both `cloud` and `local` sliced fetches, calls `_floor_labels` but never `_to_naive_dates`; the `dbt` provider's sliced path *does* call it (`dbt_provider.py:479`) — the C1 policy propagated to one of three. *(full history: [C23](roadmap_log.md#c23))* | ✅ | The rule-1 invariant only inspects `fetch_metric`, which is how this survived: the sixth silent-wrong-number defect at a date-alignment seam, found by asking where else the C1/C2/C18 contract *doesn't* reach. |
| C24 | **A rate's slice panel can never say "localized" — the concentration verdict is dead for the showcase shape** — the UI verdict gates on `top.baseline_share != null` (`static/app.js:3065-3067`) and the rate-attribution rows (`engine/slices.py:825-836`) never emit `baseline_share`; only the sum path does (`:630`). *(full history: [C24](roadmap_log.md#c24))* | ✅ | "Churn rate fell, concentrated in EMEA" is the product's showcase sentence, and the panel is structurally incapable of saying it. |
| C25 | **`/simulate` is the surface the honesty policies stopped short of** — two halves, one surface. *(full history: [C25](roadmap_log.md#c25))* | ✅ | The third instance of the C17 pattern by the project's own count — `slices.py` sanitized, then `rca.py` learned it, and `simulate.py` never did — and the labelling half is 1.11's policy failing its first propagation test. |
| C26 | **`non_physical` is a one-sided check, and a rate has two impossible sides** — `engine/simulate.py:782` fires the flag only on `simulated < 0 and hist["hist_min"] >= 0`. *(full history: [C26](roadmap_log.md#c26))* | ✅ | The what-if tab's pitch is the sentence the guided tour scripts out loud: *it is telling you the scenario is nonsense rather than returning a confident number.* That sentence was true in one direction and false in the other, and "103% of members were active" is exactly the … |
| C27 | **One node, two warm-ups, depending on the route you asked through** — `POST /analyze/{name}` declares `tune: int = Query(default=500, ...)` while `run_rca` and `run_scenario` take `fit_metric`'s own `tune=1000`, so the same node fitted over the same window returns a posterior drawn after a different warm-up depending on whether the caller came through the single-metric route or an analysis. *(full history: [C27](roadmap_log.md#c27))* | ✅ | The engine is stateless and a fit is supposed to be a pure function of (DAG, data, target). |
| C28 | **Every MCP tool error collapsed to `Error executing tool <name>` the day the SDK hardened** — `mcp` 2.1.0 (PyPI, 2026-08-24T19:04:29) split a tool failure in two: `ToolError` is *anticipated* and its message is handed to the caller as an `isError` result, everything else is a crash whose text stays in the server log (`mcp/server/mcpserver/tools/base.py`: `raise … *(full history: [C28](roadmap_log.md#c28))* | ✅ | Not a wrong number, which is why it is worth recording next to ones that are: the consumer is an AI assistant, and it has no server log to open. |
| C29 | **The posterior attribution branch publishes NaN** — a `kind: rate` parent with a zero-denominator period inside the analysis window but outside the fit window reaches `estimate`, `ci_95` and `prob_same_direction` unfiltered; `[nan, nan]` passes the degeneracy guard (a NaN comparison is False) and Starlette's `allow_nan=False` turns the payload into an unhandled 500. *(full history: [C29](roadmap_log.md#c29))* | ✅ | Rule 3, in the one place no test looks — and someone hardened the consumer (`_hop_weights` defends against a NaN share) without hardening the producer |
| C30 | **C4 and C5 stopped at `rca.py`** — `slices._excess_fields` publishes zero-width `ci_95` (C4's exact defect, in a module that already imports the guarded `_sample_summary` and doesn't call it for this), and every gap test in `slices.py` and `simulate.py` is still the absolute `abs(g) < 1e-12` whose retirement is quoted in `_share_of_gap`'s own docstring — measured shares in … *(full history: [C30](roadmap_log.md#c30))* | ✅ | Two closed C-items were open one module over the whole time; the whitepaper's "no published `ci_95` is zero-width by any route" was false when written, and its 2026-08-30 revision says so |
| C31 | **`GET /metrics/{name}/query` hands out the SQL that `GET /dag` just redacted** — no redaction, no token check, and with `BREAKDOWN_API_TOKEN` set alone the middleware gates only `/mcp`, so the route is fully open; `get_dag`'s docstring even points callers here. *(full history: [C31](roadmap_log.md#c31))* | ✅ | The redaction and the leak are 40 lines apart, and `docs/deploying.md` repeats the promise the route breaks |
| C32 | **The slice and flow caches are bounded by entry count, and the thing that grows is cardinality** — a high-cardinality dimension (830 days × 5,000 slice values) is 154 MB per cached raw frame, ×64 entries against the demo's 2 GB box; the generated `GROUP BY` has no LIMIT and `top_k` is applied only after the whole frame is resident. *(full history: [C32](roadmap_log.md#c32))* | ◐ | Rule 2's own words: bound by the thing that actually grows — the invariant test checked the cache's *type* and never its bound |
| C33 | **`GET /shapley/{name}` runs the O(2ⁿ) engine on the event loop** — no `to_thread`, no lock; a measured 1.09 s stall freezes `/health` (the container healthcheck), every `/progress` poll and every `/ui` asset per uncached, unauthenticated call. **Shipped 2026-08-31**: `async with tree.lock: await asyncio.to_thread(...)`, and the new invariant `test_no_async_route_calls_the_engine_inline` AST-enumerates the property — no async handler in `api/main.py` may call a heavy engine function directly (verified catching the pre-fix route). [Grill H4](grill_2026_08_29.md) | ✅ | `_MAX_SHAPLEY_PARENTS` caps the work; nothing stops it landing on the loop |
| C34 | **`localized: true` derived from evidence the module itself withheld** — the gate is `not top.get("noise_level")`, and `None` (withheld: single-period window, or too few finite replicates) passes it, so the panel prints "carries 97% of the gap" off two data points with every uncertainty field explicitly declined. *(full history: [C34](roadmap_log.md#c34))* | ✅ | The function's docstring promises the verdict is withheld in exactly this case; the code implements neither `None` path |
| C35 | **The surfaces that circulate cannot name the sampler** — the RCA node payload carries no `inference_method` (only MCP publishes it, with a comment on why an agent needs it), so the RCA tab, the node cards and the exported HTML report render an ADVI fit byte-identical to a NUTS one; `ui_design_spec.md` specified the `"posterior · ADVI"` string and it was never wired, and `GET /metrics/{name}` omits the field too. *(full history: [C35](roadmap_log.md#c35))* | ✅ | Fifth rule: S2 made the sampler an explicit choice, and the one fact that makes the choice legible is absent from the artifact that leaves the app without its author |
| C36 | **`applyWhatifOverlay` tints on raw `est !== 0`** — and `null !== 0` is `true`, so a node whose delta the engine explicitly withheld tints as a decline; a 1e-15 delta paints green with a ▲ while the outcome card beside it, routed through `gapDir`, stays colourless. **Shipped 2026-08-31**: overlay tint, card direction and `waterfallHtml` all route through `gapDir` — the withheld-`null` case renders as no claim, and the literal `>= 0 ? "up" : "down"` AGENTS.md names is gone. [Grill M7](grill_2026_08_29.md) | ✅ | The fifth rule's own worked example — `null` compared against zero — reintroduced in the neighbouring function, which is C21's lesson unlearned |
| C37 | **`fit_quality: "suspect"` has drifted into six wordings** — five render sites test `=== "suspect"` with hand-written, mutually inconsistent explanations (ADVI-first in two, NUTS-first in two, no cause at all in one) and no fallback, so a third engine value renders as silence; one site has the fallback chain, which is the tell. *(full history: [C37](roadmap_log.md#c37))* | ✅ | The lookup-table treatment exists precisely so an unknown status renders as its own words rather than nothing — every neighbouring vocabulary got it |
| C38 | **`run_rca` raises `RuntimeError` and every route catches only `ValueError`** — the pre-fit scoping `fit_frame` call is unguarded (the identical call 1,000 lines up wraps both), so an empty grain-join is an unhandled 500 with the diagnostic thrown away; and PyMC's `SamplingError` subclasses `RuntimeError`, so a node whose model fails to initialize propagates out instead of degrading to `fit_failed`. *(full history: [C38](roadmap_log.md#c38))* | ✅ | Contradicts the module's own headline — "one bad node does not end the analysis" |
| C39 | **`run_scenario` refuses a whole scenario over an unrelated node's grain** — the fitted-baseline loop and the honesty stats iterate `dag.nodes`, not the affected subtree, so one disconnected `grain: month` node kills every sub-month scenario in a wide tree. *(full history: [C39](roadmap_log.md#c39))* | ✅ | "A process serves several trees, and they are peers" — one monthly node should not make what-if unusable for everyone else |
| C40 | **Three HTTP sanitizers test `isnan`, their neighbours test `isfinite`** — `math.isnan(inf)` is False, so `±inf` (a DuckDB float division returns it) passes four sites into the strict encoder as a 500; the invariant tests mirror the same asymmetry, asserting `inf` on one helper and only NaN on the other. **Shipped 2026-08-31**: `not math.isfinite(...)` at all four sites, and the strict-encoding invariant now pokes an `inf` into a served series and reads it back as `null`. [Grill M3](grill_2026_08_29.md) | ✅ | Rule 3 half-applied — the sweep that closed C17 checked for the wrong predicate's twin |
| C41 | **The process-wide `TraceStore` is mutated from worker threads with no lock — and a cancelled request leaves an orphan run writing into it** — concurrent RCAs on two trees put two threads in `__setitem__`/`_evict` unserialized (reproduced: `dictionary changed size during iteration`, and `total_bytes` drifting 12 MB *low*, which silently disarms the byte budget rule 2 exists … *(full history: [C41](roadmap_log.md#c41))* | ✅ | The C8 failure class reintroduced on the writer side after being fixed for readers; the free-threaded 3.14 build turns "a few bytecodes" into routine |
| C42 | **`serve --host 0.0.0.0` with no token silently removes the last barrier on `/mcp`** — a non-loopback bind disables Host/DNS-rebinding validation and the bearer gate needs `BREAKDOWN_API_TOKEN`, so the Dockerfile's default CMD leaves the tools that run `run_rca`/`run_whatif` and return the whole tree open to anything that can reach the port, and nothing logs it — while the … *(full history: [C42](roadmap_log.md#c42))* | ✅ | Same file, opposite policy, no stated reason — the four rules' definition of the meta-defect |
| C43 | **`/health` echoes the raw exception through the one route auth deliberately leaves open** — a parse failure leaks the tree's SQL and a provider failure leaks hostnames and usernames into an unauthenticated 200 body. *(full history: [C43](roadmap_log.md#c43))* | ✅ | The healthcheck route is public *because* it must be safe to expose, and it is the least safe body the server can produce |
| C44 | **`_reconciliation` crashes with numpy's words; `_window_aggregates` fabricates a zero** — an all-zero-weight window (mis-specified `weight:`, retired plan tier, filter typo) gets `zero-size array to reduction operation maximum which has no identity` where every other refusal in the module names the metric, dimension and remedy; and a window with no weight publishes `baseline: … *(full history: [C44](roadmap_log.md#c44))* | ✅ | Rule 1 inside the engine — the one substitution the grill found anywhere that is neither logged nor named, in the module C23 taught to log its fills |

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
reconciliation directly. *(C4 shipped 2026-08-13; see its row.)*

**Order for the open remainder (2026-08-17):** C12, C23, C24 and C25 went
first — the ones on the path of a real client's ordinary RCA — and all four
shipped the day they were filed ([milestone report](milestone_readiness_2026_08_17.md) §3);
C9 and C13 followed the same day, and C7 closed the list once the author chose
option 1 on the ratio-centre decision. **The gate is closed.**

**C10 shipped (2026-08-08), and cost two new rows.** It was filed as a
data-authoring item and it was mostly that, but the row predicted it would
"surface further engine gaps while doing it" and it did: C11 and C12 are both
findings from authoring the tree, not from reading the code. Neither could have
been seen without a tree that actually declares `kind`, `dimensions` and
`expected_signs` — which is the argument for keeping the reference tree honest
in the first place. Two things worth recording. The audit's count of 31 rates
was low by 15: a name heuristic catches `_rate` and `_ctr` but not
`average_new_mrr`, `page_speed` or `sent_emails_per_contact`, and the mean of a
month of daily averages is no more meaningful than the mean of a month of daily
conversion rates. And the grain cut is not a per-node choice — a rate parent may
not be finer than its child, so declaring one node's grain propagates through
its whole downstream chain, and since weeks do not nest in months a tree with a
single apex admits `{day, week}` or `{day, month}`, never all three.

*(This horizon is a punch list with an end, so rows are added only for defects
found while closing it, never for new scope. C11–C14 were found while closing
it. C15 and C16 are the exception that proves the rule: they are not new scope
either, but they were not found while closing this horizon — they came from a
second hostile review (2026-08-12) aimed at the release gates, and they land
here because they are the same failure class the horizon exists for, a
plausible wrong number at the provider boundary. Both block the first client
deployment; C15 also blocks PyPI.)*

*(**C17 and C18 come from the same review and the same exception** — but from
its second pass rather than its first. The review was frozen at `c18d150` and
said so; re-checking all 33 findings against `e433daa` on 2026-08-12 confirmed
28 still live, and promoted these two out of the "high" and "medium" tiers into
this horizon on the horizon's own test — does it hand someone a number the
engine cannot defend. C17 does, over MCP, where the NaN becomes `null` and the
surviving numbers are arithmetically impossible; C18 does directly. **The other
26 findings are not in Horizon 0 and should not be moved here**: they are
availability, performance, packaging or documentation defects, and the ones
fixed in the same pass are recorded in [2.18](#horizon-2--make-it-repeatable-a-stranger-can-onboard).
The triage that made those calls, with the reproductions behind them, is
[`archive/grill_2026_08_12_triage.md`](archive/grill_2026_08_12_triage.md)
— archived 2026-08-17, its three then-open findings carried forward as
[C25](#horizon-0--correctness-numbers-the-engine-cant-defend)(b) and
[2.20](#horizon-2--make-it-repeatable-a-stranger-can-onboard).)*

*(**C19–C22 are the read-the-numbers exception** — found 2026-08-13 by a
reviewer running the demo trees and reading the output rather than the code
(PR #66), which is the practice the fifth rule prescribes and the one both
hostile reviews stopped short of. All four shipped the day they were found;
the rows exist because [1.11](#horizon-1--prove-it-a-trustworthy-reproducible-rca)
already cited C21 as a design lesson and a punch list a row cites had better
carry the row. **C23–C25 are the 2026-08-17 audit's exception** — the
[milestone-readiness audit](milestone_readiness_2026_08_17.md) traced four
recently-decided policies from decision to code to tests and found the same
failure class at three boundaries the four rules' structural tests do not
enumerate: engine→MCP shaping, engine→`app.js`, and metric-path→slice-path.
Its other findings — mediums and lows that are test gaps, stale comments or
doc drift rather than wrong numbers — stay in the report and are not rows.)*

**Exit:** every row ✅, and no statement in [`docs/model.md`](../docs/model.md) or
the [white paper](statistics_whitepaper.md) describes behavior the code does not
have.

---

## Horizon 1 — Prove it: a trustworthy, reproducible RCA

Goal: an RCA a stakeholder believes, on governed metrics, that re-runs deterministically.

| # | Item | Status | Why |
|---|------|--------|-----|
| 1.1 | **Statistical hardening finish** (T9) — input validation, seasonality identifiability, and the example's documented pitfall. *(full history: [1.1](roadmap_log.md#1-1))* | ✅ | Silent-corruption guards. |
| 1.2 | **Calibration test suite** (T10) — known-root-cause recovery, null-case restraint, CI coverage against synthetic ground truth. Shipped as `tests/test_calibration.py` (contemporaneous/lagged/identity recovery, null + unrelated-parent restraint, 20-world CI coverage), made deterministic by the sampler seeding | ✅ | The moat made testable; guards T1–T9 against regression |
| 1.3 | **Config hardening** — per-metric grain floors, `kind` (flow/stock/rate) and sign-convention metadata. `grain` + `kind` shipped with 1.7; `expected_signs` (declared coefficient direction + contradiction diagnostic) shipped after a live wrong-sign what-if on the Net-New-MRR tree; display sign conventions shipped as `direction: up_is_good\|down_is_good\|neutral` (goodness-aware UI coloring; arrows stay directional) | ✅ | Table stakes before config lands in an external repo; prevents cumulative-vs-flow and sign traps |
| 1.4 | **UI trust finish** — fit provenance in the Metric tab, name-keyed coefficients, fit-window controls (U5); accessibility & keyboard pass (U6) | ○ | The reader/reviewer persona is the audience these features serve |
| 1.5 | **Exportable RCA report** — one click → self-contained HTML (printable to PDF): target strip, tree snapshot, ranked causes, attribution tables, methods footnote. Shipped client-side from the Share menu (embedded PNGs, zero external requests) | ✅ | The shareable artifact; the thing an analysis becomes when it leaves the app |
| 1.6 | **Validate against a known incident** — replay a historical anomaly on real governed data end-to-end. Done on the Net-New-MRR tree: the May→June 2025 credit-pack→unified-subscription migration recovered live (expansion MRR ≈ the whole swing), on both the daily and weekly engines | ✅ | The validation moment: recovering a known answer earns trust on the unknown ones |
| 1.7 | **Per-node aggregation grain** — let a node declare its natural grain (daily flows, weekly/monthly cohort rates); resample each node to its grain before fit/attribution instead of forcing daily. *(full history: [1.7](roadmap_log.md#1-7))* | ✅ | Ratio/cohort nodes are degenerate at daily grain (ARPU on a 1-member day; conversion on a low-volume day), producing noise the bootstrap then papers over. |
| 1.8 | **Covariance-asymmetry test + fix** — per-day Shapley drops reference-window covariance from the reconstruction baseline, so `unexplained` absorbs `−cov_ref` for co-moving multiplicative factors even on exact identities. *(full history: [1.8](roadmap_log.md#1-8))* | ✅ | Attributions on multiplicative nodes with correlated factors under-explain by exactly the factors' reference covariance — violates "never ship a number the engine can't defend" (principle 3) |
| 1.9 | **A stock-accumulation edge** — express `level[t] = level[t-1] + inflow[t] − outflow[t]` (subscriber base, customer count, cash) as a first-class relationship. *(full history: [1.9](roadmap_log.md#1-9))* | ○ | Surfaced by C10. |
| 1.10 | **Reference-window defaulting + history discovery** — make the reference window optional everywhere (engine, API, MCP, UI): omitted references default to the *matched adjacent block* (4× the analysis length, min 28 days, whole-week length when seasonality is in the target's scope, clamped to loaded data), echoed back with `reference_defaulted`. *(full history: [1.10](roadmap_log.md#1-10))* | ✅ | Users conflate the reference window with the training window and hand-shrink both: the fit already uses all loaded history before `analysis_start`, and the reference is only the comparison baseline — where "as long as possible" is actively wrong on a trending metric (the gap … |
| 1.11 | **Rates that are undefined, not zero** — a rate whose denominator is legitimately zero in some periods has no value there, and the engine had no way to say so. *(full history: [1.11](roadmap_log.md#1-11))* | ✅ | **We were not inventing a representation for this; we were failing to receive one that upstream already sends.** The White Cube dbt project writes its rates as `num / nullif(den, 0)` — the canonical MetricFlow idiom — so the semantic layer returns NULL for an undefined rate, … |
| 1.12 | **`denominator` stays optional; the enforcement moved to `doctor`** — [`rate_denominator_policy.md`](rate_denominator_policy.md)'s recommendation, shipped rather than left as a written argument. *(full history: [1.12](roadmap_log.md#1-12))* | ✅ | Closes the loop the policy doc opened the same day: permissive-and-disclosed only holds up if the disclosure actually gates something, and until this shipped, an unanswered rate's only consequence anywhere in the product was a log line and a `[SKIP]` nobody's exit code noticed. |

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
| 2.1 | **Connection doctor** for dbt Cloud SL — walk the auth chain (token → host cell → environment → SL config → credential → mapping), name the missing link, emit a copy-paste remediation page for the admin steps outside our control. *(full history: [2.1](roadmap_log.md#2-1))* | ✅ | Turns days of provisioning archaeology into minutes; is itself the onboarding demo |
| 2.2 | **CSV ingest + per-metric provider mixing** — a tree where some nodes come from the SL and some from direct SQL/CSV is a normal migration state (`source:` already carries a provider-qualified path). *(full history: [2.2](roadmap_log.md#2-2))* | ◑ | direct-SQL/warehouse provider exists; CSV ingest and per-metric mixing remain. |
| 2.3 | **Tree scaffolder / bootstrapper** — enumerate SL metrics; turn `derived`/`ratio` `input_metrics` into formula edges; LLM-assisted import of latent trees (canvas exports, metric docs → draft YAML). *(full history: [2.3](roadmap_log.md#2-3))* | ○ | Blank-YAML is the adoption killer; trees already exist in fragments. |
| 2.4 | **Snapshot store** (parquet/DuckDB) — fetch once per (metric, window, grain), refit from snapshots. Shipped as a parquet read-through cache at the fetcher boundary (`snapshots.py`): tree-adjacent `.breakdown/snapshots/` (committable → RCAs re-run from a fresh clone), `--refresh`/`--no-snapshots`/`--snapshot-dir` controls, failure-soft on read-only mounts, and a warehouse outage is survivable when every metric has a snapshot. DuckDB deferred until scheduling (3.1) needs cross-snapshot queries | ✅ | Reproducibility, provider-migration invisibility, warehouse politeness, and the foundation for scheduling |
| 2.5 | **MCP server** — expose `run_rca`, `get_tree`, `explain_metric` as tools | ✅ | AI analysts guess at "why"; breakdown is the grounded causal tool they should call. Cheap (endpoints exist), differentiating, and meets users where they already ask why-questions. Shipped as streamable HTTP at `/mcp` with a fourth tool beyond the original scope (`run_whatif`); analysis responses carry `how_to_read` caveats and `report_url` deep links into the UI |
| 2.6 | **Outsider docs pass** — install guide + first-tree tutorial on public data, and a README that is a landing page rather than a manual. *(full history: [2.6](roadmap_log.md#2-6))* | ✅ | First impressions for anyone arriving cold — and the first impression is 122 KB of reference material on PyPI. |
| 2.8 | **Dimensional slicing on the `warehouse` provider** — the SQL contract for a sliced fetch (`fetch_metric_sliced` returning `[date, slice, value]`), `doctor` checks for it, and the docs. *(full history: [2.8](roadmap_log.md#2-8))* | ○ | Slicing is the bridge from *metric* to *event*: "AOV fell" is a narrowing, "AOV fell, concentrated in EMEA on iOS 18.4" is a diagnosis — and the diagnosis is what the customer was going to spend the next three hours on. |
| 2.9 | **Per-node binding contract** — a node binds to its source individually via a `bind:` block (`relation` or inline `sql:`, `grain_key`, `time_column`, `agg`, `numerator`/`denominator` on ratios, `entity_key` on non-additive, `dimensions` with many-to-one join paths); `provider:` demotes to a tree-level default, so existing trees are unchanged. *(full history: [2.9](roadmap_log.md#2-9))* | ✅ | The prerequisite is the deal-killer: most prospects have no semantic layer, "come back once you've built one" is a six-week tax before first value, and what they would build serves org-wide self-serve when breakdown needs one series per node. |
| 2.10 | **dbt binding — read the semantic manifest, generate the SQL, prove it agrees** — parse `target/semantic_manifest.json` (originally via `metricflow_semantic_interfaces`, **never** the deprecated `dbt-semantic-interfaces`, which returns new-spec metrics with no aggregation *and validates with 0 errors* — 61/61 on a real project; the models moved in-tree in … *(full history: [2.10](roadmap_log.md#2-10))* | ✅ | Both dbt paths we ship tax principle 4 in ways we cannot fix from our side: `cloud` dies on provisioning archaeology (2.1's whole existence) and excludes dbt Core shops entirely, while `local` shells out to `mf` once *per slice* behind a 120s timeout and is why the `dbt` extra … |
| 2.11 | **Query provenance in the API/UI** — surface the generated SQL per fetched series and per slice; "show query" on node cards, slice panels and the RCA export **Shipped:** `GET /metrics/{name}/query` plus a `show query` toggle on the Metric tab. *(full history: [2.11](roadmap_log.md#2-11))* | ✅ | Principle 3 is "never ship a number the engine can't defend", and today `warehouse` is the only provider where the user can see what was queried — because they wrote it themselves. |
| 2.13 | **Supersede the `local` provider** — ✅ **Amended and shipped (2026-08-11).** Scoped as "retire", on the claim that 2.10 does everything `local` does. *(full history: [2.13](roadmap_log.md#2-13))* | ✅ | Two providers solving one problem is a maintenance and docs cost, but a blanket deprecation would have been noise for the author whose tree genuinely needs MetricFlow, and actively misleading if the general claim were taken at face value. |
| 2.14 | **Differential verification against MetricFlow** — `breakdown doctor --verify-against-metricflow` runs our generated SQL *and* MetricFlow's own compiled SQL for the same metric, grain and window, and asserts equality on real data. *(full history: [2.14](roadmap_log.md#2-14))* | ○ | The strongest argument against generating our own SQL is that a number disagreeing with the client's dashboard is dead on arrival however right we are. |
| 2.15 | **No runtime dependency on dbt Labs** — ✅ **shipped 2026-08-11.** The `dbt-bridge` extra pulled `metricflow` solely for `metricflow_semantic_interfaces`: twelve transitive packages and a `<3.15` Python ceiling for a single `parse_obj` call. *(full history: [2.15](roadmap_log.md#2-15))* | ✅ | Two interested clients, zero dependents and nothing published yet — the cheapest this debt will ever be to cut. |
| 2.16 | **Multiple metric trees** — ✅ **shipped 2026-08-12.** `--tree` accepts a directory (the `breakdown/` folder of a dbt repo), each `*.yml` a tree with its id from the filename stem. *(full history: [2.16](roadmap_log.md#2-16))* | ✅ | "A company can serve one **or more** trees" is the whole ask, and any of them may be durable or disposable, with a goal or without. |
| 2.17 | **Real filter support — `where:` on `BindingSpec`** — carry a predicate on the binding, fed by dbt's `where_sql_template`, and compile it into the generated SQL alongside the grain/window/dimension clauses. *(full history: [2.17](roadmap_log.md#2-17))* | ✅ | **C15 made this urgent by making it visible.** Filtered metrics used to "work" — wrongly, serving the unfiltered relation — and now they are refused by name, which is correct and is also a capability the tree author can see they don't have. |
| 2.19 | **`fill_nulls_with` is refused, and it is most of a real project** — [C15](#horizon-0--correctness-numbers-the-engine-cant-defend) refused four semantics-changing MetricFlow fields together, correctly: a measure declaring `fill_nulls_with: 0` means something different from the measure without it, and `BindingSpec` cannot express the difference, so serving it would be a silent … *(full history: [2.19](roadmap_log.md#2-19))* | ○ | 2.17's row argues that filters are "the difference between the dbt path serving a real project and serving a demo". |
| 2.18 | **Request-lifecycle and deployment hardening** — ✅ **shipped 2026-08-12.** The non-wrong-number half of the second review's findings, taken as one pass because they are one shape: an operation that is unbounded, or that runs somewhere it should not. *(full history: [2.18](roadmap_log.md#2-18))* | ✅ | Two viewers share one process, one lock and one cache, so every unbounded operation here is one careless request away from being everyone's outage — and the second review reached 261 cached traces, a 73,000-day provider scan and a 25-second freeze through the **public API**, without malice. |
| 2.20 | **Snapshot-mode operational honesty** — the two 2026-08-12 review findings that survived every fixing pass, promoted from the archived triage because they block the first client deployment. *(full history: [2.20](roadmap_log.md#2-20))* | ✅ | Neither is a wrong number; both are the tool declaring itself broken when it is not, which at a first deployment costs trust at the same rate a wrong number does — and every failure path in the product points the operator at `doctor`. |
| 2.21 | **A localization verdict may not be headlined by the roll-up bucket** — `POST /rca/{name}/slices` publishes `localized` (the verdict the UI and MCP read verbatim since [C24](#horizon-0--correctness-numbers-the-engine-cant-defend)), and the rule behind it is excess-over-gap >= 25% on the top slice. *(full history: [2.21](roadmap_log.md#2-21))* | ✅ | "Not localized" restraint is the pitch — the demo scripts it as the reason the localized verdicts are worth believing — and a verdict that names the bucket of unenumerated leftovers as the culprit spends exactly that credibility. |

*(2.12 — a `bsl` binding — was removed 2026-08-11. It existed to serve a local
parquet file with no warehouse, and [2.10](#horizon-2--make-it-repeatable-a-stranger-can-onboard)
made that possible without it: the binding contract plus the DuckDB connector
run `relation: read_parquet('…')` or a `bind.sql` over the file, verified end to
end. The residue — a provider type that reaches per-node bindings without a dbt
project on disk — is a fraction of a BSL adapter and folds into 2.2. What was
left of the case was "someone might already author BSL YAML", which principle 1
rules out and which this roadmap already applies to Cube, Rill, Lightdash and
Superset. IDs are not reused.)*

*(2.7 — hybrid cold-start→fitted mode — was removed 2026-08-05; see
[Deliberately not on the roadmap](#deliberately-not-on-the-roadmap). IDs are not
reused.)*

**Exit:** a new tree onboards in < 1 day; an external tool runs an RCA against a demo tree via MCP; a company can keep a tree per quarterly goal and see them all in one place.

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
| 3.2 | **Dimensional slicing inside the tree** — attribute a node's gap across a declared dimension (geo, plan, channel), warehouse-side *(full history: [3.2](roadmap_log.md#3-2))* | ◑ | Completes the traverse + slice workflow. |
| 3.3 | **Native metric-view connectors** (e.g. Databricks metric views) — **largely superseded (2026-08-10) by [2.9](#horizon-2--make-it-repeatable-a-stranger-can-onboard)**: generating SQL through sqlglot dialects covers Databricks/Snowflake/BigQuery/Postgres/DuckDB in one provider, and 2.9's whole point is that it needs no dbt SL. What survives here is genuinely *native* metric objects (Databricks metric views, Snowflake semantic views) whose definitions live outside dbt entirely | ○ | Hedge against thin dbt-SL adoption; slots in as another `BaseDataFetcher` + scaffolder. The hedge is mostly paid for once 2.9 ships — the residual is shops with no dbt at all |
| 3.4 | **Counterfactual RCA** (T11: posterior-predictive forecast) — "the drop was X units below what the normal regime predicts (95% CI …)" | ○ | Upgrades the flat-trend approximation; strong headline number. Distinct from the existing steady-state what-if |
| 3.5 | **Hosted mode** — auth, scheduled refresh, fit queue + warm cache | ○ | The operational product layer; PyMC fits are CPU-heavy, so a queue + cache is required |
| 3.6 | **Domain template packs** — worked example trees + methodology for specific domains (e.g. emissions/impact driver-tree decomposition) | ○ | Content that doubles as onboarding examples and demonstrates breadth |
| 3.7 | **Deployable demo instance** — a hosted Breakdown over synthetic B2C SaaS data ("White Cube") with planted, ground-truth-labeled anomalies, per [`white_cube_demo_plan.md`](white_cube_demo_plan.md). *(full history: [3.7](roadmap_log.md#3-7))* | ✅ | The pitch artifact: a link a prospect can actually use, live and auto-deployed on every push to `main`. |
| 3.8 | **Non-additive metrics at entity grain** — a `count_distinct` metric's slices overstate the total by their deduplication overlap, and the engine currently reports that as `reconciliation.status = "discrepant"` in red, which reads as a broken pipeline rather than as arithmetic. *(full history: [3.8](roadmap_log.md#3-8))* | ✅ | Measured on a real warehouse: `active_subscription_count` sliced by status came to **2,106 against an unsliced 2,069** — 37 subscriptions that changed status inside a day, counted once in the total and once per status. |
| 3.9 | **A documentation site** — render `docs/` as a navigable site (MkDocs Material is the default choice for a Python project of this shape; the files are already markdown in the repo, so this is a generator, a CI job and hosting rather than a rewrite). *(full history: [3.9](roadmap_log.md#3-9))* | ○ | Once `docs/` holds a reference, a tutorial and an operations guide, flat markdown on GitHub stops being navigable — no search, no sidebar, no versioning against a package whose YAML schema is its public API. |

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
| S1 | **Benchmark full-rank ADVI** as the RCA default — `pm.fit(method="fullrank_advi")` fits a full covariance matrix instead of a diagonal one. *(full history: [S1](roadmap_log.md#s1))* | ✅ | The synthetic promise is real: at convergence (40k optimizer steps, not the engine's 20k default — at 20k every full-rank fit is `suspect` with width ~1.8× NUTS and falling) full-rank reproduces the NUTS interval on the β-vs-trend ridge to within 4% where mean-field is ~20% … |
| S4 | **Parent collinearity diagnostic** — pairwise correlation (or VIF) among a node's parent regressors over the fit window; warn when the split of credit is unstable *(full history: [S4](roadmap_log.md#s4))* | ✅ | **Promoted (2026-08-05)** from below S3 to here. |
| S2 | **A real ADVI approximation diagnostic** — PSIS-based k̂ (Yao et al., 2018) reported per fit; where k̂ is poor, auto-escalate that node to NUTS or mark its intervals unreliable in the response. *(full history: [S2](roadmap_log.md#s2))* | ✅ | Today's `fit_quality` for ADVI checked only that the ELBO stopped moving, so a well-converged *bad* approximation passed as `"ok"`. |
| S3 | **Posterior predictive checks on every fit** — simulate replicated series from the posterior, compare summary statistics against the observed series, flag nodes whose data sits in the tail; surface through the existing `fit_quality` channel. *(full history: [S3](roadmap_log.md#s3))* | ✅ | The single most informative Bayesian model check there is (Gelman et al., 2020) and the one the engine most conspicuously lacked. |
| S5 | **Simulation-based calibration** (Talts et al., 2018) — draw parameters from the prior, simulate, refit, check that the rank of the true parameter within the posterior is uniform | ○ | The definitive test that inference is calibrated. Turns 1.2's single-scenario coverage test into a real guarantee. Expensive: a release-gate or nightly job, not per-commit |
| S6 | **Data-driven bootstrap block length** (Politis & White, 2004), replacing the fixed per-grain constants in `BOOT_BLOCK`. *(full history: [S6](roadmap_log.md#s6))* | ○ | Block length currently is not estimated from the data at all. |
| S7 | **Correlated cold-start beliefs** — let authors declare correlations between priors (or a joint distribution over a small set of beliefs) | ○ | The largest modeling gap in cold start: beliefs are sampled independently today, so "if price lands high, conversion lands low" is unrepresentable and intervals are wrong in either direction wherever beliefs genuinely co-vary. Already disclosed in every cold-start response |
| S8 | **Local linear trend as an opt-in** — a trend with a slope component, chosen per node in the YAML; local level stays the default | ○ | A node with genuine momentum is currently modeled as a level that happened to move, which pushes momentum onto the parents. Keep the tight-prior default (it does deliberate work) and give the exception an escape hatch |
| S9 | **Narrow nonlinear edges** — a declared transform on a specific edge (`response: log` on ad spend → conversions), not a modeling language | ○ | Covers the most common nonlinearity (diminishing returns) without opening the door to arbitrary model complexity. MVP-first: one named transform |
| S10 | **Posterior predictive plot in the UI** — observed vs replicated series per node. *(full history: [S10](roadmap_log.md#s10))* | ✅ | The most persuasive single visual a Bayesian tool can offer, and nearly free once S3 computes it. |
| S11 | **Prior-vs-posterior visualization** per coefficient — "you believed 0.1 ± 0.02; the data says 0.08 ± 0.01" | ○ | Makes the Bayesian update concrete and teaches the model while it informs. Directly serves the reader/reviewer persona 1.4 targets |
| S12 | **Make `ranked_causes` visibly a heuristic in the UI** — distinguish "ranked by triage score" from "ranked by evidence", or attach the underlying interval so a wide-interval cause cannot outrank a tight one on a point estimate | ○ | It is documented as triage and rendered as the most prominent number in the UI. Prominence implies rigor whatever the docs say |
| S13 | **Methods appendix in the exported report** — a linkable expansion of the existing methods footnote stating fit window, inference method, diagnostics, and the caveats that applied to *that* analysis | ○ | Makes an exported RCA self-defending when it circulates without its author — the whole point of 1.5's export |
| S14 | **Quantify the DAG assumption** — a sensitivity statement: "if an unmodeled confounder explained X% of this parent's movement, the attribution would change by Y" | ○ | Puts a number on the assumption everything rests on. Highest ceiling and least defined item here; adapting unmeasured-confounding sensitivity analysis to metric trees is research-flavored. Note this is *not* causal discovery, which stays off the roadmap |
| S15 | **Multiplicity and selection-aware reporting** — disclose first (`how_to_read`, [`docs/model.md`](../docs/model.md), the UI), then evaluate whether the reported interval on the *selected* top cause can be made selection-aware *(full history: [S15](roadmap_log.md#s15))* | ○ | A single `run_rca` on a 15-node tree emits 25–30 intervals plus a `prob_same_direction` each, sorts by effect size, and presents the top one — whose `ci_95` was computed **pre-selection**. |
| S16 | **Forward-simulation variance in the trend interval** — `rca.py:1618` computes `trend_delta` from `trend_samples[:, -1]`; `t_an` is computed three lines earlier and feeds only the seasonal term, so a one-day analysis window and a ninety-day one starting the same date return the **identical** trend estimate *and* the identical CI. *(full history: [S16](roadmap_log.md#s16))* | ○ | The flat *point* forecast is a deliberate, documented property of a local-level random walk (and S8/3.4 address it). |
| S17 | **Rebuild the calibration suite's coverage test** — draw truth from the DGP rather than the realized series; add the two cases with **zero** coverage today; vary the seed per world; raise the pass bar *(full history: [S17](roadmap_log.md#s17))* | ○ | `_planted_step_world` computes `truth = beta * (x[an].mean() − x[ref].mean())` from the **realized** `x`, and checks it against percentiles of `beta_samples × bootstrap of that same realized x` — so the window-sampling term is pure added width around a point already equal to the … |
| S18 | **Right-censored metrics — series whose past values restate.** The snapshot key is `(metric, grain, kind, window)` (`snapshots.py:_filename`) with no content hash and no TTL, so a series that rewrites its own history is frozen at whatever it said on first fetch, and a model refit from snapshots trains on values the warehouse has since changed. *(full history: [S18](roadmap_log.md#s18))* | ○ | Found at Northern Nights (2026-08-11), where payment plans settle backwards and the settled basis catches ~25% of booked early in a cycle — but the shape is general: late-arriving conversions, refunds, chargebacks, insurance claims, anything bitemporal. |
| S19 | **Partial pooling across a repeated-cycle grouping** — let nodes declare membership in a `cycle` (edition, season, cohort, campaign) and partially pool coefficients across cycles, so a node with five observations borrows strength from its own history rather than fitting each cycle alone *(full history: [S19](roadmap_log.md#s19))* | ○ | The thin-panel case: one product cycle a year and five editions of history ever is not a pathology, it is festivals, conferences, annual enrollment, agriculture and elections. |
| S20 | **Zero-inflated and count likelihoods** — the observation model is Gaussian throughout (`model.py:727`). *(full history: [S20](roadmap_log.md#s20))* | ○ | A seasonal business has months-long true-zero windows between cycles, and a Gaussian fit to a series that is exactly zero for a third of its length puts mass on negative counts and mis-states the variance everywhere else. |
| S21 | **Fit a node with undefined periods by masking the likelihood, not refusing it** — **optional; build it when a real tree needs it, not before.** [1.11](#horizon-1--prove-it-a-trustworthy-reproducible-rca) settled the undefined-rate policies and chose *refusal* at the fit: a node with any undefined period reports `fit_failed`, naming the periods. *(full history: [S21](roadmap_log.md#s21))* | ○ | The alternative to a posterior is no posterior, and this project's own position is that withholding is honest — so this is not a correctness gap and does not belong in Horizon 0. |
| S22 | **k̂ is a Monte-Carlo estimate published without its own error** — [S2](#statistical-rigor-s--a-standing-workstream) computes k̂ from `_KHAT_DRAWS = 1000` samples of the approximation, and `POST /analyze/{name}` does not seed that draw. *(full history: [S22](roadmap_log.md#s22))* | ✅ | Rule 1's shape, one layer in from the provider boundary: the check that exists to refuse an approximation is approximating, and says so nowhere. |
| S23 | **Reference-window sensitivity** — every number the engine publishes is a contrast of two window means, and the only uncertainty ever quantified is *within*-window sampling; nothing resamples the choice of window, and that choice is usually the engine's own (`default_reference_window` applies four heuristics in sequence, none a property of the data). *(full history: [S23](roadmap_log.md#s23))* | ○ | The module scrupulous enough to censor a direction probability at 1 − 1/500 says nothing about the single input with the largest influence on its headline answer — a reader cannot tell whether "X is the top cause" survives moving the reference window by one week |

*(**Considered and not scheduled: dated events as precision-aware model terms.**
Northern Nights asked for an event term carrying its own date uncertainty, their
marketing calendar tagging every row day / week / month precision. The MVP-first
answer already exists and they had independently built it: encode events as
**intensity series** at the grain their precision supports — day rows feed daily
nodes, week rows a weekly series, month rows stay out — which needs no schema at
all and keeps the engine's "no uncertainty slot for dates" honest rather than
faked. A first-class `event:` node type with a date prior is the modeling-language
scope creep [Deliberately not on the roadmap](#deliberately-not-on-the-roadmap)
rules out. What is worth taking is their **precision discipline** and their
separation of an event's *comms* (a demand spike, an intensity node) from its
*execution* (a price change, visible through the units × price identity) —
collapsing the two recreates a confound. Both are authoring guidance, not
engine work.)*

**Related, already scheduled elsewhere:** [3.4](#horizon-3--make-it-findable-and-sticky-it-comes-to-you)
(counterfactual RCA via posterior-predictive forecast) is the white paper's
fourth §4.1 item — it upgrades the flat-trend approximation and shares
machinery with S3. It stays in Horizon 3 rather than being duplicated here.

**Exit:** no fixed exit — this track is maintenance of a property, not a
milestone. The nearest thing to a bar: intervals that pass SBC (S5) *and a
coverage test that can fail* (S17), a default inference path whose approximation
error is *measured* rather than assumed (S1/S2 — met 2026-08-24: the default
path has no approximation error, because it is exact MCMC; the opt-in
approximation carries a measured k̂ on every fit), and
no weakness in white paper §3.2 left without either a fix or a disclosed caveat.

---

## Deliberately not on the roadmap

- Real-time / streaming grains.
- Our own **semantic layer** — governance, access control, self-serve, result
  caching, reusable dimension groups, general join planning. We ride dbt's where
  it exists. **Amended (2026-08-10):** this line previously read "our own metric
  definition language (we ride dbt's)", which was always broader than the
  intent — the tree schema has carried `priors`, `lags`, `expected_signs`,
  `kind`, `plausible` and `formula` since 0.1.0, and the `warehouse` provider's
  per-metric `sql:` was already an untyped binding. Roadmap
  [2.9](#horizon-2--make-it-repeatable-a-stranger-can-onboard) makes that last
  mile explicit as a **fetch descriptor** — how one node gets one series — and
  nothing more. The boundary test for any proposed field, and the `sql:` stop
  rule that holds it, are in
  [`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
  §4.1. What is listed above stays out permanently.
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

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

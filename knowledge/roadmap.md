# Breakdown — Product Roadmap

A prioritized list of what to build, grounded in what's already shipped. Horizons,
not dates: each gates on its exit criteria. This is the product/engineering roadmap
only — no go-to-market.

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
attribution). *(statistical plan T1–T8; what-if design spec.)*

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

**Not yet built** (the roadmap below): remaining statistical rigor (T9–T11), UI trust
finish (U5–U6), the connectivity kit, and the market-driven items (report export,
scheduled monitoring, dimensional slicing).

---

## Horizon 1 — Prove it: a trustworthy, reproducible RCA

Goal: an RCA a stakeholder believes, on governed metrics, that re-runs deterministically.

| # | Item | Status | Why |
|---|------|--------|-----|
| 1.1 | **Statistical hardening finish** — input validation (window ordering/overlap, lagged-window bounds), seasonality identifiability checks, and remove the unidentifiable annual component from the example tree (T9) | ○ | Silent-corruption guards; the example currently ships a documented pitfall |
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

**Exit:** a new tree onboards in < 1 day; an external tool runs an RCA against a demo tree via MCP.

---

## Horizon 3 — Make it findable and sticky: it comes to you

Gate: real, recurring usage asking for these.

| # | Item | Status | Why |
|---|------|--------|-----|
| 3.1 | **Scheduled evaluation + anomaly flagging + digest** — "revenue moved; order_count explains ~80%, CI attached — link to full RCA" | ○ | Converts breakdown from pull to push; the retention feature and the seed of the ops layer |
| 3.2 | **Dimensional slicing inside the tree** — attribute a node's gap across a declared dimension (geo, plan, channel), warehouse-side | ○ | Completes the traverse + slice workflow. Flat slicing is commoditized; *tree × slice* is not — the biggest pure-product differentiator |
| 3.3 | **Native metric-view connectors** (e.g. Databricks metric views) | ○ | Hedge against thin dbt-SL adoption; slots in as another `BaseDataFetcher` + scaffolder |
| 3.4 | **Counterfactual RCA** (T11: posterior-predictive forecast) — "the drop was X units below what the normal regime predicts (95% CI …)" | ○ | Upgrades the flat-trend approximation; strong headline number. Distinct from the existing steady-state what-if |
| 3.5 | **Hosted mode** — auth, scheduled refresh, fit queue + warm cache | ○ | The operational product layer; PyMC fits are CPU-heavy, so a queue + cache is required |
| 3.6 | **Domain template packs** — worked example trees + methodology for specific domains (e.g. emissions/impact driver-tree decomposition) | ○ | Content that doubles as onboarding examples and demonstrates breadth |

---

## Deliberately not on the roadmap

- Real-time / streaming grains.
- Our own metric definition language (we ride dbt's).
- Causal **discovery** — the DAG stays the analyst's hypothesis; that's the premise, not a gap.
- Per-seat anything.
- Dark mode (for now).

---

*Ticket-level detail and the rationale behind shipped work live in
[`archive/`](archive/) (statistical review T1–T12, UI plan U1–U6, connectivity
analysis). This roadmap absorbs their open items.*

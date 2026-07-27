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

**Not yet built** (the roadmap below): remaining statistical rigor (T9–T11), UI trust
finish (U5–U6), the connectivity kit, and the market-driven items (report export,
MCP server, scheduled monitoring, dimensional slicing).

---

## Horizon 1 — Prove it: a trustworthy, reproducible RCA

Goal: an RCA a stakeholder believes, on governed metrics, that re-runs deterministically.

| # | Item | Status | Why |
|---|------|--------|-----|
| 1.1 | **Statistical hardening finish** — input validation (window ordering/overlap, lagged-window bounds), seasonality identifiability checks, and remove the unidentifiable annual component from the example tree (T9) | ○ | Silent-corruption guards; the example currently ships a documented pitfall |
| 1.2 | **Calibration test suite** (T10) — known-root-cause recovery, null-case restraint, CI coverage against synthetic ground truth | ○ | The moat made testable; guards T1–T9 against regression |
| 1.3 | **Config hardening** — per-metric grain floors, `kind` (flow/stock/rate) and sign-convention metadata | ○ | Table stakes before config lands in an external repo; prevents cumulative-vs-flow and sign traps |
| 1.4 | **UI trust finish** — fit provenance in the Metric tab, name-keyed coefficients, fit-window controls (U5); accessibility & keyboard pass (U6) | ○ | The reader/reviewer persona is the audience these features serve |
| 1.5 | **Exportable RCA report** — one click → self-contained HTML (printable to PDF): target strip, tree snapshot, ranked causes, attribution tables, methods footnote | ○ | The shareable artifact; the thing an analysis becomes when it leaves the app |
| 1.6 | **Validate against a known incident** — replay a historical anomaly on real governed data end-to-end | ○ | The validation moment: recovering a known answer earns trust on the unknown ones |

**Exit:** a stakeholder accepts an RCA finding; the same RCA re-runs deterministically from a fresh clone; report export exists.

---

## Horizon 2 — Make it repeatable: a stranger can onboard

Goal: onboarding a new tree costs a day, not a week.

| # | Item | Status | Why |
|---|------|--------|-----|
| 2.1 | **Connection doctor** for dbt Cloud SL — walk the auth chain (token → host cell → environment → SL config → credential → mapping), name the missing link, emit a copy-paste remediation page for the admin steps outside our control | ○ | Turns days of provisioning archaeology into minutes; is itself the onboarding demo |
| 2.2 | **CSV ingest + per-metric provider mixing** — a tree where some nodes come from the SL and some from direct SQL/CSV is a normal migration state (`source:` already carries a provider-qualified path) | ◑ | direct-SQL/warehouse provider exists; CSV ingest and per-metric mixing remain. The zero-integration on-ramp: "send a CSV, get an RCA," then migrate node-by-node to governed metrics |
| 2.3 | **Tree scaffolder** — enumerate SL metrics; turn `derived`/`ratio` `input_metrics` into formula edges; LLM-assisted import of latent trees (canvas exports, metric docs → draft YAML) | ○ | Blank-YAML is the adoption killer; trees already exist in fragments |
| 2.4 | **Snapshot store** (parquet/DuckDB) — fetch once per (metric, window, grain), refit from snapshots | ○ | Reproducibility, provider-migration invisibility, warehouse politeness, and the foundation for scheduling |
| 2.5 | **MCP server** — expose `run_rca`, `get_tree`, `explain_metric` as tools | ○ | AI analysts guess at "why"; breakdown is the grounded causal tool they should call. Cheap (endpoints exist), differentiating, and meets users where they already ask why-questions |
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

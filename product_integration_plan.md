# Breakdown as a product: data-connectivity plan

*How a paying customer connects Breakdown to their metrics stack — written against the
Narrative pilot (July 2026), which serves as the worked example throughout.*

Breakdown's engine (BSTS fits, Shapley attribution, RCA) is the differentiated part.
The question this document answers is everything upstream of the engine: **where metric
definitions come from, where the time series come from, and where the tree itself
lives** when the customer isn't us.

---

## 1. What the pilot taught us (evidence, not theory)

Two days of connecting Breakdown to Narrative's stack surfaced the actual friction a
customer would hit. Each of these is a product requirement in disguise:

| Pilot friction | Product implication |
|---|---|
| Narrative's dbt Cloud SL was *half-provisioned*: SL config existed, warehouse credential existed ("Metricflow-Databricks"), but no service-token mapping — so every SL query failed with an opaque "Credentials have not been set up for this environment." | Onboarding must include a **connection doctor** that walks the whole auth chain (token → host cell → environment → SL config → credential → mapping) and names the exact missing link. I did this by hand against the Admin API; the product should do it in one click. |
| Service tokens can't be created via user API tokens ("This endpoint cannot be accessed with user api tokens") — an admin must click through the dbt Cloud UI. | Onboarding will always have a **human-admin step** we don't control. Design for it: generate a copy-paste instruction page for the customer's dbt admin, then auto-verify when done. |
| Plan gating: "Only 1 Semantic Layer credential is supported with your plan." Cell-based hostnames (`hx434.semantic-layer.us1.dbt.com`, not `semantic-layer.cloud.getdbt.com`) had to be discovered via `~/.dbt/dbt_cloud.yml`. | SL access is **plan- and topology-dependent**. The connector must auto-detect host cells and degrade gracefully by plan tier. |
| The metric tree already existed — twice. Once as a Count canvas (boxes and prose), once as semantic-layer metrics (PR #67) with an OKF concept doc holding the tribal knowledge: sign conventions (churn/contraction negative), `mrr_usd` is a running cumulative (MAX not SUM), ratio metrics only valid at month grain. | Customers have **latent trees** in canvases, docs, and derived-metric definitions. The product should *scaffold* the tree from what exists rather than start from a blank YAML. Metric-level gotchas (grain validity, cumulative vs flow, sign) need first-class metadata, not doc folklore. |
| Provider config in Breakdown's tree YAML takes a raw `token:` value — unusable the moment the YAML is committed to a customer repo. | **Secret references** (`${ENV_VAR}`, or secret-manager URIs) are table stakes before any customer sees a config file. |

---

## 2. What Breakdown actually needs from a customer

Strip away the plumbing and the engine needs exactly three things:

1. **A metric list with stable names** — the nodes.
2. **Aligned time series** for each metric at a consistent grain (daily preferred;
   BSTS wants ≥ ~90 points, seasonality wants full weekly cycles) over a queryable
   window, refetchable for new RCA windows.
3. **The tree structure**: parent edges, which edges are arithmetic identities
   (formulas) vs learned (priors, lags), plus per-metric metadata (grain validity,
   sign convention, seasonality).

Item 3 is *ours* — no semantic layer expresses priors or lag structure. Items 1–2 are
what the connectivity strategy below is about. The key insight: **the semantic layer
question is only about items 1 and 2, and it's a per-customer choice, not an
architecture choice** — `BaseDataFetcher` already abstracts it correctly.

---

## 3. Connection strategies, compared honestly

### A. dbt Cloud Semantic Layer (the `cloud` provider, exists today)

Query governed metrics via the SL GraphQL/ADBC APIs (`dbtsl` SDK) using an SL service
token. No warehouse credentials ever touch Breakdown.

**For:**
- **Trust is the product.** RCA output that decomposes *the same numbers the board
  deck reports* is credible; RCA on hand-rolled SQL that disagrees with the dashboard
  by 3% is dead on arrival. Governed definitions are the moat against that failure.
- Cleanest security story for SaaS: one revocable, read-only, metrics-only token.
  The customer's security review is 10 minutes, not a warehouse-credential debate.
- Metadata API enables **auto-scaffolding**: `client.metrics()` returns names, types,
  and — for `derived` and `ratio` metrics — `input_metrics`, which are literally
  parent edges. Narrative's `net_revenue_retention_pct` declares its four inputs; a
  scaffolder turns that into DAG edges for free.
- Per-query cost is a non-issue for our access pattern (one daily series per metric
  per window, cached thereafter).

**Against:**
- Requires dbt Cloud on a plan with SL — excludes dbt Core shops entirely.
- Provisioning friction documented in §1. Real, but *one-time* and diagnosable.
- Mixed grain support: some metrics are only valid monthly (offset-window ratios).
  The connector must read grain metadata and refuse/flag invalid daily fetches.

### B. MetricFlow locally (the `local` provider, exists today)

Shell out to `mf query` (or `dbt sl query` for Cloud CLI) inside the customer's dbt
project.

**For:** serves dbt Core users — same governed definitions, no dbt Cloud plan.
**Against:** Breakdown must run *inside* the customer's environment with a working
dbt profile (warehouse creds, correct dbt version, installed deps). Subprocess + CSV
parsing is fragile. This is not a SaaS connector; it's a **self-hosted / CLI mode**
feature — which is exactly what an open-core distribution wants anyway.

### C. Circumvent the semantic layer: direct warehouse SQL

Customer maps each metric to a SQL statement (or a mart table + column); Breakdown
queries the warehouse with scoped read-only credentials.

**For:**
- Universal — works for shops with no dbt at all (Looker, Cube, Count-only, spreadsheets-and-vibes).
- Fastest possible pilot: I could have had Narrative's daily series in an hour via
  Count/Databricks instead of two days of SL provisioning archaeology.
- Full control of grain, filters, and window — no plan gating.

**Against:**
- **Definition drift is the trust killer** (see A). Every hand-written metric SQL is
  a future "why doesn't Breakdown match Looker?" ticket.
- Breakdown holds warehouse credentials — heavier security review, per-warehouse
  connector maintenance (Databricks, Snowflake, BigQuery dialects...).
- Someone must write and maintain N SQL definitions; that someone is initially us,
  which doesn't scale.

### D. File / push ingestion (CSV, parquet, POST API)

**For:** zero-integration demos, air-gapped evaluations, "send us a CSV and we'll show
you an RCA on your own data" as a sales motion. Trivial to build.
**Against:** stale by construction; no refetch for new RCA windows; never the
steady state.

### Verdict: don't circumvent — *sequence*

The semantic layer is not an obstacle to route around; it's the wedge. The honest
posture is:

1. **Lead with A** (dbt Cloud SL) as the recommended steady state and the thing our
   marketing says: *"Breakdown explains the metrics you already govern."*
2. **Ship C and D as pilot accelerators** — get the RCA in front of the buyer in week
   one on direct SQL or a CSV drop, *then* migrate node-by-node to SL metrics as the
   customer provisions them. The provider abstraction already supports per-tree choice;
   extend it to **per-metric choice** (a tree where 12 nodes come from the SL and 3
   from direct SQL is a normal migration state, and `source:` already carries a
   provider-qualified path).
3. **Keep B for the open-core / self-hosted mode** that builds bottom-up adoption with
   dbt Core users.

Future connectors when customer demand shows up, in likely order: Databricks
Unity Catalog metric views (Narrative is already on Databricks; this is the SL-shaped
thing their platform ships natively), Cube, Looker API, Snowflake semantic views.
All of them slot in as `BaseDataFetcher` implementations plus a scaffolder each.

---

## 4. Where does the tree live?

Today: a YAML file we wrote, in our repo. For a product there are three candidate homes:

**(a) Breakdown-hosted (our DB, edited in our UI).**
Best UX, worst governance — the tree drifts from the dbt repo silently, and the
customer's analytics engineers can't review changes in PRs.

**(b) In the customer's dbt repo as `meta.breakdown` annotations on metrics.**
Priors, lags, seasonality, and parent overrides ride along on the governed metric
definitions; dbt's manifest carries them; Breakdown reads the manifest via the
Discovery API or repo checkout. The pilot already proved the motion: our deliverable
to Narrative *is a PR to `NarrativeDBT_Databricks`*. Sales artifact = code review the
customer's own team approves. This is unusually aligned incentives: the customer owns
their tree, we own the engine.

**(c) A standalone `breakdown.yml` in the customer's repo** (what we're building for
the pilot). Simpler than (b), still versioned/reviewable, but duplicates metric names
and won't be validated by dbt's parser.

Recommendation: **(c) now, (b) as the product's governed mode**, (a) only as a cache
of (b)/(c) for the UI — never the source of truth. The scaffolder (from SL metadata,
§3A) emits (c); a later `breakdown sync` promotes annotations into (b).

Metric-level metadata the schema must grow, straight from pilot scars:
`grain: {day|week|month}` with per-metric floors (ratio metrics: month), `kind:
{flow|stock|rate}` (stocks like `mrr_usd` are display-only, never formula inputs —
the MAX-vs-SUM cumulative trap), and explicit sign conventions (churn stored
negative is *why* `net_new_mrr` is all-plus-signs).

---

## 5. Reference architecture

```
customer stack                          breakdown
──────────────                          ─────────
dbt Cloud SL  ──┐                       ┌─────────────┐    ┌────────────┐
MetricFlow    ──┤   provider plugins    │  snapshot   │    │   engine   │
warehouse SQL ──┼──▶ (BaseDataFetcher) ─▶│  store      │───▶│ BSTS/RCA/  │
CSV / push    ──┘                       │ (parquet/   │    │  Shapley   │
                                        │  duckdb)    │    └────────────┘
tree definition                         └─────────────┘         │
(meta.breakdown / breakdown.yml) ──▶ parser/DAG ────────────────┘
```

The one new component is the **snapshot store**: fetch once per (metric, window,
grain), persist as parquet, refit BSTS against snapshots. This decouples engine
latency from SL latency, gives reproducible RCAs ("as of the data fetched at T"),
enables scheduled refresh + change detection (the future alerting feature), and keeps
us polite to customer warehouses. It also makes provider migration (§3, C→A) invisible
to the engine.

**Deployment modes**, sharing one codebase:
1. **CLI / open-core** (today's shape): local MetricFlow + direct SQL + CSV. Adoption
   engine for dbt Core users.
2. **SaaS**: SL service token or scoped warehouse creds, hosted UI, scheduled
   refresh. Secrets in a real secret manager, `${ENV}`-style references in all config.
3. **In-VPC** for the security-sensitive: same container, customer's cloud.

---

## 6. Onboarding as a product surface

The pilot's two days compress into a designed flow:

1. **Connect** — paste an SL service token (or warehouse creds / CSV). The
   **connection doctor** (§1) validates the full chain and, on failure, emits the
   exact remediation with links — including the "send this page to your dbt admin"
   artifact for the steps only they can do (service token creation, credential
   mapping).
2. **Scaffold** — enumerate metrics via the metadata API; propose a tree: derived/ratio
   `input_metrics` become formula edges; the customer (or we, in white-glove pilots)
   sketches the learned edges. Import assist from existing artifacts where feasible —
   Narrative's tree came from a Count canvas; a "paste your metric-tree doc/canvas
   export, get a draft YAML" step is cheap LLM leverage.
3. **Backfill** — snapshot 12–24 months daily; run grain/kind lints (cumulative
   detection, null-ratio days, month-only metrics in daily trees) *before* the first
   fit, so the first thing the customer sees is never a pathological posterior.
4. **First RCA** — target their North-Star flow metric over a window they already
   know the story for (Narrative: `net_new_mrr_usd`, June 2025 churn spike). The
   validation moment is Breakdown independently recovering a known answer — *then*
   they trust it on the unknown ones.

---

## 7. Phased roadmap

**Phase 0 — pilot hardening (now, days).** Env-var interpolation for provider
secrets; per-metric grain floors; the Narrative tree YAML on its branch; RCA on
`net_new_mrr_usd`. Everything here is also product code.

**Phase 1 — connectivity kit (weeks).** Connection doctor for dbt Cloud SL (auth
chain walk via Admin API — the pilot debugging session, productized); direct-SQL
provider with per-metric `source` mixing; CSV ingest; snapshot store; tree scaffolder
from SL metadata.

**Phase 2 — governed mode (month+).** `meta.breakdown` annotations + manifest
ingestion; `breakdown sync`; scheduled snapshot refresh; SaaS packaging with secret
manager; first non-dbt connector chosen by whoever the second design partner is.

**Explicitly deferred:** building our own metric definition language (we ride dbt's),
real-time/streaming grains, warehouse write-back.

---

## 8. Open questions

- **Pricing surface:** per-tree? per-metric-node? per-seat? The snapshot store makes
  per-node metering trivial if wanted.
- **How much white-glove is in the box?** Pilot experience says tree *scaffolding* is
  automatable but edge *choice* (what's a formula vs a learned edge, which lags) is
  consulting-shaped for now. That's fine — it's also how we learn what to automate.
- **Multi-tenancy of the engine:** PyMC fits are CPU-heavy; SaaS needs a fit queue
  and warm result cache (deep-linkable RCA runs already point this direction).
- **Does dbt's SL roadmap absorb any of this?** If dbt ships first-party
  metric-tree/attribution features, options C/D and the non-dbt connectors are the
  hedge; the Bayesian engine and priors/lags layer remain differentiated either way.

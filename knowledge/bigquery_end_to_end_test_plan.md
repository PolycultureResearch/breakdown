# Plan: stand up a real BigQuery and run breakdown against it end to end

Status: **plan, not executed.** Written 2026-08-13. Nothing in this document has
been run against a warehouse; everything marked *verified* was verified by
reading code or by running the generator offline, and everything else is called
out as such in [§8](#8-what-i-could-not-verify).

Companion to
[`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
(§5 execution, §10 correctness hazards — this is the BigQuery half of §10's
"open verification" list) and
[`white_cube_demo_plan.md`](white_cube_demo_plan.md) (the dataset).
Roadmap context: **2.10** (the `dbt` binding, shipped and Databricks-verified)
and **2.14** (differential verification, open).

---

## 0. Why this exists

The `dbt` provider has generated BigQuery SQL since 2.10 and has had a BigQuery
connector since 2026-08-11 (`breakdown/dbt_provider.py:164`). **Nothing has ever
executed against a real BigQuery.** Every BigQuery test in the repo is one of
two things:

- a **structural** assertion on the sqlglot parse tree — `tests/test_dbt_sql.py`
  lines 146, 233–330, 599–620;
- a **refusal-path or monkeypatched** connector test —
  `tests/test_dbt_provider.py:627–716`, where `bigquery.Client` and
  `dbapi.connect` are both replaced.

Neither touches Google. The roadmap's own 2.10 row states the rule —
*"generation and execution ship separately here, and a mapped dialect is not a
supported warehouse"* — and BigQuery has been on the wrong side of it since the
connector shipped.

That is not theoretical. On 2026-08-12 the day- and month-grain SQL was found
**malformed** (`DATE_TRUNC` arguments reversed against BigQuery's
expression-first signature), and the week override was wrong too for a
`TIMESTAMP` column — so BigQuery had *no* fully working grain, not one. It is
fixed (`breakdown/dbt_sql.py:94–99`) and structurally tested, but still never
executed. The Databricks port found two defects that **only** a warehouse could
find (Spark's `trunc` returning NULL for DAY; a double-quoted `"date"` being a
string literal). The base rate for "a dialect port has an execution-only defect"
in this codebase is 2 for 2.

A first client deployment on BigQuery is imminent. This is the pre-deployment
ritual.

**What this plan is not.** It is not a migration of the White Cube demo to
BigQuery. The demo stays on DuckDB + committed snapshots and should not move
([§7](#7-cleanup-and-what-to-keep)). BigQuery is a *throwaway* environment whose
only job is to make the generated SQL and the connector meet a real warehouse
once.

### 0.1 The afternoon, in order

Read the sections; this is the spine.

| | Step | Section | ~Time |
|---|---|---|---|
| 0 | `bq load` one trivial file into a **sandbox** project — decides whether a card is needed at all | §1.1, §8.7 | 10 min |
| 1 | Create the project (hyphen-free id), cap it, create a `US` dataset | §1.5, F3 | 15 min |
| 2 | `fake-companies export --format parquet`, `bq load` the 11 raw tables | §2.3 | 20 min |
| 3 | Add a `bigquery` output to `fake_companies/dbt/profiles.yml`; `dbt build` | §2.3, §2.4 | 45 min — **the four SQL fixes live here** |
| 4 | `dbt parse` → BigQuery `target/semantic_manifest.json` | §3.1 | 2 min |
| 5 | Write the probe tree: 5–8 nodes, each with its own `bind:`, one on a TIMESTAMP column | §3.1, F7 | 30 min |
| 6 | `gcloud auth application-default login` + `set-quota-project` + `dbt debug` | §4 step 0 | 10 min |
| 7 | `breakdown doctor` | §4 step 1 | 5 min |
| 8 | `breakdown serve --eager --no-snapshots`, RCA, slices | §4 steps 2–4 | 20 min |
| 9 | The `ISOWEEK` boundary query, and the three-tier differential against the committed snapshots | F8, §6 | 45 min |
| 10 | Amend this document with what happened; delete the project | §7 | 20 min |

**The one thing to do before anything else, if you want to fail cheap:** run
`breakdown doctor` with `provider: {type: dbt}` against the *existing DuckDB*
target. It reproduces §3's zero-bindings finding in thirty seconds, for free, on
your laptop, and it is the largest planning surprise in this document.

---

## 1. The GCP account: what is free, what bills, how to cap it

*Every figure in this section was read off Google's live pages on **2026-08-13**
and carries its URL. Prices change; re-check before putting a card down. Where
Google's own pages disagree with each other, that is recorded rather than
smoothed.*

### 1.1 The headline: you probably do not need a credit card at all

**The BigQuery sandbox still exists and is the recommended vehicle for this
exercise.** Verbatim:

> "The BigQuery sandbox lets you experience BigQuery **without providing a
> credit card or creating a billing account** for your project."
> — <https://docs.cloud.google.com/bigquery/docs/sandbox>

It grants the same free allowances as the paid free tier ("10 GB of active
storage and 1 TB of processed query data each month", same page), with three
restrictions that matter here:

| Sandbox restriction | Does it block this plan? |
|---|---|
| All tables, views and partitions **auto-expire after 60 days** | **No** — this is a throwaway environment. It is a feature. |
| **No DML** (`INSERT`/`UPDATE`/`DELETE`/`MERGE`) | **No** — `fake_companies` has no incremental models and no snapshots (§2.3). dbt's `view` and `table` materializations are DDL (`CREATE OR REPLACE VIEW` / `CREATE OR REPLACE TABLE … AS SELECT`), not DML. |
| No streaming data, no Data Transfer Service | **No** — batch load is the recommended path anyway (§1.3), and streaming is the *one* thing that would have cost money. |

**The one unverified dependency:** the sandbox limitations page does not mention
load jobs at all, and does not explicitly state that `bq load` works. It is
absent from the unsupported list, which strongly implies it does, but that is an
inference — see §8. **Settle it in five minutes**: create a sandbox project,
`bq load` one 200-byte parquet file, done. If load jobs turn out to be blocked,
fall back to §1.2.

### 1.2 If you do enable billing

The Free Trial is **$300 of credit over 90 days**, and *"during the sign up, you
must provide a credit card or other payment method"*
(<https://docs.cloud.google.com/free/docs/free-cloud-features>). The always-free
allowances survive it: *"These free usage limits are available during and after
the free trial period"* (<https://cloud.google.com/bigquery/pricing>).

**Isolate it.** Create a **new project** for this and nothing else, so the
teardown in §7.1 is a single `gcloud projects delete` and so no custom quota you
set here can throttle anything you care about.

### 1.3 The numbers, and whether White Cube fits

Always-free, verbatim from <https://cloud.google.com/bigquery/pricing>:

> "Storage — The first **10 GiB per month** is free."
> "Queries (analysis) — The first **1 TiB of query data processed per month** is
> free."

Scope: **per billing account**, monthly, **no rollover**. Every price row on the
pricing page carries the suffix *"per 1 month / account"*, and the free-tier
page states *"there are monthly usage limits that are calculated per billing
account"* and *"Free Tier limits aren't credit; they don't accumulate or roll
over."* (One of Google's own pages was paraphrased by a summarizer as "each
project receives 1 TB" — treat **per billing account** as the answer.)

Beyond the allowance, US multi-region on-demand:

| | US multi-region |
|---|---|
| Queries (on-demand), above 1 TiB | **$6.25 / TiB** |
| Active logical storage | **$0.02 / GiB-month** |
| Long-term logical storage (untouched 90 days) | $0.01 / GiB-month |

*Discrepancy worth knowing:* the pricing page's default rendering
(Iowa / `us-central1`) shows active logical storage at $0.000031507/GiB-hour =
**$0.023/GiB-month**, and its own worked example agrees ("For 1 TiB for a full
month, you pay $23.552 USD"). So `us-central1` currently reads *higher* than the
`US` multi-region. **Pin the region explicitly.** Use the `US` multi-region for
the dataset and set `location: US` in the dbt profile — which also avoids
failure **F6**.

**Does White Cube fit?** Comfortably, on both meters.

- **Storage:** ~1–1.5 GB estimated for raw + marts (§2.1). Against a 10 GiB
  free allowance that is ~10–15% of it, and free. Even if the estimate is off
  by 3×, it is free.
- **Queries:** the entire workload in §4 — a `doctor` cascade, a startup fetch
  over 26 months, an RCA and a few slice calls — reads a few hundred MB to a
  couple of GB of *referenced columns*. Against 1 TiB/month you could run the
  whole plan **several hundred times** and stay inside the free tier.

The per-query floor is what to actually watch, and it is trivially small:

> "Charges are rounded up to the nearest MB, with a **minimum 10 MB data
> processed per table referenced by the query, and with a minimum 10 MB data
> processed per query**." — <https://cloud.google.com/bigquery/pricing>

At $6.25/TiB, a 10 MB minimum query costs $0.00006. You would need ~175,000 of
them to consume the free TiB. And: *"You aren't charged for queries that return
an error or for queries that retrieve results from the cache."*

### 1.4 What could actually produce a bill

Ranked by likelihood for this workload:

1. **Streaming inserts / Storage Write API (REST) — the real trap.**
   `$0.01 / 200 MiB` with **no stated free allowance**, and *"Individual rows
   are calculated using a 1 KB minimum size."* Ten million small synthetic rows
   billed at a 1 KB floor is ~10 GiB of chargeable ingest for ~1 GB of data.
   **Never stream this dataset.** The gRPC Storage Write API is different
   ($0.025/GiB, first 2 TiB/month free), but you do not need it either.
2. **Batch loading is free**, verbatim: *"By default, you are not charged for
   batch loading data from Cloud Storage or from local files into BigQuery."*
   (Caveat on the same page: load jobs assigned to a *reservation* lose the free
   pool — do not create a reservation.) This is why route A in §2.3 stages
   parquet and uses `bq load`.
3. **Cross-region anything.** *"Cross-region load jobs are billed for network
   usage under Network Data Transfer SKUs"*, and the same for cross-region copy
   and extract. Keep the GCS bucket, the dataset and the jobs in one location.
4. **GCS staging** — free at this size. The always-free tier is *"5 GB-months of
   regional storage (US regions only)"*, restricted to `us-east1`, `us-west1`
   and `us-central1`; Standard storage otherwise is $0.02/GiB-month. A few
   hundred MB of ZSTD parquet is inside it — but note the free-GCS regions and
   the recommended `US` BigQuery multi-region are not the same thing, so item 3
   applies. If in doubt, load from **local files** (route C) and skip GCS
   entirely.
5. **Not a risk, recorded so nobody worries:** query-result egress is explicitly
   free (*"You are not charged for data extraction or data transfer when
   accessing query results in the Google Cloud console, BigQuery API, or any
   other clients"*); the Storage Read API used by fast pandas reads is
   `$1.10/TiB` with **300 TiB/month free**; BI Engine requires a reservation you
   would have to create deliberately.

*Could not verify:* materialized-view pricing as a standalone line item for
non-Omni BigQuery. Irrelevant here — the dbt project has none — but recorded.

### 1.5 How to cap it, and the thing most people get wrong

**A budget alert does not stop spend.** Verbatim, from
<https://docs.cloud.google.com/billing/docs/how-to/budgets>:

> "Setting an *alerts-only* budget *doesn't* automatically cap Google Cloud or
> Google Maps Platform usage or spending. … Budget alert emails might prompt you
> to take action to control your costs, but they don't automatically prevent the
> use or billing of your services."

**Spend-cap budgets** — which *do* pause usage — exist, but
<https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps> lists
only Gemini API, Gemini Enterprise Agent Platform, Cloud Run and Cloud Run
functions as eligible. **BigQuery is not.** So the ordinary billing controls are
notification-only for this service, and that is the fact worth carrying.

The actual hard cap is a **custom query quota**
(<https://docs.cloud.google.com/bigquery/docs/custom-quotas>):

- `QueryUsagePerDay` — project-level, aggregate across all users. Default
  **200 TiB/project/day**. `QueryUsagePerUserPerDay` defaults to unlimited.
- It is **proactive**: *"you can't run an 11 TB query if you have a 10 TB
  quota"* — the query is rejected before it runs, not billed and then noticed.
- Resets at **midnight Pacific**. *"When you request a lower quota, the change
  takes effect within a few minutes."*
- Console only — no `gcloud` equivalent is documented. Path, verbatim:
  **IAM & Admin > Quotas & System Limits** → filter Service = **BigQuery API**
  → select **Query usage per day** and **Query usage per day per user** →
  **Edit** → enter the value **in TiB** → **Submit request**. Needs
  `roles/servicemanagement.quotaAdmin`.
- Google's own honesty caveat: *"Custom quotas are approximate. … BigQuery might
  occasionally run a query that exceeds a quota."*

**The recommended belt-and-braces for this exercise, in order:**

1. Use the **sandbox** (§1.1). If that works, everything below is optional.
2. Otherwise: a **dedicated project**, so teardown is total.
3. Set `QueryUsagePerDay` to **1 TiB** on that project. This is the only control
   that actually refuses work, and 1 TiB is ~500× the whole plan's needs.
4. Set a **budget alert** at $1 / $5 / $10 (`gcloud billing budgets create`, or
   Billing → Budgets & alerts). Understand it as a smoke detector, not a
   sprinkler.
5. Per-query: `bq query --dry_run` gives a free byte estimate, and
   `--maximum_bytes_billed` fails a query before it incurs charges. Use
   `--dry_run` once on a statement pulled from `/metrics/{name}/query` (§4,
   step 2) to establish what a real breakdown query costs — that number turns
   **F11** from a worry into a fact.

---

## 2. Getting White Cube's data into BigQuery

### 2.1 What the dataset actually is

Verified by reading `fake_companies` on disk:

- The generator writes a **DuckDB file**, not parquet:
  `fake-companies generate --config configs/white_cube_b2c_app.yaml --out out/white_cube.duckdb`
  (`breakdown/demo/Makefile:44–46`). The file on disk today is **219 MB**.
- The scenario is `2024-06-01` + 790 days → `2026-07-30`
  (`fake_companies/configs/white_cube_b2c_app.yaml:20–22`), seed `20240601`,
  and generation is byte-deterministic from seed + config.
- **Raw** tables are ~10.3M rows, 99% of it in two: `product.events`
  (8,604,681) and `web.sessions` (1,478,015). Everything else is ≤55k rows.
  dbt then builds ~7.3M more rows of marts on top (`fct_activity_days` 3.8M,
  `fct_subscription_days` 1.9M, `fct_sessions` 1.5M).
- Estimated BigQuery logical footprint: **~1–1.5 GB** raw + marts. This is
  *inference* from column types in
  `fake_companies/src/fake_companies/output/schemas.py`, not a measurement.

Order of magnitude matters for §1: this is a **megabytes-to-low-gigabytes**
dataset. Storage is noise; the only thing worth thinking about is query bytes,
and §4 quantifies that.

### 2.2 The committed parquet snapshots are not the shortcut

`demo/.breakdown/snapshots` holds 40 committed parquet files, 540 KB total.
**They are the wrong artifact for this job, and assessing them is worth two
minutes because the mistake is tempting.**

They are not tables. They are *fetched metric series*, keyed
`<metric>__[by-<entity>__<dim>__]<grain>-<kind>__<start>__<end>.parquet` — e.g.
`net_new_mrr__week-flow__2024-06-01__2026-07-30.parquet`, 3.4 KB, ~113 weekly
rows. They are the **output** of the pipeline this plan is trying to exercise,
not an input to it. You cannot load them into BigQuery and get anything the
generator would have to aggregate.

They are, however, **exactly the right oracle**. They are the DuckDB-computed
answer for every metric × grain × window, committed to git, produced by the
`local`/MetricFlow path. That makes them the fixed side of the differential
check in [§6](#6-what-success-looks-like-as-evidence). Keep them; do not load
them.

### 2.3 Three routes, and the recommendation

**First, dispose of the tempting version of the question.** "Can we just point
the dbt project at a BigQuery profile and rebuild there instead of exporting
anything?" — **no, and not because of dbt.** dbt transforms; it does not move
data. The raw tables (`product.events`, `web.sessions`, `app_db.*`, `billing.*`,
`ad_platform.ad_spend`) exist only inside `white_cube.duckdb`, and
`sources.yml` points `source()` at them as ordinary tables, not at files. Some
export is unavoidable. What *is* true — and it is the useful half of the
instinct — is that once the ~10.3M **raw** rows are in BigQuery, the entire
modelled layer rebuilds there with `dbt build`, and that is the right way round.

So the choice is only about the **raw layer**. Marts have to be rebuilt in
BigQuery either way if you want `dbt parse` to write a semantic manifest whose
`node_relation` points at BigQuery relations — and it does, because
`dbt_bridge._relation()` composes `database.schema_name.alias` straight from
that artifact (`breakdown/dbt_bridge.py:157–167`).

| Route | What you load | dbt runs where | Verdict |
|---|---|---|---|
| **A** | The 11 raw + meta tables, as parquet → GCS → `bq load` | On BigQuery, full `dbt build` | **Recommended** |
| **B** | Nothing; export the built *marts* and load those | Not at all | Rejected |
| **C** | Raw tables via `bq load` from local parquet (no GCS) | On BigQuery | Fine, slightly slower |

**Route A, in detail.** `fake_companies` already ships a production-quality
exporter: `fake-companies export --db out/white_cube.duckdb --format parquet`
(`fake_companies/src/fake_companies/cli.py:42–52`), which does
`COPY (SELECT * FROM <fqn> ORDER BY 1) TO '<dest>' (FORMAT PARQUET, COMPRESSION ZSTD)`
per table (`fake_companies/src/fake_companies/output/export.py:16–38`). It
exports exactly `RAW_TABLES + META_TABLES`
(`fake_companies/src/fake_companies/output/schemas.py:242`) — **marts are
deliberately not exported**, which is the same judgement this plan reaches
independently. One file per table, named `<schema>.<name>.parquet`,
deterministically ordered.

*(Caution: the parquet files sitting in `fake_companies/out/export/` today are
212–528 bytes each and dated 2026-07-25. They are leftovers from an empty run,
not a White Cube export. Regenerate.)*

**Why route A over route B.** Route B — export the marts, skip dbt — looks
faster and is a trap. It bypasses `dbt build`, so nothing writes a BigQuery
`target/semantic_manifest.json`, so `dbt_bridge` has nothing to read, so
`doctor`'s first check fails and the whole cascade skips. You would be testing
`build_query` against hand-written bindings over hand-loaded tables, which
tests the SQL generator but not the *provider*, which is half of what is
unexercised. It also loses the dbt build itself as a signal: the four
DuckDB-isms in §2.4 are found by `dbt build` failing loudly on BigQuery, which
is a much better place to find them than in a breakdown stack trace.

**Why "point dbt at a BigQuery profile and rebuild" is the right instinct and
also not the whole story.** It is the right instinct: this project is unusually
portable. Verified by reading `fake_companies/dbt`:

- **No `packages.yml`, no `dependencies.yml`, no `dbt_packages/`, no
  `macros/`.** Nothing to re-pin for a BigQuery adapter. This is the single
  biggest portability win and it is rare.
- 20 models, 9 staging views + 11 marts tables, no incremental models, no
  snapshots, no seeds.
- All 11 `source()` calls resolve to real **tables**, not `read_parquet`
  external locations.
- Zero `::` casts, zero `SELECT * EXCLUDE/REPLACE`, zero `PRAGMA`, zero
  `strftime`/`epoch`/`datediff`, zero `TRY_CAST`, zero cross-database refs.

But the profile is not target-switchable as it stands.
`fake_companies/dbt/profiles.yml:2` hardcodes `target: dev` and `dev` is the
only output; only the DuckDB *path* is env-configurable, via `FAKE_DB`
(`profiles.yml:6`). The clean edit is two lines — add a `bigquery` output and
make `target: "{{ env_var('DBT_TARGET', 'dev') }}"` — which keeps the DuckDB
path that CI, `demo/Makefile` and `scripts/verify_white_cube_stories.py` all
depend on working untouched. `DBT_PROFILES_DIR` is already threaded through
every caller (`demo/Makefile:35`). You also need `dbt-bigquery` in *that*
venv's `dbt` extra (`fake_companies/pyproject.toml:22–25`) — remember the two
interpreters: dbt-core does not run on breakdown's Python 3.14, so the whole
dbt toolchain lives in `fake_companies`' 3.13 environment
(`demo/README.md:43–53`).

### 2.4 The four SQL files that will not compile, and the ninefold `QUALIFY` question

Found by grepping every model. These are `dbt build` failures on BigQuery, not
breakdown failures — but they are on the critical path and budgeting for them is
the difference between an afternoon and a day.

1. **`arg_max` / `arg_min`** — BigQuery has neither.
   `fct_activity_days.sql:22–24` (three uses) and `fct_trials.sql:22` (one).
   Swap to `MAX_BY` / `MIN_BY`.
2. **The time spine.** `metricflow_time_spine.sql:4–13` uses
   `unnest(generate_series(date '2023-01-01', date '2027-12-31', interval '1 day'))`
   — three incompatibilities in five lines: `generate_series` over dates,
   `unnest` in the SELECT list (BigQuery allows `UNNEST` only in `FROM`), and
   `interval '1 day'`. Rewrite as
   `SELECT date_day FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2023-01-01', DATE '2027-12-31', INTERVAL 1 DAY)) AS date_day`.
3. **`cast(... as varchar)`** in the two surrogate keys —
   `fct_activity_days.sql:31` and `fct_subscription_days.sql:82–83`. `VARCHAR`
   is not a BigQuery type; use `STRING`. `||` itself is fine. These keys are
   load-bearing: they are the MetricFlow **primary entities** for
   `activity_days` and `subscription_days`, which is what
   `dbt_bridge._grain_key()` reads (`breakdown/dbt_bridge.py:176–187`) and what
   `doctor`'s grain assertion counts distinct values of.
4. **`QUALIFY` with no `WHERE`.** All nine staging models end with a bare
   `qualify row_number() over (partition by <pk> order by _loaded_at desc) = 1`
   and none has a `WHERE`/`GROUP BY`/`HAVING` in the same block. BigQuery
   supports `QUALIFY`; whether it still requires a companion clause is the open
   question. **I could not verify this** — see §8. The unconditionally safe move
   is a mechanical rewrite to a `row_number()` CTE + `where rn = 1` across nine
   near-identical files. Budget it; skip it if a one-model `dbt run` proves it
   unnecessary.

Two smaller items: a column literally named `date` in
`stg_ad_spend.sql:9` (`cast(date as date)`) — backtick it and stop wondering;
and `_loaded_at`, used for source freshness on 8 tables, should land in BigQuery
as `TIMESTAMP` rather than `DATETIME` or freshness will fail on a type mismatch.

### 2.5 The good news about `agg_time_dimension`

**Every MetricFlow time axis in this project is already a `DATE`.** Verified two
ways by the sub-agent that read the repo: the model SQL (each staging model
emits both the raw `TIMESTAMP` and a `cast(... as date)` derived column, and
only the DATE column is ever wired into `agg_time_dimension`) and the
materialized types in the built DuckDB. All ten semantic models plus the spine.

That is a relief and a problem. It means the `DATE_TRUNC` port is on easy mode —
and it means **White Cube cannot exercise the `CAST(col AS DATE)` that the
2026-08-12 fix was actually about**. The cast exists for the `TIMESTAMP`
`agg_time_dimension`, which is the common shape in real dbt projects and is
absent here. See failure **F7** in [§5](#5-what-will-break-named-in-advance) for
the deliberate extra binding that closes this.

---

## 3. The finding that reorders everything: White Cube produces **zero** dbt bindings

**Verified by running the bridge against the real manifest on disk** at
`fake_companies/dbt/target/semantic_manifest.json`:

```
BINDINGS: 0
FORMULAS: 5   (customer_churn_rate, net_new_mrr, payment_failure_rate,
               trial_conversion_rate, visit_signup_rate)
SKIPPED: 22
```

Nineteen of the twenty-two skips read:

> `its measure input declares 'fill_nulls_with: 0', which substitutes a value on
> every period the measure has no rows in; breakdown gap-fills from the node's
> 'kind' instead …`

`fake_companies/dbt/models/semantic/metrics.yml` sets `fill_nulls_with: 0` on
**19 metrics**, and `dbt_bridge._unsupported_semantics()` refuses every one by
name (`breakdown/dbt_bridge.py:311–320`). The remaining three skips are the
`nullif()` ARPU metrics (`breakdown/formula.py` has no function calls) and
`wau`, a cumulative metric.

**Consequence: `provider: dbt` cannot serve White Cube at all — on any
warehouse.** `demo/white_cube_tree.yml:22–24` declares `provider: type: local`
for exactly this reason, whether or not anyone noticed. Repointing it at
BigQuery by changing `type: local` → `type: dbt` fails `doctor`'s **"tree
metrics bind"** check for all 18 non-formula nodes and stops the cascade before
the connection is ever proven useful.

There is a second, independent blocker in the same tree. Its `dimensions:`
blocks use MetricFlow's `<entity>__<dimension>` identifiers — `mrr_movement__plan`,
`session__country`, `user__country` — which are documented as not guessable from
the YAML (`demo/AUTHORING.md:20–31`). `dbt_bridge._categorical_dimensions()`
names bindings' dimensions by the **bare** dimension name and is
**same-relation only**, refusing anything reached through an entity join
(`breakdown/dbt_bridge.py:202–214`). So even with bindings, every declared
dimension would fail `doctor`'s **"declared dimensions exist"** check, and
`user__country` — which reaches `fct_mrr_movements` through the `user` foreign
entity into `dim_users`, and is the demo's best slice — is simply unavailable on
the `dbt` provider.

### 3.1 The fix: a BigQuery tree of hand-written `bind:` blocks

This is what the `bind:` override exists for, and both the server and `doctor`
honour it: `doctor` builds `overrides = {provider_query_name("dbt", m): m.bind …}`
and passes them to `fetcher_from_project`
(`breakdown/doctor.py:521–533`), which does `bindings.update(overrides)`
(`breakdown/dbt_provider.py:630–632`). A node with a `bind:` block needs nothing
from the semantic manifest at all.

So: **a new, small, throwaway tree** — call it `knowledge/bigquery_probe_tree.yml`
or keep it out of the repo entirely — with `provider: {type: dbt, project_path: …}`
and 5–8 nodes, each carrying its own `bind:`. Deliberately not 18. The goal is
to exercise every code path once, not to reproduce the demo:

| Node | Why it is in the probe tree |
|---|---|
| `marketing_spend` (day, sum, `fct_...`/`stg_ad_spend`, dim `channel`) | the simplest possible flow; proves day-grain `DATE_TRUNC` |
| `sessions` (day, sum or count, dim `country`, `device`) | a 1.5M-row table; proves the window predicate prunes |
| `new_mrr` (week, sum, dim `plan`) | proves **week** grain — the `ISOWEEK` check (F8) |
| `active_subscriptions` (week, `count_distinct` + `entity_grain: {resolve: last}`) | the only path that reaches `build_resolved_slice_query` and `build_multivalue_assertion` — and the only one where `doctor`'s **entity grain resolves** check does anything |
| one node bound to a **`TIMESTAMP`** column | closes F7 — see below |
| `net_new_mrr` or any `formula:` node | proves formula nodes need no binding |

The manifest is still needed — `doctor`'s first check is
`os.path.exists(manifest_path(project))` (`breakdown/doctor.py:507–520`) and it
stops the whole cascade if absent. `dbt parse` against the BigQuery target is
enough; it need not translate a single metric.

The TIMESTAMP node is the one worth insisting on. Bind it directly to a raw
table whose time column is a `TIMESTAMP` — `product.events.occurred_at` or
`app_db.users.created_at` — via `bind.relation` + `bind.time_column`. That is
the only way this dataset can test the `CAST(… AS DATE)` the fix was for, and
it costs one YAML block.

---

## 4. What to run, in order, and what each step proves

Assume: GCP project created and capped (§1), raw parquet loaded, `dbt build`
green on the BigQuery target, `dbt parse` has written
`target/semantic_manifest.json`, `pip install 'metric-breakdown[bigquery,dbt-bridge]'`,
and the probe tree from §3.1 exists.

### Step 0 — prove the credential outside breakdown

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project <PROJECT_ID>
bq query --use_legacy_sql=false 'SELECT 1'
cd fake_companies/dbt && DBT_TARGET=bigquery dbt debug
```

**Proves:** that any later failure is breakdown's, not Google's. Skipping this
step is how you spend an hour reading `dbt_provider.py` over an unset quota
project. `_connect_bigquery` with `method: oauth` reads Application Default
Credentials and nothing else (`breakdown/dbt_provider.py:179–207`), so `bq
query` and breakdown share one credential exactly.

### Step 1 — `breakdown doctor`

```bash
breakdown doctor --tree knowledge/bigquery_probe_tree.yml \
  --start-date 2026-07-01 --end-date 2026-07-30
```

`doctor` is a **cascade of skips** and the shape of its output is the
diagnostic. In order, for `provider: dbt` (`breakdown/doctor.py:806–878` for the
outer sequence, `465–653` for the `dbt` chain):

| # | Check | Source | A failure here means |
|---|---|---|---|
| 1 | `tree file` | `doctor.py:53–57` | wrong path. Nothing else runs. |
| 2 | `provider env vars` | `doctor.py:69–88` | an unset `${VAR}` in the provider block, reported *all at once* before the Pydantic parse |
| 3 | `tree parses` | `doctor.py:90–100` | YAML/schema error; prints metric count and provider type |
| 4 | `dbt-bridge extra installed` | `doctor.py:136–159` | **only checks `sqlglot`** (`data_fetch.py:88–94`). It does **not** check `google-cloud-bigquery`. See F4. |
| 5 | `semantic manifest` | `doctor.py:498–553` | no `dbt_project.yml`, or no `target/semantic_manifest.json` → run `dbt parse`. Reports how many metrics bound. |
| 6 | `dbt profile` | `doctor.py:555–562` | prints `target 'x' -> bigquery (sqlglot dialect 'bigquery')`. **If the dialect prints `generic`, stop** — `ADAPTER_DIALECTS` did not match and every truncation is the portable form BigQuery rejects. |
| 7 | `warehouse connection` | `doctor.py:564–577` | `connect_from_profile(out).close()` — the real connector, not a lookalike. This is the first line of code in the repo that has ever talked to BigQuery. |
| 8 | `tree metrics bind` | `doctor.py:579–595` | a metric resolved to neither the manifest nor a `bind:` block — §3's failure if you skipped the probe tree |
| 9 | `declared dimensions exist` | `doctor.py:597–616` | a `dimensions:` entry naming something the binding does not expose. Without this the failure is a 500 on the first slice click. |
| 10 | `grain claims hold` | `doctor.py:618–649` | **the first executed query per metric**: `COUNT(*)` vs `COUNT(DISTINCT grain_key)`, bounded to the probe window. Unequal = fan-out, every aggregate silently multiplied. |
| 11 | `entity grain resolves` | `doctor.py:656–724` | runs `build_multivalue_assertion` for each non-additive metric × declared dimension. `skip` if no `count_distinct` node — which is why the probe tree has one. |
| 12 | `fit readiness` + `history headroom` | `doctor.py:727–792` | only runs when **both** dates are explicit and nothing above failed. Fetches **every metric end to end** through the real provider path — this is a full `build_query` execution per node. |

Two operational notes. `--start-date`/`--end-date` default to a deliberately
tiny 7-day probe window (`doctor.py:881–894`), and the sampled checks
report which window they used (`doctor.py:119–129`) — "checked over these dates"
is not "checked". And `check_fit_readiness` is where the bytes are: it is a full
`fetch_metric` per node, so run it once against the real analysis window
(`2024-06-01`→`2026-07-30`) and otherwise keep the probe window small.

**Expected result:** all pass, `entity grain resolves` reporting a count rather
than a skip.

### Step 2 — the startup fetch

```bash
breakdown serve --tree knowledge/bigquery_probe_tree.yml \
  --start-date 2024-06-01 --end-date 2026-07-30 \
  --eager --no-snapshots --port 9090
```

`--no-snapshots` and `--eager` are both load-bearing; see **F1**. `--eager`
forces the fetch at startup instead of on first use, so a failure lands in the
terminal rather than in a browser tab.

**Proves:** `build_query` at each grain against real data, `_frame`'s column
lowercasing (`dbt_provider.py:308–317`), `_to_naive_dates`, `_floor_labels`,
`_align_to_spine`. **Read the log, not just the exit code** — `_floor_labels`
warns rather than fails when a returned label was not on a period start
(`data_fetch.py:187–202`), and that warning is the *only* in-band signal that
BigQuery and the engine disagree about where a week begins.

Then:

```bash
curl -s localhost:9090/metrics/new_mrr/query | jq -r '.sql, .executed'
```

Prints the statement that actually ran (`executed: true`) — roadmap 2.11. Paste
it into the BigQuery console and confirm the dry-run byte estimate is what you
expect.

### Step 3 — RCA

```bash
curl -s -X POST 'localhost:9090/rca/net_new_mrr' \
  -H 'content-type: application/json' \
  -d '{"start_date":"2026-05-01","end_date":"2026-07-30"}' | jq
```

**Proves:** on-demand fits over BigQuery-sourced series, and — the part worth
watching — that the values are **floats**. `_align_to_spine` does
`.astype(float)` (`data_fetch.py:267`), which I verified handles the
`decimal.Decimal` that BigQuery's `NUMERIC`/`BIGNUMERIC` returns through DBAPI.
A failure here that is *not* an exception is the thing to fear: an RCA that
returns but ranks nothing.

### Step 4 — slicing

```bash
curl -s 'localhost:9090/rca/new_mrr/slices?dimension=plan&…' | jq
```

**Proves:** `build_query` with a `dimension`, and on the `count_distinct` node,
`build_resolved_slice_query` — the one builder with a window function, a
subquery and internal *unquoted* aliases, added specifically because a quoted
`"date"` is a string literal on BigQuery (`dbt_sql.py:328–334`). That comment
was written from the Databricks port; BigQuery has never confirmed it.

Also exercise **entity flows** (`build_entity_flow_query`) if the probe tree's
`count_distinct` node declares `entity_grain` — it is the only `FULL OUTER JOIN`
the generator emits.

---

## 5. What will break, named in advance

Derived from the code, most-likely first. For each: how it presents, and how to
tell it from a configuration mistake.

### F1 — You test parquet and think you tested BigQuery *(highest probability, lowest drama)*

`--snapshot-dir` defaults to `.breakdown/snapshots` next to the tree. Put the
probe tree in `demo/` and every series is served from the committed White Cube
parquet without a single byte leaving your laptop, and everything looks
wonderful.

*Presents as:* a suspiciously fast startup, and `/metrics/{name}/query`
returning `executed: false`.

*Mitigating fact, which you must not rely on:* snapshot records carry a
`definition_sha`, and `null` versus a sha is a deliberate mismatch that forces a
refetch (`breakdown/snapshots.py:263–305`). The existing snapshots were written
by the `local` provider, which has no per-metric definition and so stored
`null`; the `dbt` provider fingerprints its `bindings`. So they *should* miss.
But that mechanism is itself under test here. **Use `--no-snapshots`, and put
the probe tree somewhere with no `.breakdown/` beside it.**

*Tell it apart from a config mistake:* `executed: false` in the provenance
response is unambiguous.

### F2 — Zero bindings

Covered in §3. *Presents as:* `[FAIL] tree metrics bind — 18 metric(s) not in
the manifest`, followed by three skips. It is **not** a BigQuery problem and
will reproduce identically against DuckDB — which is the cheap way to confirm
it: run `breakdown doctor` with `type: dbt` against the *existing* DuckDB target
before you spend anything on GCP.

### F3 — A hyphenated GCP project id in the relation

`dbt_bridge._relation()` composes `database.schema_name.alias` joined by dots
and **unquoted**, explicitly declining to reuse dbt's own adapter-quoted
`relation_name` (`breakdown/dbt_bridge.py:157–167`). On BigQuery `database` is
the GCP project id, and default project ids contain hyphens.

I generated the SQL to see exactly what BigQuery would be handed (verified,
offline):

```sql
SELECT
  DATE_TRUNC(CAST(bd_fact.movement_at AS DATE), DAY) AS `date`,
  SUM(bd_fact.mrr_amount) AS `value`
FROM white-cube-demo.dbt_white_cube.fct_mrr_movements AS bd_fact
WHERE
  bd_fact.movement_at >= '2024-06-01' AND bd_fact.movement_at < '2026-07-31'
GROUP BY 1 ORDER BY 1
```

Per Google's lexical documentation, an unquoted identifier **may** contain
dashes when it is the *first* part of a table path in a `FROM` or `TABLE` clause
— which is precisely this case — and dashes are *not* supported in dataset
names. So this most likely parses. But it is the single most likely parse-level
surprise, it costs one query to settle, and the mitigation is free:
**create the GCP project with a hyphen-free id** (e.g. `breakdownbqprobe`).

*Presents as:* `Syntax error: Unexpected "-"` or `Invalid project ID` on every
query, including `doctor`'s grain assertion — so it fails at check 10, uniformly,
never intermittently.

*Tell it apart from a config mistake:* a permissions or dataset-name error names
the resource; this one names a character.

### F4 — A missing driver wearing a connection failure's remediation

`check_provider_extra("dbt")` resolves to the `dbt-bridge` extra and probes for
**`sqlglot` only** (`breakdown/data_fetch.py:88–94`). `google-cloud-bigquery` is
a separate `bigquery` extra (`pyproject.toml:108–114`) that nothing checks. So
without it, `doctor` prints `[PASS] dbt-bridge extra installed`, then fails at
check 7 with:

```
[FAIL] warehouse connection — MissingProviderExtra: provider type 'dbt' with a
       bigquery target needs the bigquery extra: pip install 'metric-breakdown[bigquery]'
       The connection comes from the dbt project's own profiles.yml, so
       `dbt debug` in that project tests the same credentials.
```

The *detail* is right and the *remediation* is wrong — `dbt debug` will pass,
because dbt has `dbt-bigquery` and breakdown does not.

*Tell it apart:* the exception type name is printed
(`doctor.py:571`, `f"{type(e).__name__}: {e}"`). `MissingProviderExtra` means
install something; `DefaultCredentialsError`, `Forbidden`, `NotFound` mean
configure something.

### F5 — The three auth methods, none of which has ever run

`_connect_bigquery` (`breakdown/dbt_provider.py:164–220`) branches on the
profile's `method`. Each has a distinct predicted failure:

**`oauth` (ADC) — recommended for this exercise.** `credentials` stays `None`
and the client finds ADC itself. The classic failure is a user credential with
no quota project: BigQuery returns `403` with a message about a user project,
which reads nothing like "you forgot a gcloud flag". Step 0's
`set-quota-project` is the whole fix. Also note `google.auth` will happily pick
up a `GOOGLE_APPLICATION_CREDENTIALS` env var left over from something else and
authenticate as a *different principal* — the code refuses an unknown `method`
by name precisely because "quietly authenticating as somebody else is worse than
a stop" (`dbt_provider.py:200–207`), but it cannot see that.

**`service-account`.** `keyfile` must be present or you get a clean
`DbtProfileError`. `from_service_account_file` is called **without `scopes=`**
(`dbt_provider.py:189`); `google.cloud.client` applies `with_scopes_if_required`
during `Client` construction, so this should be fine — but it is unexercised, so
watch for a scope error rather than assuming.

**`service-account-json` — the one I predict breaks.**
`_render()` resolves `{{ env_var(...) }}` **only on string values, and only at
the top level of the output dict**: `if not isinstance(value, str): return value`
(`breakdown/dbt_provider.py:62–64`). dbt's canonical `keyfile_json` is a
*nested mapping* whose fields are usually themselves `env_var()` calls. That
mapping is `isinstance(value, dict)`, so `_render` returns it untouched, Jinja
and all, and `from_service_account_info` is handed literal
`"{{ env_var('GCP_PRIVATE_KEY') }}"` as a private key.

*Presents as:* a cryptography/`ValueError` about an unparseable PEM key or
`("No key could be detected.",)` — an error that names a *key format* problem
when the actual problem is a *templating* one.

*Tell it apart:* set the env var to garbage and re-run. If the error is
identical, the env var is not being read at all, and that is F5.

**Recommendation: use `oauth` for the probe run.** It has the fewest moving
parts and the fewest untested lines. Then do one deliberate `service-account`
run, because that is what a client deployment will actually use.

### F6 — `location` mismatch

`bigquery.Client(..., location=out.get("location"))`
(`breakdown/dbt_provider.py:209–217`). dbt BigQuery profiles conventionally set
`location: US`. If you create the dataset in a *region* (`us-central1`) and the
profile says the *multi-region* `US`, the job is submitted to the wrong location
and BigQuery reports the dataset as not found.

*Presents as:* `NotFound: 404 Not found: Dataset <project>:<dataset> was not
found in location US` — from `doctor` check 10, after the connection has
already passed, which is confusing because the connection "worked".

*Tell it apart from a genuinely missing dataset:* the message names a location.
A missing dataset does not. Fix: create everything in the `US` multi-region and
set `location: US`. Also relevant to §1 — the free storage tier and pricing are
region-dependent.

### F7 — The `CAST(col AS DATE)` that White Cube cannot exercise

The whole point of the 2026-08-12 fix (`breakdown/dbt_sql.py:80–99`) is that a
dbt `agg_time_dimension` is *often* a `TIMESTAMP`, and BigQuery's `DATE_TRUNC`
takes a `DATE`. Every White Cube time axis is already a `DATE` (§2.5). **So the
default probe proves nothing about the fix that motivated this whole exercise.**

*Mitigation, not a prediction:* the extra `bind:` block from §3.1, pointed at a
raw `TIMESTAMP` column. Then check two things: that the query runs at all, and
that the bucket boundary is where you think it is. `CAST(TIMESTAMP AS DATE)` in
BigQuery is **UTC**, and the window predicate `col >= '2024-06-01'` coerces the
string literal to a UTC timestamp — so the two are consistent *with each other*.
They are consistent with DuckDB only if the load landed naive DuckDB timestamps
as UTC. Load `_loaded_at` and every raw timestamp as `TIMESTAMP` (UTC), not
`DATETIME`, and verify one row either side of a midnight boundary.

### F8 — Does a BigQuery week agree with what the engine believes a week is?

This deserves a named check because **the engine is designed to hide the
disagreement.**

The engine's week is ISO Monday: `_FREQ = {"week": "W-MON"}`
(`breakdown/grains.py:36`), and period labels are period-start timestamps —
week = Monday (`grains.py` module docstring). BigQuery's `DATE_TRUNC` defaults
`WEEK` to **Sunday**, which is why the override spells `ISOWEEK`
(`dbt_sql.py:96`).

Now the trap. If the bucketing were wrong, `_floor_labels` would relabel each
Sunday to the previous Monday, one label per week, landing cleanly on the spine
— and the shift in each bucket's *composition* would be invisible
(`dbt_sql.py:43–52`, and `semantic_layer_connectivity_design.md` §10.3 says the
same). The **only** in-band signals are:

1. the `_floor_labels` warning, `"week-grain labels were not on period starts"`
   (`data_fetch.py:194–199`) — which fires if BigQuery returns Sundays but is
   silent if it returns Mondays;
2. the numbers themselves, against DuckDB — §6.

**The explicit check, worth running by hand before trusting anything:**

```sql
SELECT d,
       DATE_TRUNC(d, ISOWEEK) AS isoweek_start,
       DATE_TRUNC(d, WEEK)    AS week_start
FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2026-06-28', DATE '2026-07-06')) AS d
```

`2026-06-28` is a Sunday. `isoweek_start` must read `2026-06-22` for it and
`2026-06-29` for Monday the 29th onward; `week_start` will differ, and seeing
them differ is what proves the override is doing work rather than being a no-op.
Run the same range in DuckDB with `date_trunc('week', d)` and require the ISO
column to match exactly. **I could not verify BigQuery's `ISOWEEK` semantics
from Google's live documentation today** (§8) — this query is the settlement.

Cross-year is the sharp edge: run `2026-12-28`→`2027-01-04` too. ISO week 53 vs
week 1 is where a truncation that is "Monday-ish" rather than genuinely ISO will
show itself.

### F9 — Quoted aliases *(verified — not a risk)*

`build_query` emits `AS "date"`, `AS "slice"`, `AS "value"`, and
`build_grain_assertion` emits `AS "rows"` / `AS "distinct_keys"`
(`dbt_sql.py:218, 234, 246, 545–549`). A double-quoted string is a **string
literal** on BigQuery, so this looked dangerous.

I generated the output: sqlglot renders them as **backticks** —
``AS `date` ``, ``AS `rows` `` — for the BigQuery dialect. Not a risk. Recorded
because it is the exact class of defect the Databricks port found, and someone
will otherwise re-derive the worry.

### F10 — `NUMERIC` → `decimal.Decimal` *(verified — not a risk)*

BigQuery returns `NUMERIC`/`BIGNUMERIC` as Python `Decimal` through DBAPI, which
would give a pandas column of dtype `object`. Both paths coerce:
`_align_to_spine` does `.astype(float)` (`data_fetch.py:267`) and the sliced
path does the same (`dbt_provider.py:481`). Recorded as checked, not as a
worry — though a *very* large `BIGNUMERIC` would lose precision silently, which
this dataset will not produce.

### F11 — Bytes billed by `doctor`'s own checks

`doctor` runs two diagnostic queries per metric (grain assertion, multi-value
assertion) plus a full `fetch_metric` per metric under `fit readiness`. The
diagnostics are window-bounded when you pass dates (`dbt_sql.py:362–379`), and
deliberately so — but `_bounded` returns the query **unchanged** when either
date is `None`, i.e. a full table scan per metric. `check_fit_readiness` is not
bounded to a probe window at all; it uses the analysis window you passed.

BigQuery bills bytes read from *referenced columns*, and there is a per-table
minimum per query. On this dataset that is small — but establish the number with
`bq query --dry_run` on one generated statement before running `doctor` in a
loop, and see §1 for the cap that makes the question moot.

### F12 — Everything the generator refuses, refusing correctly

`agg: last`, joined dimensions under entity-grain resolution, and entity flows
on a joined dimension all raise `UnsupportedBinding`
(`dbt_sql.py:201–210, 313–321, 419–425`). These are *supposed* to fail. If one
of them silently succeeds on BigQuery, that is a finding.

---

## 6. What success looks like as evidence

"It worked" is not the deliverable. The deliverable is **agreement**, and the
repo already has the concept: roadmap **2.14**, `--verify-against-metricflow`.
This is the same shape with DuckDB standing in for MetricFlow.

### 6.1 The differential, and why the snapshots are the right oracle

`demo/.breakdown/snapshots` is a committed, DuckDB-computed answer for every
White Cube metric × grain × window, produced by the `local`/MetricFlow path.
Both sides consume the **same generated rows** — generation is byte-deterministic
from seed + config (`fake_companies/AGENTS.md`), and route A loads rather than
regenerates. So any disagreement is a *code* disagreement, which is exactly what
this exercise is for.

Three tiers, in increasing strength:

**Tier 1 — totals.** For each probe metric, at its own grain, over
`2024-06-01`→`2026-07-30`: BigQuery's sum of `value` versus the parquet
snapshot's. Require **exact** equality for integer-valued metrics
(`sessions`, `new_subscriptions`) and equality to within float representation
for currency. A discrepancy of a few tenths of a percent is a **window
boundary** or a **timezone** difference, not noise — chase it.

**Tier 2 — per-period.** Join the two series on `date` and require every period
to match. This is what catches the week question: a bucketing shift changes
individual weeks while leaving the total identical, because every row is still
counted exactly once. **A tier-1-only check would pass a Sunday-vs-Monday week
bug outright.** That is the whole reason tier 2 exists.

**Tier 3 — slices.** Per `(date, slice)`, and the residual: `Σ slices` versus
the unsliced metric. For the `count_distinct` node with
`entity_grain: {resolve: last}` this must be **exact** — that is the claim
`slice_additivity()` makes (`dbt_provider.py:499–504`, "verified against both
DuckDB and a real Databricks warehouse"), and BigQuery would be the third.

### 6.2 Evidence that is not a number

- **`doctor` output pasted verbatim**, all `[PASS]`, with the `dbt profile` line
  showing `-> bigquery (sqlglot dialect 'bigquery')`.
- **The generated SQL** from `/metrics/{name}/query` for one day-grain, one
  week-grain and one sliced query, and the BigQuery job ids they ran as.
- **The log** from the startup fetch, specifically the **absence** of the
  `_floor_labels` "labels were not on period starts" warning at week grain, and
  the absence of the `_to_naive_dates` timezone warning if you bound only DATE
  columns (its **presence** on the TIMESTAMP node is expected and correct).
- **The `ISOWEEK` boundary query** from F8, both the June and the New Year
  ranges, side by side with DuckDB.

### 6.3 What would make this a failure worth shipping a fix for

Any of: a grain that does not execute; a per-period disagreement with DuckDB; a
`count_distinct` slice residual that is non-zero under `resolve: last`; an auth
method that cannot be made to work from a well-formed dbt profile. Each of those
is a 2.10 follow-up with a roadmap row, not a footnote.

---

## 7. Cleanup, and what to keep

### 7.1 Torn down

**Delete the GCP project**, not the dataset — `gcloud projects delete <id>`.
Deleting the project stops every possible meter in one action and removes the
ability to accrue anything by accident later; deleting a dataset leaves a
project that can still be billed for something you forgot. Empty the GCS staging
bucket first if you used route A.

If you took the sandbox route (§1.1) this is belt-and-braces: sandbox tables
auto-expire after 60 days and there is no billing account to charge. Delete the
project anyway, so nobody finds a half-loaded White Cube in a console a year
from now and wonders whether it is load-bearing.

**Do not migrate the demo.** `demo/white_cube_tree.yml` stays `provider: local`
over DuckDB with committed snapshots. The deployed image has no provider at all
and boots in seconds (`demo/README.md:32–41`); making it depend on a warehouse
would trade the demo's best property for nothing.

### 7.2 Kept

1. **This document, amended in place** with what actually happened — the
   `[PASS]`/`[FAIL]` list, the numbers, and the failures that were not
   predicted here. The unpredicted ones are the valuable output.
2. **The probe tree**, as `tests/fixtures/` or in this document verbatim. It is
   the cheapest possible reproduction for the next warehouse port, and its shape
   (one node per code path, not one node per metric) is the reusable idea.
3. **The `bigquery` output in `fake_companies/dbt/profiles.yml`** plus the four
   SQL fixes from §2.4, upstreamed. `MAX_BY`/`MIN_BY`, `STRING` instead of
   `VARCHAR`, and `GENERATE_DATE_ARRAY` are all *also valid on DuckDB*, so the
   project becomes dual-warehouse at no cost to the existing path. That is worth
   a PR to `fake_companies` regardless of what this exercise concludes.
4. **New structural tests** for anything found — in `tests/test_dbt_sql.py`,
   parsing the generated SQL in the BigQuery dialect and asserting on the parse
   tree. That is the discipline the 2026-08-12 correction established and it is
   what makes a warehouse finding permanent without a warehouse.

### 7.3 Should there be a BigQuery-backed CI job?

**Recommendation: no. Keep this a manual pre-deployment ritual, and write down
when to repeat it.**

The honest arguments each way:

*For.* The query cost genuinely is negligible (§1, §4) — a fixture dataset of a
few thousand rows would consume a rounding error of the monthly free allowance.
The `local`/MetricFlow path cannot be tested on Python 3.14 at all, so CI's
coverage of the semantic-layer story is already thinner than it looks.

*Against, and this decides it.* A warehouse CI job needs a **long-lived service
account key** in GitHub secrets, on a public repo, for an account that can
create jobs in a billing-enabled project. That is a standing credential-exposure
liability traded for a test that would have caught the 2026-08-12 defect — which
the *structural* tests now also catch, for free, offline, on every push. It also
introduces a class of flake (auth expiry, quota, region availability) that fails
`main` for reasons unrelated to the change, and this repo's CI is currently
honest about what it proves.

The middle path, if the appetite exists later: a **manually-dispatched**
workflow (`workflow_dispatch`, never `on: push`) using Workload Identity
Federation rather than a key, against a tiny committed fixture. That gets the
button without the standing secret. It is not worth building before this
exercise has been done once by hand.

**When to repeat it manually:** before any client deployment on BigQuery; when
`_TRUNC_OVERRIDES`, `_parse_dialect` or `ADAPTER_DIALECTS` change; when sqlglot
is upgraded across a major version; and when a new builder is added to
`dbt_sql.py` (the cross-builder test at `tests/test_dbt_sql.py:304` enumerates
them structurally, which is the offline half of the same guarantee).

---

## 8. What I could not verify

Stated plainly, because a confident guess here is worse than a gap. In the
register of `semantic_layer_connectivity_design.md` §10's own unverified list.

1. **BigQuery's `ISOWEEK` truncation semantics.** I could not read
   `cloud.google.com/bigquery/docs/reference/standard-sql/date_functions`
   today — the page renders as navigation to a fetcher and returned no function
   reference. I am relying on the existing comment at `dbt_sql.py:44–52` and on
   the ISO-8601 meaning of the word. **Settled by:** the boundary query in F8,
   run in both warehouses, including the New Year range. Two lines of output.
2. **Whether an unquoted hyphenated project id parses in every clause the
   generator emits.** Google's lexical documentation states that dashes are
   allowed in an unquoted identifier when it is the first part of a table path
   in a `FROM` or `TABLE` clause, and that dashes are not supported in dataset
   names — which covers the generated shape. I could not read the primary page
   directly (same rendering problem) and am taking this from Google's own search
   result summaries. **Settled by:** creating the project with a hyphen-free id,
   which makes the question moot, or by one `SELECT COUNT(*)` against a
   hyphenated one.
3. **Whether BigQuery still requires a companion `WHERE`/`GROUP BY`/`HAVING`
   for `QUALIFY`.** Nine staging models depend on the answer. **Settled by:**
   `dbt run --select stg_ad_spend` against the BigQuery target — one model,
   thirty seconds.
4. **The BigQuery storage footprint of the loaded dataset.** The ~1–1.5 GB
   figure is inferred from declared column types, not measured. **Settled by:**
   `bq show --format=prettyjson <dataset>.<table>` after loading, or the
   `INFORMATION_SCHEMA.TABLE_STORAGE` view.
5. **`from_service_account_file` without explicit scopes.** I reasoned that
   `google.cloud.client` applies `with_scopes_if_required` during `Client`
   construction. I did not read the installed SDK to confirm it for the version
   pinned by `google-cloud-bigquery>=3.11.0`. **Settled by:** the deliberate
   `service-account` run in F5.
6. **Whether the `local` provider could reach BigQuery through MetricFlow as a
   cross-check.** It could, in principle — but it would exercise neither
   `dbt_sql.py` nor `_connect_bigquery`, so it proves nothing about the code
   under test. Noted only so nobody proposes it as a shortcut.
7. **Whether batch load jobs work in the BigQuery sandbox.** The sandbox
   limitations page does not list them as unsupported, but never states they are
   supported either, and §1.1's whole recommendation rests on it. **Settled by:**
   one `bq load` of a trivial file into a sandbox project. Do this *first* —
   it decides whether a credit card is needed at all.
8. **Materialized-view pricing** as a standalone line item for non-Omni
   BigQuery. No such row was found on the pricing page today. Irrelevant here
   (the dbt project has no materialized views) but recorded so the §1.4 list is
   not read as exhaustive.
9. **Whether `us-central1`'s $0.023/GiB-month active-logical storage figure is a
   recent price change or a page inconsistency.** It was read in two independent
   places on the pricing page (the rendered table and the worked example), and
   it exceeds the `US` multi-region rate, which is unusual. No changelog
   confirms it. **Settled by:** not caring — pin `US` and stay under 10 GiB, at
   which point the rate is not charged at all.
10. **Google's own GiB-vs-GB inconsistency.** The pricing page says 10 GiB /
    1 TiB; the sandbox page says 10 GB / 1 TB; the cost-best-practices page says
    the per-query minimum is 10 MiB where the pricing page says 10 MB. The
    difference is ~7% and irrelevant at this scale, but do not quote a figure
    to a client without saying which page it came from.

---

## 9. What the roadmap should gain

`knowledge/roadmap.md` is **not edited by this change** — its author is editing
it. Recorded here so the amendment is not lost:

1. **Amend the 2.10 row.** It currently ends with the 2026-08-12 correction and
   the statement of the rule (*"generation and execution ship separately here,
   and a mapped dialect is not a supported warehouse"*). It should add that
   BigQuery is **still on the wrong side of that rule** — connector shipped
   2026-08-11, truncation corrected 2026-08-12, **never executed** — with a
   pointer here. The 2.10 row's own "Verified end to end against a real
   Databricks warehouse" is what BigQuery does not yet have, and the row should
   say so in the same sentence.
2. **Amend 2.14.** This plan's §6 is `--verify-against-metricflow` in manual
   form, with DuckDB standing in for MetricFlow and the committed White Cube
   snapshots as the fixed side. That is worth recording as a *precedent for the
   shape* rather than a substitute — and §6's three tiers (totals / per-period /
   slices, with the note that a totals-only check passes a week-bucketing bug)
   are the acceptance criteria 2.14 will need anyway.
3. **A new Horizon 2 row, or a 2.10 sub-item**, for the finding in §3: the
   `dbt` provider cannot serve **any** dbt project whose metrics declare
   `fill_nulls_with` — which is 19 of 26 metrics in the one real project in
   reach, and is a plausible majority in the wild, because `fill_nulls_with: 0`
   is the idiomatic way to write a countable event metric in MetricFlow. The
   refusal is *correct* (`dbt_bridge.py:311–320`, the C15 lesson), but the
   coverage consequence has not been costed anywhere. It belongs next to
   **2.17** (real `where:`/filter support), because it is the same gap: the
   binding contract cannot express something MetricFlow metrics routinely
   declare, so the provider refuses a large fraction of real projects by name.
   `doctor`'s `dbt provider migration` check (`doctor.py:408–462`) already
   measures this per project and is the right place to surface the number.
4. **A note under Horizon 0 or the S workstream** that `check_provider_extra`
   does not know about per-adapter driver extras (F4): a missing
   `metric-breakdown[bigquery]` reports as a warehouse-connection failure with
   a `dbt debug` remediation that will pass. Small, cheap, and exactly the
   "right policy in one file, not propagated to its neighbour" meta-defect the
   four rules were written for.

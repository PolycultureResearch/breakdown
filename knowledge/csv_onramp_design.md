# CSV on-ramp: the `SqlDataFetcher` refactor and a DuckDB provider

**Status:** step 1 (refactor) in PR #137; step 2 (DuckDB provider) in the
follow-up PR stacked on it. The customer guide in §4 works as written once
both merge.

Roadmap link: completes the CSV half of **2.2** ("send a CSV, get an RCA") and
lays the groundwork for a BigQuery provider (raised by the Northern Nights
deployment).

---

## 1. The problem

Breakdown can only analyze data it can reach. Today there are three routes:

| Route | Requires | Who has it |
|---|---|---|
| dbt Semantic Layer (`local` / `cloud`) | a governed dbt project with MetricFlow | mature data teams |
| Direct SQL (`warehouse`) | a **Databricks** SQL warehouse | Databricks shops only |
| `mock` / `none` | nothing | demos and cold start only |

The companies most likely to benefit from a first engagement — young, small
data team, maybe no warehouse at all — have **none** of the first two. Their
data lives in Eventbrite/DICE, Stripe, QuickBooks, and ad platforms, reachable
as CSV exports. There is no way to point Breakdown at that today, so every
evaluation starts with "first, stand up a warehouse," which is exactly the
ground-truth work we want to skip in an assessment.

## 2. Why the refactor comes first

`WarehouseDataFetcher.fetch_metric` did two different jobs in one method:

1. **Talk to Databricks** — connect, authenticate, run a query. *Specific to
   one database.*
2. **Enforce the metric contract** — look up the metric's SQL, require
   `date`/`value` columns, check dates sit on period starts, reindex onto the
   window's periods, fill gaps by `kind` (flow → 0, stock → carry forward,
   rate → error), trim not-yet-loaded trailing periods. *Identical for every
   SQL database.*

Job 2 is the valuable, subtle part — it encodes decisions like "a missing
trailing week means not loaded yet, not zero," which protect every headline
number downstream. If each new provider re-implemented it, the providers would
drift and the same CSV could produce different answers depending on which
engine read it.

The refactor splits them:

- **`SqlDataFetcher`** (new base class) owns job 2, plus a small
  `align_to_spine` helper.
- **`WarehouseDataFetcher`** now only implements `_execute(sql, params)` —
  job 1 — for Databricks. Its name and behavior are unchanged (snapshot
  manifests record the class name).

**Result:** a new SQL engine is ~20 lines — implement `_execute` — and
inherits the exact same gap-filling semantics for free. The full existing test
suite for the fetcher, doctor, slices and snapshots (92 tests) passes
unchanged, which is the evidence nothing moved for current users.

## 3. What a DuckDB provider gives the product

**DuckDB** is a database that runs inside the Python process and queries CSV
and Parquet files where they sit. No server, no account, no credentials.

| Benefit | Why it matters |
|---|---|
| **Zero-infrastructure evaluation** | A prospect can be assessed from exports alone. Time-to-first-RCA drops from weeks (warehouse + dbt) to hours (the roadmap's north-star metric). |
| **Same contract as production** | The per-metric SQL written for the assessment is the same `(date, value)` shape the warehouse provider uses. Graduating to a live source is a provider swap plus a SQL-dialect review, not a rewrite. |
| **Data stays local** | Files never leave the analyst's machine — no third-party data-processing approval needed to start. |
| **Reproducible by construction** | The exports can be committed next to the tree, so an RCA re-runs byte-identically from a fresh clone (roadmap exit criterion for Horizon 1). |
| **Cheap to test** | Tests use tiny CSV fixtures; no external service to fake. |
| **Path to MotherDuck** | DuckDB connects to MotherDuck (hosted DuckDB) with an `md:` connection string, so a shared cloud option is nearly free to add later if a client needs one. |

Honest limits:

- **A CSV is a snapshot.** Great for "does this explain last quarter?"; for a
  weekly operating tool the tree should be re-pointed at a live source.
- **History still matters.** Each metric needs ≥ 10 whole periods at its grain
  (10 days for daily, ~a year for monthly). A thin export will fit poorly.
- **The tree and the SQL are the real work.** Loading files is trivial;
  deciding the metric tree and writing each metric's query is where an
  assessment spends its time — and where fit (or misfit) shows up.

## 4. Customer guide: from CSV to breakdown

*Install with `pip install 'metric-breakdown[duckdb]'` — that pulls in DuckDB
too; there is nothing else to set up.*

### Step 1 — Export your data

Export one CSV per source table, with a header row. Typical set for a ticketed
event business:

```
exports/
  orders.csv          # order_id, created_at, status, ticket_tier, quantity, gross_amount
  ad_spend.csv        # date, channel, spend
  marketing_events.csv # date, event_type (announcement, price_flip, ...)
```

Rules that save pain later:
- **Dates as ISO strings** (`2025-06-02` or `2025-06-02T14:31:00`).
- **One row per record**, not pre-built pivot tables. Aggregation happens in
  the metric SQL, where it's visible and reviewable.
- **Keep the raw export.** Don't hand-edit it; fix things in SQL so the fix
  is recorded.

### Step 2 — Point the tree at the folder

Each CSV becomes a table named after its file (`orders.csv` → `orders`).

```yaml
provider:
  type: duckdb
  data_dir: ./exports
```

### Step 3 — Write one query per metric

Each query returns `date` and `value`, one row per period, filtered to the
window with `:start_date` / `:end_date`:

```yaml
metrics:
  - name: tickets_sold
    grain: day
    kind: flow
    sql: |
      SELECT CAST(created_at AS DATE) AS date, SUM(quantity) AS value
      FROM orders
      WHERE status != 'test'
        AND CAST(created_at AS DATE) BETWEEN :start_date AND :end_date
      GROUP BY 1

  - name: paid_spend
    grain: week
    kind: flow
    sql: |
      SELECT DATE_TRUNC('week', date) AS date, SUM(spend) AS value
      FROM ad_spend
      WHERE date BETWEEN :start_date AND :end_date
      GROUP BY 1
```

`DATE_TRUNC('week', …)` in DuckDB starts weeks on **Monday**, which is what
Breakdown expects — no adjustment needed (unlike BigQuery, where the default
is Sunday).

### Step 4 — Check the wiring

```bash
breakdown doctor --tree tree.yml --start-date 2025-01-01 --end-date 2025-09-30
```

The doctor confirms the files are found, every metric's SQL runs, and each
metric has enough whole periods to fit. Fix what it names before going on.

### Step 5 — Run it

```bash
breakdown serve --tree tree.yml --start-date 2025-01-01 --end-date 2025-09-30
```

Open `http://localhost:9090/ui`, pick a reference and an analysis window, and
run the root-cause analysis.

### Graduating to production

When the assessment earns a recurring place in the business, swap the
provider block for the client's live source (warehouse, MotherDuck, or — once
built — BigQuery), review each query for dialect differences, and keep the
tree. The snapshot cache then keeps runs reproducible while the source
updates.

---

## 5. Implementation plan

1. ✅ **Refactor** — `SqlDataFetcher` base + `align_to_spine`;
   `WarehouseDataFetcher` implements only `_execute`. No behavior change.
2. ✅ **`DuckDBDataFetcher`** — registers each `*.csv` / `*.parquet` in
   `data_dir` as a view named by file stem; `_execute` translates the
   `:start_date`/`:end_date` params and runs the query. Ships as an optional
   `duckdb` extra, imported at the point of use (base install stays lean).
3. ✅ **Config** — `provider: {type: duckdb, data_dir: ...}` in
   `DataProviderConfig`, relative paths resolved against the tree file.
4. ✅ **Doctor** — checks `data_dir` exists, lists the tables found, runs each
   metric's SQL over the probe window.
5. ✅ **Tests** — `tests/test_duckdb_provider.py`: gap-fill, Monday weeks,
   Parquet, `::` casts, missing/empty folder, stem collisions, config
   resolution, doctor end to end.
6. ✅ **Docs** — README provider section; this guide.

Open questions for review:
- Should `data_dir` also accept a single DuckDB database file (`.duckdb`), for
  clients who send one file instead of many CSVs?
- MotherDuck: in scope for v1, or wait for a client to ask?

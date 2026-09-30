# CSV on-ramp: `provider: duckdb` over the per-node binding contract

**Status:** shipped 2026-09-21 (PR #138), the CSV half of roadmap
[2.2](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard). The
customer guide in §4 works as written.

**Credit:** the original implementation — view registration over a folder of
exports, `data_dir` with tree-relative resolution, the doctor's `data files`
check, and the test fixtures — is Justin Fung's (justincfung, 39f50514). It
was reworked in place from a `date, value` SQL provider onto the `bind:` route,
for the reasons in §2; the decision is Devon's (2026-09-21) and is recorded in
[`roadmap_log.md#2-2`](roadmap_log.md#2-2).

---

## 1. The problem

Breakdown can only analyze data it can reach. Before this shipped there were
three real routes:

| Route | Requires | Who has it |
|---|---|---|
| dbt Semantic Layer (`local` / `cloud`) | a governed dbt project with MetricFlow | mature data teams |
| a dbt project's own manifest (`dbt`) | `dbt parse` on a dbt Core project, and its warehouse | teams already on dbt |
| Direct SQL (`warehouse`) | a **Databricks** SQL warehouse | Databricks shops only |
| `mock` / `none` | nothing | demos and cold start only |

The companies most likely to benefit from a first engagement — young, small
data team, maybe no warehouse at all — have none of the first three. Their
data lives in Eventbrite, Stripe, QuickBooks and ad platforms, reachable as
CSV exports. Every evaluation therefore started with "first, stand up a
warehouse," which is exactly the work an assessment exists to skip.

## 2. Why bindings, not a second SQL provider

The first version of this was a `DuckDBDataFetcher`: each metric carried a
`sql:` returning `date, value`, the `warehouse` provider's contract, run by
DuckDB over the exports. It worked, and it was the wrong shape for three
reasons, in order of weight.

**A `date, value` query is opaque to everything 2.9 built.** The per-node
`bind:` contract exists so the engine can *check* what it aggregates. It
asserts `count(*) == count(distinct grain_key)` on every relation — and a
hand-exported CSV with a duplicated `order_id` is the single most likely
fan-out in the product; a query that had already summed it would return a
confident wrong number. It slices by declared dimensions, which is what turns
"tickets fell" into "tickets fell, in the VIP tier" — and roadmap 2.8 is still
open on `warehouse` precisely because a finished-series query has nothing to
slice. And it keeps a ratio's numerator and denominator separate, so a rate
decomposes into within-slice movement and mix; a `date, value` ratio can only
be attributed additively, which is the confidently-wrong root cause the
binding contract was designed to prevent.

**The on-ramp's promise is that the tree written against exports is the tree
kept.** A `bind:` block moves to a warehouse by changing `provider:`; the
relation name is reviewed for the new dialect and everything else stays. A
DuckDB-dialect `date, value` query has to be rewritten, and the rewrite is
where the definition drifts.

**It costs nothing.** `DbtDataFetcher` never depended on a dbt artifact —
`bridge_project` is called only inside `fetcher_from_project`. The provider is
therefore a factory beside it, `fetcher_from_data_dir`, whose `connect` opens
an in-memory DuckDB with one view per file. No new fetcher class, no
`SqlDataFetcher` base: the refactor the first version needed is superseded,
because the engine that turns a binding into dialect SQL and lands the result
on the period spine already existed.

What carried over from the original: the view registration
(`read_csv_auto` / `read_parquet`, one view per file stem) with the
stem-collision and missing-or-empty-folder refusals, `data_dir` with `${ENV}`
expansion and resolution against the tree file, the doctor's `data files`
check, the test fixtures, and this guide's structure.

## 3. What the provider gives the product

| Benefit | Why it matters |
|---|---|
| **Zero-infrastructure evaluation** | A prospect can be assessed from exports alone. Time-to-first-RCA drops from weeks (warehouse + dbt) to hours, the roadmap's north-star metric. |
| **The production contract from day one** | Every binding is checked the way a warehouse binding is: grain claim, declared dimensions, filters, entity grain, and that the generated SQL runs. `doctor` says so, step by step. |
| **Graduation is a `provider:` change** | The bindings stay as written when the client stands up a warehouse or a dbt project; under `dbt`, a node's own `bind:` still overrides the manifest. |
| **Data stays local** | Files never leave the analyst's machine; no third-party data-processing approval is needed to start. |
| **Reproducible by construction** | The exports are committed next to the tree, so an RCA re-runs from a fresh clone. The provider is deliberately *not* snapshot-cached: the files are the artifact, and a cache keyed without a content hash would freeze an edited CSV silently. |
| **Cheap to test** | Tests use tiny CSV fixtures; no external service to fake. |

Honest limits:

- **A CSV is a snapshot.** Right for "does this explain last quarter?"; a
  weekly operating tool should be re-pointed at a live source.
- **History still matters.** Each metric needs ≥ 10 whole periods at its
  grain. A thin export fits poorly, and `doctor --start-date … --end-date …`
  says so per metric.
- **The tree and the bindings are the real work.** Loading files is trivial;
  deciding the metric tree and getting each `grain_key` right is where an
  assessment spends its time — and where fit, or misfit, shows up.

## 4. Customer guide: from CSV to breakdown

*Install with `pip install 'metric-breakdown[duckdb]'`. That brings in DuckDB
and the SQL generator; there is nothing else to set up.*

### Step 1 — Export your data

Export one CSV (or Parquet) per source table, with a header row. A typical
set for a ticketed event business:

```
exports/
  orders.csv           # order_id, created_at, status, ticket_tier, quantity, gross_amount
  ad_spend.parquet     # spend_id, day, channel, spend
```

Rules that save pain later:

- **Dates as ISO strings** (`2025-06-02` or `2025-06-02T14:31:00`). DuckDB
  reads both; a timestamp buckets to its day.
- **One row per record, not pre-built pivots.** Aggregation happens in the
  binding, where `doctor` can check it.
- **One file per relation, named for what it is.** The file stem is the
  relation name (`orders.csv` → `orders`); two files with one stem are refused.
- **Keep the raw export.** Do not hand-edit it; fix things in a `bind.sql`
  relation so the fix is recorded in the tree.

### Step 2 — Point the tree at the folder

```yaml
provider:
  type: duckdb
  data_dir: ./exports      # relative to this tree file
```

### Step 3 — Bind each metric

Every fetched node declares a `bind:` block — the same block it would declare
over a warehouse table. `source` is required and is a label; bindings are
keyed by the node's `name`.

```yaml
metrics:
  - name: tickets_sold
    source: exports.orders.tickets_sold
    kind: flow
    dimensions:
      tier: ticket_tier
    bind:
      sql: SELECT * FROM orders WHERE status != 'test'
      grain_key: order_id
      time_column: created_at
      agg: sum
      measure: quantity
      dimensions:
        ticket_tier: {column: ticket_tier}

  - name: average_ticket_price
    source: exports.orders.average_ticket_price
    kind: rate
    denominator: tickets_sold
    bind:
      sql: SELECT * FROM orders WHERE status != 'test'
      grain_key: order_id
      time_column: created_at
      agg: ratio
      numerator: gross_amount
      denominator: quantity

  - name: paid_spend
    source: exports.ad_spend.paid_spend
    grain: week
    bind:
      relation: ad_spend
      grain_key: spend_id
      time_column: day
      agg: sum
      measure: spend
```

Weeks bucket to Monday. A top-level `sql:` is refused with the message
pointing at `bind.sql` — the two are different contracts, and the parser says
so rather than producing a wrong shape at startup.

### Step 4 — Check the wiring

```bash
breakdown doctor --tree tree.yml --start-date 2025-01-01 --end-date 2025-09-30
```

The doctor reports the folder and which relation each file became, then runs
the binding checks a dbt project gets: every metric binds, every declared
dimension exists on its binding, every relation is one row per `grain_key`,
every metric's generated SQL runs, and each metric has enough whole periods to
fit. Fix what it names before going on.

### Step 5 — Run it

```bash
breakdown serve --tree tree.yml --start-date 2025-01-01 --end-date 2025-09-30
```

Open `http://localhost:9090/ui`, pick a reference and an analysis window, and
run the root-cause analysis. *Show query* on a node card displays the SQL the
binding generated over the export.

### Graduating to production

When the assessment earns a recurring place in the business, change the
`provider:` block to the client's live source — `dbt` over their project, or
`warehouse` — and keep the tree. Each `bind:` stays as written, reviewed for
dialect differences in the relation names. The snapshot cache then keeps runs
reproducible while the source updates.

---

## 5. What shipped, and what was left out

Shipped (PR #138):

1. **Config** — `provider: {type: duckdb, data_dir: …}`; `${ENV}` expansion;
   relative `data_dir` anchored to the tree file in one place
   (`loading.resolve_data_dir`, reached through `build_fetcher(tree_path=…)`).
   Every fetched node must declare `bind:`; a top-level `sql` is refused at
   parse with the pointer at `bind.sql`.
2. **Fetcher** — `dbt_provider.fetcher_from_data_dir`: `DbtDataFetcher` with
   the tree's bindings, `connect=open_data_dir` (imports `duckdb` at the point
   of use, one view per file), `dialect="duckdb"`. Never wrapped in the
   snapshot cache.
3. **Packaging** — the `duckdb` extra (`duckdb` + `dbt-bridge`), in `[all]`.
4. **Doctor** — `data files`, then `_check_bindings`, the back half of
   `check_dbt` extracted so both providers run the same code. That shared
   chain gained a `metric sql runs` step the `dbt` chain never had.
5. **Tests** — `tests/test_duckdb_provider.py`, skipped where the extra is
   absent; the no-extras CI job asserts the extra is named before the folder
   is looked at.

Left out, deliberately:

- **MotherDuck** (`md:` connections). A hosted copy is a small follow-up if a
  client needs one; nothing here precludes it.
- **A single `.duckdb` file as `data_dir`.** A client who sends one database
  file instead of many CSVs can already be served by the `dbt` provider's
  duckdb connector with a two-line `profiles.yml`; a dedicated path is not
  worth a second `data_dir` meaning until someone asks.
- **Per-metric provider mixing** — the other half of 2.2, still open. A tree
  is served by one provider; mixing a CSV-bound node into a `dbt` tree is the
  next step and needs the unbound-node RCA policy that
  [`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
  §3 defers.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

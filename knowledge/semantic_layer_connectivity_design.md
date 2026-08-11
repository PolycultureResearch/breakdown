# Design: Node bindings and semantic-layer connectivity (roadmap 2.9–2.13)

Status: draft (evaluation complete, decisions taken, nothing built). Companion to
[`archive/product_integration_plan.md`](archive/product_integration_plan.md) (the
2026-07 connectivity analysis this supersedes on the dbt question),
[`grain_design.md`](grain_design.md) (the coarsen-only invariant a binding must
honour) and
[`dimensional_slicing_design.md`](dimensional_slicing_design.md) (the sliced-fetch
contract this makes cheap).

Two decisions are recorded here that revise standing guidance, and both are
deliberate rather than incidental:

1. **A node binds to its data source individually**, replacing the tree-level
   `provider:` as the primary mechanism (§3). This promotes roadmap 2.2 from an
   incremental convenience to the foundation.
2. **breakdown ships a minimal binding contract** — a fetch descriptor, not a
   semantic layer (§4). This amends the *Deliberately not on the roadmap* entry
   "our own metric definition language"; §4.1 states what the amended constraint
   is and why it still has teeth.

## 1. Product framing

Principle 4 is time-to-first-trusted-RCA — *elapsed time from "here are my
credentials" to a number the user believes*. Everything in this document is
subordinate to that number, and today two things dominate it.

**The provisioning tax on dbt Cloud.** `cloud` fails on entitlement, not on code.
The archive plan §1 recorded the whole chain from the Narrative pilot: SL config
present but no service-token mapping, an opaque *"Credentials have not been set
up for this environment"*, service tokens that cannot be created with a user API
token so an admin must click through the UI, cell-based hostnames discoverable
only from `~/.dbt/dbt_cloud.yml`, and **"Only 1 Semantic Layer credential is
supported with your plan."** None of it is fixable from our side, and it excludes
dbt Core shops outright. `local` routes around dbt Cloud into a different
problem: `mf query` as a subprocess writing a temp CSV behind a 120-second
timeout, **one process per slice**, and a hard dependency on the `mf` binary —
which is why the `dbt` extra does not work on Python 3.14.

**The prerequisite tax on everyone else.** Most prospects have no semantic layer
at all. "Come back once you've built one" is a six-week prerequisite before first
value — and what they would build is *overkill*, because a semantic layer serves
org-wide governed self-serve while breakdown needs one series per node.

These are different problems and they need different answers. §2 and §5 answer
the first. §4 answers the second.

## 2. The finding: dbt's own artifact is free

> `target/semantic_manifest.json` — the complete, **fully resolved** form of
> every semantic model and metric in a dbt project — is written by plain
> `dbt parse` on **dbt Core**. No dbt Cloud. No SL credential. No service token.
> No plan tier.

Verified against dbt-core source and two real projects:

- `dbt/constants.py` → `SEMANTIC_MANIFEST_FILE_NAME = "semantic_manifest.json"`.
  `write_semantic_manifest` is called only from `write_manifest`, so **every
  command that writes `manifest.json` also writes this** — `parse`, `compile`,
  `build`, `run`, `test`, `seed`, `snapshot`, `list`, `clone`. `dbt parse` is the
  cheapest producer; `--no-write-json` suppresses it.
- Introduced in **dbt-core 1.6.0**; `saved_queries` added in 1.7.0.
- `node_relation` carries the real, adapter-resolved warehouse relation —
  `{"alias": "dim_users", "schema_name": "dbt_dsampson", "database": "narrative",
  "relation_name": "`narrative`.`dbt_dsampson`.`dim_users`"}`. **No join to
  `manifest.json` is needed** to find the table.
- `dbt parse` needs a resolvable `profiles.yml` and an installed adapter, but
  does not open a warehouse connection.

This is exactly what the archive plan wanted from dbt Cloud's Metadata API —
*"`client.metrics()` returns names, types, and — for `derived` and `ratio`
metrics — `input_metrics`, which are literally parent edges"* — and assumed was
gated behind a paid tier. It is not. It sits in `target/` of every dbt Core
project on disk.

## 3. Bindings are per node, not per tree

Today `DataProviderConfig` is tree-level: one provider serves every metric. That
is wrong for the state clients are actually in. A tree where some nodes come from
a semantic layer, some from direct SQL, and some from a CSV is not an edge case —
it is the normal migration state, and roadmap 2.2 already says so.

**A node is either bound or unbound.** A bound node declares where its series
comes from; an unbound node has priors and no data. A tree can be sketched whole
and bound incrementally, so onboarding starts with the causal theory — which is
the part only breakdown has — rather than with data plumbing.

`provider:` survives as a **tree-level default** for nodes that declare no
binding of their own, so every existing tree keeps working unchanged.

⚠️ **Unbound nodes carry an unsolved design cost, and it is not this document's.**
Hybrid cold-start→fitted mode (was 2.7) was removed 2026-08-05 partly because
per-node graduation "needs a policy for partially-fitted paths that the roadmap
itself admitted was *worth its own small spec*." A tree mixing bound and unbound
nodes has precisely that problem: what does RCA report for a path that runs
through a node with no data? **Mixed *sources* (2.2) and mixed *bound/unbound*
are separable, and only the first is in scope here.** v1 binds every node in a
tree or refuses to run RCA through the gap; unbound-node RCA policy is deferred
(§10).

## 4. The binding contract

**Design target: ~12 lines per bound node.** It is small because it serves
exactly one consumer — decomposition — not org-wide governed self-serve.

```yaml
revenue:
  bind:
    relation: analytics.fct_orders
    grain_key: order_id
    time_column: ordered_at
    agg: sum
    measure: amount
    dimensions:
      region: {join: dim_customers, on: customer_id, column: region}

conversion_rate:
  bind:
    relation: analytics.fct_funnel
    grain_key: session_id
    time_column: session_at
    agg: ratio
    numerator: converted
    denominator: sessions
```

| Field | Notes |
|---|---|
| `relation` | table/model reference, **or** an inline `sql:` block |
| `grain_key` | the column making the relation one row per grain — see §6 |
| `time_column` | |
| `agg` | `sum` \| `count` \| `count_distinct` \| `ratio` \| `average` \| `last` |
| `numerator` / `denominator` | **required** when `agg: ratio`, as separate measures |
| `entity_key` | **required** when `agg: count_distinct` or semi-additive |
| `dimensions` | declared slices, each with a many-to-one join path |

`kind` and `grain` stay where they already live, on the node — they are modeling
facts, not fetch facts.

### 4.1 What this is not, and the constraint that keeps it that way

This amends *Deliberately not on the roadmap* → "our own metric definition
language (we ride dbt's)". The amendment is narrow, and worth stating precisely
because the phrase is broader than what is being built.

breakdown **already** has a metric definition language: `priors`, `lags`,
`expected_signs`, `kind`, `plausible`, `baseline`, `direction`, `format`,
`formula`. The archive plan concedes it — *"Item 3 is ours — no semantic layer
expresses priors or lag structure."* And the `warehouse` provider's per-metric
`sql:` returning `date, value` is already a primitive, untyped binding. The
decision here is not *whether* to have a language; it is whether to own the last
mile of it coherently instead of as accreted overrides.

The non-goal was protecting against reimplementing MetricFlow. It still should.
So the boundary is a test, applied to every proposed field:

> Does this field describe **how to fetch one series for one node**, or does it
> describe **shared org-wide semantics**? The second is out.

Explicitly out, permanently: reusable dimension groups, metric-on-metric
references (that is `formula` — the DAG, already ours), joins beyond
fact → conformed-dimension many-to-one, governance, access control, result
caching, self-serve.

**The stop rule is structural, not aspirational: `sql:` answers every feature
request first.** A new field lands only if `sql:` genuinely cannot express the
thing *and* the concept is required for correctness — `kind`, `entity_key`,
separate numerator/denominator — never for convenience. Every semantic layer that
grew into a platform (MetricFlow, Cube, LookML) started at twelve lines; an
escape hatch that absorbs feature pressure is the only thing that has ever held
that line.

Two arguments carried this decision beyond the prerequisite tax:

- **Annotation is mandatory regardless.** `kind` (flow/stock/rate) has no
  MetricFlow equivalent and is not derivable from `agg`, yet it drives resample-up
  and gap-fill. `entity_key` and explicit numerator/denominator are likewise
  absent from weaker sources. Even a *perfect* import needs breakdown-side
  annotation — so the only real choice is between a coherent contract and a pile
  of per-source overrides.
- **The bootstrapper depends on it.** Having an LLM emit MetricFlow semantic
  models *plus* a breakdown tree is two artifacts in two dialects with
  cross-references — hard to generate, harder to review. Twelve lines colocated
  with the node they serve is a tractable generation target and a reviewable
  diff. Roadmap 2.3, the highest-leverage item for the no-semantic-layer client,
  is downstream of this decision.

### 4.2 Imported beats hand-written — enforced

Definition drift is the trust killer, and it is the one real cost of owning a
binding contract. A client with dbt metrics *and* hand-written bindings has two
definitions of revenue; when they disagree breakdown loses, because the dashboard
is what the business already believes.

**Policy: if a metric is importable from a semantic layer present in the repo, a
hand-written binding that shadows it is a `breakdown doctor` error**, naming both
definitions and the divergence. A per-node `override: true` escape exists for the
case where the upstream definition is genuinely wrong or wrongly grained — loud
by default, unblockable in practice.

Bindings are for metrics with nothing behind them. That is the whole point.

## 5. Execution: generate the SQL, and prove it agrees

The strongest argument against generating our own SQL is that **if breakdown's
numbers disagree with the client's dashboards, the RCA is dead on arrival
regardless of whether we are right.** The safe-looking answer is to push
execution down into the client's semantic layer.

That answer does not survive contact with the case that motivated this document:
pushing down to dbt means dbt Cloud SL or the `mf` binary — the two things §1 is
escaping. Push-down cannot serve the dbt Core client.

**So: generate the SQL, and make agreement provable instead of assumed.**

`breakdown doctor --verify-against-metricflow` runs our generated SQL *and*
MetricFlow's own compiled SQL for the same metric, grain and window, and asserts
equality on real data. **MetricFlow becomes a validation-time dependency, not a
runtime one** — no dbt Cloud on the RCA path, and definition drift becomes a
failing check rather than something discovered mid-incident. Pinned in CI against
a fixture project.

Framed against what exists, this is **the `warehouse` provider with generated
instead of hand-written SQL.** It reuses `WarehouseDataFetcher`'s proven
`[date, value]` contract and execution path, and it closes the archive plan's one
real objection to warehouse-direct —

> "**Definition drift is the trust killer** … the analyst mirrors governed
> definitions in SQL"

— because nothing is mirrored: the definitions **are** dbt's, resolved by dbt,
and §5's differential check proves it.

### 5.1 Parse with MSI, not DSI — this one is load-bearing

Use **`metricflow_semantic_interfaces` (MSI)**, which ships inside the
`metricflow` wheel from 0.210.0. Do **not** use `dbt-semantic-interfaces` (DSI).

DSI is deprecated — 0.10.5 (2026-02) is its final release, and dbt-core dropped
it in 1.12.0 for `metricflow>=0.211.0`. More importantly it **silently corrupts
new-spec manifests**. dbt's new metrics spec replaces the measure layer with
`type: simple` metrics carrying their own aggregation in
`type_params.metric_aggregation_params`; DSI ignores unknown fields, so against a
real Fusion project on disk it parsed the manifest, **validated with 0 errors and
0 warnings**, and returned all 61 simple metrics with no aggregation at all. MSI
models the field natively, pulls no dbt-core and no adapters, and ships
`sqlglot` — which the generator wants anyway.

Two rules follow:

1. **Handle both metric shapes** — classic `type_params.measure` and new-spec
   `metric_aggregation_params` — and **hard-fail if a simple metric resolves to
   neither**, rather than trusting a clean validation result.
2. **Apply only the five safe normalization rules**
   (`BooleanMeasureAggregation`, `ConvertCountToSum`, `ConvertMedianToPercentile`,
   `SetCumulativeTypeParams`, `RemovePluralFromWindowGranularity`). The full
   transform set crashes on an already-written manifest —
   `AddInputMetricMeasuresRule` asserts `input_measures` is empty and dbt-core has
   already populated it.

### 5.2 Metric coverage ladder

v1 covers **simple + ratio + derived-without-offset + filters** — roughly 80% of
real trees. Then `join_to_timespine`, derived offset windows, and cumulative
(`window` and `grain_to_date`), each of which needs a time-spine join.

**Out of scope, with an explicit unsupported diagnostic rather than an
approximation:** `conversion` metrics (four stages — UUID tagging, a non-equi
join within the conversion window, last-touch `FIRST_VALUE` attribution, then a
`FULL OUTER JOIN` against the independently aggregated base leg) and
`non_additive_dimension` (the MIN/MAX filter is applied per grain window when the
query groups by that dimension, so it is *query-grain-dependent* and silently
wrong if approximated).

Refusing loudly is the point. A metric breakdown cannot express correctly must
fail at `doctor` time with the metric named, never produce a plausible number.

## 6. Fan-out: validate the grain claim, don't build a join planner

A bound node resolves to exactly one relation, declared one of two ways:

1. **Star-shaped** — a fact table plus dimension joins declared **many-to-one
   only** (fact → conformed dimension). Fan-out is definitionally impossible on
   this shape, so symmetric aggregates are never required.
2. **Escape hatch** — an arbitrary `sql:` block producing one row per declared
   grain. This covers chasm traps, bridge tables and multi-step joins.

**The validation gate: at bind time, assert `count(*) == count(distinct
grain_key)` on the resolved relation, and fail the binding if they disagree.**

This converts silent fan-out from a class of subtle wrong answers into a startup
error, and it is strictly better than inferring safety from declared entity
cardinality — it checks the data rather than trusting the metadata. **MetricFlow
and Cube do not do this**; they accept declared relationships on trust. It is
cheap, it belongs in `breakdown doctor`, and it is a real quality differentiator
that only exists because we own the contract.

A client needing a multi-hop join was going to write that model anyway, and it
belongs in dbt where it can be tested and versioned — not inside a query planner
of ours.

## 7. Aggregation semantics: decomposition diverges by type

Store the aggregation type explicitly; never infer it from a SQL string.

| Type | Example | Decomposition |
|---|---|---|
| Additive | `sum(revenue)`, `count(orders)` | Δtotal = Σ Δslice exactly; contributions sum, no residual |
| Ratio | conversion rate, AOV | Denominator-weighted average of slice ratios; splits into within-slice movement, denominator mix shift, and a cross term. **Requires numerator and denominator separately.** Where Simpson's paradox lives |
| Non-additive | `count(distinct user_id)` | Slices genuinely don't sum; the residual is dedup overlap, not an unexplained cause — §8 |
| Semi-additive | balances, headcount, inventory | Additive across dimensions, not across time; period-over-period needs stock-and-flow |
| Cumulative | trailing 30d | Change is driven by entries and exits at the window boundary; attribution belongs to the boundary period |

Metadata availability by source: **MetricFlow** declares all of this first-class
(`agg`, `agg_time_dimension`, `non_additive_dimension`, metric `type` with
explicit numerator/denominator refs) — import is high fidelity. **Cube** tracks
additivity because pre-aggregation rollups depend on it. **Lightdash** has a
metric type enum but metrics cannot reference metrics, so ratios arrive as opaque
SQL. **Superset** metrics are an expression string plus a label —
`SUM(a)/SUM(b)` and `COUNT(DISTINCT user_id)` are indistinguishable from `SUM(x)`
without parsing. For the last two, **require a breakdown-side binding rather than
building an expression parser with heuristics** — which is precisely what §4's
contract is for.

⚠️ **Correctness requirement on import:** `count_distinct` is not re-aggregable,
and `resample_up` sums flows. A `count_distinct` measure imported as `kind: flow`
at day grain and resampled up to a monthly child **over-counts** — summing daily
distinct users is not monthly distinct users. The importer must mark these and
the engine must refuse to resample them, exactly as `kind: rate` is already
refused ([`grain_design.md`](grain_design.md)).

## 8. Non-additive metrics: change the grain until it's additive

`count(distinct user_id)` is not additive because a user appears in several
slices. But it is identically `sum(1)` over a user × period presence table.

When `agg: count_distinct`, the contract requires an `entity_key` and optionally
a relation at entity × period grain. Three tiers follow:

1. **Entity-grain relation declared** — full decomposition plus growth
   accounting: Δusers = new + resurrected − churned. That identity is *exactly*
   additive, and each flow slices additively because a user lands in exactly one
   bucket. Usually the answer the user actually wanted.
2. **Entity key only** — query each slice's distinct count independently (never
   sum them), report per-slice Δ for ranking, and surface overlap as an
   explicitly named residual rather than distributing it. This is roughly what
   ships today: `dimensional_slicing_design.md` already reports slices that don't
   sum instead of silently rescaling them.
3. **Neither** — refuse contribution percentages. Trend only.

**The motivating failure mode:** a user switches platforms. Platform A shows −1,
platform B shows +1, the total is unchanged. Naive slice attribution reports two
large offsetting causes for a change that never happened. Entity flows label it
correctly as *migration* — and migration is frequently the real finding.

General rule: **a non-additive metric requires a declared entity, and
decomposition happens at the grain where the metric becomes a sum.** The same
mechanism handles semi-additive balances via stock-and-flow at entity grain.

Tier 1 is a substantial capability and gets its own design doc; it is tracked as
roadmap 3.8, not as part of the connectivity work.

## 9. What we evaluated, and what we rejected

Two semantic-layer libraries were assessed as substrates. Both were read at
source, not from their READMEs.

**Boring Semantic Layer** (boringdata + xorq-labs, MIT, 0.3.16) has a clean
Ibis-backed query surface that maps almost 1:1 onto a fetcher — and **no dbt
integration of any kind**: no `from_dbt`, no manifest reader, no MetricFlow
parsing, no `dbt` extra. The only artifact is
[issue #30](https://github.com/boringdata/boring-semantic-layer/issues/30), open
since 2025-08 with zero comments, whose aspiration is one-way scaffolding from
dbt **models** — tables and columns — not metric semantics. It is therefore not a
route to §2 at all. It survives as an optional binding (roadmap 2.12) because it
is the only way to serve a local parquet file with no warehouse.

**Sidemantic** reads 23 semantic-layer formats into one graph and has the best
fan-out story of anything surveyed (symmetric aggregates, multi-hop joins,
raising rather than guessing on non-decomposable aggregates), with CI across
Python 3.11–3.14. **It is AGPL-3.0 and breakdown is Apache-2.0.** Verified
directly: `LICENSE` is the GNU AGPL v3 text, `pyproject.toml` declares
`license = {file = "LICENSE"}`, and PyPI carries the AGPL text with no
classifier. Linking it relicenses the combined distributed work, and §13's
network clause bites precisely the `breakdown serve` deployment mode we target.

⚠️ **Vendoring the parsers does not mitigate this — it makes it worse.** Vendored
AGPL is still AGPL and now lives in our tree. The mitigation for a
maintainer-concentration risk is not available here because the disqualifying
risk is the licence.

It would not have served the Tier 1 case anyway: its MetricFlow adapter does
`rglob("*.yml")` over the user's *source* YAML rather than reading the resolved
manifest, **`metric_time` is not implemented** (zero occurrences in the package),
there is no time spine, and `{{ Dimension(…) }}` Jinja filters are captured
verbatim and never translated.

⚠️ **Licensing hygiene for whoever implements this:** do not use Sidemantic's
source as an implementation reference — reimplementing from AGPL code carries
derivative-work risk. The clean reference is **MetricFlow itself, Apache-2.0 from
0.209.0**; its source, tests and compiled-SQL snapshots are the authority on
every semantic we need.

**Adapter scope.** Cube, Rill, Lightdash and Superset get no adapters until a
user is blocked on one — roadmap principle 1, *"prefer the item a concrete use
case is blocked on over the speculatively useful one."* §4's binding contract
plus the `sql:` escape hatch already covers all of them at zero adapter cost, and
for Lightdash and Superset a binding is the *better* answer anyway (§7).

## 10. Correctness hazards and open verification

Beyond §7's `count_distinct` requirement, each of these produces a *plausible
wrong number* rather than an error — the Horizon 0 failure class.

1. **`agg: count` is not `COUNT(*)`.** It requires `expr` and desugars to
   `SUM(CASE WHEN <expr> IS NOT NULL THEN 1 ELSE 0 END)`. Relatedly, some
   `CASE WHEN` in a manifest is *machine-generated* by
   `BooleanMeasureAggregationRule` — SQL in a manifest does not imply a human
   wrote it.
2. **`percentile` is a fraction strictly in (0,1).** dbt's own docs show
   `percentile: 95.0`, which the validator rejects. Trust the code.
3. **Snowflake's week start is session-dependent.** Ibis forces Monday on
   BigQuery but sets no `WEEK_START` on Snowflake, so it inherits the account
   default. `_floor_labels`' warn-on-mismatch is **load-bearing here** and must
   not be optimized away.
4. **Timezone dtype is warehouse- and column-dependent.** `_to_naive_dates` (the
   C1 fix) already armors us; every binding must route through it.
5. **Two systems will want to gap-fill.** MetricFlow's `join_to_timespine` and
   `fill_nulls_with` overlap `_align_to_spine`'s kind-aware fill. breakdown owns
   it; the importer must not set `fill_nulls_with`.
6. **Fusion writes nothing for legacy-spec projects.** dbt Core v2 guards the
   write on `if !semantic_layer_spec_is_legacy`, so a classic `semantic_models:`
   project on Fusion produces **no artifact at all**. `doctor` should say so by
   name rather than reporting a missing file.

Two things are **unverified** and must be settled by a smoke test before 2.10 is
scheduled, recorded here so nobody mistakes them for established:

1. **Python version.** MSI's pydantic path is ≤3.13 in practice. breakdown's
   existing `dbt` extra is *already* 3.14-broken, so this is at worst not a
   regression — but it must not be sold as a 3.14 fix until `dbt parse` → MSI has
   been run end to end on the version we ship.
2. **Returned timestamp dtype and timezone per warehouse**, from a real query
   rather than from source reading.

## 11. What this unlocks

1. **Show the SQL behind every number.** Because we generate it, provenance is
   exact and free. Principle 3 is *"never ship a number the engine can't
   defend"*, and today `warehouse` is the only provider where the user can see
   what was queried — because they wrote it. A "show query" affordance on node
   cards, slice panels and the RCA export is the strongest value-alignment here.
2. **Dimension discovery removes the declaration tax.** The manifest lists every
   dimension with description and type, giving `doctor`-time validation of
   declared slices instead of a 500 on first click — the same too-late failure
   class as [C12](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
   — plus `dimensions: auto` and a UI that offers *slice by* for dimensions
   nobody declared.
3. **The tree scaffolder gets real input (roadmap 2.3).** `ratio` and `derived`
   input metrics **are** formula edges; `agg` and metric type infer `kind`;
   `time_granularity` and `agg_time_dimension` infer `grain` — exactly the
   metadata [C10](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
   found missing or wrong across 46 nodes of the reference tree.
4. **One fetch, all slices.** A single `GROUP BY` covers a metric and every
   declared dimension; today each slice is a separate `mf` subprocess. The
   traverse-then-slice loop is the stated differentiator and this is where its
   latency lives.
5. **Every warehouse.** sqlglot dialects cover Snowflake, BigQuery, Databricks,
   Postgres and DuckDB, collapsing roadmap 3.3.
6. **Graduation is a supported path, not a rewrite.** Binding → MetricFlow is
   mechanical, so a client who later adopts dbt can emit semantic models from
   their bindings. breakdown becomes the on-ramp *to* a semantic layer rather
   than a competitor to one — which is also the answer to any lock-in objection
   against §4.

## 12. Explicitly deferred

- **Unbound-node RCA policy** — §3. The partially-fitted-path problem that
  removed 2.7; needs its own spec before mixed bound/unbound trees are supported.
- **Conversion metrics and `non_additive_dimension`** — §5.2. Diagnosed and
  named, never approximated.
- **Symmetric aggregates.** §6 refuses fan-out rather than computing through it.
  Real demand upgrades refusal to correctness.
- **Cube / Rill / Lightdash / Superset adapters** — §9. The `sql:` binding covers
  them until someone is blocked.
- **OSI as a contract subset.** [OSI v1.0](https://open-semantic-interchange.github.io/osi-website/)
  shipped 2026-01-27 with dbt Labs, Snowflake and Salesforce backing, and
  aligning the binding contract to a subset of it would make §4 an on-ramp rather
  than a proprietary format. But dbt's emitted `osi_document.json` declares
  0.1.x and metricflow's converter is documented lossy (it warns on conversion,
  private, natural-entity and cumulative constructs). Directionally right,
  prematurely specific — **check what dbt actually emits before shaping the
  contract around it.**
- **`saved_queries`.** Present in the manifest; neither real project had a
  non-empty one, so there is nothing to design against yet.
- **Writing semantic YAML as an export target.** Excepting the graduation path in
  §11.6, breakdown reads semantic layers; it does not become one.
- **dbt Cloud SL (`cloud`) removal.** It stays — still right for a shop with a
  working SL credential that does not want breakdown holding warehouse access.
  Only `local` is superseded (roadmap 2.13).

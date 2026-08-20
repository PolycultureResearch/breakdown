# YAML reference

A metric tree is one YAML file, and this page is the whole of what the parser
accepts: every block, every field, and the rules each one is checked against.
Use it when authoring a tree of your own. The
[README](../README.md) covers what a tree is and how to run one, and
[the model and its assumptions](model.md) covers how to read what a tree
produces.

## `tree` (optional)

A tree's identity as a document: what it is called, who owns it, and
optionally a target it is being held to. **Every field is optional,
including the block itself**, so a tree can declare only a title, or nothing at
all and take its name from its filename. Most trees have no `goal`.

```yaml
tree:
  title: "Marketing"
  description: "Paid, organic and lifecycle, down to the campaign"
  owner: "growth@acme.com"
  period: "FY27"               # free-form label, shown on the index card
  goal:                        # optional — a tree of any lifetime may have one
    metric: paid_signups
    target: 200
    direction: up              # up | down — which way is winning
    deadline: "2026-09-30"     # YYYY-MM-DD, optional
```

- **`goal.metric` must resolve to a metric in this tree.** A goal naming a
  metric that doesn't exist is a parse error, not a silently blank card.
- **`goal.direction` defaults from the named metric's own `direction`** (see
  [`metrics`](#metrics)) when that metric declares one. Declaring both and
  disagreeing is an error; a goal on a `neutral` metric must state its own.
- **`period` is a label, not a parsed date range.** `"2026-Q3"`, `"FY27"` and
  `"2026-2031"` are all fine; the value is shown, never interpreted.
  `deadline` is the machine-readable date, and is optional too.
- **`title` is display-only.** The id is always the filename stem, which is
  what `#tree=` deep links and `/trees/{id}/…` routes use.

The block is ignored by builds that predate it, so trees can be annotated
before upgrading, and a tree with no `tree:` block loads on every build. There
is no migration.

## `provider`

Controls how metric time-series data is fetched.

```yaml
provider:
  type: mock           # mock | local | cloud | dbt | warehouse | none
  project_path: "..."  # required for type: local and type: dbt
  target: "..."        # optional for type: dbt (defaults to the profile's target)
  profiles_dir: "..."  # optional for type: dbt (defaults to $DBT_PROFILES_DIR, then ~/.dbt)
  environment_id: "..."  # required for type: cloud
  host: "..."            # required for type: cloud; optional for warehouse (read from profile)
  token: "..."           # required for type: cloud; warehouse: use this OR profile
  http_path: "..."       # required for type: warehouse
  profile: "..."         # warehouse: Databricks CLI OAuth profile (alternative to token)
  catalog: "..."         # optional for type: warehouse
  schema: "..."          # optional for type: warehouse
```

| Type | Description |
|------|-------------|
| `mock` | Deterministic synthetic data that respects the tree structure (formula nodes satisfy their formulas, probabilistic children co-move with parents). No config needed. Use for development and testing. |
| `local` | Queries a dbt project on disk via the MetricFlow CLI (`mf query`). Requires `project_path`. **Superseded by `dbt` for most trees**; see below. |
| `cloud` | Queries the dbt Semantic Layer API via the `dbt-sl-sdk`. Requires `environment_id`, `host`, and `token`. |
| `dbt` | Reads your dbt project's own `target/semantic_manifest.json` (written by plain `dbt parse` on **dbt Core**) and generates the SQL for each metric, running it over the connection in the project's `profiles.yml`. **No dbt Cloud, no Semantic Layer credential, no service token, and no new credentials of any kind.** Requires `project_path`. A node may override what dbt declares with its own `bind:` block. Executes on **BigQuery, Databricks, DuckDB, Postgres or Snowflake**, whichever your project's own target already uses. |
| `warehouse` | Runs each metric's own `sql` directly against a warehouse (currently Databricks SQL). Use when the semantic layer isn't queryable, so the analyst mirrors governed definitions in SQL. Requires `http_path` plus **one of**: a PAT `token` (with `host`), or a Databricks CLI OAuth `profile` created by `databricks auth login --profile <name>` (host is read from the profile). |
| `none` | No data is ever fetched; the tree is a **cold-start tree** of declared beliefs (`assumed` is an accepted alias). Only what-if simulation is available; every non-formula node needs a `baseline` and every probabilistic edge an explicit prior. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |

**Installing the extra.** The base install is the whole product. Engine, API,
UI and MCP server all ship with it, plus the **`mock` provider**, which is
enough to run the bundled example tree and every analysis in the README.
Connecting to real data pulls in a vendor SDK, and those are **extras** you
opt into:

| You want to use | Install | Brings in |
|---|---|---|
| `mock`, or cold-start `none` | `pip install metric-breakdown` | nothing |
| `local` (MetricFlow CLI) or `cloud` (dbt Cloud Semantic Layer) | `pip install 'metric-breakdown[dbt]'` | `dbt-metricflow`, `dbt-sl-sdk` |
| `warehouse` (direct SQL) | `pip install 'metric-breakdown[databricks]'` | `databricks-sdk`, `databricks-sql-connector` |
| reading a dbt project's own metric definitions (the `dbt` provider) | `pip install 'metric-breakdown[dbt-bridge]'` | `sqlglot` |
| running that generated SQL on **BigQuery** | `pip install 'metric-breakdown[bigquery]'` | `google-cloud-bigquery` |
| all of them | `pip install 'metric-breakdown[all]'` | all of the above, **except on Python 3.14**, where it installs `databricks` and `dbt-bridge` and omits `dbt` (see below) |

`dbt-bridge` is deliberately not part of `dbt`, and depends on nothing from dbt
Labs: reading the semantic manifest `dbt parse` already wrote needs neither
dbt-core, a warehouse adapter, nor the `mf` binary. The manifest is a resolved
JSON artifact, so breakdown models the subset it reads itself and the extra is
one package. That is what keeps the `dbt` provider free of anyone else's Python
ceiling.

This is not cosmetic: the extras are ~66 packages and ~120 MB that most installs
never touch, and dbt-core in particular drags in a large tree of its own.
Selecting a provider without its extra fails with the exact command to run
rather than an `ImportError` traceback, and `breakdown doctor --tree …`
reports it as its own check.

> **Python 3.14 and the `dbt` extra.** `dbt-metricflow` and `dbt-sl-sdk` both
> declare `requires-python < 3.14`, so **`pip install 'metric-breakdown[dbt]'`
> fails to resolve on 3.14**. Use 3.13 or earlier for the `local` and `cloud`
> providers. `[all]` degrades rather than failing there: it installs
> `databricks` and `dbt-bridge`, which work on 3.14, and omits `dbt`. The
> **`dbt` provider is unaffected** and is the one to use on 3.14, since it
> needs neither dbt-core nor the `mf` binary.

A tree that names `local` but is served entirely from committed snapshots (see
[Snapshots](deploying.md#snapshots-fetch-once-refit-forever)) needs neither the
extra nor a dbt project: the extra is only required when a query actually
reaches the provider. Until breakdown is published to PyPI, a checkout's
`uv sync` already installs every extra, because its dev group asks for `[all]`.
See the README's [Installation](../README.md#installation) section.

**Your dbt filters come across.** Most real dbt metrics narrow their measure
with `filter:`, whether regional revenue, paid signups, or orders excluding
test accounts, and breakdown imports the predicate along with everything else.
There is nothing to write: the binding's filter comes from your
`semantic_manifest.json` and it is **not a field you can author**. If you want a hand-written filter, put
it in `bind.sql`, which already expresses every one of them:

```yaml
- name: food_revenue
  source: dbt.metrics.food_revenue
  bind:
    sql: SELECT * FROM analytics.fct_orders WHERE is_food_order
    grain_key: order_id
    time_column: ordered_at
    agg: sum
    measure: amount
```

What imports is deliberately narrower than what dbt can express, and everything
outside it is **refused by name** rather than approximated. A metric breakdown
cannot translate exactly is listed as skipped, never served as a different
number under your governed metric's name. Today a predicate imports when every
reference in it is a *categorical dimension on the metric's own semantic model*:

```
{{ Dimension('order__is_food_order') }} = true          ✅  imported
{{ Dimension('order__region') }} IN ('US', 'CA')        ✅  imported
{{ Dimension('customer__country') }} = 'US'             ⏭️  skipped — a join
{{ TimeDimension('metric_time', 'week') }} >= '2024-01-01'  ⏭️  skipped — time grain
{{ Metric('revenue', group_by=['customer']) }} > 1000   ⏭️  skipped — a subquery
```

A filter on a `ratio` or `derived` metric is also skipped. Those become formula
edges over metrics referenced *by name*, and a name carries no scope, so the
edge would silently be over the unfiltered metric. Express the scoped side as
its own dbt metric and the edge picks it up.

The rule is all-or-nothing per metric. If one conjunct of one filter does not
resolve, the whole metric is skipped. There is no partial filter, because a
dropped conjunct is a larger number wearing the right name. `breakdown doctor`
lists what was skipped and why, counts the metrics that did import a filter, and
[proves each one actually narrows](deploying.md#checking-connectivity-breakdown-doctor)
against your warehouse.

> **A filtered node is smaller than the dashboard tile of the same name**, by
> design. That is what the dbt metric says. *Show query* on the node card
> displays the predicate in the generated SQL, which is where to check when a
> number looks low.

**Distinct counts and slicing.** A `count_distinct` metric's slices overstate
it whenever one entity holds several values of a dimension inside a period: a
subscription `active` in the morning and `cancelled` by evening is counted once
in the metric and once in each status. Declare how to resolve it and the slices
sum exactly:

```yaml
bind:
  agg: count_distinct
  measure: user_id
  entity_key: user_id
  entity_grain:
    resolve: last        # last | first | error
```

`resolve` has no default: `first` and `last` answer different questions (*what
state did they arrive in* vs *what state did they end in*), and `error` asserts
the data is already single-valued, which `breakdown doctor` then verifies.
Without it the slices are reported as overlapping, the overlap is quantified,
and contribution shares are withheld rather than computed against a total the
slices do not sum to.

**Bind entity flows to a state table, not an event table.** With `entity_grain`
declared, a slice panel also reports *movement between windows*: how many
entities are new, churned, retained, or **migrated** from one slice to another.
That is what tells you a platform switch (`−1` on iOS, `+1` on web, total
unchanged) is one user moving rather than two offsetting causes.

Those labels assume the relation has **one row per entity per period**, a daily
state table. On an *event* table, where a row means "something changed", an
entity only appears in windows where it changed, so `new` means *its first event
in this window*, not a new entity. The counts are still arithmetically correct
and migration still nets to zero, but they answer a different question than
their names suggest.

breakdown cannot tell the two apart from the schema, so it reports
`retention_share`, the fraction of reference-window entities that reappear,
and raises a caveat below 5%, the signature of an event table. Treat
that caveat as a prompt to check what the relation records, not as a verdict on
your data. If you want membership semantics, bind to a relation with one row
per entity per period.

**Moving from `local` to `dbt`.** Both read a dbt project on disk with no dbt
Cloud, but `local` shells out to `mf query` once per metric *and once per
slice*, behind a 120-second timeout, and needs the `mf` binary, which is why
the `dbt` extra does not work on Python 3.14. The `dbt` provider runs in
process, groups multiple dimensions in one query, and can show you the SQL
behind every number.

```yaml
provider:
  type: dbt                     # was: local
  project_path: /path/to/dbt    # unchanged
```

Credentials, target and warehouse all come from the project's own
`profiles.yml`, so there is nothing else to configure. You do need the driver
for your adapter, the same one your dbt adapter already depends on, which is
why it is not bundled: `bigquery` (`metric-breakdown[bigquery]`), `databricks`
(`metric-breakdown[databricks]`), `duckdb`, `psycopg2-binary` for Postgres, or
`snowflake-connector-python`. On BigQuery the profile's `method` is honoured:
`oauth` (Application Default Credentials), `service-account`, and
`service-account-json`.

**It is not a drop-in for every tree.** `local` hands a metric name to
MetricFlow, which plans the SQL, so it serves things the `dbt` provider refuses
rather than approximates: cumulative metrics, derived metrics that offset an
input in time, aggregations with no additive decomposition (`min`, `max`,
`median`, `percentile`), conversion metrics, `non_additive_dimension`, and
filters that reach across a join or into a time dimension. On two real dbt
projects that was 2 of 24 and 8 of 86 metrics.

Rather than guess, ask about *your* tree:

```bash
dbt parse                       # in the dbt project
breakdown doctor --tree tree.yml
```

The `dbt provider migration` check either says every metric translates, or
names the ones that need MetricFlow. A tree can also mix the two: keep `local`
for the metrics that need it, or give a node its own `bind:` block with the SQL
you want and move the rest.

For `local`, `cloud` and `dbt`, the metric queried from the semantic layer is the last segment of `source` (e.g., `source: jaffle_shop.metrics.revenue` queries the metric `revenue`); the result is exposed in the tree under `name`. For `warehouse`, each metric carries its own `sql` (see the `metrics` table) and is keyed by `name`. The data window defaults to `2024-01-01`–`2024-04-09` and is set with `--start-date` / `--end-date` (or the `BREAKDOWN_START_DATE` / `BREAKDOWN_END_DATE` / `BREAKDOWN_TREE` environment variables).

**Secrets in config.** Any provider string field may reference an environment variable with `${VAR}` syntax (e.g. `token: ${DATABRICKS_TOKEN}`), so a tree can be committed without embedding credentials. A referenced variable that isn't set raises a clear error at load time. The `warehouse` provider's `profile` avoids secrets entirely. Credentials come from the Databricks CLI's OAuth token cache, so nothing sensitive lives in the tree or the environment.

## `metrics`

Each metric entry supports the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier used throughout the tree |
| `source` | string | dbt Semantic Layer metric path (e.g., `jaffle_shop.metrics.revenue`) |
| `grain` | string | The metric's natural grain: `day` (default), `week`, or `month`. It is fetched, fitted, and attributed at this grain, never below it. See [Grains](#grains). |
| `kind` | string | Temporal aggregation kind: `flow` (default, sums over time), `stock` (a point-in-time level, takes the last value), or `rate` (a ratio, never auto-aggregated). See [Grains](#grains). |
| `sql` | string | For the `warehouse` provider: a SQL query returning columns `date` and `value`, with `:start_date` / `:end_date` named parameters, one row per period at the metric's `grain`. Ignored by other providers. |
| `description` | string | Optional human-readable description |
| `parents` | list | Names of metrics that causally influence this one |
| `formula` | string | Arithmetic expression over parent names (e.g., `"order_count * average_order_value"`). Enables Shapley attribution. |
| `priors` | dict | Bayesian priors for the causal coefficients (see below) |
| `lags` | dict | Per-parent time lag in grain steps **at the node's grain** (days for a daily node, weeks for a weekly one). On a probabilistic node, regresses the child on each parent's value `N` steps earlier; combined with `formula`, declares a cohort-aligned lagged identity. See [Lagged regressors](#lagged-regressors). |
| `expected_signs` | dict | Per-parent declared coefficient direction (`positive` \| `negative`) on a probabilistic node. **Not a prior.** The fit is unconstrained, but a posterior that contradicts the declaration raises a `sign_warnings` diagnostic (reported in `/analyze`, `/metrics`, RCA responses, and the UI). |
| `dimensions` | dict | Declared slicing dimensions, `name: provider_dimension` shorthand (`region: customer__region`) or a mapping with `source`, `top_k`, `values`, `weight`. Enables `POST /rca/{name}/slices` and the MCP `slice_metric` tool, which localize a gap within the metric (which geo, plan, app version). Analysis-time only; never affects fetching at startup, fitting, or tree attribution. See [Dimensions (slicing)](#dimensions-slicing). |
| `seasonality` | list | Periodic components to include in the BSTS model. Periods are in grain steps at the node's grain. |
| `trend` | string or dict | Local-level (random-walk) trend. `trend: linear` uses the default step-size prior HalfNormal(0.05); `trend: {type: linear, sigma: 0.1}` widens it so the trend may absorb faster drift. Only `type: linear` is supported. |
| `baseline` | number or dict | **Cold-start mode only.** Asserted operating point for a tree with no data: `baseline: 1200` (point) or `baseline: {low: 800, high: 1600}` (central 90% interval), in mean-per-period units at the node's grain. `distribution: Normal` (default) or `LogNormal`; the latter reads `[low, high]` on the log scale, needs `low > 0`, and is the natural shape for an order-of-magnitude belief about a positive quantity. Rejected on formula nodes, since theirs derive from parents so the identity holds. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |
| `plausible` | dict | **Cold-start mode only.** Declared honesty band `{min, max}` (either bound may be omitted, at least one required). Belief draws are **truncated** to it at sampling time (rejection resampling, so no mass piles up on the bound, and `min: 0` means customer counts cannot be drawn negative), and it stands in for historical min/max in the what-if extrapolation flags. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |
| `format` | string or dict | UI display hint for the node card's big number. Presentation only, no effect on modeling. See [Display format](#display-format). |
| `direction` | string | Which way is good news, for UI coloring only: `up_is_good`, `down_is_good` (costs, tickets, time-to-X), or `neutral` (gray, no judgment). Arrows stay directional; only the green/red coloring follows the declaration. Note: a stored-negative flow like churn MRR is `up_is_good`, since moving toward zero means less churn. **There is no default.** An undeclared metric serializes `direction: null` and renders like `neutral`, gray with no good/bad claim, because green means "improved", and on a metric nobody classified that is a claim the tree never made. Declare it on anything you want coloured. |

## Priors

Priors apply when the relationship with a parent is probabilistic (no formula). They are stated in **business units**: `mu: 0.1` below means "each additional session is worth ~0.1 orders". Internally the model fits on z-scored data, and breakdown translates the prior into normalized space automatically. The posterior reports both `beta` (normalized) and `beta_raw` (business units).

```yaml
priors:
  coefficient:
    distribution: "Normal"
    params: { mu: 0.1, sigma: 0.02 }
```

Supported distributions and their parameters:

| Distribution | Params | Use when |
|--------------|--------|----------|
| `Normal` | `mu`, `sigma` | You have a point estimate and uncertainty |
| `HalfNormal` | `sigma` | The effect must be positive |
| `Exponential` | `lam` | Positive effect, most mass near zero |
| `LogNormal` | `mu`, `sigma` | Positive, right-skewed effect |

**Per-parent priors.** `coefficient` sets the default prior for every parent. To override a specific parent, add its name as a key alongside `coefficient`. The named prior wins for that parent, and the rest fall back to `coefficient` (or `Normal(0, 1)` if `coefficient` is absent):

```yaml
priors:
  coefficient:                          # default for all parents
    distribution: "Normal"
    params: { mu: 0.1, sigma: 0.05 }
  marketing_spend:                      # override for one parent (must be a parent name)
    distribution: "HalfNormal"
    params: { sigma: 0.2 }
```

Every key under `priors` must be either `coefficient` or the name of a parent; any other key is rejected at parse time. Each parent's prior is scaled into normalized space using that parent's own units.

**Declared signs (`expected_signs`).** When you *know* which direction an effect should run ("more engagement → less churn"), declare it instead of forcing it:

```yaml
- name: churn_mrr
  source: my.metrics.churn_mrr
  parents: [paid_cmau]
  expected_signs: { paid_cmau: positive }   # churn_mrr is stored negative: more actives should mean less-negative churn
```

Unlike a `HalfNormal` prior, this never constrains the fit. After fitting, the engine checks the `beta_raw` posterior: if less than 10% of its mass lies on the declared side, the fit carries a `sign_warnings` diagnostic naming the parent, the posterior probability, and the mean. A contradicted sign is usually not a bug in the fit. It means the edge as defined answers a different question than you meant. The classic case is **scale confounding**: regressing a dollar flow on a user count when both grow with the business. The learned sign reflects "bigger base → more of both," swamping the per-user effect you intended. The fix is to redefine the edge as **rates on rates** (e.g. churn *rate* on active *share*), not to constrain the sign.

## Seasonality

```yaml
seasonality:
  - period: 7      # in grain steps: 7 on a daily metric is weekly
    name: weekly
```

Each seasonality component is modeled with up to 2 Fourier harmonics (4 parameters: sin/cos × 2 harmonics). `period` is expressed in the node's own grain steps, so `period: 7` means weekly on a daily metric and is meaningless on a monthly one.

**Declare only seasonality your fit window can see.** Two constraints, both enforced:

- **Period vs. grain.** A harmonic needs more than two steps per cycle to be distinguishable from the level (Nyquist), so `period` must be ≥ 3, and the second harmonic is dropped below `period: 5`. Dropped harmonics are reported in the fit's `seasonality_warnings` diagnostic.
- **Period vs. data.** Identifying a component takes at least two full periods *inside the fit window*, and RCA fits stop at `analysis_start`, so the window is shorter than your data. A `period: 365` component on a few months of history is unidentifiable and will soak up degrees of freedom the parents need; it too lands in `seasonality_warnings`. RCA responses report these warnings per node (with the fitted window under `fit_window`), so an unidentifiable component is flagged in the result, not just the server log. The fix is more history (an earlier `--start-date`), not a different reference window.

## Formula

Formulas express exact arithmetic relationships between a metric and its parents. The expression is a restricted Python arithmetic expression: only the operators `+`, `-`, `*`, `/`, `**` and named parent metrics are allowed. Function calls and attribute access are rejected at parse time.

```yaml
- name: net_revenue
  source: my.metrics.net_revenue
  formula: "gross_revenue - cost_of_goods_sold"
  parents: [gross_revenue, cost_of_goods_sold]

- name: revenue
  source: my.metrics.revenue
  formula: "order_count * average_order_value"
  parents: [order_count, average_order_value]

- name: conversion_rate       # `denominator` defaults from the `num / den` formula
  source: my.metrics.conversion_rate
  kind: rate
  formula: "order_count / daily_sessions"
  parents: [order_count, daily_sessions]
```

Every metric needs a `source` **except** a formula node, which may omit it to be
*derived* from its parents instead. See [Rates over true-zero
periods](#kinds) for what that changes, including what `unexplained` then means.

When a formula is defined, the BSTS model fits the **residual** (`y - formula(parents)`) rather than using parent regressors. This correctly captures the structural relationship and surfaces unexplained variance in the residual.

**At most 10 parents on a formula node.** Exact Shapley attribution enumerates
every coalition, so the work doubles with each parent. End to end through an
RCA, 10 parents is ~3.5s, 12 is ~20s, 14 is ~80s, all of it holding the tree's
lock. An 11th parent is **refused by name** rather than quietly approximated,
because a sampled or truncated Shapley value is a different number from the one
you asked for, and breakdown does not substitute one for the other.

The remedy is to **split the node into intermediate sums**. Group some parents
under their own formula node and make that node the parent here:

```yaml
- name: other_revenue          # the intermediate sum
  source: my.metrics.other_revenue
  formula: "services_revenue + partner_revenue + marketplace_revenue"
  parents: [services_revenue, partner_revenue, marketplace_revenue]

- name: total_revenue          # now 2 parents instead of 4
  source: my.metrics.total_revenue
  formula: "product_revenue + other_revenue"
  parents: [product_revenue, other_revenue]
```

That preserves the identity exactly, so every attribution stays exact, and it
usually reads better, since the intermediate node is a number someone in the
business already talks about. (The same cap applies to what-if scenario sources,
which enumerate the same coalitions.)

## Lagged regressors

Some causal effects show up with a delay. The README's motivating example is support tickets driving churn *weeks later*. A `lags` dict regresses the child on each parent's value `N` grain steps earlier, at the **child's** grain (days for a daily child, weeks for a weekly one):

```yaml
- name: churn_rate
  source: my.metrics.churn_rate
  kind: rate
  denominator: active_customers   # a window's churn rate is Σchurned / Σactive
  parents: [support_tickets]
  lags: { support_tickets: 21 }   # churn responds to tickets from 3 weeks earlier (daily node)
```

Rules:
- Every `lags` key must be a parent; every value must be an integer ≥ 1 (grain steps at the node's grain).
- The engine shifts each parent by its lag and trims the leading `max(lags)` rows so all series align with no NaNs. It raises if fewer than 10 rows remain.

**Cohort-aligned lagged identities.** `lags` combines with `formula` to declare an *exact* identity over time-shifted parents: `A[t] = f(each parent shifted back by its lag)`. This is how cohort conversion gets a deterministic form instead of a blended same-period ratio or a fully probabilistic edge:

```yaml
- name: conversions
  source: my.metrics.conversions
  formula: "trial_starts * cohort_rate"
  parents: [trial_starts, cohort_rate]
  lags: { trial_starts: 14 }   # today's conversions come from the cohort that started 14 days ago
```

Shapley attribution and the residual fit both read each lagged parent from windows shifted back by its lag, so the identity and its exact attribution both hold cohort-by-cohort.

## Grains

Metrics have different natural time grains: signups are daily events, a cohort conversion rate is only meaningful per week, MRR is a monthly snapshot. Forcing everything onto a daily spine manufactures fake sample size (a monthly value repeated 30 times is still one observation) and makes per-day ratios degenerate on low-volume days. Instead, each node declares its natural `grain` and is **fetched, fitted, and attributed at that grain, never below it**:

```yaml
- name: trial_starts            # daily flow (defaults: grain day, kind flow)
  source: my.metrics.trial_starts

- name: trial_conversion_rate   # weekly cohort rate
  source: my.metrics.trial_conversion_rate
  grain: week
  kind: rate
  denominator: trial_starts     # the cohort it is a share of

- name: conversions             # weekly identity over a daily flow and a weekly rate
  source: my.metrics.conversions
  grain: week
  formula: "trial_starts * trial_conversion_rate"
  parents: [trial_starts, trial_conversion_rate]
```

**Kinds determine aggregation.** Resampling a series upward is only well-defined once you know how it aggregates: `flow` metrics **sum** (orders, new MRR), `stock` metrics take the **last value** (total MRR, account balances), and `rate` metrics can never be auto-aggregated. The average of daily ratios is not the coarser ratio, so a rate must be *declared* at the grain it's consumed at, recomputed from its components.

`rate` covers more than the metrics whose names end in `_rate`. Averages (`average_order_value`), per-unit intensities (`emails_per_subscriber`) and durations (`time_to_first_response`, `page_speed`) are all ratios: the mean of a month of daily averages is not that month's average. Since `kind` defaults to `flow` and a missing declaration is indistinguishable from a deliberate one, the parser **warns** when a metric with a ratio-shaped name never declared a `kind`, since it would otherwise be silently summed. It is a naming heuristic, so it never rejects the tree, and declaring `kind: flow` explicitly silences it for the cases where the name misleads.

**Mixed-grain rules** (enforced at parse time):
- A parent may never be **coarser** than its child. Downward disaggregation is undefined.
- A **finer flow/stock** parent is automatically resampled up to the child's grain (sum / last). In the example above, `conversions` at week grain sees the *weekly sum* of `trial_starts`.
- A **finer rate** parent is an error. Declare the rate at the child's grain.
- The finer grain must nest in the coarser: days tile weeks and months, but weeks straddle month boundaries, so a weekly parent under a monthly child is an error.

**Period labels are period starts** everywhere: days at midnight, weeks on Monday (ISO), months on the 1st. Partial edge periods are dropped, never zero-filled, so a coarse metric's series may end a few days before the raw data window does.

**Windows snap per node.** RCA windows stay day-resolution dates in the API; each node interprets them as the whole periods fully inside. A node whose window holds no whole period reports `"status": "window_shorter_than_grain"` instead of failing the RCA, and every node reports its `grain` and `effective_windows`. Windows that snap to a single period suppress the bootstrap CI (`ci_status: "degenerate_single_period"`) rather than reporting a falsely-precise interval. `window_shorter_than_grain` is one of several per-node statuses. See [Per-node `status`](api-reference.md#per-node-status--one-bad-node-does-not-end-the-analysis) for the full set and what each means.

**Gaps, and what happens to them.** Every provider aligns its result onto the
spine of whole periods inside the loaded window, and what happens to a missing
period depends on **which edge of the series it is on** and on the metric's
`kind`. Partial edge periods are always dropped. For the `warehouse` provider the
SQL owns the aggregation, so return one row per period at the declared grain,
labeled by period start.

| Where the gap is | `flow` | `stock` | `rate` |
|---|---|---|---|
| **Leading**, before the source's first row | filled with `0`, **with a warning naming the invented periods** | error (nothing to forward-fill from) | error |
| **Interior**, a hole in the middle | filled with `0`, with a warning | forward-fill, with a warning | error |
| **Trailing**, after the source's last row | **trimmed**, not filled | trimmed | trimmed |

- **Trailing gaps are trimmed rather than filled** because periods after the last
  row are *not yet loaded*, not zero. A lagging mart should end the series early
  rather than manufacture a collapse at the tail. This is what `data_through`
  reports per metric.
- **Interior gaps are warned about** because a three-day ETL outage becomes three
  zero days, which is indistinguishable from a real collapse, and RCA will
  happily name it as the root cause.
- **A query returning no rows at all** keeps the full zero spine for flows and
  draws no leading warning. An all-quiet window is a legitimate flow series, and
  the provider that knows the result was empty says so itself.

**A metric that started partway into the loaded window is zero-filled before its
first row.** A product launched in March, a channel switched on in week 3, a
metric the warehouse only began recording last quarter: with `kind: flow` all of
these get a run of fabricated zeros back to your `--start-date`. That is not
harmless padding. The fit sees a manufactured level shift and a manufactured
trend on a node RCA can then rank as a cause. breakdown now **warns and names the
fabricated periods**, so check your startup logs for it.

The honest fix is a **later `--start-date` for that tree**. Start the window
where the metric actually starts, and fit only observed periods. (Trimming the
leading run automatically is unavailable on purpose. Per-grain frames are
assembled by inner join, so dropping one node's leading periods would delete them
for *every* metric at that grain, a whole tree losing January because one node
launched in March.) If the late-starting metric matters less than the history the
rest of the tree needs, the alternative is to split it into its own tree with its
own window.

**Rates over true-zero periods.** A seasonal business has stretches where the
denominator is genuinely zero: nothing on sale, no sessions, no sends, nobody
churned. A rate is **undefined** there, not low. breakdown carries that through
rather than inventing a number. A rate period with no value stays undefined all
the way to the payload, where it is `null`, and the UI draws a break in the
line rather than a dip to zero. Filling `0` would assert the
average was zero; forward-filling would assert last period's rate applied.
Neither is a fact.

What an undefined period costs, stated once so you can plan around it:

| Consumer | Policy for an undefined period |
|---|---|
| The window aggregate | Drops out of `Σnumerator / Σdenominator`, so the window rate stays defined. A window where *every* period is undefined has no value, and the node reports `undefined_over_window` instead of a number. |
| The fit (`POST /analyze`, RCA's on-demand fits) | **Refused.** The node is unfittable over any window containing one, and reports `fit_failed` with the periods named. Nothing is imputed, and the row is not dropped (that would re-date every later period). |
| A formula node that reads the rate | Refused over that window, with `attribution_failed` and the dates. A factor with no value has no decomposition. |
| The grain frame | The period keeps its row. Contiguity is about dates, not values. |

The most useful thing you can do about it is **declare the rate's
`denominator`**, below. It makes the window aggregate correct, and it lets
breakdown tell an undefined period (denominator zero, a fact) from a missing
one (denominator non-zero, an ETL problem), which it says in the startup log.

The stronger remedy, when the numerator and denominator are themselves metrics
worth having, is to declare them as `flow` nodes, which fill to zero honestly,
and make the rate a **derived** node over them:

```yaml
- name: orders            # flow: 0 in the dark window is a fact
  source: my.metrics.orders
- name: sessions          # flow
  source: my.metrics.sessions
- name: conversion_rate   # derived: no `source`, so nothing is fetched
  kind: rate
  formula: "orders / sessions"
  parents: [orders, sessions]
  denominator: sessions
```

This buys exact Shapley attribution on the rate, and it keeps the dark window
visible as what it was, no traffic, instead of an invented number. Coarsening
the grain until every period has a denominator is the other option, and the
worse one. It throws away resolution everywhere to fix a problem that exists in
a few windows.

**`source` is optional on a formula node, and only there.** Its presence is a
real choice, not a formality:

- **With a `source`** the node is *measured*. breakdown fetches it like any
  other metric, checks the identity against what came back **at load** (a
  warning names the drift and the worst periods), fits the residual
  `y − formula(parents)`, and `unexplained` means *the identity missed reality
  by this much*. Point `source` at the governed metric even when you could
  compute it. That check is the whole value.
- **Without one** the node is *derived*: its series is computed from its
  parents, period by period, and nothing is ever fetched for it. `unexplained`
  is then **0 by construction**, which is a different fact from a measured
  zero, and the output says so everywhere: the API sends `unexplained_status:
  "definitional"` and `derived: true`, the RCA table labels the row *unexplained
  — none by definition*, and the exported report spells out that nothing was
  reconciled.

A derived node may not declare `bind`, `sql`, `dimensions` (there is nothing to
ask the provider to slice) or `lags` (deriving period *t* from parents at
*t−lag* leaves the first periods of any window underivable). Each is refused by
name at parse time.

**A rate declares its `denominator`.** A window's rate is
`Σnumerator / Σdenominator`, never the average of the per-period ratios, so
breakdown needs to know what the rate is a rate *of*:

```yaml
- name: churned_subscriptions
  source: my.metrics.churned_subscriptions
- name: churn_arpu
  source: my.metrics.churn_arpu
  kind: rate
  denominator: churned_subscriptions   # weights each period; zero -> undefined
```

The rules:

- It names **a metric in this tree**, and it is *not* a parent; no DAG edge is
  created. It says how the series aggregates over time, not what causes it.
- It must be a `flow` or a `stock` (rates do not sum) at a grain that is not
  coarser than the rate's own.
- It is **derived where unambiguous**: from a `formula: "num / den"`, from an
  agreeing `dimensions[].weight`, or from a `bind:` ratio whose denominator
  names a tree metric. Declaring it twice and differently is an error.
- `dimensions[].weight` now **defaults from it**, so the blend weights for a
  sliced rate and its window aggregate are the same fact. A rate with a
  dimension still needs one, from wherever.
- A rate that declares one nowhere is **warned about, not refused**. The
  startup log and `breakdown doctor` name every such node, and its window
  aggregate falls back to the plain average of the defined periods. That
  fallback is wrong whenever the denominators differ, so declare one wherever
  you can establish it.

**But some rates genuinely have none, and inventing one is worse than leaving
it off.** A wrong denominator computes a confident wrong number; no denominator
computes a disclosed fallback. Three shapes to leave undeclared:

- **A mean duration whose cohort you do not have as a metric.** Mean days-to-
  activation is over *trials that activated*; if the tree only counts trials
  *started*, no series in it is the denominator. A near-miss is still wrong.
- **A ratio whose base is not in the tree.** Bounce rate is over sessions; if
  the tree carries only per-channel visitor counts, leave it.
- **A median, or any other non-linear summary.** `Σnumerator / Σdenominator`
  reconstructs a *mean*. A median page-load time is not that quantity for any
  pair of series, so no denominator is correct.

The test for a candidate denominator is arithmetic, not linguistic: does
`window_rate × mean(denominator) == mean(numerator)`? If the tree names that
numerator and the identity closes, the denominator is right. If it does not
close, the denominator is wrong. Treat that as evidence, not as tolerance.

**When the answer is "there is none", say so: `no_denominator`.** Leaving the
field off says two things at once, *nobody has looked at this metric yet* and
*this metric has no denominator*, and they want opposite responses. So the
answer is its own field, and its value is the reason:

```yaml
- name: page_speed
  source: my.metrics.page_speed
  kind: rate
  no_denominator: "a median — not Σnum / Σden for any pair of series"
```

- **The presence of the field is the answer; the value is the argument.** An
  empty reason is refused. The reason is what stops the question being re-opened
  by the next reader, and it is what `breakdown doctor` and the payload quote.
- **It is refused alongside a `denominator`**, and refused when anything else in
  the tree names one anyway (a `num / den` formula, a `dimensions[].weight`, a
  `bind:` ratio over a tree metric). Exactly one of the two statements is wrong
  and only you know which. It is refused on a `flow` or a `stock` for the same
  reason `denominator` is.
- **A rate with `no_denominator` cannot declare `dimensions`.** Blending slices
  needs a weight per slice, and there is none.
- **Nothing changes about the number.** The window value is still the mean of
  the defined periods; for a median it is the only number there is. What
  changes is that it stops looking like an unfinished tree. `breakdown doctor`
  passes and quotes your reason instead of advising a denominator you cannot
  supply, and the payload carries
  `window_aggregate: "period_mean_none_exists"` with
  `window_aggregate_reason`, as against `"period_mean_undeclared"` for a rate
  nobody has answered for. The UI labels the difference under the number.

Declaring it is not mandatory. It is worth doing on any rate you have already
thought about, because the alternative is thinking about it again next quarter.

**Data freshness.** breakdown tracks each metric's true data edge as it fetches and exposes it as `data_through` in `GET /meta`, the inclusive last date its last observed period covers. When sources disagree (one mart lags the others), the UI anchors every card's headline number, delta, and sparkline at the tree-wide edge via the **As of** selector (toolbar), which defaults to the oldest `data_through` across metrics and counts only periods *fully completed* by that date, so a calendar week the data edge cuts in half never becomes a headline number. The one case this cannot catch is a partially loaded most-recent period, where the mart wrote *some* rows for it. Detecting that needs load-completeness metadata on the mart side.

**Data-length guidance.** Fits need at least 10 whole periods at the node's grain, and coarser grains need proportionally longer windows (a monthly node wants roughly a year of history). Seasonality periods and lags are in grain steps: `period: 7` means weekly on a daily node and seven *months* on a monthly one (the parser warns about that).

## Dimensions (slicing)

Tree RCA says *which upstream metric* moved; slicing says *where inside it*:
which geo, plan tier, or app version. Declare the dimensions worth slicing a
metric by, and the slice endpoint/MCP tool can attribute its
window-over-window gap across the dimension's values:

```yaml
- name: signups
  source: my.metrics.signups
  dimensions:
    region: customer__region            # shorthand: name -> provider dimension id
    plan:
      source: subscription__plan_tier
      top_k: 6                          # slices kept individually (default 8); rest fold into __other__
      values: [pro, team, enterprise]   # optional pin-list, overrides top_k

- name: trial_conversion_rate
  source: my.metrics.trial_conversion_rate
  kind: rate
  formula: "conversions / trial_starts"
  parents: [conversions, trial_starts]
  dimensions:
    region: customer__region            # rate: weight defaults to the formula denominator
```

For the semantic-layer providers, `source` is the MetricFlow dimension
identifier (added to the query's `group_by`); the mock provider synthesizes
deterministic slices for any source; the warehouse provider does not support
slicing yet. A `rate` metric needs a `weight`, the tree metric whose sliced
shares blend the per-slice rates. It defaults from a simple `num / den`
formula's denominator and otherwise must be declared:
`region: {source: customer__region, weight: trial_starts}`.

Slicing runs **at analysis time**: breakdown fetches sliced series on demand
for the requested windows only, and they never enter the startup data, the
fits, or tree attribution. Attribution is exact. A flow/stock decomposes as a
sum over slices; a rate splits per slice into `within` (its own rate moved) and
`mix` (traffic shifted between slices). Slices are ranked by **excess
concentration** (`excess`), how much more of the gap a slice carries than its
baseline share predicts, with bootstrap credible intervals. Slices that don't
sum back to the metric are reported in a `reconciliation` block, never
silently rescaled. See `knowledge/dimensional_slicing_design.md` for the full
design.

## Display format

`format` controls how the UI displays a metric's **big number** on its node card. It is presentation only and never affects modeling, attribution, or the API's numeric values. Use the string shorthand for the common case, or a mapping for finer control:

```yaml
- name: revenue
  source: my.metrics.revenue
  format: currency          # shorthand for {style: currency}

- name: daily_sessions
  source: my.metrics.daily_sessions
  format:
    style: number           # currency | percent | number  (default number)
    unit: sessions          # small caption under the value; grows the card one line
    decimals: 0             # fixed fraction digits (default: automatic)
    compact: true           # k / M / B notation (default: auto — currency compacts large values)
    symbol: "$"             # currency symbol, when style is currency
```

Delta values (period-over-period change) always render as a percent; `format` applies to the big number only.

**Display defaults.** When a metric declares no `format`, the UI guesses one from naming conventions: names containing tokens like `mrr`, `arr`, `revenue`, `arpu`, `aov`, `usd`, `cost`, `spend` render as currency; `rate`, `pct`, `percent`, `share`, `ratio` render as percent; everything else as a plain number. This is presentation-only and an explicit `format` always wins, so declare one whenever the guess would be wrong.

## Cold-start mode (what-if with no data)

A tree with **no data provider** can still run what-if scenarios, on declared beliefs alone. The what-if engine's propagation core consumes operating points, edge slopes, and assumption effects; in cold-start mode all three are stated rather than fitted, so a pre-revenue company can simulate its business before the first row of data exists. The output quantifies the consequences of your assumptions: honestly wide intervals, never evidence.

A cold-start tree declares beliefs everywhere:

- **`baseline` on every non-formula node**: the asserted operating point, as a point (`baseline: 1200`) or a central-90% interval (`baseline: {low: 800, high: 1600}`), in mean-per-period units at the node's grain. Add `distribution: LogNormal` to read `[low, high]` on the log scale, the natural elicitation for an order-of-magnitude belief about a positive quantity ("between 2 and 40 signups"), whose support excludes zero by construction (`low` must be > 0). Formula nodes derive theirs per-draw from their parents so the arithmetic identity holds by construction; declaring one there is a parse error.
- **An explicit prior on every probabilistic edge** (parent-specific or shared `coefficient`). Priors are already stated in business units, and with nothing to fit the prior *is* the coefficient distribution. Coefficient draws are sampled from it directly. The fitted-mode fallback `Normal(0, 1)` is meaningless without data to set the scale, so it is not allowed here.
- **`plausible: {min, max}`** (optional; either bound alone is fine): the declared honesty band, consulted **at sampling time and after**. Belief draws are truncated to it by rejection resampling (so `min: 0` means a customer count cannot be drawn negative, and nothing piles up on the bound), and a *simulated* value outside it still raises the same extrapolation warning fitted mode derives from history. A `baseline` whose mass lies almost entirely outside its own `plausible` range is a contradiction and is refused with both declarations named. One consequence worth declaring bounds for: a formula that **divides** by a belief is refused when that belief's draws cross zero, because the ratio's Monte-Carlo mean would not exist and its centre would be a seed artifact. The remedy is a `plausible: {min: ...}` floor above zero (or a `LogNormal` baseline) on the divisor.

```yaml
- name: site_sessions
  source: assumed                       # provenance label; no provider is queried
  baseline: { low: 800, high: 1600 }
  plausible: { min: 0 }

- name: signups
  source: assumed
  parents: [site_sessions]
  baseline: { low: 10, high: 60 }
  priors:
    site_sessions:
      distribution: "Normal"
      params: { mu: 0.02, sigma: 0.01 } # ~2 signups per 100 sessions, stated as a belief
```

Propagation, do-operator semantics, draw alignment, and the Shapley source decomposition are identical to fitted mode. The response is labeled `mode: "cold_start"`, adds a per-node `baseline_ci_95` where the asserted baseline is a range, and carries cold-start caveats so the output can't be mistaken for estimates from data. When data arrives, the same YAML priors feed the fit, and posteriors replace priors with zero config changes.

**Serving a cold-start tree.** Declare `provider: type: none` and `breakdown serve` boots without fetching anything. That is not a degraded start; the tree simply has no data. Startup validates the declarations and fails loudly with the full list of blockers if any are missing (`breakdown doctor` runs the same check). `GET /meta` reports `"mode": "cold_start"`; endpoints that consume history (`/series`, `/analyze`, `/shapley`, `/rca`) return 422 pointing at `POST /simulate`, which runs scenarios with **no baseline window**. Operating points come from the tree, so a scenario passing `baseline_start`/`baseline_end` is rejected. The MCP `run_whatif` tool works the same way (omit the baseline dates).

**The UI boots what-if-first** on a cold-start tree: node cards show each metric's asserted operating point with its 90% belief range (formula nodes derive theirs), probabilistic edges are labeled with their stated priors (`β ~ 0.03 [0.01, 0.05] · belief`), the adjust panel's range strip renders from the declared `plausible` bounds, and results are labeled as consequences of beliefs. The Root cause tab is inert, since there is no history to explain. Try it with the bundled example:

```bash
uv run breakdown serve --tree breakdown/examples/cold_start_tree.yml
```

See [`model.md`](model.md) ("Reading cold-start output") before presenting results, and `knowledge/cold_start_design.md` for the full design.

**Graduating from cold start.** The tree you build pre-data *is* the tree you fit once data exists. The Bayesian promise is literal. When real numbers start flowing:

1. Swap the provider block (`type: none` → `local` / `cloud` / `warehouse`) and give each metric a real `source` (or `sql`). Nothing else in the tree changes.
2. Your `priors` carry over untouched. The same declarations that were sampled directly in cold start become the Bayesian priors of the BSTS fit, and the data updates them into posteriors. What-if flips from prior draws to posterior draws automatically; RCA becomes available.
3. Fitted mode ignores `baseline` and `plausible`. They stay in the YAML as a record of what you believed before the data arrived, and that record is worth keeping.

Two things to plan for. Each node needs at least **10 whole periods at its grain** before it can be fitted. A monthly tree waits most of a year for its first fit, so author cold-start trees at the finest grain you'll actually measure (weekly for most funnels; edge priors are per-parent-unit and carry over, but per-period `baseline` values would need rescaling). And check where you stand at any point with the doctor's **fit readiness** report:

```bash
uv run breakdown doctor --tree my_tree.yml --start-date 2026-01-01 --end-date 2026-08-01
```

It reports every metric's whole-period count against the fit minimum (`signups: 30/10 whole day periods` … `churn_rate: 4/10 — not fittable yet`), so you can watch the tree graduate metric by metric.

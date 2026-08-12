# breakdown

**An open engine for Bayesian metric tree construction and root cause analysis**

Metrics trees model causal relationships between business metrics and assist in diagnosing the root causes of changes in KPIs. Breakdown models your business metrics as a causal graph and uses Bayesian inference to learn the probabilistic relationships between them. Instead of asking "did revenue drop?", you can ask "which upstream metric drove it, and how confident are we?"

---

## Two kinds of causal relationships

Breakdown handles both types of relationships you find in real metric trees.

**Deterministic (formula-based):** Some metrics are arithmetic identities.

> `Revenue = Order Count × Average Order Value`

When revenue drops, you can decompose the gap exactly. Breakdown uses **Shapley value attribution** to distribute the revenue shortfall between `order_count` and `average_order_value` in a mathematically fair way — accounting for interaction effects that simpler approaches miss.

**Probabilistic (learned):** Other metrics have a causal effect that isn't computable by formula.

> Support ticket volume → Churn rate (weeks later)

There is no arithmetic connecting them, but historically they co-move. Breakdown learns these relationships from your time-series data using **Bayesian Structural Time Series (BSTS)** models. Each BSTS model decomposes a metric into trend, seasonality, and causal regression terms, producing a posterior distribution over the coefficient on each parent metric.

---

## How it works

### 1. Define your metric tree in YAML

```yaml
provider:
  type: mock  # or: local, cloud

metrics:
  - name: daily_sessions
    source: jaffle_shop.metrics.sessions

  - name: order_count
    description: "~10% session-to-order conversion — modeled probabilistically"
    source: jaffle_shop.metrics.order_count
    parents:
      - daily_sessions
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }

  - name: average_order_value
    source: jaffle_shop.metrics.average_order_value

  - name: revenue
    description: "Arithmetic identity — Shapley attribution available"
    source: jaffle_shop.metrics.revenue
    formula: "order_count * average_order_value"
    parents:
      - order_count
      - average_order_value
    seasonality:
      - period: 7
        name: weekly
```

### 2. Breakdown parses this into a DAG

The YAML is validated and compiled into a directed acyclic graph using NetworkX. Cycles and undefined parent references are caught at parse time.

### 3. For each metric, choose your analysis

- **BSTS sampling** (`POST /analyze/{name}`) — runs PyMC to fit a state-space model and returns a posterior over trend, seasonality, and causal coefficients.
- **Shapley attribution** (`GET /shapley/{name}`) — for metrics with a `formula`, computes how much of a period-over-period gap each parent is responsible for.

---

## Quickstart

**Requirements:** Python 3.11+

> **Installed as `metric-breakdown`, used as `breakdown`.** The name `breakdown`
> was already taken on PyPI, so that is the distribution name — but the command,
> the import package and everything in this documentation are `breakdown`.

```bash
pip install metric-breakdown
breakdown serve
```

Or, with [uv](https://github.com/astral-sh/uv), without installing anything:

```bash
uvx --from metric-breakdown breakdown serve
```

Or work from a checkout:

```bash
git clone https://github.com/PolycultureResearch/breakdown
cd breakdown
uv sync
uv run breakdown serve            # or: uv run python main.py serve
```

`breakdown --version` reports the installed version — the first thing to include
in a bug report.

### Provider extras

The base install is the whole product — engine, API, UI, MCP server — with the
**`mock` provider**, which is enough to run the bundled example tree and every
analysis in this README. Connecting to real data pulls in a vendor SDK, and
those are **extras** you opt into:

| You want to use | Install | Brings in |
|---|---|---|
| `mock`, or cold-start `none` | `pip install metric-breakdown` | — |
| `local` (MetricFlow CLI) or `cloud` (dbt Cloud Semantic Layer) | `pip install 'metric-breakdown[dbt]'` | `dbt-metricflow`, `dbt-sl-sdk` |
| `warehouse` (direct SQL) | `pip install 'metric-breakdown[databricks]'` | `databricks-sdk`, `databricks-sql-connector` |
| reading a dbt project's own metric definitions | `pip install 'metric-breakdown[dbt-bridge]'` | `sqlglot` |
| running that generated SQL on **BigQuery** | `pip install 'metric-breakdown[bigquery]'` | `google-cloud-bigquery` |
| all of them | `pip install 'metric-breakdown[all]'` | all of the above — **except on Python 3.14**, where it installs `databricks` and `dbt-bridge` and omits `dbt` (see below) |

`dbt-bridge` is deliberately not part of `dbt`, and depends on nothing from dbt
Labs: reading the semantic manifest `dbt parse` already wrote needs neither
dbt-core, a warehouse adapter, nor the `mf` binary. The manifest is a resolved
JSON artifact, so breakdown models the subset it reads itself and the extra is
one package. That is what keeps the `dbt` provider free of anyone else's Python
ceiling.

This is not cosmetic: the extras are ~66 packages and ~120 MB that most installs
never touch, and dbt-core in particular drags in a large tree of its own.
Selecting a provider without its extra fails with the exact command to run —
and `breakdown doctor --tree …` reports it as its own check — rather than an
`ImportError` traceback.

> **Python 3.14 and the `dbt` extra.** `dbt-metricflow` and `dbt-sl-sdk` both
> declare `requires-python < 3.14`, so **`pip install 'metric-breakdown[dbt]'`
> fails to resolve on 3.14** — use 3.13 or earlier for the `local` and `cloud`
> providers. `[all]` degrades rather than failing there: it installs
> `databricks` and `dbt-bridge`, which work on 3.14, and omits `dbt`. The
> **`dbt` provider is unaffected** and is the one to reach for on 3.14 — it
> needs neither dbt-core nor the `mf` binary.

A tree that names `local` but is served entirely from committed snapshots (see
[Snapshots](#snapshots-fetch-once-refit-forever)) needs neither the extra nor a
dbt project: the extra is only required when a query actually reaches the
provider.

Open `http://localhost:9090/ui` to explore the metric tree. The UI shows the DAG (formula vs learned edges, fit status), per-metric time series and posteriors in business units, and a full point-and-click RCA workflow: pick a target and two windows, run it, and read the answer off the graph — nodes tinted by direction of change, edges weighted by share of the gap explained, ranked causes with credible intervals in the sidebar. RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`) so an analysis can be shared as a URL.

By default the server loads the bundled `breakdown/examples/jaffle_shop_tree.yml` with mock data. Point it at your own tree and data window:

```bash
uv run breakdown serve --tree path/to/my_tree.yml --start-date 2025-01-01 --end-date 2025-06-30
```

`serve` binds to `127.0.0.1` without hot reload by default; use `--host 0.0.0.0` to accept outside connections (containers) and `--reload` while developing breakdown itself.

At startup, breakdown fetches the time series for every metric in the tree from the configured provider (mock, local MetricFlow, dbt Cloud Semantic Layer, or warehouse-direct SQL) and aligns them on date.

Run a Bayesian analysis on a metric:

```bash
curl -X POST "http://localhost:9090/analyze/order_count"
```

Get Shapley attribution for a formula node:

```bash
curl "http://localhost:9090/shapley/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
```

Run tests:

```bash
uv run pytest tests/ -v
```

---

## Deploying

### A shared instance with Docker

```bash
cp path/to/my_tree.yml tree.yml
export DATABRICKS_TOKEN=...        # whatever ${VARS} your tree references
docker compose up --build
```

The [`compose.yaml`](https://github.com/PolycultureResearch/breakdown/blob/main/compose.yaml) mounts `./tree.yml` read-only at `/config/tree.yml`, passes provider credentials through as environment variables, and healthchecks `GET /health`. The image is large (~2.5–3 GB — PyMC and its compiler toolchain); the first build takes a while.

Two things differ from a laptop run:

- **Credentials must be headless.** The Databricks CLI OAuth `profile:` flow opens a browser, which a container can't. Use `token: ${DATABRICKS_TOKEN}` in the tree's provider block instead (see [`provider`](#provider) for `${VAR}` interpolation). If you must reuse a profile, mount both `~/.databrickscfg` and `~/.databricks/token-cache.json` read-only into the container.
- **Startup failures degrade, not crash.** If the provider can't be reached (bad token, warehouse down), the server still starts: `GET /health` returns `{"status": "degraded", "error": …}`, data endpoints return 503, and the UI shows the error with a pointer to `breakdown doctor`. Fix the config and restart — no crash-loop to debug through.

### Checking connectivity: `breakdown doctor`

Before the first `serve` against real data — or whenever startup reports `degraded` — run:

```bash
uv run breakdown doctor --tree path/to/my_tree.yml
```

It walks the provider's auth chain step by step (tree parses → env vars set → CLI/profile/token valid → connection opens → every metric's query actually runs) and prints `[PASS]`/`[FAIL]` per step with copy-paste remediation for each failure. Exit code is non-zero if anything failed. Probes run over the last 7 days by default; override with `--start-date`/`--end-date`.

Two mode-specific checks ride along: a cold-start tree (`provider: none`) gets its declarations validated instead of a connection probe, and when you pass an explicit `--start-date`/`--end-date` window the doctor adds a **fit readiness** report — each metric's whole-period count against the 10-period fit minimum, the graduation check for a tree [moving from cold start to fitted mode](#cold-start-mode-what-if-with-no-data) — plus a **history headroom** report: whether the provider has history before your `--start-date`. Breakdown trains on everything you load, so an earlier start date strengthens every fit (and the default RCA reference windows) at no cost beyond fetch time.

For the **`dbt` provider**, `doctor` walks manifest → profile → connection →
bindings → dimensions → grain claims, in the order a failure cascades. The last
two are the ones that pay for themselves: a declared dimension that does not
exist becomes a startup failure rather than a 500 on the first *slice by* click,
and the **grain claim** (`count(*)` vs `count(distinct grain_key)`) catches a
relation that is not one row per grain — silent fan-out that multiplies every
aggregate over it, which neither MetricFlow nor Cube checks.

### Snapshots: fetch once, refit forever

For non-mock providers, every fetched series is cached as a parquet snapshot keyed on `(metric, grain, kind, window)` — by default in `.breakdown/snapshots/` next to the tree. Later startups with the same window read from disk instead of the warehouse, which makes restarts fast, keeps re-runs reproducible (commit the snapshots next to the tree and an RCA re-runs from a fresh clone), and lets the server boot even when the warehouse is unreachable, as long as every metric has a snapshot.

```bash
uv run breakdown serve --tree my_tree.yml --refresh        # refetch everything, overwrite snapshots
uv run breakdown serve --tree my_tree.yml --no-snapshots   # always hit the provider
uv run breakdown serve --tree my_tree.yml --snapshot-dir /somewhere/writable
```

A snapshot freezes what the provider returned at fetch time — if the warehouse backfills late-arriving data, run `--refresh` once to pick it up. `BREAKDOWN_REFRESH=1` is the environment-variable form, for a scheduled refresh that has no command line to edit. In Docker, `compose.yaml` mounts `./snapshots` and sets `BREAKDOWN_SNAPSHOT_DIR` (the default tree-adjacent location is unwritable there because `/config` is read-only); an unwritable snapshot directory is never fatal — the server logs one warning and runs uncached.

**If your metric restates, the snapshot key cannot tell.** The key is
`(metric, grain, kind, window)` with no content hash, so a series whose *past*
values change — payment plans settling backwards, late-arriving conversions,
any bitemporal source — is frozen at whatever it said the first time. Two rules
follow. Refresh unconditionally on a schedule (`BREAKDOWN_REFRESH=1`) rather
than relying on the cache to notice. And prefer a basis that never restates for
the series you actually fit: an order-*created* date including every status
moves forward only, where a settled or completed basis rewrites history behind
you. Keep the restating version in the tree if you report on it, but do not make
it an RCA target mid-period — the model would be training on values that are
still changing.

---

## Driving the UI

Start the server and open `http://localhost:9090/ui`. Breakdown fetches every metric's series from the provider at startup, so the first load takes a few seconds. The steps below use the bundled default `breakdown/examples/jaffle_shop_tree.yml` and its `2024-01-01`–`2024-04-09` window; substitute your own target and dates. The header date pickers are bounded to the loaded `--start-date`/`--end-date` window.

**1. Inspect a metric — and fit its model.** Click any node in the graph to open the **Metric** tab (right sidebar) with its time series. Nodes that have a probabilistic parent (e.g. `order_count`) show an **Analyze** section: pick **ADVI (fast)** or **NUTS (accurate)** and click **Run** to fit the BSTS. The posterior — trend, seasonality, and the `beta` / `beta_raw` coefficient on each parent — fills in, and the node picks up the "fitted" tint. Leaf and formula nodes just show their series.

**2. Run a root-cause analysis.** Choose a **Target** in the header bar, then open the **Root cause** tab and pick the **Analysis** window — the period you want explained — from a preset (Last 7 days, Last 14 days, Last full week) or the date pair. The **Reference** fills itself with the matched adjacent block (marked **auto**) and stays editable: touch it and it becomes custom; the auto chip restores it. The model doesn't train on the reference — it trains on *all* loaded history before the analysis window (each node's result says exactly what it was fitted on) — so the reference is only the baseline the gap is measured against. Click **Run RCA**: breakdown auto-fits any upstream probabilistic models it needs and paints the result on the graph — nodes tinted by direction of change, edges weighted by each parent's share of the explained gap, and a ranked cause list with credible intervals. **Copy link** yields a shareable `#rca=…` URL carrying the resolved windows; **Clear** resets.

**3. Localize a cause with a slice (optional).** Any ranked cause whose metric declares [`dimensions`](#dimensions-slicing) shows a **slice by** row — click a dimension to attribute that metric's gap across its values. Slices are ranked by excess concentration (how much more of the gap a value carries than its baseline share predicts), with the leader highlighted, `noise` badges where the bootstrap can't separate concentration from zero, and an explicit "not localized" verdict when nothing stands out. Across a lagged edge the slice automatically uses the parent's lag-shifted windows, so it compares the periods the contribution was actually measured over.

**4. Simulate a what-if (optional).** Open the **What-if** tab, click nodes to adjust them (interventions), optionally add assumption links for effects the tree doesn't encode, and click **Run simulation** for a steady-state projection with credible intervals rendered on the graph and in the sidebar.

RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`), so any analysis can be shared or bookmarked as a URL.

---

## YAML reference

### `provider`

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
| `local` | Queries a dbt project on disk via the MetricFlow CLI (`mf query`). Requires `project_path`. **Superseded by `dbt` for most trees** — see below. |
| `cloud` | Queries the dbt Semantic Layer API via the `dbt-sl-sdk`. Requires `environment_id`, `host`, and `token`. |
| `dbt` | Reads your dbt project's own `target/semantic_manifest.json` (written by plain `dbt parse` on **dbt Core**) and generates the SQL for each metric, running it over the connection in the project's `profiles.yml`. **No dbt Cloud, no Semantic Layer credential, no service token, and no new credentials of any kind.** Requires `project_path`. A node may override what dbt declares with its own `bind:` block. Executes on **BigQuery, Databricks, DuckDB, Postgres or Snowflake** — whichever your project's own target already uses. |
| `warehouse` | Runs each metric's own `sql` directly against a warehouse (currently Databricks SQL). Use when the semantic layer isn't queryable — the analyst mirrors governed definitions in SQL. Requires `http_path` plus **one of**: a PAT `token` (with `host`), or a Databricks CLI OAuth `profile` created by `databricks auth login --profile <name>` (host is read from the profile). |
| `none` | No data is ever fetched — a **cold-start tree** of declared beliefs (`assumed` is an accepted alias). Only what-if simulation is available; every non-formula node needs a `baseline` and every probabilistic edge an explicit prior. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |

**Distinct counts and slicing.** A `count_distinct` metric's slices overstate
it whenever one entity holds several values of a dimension inside a period — a
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
the data is already single-valued — which `breakdown doctor` then verifies.
Without it the slices are reported as overlapping, the overlap is quantified,
and contribution shares are withheld rather than computed against a total the
slices do not sum to.

**Bind entity flows to a state table, not an event table.** With `entity_grain`
declared, a slice panel also reports *movement between windows* — how many
entities are new, churned, retained, or **migrated** from one slice to another.
That is what tells you a platform switch (`−1` on iOS, `+1` on web, total
unchanged) is one user moving rather than two offsetting causes.

Those labels assume the relation has **one row per entity per period** — a daily
state table. On an *event* table, where a row means "something changed", an
entity only appears in windows where it changed, so `new` means *its first event
in this window*, not a new entity. The counts are still arithmetically correct
and migration still nets to zero, but they answer a different question than
their names suggest.

breakdown cannot tell the two apart from the schema, so it reports
`retention_share` — the fraction of reference-window entities that reappear —
and raises a caveat below 5%, which is the signature of an event table. Treat
that caveat as a prompt to check what the relation records, not as a verdict on
your data. If you want membership semantics, bind to a relation with one row
per entity per period.

**Moving from `local` to `dbt`.** Both read a dbt project on disk with no dbt
Cloud, but `local` shells out to `mf query` once per metric *and once per
slice*, behind a 120-second timeout, and needs the `mf` binary — which is why
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
for your adapter — the same one your dbt adapter already depends on, which is
why it is not bundled: `bigquery` (`metric-breakdown[bigquery]`), `databricks`
(`metric-breakdown[databricks]`), `duckdb`, `psycopg2-binary` for Postgres, or
`snowflake-connector-python`. On BigQuery the profile's `method` is honoured —
`oauth` (Application Default Credentials), `service-account`, and
`service-account-json`.

**It is not a drop-in for every tree.** `local` hands a metric name to
MetricFlow, which plans the SQL, so it serves things the `dbt` provider refuses
rather than approximates: cumulative metrics, derived metrics that offset an
input in time, aggregations with no additive decomposition (`min`, `max`,
`median`, `percentile`), conversion metrics, and `non_additive_dimension`. On
two real dbt projects that was 2 of 24 and 8 of 86 metrics.

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

**Secrets in config.** Any provider string field may reference an environment variable with `${VAR}` syntax (e.g. `token: ${DATABRICKS_TOKEN}`), so a tree can be committed without embedding credentials. A referenced variable that isn't set raises a clear error at load time. The `warehouse` provider's `profile` avoids secrets entirely — credentials come from the Databricks CLI's OAuth token cache, so nothing sensitive lives in the tree or the environment.

### `metrics`

Each metric entry supports the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier used throughout the tree |
| `source` | string | dbt Semantic Layer metric path (e.g., `jaffle_shop.metrics.revenue`) |
| `grain` | string | The metric's natural grain: `day` (default), `week`, or `month`. It is fetched, fitted, and attributed at this grain, never below it. See [Grains](#grains). |
| `kind` | string | Temporal aggregation kind: `flow` (default — sums over time), `stock` (point-in-time level — takes the last value), or `rate` (a ratio — can never be auto-aggregated). See [Grains](#grains). |
| `sql` | string | For the `warehouse` provider: a SQL query returning columns `date` and `value`, with `:start_date` / `:end_date` named parameters — one row per period at the metric's `grain`. Ignored by other providers. |
| `description` | string | Optional human-readable description |
| `parents` | list | Names of metrics that causally influence this one |
| `formula` | string | Arithmetic expression over parent names (e.g., `"order_count * average_order_value"`). Enables Shapley attribution. |
| `priors` | dict | Bayesian priors for the causal coefficients (see below) |
| `lags` | dict | Per-parent time lag in grain steps **at the node's grain** (days for a daily node, weeks for a weekly one). On a probabilistic node, regresses the child on each parent's value `N` steps earlier; combined with `formula`, declares a cohort-aligned lagged identity. See [Lagged regressors](#lagged-regressors). |
| `expected_signs` | dict | Per-parent declared coefficient direction (`positive` \| `negative`) on a probabilistic node. **Not a prior** — the fit is unconstrained, but a posterior that contradicts the declaration raises a `sign_warnings` diagnostic (surfaced in `/analyze`, `/metrics`, RCA responses, and the UI). |
| `dimensions` | dict | Declared slicing dimensions, `name: provider_dimension` shorthand (`region: customer__region`) or a mapping with `source`, `top_k`, `values`, `weight`. Enables `POST /rca/{name}/slices` and the MCP `slice_metric` tool — localizing a gap within the metric (which geo, plan, app version). Analysis-time only: never affects fetching at startup, fitting, or tree attribution. See [Dimensions (slicing)](#dimensions-slicing). |
| `seasonality` | list | Periodic components to include in the BSTS model. Periods are in grain steps at the node's grain. |
| `trend` | string or dict | Local-level (random-walk) trend. `trend: linear` uses the default step-size prior HalfNormal(0.05); `trend: {type: linear, sigma: 0.1}` widens it so the trend may absorb faster drift. Only `type: linear` is supported. |
| `baseline` | number or dict | **Cold-start mode only.** Asserted operating point for a tree with no data: `baseline: 1200` (point) or `baseline: {low: 800, high: 1600}` (central 90% interval of a Normal), in mean-per-period units at the node's grain. Rejected on formula nodes — theirs derive from parents so the identity holds. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |
| `plausible` | dict | **Cold-start mode only.** Declared honesty band `{min, max}` (either bound may be omitted, at least one required) standing in for historical min/max in the what-if extrapolation flags; `min: 0` recovers the "can't go negative" check. See [Cold-start mode](#cold-start-mode-what-if-with-no-data). |
| `format` | string or dict | UI display hint for the node card's big number — presentation only, no effect on modeling. See [Display format](#display-format). |
| `direction` | string | Which way is good news, for UI coloring only: `up_is_good` (default), `down_is_good` (costs, tickets, time-to-X), or `neutral` (gray, no judgment). Arrows stay directional; only the green/red coloring follows the declaration. Note: a stored-negative flow like churn MRR is `up_is_good` — moving toward zero means less churn. |

### Priors

Priors apply when the relationship with a parent is probabilistic (no formula). They are stated in **business units** — e.g., `mu: 0.1` below means "each additional session is worth ~0.1 orders". Internally the model fits on z-scored data, and breakdown translates the prior into normalized space automatically. The posterior reports both `beta` (normalized) and `beta_raw` (business units).

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

**Per-parent priors.** `coefficient` sets the default prior for every parent. To override a specific parent, add its name as a key alongside `coefficient` — the named prior wins for that parent, and the rest fall back to `coefficient` (or `Normal(0, 1)` if `coefficient` is absent):

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
  parents: [paid_cmau]
  expected_signs: { paid_cmau: positive }   # churn_mrr is stored negative: more actives should mean less-negative churn
```

Unlike a `HalfNormal` prior, this never constrains the fit. After fitting, the engine checks the `beta_raw` posterior: if less than 10% of its mass lies on the declared side, the fit carries a `sign_warnings` diagnostic naming the parent, the posterior probability, and the mean. A contradicted sign is usually not a bug in the fit — it means the edge as defined answers a different question than you meant. The classic case is **scale confounding**: regressing a dollar flow on a user count when both grow with the business — the learned sign reflects "bigger base → more of both," swamping the per-user effect you intended. The fix is to redefine the edge as **rates on rates** (e.g. churn *rate* on active *share*), not to constrain the sign.

### Seasonality

```yaml
seasonality:
  - period: 7      # in grain steps: 7 on a daily metric is weekly
    name: weekly
```

Each seasonality component is modeled with up to 2 Fourier harmonics (4 parameters: sin/cos × 2 harmonics). `period` is expressed in the node's own grain steps, so `period: 7` means weekly on a daily metric and is meaningless on a monthly one.

**Declare only seasonality your fit window can see.** Two constraints, both enforced:

- **Period vs. grain.** A harmonic needs more than two steps per cycle to be distinguishable from the level (Nyquist), so `period` must be ≥ 3, and the second harmonic is dropped below `period: 5`. Dropped harmonics are reported in the fit's `seasonality_warnings` diagnostic.
- **Period vs. data.** Identifying a component takes at least two full periods *inside the fit window* — and RCA fits stop at `analysis_start`, so the window is shorter than your data. A `period: 365` component on a few months of history is unidentifiable and will soak up degrees of freedom the parents need; it too lands in `seasonality_warnings`. RCA responses surface these warnings per node (with the fitted window under `fit_window`), so an unidentifiable component is flagged in the result, not just the server log — the fix is more history (an earlier `--start-date`), not a different reference window.

### Formula

Formulas express exact arithmetic relationships between a metric and its parents. The expression is a restricted Python arithmetic expression — only the operators `+`, `-`, `*`, `/`, `**` and named parent metrics are allowed. Function calls and attribute access are rejected at parse time.

```yaml
- name: net_revenue
  formula: "gross_revenue - cost_of_goods_sold"
  parents: [gross_revenue, cost_of_goods_sold]

- name: revenue
  formula: "order_count * average_order_value"
  parents: [order_count, average_order_value]

- name: conversion_rate
  formula: "order_count / daily_sessions"
  parents: [order_count, daily_sessions]
```

When a formula is defined, the BSTS model fits the **residual** (`y - formula(parents)`) rather than using parent regressors. This correctly captures the structural relationship and surfaces unexplained variance in the residual.

### Lagged regressors

Some causal effects show up with a delay — the README's motivating example is support tickets driving churn *weeks later*. A `lags` dict regresses the child on each parent's value `N` grain steps earlier, at the **child's** grain (days for a daily child, weeks for a weekly one):

```yaml
- name: churn_rate
  source: my.metrics.churn_rate
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

Shapley attribution and the residual fit both read each lagged parent from windows shifted back by its lag, so the identity — and its exact attribution — holds cohort-by-cohort.

### Grains

Metrics have different natural time grains: signups are daily events, a cohort conversion rate is only meaningful per week, MRR is a monthly snapshot. Forcing everything onto a daily spine manufactures fake sample size (a monthly value repeated 30 times is still one observation) and makes per-day ratios degenerate on low-volume days. Instead, each node declares its natural `grain` and is **fetched, fitted, and attributed at that grain, never below it**:

```yaml
- name: trial_starts            # daily flow (defaults: grain day, kind flow)
  source: my.metrics.trial_starts

- name: trial_conversion_rate   # weekly cohort rate
  source: my.metrics.trial_conversion_rate
  grain: week
  kind: rate

- name: conversions             # weekly identity over a daily flow and a weekly rate
  source: my.metrics.conversions
  grain: week
  formula: "trial_starts * trial_conversion_rate"
  parents: [trial_starts, trial_conversion_rate]
```

**Kinds determine aggregation.** Resampling a series upward is only well-defined once you know how it aggregates: `flow` metrics **sum** (orders, new MRR), `stock` metrics take the **last value** (total MRR, account balances), and `rate` metrics can never be auto-aggregated — the average of daily ratios is not the coarser ratio, so a rate must be *declared* at the grain it's consumed at, recomputed from its components.

`rate` covers more than the metrics whose names end in `_rate`. Averages (`average_order_value`), per-unit intensities (`emails_per_subscriber`) and durations (`time_to_first_response`, `page_speed`) are all ratios: the mean of a month of daily averages is not that month's average. Since `kind` defaults to `flow` and a missing declaration is indistinguishable from a deliberate one, the parser **warns** when a metric with a ratio-shaped name never declared a `kind` — it would otherwise be silently summed. It is a naming heuristic, so it never rejects the tree, and declaring `kind: flow` explicitly silences it for the cases where the name misleads.

**Mixed-grain rules** (enforced at parse time):
- A parent may never be **coarser** than its child — downward disaggregation is undefined.
- A **finer flow/stock** parent is automatically resampled up to the child's grain (sum / last). In the example above, `conversions` at week grain sees the *weekly sum* of `trial_starts`.
- A **finer rate** parent is an error — declare the rate at the child's grain.
- The finer grain must nest in the coarser: days tile weeks and months, but weeks straddle month boundaries, so a weekly parent under a monthly child is an error.

**Period labels are period starts** everywhere: days at midnight, weeks on Monday (ISO), months on the 1st. Partial edge periods are dropped, never zero-filled — a coarse metric's series may therefore end a few days before the raw data window does.

**Windows snap per node.** RCA windows stay day-resolution dates in the API; each node interprets them as the whole periods fully inside. A node whose window holds no whole period reports `"status": "window_shorter_than_grain"` instead of failing the RCA, and every node reports its `grain` and `effective_windows`. Windows that snap to a single period suppress the bootstrap CI (`ci_status: "degenerate_single_period"`) rather than reporting a falsely-precise interval.

**Warehouse SQL contract per grain.** The SQL owns the aggregation: return one row per period at the declared grain, labeled by period start. **Interior** gaps are filled by kind — flow → 0, stock → forward-fill (a gap before the first period is an error), rate → any missing period is an error. **Trailing** gaps are trimmed, not filled: periods after the last row the SQL returned are treated as not-yet-loaded, so a lagging mart ends the series early instead of manufacturing zeros at the tail. (A query returning no rows at all keeps the full zero spine for flows — an all-quiet window is legitimate.)

**Rates over true-zero periods.** A seasonal business has stretches where the
denominator is genuinely zero — nothing on sale, no sessions, no sends — and a
rate is undefined there rather than low. Because a missing rate period is an
error and a rate can never be invented, do not fetch such a metric as a rate.
Declare the numerator and denominator as their own `flow` nodes, which fill to
zero honestly, and make the rate a `formula` node over them:

```yaml
- name: orders            # flow: 0 in the dark window is a fact
  source: my.metrics.orders
- name: sessions          # flow
  source: my.metrics.sessions
- name: conversion_rate   # derived, not fetched
  kind: rate
  formula: "orders / sessions"
  parents: [orders, sessions]
```

This also buys exact Shapley attribution on the rate, and it keeps the dark
window visible as what it was — no traffic — instead of an error or an invented
number. Coarsening the grain until every period has a denominator is the other
option, and the worse one: it throws away resolution everywhere to fix a
problem that exists in a few windows.

**Data freshness.** Each metric's true data edge is tracked as it is fetched and exposed as `data_through` in `GET /meta` — the inclusive last date its last observed period covers. When sources disagree (one mart lags the others), the UI anchors every card's headline number, delta, and sparkline at the tree-wide edge via the **As of** selector (toolbar), which defaults to the oldest `data_through` across metrics and counts only periods *fully completed* by that date — so a calendar week the data edge cuts in half never becomes a headline number. The one case this cannot catch is a partially loaded most-recent period (the mart wrote *some* rows for it): detecting that needs load-completeness metadata on the mart side.

**Data-length guidance.** Fits need at least 10 whole periods at the node's grain — coarser grains need proportionally longer windows (a monthly node wants roughly a year of history). Seasonality periods and lags are in grain steps: `period: 7` means weekly on a daily node and seven *months* on a monthly one (the parser warns about that).

### Dimensions (slicing)

Tree RCA says *which upstream metric* moved; slicing says *where inside it* —
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
  kind: rate
  formula: "conversions / trial_starts"
  parents: [conversions, trial_starts]
  dimensions:
    region: customer__region            # rate: weight defaults to the formula denominator
```

For the semantic-layer providers, `source` is the MetricFlow dimension
identifier (added to the query's `group_by`); the mock provider synthesizes
deterministic slices for any source; the warehouse provider does not support
slicing yet. A `rate` metric needs a `weight` — the tree metric whose sliced
shares blend the per-slice rates — which defaults from a simple `num / den`
formula's denominator and otherwise must be declared:
`region: {source: customer__region, weight: trial_starts}`.

Slicing runs **at analysis time**: sliced series are fetched on demand for the
requested windows only, and never enter the startup data, the fits, or tree
attribution. Attribution is exact — a flow/stock decomposes as a sum over
slices; a rate splits per slice into `within` (its own rate moved) and `mix`
(traffic shifted between slices) — and slices are ranked by **excess
concentration** (`excess`): how much more of the gap a slice carries than its
baseline share predicts, with bootstrap credible intervals. Slices that don't
sum back to the metric are reported in a `reconciliation` block, never
silently rescaled. See `knowledge/dimensional_slicing_design.md` for the full
design.

### Display format

`format` controls how a metric's **big number** is displayed on its node card in the UI. It is presentation only — it never affects modeling, attribution, or the API's numeric values. Use the string shorthand for the common case, or a mapping for finer control:

```yaml
- name: revenue
  format: currency          # shorthand for {style: currency}

- name: daily_sessions
  format:
    style: number           # currency | percent | number  (default number)
    unit: sessions          # small caption under the value; grows the card one line
    decimals: 0             # fixed fraction digits (default: automatic)
    compact: true           # k / M / B notation (default: auto — currency compacts large values)
    symbol: "$"             # currency symbol, when style is currency
```

Delta values (period-over-period change) always render as a percent; `format` applies to the big number only.

**Display defaults.** When a metric declares no `format`, the UI guesses one from naming conventions — names containing tokens like `mrr`, `arr`, `revenue`, `arpu`, `aov`, `usd`, `cost`, `spend` render as currency; `rate`, `pct`, `percent`, `share`, `ratio` render as percent; everything else as a plain number. This is presentation-only and an explicit `format` always wins — declare one whenever the guess would be wrong.

### Cold-start mode (what-if with no data)

A tree with **no data provider** can still run what-if scenarios — on declared beliefs alone. The what-if engine's propagation core consumes operating points, edge slopes, and assumption effects; in cold-start mode all three are stated rather than fitted, so a pre-revenue company can simulate its business before the first row of data exists. The output quantifies the consequences of your assumptions — honestly wide intervals, never evidence.

A cold-start tree declares beliefs everywhere:

- **`baseline` on every non-formula node** — the asserted operating point, as a point (`baseline: 1200`) or a central-90% interval (`baseline: {low: 800, high: 1600}`), in mean-per-period units at the node's grain. Formula nodes derive theirs per-draw from their parents so the arithmetic identity holds by construction — declaring one there is a parse error.
- **An explicit prior on every probabilistic edge** (parent-specific or shared `coefficient`). Priors are already stated in business units, and with nothing to fit the prior *is* the coefficient distribution — coefficient draws are sampled from it directly. The fitted-mode fallback `Normal(0, 1)` is meaningless without data to set the scale, so it is not allowed here.
- **`plausible: {min, max}`** (optional; either bound alone is fine) — the declared honesty band standing in for historical min/max: a simulated value outside it raises the same extrapolation warning fitted mode derives from history. `min: 0` recovers the "this can't go negative" check.

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

Propagation, do-operator semantics, draw alignment, and the Shapley source decomposition are identical to fitted mode. The response is labeled `mode: "cold_start"`, adds a per-node `baseline_ci_95` where the asserted baseline is a range, and carries cold-start caveats so the output can't be mistaken for estimates from data. When data arrives, the same YAML priors feed the fit — posteriors replace priors with zero config changes.

**Serving a cold-start tree.** Declare `provider: type: none` and `breakdown serve` boots without fetching anything — not a degraded start; the tree simply has no data. Startup validates the declarations and fails loudly with the full list of blockers if any are missing (`breakdown doctor` runs the same check). `GET /meta` reports `"mode": "cold_start"`; endpoints that consume history (`/series`, `/analyze`, `/shapley`, `/rca`) return 422 pointing at `POST /simulate`, which runs scenarios with **no baseline window** — operating points come from the tree, so a scenario passing `baseline_start`/`baseline_end` is rejected. The MCP `run_whatif` tool works the same way (omit the baseline dates).

**The UI boots what-if-first** on a cold-start tree: node cards show each metric's asserted operating point with its 90% belief range (formula nodes derive theirs), probabilistic edges are labeled with their stated priors (`β ~ 0.03 [0.01, 0.05] · belief`), the adjust panel's range strip renders from the declared `plausible` bounds, and results are labeled as consequences of beliefs — the Root cause tab is inert, since there is no history to explain. Try it with the bundled example:

```bash
uv run breakdown serve --tree breakdown/examples/cold_start_tree.yml
```

See [`docs/model.md`](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md) ("Reading cold-start output") before presenting results, and `knowledge/cold_start_design.md` for the full design.

**Graduating from cold start.** The tree you build pre-data *is* the tree you fit once data exists — the Bayesian promise is literal. When real numbers start flowing:

1. Swap the provider block (`type: none` → `local` / `cloud` / `warehouse`) and give each metric a real `source` (or `sql`). Nothing else in the tree changes.
2. Your `priors` carry over untouched — the same declarations that were sampled directly in cold start become the Bayesian priors of the BSTS fit, and the data updates them into posteriors. What-if flips from prior draws to posterior draws automatically; RCA becomes available.
3. `baseline` and `plausible` are ignored by fitted mode and stay in the YAML as a record of what you believed before the data arrived — worth keeping.

Two things to plan for. Each node needs at least **10 whole periods at its grain** before it can be fitted — a monthly tree waits most of a year for its first fit, so author cold-start trees at the finest grain you'll actually measure (weekly for most funnels; edge priors are per-parent-unit and carry over, but per-period `baseline` values would need rescaling). And check where you stand at any point with the doctor's **fit readiness** report:

```bash
uv run breakdown doctor --tree my_tree.yml --start-date 2026-01-01 --end-date 2026-08-01
```

It reports every metric's whole-period count against the fit minimum (`signups: 30/10 whole day periods` … `churn_rate: 4/10 — not fittable yet`), so you can watch the tree graduate metric by metric.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/meta` | Metric names, data window, provider type, mode (`fitted` \| `cold_start`), fitted models, per-metric `earliest_available` history discovery (UI bootstrap) |
| `GET` | `/dag` | Full metric DAG (nodes + edges) |
| `GET` | `/metrics/{name}` | Metric definition, time series, and posterior summary |
| `POST` | `/analyze/{name}` | Run Bayesian sampling for a metric |
| `GET` | `/shapley/{name}` | Shapley attribution for a formula metric |
| `POST` | `/rca/{name}` | Root cause analysis over the metric's ancestors |
| `POST` | `/simulate` | Do-operator what-if scenario (fitted posteriors, or declared beliefs on a cold-start tree) |
| `GET` | `/ui` | Interactive DAG visualization |

### `POST /analyze/{name}`

Query parameters:

| Param | Default | Description |
|-------|---------|-------------|
| `inference_method` | `nuts` | `nuts` (full MCMC) or `advi` (variational inference — faster, less accurate) |
| `draws` | `500` | Posterior samples to draw |
| `tune` | `500` | Tuning steps (NUTS only) |
| `chains` | `4` | Number of NUTS chains (NUTS only) |
| `fit_end` | none | Exclusive date cutoff (`YYYY-MM-DD`): fit only on rows before it. Defaults to the full window; pass the analysis-window start to reproduce what RCA fits. |

```bash
# Full MCMC (use for post-mortem analysis)
curl -X POST "http://localhost:9090/analyze/order_count?inference_method=nuts&draws=1000"

# Fast variational inference (use for live incident triage)
curl -X POST "http://localhost:9090/analyze/order_count?inference_method=advi"
```

### `GET /shapley/{name}`

Returns how much of the target metric's gap between two time windows is attributable to each parent. Requires a `formula` on the metric definition.

Query parameters:

| Param | Description |
|-------|-------------|
| `analysis_start` | Start of the analysis window (`YYYY-MM-DD`) |
| `analysis_end` | End of the analysis window (`YYYY-MM-DD`) |
| `reference_start` | *(optional)* Start of the baseline window (`YYYY-MM-DD`) |
| `reference_end` | *(optional)* End of the baseline window (`YYYY-MM-DD`) |

Omit **both** reference dates (passing exactly one is a 422) and the engine
defaults to the **matched adjacent block**: the window ending the day before
`analysis_start`, 4× the analysis length (min 28 days, whole weeks when
seasonality is in the target's scope), clamped to the loaded data. The
response echoes the resolved `reference_window`/`analysis_window` and sets
`reference_defaulted`. The reference is only the comparison baseline — the
model always fits on all loaded history before `analysis_start` — see
[docs/model.md](docs/model.md).

Example response:

```json
{
  "target": "revenue",
  "formula": "order_count * average_order_value",
  "grain": "day",
  "effective_windows": {
    "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
    "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
  },
  "baseline": 50000.0,
  "actual": 42000.0,
  "gap": -8000.0,
  "attribution": {
    "order_count": -6200.0,
    "average_order_value": -1800.0
  },
  "decomposition": {
    "order_count": {"means": -6100.0, "covariance_analysis": -80.0, "covariance_reference": 20.0},
    "average_order_value": {"means": -1700.0, "covariance_analysis": -80.0, "covariance_reference": 20.0}
  }
}
```

`baseline` and `actual` are each the **mean of the formula evaluated period by period** (at the target's grain) over the reference and analysis windows respectively (so both windows' within-window co-movement of the parents is included); `gap = actual − baseline`. Each `attribution` value is the sum of three exact Shapley games, reported per parent in `decomposition`: `attribution = means + covariance_analysis − covariance_reference` (the window-means bridge plus the parent's share of each window's within-window co-movement term). The attributions are guaranteed to sum to `gap`. Windows are snapped to whole periods at the target's grain (`effective_windows`); a window with no whole period is a 422.

### `POST /rca/{name}`

Walks the ancestor DAG of `name` and attributes the change between a reference window and an analysis window to upstream metrics. Any probabilistic node in scope that hasn't been fit yet is fit on demand with ADVI and its trace is cached (a second call is much faster).

Query parameters (`YYYY-MM-DD`): `analysis_start`, `analysis_end` (required),
`reference_start`, `reference_end` (optional — omitting both uses the matched
adjacent block, exactly as on `GET /shapley/{name}` above; the response carries
`reference_defaulted`).

```bash
# explicit reference
curl -X POST "http://localhost:9090/rca/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
# defaulted reference
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-02-16&analysis_end=2024-04-09"
```

Trimmed response:

```json
{
  "target": "revenue",
  "reference_window": {"start": "2024-01-01", "end": "2024-02-15"},
  "analysis_window": {"start": "2024-02-16", "end": "2024-04-09"},
  "nodes": {
    "revenue": {
      "status": "ok", "grain": "day",
      "effective_windows": {
        "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
        "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
      },
      "baseline": 25000.0, "actual": 27000.0, "gap": 2000.0, "relative_change": 0.08,
      "attribution_method": "shapley",
      "ci_status": "ok",
      "unexplained": 12.0,
      "components": null,
      "contributions": [
        {"parent": "order_count", "estimate": 1600.0, "share_of_gap": 0.8,
         "ci_95": [1450.0, 1740.0], "prob_same_direction": 1.0},
        {"parent": "average_order_value", "estimate": 388.0, "share_of_gap": 0.19,
         "ci_95": [210.0, 560.0], "prob_same_direction": 0.99}
      ]
    },
    "order_count": {
      "status": "ok", "grain": "day",
      "effective_windows": {
        "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
        "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
      },
      "baseline": 500.0, "actual": 540.0, "gap": 40.0, "relative_change": 0.08,
      "attribution_method": "posterior",
      "ci_status": "ok",
      "unexplained": 1.4,
      "components": {
        "trend": {"estimate": 0.5, "ci_95": [-1.1, 2.2]},
        "seasonal": {"estimate": 0.1, "ci_95": [-0.6, 0.8]}
      },
      "contributions": [
        {"parent": "daily_sessions", "estimate": 38.0, "share_of_gap": 0.95,
         "ci_95": [30.0, 46.0], "prob_same_direction": 0.99}
      ]
    }
  },
  "ranked_causes": [
    {"metric": "order_count", "score": 0.8, "via": "revenue"},
    {"metric": "daily_sessions", "score": 0.76, "via": "order_count"}
  ]
}
```

Per-node fields added by grain support: `grain` (the grain the node was analyzed at), `effective_windows` (the whole periods the requested windows snapped to at that grain), `status` (`"ok"`, or `"window_shorter_than_grain"` when the windows contain no whole period — the node is reported without attribution instead of failing the RCA), and `ci_status` (`"ok"`, `"degenerate_single_period"` for formula nodes whose window snapped to one period — bootstrap CIs are withheld — or `"posterior_only_single_period"` for posterior nodes, whose coefficient uncertainty remains but whose window-sampling component is absent). Gaps are mean-per-period at each node's own grain, so compare nodes via `share_of_gap` and `ranked_causes` scores, not raw gaps, in mixed-grain trees.

**Two-level attribution (formula nodes).** Each formula-node contribution also carries a `decomposition` — `{"means": {estimate, ci_95}, "comovement": {estimate, ci_95}}` with `means + comovement = estimate` exactly per bootstrap replicate — and the node carries an `interaction` summary (the summed co-movement shift across parents, with its own CI). The UI's default **Headline** view is the classic price/volume/mix bridge built from these: one row per parent showing its means-bridge contribution, plus one explicit *co-movement shift* row, plus unexplained — rows total to the gap. The **Detailed** toggle expands each parent to its full split. The interaction is shown as its own labeled row rather than silently folded into the factors; for products it is exactly the parents' covariance delta, for other formulas the full within-window co-movement/Jensen shift.

### Root cause analysis

`POST /rca/{name}` combines the two attribution methods across a metric tree:

- **Formula nodes** get `attribution_method: "shapley"` — exact symmetric per-day Shapley values (a window-means bridge plus each parent's share of the within-window co-movement term of each window, analysis added and reference subtracted), so shifts in the parents' within-window co-movement are attributed to parents. `unexplained` is only the target's own measurement noise around the formula — for an exact identity it is zero.
- **Probabilistic nodes** get `attribution_method: "posterior"` — each contribution is the posterior over the parent's raw-scale coefficient (`beta_raw`) times the parent's window-over-window change. Lagged parents are compared over windows shifted back by the lag, and each lagged contribution reports `lag` and `parent_windows` — the parent's own shifted `{reference, analysis}` windows — so you can see (and reuse, e.g. for `POST /rca/{parent}/slices`) exactly which parent periods were examined. These nodes also report a `components` block: the fitted model's own trend and seasonal terms as window-over-window deltas with CIs, so they no longer hide inside `unexplained`.

Every contribution is reported as an `estimate` (mean), a 95% interval (`ci_95`), and `prob_same_direction` (mass on the dominant side of zero). The intervals combine coefficient uncertainty (probabilistic nodes) with **window-sampling uncertainty** — the window means themselves are resampled with a circular moving-block bootstrap (≤7-day blocks, jointly across metrics, seeded so responses are deterministic). This is what keeps a 3-day analysis window honest: its CIs are visibly wider than a 4-week window's.

Unfitted probabilistic nodes in scope are fit with ADVI on demand — on data strictly before the analysis window — and cached, so the endpoint works without a prior `/analyze` call. `ranked_causes` is a documented heuristic that propagates an influence score from the target up the ancestor tree (weighting each hop by the parent's clamped share of its child's gap); use it as a triage ordering.

See [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md) for how to read `components`, `unexplained`, and the bootstrap's assumptions.

### `POST /rca/{name}/slices`

The traverse-then-slice follow-up: attribute one metric's window-over-window gap across a declared dimension's values. The reference dates are optional here too (same defaulting rule; the response carries `reference_defaulted`) — but when slicing a **lagged parent** surfaced by an RCA, pass its `parent_windows` explicitly: the default matches the metric's own timeline, not a lag-shifted one.

```bash
curl -X POST "http://localhost:9090/rca/signups/slices?dimension=region&reference_start=2024-02-05&reference_end=2024-03-03&analysis_start=2024-03-04&analysis_end=2024-03-10"
```

```json
{
  "metric": "signups", "dimension": "region", "grain": "day", "kind": "flow",
  "effective_windows": {
    "reference": {"start": "2024-02-05", "end": "2024-03-03", "n_periods": 28},
    "analysis": {"start": "2024-03-04", "end": "2024-03-10", "n_periods": 7}
  },
  "baseline": 1240.0, "actual": 1130.0, "gap": -110.0,
  "attribution_method": "slice_sum",
  "slices": [
    {"value": "emea", "baseline": 273.0, "actual": 178.0,
     "contribution": -95.0, "share_of_gap": 0.86, "baseline_share": 0.22,
     "excess": -70.8, "ci_95": [-84.1, -57.9], "prob_concentrated": 0.99,
     "noise_level": false},
    {"value": "__other__", "n_values": 2, "contribution": -9.0, "...": "..."}
  ],
  "reconciliation": {"mean_residual": 0.0, "max_abs_residual": 0.0,
                     "residual_share_of_baseline": 0.0, "status": "ok"},
  "ci_status": "ok", "caveats": []
}
```

- `contribution` is the slice's own window-mean change; contributions sum exactly to the sliced gap (flows/stocks are sum identities over slices).
- `excess = contribution − baseline_share × gap` is the **localization signal**: how much more of the gap the slice carries than its size predicts. Excesses sum to zero — concentration is a reallocation of the gap. `prob_concentrated` is the bootstrap probability the excess direction is real; `noise_level: true` rows should not be narrated as localized.
- Rate metrics return `attribution_method: "slice_blend"`: each slice splits into `within` (its own rate moved) and `mix` (traffic shifted between slices), summing exactly to the blended gap, with the total composition effect in `mix_total`.
- `reconciliation` compares the slices' sum (or weighted blend) against the metric's own series; `"discrepant"` means the dimension doesn't cleanly partition the metric — attributions are then approximate, and say so.
- When slicing a **lagged parent** surfaced by an RCA, pass the parent's lag-shifted windows — its RCA contribution carries them as `parent_windows`; those are the periods that influenced the child.

Sliced series are fetched from the provider on demand for just these windows and cached per (metric, dimension, window); nothing about slicing touches the startup data or the fits.

---

## MCP server (AI assistants)

The server exposes the engine to AI assistants over [MCP](https://modelcontextprotocol.io) at `http://127.0.0.1:9090/mcp` (streamable HTTP; started automatically by `serve`). A chat assistant connected to it can answer "why was revenue down last week?" by running a real RCA — Shapley attributions, credible intervals, the honest `unexplained` remainder — instead of guessing, and "what if we raise marketing spend 10%?" with a posterior from the what-if engine.

Five tools:

| Tool | Backed by | Description |
|------|-----------|-------------|
| `get_tree` | `/meta` + `/dag` | Metric DAG, grains, kinds, declared dimensions, and the loaded data window — assistants call this first |
| `explain_metric` | `/metrics/{name}` | One metric's definition, neighbors, recent series, and fit status |
| `run_rca` | `/rca/{name}` | Full root-cause analysis between two windows |
| `slice_metric` | `/rca/{name}/slices` | Localize a metric's gap within a declared dimension (geo, plan, app version) — the traverse-then-slice follow-up to `run_rca` |
| `run_whatif` | `/simulate` | Do-operator what-if scenario with posterior deltas |

Analysis responses are compacted for token economy (rounded floats, decompositions dropped) and carry two extra fields: `how_to_read` — the interpretation rules from [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md) (what `unexplained` means, why `share_of_gap` can exceed 100%, ADVI vs NUTS), so the narrating model states caveats instead of flattening them — and `report_url`, a deep link that replays the exact analysis in the UI (the engine is seeded, so the link reproduces the numbers).

Connect from Claude Code:

```bash
claude mcp add --transport http breakdown http://127.0.0.1:9090/mcp
```

or from Claude Desktop via `claude_desktop_config.json` (stdio bridge):

```json
{
  "mcpServers": {
    "breakdown": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:9090/mcp"]
    }
  }
}
```

### What it looks like

A real, unedited exchange against the bundled demo tree (Claude with the MCP server connected):

**Prompt:** *"Why was revenue down the last two weeks?"*

> Revenue was down because average order value dropped, not because of traffic or order volume — those actually improved and partially masked a bigger AOV problem.
>
> Comparing the last two weeks of data (Mar 27 – Apr 9, 2024) to the two weeks before:
>
> - Revenue fell ~1.4%, from about $735.7K to $725.2K per day (a gap of roughly –$10.4K/day).
> - Average order value is the culprit: it dropped ~4.9% ($5,132 → $4,879), contributing about –$36K/day to revenue. The model is essentially certain about this (95% credible interval of –$50.9K to –$20.5K, with the direction certain).
> - Order count rose ~3.8% (142.7 → 148 orders/day), contributing about +$27K/day — offsetting most of the AOV damage. Without that lift, the revenue decline would have been ~3.5× larger.
> - Sessions were up ~2.2% and explain about half of the order-count gain; the rest is a modest underlying trend.
>
> The unexplained remainder is small (~$583/day), so the AOV-vs-volume story accounts for nearly the whole move. Note this tree can't say why AOV fell — that's outside the modeled drivers — so the next step would be checking things like discounting/promos, product mix, or a shift toward cheaper SKUs in that window.
>
> One caveat on the question: the loaded data only covers Jan 1 – Apr 9, 2024, so "last two weeks" here means the most recent two weeks of available data, not the end of the year.
>
> Interactive report: `http://127.0.0.1:9090/ui/#rca=revenue&reference_start=2024-03-13&reference_end=2024-03-26&analysis_start=2024-03-27&analysis_end=2024-04-09`

Every claim in that answer traces back to a field in the tool response: the offsetting order-count lift is the unclamped `share_of_gap` design at work, "essentially certain" is `ci_95` + `prob_same_direction` read as confidence language, the small `unexplained` is cited as evidence the story is complete, and "this tree can't say why AOV fell" is the DAG-is-a-hypothesis caveat from `how_to_read` — applied where it matters instead of flattened into a disclaimer. The closing link replays the exact analysis in the UI.

Notes: the first `run_rca`/`run_whatif` on a tree fits models on demand (ADVI) and can take a minute; fits are cached and shared with the UI. The cache resets when `--reload` restarts the process. Set `BREAKDOWN_PUBLIC_URL` if the server is reached at anything other than `http://127.0.0.1:<port>` so `report_url` links resolve.

---

## Inference methods

### NUTS (default)

No-U-Turn Sampler via PyMC. Produces exact posterior samples with convergence diagnostics (R-hat, effective sample size). Use for:

- Post-mortem root cause analysis
- Building confidence in a new metric relationship
- Any situation where accuracy matters more than speed

### ADVI

Automatic Differentiation Variational Inference. Fits a parametric approximation to the posterior rather than sampling it. Typically 5–10× faster than NUTS. Use for:

- Live incident triage where speed matters
- Early exploration of a new metric tree

---

## Shapley attribution

For metrics connected by a formula, Breakdown computes exact Shapley values to attribute a period-over-period gap to each parent.

**Why Shapley?** Simpler decompositions (e.g., holding one factor fixed while varying the other) produce different answers depending on the order of decomposition. Shapley values are the unique attribution method that is simultaneously: efficient (values sum to the gap), symmetric (order doesn't matter), and null (a parent that didn't move gets zero credit).

**How it works:** Each parent's attribution is the sum of **three exact Shapley games**, all computed by full coalition enumeration (2ⁿ coalitions, vectorized across days):

1. **The window-means bridge** — one game from the parents' reference-window means to their analysis-window means.
2. **The analysis window's co-movement share** — one game per analysis-window day, non-members held at the *analysis* means; averaged over the window it is the parent's share of `mean_an(formula daily) − formula(analysis means)`.
3. **The reference window's co-movement share** — the same inside the reference window, *subtracted*.

The parts telescope, so attributions sum exactly to `mean(formula daily over analysis) − mean(formula daily over reference)` — the formula's own gap. For a 2-parent multiplicative formula `A × B` this reduces to the closed form:

```
φ(A) = Δmean(A) × (mean_ref(B) + mean_an(B)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
φ(B) = Δmean(B) × (mean_ref(A) + mean_an(A)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
```

**Why per-day, in both windows?** For any nonlinear formula, `mean(A × B)` differs from `mean(A) × mean(B)` by the within-window covariance of A and B. Attributing on window means would silently drop that term — a real behavioral change like "the large orders disappeared" (an orders–AOV covariance shift) would be reported as noise. Treating **both** windows per-day means the covariance *delta* is handed to the parents where it belongs, while a covariance that exists but didn't change contributes nothing — and `unexplained` stays exactly zero for an exact identity instead of absorbing the reference window's covariance.

---

## Project structure

```
AGENTS.md            # Orientation for contributors (human or AI) — start here to build
breakdown/
  parser.py          # YAML → Pydantic models → NetworkX DAG
  formula.py         # Shared formula validation / safe evaluation
  data_fetch.py      # BaseDataFetcher + Mock / Local / Cloud / Warehouse implementations
  engine/
    model.py         # fit_metric() — BSTS via PyMC; compute_shapley()
    rca.py           # run_rca() + shapley_attribution() — root cause analysis
    slices.py        # slice_attribution() — dimensional slicing of a metric's gap
  api/
    main.py          # FastAPI app
  mcp/
    server.py        # MCP tools for AI assistants (get_tree, explain_metric, run_rca, slice_metric, run_whatif)
    shaping.py       # MCP response compaction + how_to_read caveats + UI deep links
  cli.py             # `breakdown serve` / `breakdown doctor` console entry point
  doctor.py          # Provider connectivity checks with copy-paste remediation
  static/
    index.html       # UI: Cytoscape DAG + RCA workflow (app.js, style.css)
  examples/
    jaffle_shop_tree.yml   # The bundled default (mock) tree
docs/
  model.md           # Model assumptions & how to interpret results — start here
  ai-context/        # Architecture deep-dives (backend, frontend) for contributors
knowledge/           # Product & design specs, roadmap, reference trees
tests/
Dockerfile           # Container image (see "Deploying")
compose.yaml
```

**If you're going to interpret breakdown's output, read [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md)** — it explains what the model assumes, what `unexplained` means, why shares can exceed 100%, and when to trust (or distrust) a credible interval. **If you want the statistics in depth**, the [statistics white paper](https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/statistics_whitepaper.md) covers every model in the engine, why it was chosen, where it breaks, and how rigorous the whole thing actually is today. **If you're going to work on the codebase, read [AGENTS.md](https://github.com/PolycultureResearch/breakdown/blob/main/AGENTS.md)** — the project's invariants and where everything lives.

---

## Tech stack

| Component | Library |
|-----------|---------|
| Bayesian inference | [PyMC](https://www.pymc.io/) 5.x |
| Posterior analysis | [ArviZ](https://python.arviz.org/) |
| Graph modeling | [NetworkX](https://networkx.org/) |
| dbt Semantic Layer | [dbt-sl-sdk](https://github.com/dbt-labs/semantic-layer-sdk-python) + [dbt-metricflow](https://github.com/dbt-labs/metricflow) |
| API | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| AI assistants | [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (streamable HTTP at `/mcp`) |
| Visualization | [Cytoscape.js](https://js.cytoscape.org/) |
| Config / validation | [Pydantic](https://docs.pydantic.dev/) v2 |

---

## References

- Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015). [Inferring causal impact using Bayesian structural time-series models](https://projecteuclid.org/journalArticle/Download?urlId=10.1214%2F14-AOAS788). *The Annals of Applied Statistics*, 9(1), 247–274.
- Štrumbelj, E., & Kononenko, I. (2014). [Explaining prediction models and individual predictions with feature contributions](https://link.springer.com/article/10.1007/s10115-013-0679-x). *Knowledge and Information Systems*, 41(3), 647–665.
- Levchuk, P. (2025). [The Metric Tree Trap: How math obscures more than it reveals](https://medium.com/@paul.levchuk/the-metric-tree-trap-4280405fd35e). Medium.

## Use case: Solving for "what happened over the weekend?" 
You get a slack message late Sunday evening from the CFO. "I hate to do this again, but can you meet later? Conversions are way down over the weekend. I have to come into Monday's meeting with some idea of what happened." 

You log into the Snowflake terminal or whatever, and open the Zoom call with the CFO. You write a query to confirm that indeed, conversions are way down starting on Friday. What then ensues is a rapid series of ad-hoc hypotheses and checks; the CFO posing questions (maybe it's just the latest iOS update? is it isolated to users in the United States? Is it caused by a decrease in trial starts or a decrease in the rate of trial to paid conversions?), you sweating and writing SQL into the terminal. Hours later, through trial and error, you can define the scope of the problem: what kinds of users, devices, geographical regions, software versions, etc. seem to be behind the observed change. And you have some idea about where in the user experience the problem may reside: was it the numerator or denominator of the rate that changed? Was there less traffic overall or were the people who visited less likely to convert?

It's been my least favorite part of my job at several companies. But there's something to learn from this kind of late-night triage. Essentially, you and the CFO are using your combined knowledge of the business to generate hypotheses, and checking them. You are traversing the causal graph in your heads to look for anomalies upstream of the observed change that might explain it. You're essentially doing three things:
1. Constructing the causal graph upstream of the metric where you observed the abnormal change.  What could have caused it? What do we measure about what could have caused it? And do we observe significant changes in those upstream metrics?
2. Slicing the metrics into smaller and smaller slices to locate the anomaly. That's the process of grouping by operating system, geographical region, user type, software version, etc. to try to understand if one or more group of users was driving the overall trend. You might slice up the metric of concern itself (the conversion rate, in this example), or the upstream metrics (the number of trials, the number of conversions, etc.).
3. Traversing the causal graph to see if something that could feasibly have caused the change also changed in the same timeframe. Upstream of conversion rate lies the number of users who could potentially convert, and the number who did convert. Upstream of that are the number of trial starts, and upstream of that are the number of web visitors, and upstream of that are the reach of marketing campaigns, etc. There are also metrics that are known or expected to influence conversion, like the rate of adoption of key features during the trial period. We're moving up the causal chain to look for anomalies that might explain the one we observed.

This process is painful, and it's limited in its ability to produce insights, but it's not crazy. It probably leaves you ready to present something in the Monday morning meeting. Can we automate it? And can we improve on it?

The premise of breakdown is that by defining the causal graph explicitly, before there is an issue to investigate, we enable a less painful and more powerful process of root cause analysis. We call that causal graph a metrics tree. The nodes of the graph are metrics. The edges (or connections) are causal relationships, either simple deterministic ones (e.g., `new subscription purchases` / `trial ends` = `conversion rate`) or probabilistic ones (e.g., trying out our key features during the trial period increases the probability of converting at the end of the trial period). You define those metrics and the relationships between them with the stakeholders, much like you define metrics. You define them in YAML. When you visualize them, they look kind of like a tree.

Defining the metric tree *a priori* solves two big problems, both related to reducing the search space when you go looking for the root cause. Consider the two implicit strategies on your late-night call with the CFO: slicing the metrics and searching the upstream metrics. You probably combine those strategies, slicing the concerning metric many ways, then slicing all the upstream metrics several ways. Without a metric tree, you could try naively looking at all your metrics, and seeing what else changed last Friday, and slicing all of those, looking for changes that correlate with your concerning metric in time. Maybe you calculate a correlation coefficient between your concerning metric and every other metric and each of its possible slices. Problem one is that the more metrics you examine, the more spurious correlations you are likely to observe. You'll spend your time chasing correlations and then trying to assess causation. Problem two is that the combinatorial explosion of metrics and all their possible slices can start to be computationally intensive. It's not a time you want to be slow. And a third problem is that you may miss any complex, conditional relationships between metrics.

A metric tree dramatically reduces the search space when you slice up metrics to try to locate the anomaly, and it constrains the space to the metrics that could feasibly cause the observed change.

Both halves of that late-night process are now first-class in breakdown. The traversal is `POST /rca/{name}`: walk the ancestor tree, attribute the gap edge by edge — with lagged edges compared over the right earlier windows, so "trial starts one trial period ago" is exactly what gets examined. The slicing is `POST /rca/{name}/slices`: declare the dimensions worth slicing (`dimensions:` on the metric — region, plan tier, app version), and the gap at any node decomposes exactly across slices, ranked by how much more of the gap each slice carries than its size predicts. An AI assistant connected over MCP runs the whole loop — `run_rca`, then `slice_metric` on the top causes — before the Monday meeting.


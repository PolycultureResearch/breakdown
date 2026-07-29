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

**Requirements:** Python 3.11+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/your-org/breakdown
cd breakdown
uv sync
uv run python main.py serve
```

Open `http://localhost:9090/ui` to explore the metric tree. The UI shows the DAG (formula vs learned edges, fit status), per-metric time series and posteriors in business units, and a full point-and-click RCA workflow: pick a target and two windows, run it, and read the answer off the graph — nodes tinted by direction of change, edges weighted by share of the gap explained, ranked causes with credible intervals in the sidebar. RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`) so an analysis can be shared as a URL.

By default the server loads `examples/jaffle_shop_tree.yml` with mock data. Point it at your own tree and data window:

```bash
uv run python main.py serve --tree path/to/my_tree.yml --start-date 2025-01-01 --end-date 2025-06-30
```

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

## Driving the UI

Start the server and open `http://localhost:9090/ui`. Breakdown fetches every metric's series from the provider at startup, so the first load takes a few seconds. The steps below use the default `examples/jaffle_shop_tree.yml` and its `2024-01-01`–`2024-04-09` window; substitute your own target and dates. The header date pickers are bounded to the loaded `--start-date`/`--end-date` window.

**1. Inspect a metric — and fit its model.** Click any node in the graph to open the **Metric** tab (right sidebar) with its time series. Nodes that have a probabilistic parent (e.g. `order_count`) show an **Analyze** section: pick **ADVI (fast)** or **NUTS (accurate)** and click **Run** to fit the BSTS. The posterior — trend, seasonality, and the `beta` / `beta_raw` coefficient on each parent — fills in, and the node picks up the "fitted" tint. Leaf and formula nodes just show their series.

**2. Run a root-cause analysis.** In the header bar: choose a **Target** (must be a metric with a `formula`, e.g. `revenue`), set the **Reference** and **Analysis** date pairs (or pick a canned pair from the **Windows** preset), then click **Run RCA**. Breakdown auto-fits any upstream probabilistic models it needs (on data strictly before the analysis window) and paints the result on the graph: nodes tinted by direction of change, edges weighted by each parent's share of the explained gap, and a ranked cause list with credible intervals in the **Root cause** tab. **Copy link** yields a shareable `#rca=…` URL; **Clear** resets.

**3. Simulate a what-if (optional).** Open the **What-if** tab, click nodes to adjust them (interventions), optionally add assumption links for effects the tree doesn't encode, and click **Run simulation** for a steady-state projection with credible intervals rendered on the graph and in the sidebar.

RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`), so any analysis can be shared or bookmarked as a URL.

---

## YAML reference

### `provider`

Controls how metric time-series data is fetched.

```yaml
provider:
  type: mock           # mock | local | cloud | warehouse
  project_path: "..."  # required for type: local
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
| `local` | Queries a dbt project on disk via the MetricFlow CLI (`mf query`). Requires `project_path`. |
| `cloud` | Queries the dbt Semantic Layer API via the `dbt-sl-sdk`. Requires `environment_id`, `host`, and `token`. |
| `warehouse` | Runs each metric's own `sql` directly against a warehouse (currently Databricks SQL). Use when the semantic layer isn't queryable — the analyst mirrors governed definitions in SQL. Requires `http_path` plus **one of**: a PAT `token` (with `host`), or a Databricks CLI OAuth `profile` created by `databricks auth login --profile <name>` (host is read from the profile). |

For `local` and `cloud`, the metric queried from the semantic layer is the last segment of `source` (e.g., `source: jaffle_shop.metrics.revenue` queries the metric `revenue`); the result is exposed in the tree under `name`. For `warehouse`, each metric carries its own `sql` (see the `metrics` table) and is keyed by `name`. The data window defaults to `2024-01-01`–`2024-04-09` and is set with `--start-date` / `--end-date` (or the `BREAKDOWN_START_DATE` / `BREAKDOWN_END_DATE` / `BREAKDOWN_TREE` environment variables).

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
| `seasonality` | list | Periodic components to include in the BSTS model. Periods are in grain steps at the node's grain. |
| `trend` | string or dict | Local-level (random-walk) trend. `trend: linear` uses the default step-size prior HalfNormal(0.05); `trend: {type: linear, sigma: 0.1}` widens it so the trend may absorb faster drift. Only `type: linear` is supported. |
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
  - period: 7
    name: weekly
  - period: 365
    name: annual
```

Each seasonality component is modeled with 2 Fourier harmonics (4 parameters: sin/cos × 2 harmonics).

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

**Mixed-grain rules** (enforced at parse time):
- A parent may never be **coarser** than its child — downward disaggregation is undefined.
- A **finer flow/stock** parent is automatically resampled up to the child's grain (sum / last). In the example above, `conversions` at week grain sees the *weekly sum* of `trial_starts`.
- A **finer rate** parent is an error — declare the rate at the child's grain.
- The finer grain must nest in the coarser: days tile weeks and months, but weeks straddle month boundaries, so a weekly parent under a monthly child is an error.

**Period labels are period starts** everywhere: days at midnight, weeks on Monday (ISO), months on the 1st. Partial edge periods are dropped, never zero-filled — a coarse metric's series may therefore end a few days before the raw data window does.

**Windows snap per node.** RCA windows stay day-resolution dates in the API; each node interprets them as the whole periods fully inside. A node whose window holds no whole period reports `"status": "window_shorter_than_grain"` instead of failing the RCA, and every node reports its `grain` and `effective_windows`. Windows that snap to a single period suppress the bootstrap CI (`ci_status: "degenerate_single_period"`) rather than reporting a falsely-precise interval.

**Warehouse SQL contract per grain.** The SQL owns the aggregation: return one row per period at the declared grain, labeled by period start. **Interior** gaps are filled by kind — flow → 0, stock → forward-fill (a gap before the first period is an error), rate → any missing period is an error. **Trailing** gaps are trimmed, not filled: periods after the last row the SQL returned are treated as not-yet-loaded, so a lagging mart ends the series early instead of manufacturing zeros at the tail. (A query returning no rows at all keeps the full zero spine for flows — an all-quiet window is legitimate.)

**Data freshness.** Each metric's true data edge is tracked as it is fetched and exposed as `data_through` in `GET /meta` — the inclusive last date its last observed period covers. When sources disagree (one mart lags the others), the UI anchors every card's headline number, delta, and sparkline at the tree-wide edge via the **As of** selector (toolbar), which defaults to the oldest `data_through` across metrics and counts only periods *fully completed* by that date — so a calendar week the data edge cuts in half never becomes a headline number. The one case this cannot catch is a partially loaded most-recent period (the mart wrote *some* rows for it): detecting that needs load-completeness metadata on the mart side.

**Data-length guidance.** Fits need at least 10 whole periods at the node's grain — coarser grains need proportionally longer windows (a monthly node wants roughly a year of history). Seasonality periods and lags are in grain steps: `period: 7` means weekly on a daily node and seven *months* on a monthly one (the parser warns about that).

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

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/meta` | Metric names, data window, provider type, fitted models (UI bootstrap) |
| `GET` | `/dag` | Full metric DAG (nodes + edges) |
| `GET` | `/metrics/{name}` | Metric definition, time series, and posterior summary |
| `POST` | `/analyze/{name}` | Run Bayesian sampling for a metric |
| `GET` | `/shapley/{name}` | Shapley attribution for a formula metric |
| `POST` | `/rca/{name}` | Root cause analysis over the metric's ancestors |
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
| `reference_start` | Start of the baseline window (`YYYY-MM-DD`) |
| `reference_end` | End of the baseline window (`YYYY-MM-DD`) |
| `analysis_start` | Start of the analysis window (`YYYY-MM-DD`) |
| `analysis_end` | End of the analysis window (`YYYY-MM-DD`) |

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

Query parameters (all required, `YYYY-MM-DD`): `reference_start`, `reference_end`, `analysis_start`, `analysis_end`.

```bash
curl -X POST "http://localhost:9090/rca/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
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
- **Probabilistic nodes** get `attribution_method: "posterior"` — each contribution is the posterior over the parent's raw-scale coefficient (`beta_raw`) times the parent's window-over-window change. Lagged parents are compared over windows shifted back by the lag. These nodes also report a `components` block: the fitted model's own trend and seasonal terms as window-over-window deltas with CIs, so they no longer hide inside `unexplained`.

Every contribution is reported as an `estimate` (mean), a 95% interval (`ci_95`), and `prob_same_direction` (mass on the dominant side of zero). The intervals combine coefficient uncertainty (probabilistic nodes) with **window-sampling uncertainty** — the window means themselves are resampled with a circular moving-block bootstrap (≤7-day blocks, jointly across metrics, seeded so responses are deterministic). This is what keeps a 3-day analysis window honest: its CIs are visibly wider than a 4-week window's.

Unfitted probabilistic nodes in scope are fit with ADVI on demand — on data strictly before the analysis window — and cached, so the endpoint works without a prior `/analyze` call. `ranked_causes` is a documented heuristic that propagates an influence score from the target up the ancestor tree (weighting each hop by the parent's clamped share of its child's gap); use it as a triage ordering.

See [docs/model.md](docs/model.md) for how to read `components`, `unexplained`, and the bootstrap's assumptions.

---

## MCP server (AI assistants)

The server exposes the engine to AI assistants over [MCP](https://modelcontextprotocol.io) at `http://127.0.0.1:9090/mcp` (streamable HTTP; started automatically by `serve`). A chat assistant connected to it can answer "why was revenue down last week?" by running a real RCA — Shapley attributions, credible intervals, the honest `unexplained` remainder — instead of guessing, and "what if we raise marketing spend 10%?" with a posterior from the what-if engine.

Four tools:

| Tool | Backed by | Description |
|------|-----------|-------------|
| `get_tree` | `/meta` + `/dag` | Metric DAG, grains, kinds, and the loaded data window — assistants call this first |
| `explain_metric` | `/metrics/{name}` | One metric's definition, neighbors, recent series, and fit status |
| `run_rca` | `/rca/{name}` | Full root-cause analysis between two windows |
| `run_whatif` | `/simulate` | Do-operator what-if scenario with posterior deltas |

Analysis responses are compacted for token economy (rounded floats, decompositions dropped) and carry two extra fields: `how_to_read` — the interpretation rules from [docs/model.md](docs/model.md) (what `unexplained` means, why `share_of_gap` can exceed 100%, ADVI vs NUTS), so the narrating model states caveats instead of flattening them — and `report_url`, a deep link that replays the exact analysis in the UI (the engine is seeded, so the link reproduces the numbers).

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

Then, with the demo tree served, ask: *"why was revenue down in the last two weeks of March 2024?"*

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
  api/
    main.py          # FastAPI app
  mcp/
    server.py        # MCP tools for AI assistants (get_tree, explain_metric, run_rca, run_whatif)
    shaping.py       # MCP response compaction + how_to_read caveats + UI deep links
static/
  index.html         # UI: Cytoscape DAG + RCA workflow (app.js, style.css)
docs/
  model.md           # Model assumptions & how to interpret results — start here
  ai-context/        # Architecture deep-dives (backend, frontend) for contributors
examples/
  jaffle_shop_tree.yml
knowledge/           # Product & design specs, roadmap, reference trees
tests/
```

**If you're going to interpret breakdown's output, read [docs/model.md](docs/model.md)** — it explains what the model assumes, what `unexplained` means, why shares can exceed 100%, and when to trust (or distrust) a credible interval. **If you're going to work on the codebase, read [AGENTS.md](AGENTS.md)** — the project's invariants and where everything lives.

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


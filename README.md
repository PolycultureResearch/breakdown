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

At startup, breakdown fetches the time series for every metric in the tree from the configured provider (mock, local MetricFlow, or dbt Cloud Semantic Layer) and aligns them on date.

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

## YAML reference

### `provider`

Controls how metric time-series data is fetched.

```yaml
provider:
  type: mock           # mock | local | cloud
  project_path: "..."  # required for type: local
  environment_id: "..."  # required for type: cloud
  host: "..."            # required for type: cloud
  token: "..."           # required for type: cloud
```

| Type | Description |
|------|-------------|
| `mock` | Deterministic synthetic data that respects the tree structure (formula nodes satisfy their formulas, probabilistic children co-move with parents). No config needed. Use for development and testing. |
| `local` | Queries a dbt project on disk via the MetricFlow CLI (`mf query`). Requires `project_path`. |
| `cloud` | Queries the dbt Semantic Layer API via the `dbt-sl-sdk`. Requires `environment_id`, `host`, and `token`. |

For `local` and `cloud`, the metric queried from the semantic layer is the last segment of `source` (e.g., `source: jaffle_shop.metrics.revenue` queries the metric `revenue`); the result is exposed in the tree under `name`. The data window defaults to `2024-01-01`–`2024-04-09` and is set with `--start-date` / `--end-date` (or the `BREAKDOWN_START_DATE` / `BREAKDOWN_END_DATE` / `BREAKDOWN_TREE` environment variables).

### `metrics`

Each metric entry supports the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier used throughout the tree |
| `source` | string | dbt Semantic Layer metric path (e.g., `jaffle_shop.metrics.revenue`) |
| `description` | string | Optional human-readable description |
| `parents` | list | Names of metrics that causally influence this one |
| `formula` | string | Arithmetic expression over parent names (e.g., `"order_count * average_order_value"`). Enables Shapley attribution. |
| `priors` | dict | Bayesian priors for the causal coefficients (see below) |
| `lags` | dict | Per-parent time lag in grain units (days). Regresses the child on each parent's value `N` steps earlier. Mutually exclusive with `formula`. |
| `seasonality` | list | Periodic components to include in the BSTS model |
| `trend` | string or dict | Local-level (random-walk) trend. `trend: linear` uses the default step-size prior HalfNormal(0.05); `trend: {type: linear, sigma: 0.1}` widens it so the trend may absorb faster drift. Only `type: linear` is supported. |

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

Some causal effects show up with a delay — the README's motivating example is support tickets driving churn *weeks later*. A `lags` dict regresses the child on each parent's value `N` grain-steps (days) earlier:

```yaml
- name: churn_rate
  source: my.metrics.churn_rate
  parents: [support_tickets]
  lags: { support_tickets: 21 }   # churn responds to tickets from 3 weeks earlier
```

Rules:
- Every `lags` key must be a parent; every value must be an integer ≥ 1 (grain units, days).
- `lags` and `formula` are mutually exclusive — a formula is a contemporaneous identity.
- The engine shifts each parent by its lag and trims the leading `max(lags)` rows so all series align with no NaNs. It raises if fewer than 10 rows remain.

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
  "baseline": 50000.0,
  "actual": 42000.0,
  "gap": -8000.0,
  "attribution": {
    "order_count": -6200.0,
    "average_order_value": -1800.0
  }
}
```

The `attribution` values are exact Shapley values and are guaranteed to sum to `gap`.

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
      "baseline": 25000.0, "actual": 27000.0, "gap": 2000.0, "relative_change": 0.08,
      "attribution_method": "shapley",
      "unexplained": 12.0,
      "contributions": [
        {"parent": "order_count", "estimate": 1600.0, "share_of_gap": 0.8,
         "ci_95": null, "prob_same_direction": null},
        {"parent": "average_order_value", "estimate": 388.0, "share_of_gap": 0.19,
         "ci_95": null, "prob_same_direction": null}
      ]
    },
    "order_count": {
      "baseline": 500.0, "actual": 540.0, "gap": 40.0, "relative_change": 0.08,
      "attribution_method": "posterior",
      "unexplained": 2.0,
      "contributions": [
        {"parent": "daily_sessions", "estimate": 38.0, "share_of_gap": 0.95,
         "ci_95": [31.0, 45.0], "prob_same_direction": 0.99}
      ]
    }
  },
  "ranked_causes": [
    {"metric": "order_count", "score": 0.8, "via": "revenue"},
    {"metric": "daily_sessions", "score": 0.76, "via": "order_count"}
  ]
}
```

### Root cause analysis

`POST /rca/{name}` combines the two attribution methods across a metric tree:

- **Formula nodes** get `attribution_method: "shapley"` — exact Shapley values over the parent window means. `unexplained` is the part of the gap the arithmetic identity doesn't account for (data noise). These contributions have no `ci_95` / `prob_same_direction`.
- **Probabilistic nodes** get `attribution_method: "posterior"` — each contribution is the posterior over the parent's raw-scale coefficient (`beta_raw`) times the parent's window-over-window change, reported as an `estimate` (mean), a 95% credible interval (`ci_95`), and `prob_same_direction` (posterior mass on the dominant side of zero). Lagged parents are compared over windows shifted back by the lag.

Unfitted probabilistic nodes in scope are fit with ADVI on demand and cached, so the endpoint works without a prior `/analyze` call. `ranked_causes` is a documented heuristic that propagates an influence score from the target up the ancestor tree (weighting each hop by the parent's clamped share of its child's gap); use it as a triage ordering.

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

**How it works:** For a gap between `formula(actuals)` and `formula(baselines)`, each parent's Shapley value is the weighted average of its marginal contribution across all possible coalitions of co-varying parents. For a 2-parent multiplicative formula `A × B`:

```
φ(A) = ΔA × (baseline_B + actual_B) / 2
φ(B) = ΔB × (baseline_A + actual_A) / 2
```

This generalizes to arbitrary formulas and any number of parents via exact enumeration (2ⁿ coalitions).

---

## Project structure

```
breakdown/
  parser.py          # YAML → Pydantic models → NetworkX DAG
  formula.py         # Shared formula validation / safe evaluation
  data_fetch.py      # BaseDataFetcher + Mock / Local / Cloud implementations
  engine/
    model.py         # fit_metric() — BSTS via PyMC; compute_shapley()
    rca.py           # run_rca() + shapley_attribution() — root cause analysis
  api/
    main.py          # FastAPI app
static/
  index.html         # UI: Cytoscape DAG + RCA workflow (app.js, style.css)
docs/
  model.md           # Model assumptions & how to interpret results — start here
examples/
  jaffle_shop_tree.yml
tests/
```

**If you're going to interpret breakdown's output, read [docs/model.md](docs/model.md)** — it explains what the model assumes, what `unexplained` means, why shares can exceed 100%, and when to trust (or distrust) a credible interval.

---

## Tech stack

| Component | Library |
|-----------|---------|
| Bayesian inference | [PyMC](https://www.pymc.io/) 5.x |
| Posterior analysis | [ArviZ](https://python.arviz.org/) |
| Graph modeling | [NetworkX](https://networkx.org/) |
| dbt Semantic Layer | [dbt-sl-sdk](https://github.com/dbt-labs/semantic-layer-sdk-python) + [dbt-metricflow](https://github.com/dbt-labs/metricflow) |
| API | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
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


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

**Requirements:** Python 3.14+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/your-org/breakdown
cd breakdown
uv sync
uv run python main.py serve
```

Open `http://localhost:9090/ui` to explore the metric tree.

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
| `seasonality` | list | Periodic components to include in the BSTS model |
| `trend` | string | Trend type — currently `linear` (Gaussian random walk) |

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

With multiple parents, the same prior currently applies to every parent (scaled per parent's units).

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

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dag` | Full metric DAG (nodes + edges) |
| `GET` | `/metrics/{name}` | Metric definition, time series, and posterior summary |
| `POST` | `/analyze/{name}` | Run Bayesian sampling for a metric |
| `GET` | `/shapley/{name}` | Shapley attribution for a formula metric |
| `GET` | `/ui` | Interactive DAG visualization |

### `POST /analyze/{name}`

Query parameters:

| Param | Default | Description |
|-------|---------|-------------|
| `inference_method` | `nuts` | `nuts` (full MCMC) or `advi` (variational inference — faster, less accurate) |
| `draws` | `500` | Posterior samples to draw |
| `tune` | `500` | Tuning steps (NUTS only) |

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
    model.py         # ModelBuilder (BSTS via PyMC) + compute_shapley()
  api/
    main.py          # FastAPI app
static/
  index.html         # Cytoscape.js DAG visualization
examples/
  jaffle_shop_tree.yml
tests/
  test_parser.py
  test_engine.py
```

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

You log into the snowflake terminal or whatever, and open the zoom call with the CFO. You write a query to confirm that indeed, conversions are way down starting on friday. What then insues is a rapid series of ad-hock hypotheses and checks; the CFO posing questions (maybe it's just the latest iOS update? is it isolated to users in the United States? Is it caused by a decrease in trial starts or a decrease in the rate of trial to paid conversions?), you sweating and writing sql into the terminal. Hours later, through trial and error, you can define the scope of the problem: what kinds of users, divices, geographcial regions, software versions, etc seem to be behind the observed change. And you have some idea about where in the user experience the problem may reside: was it the numerator or denominator of the rate that changed? Was there less traffic overall or were the people who visited less likely to convert? 

It's been my least favorite part of my job at several companies. But there's something to learn from this kind of late-night triage. Essentially, you and the CFO are using your combined knowledge of the business to generate hypotheses, and checking them. You are traversing the causal graph in your heads to look for abnormalies upstream of the observed change that might explain it. You're essentially doing three things: 
1. Constructing the causal graph upstream of the metric where you observed the abnormal change.  What could have caused it? What do we measure about what could have caused it? And do we observe significant changes in those upstream metrics?
2. Slicing the metrics into smaller and smaller slices to locate the anomoly. That's the process of grouping by operating system, geographical region, user type, software version, etc to try to understand if one or more group of users was driving the overall trend. You might slice up the metric of concern itself (the conversion rate, in this example), or the upstream metrics (the number of trials, the number of conversions, etc).
3. Traversing the causal graph to see if something that could pheasably have caused the changed also changed in the same timeframe. Upstream of conversion rate lies the number of users who could potentially convert, and the number who did convert. Upstream of that are the number of trial starts, and upstream of that are the number of web visitors, and upstream of that are the reach of marketing campaigns, etc. There are also metrics that are known or expected to influence conversion, like the rate of adoption of key features during the trial period. We're moving up the causal chain to look for anomolies that might explain the one we observed.

This process is painful, and it's limited in it's ability to produce insights, but it's not crazy. It probably leaves you ready to present something in the Monday mornign meeting. Can we autmate it? And can we improve on it? 

The premise of breakdown is that by defining the causal graph explicitly, before there is an issue to investigate, we enable a less painfull and more powerful process of root cause analysis. We call that causal graph a metrics tree. The nodes of the graph are metrics. The edges (or connections) are causal relationships, either simple deterministic ones (e.g., `new subscription purchases` / `trial ends` = `conversion rate`) or probabalistic ones (e.g., trying out our key features during the trial period increses the probabily of converting at the end of the trial period). You define those metrics and the relationships between them with the stakeholders, much like you define metrics. You define them in YAML. When you visualize them, they look kind of like a tree. 

Defining the metric tree *a priori* solves two big problems, both related to the reducing the search space when you go looking for the root cause. Consider the two implicit strategies on your late night call with the CFO: slicing the metrics and searching the upstream metrics. You probably combine those strategies, slicing the concerning metric many ways, then slicking all the upstream metrics several ways. Without a metric tree, you could try naively lookihg at all your metrics, and seeing what else changed last friday, and slicing all of those, looking for changes that correlate with your concerning metric in time. Maybe you calculate a correlation cofficinet between your concerning metric and every other metric and each of it's possible slices. Problem one is that the more metrics you examine, the more spurious correlations you are likely to observe. You'll spend your time chasing correlations and then trying to assess causation. Problem two is that the combinatorial explosion of metrics and all their possible slices can start to be computationally intensive. It's not a time you want to be slow. And a third problem is that you may miss any complex, conditional relationships between metrics. 

A metric tree dramatically reduces the search space when you slice up metrics to try to locate the anomoly, and it constrains the space to the metrics that could pheasably cause the observed change. 


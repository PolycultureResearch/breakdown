# YAML Syntax Specification: `breakdown` Metric Trees

The `breakdown` platform uses a YAML-based configuration to define the DAG of business metrics and their causal relationships.

## Top-level structure

```yaml
provider:
  type: mock | local | cloud
  # type-specific fields below

metrics:
  - name: ...
    ...
```

---

## `provider`

Controls where metric time-series data comes from.

### `type: mock`
No additional fields. Generates deterministic correlated synthetic data for the jaffle-shop metric tree.

### `type: local`
Invokes the MetricFlow CLI (`mf query`) against a dbt project on disk.

```yaml
provider:
  type: local
  project_path: "/path/to/dbt/project"
```

### `type: cloud`
Queries the dbt Semantic Layer API via `dbt-sl-sdk`.

```yaml
provider:
  type: cloud
  environment_id: "12345"
  host: "semantic-layer.cloud.getdbt.com"
  token: "your-dbt-cloud-token"
```

---

## `metrics`

A list of metric definitions. Each defines a node in the DAG.

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique identifier. Used in `parents` references and API paths. |
| `source` | string | yes | dbt Semantic Layer metric path (e.g., `jaffle_shop.metrics.revenue`). |
| `description` | string | no | Human-readable description. |
| `parents` | list[string] | no | Names of metrics that causally influence this one. All names must exist in the `metrics` list. |
| `formula` | string | no | Arithmetic expression over parent names. Enables Shapley attribution. See below. |
| `priors` | dict | no | Bayesian priors for causal coefficients when the relationship is probabilistic (no formula). |
| `lags` | dict | no | Per-parent time lag in grain units (days) for probabilistic nodes. Mutually exclusive with `formula`. See below. |
| `seasonality` | list | no | Periodic components for the BSTS model. |
| `trend` | string | no | Trend type. Currently unused — trend defaults to a Gaussian random walk. |

---

## `formula`

Expresses an exact arithmetic relationship between a metric and its parents.

```yaml
- name: revenue
  formula: "order_count * average_order_value"
  parents: [order_count, average_order_value]
```

**Allowed operators:** `+`, `-`, `*`, `/`, `**`  
**Allowed operands:** parent metric names and numeric constants  
**Not allowed:** function calls (e.g., `abs(x)`), attribute access, any Python built-in

All names in the formula must appear in `parents`. The parser validates this at load time using Python's `ast` module — no unsafe `eval` is possible.

**Effect on the model:** When a formula is defined, `fit_metric` computes `y_formula = eval(formula, parent_data)` for each time step and fits a BSTS model to the **residual** (`y - y_formula`). This means:
- No `beta` regressor appears in the posterior — the structural relationship is captured by the formula
- Shapley attribution becomes available via `GET /shapley/{name}`
- The residual BSTS still models unexplained trend, seasonality, and noise

**Examples:**

```yaml
formula: "order_count * average_order_value"   # product
formula: "gross_revenue - cost_of_goods"       # difference
formula: "orders / daily_sessions"             # ratio
formula: "a + b + c"                           # additive (same as linear BSTS, but explicit)
```

---

## `priors`

Priors apply to the `coefficient` (β) of each parent regressor in a probabilistic (non-formula) node. If omitted, a weakly informative `Normal(0, 1)` prior is used on normalized data.

Prior parameters are stated in **business units** (raw scale). The engine translates them into normalized space via `scale_prior_params()` before fitting, and adds a `beta_raw` deterministic to the trace so posteriors are also readable in business units.

```yaml
priors:
  coefficient:
    distribution: "Normal"
    params: { mu: 0.1, sigma: 0.02 }
```

Supported distributions and params: `Normal` (`mu`, `sigma`), `HalfNormal` (`sigma`), `Exponential` (`lam`), `LogNormal` (`mu`, `sigma`). Unknown distributions are rejected at parse time and again by the engine.

### Per-parent priors

`coefficient` is the default prior applied to every parent. To give a specific parent a different prior, add that parent's name as a key:

```yaml
priors:
  coefficient: { distribution: "Normal", params: { mu: 0.1, sigma: 0.05 } }  # default for all parents
  daily_sessions: { distribution: "HalfNormal", params: { sigma: 0.2 } }      # per-parent override
```

Resolution per parent: use its own named prior if present, else `coefficient`, else a weakly informative `Normal(0, 1)`. Every key under `priors` must be `"coefficient"` or a member of `parents`; anything else raises a `ValueError` at parse time. Each parent's prior is scaled by that parent's units.

---

## `lags`

Expresses a **time-lagged** causal relationship: the child responds to a parent's value some number of grain-steps (days) in the past. This lets the model capture effects the contemporaneous regression can't — e.g., support tickets driving churn weeks later.

```yaml
- name: churn_rate
  source: my.metrics.churn_rate
  parents: [support_tickets]
  lags: { support_tickets: 21 }   # churn responds to tickets from 3 weeks earlier
```

Rules:
- Keys must be members of `parents`; values must be integers ≥ 1 (in grain units — days).
- `lags` and `formula` are **mutually exclusive** (a formula is a contemporaneous arithmetic identity). Declaring both raises a `ValueError`.

**Effect on the model:** in the probabilistic path, each parent series is shifted back by its lag; the leading `max(lags)` rows of `y` and every parent column are then trimmed so all arrays align with no NaNs, and normalization happens on the trimmed series. If fewer than 10 rows remain after trimming, the engine raises. Parents without an entry in `lags` are used contemporaneously (lag 0).

---

## `seasonality`

Adds Fourier seasonality components to the BSTS model. Each entry adds 4 parameters (sin/cos × 2 harmonics).

```yaml
seasonality:
  - period: 7
    name: weekly
  - period: 365
    name: annual
```

`period` is in the same units as the data grain (default: days). `name` is used to label the posterior variables (e.g., `sin_weekly_h1`, `cos_weekly_h1`).

---

## Validation rules

1. **DAG integrity:** The metric graph must be acyclic. Cycles are detected at parse time.
2. **Parent references:** All names in `parents` must be defined elsewhere in `metrics`.
3. **Formula safety:** Only arithmetic operators and parent names are allowed in `formula`. Unknown names (not in `parents`) or disallowed syntax raises a `ValueError`.
4. **Distribution names:** each prior's `distribution` must be one of the four supported values.
5. **Prior keys:** every key under `priors` must be `"coefficient"` or a declared parent name.

---

## Complete example (jaffle-shop)

```yaml
provider:
  type: mock

metrics:
  - name: daily_sessions
    description: "Total website sessions per day"
    source: jaffle_shop.metrics.sessions

  - name: order_count
    description: "Total orders placed"
    source: jaffle_shop.metrics.order_count
    parents:
      - daily_sessions
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }  # ~10% session-to-order conversion

  - name: average_order_value
    description: "Average revenue per order"
    source: jaffle_shop.metrics.average_order_value

  - name: revenue
    description: "Total revenue — arithmetic identity, Shapley attribution available"
    source: jaffle_shop.metrics.revenue
    formula: "order_count * average_order_value"
    parents:
      - order_count
      - average_order_value
    seasonality:
      - period: 7
        name: weekly
      - period: 365
        name: annual
```

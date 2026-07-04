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

**Effect on the model:** When a formula is defined, `ModelBuilder` computes `y_formula = eval(formula, parent_data)` for each time step and fits a BSTS model to the **residual** (`y - y_formula`). This means:
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

```yaml
priors:
  coefficient:
    distribution: "Normal"
    params: { mu: 0.1, sigma: 0.02 }
```

Supported distributions: `Normal`, `HalfNormal`, `Exponential`, `LogNormal`.

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
4. **Distribution names:** `priors.coefficient.distribution` must be one of the four supported values.

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

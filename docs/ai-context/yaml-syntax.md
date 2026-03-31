# YAML Syntax Specification: `breakdown` Metric Trees

The `breakdown` platform uses a YAML-based configuration to define the Directed Acyclic Graph (DAG) of business metrics and their Bayesian causal relationships.

## Core Schema

The configuration file must define a list of `metrics`.

### 1. Metric Definition
Each metric object contains:
- `name` (string): Unique identifier (e.g., `dau`, `revenue`).
- `description` (string, optional): Human-readable description.
- `source` (string): The dbt Semantic Layer metric identifier (e.g., `dbt.metric.revenue`).
- `parents` (list of strings, optional): Names of metrics that causally influence this metric.
- `priors` (dictionary, optional): Bayesian priors for the relationship with parents and intrinsic properties.

### 2. Bayesian Priors
Priors are defined for the `coefficient` (the causal weight of a parent) and `intercept`.
Supported distributions: `Normal`, `HalfNormal`, `Exponential`, `LogNormal`.

#### Example: Linear Relationship
```yaml
metrics:
  - name: dau
    source: dbt.metric.dau

  - name: conversions
    source: dbt.metric.conversions
    parents:
      - dau
    priors:
      coefficient:
        distribution: "Normal"
        mu: 0.05
        sigma: 0.02
      intercept:
        distribution: "Normal"
        mu: 0
        sigma: 10
```

### 3. Structural Time Series Components
Metrics can include structural components to account for seasonality and trends.
- `trend`: `linear` or `constant`.
- `seasonality`: `weekly`, `monthly`, `annual`.

#### Example: Seasonality
```yaml
metrics:
  - name: revenue
    source: dbt.metric.total_revenue
    seasonality:
      - period: 7
        name: "weekly"
      - period: 365
        name: "yearly"
```

## Validation Rules
1. **Cycle Detection**: The metric tree must be a Directed Acyclic Graph (DAG). Cycles are not allowed.
2. **Metric References**: All parent names must exist within the `metrics` list.
3. **Source Verification**: Sources must match valid dbt metric identifiers (this will be validated against the dbt manifest/semantic layer).

# Backend Architecture: Bayesian Engine

The `breakdown` backend uses `PyMC` to perform Bayesian inference on metric relationships.

## Bayesian Structural Time Series (BSTS) Model

For each metric $Y_t$, we define a state-space model:

$$Y_t = \mu_t + \tau_t + \sum_{j \in \text{parents}} \beta_j X_{j,t} + \epsilon_t$$

Where:
- $\mu_t$ (Trend): Modeled as a random walk or local linear trend.
- $\tau_t$ (Seasonality): Periodic components (e.g., weekly, monthly).
- $\beta_j$ (Causal Coefficients): The impact of parent metric $X_j$ on $Y$.
- $\epsilon_t$ (Innovation): Gaussian noise.

## Core Components

### 1. `ModelBuilder`
Responsible for translating the `NetworkX` DAG into a `PyMC` model.
- Iterates through metrics in topological order.
- Sets up priors based on YAML definitions (or defaults to weakly informative priors).
- Handles missing data and alignment.

### 2. `DataFetch`
Initially mocks dbt Semantic Layer responses.
- Provides daily time-series data for each metric.
- Supports data masking and normalization.

### 3. `InferenceEngine`
Wraps the `PyMC` sampling process.
- Uses `pm.sample()` (NUTS sampler).
- Manages chains, tuning, and convergence checks (R-hat).

## Output Format
The engine returns an `ArviZ` InferenceData object, which is then serialized to JSON for the frontend:
- **Posteriors**: Distribution of $\beta_j$ (mean, 94% Credible Interval).
- **Decomposition**: Time-series plot of trend, seasonal, and causal components.
- **Simulations**: Posterior predictive checks for what-if scenarios.

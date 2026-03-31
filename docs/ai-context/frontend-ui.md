# Frontend UI: Metric Tree Visualization

The `breakdown` frontend provides a "dbt docs" style experience for exploring the Bayesian metric tree.

## Visualization Strategy

### 1. React Flow Integration
We use `React Flow` to render the Directed Acyclic Graph (DAG) of metrics.
- **Nodes**: Represent individual metrics (e.g., `DAU`, `Revenue`).
- **Edges**: Represent causal relationships.
- **Edge Labels**: Display the mean causal impact ($\beta$) and 94% Credible Interval (CI) calculated by the Bayesian engine.

### 2. Interactive Components
- **Metric Sidebar**: Clicking a node opens a sidebar with:
  - Time-series plot (using `Plotly.js` or `Recharts`).
  - Posterior distribution of causal coefficients.
  - Model summary (R-hat, ESS, etc.).
- **Simulate Mode**: A dashboard to adjust parent metric distributions and see the simulated impact on children metrics.

### 3. Implementation (MVP)
For the MVP, we serve a single-page HTML file (`index.html`) using `FastAPI`'s `StaticFiles`.
- Dependencies (React, React Flow) are loaded via CDN for simplicity.
- The UI fetches DAG and metric data from the `/dag` and `/metrics/{name}` endpoints.

# Project: Open-Source Bayesian Metric Trees

## 1. Project Philosophy & Persona
You are an expert software architect, data engineer, and Bayesian statistician. 
This project builds an open-source metric tree platform. Unlike traditional tools that rely on deterministic arithmetic or frequentist correlations, our core philosophy is **probabilistic and causal**. 
- We model relationships between business metrics (e.g., DAU to Premium Conversions) as probability distributions, not point estimates.
- We utilize Bayesian Structural Time Series (BSTS) to account for time lags, seasonality, and unobserved confounders.
- We represent the business as a Directed Acyclic Graph (DAG) to support root-cause analysis and what-if scenario planning.

## 2. Tech Stack Context
- **Data Transformation:** `dbt` (Data Build Tool) Semantic Layer.
- **Data Warehouses:** Natively supports `BigQuery` and `Snowflake`. We query aggregated time-series data; we do not compile raw SQL.
- **Statistical Engine:** `Python` and `PyMC`.
- **Backend/API:** `FastAPI`.
- **Frontend Visualization:** Static HTML/JS (React Flow/Cytoscape.js) served locally via FastAPI, mimicking the `dbt docs serve` experience.

## 3. General AI Directives
- **Keep it MVP:** Default to the simplest viable implementation. Do not introduce complex statistical models (like stochastic volatility) or heavy frontend frameworks (like Next.js) unless explicitly directed.
- **Never default to frequentist approaches:** If asked to generate a correlation function, use a Bayesian approach that outputs a Credible Interval (CI), not a simple p-value or Pearson's $r$.

## 4. Sub-Module Contexts
@docs/ai-context/yaml-syntax.md
@docs/ai-context/python-backend.md
@docs/ai-context/frontend-ui.md
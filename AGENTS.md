# AGENTS.md

Guidance for anyone — human or AI agent — working **on** the breakdown codebase.
If instead you want to **use** breakdown (author a metric tree, run an analysis),
start with the [README](README.md).

## What this is

breakdown is an open engine for **Bayesian metric trees** and root-cause
analysis. It models a business as a DAG of metrics and learns the relationships
between them. The stance is **probabilistic and causal**, never frequentist:

- Relationships are posterior distributions with credible intervals — not point
  estimates, p-values, or Pearson correlations.
- Deterministic edges (arithmetic identities) are decomposed with exact **Shapley
  attribution**; probabilistic edges are learned with **Bayesian Structural Time
  Series (BSTS)**.
- The business is a **DAG**, which is what makes root-cause analysis and what-if
  simulation tractable.

## Tech stack

- **Engine:** Python + [PyMC](https://www.pymc.io/) (BSTS), [ArviZ](https://python.arviz.org/) (posteriors), [NetworkX](https://networkx.org/) (DAG)
- **API:** [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn
- **Frontend:** vanilla JS + [Cytoscape.js](https://js.cytoscape.org/)/dagre + Plotly, served static at `/ui` — **no build step**, in the spirit of `dbt docs serve`
- **Data:** dbt Semantic Layer (local MetricFlow / dbt Cloud) or warehouse-direct SQL; a deterministic **mock** provider for development
- **Config/validation:** [Pydantic](https://docs.pydantic.dev/) v2 · **Packaging:** [uv](https://github.com/astral-sh/uv)

## Repository map

**Using breakdown** — author trees, run analyses, interpret output:

- [`README.md`](README.md) — quickstart, the canonical YAML/tree-authoring reference, API reference, UI walkthrough
- [`docs/model.md`](docs/model.md) — statistical assumptions and how to read results; **read this before trusting output**
- [`examples/`](examples/), [`knowledge/b2b_mrr_tree.yml`](knowledge/b2b_mrr_tree.yml) — a runnable example and a full worked-reference tree

**Building breakdown** — contributing to the code:

- [`docs/ai-context/python-backend.md`](docs/ai-context/python-backend.md) — backend architecture (parser → engine → API, data flow)
- [`docs/ai-context/frontend-ui.md`](docs/ai-context/frontend-ui.md) — frontend architecture (canvas, tabs, overlays, node cards)
- `breakdown/` (engine) · `static/` (UI) · `tests/`
- [`knowledge/`](knowledge/) — product & design specs, roadmap, and historical design docs

## Run & test

```bash
uv sync
uv run python main.py serve            # UI at http://localhost:9090/ui
uv run pytest tests/ -v
```

Point at your own tree with `--tree path/to/tree.yml --start-date … --end-date …`.

## Working agreements

- **Probabilistic, never frequentist.** Anything that measures a relationship
  outputs a credible interval, not a p-value or Pearson _r_.
- **MVP-first.** Prefer the simplest viable implementation. Don't introduce heavy
  statistical models (e.g. stochastic volatility) or frontend frameworks
  (e.g. React/Next) without a clear, stated reason.
- **The engine is stateless.** `fit_metric` is a pure function
  (DAG + data + target → trace). The only cache is `app.state.traces`, passed in
  explicitly — never introduce hidden global state.
- **Parent order is load-bearing.** Parents always come from
  `list(dag.predecessors(name))`; that is the axis order of `beta` / `beta_raw`.
  Any new component must use the same call.
- **The DAG node carries its definition.** `dag.nodes[name]["definition"]` is the
  validated `MetricDefinition` and the single source of truth downstream (attribute
  access, not dict `.get`).
- **Frontend stays a single file each, no build step.**
  `static/{index.html,app.js,style.css}`, dependencies from CDN. Keep it vanilla
  until the UI genuinely outgrows one file.
- **Docs travel with the code.** When you change the API surface, the YAML schema,
  or UI behavior, update the [README](README.md) (user-facing) and the relevant
  `docs/ai-context/` doc (architecture) in the same change.

## Conventions

- **Python:** ruff-formatted (config in `pyproject.toml`); type hints throughout;
  Pydantic models for all external config.
- **Tests:** pytest with deterministic, seeded mock data (`tests/synthetic.py`).
  NUTS-based tests are inherently stochastic — an occasional diagnostic flake
  passes on re-run in isolation.
- **Commits / PRs:** small and focused; the message explains the _why_.

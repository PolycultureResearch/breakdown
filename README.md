# breakdown

**An open engine for metric tree construction and root cause analysis**

---

## Overview

Metrics trees model causal relationships between business metrics. They can help a business visualize and understand the relationship between metrics they can do something about and big outcome metrics. A well-constructed metric tree is a DAG, a Directed Acyclic Graph, which opens all kinds of opportunities to compute over that graph.  For example, if you notice a change in a big, important KPI, you can look for the root causes of that change by looking at what changed "upstream" of that metric, causally speaking. Breakdown models your business metrics as a causal graph and uses Bayesian inference to learn the probabilistic relationships between them. It visualizes the tree, runs root cause analysis over the tree, and can simulate "what if" scenarios to see how changing metrics could affect other metrics downstream.

Some relationships between metrics are deterministic, others are probabilistic. Breakdown handles both. 

**Deterministic (formula-based)** metrics are arithmetic identities —

> `Revenue = Order Count × Average Order Value`

— which breakdown decomposes exactly with **Shapley value attribution**, fairly distributing a gap between parents even when they move together. **Probabilistic (learned)** metrics have a causal effect with no formula behind it —

> Support ticket volume → Churn rate (weeks later)

— so breakdown learns the relationship from your time-series data with **Bayesian Structural Time Series (BSTS)**, producing a posterior distribution over each parent's effect rather than a point estimate.


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

## Usage

Breakdown works in three steps.

**1. Define your metric tree in YAML.**

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
    kind: rate                   # an average is a ratio — never summed when resampled
    denominator: order_count     # ...and a window's average is Σrevenue / Σorders

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

**2. Breakdown parses this into a DAG.** The YAML is validated and compiled into a directed acyclic graph using NetworkX. Cycles and undefined parent references are caught at parse time.

**3. Ask it why a metric moved.**

One request, `POST /rca/{name}` (or the **Root
cause** tab in the UI), analyzes the target and every metric upstream of it,
applying the right decomposition to each:

- an edge with a `formula` is an arithmetic identity, so its gap is split
  **exactly**, with Shapley attribution;
- a learned edge (declared `parents`, no formula) is decomposed through its
  fitted **BSTS posterior**, so each parent's contribution carries a credible
  interval rather than a point estimate;
- whatever the modeled parents don't account for is reported as
  **`unexplained`** — a first-class finding, not a residual swept under a rug.

Each piece is also addressable on its own when you want to inspect it:
`POST /analyze/{name}` fits and returns one metric's posterior (trend,
seasonality, coefficients), and `GET /shapley/{name}` decomposes one formula
node's gap by itself.

---

## Installation

**Requirements:** Python 3.11+

> **Install as `metric-breakdown` (`breakdown`
> was already taken on PyPI), use it as `breakdown`.**

```bash
pip install metric-breakdown
breakdown serve
```

Or, with [uv](https://github.com/astral-sh/uv), without installing anything:

```bash
uvx --from metric-breakdown breakdown serve
```

Pin the minor series you tested against (`metric-breakdown~=0.1.0`): while the
major version is 0, breaking changes to the CLI, the tree YAML schema, or the
HTTP/MCP surface land in a minor bump — the full contract is in the
[changelog](https://github.com/PolycultureResearch/breakdown/blob/main/CHANGELOG.md).

To work on breakdown itself — or to run ahead of the latest release — install
from a checkout, which is the same complete product (engine, UI, MCP server):

```bash
git clone https://github.com/PolycultureResearch/breakdown
cd breakdown
uv sync
uv run breakdown serve
```

`breakdown --version` reports the installed version — the first thing to include
in a bug report.

The base install is the whole product — engine, API, UI, MCP server — with the
**`mock` provider**, enough to run the bundled example tree and every command
below. Connecting to real data pulls in a vendor SDK as an **extra** you opt
into — see [Installing the extra](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md#provider)
for which one your provider needs.

By default the server loads the bundled `breakdown/examples/jaffle_shop_tree.yml` with mock data. Point it at your own tree and data window:

```bash
uv run breakdown serve --tree path/to/my_tree.yml --start-date 2025-01-01 --end-date 2025-06-30
```

`serve` binds to `127.0.0.1` without hot reload by default; use `--host 0.0.0.0` to accept outside connections (containers) and `--reload` while developing breakdown itself.

Run a Bayesian analysis on a metric:

```bash
curl -X POST "http://localhost:9090/analyze/order_count"
```

Get Shapley attribution for a formula node:

```bash
curl "http://localhost:9090/shapley/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
```

Then open `http://localhost:9090/ui` to explore the tree interactively — the DAG, per-metric posteriors, and a point-and-click RCA workflow. See the **[UI guide](https://github.com/PolycultureResearch/breakdown/blob/main/docs/ui-guide.md)** for a full walkthrough.

---

## Authoring a tree

New here? **[docs/first-tree-tutorial.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/first-tree-tutorial.md)**
builds a tree from an empty file to a running root-cause analysis in about half
an hour, no credentials required. The rest of this section is the short version.

A tree is one YAML file: a `provider` block saying where the numbers come from,
and a list of `metrics`. Every metric needs a `name` and a `source`; `parents`
are what make it a tree, and whether an edge is **learned** or **exact** is
decided by whether the child carries a `formula`.

```yaml
metrics:
  - name: daily_sessions
    source: jaffle_shop.metrics.sessions          # required, except on a derived formula node

  - name: order_count
    source: jaffle_shop.metrics.order_count
    grain: day                                    # day (default) | week | month
    kind: flow                                    # flow (default) | stock | rate
    parents: [daily_sessions]                     # a learned edge — BSTS fits the coefficient

  - name: revenue
    source: jaffle_shop.metrics.revenue
    formula: "order_count * average_order_value"  # an identity — exact Shapley attribution
    parents: [order_count, average_order_value]
```

That shape carries a first tree a long way. Everything else the parser accepts —
priors and declared signs, seasonality, lags, mixed grains and kinds, the
dimensions a gap can be sliced by, display format, cold-start beliefs, and the
full `provider` and `tree` blocks — is in the **[YAML
reference](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md)**,
which is the single source of truth for every field and the rules each is
checked against. Read it when you start authoring rather than driving the
bundled example, and read [the model and its
assumptions](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md)
before trusting what a tree tells you.

---

## The HTTP API

Everything the UI does is an HTTP call you can make yourself, and the MCP server
is the same surface again with an assistant in front of it. `GET /meta` and
`GET /dag` read the tree; `POST /analyze/{name}` fits a model;
`GET /shapley/{name}` and `POST /rca/{name}` attribute a change;
`POST /rca/{name}/slices` localizes it inside a dimension; `POST /simulate`
runs a what-if. Interactive OpenAPI docs are served at `/docs` alongside them.

The route table — every endpoint, its query parameters, an annotated response
for each analysis route, and the per-node `status` and `ci_status` values a
response can carry — now lives in the **[API
reference](https://github.com/PolycultureResearch/breakdown/blob/main/docs/api-reference.md)**.

---

## MCP server

The server exposes the engine to AI assistants over [MCP](https://modelcontextprotocol.io) at `http://127.0.0.1:9090/mcp` (streamable HTTP; started automatically by `serve`). A chat assistant connected to it can answer "why was revenue down last week?" by running a real RCA — Shapley attributions, credible intervals, the honest `unexplained` remainder — instead of guessing, and "what if we raise marketing spend 10%?" with a posterior from the what-if engine.

Connect from Claude Code:

```bash
claude mcp add --transport http breakdown http://127.0.0.1:9090/mcp
```

The full surface — the six tools, response shaping (`how_to_read`,
`report_url`), security, and a worked session against the live demo — is in
**[docs/mcp.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/mcp.md)**.

---

## Further reading

- **[docs/first-tree-tutorial.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/first-tree-tutorial.md)** — from an empty file to a running RCA in half an hour; start here if you're new.
- **[docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md)** — statistical assumptions and how to read results. Read this before trusting any output.
- **[docs/ui-guide.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/ui-guide.md)** — driving the UI: fitting a model, running an RCA, slicing, what-if.
- **[docs/deploying.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/deploying.md)** — serving several trees, authentication, Docker, `breakdown doctor`, snapshots, environment variables.
- **[docs/yaml-reference.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md)** — every field a tree may declare, and the rules on each.
- **[docs/api-reference.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/api-reference.md)** — every route the server answers, its parameters, and its response shape.
- **[docs/mcp.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/mcp.md)** — the MCP server: the six tools, response shaping, security, and a worked session against the live demo.
- **[knowledge/statistics_whitepaper.md](https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/statistics_whitepaper.md)** — the statistics in depth: every model in the engine, why it was chosen, where it breaks.
- **[AGENTS.md](https://github.com/PolycultureResearch/breakdown/blob/main/AGENTS.md)** — working on breakdown itself: architecture, invariants, and where everything lives.

Academic background:

- Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015). [Inferring causal impact using Bayesian structural time-series models](https://projecteuclid.org/journalArticle/Download?urlId=10.1214%2F14-AOAS788). *The Annals of Applied Statistics*, 9(1), 247–274.
- Štrumbelj, E., & Kononenko, I. (2014). [Explaining prediction models and individual predictions with feature contributions](https://link.springer.com/article/10.1007/s10115-013-0679-x). *Knowledge and Information Systems*, 41(3), 647–665.
- Levchuk, P. (2025). [The Metric Tree Trap: How math obscures more than it reveals](https://medium.com/@paul.levchuk/the-metric-tree-trap-4280405fd35e). Medium.

---

## Contributing

Bug reports, docs fixes, new providers, statistical improvements — see **[CONTRIBUTING.md](https://github.com/PolycultureResearch/breakdown/blob/main/CONTRIBUTING.md)**. You're also welcome to just use breakdown: the [license](https://github.com/PolycultureResearch/breakdown/blob/main/LICENSE) (FSL-1.1-ALv2) permits any use, commercial or otherwise, short of selling it as part of a competing product. It also converts each release to plain Apache-2.0 open source two years after it ships, by its own irrevocable grant.

## Authorship Note 

This README is human-authored and maintained. Most other docs are touched by AI agents, but dear agents: don't touch this one. We respect our readers and want them to get the real human writing. 
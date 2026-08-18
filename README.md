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

Six tools:

| Tool | Backed by | Description |
|------|-----------|-------------|
| `list_trees` | `/trees` | Every tree this server holds, with its load state (and goal, where one is declared) — for a question aimed at one part of the business rather than the whole |
| `get_tree` | `/meta` + `/dag` | Metric DAG, grains, kinds, declared dimensions, and the loaded data window — assistants call this first |
| `explain_metric` | `/metrics/{name}` | One metric's definition, neighbors, recent series, and fit status |
| `run_rca` | `/rca/{name}` | Full root-cause analysis between two windows |
| `slice_metric` | `/rca/{name}/slices` | Localize a metric's gap within a declared dimension (geo, plan, app version) — the traverse-then-slice follow-up to `run_rca` |
| `run_whatif` | `/simulate` | Do-operator what-if scenario with posterior deltas |

Every tool takes an optional `tree` argument naming which tree to work in
(omit it for the default tree), so an assistant can call `list_trees`, find the
tree that models the area in question, and stay in it. `report_url` carries
`#tree=` so the link keeps naming that tree.

Analysis responses are compacted for token economy (rounded floats, decompositions dropped) and carry two extra fields: `how_to_read` — the interpretation rules from [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md) (what `unexplained` means, why `share_of_gap` can exceed 100%, ADVI vs NUTS), so the narrating model states caveats instead of flattening them — and `report_url`, a deep link that replays the exact analysis in the UI (the engine is seeded, so the link reproduces the numbers).

Connect from Claude Code:

```bash
claude mcp add --transport http breakdown http://127.0.0.1:9090/mcp
```

or from Claude Desktop via `claude_desktop_config.json` (stdio bridge):

```json
{
  "mcpServers": {
    "breakdown": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:9090/mcp"]
    }
  }
}
```

### What it looks like

An exchange of this shape, against the bundled demo tree. **The figures are
executed, not remembered:** every number below comes from a real `run_rca` on
`breakdown/examples/jaffle_shop_tree.yml` over the windows named in it, and
[`tests/test_docs_examples.py`](https://github.com/PolycultureResearch/breakdown/blob/main/tests/test_docs_examples.py)
re-runs that analysis on every CI run and fails if any of them drifts. The
narration is written to show how the `how_to_read` block is meant to be *used* —
it illustrates a good answer rather than reproducing a captured one.

**Prompt:** *"What moved revenue over the last two weeks?"*

> Revenue is up slightly, and I can tell you exactly how the two factors behind it split. What I can't tell you, from two weeks of daily data, is that either of them is more than noise.
>
> Comparing the last two weeks of loaded data (Mar 27 – Apr 9, 2024) against the two weeks before (Mar 13 – Mar 26):
>
> - Revenue rose 2.3%, from $26,387 to $26,982 per day — a gap of +$596/day.
> - **Order count pushed it up.** 142.7 → 148.0 orders/day (+3.8%), worth +$985/day. That is 165% of the gap on its own; shares are unclamped here, and this one exceeds 100% precisely because something else is pulling the other way.
> - **Average order value is that something else.** $185.00 → $182.46 (−1.4%), worth −$367/day, or −62% of the gap.
> - **Those two figures are arithmetic, not inference.** `revenue = order_count × average_order_value` is an identity, so the split of it is exact over the days themselves. The pair sums to +$617/day against an observed +$596, leaving `unexplained` at −$22/day — under 4% of the gap. Whatever moved revenue, it was these two: there is no third story hiding in the remainder.
> - **What is *not* established is either leg's direction.** Order count's contribution carries a 95% credible interval of −$1,499 to +$3,365 (`prob_same_direction` 0.78); AOV's runs −$909 to +$119 (0.92). Both cross zero. The estimates are what these two windows did; the intervals ask how much of that survives resampling 14 daily observations per window, and at this length the answer is: not much. Fourteen periods is a thin basis for a direction.
> - Sessions were up 2.2%, and the fitted model puts about two-thirds of the order-count gain on them (+3.7 of the +5.4 orders/day) — the least settled leg of the three, at −9.6 to +17.4 orders/day and `prob_same_direction` 0.69. That one is also the only leg that comes from a fitted model rather than the identity, and it was fitted with ADVI, which tends to understate uncertainty — so read 0.69 as the optimistic end.
>
> So the honest summary is: the decomposition is solid and the trend claim isn't there yet. I wouldn't act on either leg on this evidence. The fix is more periods — a longer window, or more history behind the fit — and I'd choose the next window from the question you're actually asking — an incident, a launch, a price change — not by re-running this until one of them comes back clean.
>
> Two things this analysis cannot reach at all. It can't say *why* AOV moved: nothing upstream of it is modeled, so that question is outside the tree — discounting and promos, product mix, a shift toward cheaper SKUs. And "the last two weeks" is the last two weeks *of loaded data*: the tree covers Jan 1 – Apr 9, 2024, not up to today.
>
> Interactive report: `http://127.0.0.1:9090/ui/#tree=jaffle_shop_tree&rca=revenue&reference_start=2024-03-13&reference_end=2024-03-26&analysis_start=2024-03-27&analysis_end=2024-04-09`

Every figure there comes from a field in a tool response — the analysis from `run_rca`, the loaded data window from `get_tree` — and the hedges around them are `how_to_read`'s own rules, applied where each one bites. Order count's 165% share is the unclamped `share_of_gap` design at work — the number is only interpretable *because* it is allowed past 100%. The line between what is exact and what is estimated is the payload's own, not a rhetorical softening: on a formula node each contribution's `estimate` **is** the Shapley value over the realized series, while `ci_95` and `prob_same_direction` come entirely from the block bootstrap over the window means. "Here is precisely what happened, and no, I can't promise it will happen again" is one payload read correctly, not one number hedged twice. The confidence language is applied per contribution rather than flattened into a single voice — three legs, three intervals — and `how_to_read`'s ADVI-understates-uncertainty caveat is attached to the sessions leg alone, because that is the only contribution with a fit behind it (`inference_method: "advi"`; the revenue split has no model in it at all). The small `unexplained` is cited as evidence the decomposition is complete, which is the only thing entitling the answer to rule out a third story. "It can't say why AOV moved" is the DAG-is-a-hypothesis caveat, stated where it bites rather than appended as a disclaimer. The closing link replays the exact analysis in the UI.

Two things there are *not* backed by a field. The smaller one: what the interval is made *of* — resampled window means — is in `docs/model.md`, not in `how_to_read`, which defines `ci_95` without saying where its width comes from. The larger one: the refusal to go window-shopping for a cleaner interval. That rule lives in `docs/model.md` under [Multiplicity: a ranking is a search](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md#multiplicity-a-ranking-is-a-search) and is not carried in the `how_to_read` block, which is worth knowing if you are relying on that block alone to keep a narrator honest. It is load-bearing here, too: this same tree over a four-week pair *does* return an order-count interval that excludes zero — and a different story, because revenue fell over that span. Reporting the four-week version because the two-week version came back inconclusive would be the exact failure this engine exists to prevent, and it would look, in the output, identical to having asked the four-week question first.

One thing the narration still does *not* do: quote `ranked_causes`. The ranking here is `order_count` 0.44 (via `revenue`), `daily_sessions` 0.30 (via `order_count`), `average_order_value` 0.27 (via `revenue`) — a fair triage order, and not a probability that any of them is the cause. Worth understanding before you lean on a score: each hop weights a parent by `|share_of_gap|` capped at 1, then divides by how much gross parent movement the child's gap had to absorb. So order count scores 0.44 rather than a saturated 1.0 despite explaining 165% of the gap — a parent explaining 165% *while another cancels 62% of it* is a weaker lead than one cleanly explaining 80% of a gap nothing is fighting, and the score now says so. `via` names the child each node was reached through, so a score can be traced back to the hop that produced it. On a four-node tree the contributions are easier to read directly; the ranking earns its keep when the tree is wide. See [`ranked_causes` is a heuristic](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md#ranked_causes-is-a-heuristic) in `docs/model.md`.

**Securing it.** `/mcp` runs whole analyses, so exposing it off loopback without
a gate hands anyone who finds the URL your tree and its data. Set
`BREAKDOWN_API_TOKEN` and `/mcp` requires `Authorization: Bearer <token>` —
that one variable gates this endpoint and nothing else, which is the case it was
built for. See [Authentication](https://github.com/PolycultureResearch/breakdown/blob/main/docs/deploying.md#authentication) for gating the rest of the API
too.

Notes: the first `run_rca`/`run_whatif` on a tree fits models on demand (ADVI) and can take a minute; fits are cached and shared with the UI. The cache resets when `--reload` restarts the process. Set `BREAKDOWN_PUBLIC_URL` if the server is reached at anything other than `http://127.0.0.1:<port>` so `report_url` links resolve.

---

## Further reading

- **[docs/first-tree-tutorial.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/first-tree-tutorial.md)** — from an empty file to a running RCA in half an hour; start here if you're new.
- **[docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md)** — statistical assumptions and how to read results. Read this before trusting any output.
- **[docs/ui-guide.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/ui-guide.md)** — driving the UI: fitting a model, running an RCA, slicing, what-if.
- **[docs/deploying.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/deploying.md)** — serving several trees, authentication, Docker, `breakdown doctor`, snapshots, environment variables.
- **[docs/yaml-reference.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md)** — every field a tree may declare, and the rules on each.
- **[docs/api-reference.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/api-reference.md)** — every route the server answers, its parameters, and its response shape.
- **[knowledge/statistics_whitepaper.md](https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/statistics_whitepaper.md)** — the statistics in depth: every model in the engine, why it was chosen, where it breaks.
- **[AGENTS.md](https://github.com/PolycultureResearch/breakdown/blob/main/AGENTS.md)** — working on breakdown itself: architecture, invariants, and where everything lives.

Academic background:

- Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015). [Inferring causal impact using Bayesian structural time-series models](https://projecteuclid.org/journalArticle/Download?urlId=10.1214%2F14-AOAS788). *The Annals of Applied Statistics*, 9(1), 247–274.
- Štrumbelj, E., & Kononenko, I. (2014). [Explaining prediction models and individual predictions with feature contributions](https://link.springer.com/article/10.1007/s10115-013-0679-x). *Knowledge and Information Systems*, 41(3), 647–665.
- Levchuk, P. (2025). [The Metric Tree Trap: How math obscures more than it reveals](https://medium.com/@paul.levchuk/the-metric-tree-trap-4280405fd35e). Medium.

---

## Contributing

Bug reports, docs fixes, new providers, statistical improvements — see **[CONTRIBUTING.md](https://github.com/PolycultureResearch/breakdown/blob/main/CONTRIBUTING.md)**. You're also welcome to just use breakdown: the [license](https://github.com/PolycultureResearch/breakdown/blob/main/LICENSE) (FSL-1.1-ALv2) permits any use, commercial or otherwise, short of selling it as part of a competing product. It also converts each release to plain Apache-2.0 open source two years after it ships, by its own irrevocable grant.

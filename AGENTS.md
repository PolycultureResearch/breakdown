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
- [`breakdown/examples/`](breakdown/examples/), [`knowledge/b2b_mrr_tree.yml`](knowledge/b2b_mrr_tree.yml) — the bundled runnable example and a full worked-reference tree

**Building breakdown** — contributing to the code:

- [`docs/ai-context/python-backend.md`](docs/ai-context/python-backend.md) — backend architecture (parser → engine → API, data flow)
- [`docs/ai-context/frontend-ui.md`](docs/ai-context/frontend-ui.md) — frontend architecture (canvas, tabs, overlays, node cards)
- `breakdown/` (engine) · `breakdown/static/` (UI) · `tests/`
- [`knowledge/`](knowledge/) — product & design specs, roadmap, and historical design docs

## Run & test

```bash
uv sync
uv run breakdown serve --reload        # UI at http://localhost:9090/ui
uv run pytest tests/ -v
```

`uv sync` installs every provider extra (the dev group pulls
`metric-breakdown[all]`), so the whole suite runs. **Users don't get that** —
`pip install metric-breakdown` is base-only and the provider SDKs are the `dbt`
and `databricks` extras, so nothing provider-specific may be imported at module
scope and the three tests that need a real SDK skip themselves when it's absent.
CI proves this with a no-extras job; see `docs/ai-context/python-backend.md`.

Point at your own tree with `--tree path/to/tree.yml --start-date … --end-date …`,
or at a **directory** of trees (roadmap 2.16 — one `*.yml` per tree, id = filename
stem; `--default-tree <id>` picks the one the unprefixed routes mean).
`--reload` is opt-in (the installed CLI defaults to no reload, loopback bind);
`breakdown doctor --tree …` checks provider connectivity. Deployment (uvx, Docker)
is covered in the [README](README.md#deploying).

## Working agreements

- **Probabilistic, never frequentist.** Anything that measures a relationship
  outputs a credible interval, not a p-value or Pearson _r_.
- **MVP-first.** Prefer the simplest viable implementation. Don't introduce heavy
  statistical models (e.g. stochastic volatility) or frontend frameworks
  (e.g. React/Next) without a clear, stated reason.
- **The engine is stateless.** `fit_metric` is a pure function
  (DAG + data + target → trace). The only cache is the addressed tree's
  `traces`, passed in explicitly — never introduce hidden global state.
- **A process serves several trees, and they are peers.** A wide revenue tree,
  a team's tree, a feature's tree, a tree behind a target — any durable or
  disposable, any with a goal or without; nothing in the code should assume a
  lifetime or require a goal. Per-tree state lives on `TreeState`
  (`breakdown/api/trees.py`), held in `app.state.trees[id]`; `app.state.X` is an
  alias for the **default** tree's. New per-tree state goes on `TreeState`, not
  on `app.state`. The lock is per-tree; the trace cap is process-wide.
- **Parent order is load-bearing.** Parents always come from
  `list(dag.predecessors(name))`; that is the axis order of `beta` / `beta_raw`.
  Any new component must use the same call.
- **The DAG node carries its definition.** `dag.nodes[name]["definition"]` is the
  validated `MetricDefinition` and the single source of truth downstream (attribute
  access, not dict `.get`).
- **Frontend stays a single file each, no build step.**
  `breakdown/static/{index.html,app.js,style.css}`, dependencies from CDN. Keep it
  vanilla until the UI genuinely outgrows one file. (The files live inside the
  package so the wheel ships them.)
- **Docs travel with the code.** When you change the API surface, the YAML schema,
  or UI behavior, update the [README](README.md) (user-facing) and the relevant
  `docs/ai-context/` doc (architecture) in the same change.
- **The statistics white paper is a living document.** When you ship a
  [Statistical rigor (S)](knowledge/roadmap.md#statistical-rigor-s--a-standing-workstream)
  or [Horizon 0 correctness (C)](knowledge/roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
  item, update three things in the same change: the roadmap row (the source of
  truth for status), the matching §3.2 weakness and §4 item in
  [`knowledge/statistics_whitepaper.md`](knowledge/statistics_whitepaper.md),
  and that paper's **Last updated** date plus a revision-history row. It is a
  public document that tells readers whether they are looking at a known current
  issue or a fixed one — a stale status there is worse than no status.

  **C items carry one extra obligation.** An S item is a *disclosed* limitation;
  a C item is behavior the docs describe wrongly, so shipping one usually makes
  a caveat somewhere false. Grep [`docs/model.md`](docs/model.md) for the
  C-number before you finish — several passages there are marked
  `**Caveat (open, roadmap Cn)**` and must be **deleted, not amended**, once the
  fix lands. A stale caveat understates the engine to its own users, which is the
  same class of error as overstating it. Not every C item has a §3.2 weakness —
  today only **C4, C5 and C7** are cited there by ID; the rest are engineering
  defects or docs corrections. Grep the white paper for the C-number rather than
  assuming, and if it is absent, the roadmap row plus the `docs/model.md` sweep
  are the whole obligation. One passage needs reading rather than grepping: §3.3
  refers to the provider-boundary defects without naming them, so re-read it when
  **C1/C2** land.

## The four rules, and why they are rules

Two hostile reviews (2026-08-05, 2026-08-12) found the same *meta*-defect each
time: **a policy chosen carefully in one file, and not propagated to its
neighbour.** Not the wrong policy — the right one, reasoned about in a comment,
sitting next to an identical situation handled the other way. Five separate
findings across the two reviews were each a defect the author had already fixed
one file over.

So these are written down, and each has a test in
[`tests/test_project_invariants.py`](tests/test_project_invariants.py) that fails
when a *new* violation is added. The tests are structural — they enumerate the
code and check the property — because a test that pins today's four call sites
would not have caught any of the five.

1. **The provider boundary refuses rather than approximates.** A source that
   cannot answer must produce an error or a warning that names what was
   invented, never a silent substitute. `dbt_sql.py` gets this right and refuses
   `agg: last`, joined dimensions and entity flows by name; `dbt_manifest.py`
   did the opposite for `filter` (C15), and `_align_to_spine` did it for a
   leading gap (C18) — same layer, same class, opposite policy, no stated
   reason. If you add a fill, a default or a coercion at this boundary, it logs.

2. **Every cache on `TreeState` is bounded.** `traces` was bounded with a
   paragraph of justification (C8); `slice_cache` and `flow_cache` sat beside it
   unbounded and undiscussed until 2.18. A cap by entry *count* is not
   automatically enough — a cached fit scales with the loaded window, so 256
   entries reached ~3.4 GB against a 2 GB box. Bound by the thing that actually
   grows.

3. **No engine result reaches an encoder unsanitized.** Starlette's
   `allow_nan=False` turns one NaN into an unhandled 500, and
   `mcp/shaping.round_floats` turns it into `null` — a decomposition of nothing,
   handed to an agent. `slices.py` filtered non-finite values with a comment
   explaining exactly this; `rca.py` did not, for one release (C17). A
   non-finite result is withheld with a named `ci_status`/`status`, never
   emitted and never quietly zeroed.

4. **Every coalition enumeration is capped.** `compute_shapley` and
   `simulate.py` both enumerate subsets, O(2ⁿ), and both hold the tree's lock
   while doing it. `simulate.py` capped at `_MAX_SOURCES = 10` and said why;
   `compute_shapley` had no cap and no documented limit until 2.18 — 12 parents
   measured 20s, 14 would be 80s. Refuse above the cap with a remedy; do not
   silently sample, because an approximate Shapley value is a *different number*.

**A fifth rule has no test, and the gap is the point.** *A correct payload
rendered dishonestly is indistinguishable, to the reader, from a dishonest
payload.* Both reviews swept the engine, providers, API, packaging and docs, and
both stopped at the payload — so `applyRcaOverlay` tinting on `node.gap >= 0`
went unnoticed, and since `null >= 0` is `true` in JavaScript, every node the
engine had explicitly declined to analyze rendered **green, with an upward
arrow**. There is no JS test runner here (deliberately — MVP-first), so this one
is enforced by review: when you change what the engine emits, open the UI and
look at it.

## Conventions

- **Python:** ruff-formatted (config in `pyproject.toml`); type hints throughout;
  Pydantic models for all external config.
- **Tests:** pytest with deterministic, seeded mock data (`tests/synthetic.py`).
  Sampler-based tests should pass `random_seed` to `fit_metric` when they
  assert on diagnostics or coefficient values — an unseeded NUTS run can flake
  by chance. RCA/simulate seed their on-demand fits internally.
- **Commits / PRs:** small and focused; the message explains the _why_.

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

- [`README.md`](README.md) — what breakdown is, quickstart, MCP
- [`docs/first-tree-tutorial.md`](docs/first-tree-tutorial.md) — the guided path: empty file → running RCA, building the bundled example from scratch
- [`docs/yaml-reference.md`](docs/yaml-reference.md) — **the canonical tree-authoring reference**: every field the parser accepts and the rules on each
- [`docs/api-reference.md`](docs/api-reference.md) — every route the server answers, its parameters, and its response shape
- [`docs/mcp.md`](docs/mcp.md) — the MCP server: the six tools, response shaping, security, and a worked session against the live demo
- [`docs/model.md`](docs/model.md) — statistical assumptions and how to read results; **read this before trusting output**
- [`docs/ui-guide.md`](docs/ui-guide.md) — driving the UI: fitting a model, running an RCA, slicing, what-if
- [`docs/deploying.md`](docs/deploying.md) — serving several trees, authentication, Docker, `breakdown doctor`, snapshots, environment variables
- [`docs/why-breakdown.md`](docs/why-breakdown.md) — the problem breakdown exists to solve
- [`breakdown/examples/`](breakdown/examples/), [`knowledge/b2b_mrr_tree.yml`](knowledge/b2b_mrr_tree.yml) — the bundled runnable example and a full worked-reference tree
- [`demo/demos.yaml`](demo/demos.yaml) — **the registry of hosted demos**, one per fake_companies vertical (White Cube live; Alpenglow/Meridian/Bristlecone planned): URL, generating scenario, and prebuilt dataset + ground-truth downloads. `python demo/check_demos.py` probes every deployed demo's `/health` + `/manifest`; `python demo/fetch_demo_data.py` pulls a vertical's duckdb (raw tables + dbt marts prebuilt) and its `ground_truth.json` — the planted-anomaly key, so an RCA answer can be *scored* (`fake-companies score` in the fake_companies repo) instead of eyeballed

**Building breakdown** — contributing to the code:

- [`docs/ai-context/python-backend.md`](docs/ai-context/python-backend.md) — backend architecture (parser → engine → API, data flow)
- [`docs/ai-context/frontend-ui.md`](docs/ai-context/frontend-ui.md) — frontend architecture (canvas, tabs, overlays, node cards)
- `breakdown/` (engine) · `breakdown/static/` (UI) · `tests/`
- [`knowledge/`](knowledge/) — product & design specs, roadmap, and historical design docs

### Project structure

```
AGENTS.md            # Orientation for contributors (human or AI) — start here to build
breakdown/
  parser.py          # YAML → Pydantic models → NetworkX DAG
  formula.py         # Shared formula validation / safe evaluation
  grains.py          # All grain arithmetic: period snapping, kind-aware resampling
  data_fetch.py      # BaseDataFetcher + Mock / Local / Cloud / Warehouse implementations
  loading.py         # Tree + provider → aligned GrainedData (no HTTP; doctor and the app share it)
  snapshots.py       # Parquet read-through cache at the fetcher boundary
  dbt_manifest.py    # In-tree models for dbt's semantic_manifest.json
  dbt_bridge.py      # semantic_manifest.json → BindingSpec per node (no dbt Cloud)
  dbt_sql.py         # BindingSpec + grain + window (+ dimension) → dialect SQL
  dbt_provider.py    # The `dbt` provider: profiles.yml → connection → generated SQL
  engine/
    model.py         # fit_metric() — BSTS via PyMC; compute_shapley()
    rca.py           # run_rca() + shapley_attribution() — root cause analysis
    slices.py        # slice_attribution() — dimensional slicing of a metric's gap
    simulate.py      # run_scenario() — do-operator what-if (fitted or cold start)
    progress.py      # Progress callbacks for long-running analyses
  api/
    main.py          # FastAPI app
    trees.py         # TreeState — one per metric tree served by the process
  mcp/
    server.py        # MCP tools for AI assistants (list_trees, get_tree, explain_metric, run_rca, slice_metric, run_whatif)
    shaping.py       # MCP response compaction + how_to_read caveats + UI deep links
  cli.py             # `breakdown serve` / `breakdown doctor` console entry point
  doctor.py          # Provider connectivity checks with copy-paste remediation
  static/
    index.html       # UI: Cytoscape DAG + RCA workflow (app.js, style.css)
  examples/
    jaffle_shop_tree.yml   # The bundled default (mock) tree
docs/
  model.md           # Model assumptions & how to interpret results — start here
  first-tree-tutorial.md  # Empty file -> running RCA, building the bundled example
  yaml-reference.md  # Every field a tree may declare, and the rules on each
  api-reference.md   # Every route the server answers, and what comes back
  mcp.md             # The MCP server: tools, shaping, security, a worked session
  ui-guide.md        # Driving the UI
  deploying.md       # Serving several trees, auth, Docker, doctor, snapshots
  why-breakdown.md   # The problem breakdown exists to solve
  ai-context/        # Architecture deep-dives (backend, frontend) for contributors
knowledge/           # Product & design specs, roadmap, reference trees
packaging/
  mcpb/              # Claude Desktop extension (.mcpb): one-click connector, built on release
tests/
Dockerfile           # Container image (see docs/deploying.md)
compose.yaml
```

## Run & test

```bash
uv sync
uv run breakdown serve --reload        # UI at http://localhost:9090/ui
uv run pytest tests/ -v                # full suite (~8 min: NUTS is the default sampler)
uv run pytest tests/ -m "not slow"     # the fast loop (~1 min): everything that never fits
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
is covered in [docs/deploying.md](docs/deploying.md).

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
- **Frontend stays vanilla, no build step.**
  `breakdown/static/{index.html,app.js,disclosures.js,style.css}`, dependencies
  from CDN, classic scripts sharing the global lexical environment. One
  deliberate split (2026-08-31, amending the old "a single file each" wording
  under its own "until the UI genuinely outgrows one file" clause): the
  **disclosure vocabulary** — every table and helper that turns an engine
  verdict into words a reader sees — lives in `disclosures.js`, loaded before
  `app.js`, because three render surfaces 2,300+ lines apart is how
  `fit_quality` drifted into four wordings (grill 2026-08-29 H7/M12/M7). New
  verdict wording goes there, never inline in a renderer. Keep it vanilla; no
  further splits without the same kind of evidence. (The files live inside the
  package so the wheel ships them.)
- **Docs travel with the code.** When you change the API surface, update
  [`docs/api-reference.md`](docs/api-reference.md); the YAML schema,
  [`docs/yaml-reference.md`](docs/yaml-reference.md); the MCP surface,
  [`docs/mcp.md`](docs/mcp.md); UI behavior or anything a
  newcomer meets first, the [README](README.md) — subject to the rule below.
  Update the relevant `docs/ai-context/` doc (architecture) in the same
  change. The user-facing docs are executed by `tests/test_docs_examples.py`,
  so a stale example there is a test failure rather than a reader's problem.
- **`README.md` is human-written, and stays that way.** Every other document
  in this repo is written and maintained by an AI agent with human oversight
  (each carries a footer saying so); the README is the deliberate exception
  in the other direction, and it says so at its end. Do not edit it directly
  — not for a typo, not to keep it in sync. When a change you are making
  affects something the README states (an install step, a route, the MCP
  surface), say exactly what needs changing in your report and let Devon make
  the edit. Touch the file only on his explicit instruction naming the
  specific change.
- **The statistics white paper is a living document.** When you ship a
  [Statistical rigor (S)](knowledge/roadmap.md#statistical-rigor-s--a-standing-workstream)
  or [Horizon 0 correctness (C)](knowledge/roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
  item, update three things in the same change: the roadmap row (the source of
  truth for status — one line: ID, status, sentence, link; an account longer
  than a sentence or two goes in [`knowledge/roadmap_log.md`](knowledge/roadmap_log.md)),
  the matching §3.2 weakness and §4 item in
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

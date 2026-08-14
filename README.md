# breakdown

**An open engine for Bayesian metric tree construction and root cause analysis**

Metrics trees model causal relationships between business metrics and assist in diagnosing the root causes of changes in KPIs. Breakdown models your business metrics as a causal graph and uses Bayesian inference to learn the probabilistic relationships between them. Instead of asking "did revenue drop?", you can ask "which upstream metric drove it, and how confident are we?"

---

## Two kinds of causal relationships

Breakdown handles both types of relationships you find in real metric trees.

**Deterministic (formula-based):** Some metrics are arithmetic identities.

> `Revenue = Order Count × Average Order Value`

When revenue drops, you can decompose the gap exactly. Breakdown uses **Shapley value attribution** to distribute the revenue shortfall between `order_count` and `average_order_value` in a mathematically fair way — accounting for interaction effects that simpler approaches miss.

**Probabilistic (learned):** Other metrics have a causal effect that isn't computable by formula.

> Support ticket volume → Churn rate (weeks later)

There is no arithmetic connecting them, but historically they co-move. Breakdown learns these relationships from your time-series data using **Bayesian Structural Time Series (BSTS)** models. Each BSTS model decomposes a metric into trend, seasonality, and causal regression terms, producing a posterior distribution over the coefficient on each parent metric.

---

## How it works

### 1. Define your metric tree in YAML

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

### 2. Breakdown parses this into a DAG

The YAML is validated and compiled into a directed acyclic graph using NetworkX. Cycles and undefined parent references are caught at parse time.

### 3. For each metric, choose your analysis

- **BSTS sampling** (`POST /analyze/{name}`) — runs PyMC to fit a state-space model and returns a posterior over trend, seasonality, and causal coefficients.
- **Shapley attribution** (`GET /shapley/{name}`) — for metrics with a `formula`, computes how much of a period-over-period gap each parent is responsible for.

---

## Quickstart

**Requirements:** Python 3.11+

> **⚠️ Not on PyPI yet — install from a checkout.** `metric-breakdown` has
> never been published, so the `pip install` and `uvx` commands in this README
> (here and in [Provider extras](#provider-extras)) **do not resolve today**.
> They are written for the release, not for the present tense. Until it lands,
> the checkout below is the only way to install breakdown, and it is a complete
> one — same engine, same UI, same MCP server. The first published version will
> be **0.1.0**, so `metric-breakdown~=0.1.0` is the pin to write down now.

```bash
git clone https://github.com/PolycultureResearch/breakdown
cd breakdown
uv sync
uv run breakdown serve            # or: uv run python main.py serve
```

> **Installed as `metric-breakdown`, used as `breakdown`.** The name `breakdown`
> was already taken on PyPI, so that is the distribution name — but the command,
> the import package and everything in this documentation are `breakdown`.

Once it is published, either of these will work:

```bash
pip install metric-breakdown
breakdown serve
```

Or, with [uv](https://github.com/astral-sh/uv), without installing anything:

```bash
uvx --from metric-breakdown breakdown serve
```

`breakdown --version` reports the installed version — the first thing to include
in a bug report.

### Provider extras

The base install is the whole product — engine, API, UI, MCP server — with the
**`mock` provider**, which is enough to run the bundled example tree and every
analysis in this README. Connecting to real data pulls in a vendor SDK, and
those are **extras** you opt into. (The `pip install` forms below describe the
package as it will publish; see the [Quickstart](#quickstart) note — until the
first release, a checkout's `uv sync` already installs all of them, because its
dev group asks for `[all]`.)

| You want to use | Install | Brings in |
|---|---|---|
| `mock`, or cold-start `none` | `pip install metric-breakdown` | — |
| `local` (MetricFlow CLI) or `cloud` (dbt Cloud Semantic Layer) | `pip install 'metric-breakdown[dbt]'` | `dbt-metricflow`, `dbt-sl-sdk` |
| `warehouse` (direct SQL) | `pip install 'metric-breakdown[databricks]'` | `databricks-sdk`, `databricks-sql-connector` |
| reading a dbt project's own metric definitions | `pip install 'metric-breakdown[dbt-bridge]'` | `sqlglot` |
| running that generated SQL on **BigQuery** | `pip install 'metric-breakdown[bigquery]'` | `google-cloud-bigquery` |
| all of them | `pip install 'metric-breakdown[all]'` | all of the above — **except on Python 3.14**, where it installs `databricks` and `dbt-bridge` and omits `dbt` (see below) |

`dbt-bridge` is deliberately not part of `dbt`, and depends on nothing from dbt
Labs: reading the semantic manifest `dbt parse` already wrote needs neither
dbt-core, a warehouse adapter, nor the `mf` binary. The manifest is a resolved
JSON artifact, so breakdown models the subset it reads itself and the extra is
one package. That is what keeps the `dbt` provider free of anyone else's Python
ceiling.

This is not cosmetic: the extras are ~66 packages and ~120 MB that most installs
never touch, and dbt-core in particular drags in a large tree of its own.
Selecting a provider without its extra fails with the exact command to run —
and `breakdown doctor --tree …` reports it as its own check — rather than an
`ImportError` traceback.

> **Python 3.14 and the `dbt` extra.** `dbt-metricflow` and `dbt-sl-sdk` both
> declare `requires-python < 3.14`, so **`pip install 'metric-breakdown[dbt]'`
> fails to resolve on 3.14** — use 3.13 or earlier for the `local` and `cloud`
> providers. `[all]` degrades rather than failing there: it installs
> `databricks` and `dbt-bridge`, which work on 3.14, and omits `dbt`. The
> **`dbt` provider is unaffected** and is the one to reach for on 3.14 — it
> needs neither dbt-core nor the `mf` binary.

A tree that names `local` but is served entirely from committed snapshots (see
[Snapshots](#snapshots-fetch-once-refit-forever)) needs neither the extra nor a
dbt project: the extra is only required when a query actually reaches the
provider.

Open `http://localhost:9090/ui` to explore the metric tree. The UI shows the DAG (formula vs learned edges, fit status), per-metric time series and posteriors in business units, and a full point-and-click RCA workflow: pick a target and two windows, run it, and read the answer off the graph — nodes tinted by direction of change, edges weighted by share of the gap explained, ranked causes with credible intervals in the sidebar. RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`) so an analysis can be shared as a URL.

By default the server loads the bundled `breakdown/examples/jaffle_shop_tree.yml` with mock data. Point it at your own tree and data window:

```bash
uv run breakdown serve --tree path/to/my_tree.yml --start-date 2025-01-01 --end-date 2025-06-30
```

`serve` binds to `127.0.0.1` without hot reload by default; use `--host 0.0.0.0` to accept outside connections (containers) and `--reload` while developing breakdown itself.

At startup, breakdown fetches the time series for every metric in the tree from the configured provider (mock, local MetricFlow, dbt Cloud Semantic Layer, or warehouse-direct SQL) and aligns them on date.

Run a Bayesian analysis on a metric:

```bash
curl -X POST "http://localhost:9090/analyze/order_count"
```

Get Shapley attribution for a formula node:

```bash
curl "http://localhost:9090/shapley/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
```

Run tests:

```bash
uv run pytest tests/ -v
```

---

## Authoring a tree

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

## Driving the UI

Start the server and open `http://localhost:9090/ui`. Breakdown fetches every metric's series from the provider at startup, so the first load takes a few seconds. The steps below use the bundled default `breakdown/examples/jaffle_shop_tree.yml` and its `2024-01-01`–`2024-04-09` window; substitute your own target and dates. The header date pickers are bounded to the loaded `--start-date`/`--end-date` window.

**1. Inspect a metric — and fit its model.** Click any node in the graph to open the **Metric** tab (right sidebar) with its time series. Nodes that have a probabilistic parent (e.g. `order_count`) show an **Analyze** section: pick **ADVI — fast, approximate** or **NUTS — slow, exact** and click **Run** to fit the BSTS. Both controls are labelled, a line under the draws box says what the current setting actually costs, and the `ⓘ` beside each expands a short explanation (with a link into [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md) where it goes deeper). The posterior — trend, seasonality, and the `beta` / `beta_raw` coefficient on each parent — fills in, and the node picks up the "fitted" tint. Leaf and formula nodes just show their series.

**2. Run a root-cause analysis.** Choose a **Target** in the header bar, then open the **Root cause** tab and pick the **Analysis** window — the period you want explained — from a preset (Last 7 days, Last 14 days, Last full week) or the date pair. The **Reference** fills itself with the matched adjacent block (marked **auto**) and stays editable: touch it and it becomes custom; the auto chip restores it. The model doesn't train on the reference — it trains on *all* loaded history before the analysis window (each node's result says exactly what it was fitted on) — so the reference is only the baseline the gap is measured against. Click **Run RCA**: breakdown auto-fits any upstream probabilistic models it needs and paints the result on the graph — nodes tinted by direction of change, edges weighted by each parent's share of the explained gap, and a ranked cause list with credible intervals. **Copy link** yields a shareable `#rca=…` URL carrying the resolved windows; **Clear** resets.

**3. Localize a cause with a slice (optional).** Any ranked cause whose metric declares [`dimensions`](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md#dimensions-slicing) shows a **slice by** row — click a dimension to attribute that metric's gap across its values. Slices are ranked by excess concentration (how much more of the gap a value carries than its baseline share predicts), with the leader highlighted, `noise` badges where the bootstrap can't separate concentration from zero, and an explicit "not localized" verdict when nothing stands out. Across a lagged edge the slice automatically uses the parent's lag-shifted windows, so it compares the periods the contribution was actually measured over.

**4. Simulate a what-if (optional).** Open the **What-if** tab, click nodes to adjust them (interventions), optionally add assumption links for effects the tree doesn't encode, and click **Run simulation** for a steady-state projection with credible intervals rendered on the graph and in the sidebar.

RCA runs and metric views are deep-linkable (`#rca=…`, `#metric=…`), so any analysis can be shared or bookmarked as a URL.

---

## Serving several trees

One breakdown process can serve several metric trees. They are **peers**, not a
hierarchy: a company typically keeps one wide tree with revenue at the top (the
net-MRR tree), and alongside it trees that go deep on one part of the business —

- a **marketing** tree whose leaves are channels and campaigns,
- a **product** tree about feature adoption and what it does to retention,
- a tree standing behind a **specific goal**, whether that is a quarter, a
  year, or five years.

Any tree may be long-lived or short-lived, and any may declare a goal or not.
breakdown takes no position on either: a focused tree can be as durable and as
useful as the revenue tree, and most trees have no target attached at all.

Point `--tree` at a directory and every `*.yml` in it (non-recursively) is one
tree, its **id the filename stem**:

```
acme-dbt-project/
  breakdown/
    net_mrr.yml           -> id "net_mrr"
    marketing.yml         -> id "marketing"
    activation.yml        -> id "activation"
```

```bash
breakdown serve --tree ./breakdown --default-tree net_mrr
```

`/ui` then opens an **index** of the trees — title, owner, and, where one is
declared, period and current-vs-target — and a **Tree** switcher appears in the
header. A single `--tree <file>` behaves exactly as it always has: no index, no
switcher, one tree.

**Trees load lazily.** Boot parses every tree's YAML — cheap, no provider
involved — and fetches none, so the index is instant on a cold process and
nobody pays for the trees they didn't open. A tree's data is fetched on the
first request that needs it (or from the index's **Load** button); until then
its card says *not loaded* rather than showing a zero. A single-file `--tree`
still loads at startup, where lazy buys nothing; `--eager` asks for the same
from a directory, loading the default tree up front.

**Failures are per tree.** One malformed YAML shows as a broken card carrying
its own parse error, and the other trees serve normally.

Every data route is available at `/trees/{tree_id}/…` as well as bare, and the
bare paths mean the default tree — so existing links, scripts and MCP clients
keep working unchanged:

```bash
curl -X POST "localhost:9090/trees/marketing/rca/paid_signups?analysis_start=2026-08-01&analysis_end=2026-08-07"
curl localhost:9090/trees            # the index: every tree and its state
```

| Flag | Default |
|------|---------|
| `--tree` | A tree file **or a directory of them** |
| `--default-tree <id>` | The only tree if there is one, else the alphabetically first |
| `--eager` | Off — a directory of trees loads on demand |

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

## Deploying

### Authentication

**By default there is none, and that is safe only because the default bind is
loopback.** `breakdown serve` listens on `127.0.0.1`, so nothing is reachable
from off the machine. The moment you pass `--host 0.0.0.0` (which every
container deployment does), the entire API — your tree, your series, your
generated SQL — is open to anyone who can reach the port. Decide this before you
expose the port, not after.

Access control is one shared bearer token, configured with two environment
variables:

| Variable | Effect |
|---|---|
| `BREAKDOWN_API_TOKEN` | The secret itself. Set alone, it gates **`/mcp` only** — every JSON data route stays open. This is the default behavior and is unchanged. |
| `BREAKDOWN_REQUIRE_AUTH` | Extends the same bearer check to **every route** except a small allow-list. Requires `BREAKDOWN_API_TOKEN`. |

Callers present it as a standard bearer header:

```bash
export BREAKDOWN_API_TOKEN=$(openssl rand -hex 32)
export BREAKDOWN_REQUIRE_AUTH=1
breakdown serve --host 0.0.0.0

curl -H "Authorization: Bearer $BREAKDOWN_API_TOKEN" http://your-host:9090/meta
```

**`BREAKDOWN_REQUIRE_AUTH` is on unless it is explicitly off.** Anything other
than `""`, `0`, `false`, `no` or `off` (case-insensitive, whitespace stripped)
counts as on — so `BREAKDOWN_REQUIRE_AUTH=ture` closes the door rather than
opening it. A typo in a security switch must fail toward the safe side.

**What stays open with the flag on**, and nothing else:

| Open | Why |
|---|---|
| `/health` | Liveness and readiness. `compose.yaml`'s healthcheck calls it with no credentials, and orchestrators can't present one — gating it makes a correctly configured deployment look dead. |
| `/ui` and everything under it | A JS bundle, not data. |
| `/` | A one-line "the API is running" message that carries nothing. |

Everything else is gated, **including `/openapi.json` and `/docs`** — the
allow-list is an allow-list precisely so a route added tomorrow is closed by
default rather than open until someone remembers it. Matching is on **path
segment boundaries**, so `/healthz` and `/uiconfig` are *not* treated as open;
only `/health` itself, and `/ui` plus genuine children like `/ui/app.js`.

> **With the flag on, the browser UI's own fetches are gated too.** `/ui` loads,
> but every request it makes (`/meta`, `/dag`, `/series`, RCA, …) needs the
> header, and a browser will not add one by itself. This mode therefore assumes
> **a reverse proxy that injects the header** — Cloudflare Access, an
> authenticating ingress, an oauth2-proxy sidecar — or an operator who accepts
> that the UI is unusable without one and is gating a machine-facing API. There
> is deliberately no login page, no cookie, and no token-in-the-URL: that is
> hosted mode ([roadmap 3.5](https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/roadmap.md)),
> and a half-built version of it would be worse than none.

**Setting `BREAKDOWN_REQUIRE_AUTH` without `BREAKDOWN_API_TOKEN` is refused.**
Every request would otherwise be checked against an empty secret and pass — the
one configuration that fails *open*. Instead, non-open routes return **503**,
`GET /health` reports `{"status": "degraded", …}` naming the misconfiguration,
and the process logs it at startup. You get loud, diagnosable 503s rather than a
deployment that looks protected and isn't.

There is one asymmetry to know about before you debug it. `/health` is on the
open allow-list and always answers **200**, which is what lets the container
healthcheck run without credentials — so in this misconfigured state the
container reports **healthy while being unusable**: every data route 503s and
the health probe passes. The `status` field is where the truth is; read the
body, not the code. (This is the same design that keeps a provider outage from
looking like a dead container, and it costs exactly this one confusing case.)

**Query redaction, independent of the flag.** Whenever `BREAKDOWN_API_TOKEN` is
set and a caller does not present it, `GET /dag` returns each node's `sql` and
`bind` as `null` rather than their real contents. `/dag` has to stay reachable
for the unauthenticated UI to draw anything — but those two blocks are the only
parts of a definition that are infrastructure rather than modeling: `sql` is the
metric's whole statement and `bind` carries the fully-qualified table name plus
its WHERE-clause business logic. On a deployment that bothered to configure a
token, "the graph is public" should not also mean "our warehouse layout and
filter logic are public". They are redacted to `null` rather than dropped, so a
client reading `def.sql` sees an absent query instead of a missing key. With no
token set (the laptop default) nothing is redacted. The UI's *show query* panel
reads [`GET /metrics/{name}/query`](https://github.com/PolycultureResearch/breakdown/blob/main/docs/api-reference.md#get-metricsnamequery),
which is gated normally, so it loses nothing.

**What this is not.** One shared secret, no per-user identity, no audit trail,
and no revocation short of rotating the value and restarting. It is a down
payment on hosted mode, not a substitute for it. If you need per-user access,
put breakdown behind something that provides it.

### A shared instance with Docker

```bash
cp path/to/my_tree.yml tree.yml
export DATABRICKS_TOKEN=...        # whatever ${VARS} your tree references
export BREAKDOWN_API_TOKEN=$(openssl rand -hex 32)
export BREAKDOWN_REQUIRE_AUTH=1    # gate every route, not just /mcp
export BREAKDOWN_PUBLIC_URL=https://breakdown.acme.com
docker compose up --build
```

The [`compose.yaml`](https://github.com/PolycultureResearch/breakdown/blob/main/compose.yaml) mounts `./tree.yml` read-only at `/config/tree.yml`, passes provider credentials through as environment variables, and healthchecks `GET /health`. The image is large (~2.5–3 GB — PyMC and its compiler toolchain); the first build takes a while.

> **Set the access-control variables in your environment — the shipped
> `compose.yaml` passes them through.** `BREAKDOWN_API_TOKEN`,
> `BREAKDOWN_REQUIRE_AUTH` and `BREAKDOWN_PUBLIC_URL` are all listed bare in its
> `environment:` block, so there is nothing to edit; what the file cannot do is
> decide them for you. A container publishes its port, so leaving the two token
> variables unset means the whole API is open to whatever can reach the host —
> that is a choice, and it should be a deliberate one. See
> [Authentication](#authentication) for exactly what each level gates: the token
> alone gates `/mcp` and redacts `sql`/`bind` from `/dag`; `BREAKDOWN_REQUIRE_AUTH`
> extends the bearer check to every route but `/`, `/health` and `/ui`.
>
> `BREAKDOWN_PUBLIC_URL` is what makes the MCP server's `report_url` deep links
> resolve — without it they point at `http://127.0.0.1:9090`, which is correct
> only on the container's own loopback and therefore useless to whoever the link
> was handed to.
>
> **A variable you never export is *absent* in the container, not empty**, which
> is what keeps an unset `BREAKDOWN_REQUIRE_AUTH` from tripping the
> no-token-configured refusal below.

Three things differ from a laptop run:

- **Credentials must be headless.** The Databricks CLI OAuth `profile:` flow opens a browser, which a container can't. Use `token: ${DATABRICKS_TOKEN}` in the tree's provider block instead (see [`provider`](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md#provider) for `${VAR}` interpolation). If you must reuse a profile, mount both `~/.databrickscfg` and `~/.databricks/token-cache.json` read-only into the container.
- **Startup failures degrade, not crash.** If the provider can't be reached (bad token, warehouse down), the server still starts: `GET /health` returns `{"status": "degraded", "error": …}`, data endpoints return 503, and the UI shows the error with a pointer to `breakdown doctor`. Fix the config and restart — no crash-loop to debug through.
- **The port is published, so the API is exposed.** The compose file passes the access-control variables through, but it cannot set them — if you export nothing, nothing is gated. See [Authentication](#authentication) above.

### Checking connectivity: `breakdown doctor`

Before the first `serve` against real data — or whenever startup reports `degraded` — run:

```bash
uv run breakdown doctor --tree path/to/my_tree.yml
```

It walks the provider's auth chain step by step (tree parses → env vars set → CLI/profile/token valid → connection opens → every metric's query actually runs) and prints `[PASS]`/`[FAIL]` per step with copy-paste remediation for each failure. Exit code is non-zero if anything failed — a `[WARN]` is a result worth reading, not a failure, and does not change the exit code. Probes run over the last 7 days by default; override with `--start-date`/`--end-date`.

Two mode-specific checks ride along: a cold-start tree (`provider: none`) gets its declarations validated instead of a connection probe, and when you pass an explicit `--start-date`/`--end-date` window the doctor adds a **fit readiness** report — each metric's whole-period count against the 10-period fit minimum, the graduation check for a tree [moving from cold start to fitted mode](https://github.com/PolycultureResearch/breakdown/blob/main/docs/yaml-reference.md#cold-start-mode-what-if-with-no-data) — plus a **history headroom** report: whether the provider has history before your `--start-date`. Breakdown trains on everything you load, so an earlier start date strengthens every fit (and the default RCA reference windows) at no cost beyond fetch time.

For the **`dbt` provider**, `doctor` walks manifest → profile → connection →
bindings → dimensions → grain claims → filters, in the order a failure cascades.
The last three are the ones that pay for themselves: a declared dimension that
does not exist becomes a startup failure rather than a 500 on the first *slice
by* click; the **grain claim** (`count(*)` vs `count(distinct grain_key)`)
catches a relation that is not one row per grain — silent fan-out that
multiplies every aggregate over it, which neither MetricFlow nor Cube checks;
and **`filters narrow`** counts kept-vs-total rows for every metric whose dbt
`filter:` was imported.

```
[PASS] grain claims hold  — 12 relation(s) one row per grain, 3 under a filter
[WARN] filters narrow     — 1 filter(s) excluded nothing: everything (6 of 6 rows)
```

Both of the degenerate answers are worth a check of their own. A filter that
keeps **no** rows fails: the node would serve an empty or all-zero series, and
that is the signature of a predicate the warehouse accepts but reads
differently — `= TRUE` against a `VARCHAR`, a boolean stored as `'Y'`. A filter
that excludes **nothing** warns: either it is genuinely vacuous over the probe
window (widen it and re-run) or it is evaluating constant-true, which is the
silently-dropped filter this check exists to prevent. As with the grain claim,
this is a question about your **data**, not your metadata, and no semantic layer
answers it.

### Snapshots: fetch once, refit forever

For non-mock providers, every fetched series is cached as a parquet snapshot keyed on `(metric, grain, kind, window)` — by default in `.breakdown/snapshots/` next to the tree. Later startups with the same window read from disk instead of the warehouse, which makes restarts fast, keeps re-runs reproducible (commit the snapshots next to the tree and an RCA re-runs from a fresh clone), and lets the server boot even when the warehouse is unreachable, as long as every metric has a snapshot.

```bash
uv run breakdown serve --tree my_tree.yml --refresh        # refetch everything, overwrite snapshots
uv run breakdown serve --tree my_tree.yml --no-snapshots   # always hit the provider
uv run breakdown serve --tree my_tree.yml --snapshot-dir /somewhere/writable
```

A snapshot freezes what the provider returned at fetch time — if the warehouse backfills late-arriving data, run `--refresh` once to pick it up. `BREAKDOWN_REFRESH=1` is the environment-variable form, for a scheduled refresh that has no command line to edit. In Docker, `compose.yaml` mounts `./snapshots` and sets `BREAKDOWN_SNAPSHOT_DIR` (the default tree-adjacent location is unwritable there because `/config` is read-only); an unwritable snapshot directory is never fatal — the server logs one warning and runs uncached.

**If your metric restates, the snapshot key cannot tell.** The key is
`(metric, grain, kind, window)` with no content hash, so a series whose *past*
values change — payment plans settling backwards, late-arriving conversions,
any bitemporal source — is frozen at whatever it said the first time. Two rules
follow. Refresh unconditionally on a schedule (`BREAKDOWN_REFRESH=1`) rather
than relying on the cache to notice. And prefer a basis that never restates for
the series you actually fit: an order-*created* date including every status
moves forward only, where a settled or completed basis rewrites history behind
you. Keep the restating version in the tree if you report on it, but do not make
it an RCA target mid-period — the model would be training on values that are
still changing.

### Environment variables

Every `breakdown serve` flag has an environment-variable form, which is what a
container or a scheduled job uses. The flag wins where both are set.

| Variable | CLI flag | Default | What it does |
|---|---|---|---|
| `BREAKDOWN_TREE` | `--tree` | bundled `jaffle_shop_tree.yml` | Tree file **or a directory** of them ([Serving several trees](#serving-several-trees)) |
| `BREAKDOWN_DEFAULT_TREE` | `--default-tree` | the only tree, else alphabetically first | Which tree the unprefixed routes mean |
| `BREAKDOWN_EAGER` | `--eager` | unset (a directory loads lazily) | Load the default tree at boot instead of on first use |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` | Start of the loaded data window |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` | End of the loaded data window |
| `BREAKDOWN_HOST` | `--host` | `127.0.0.1` | Bind address. Anything non-loopback exposes the API — see [Authentication](#authentication) |
| `BREAKDOWN_PORT` | `--port` | `9090` | Listen port |
| `BREAKDOWN_SNAPSHOT_DIR` | `--snapshot-dir` / `--no-snapshots` | `.breakdown/snapshots` beside the tree | Parquet snapshot cache; `off` disables it |
| `BREAKDOWN_REFRESH` | `--refresh` | unset | Skip snapshot reads for one pass and refetch, still writing |
| `BREAKDOWN_API_TOKEN` | — | unset | Bearer token. Alone it gates `/mcp` and redacts `sql`/`bind` from `/dag` |
| `BREAKDOWN_REQUIRE_AUTH` | — | unset | Gate every route but `/`, `/health`, `/ui`. Needs `BREAKDOWN_API_TOKEN` |
| `BREAKDOWN_PUBLIC_URL` | — | `http://127.0.0.1:$BREAKDOWN_PORT` | Base URL for MCP `report_url` deep links, when the server is reached at anything else |
| `BREAKDOWN_MAX_TRACE_BYTES` | — | `536870912` (512 MiB) | Byte budget for the fitted-model cache; `0` disables the byte bound |

**`BREAKDOWN_MAX_TRACE_BYTES` is the one worth understanding before you size a
box.** Fitted models are cached so a second RCA is fast, and the cache is bounded
by **total bytes** rather than by a number of entries, with a 256-entry backstop
behind it. That is not a stylistic choice: one entry's size scales with the
loaded data window — a single ADVI fit over an 830-day window measures ~13 MB of
posterior — so no fixed entry count can be safe for every window, and tuning the
count down only moves the cliff to a wider one. With a byte budget, a wider
window simply caches fewer fits instead of OOM-killing the process. The 512 MiB
default assumes the smallest box this is expected to run on (a 2 GB VM, where the
interpreter plus PyMC and one tree's frames sit near 0.5–0.7 GB resident); raise
it on a larger host, lower it on a smaller one. The on-demand slice and
entity-flow caches are bounded too, by entry count — a slice frame is two orders
of magnitude smaller than a trace.

---

## MCP server (AI assistants)

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
built for. See [Authentication](#authentication) for gating the rest of the API
too.

Notes: the first `run_rca`/`run_whatif` on a tree fits models on demand (ADVI) and can take a minute; fits are cached and shared with the UI. The cache resets when `--reload` restarts the process. Set `BREAKDOWN_PUBLIC_URL` if the server is reached at anything other than `http://127.0.0.1:<port>` so `report_url` links resolve.

---

## Inference methods

### NUTS (default)

No-U-Turn Sampler via PyMC. Produces exact posterior samples with convergence diagnostics (R-hat, effective sample size). Use for:

- Post-mortem root cause analysis
- Building confidence in a new metric relationship
- Any situation where accuracy matters more than speed

### ADVI

Automatic Differentiation Variational Inference. Fits a parametric approximation to the posterior rather than sampling it. Typically 5–10× faster than NUTS. Use for:

- Live incident triage where speed matters
- Early exploration of a new metric tree

---

## Shapley attribution

For metrics connected by a formula, Breakdown computes exact Shapley values to attribute a period-over-period gap to each parent.

**Why Shapley?** Simpler decompositions (e.g., holding one factor fixed while varying the other) produce different answers depending on the order of decomposition. Shapley values are the unique attribution method that is simultaneously: efficient (values sum to the gap), symmetric (order doesn't matter), and null (a parent that didn't move gets zero credit).

**How it works:** Each parent's attribution is the sum of **three exact Shapley games**, all computed by full coalition enumeration (2ⁿ coalitions, vectorized across days):

1. **The window-means bridge** — one game from the parents' reference-window means to their analysis-window means.
2. **The analysis window's co-movement share** — one game per analysis-window day, non-members held at the *analysis* means; averaged over the window it is the parent's share of `mean_an(formula daily) − formula(analysis means)`.
3. **The reference window's co-movement share** — the same inside the reference window, *subtracted*.

The parts telescope, so attributions sum exactly to `mean(formula daily over analysis) − mean(formula daily over reference)` — the formula's own gap. For a 2-parent multiplicative formula `A × B` this reduces to the closed form:

```
φ(A) = Δmean(A) × (mean_ref(B) + mean_an(B)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
φ(B) = Δmean(B) × (mean_ref(A) + mean_an(A)) / 2  +  (cov_an(A,B) − cov_ref(A,B)) / 2
```

**Why per-day, in both windows?** For any nonlinear formula, `mean(A × B)` differs from `mean(A) × mean(B)` by the within-window covariance of A and B. Attributing on window means would silently drop that term — a real behavioral change like "the large orders disappeared" (an orders–AOV covariance shift) would be reported as noise. Treating **both** windows per-day means the covariance *delta* is handed to the parents where it belongs, while a covariance that exists but didn't change contributes nothing — and `unexplained` stays exactly zero for an exact identity instead of absorbing the reference window's covariance.

---

## Project structure

```
AGENTS.md            # Orientation for contributors (human or AI) — start here to build
breakdown/
  parser.py          # YAML → Pydantic models → NetworkX DAG
  formula.py         # Shared formula validation / safe evaluation
  grains.py          # All grain arithmetic: period snapping, kind-aware resampling
  data_fetch.py      # BaseDataFetcher + Mock / Local / Cloud / Warehouse implementations
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
  yaml-reference.md  # Every field a tree may declare, and the rules on each
  api-reference.md   # Every route the server answers, and what comes back
  ai-context/        # Architecture deep-dives (backend, frontend) for contributors
knowledge/           # Product & design specs, roadmap, reference trees
tests/
Dockerfile           # Container image (see "Deploying")
compose.yaml
```

**If you're going to interpret breakdown's output, read [docs/model.md](https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md)** — it explains what the model assumes, what `unexplained` means, why shares can exceed 100%, and when to trust (or distrust) a credible interval. **If you want the statistics in depth**, the [statistics white paper](https://github.com/PolycultureResearch/breakdown/blob/main/knowledge/statistics_whitepaper.md) covers every model in the engine, why it was chosen, where it breaks, and how rigorous the whole thing actually is today. **If you're going to work on the codebase, read [AGENTS.md](https://github.com/PolycultureResearch/breakdown/blob/main/AGENTS.md)** — the project's invariants and where everything lives.

---

## Tech stack

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

## References

- Brodersen, K. H., Gallusser, F., Koehler, J., Remy, N., & Scott, S. L. (2015). [Inferring causal impact using Bayesian structural time-series models](https://projecteuclid.org/journalArticle/Download?urlId=10.1214%2F14-AOAS788). *The Annals of Applied Statistics*, 9(1), 247–274.
- Štrumbelj, E., & Kononenko, I. (2014). [Explaining prediction models and individual predictions with feature contributions](https://link.springer.com/article/10.1007/s10115-013-0679-x). *Knowledge and Information Systems*, 41(3), 647–665.
- Levchuk, P. (2025). [The Metric Tree Trap: How math obscures more than it reveals](https://medium.com/@paul.levchuk/the-metric-tree-trap-4280405fd35e). Medium.

## Use case: Solving for "what happened over the weekend?" 
You get a slack message late Sunday evening from the CFO. "I hate to do this again, but can you meet later? Conversions are way down over the weekend. I have to come into Monday's meeting with some idea of what happened." 

You log into the Snowflake terminal or whatever, and open the Zoom call with the CFO. You write a query to confirm that indeed, conversions are way down starting on Friday. What then ensues is a rapid series of ad-hoc hypotheses and checks; the CFO posing questions (maybe it's just the latest iOS update? is it isolated to users in the United States? Is it caused by a decrease in trial starts or a decrease in the rate of trial to paid conversions?), you sweating and writing SQL into the terminal. Hours later, through trial and error, you can define the scope of the problem: what kinds of users, devices, geographical regions, software versions, etc. seem to be behind the observed change. And you have some idea about where in the user experience the problem may reside: was it the numerator or denominator of the rate that changed? Was there less traffic overall or were the people who visited less likely to convert?

It's been my least favorite part of my job at several companies. But there's something to learn from this kind of late-night triage. Essentially, you and the CFO are using your combined knowledge of the business to generate hypotheses, and checking them. You are traversing the causal graph in your heads to look for anomalies upstream of the observed change that might explain it. You're essentially doing three things:
1. Constructing the causal graph upstream of the metric where you observed the abnormal change.  What could have caused it? What do we measure about what could have caused it? And do we observe significant changes in those upstream metrics?
2. Slicing the metrics into smaller and smaller slices to locate the anomaly. That's the process of grouping by operating system, geographical region, user type, software version, etc. to try to understand if one or more group of users was driving the overall trend. You might slice up the metric of concern itself (the conversion rate, in this example), or the upstream metrics (the number of trials, the number of conversions, etc.).
3. Traversing the causal graph to see if something that could feasibly have caused the change also changed in the same timeframe. Upstream of conversion rate lies the number of users who could potentially convert, and the number who did convert. Upstream of that are the number of trial starts, and upstream of that are the number of web visitors, and upstream of that are the reach of marketing campaigns, etc. There are also metrics that are known or expected to influence conversion, like the rate of adoption of key features during the trial period. We're moving up the causal chain to look for anomalies that might explain the one we observed.

This process is painful, and it's limited in its ability to produce insights, but it's not crazy. It probably leaves you ready to present something in the Monday morning meeting. Can we automate it? And can we improve on it?

The premise of breakdown is that by defining the causal graph explicitly, before there is an issue to investigate, we enable a less painful and more powerful process of root cause analysis. We call that causal graph a metrics tree. The nodes of the graph are metrics. The edges (or connections) are causal relationships, either simple deterministic ones (e.g., `new subscription purchases` / `trial ends` = `conversion rate`) or probabilistic ones (e.g., trying out our key features during the trial period increases the probability of converting at the end of the trial period). You define those metrics and the relationships between them with the stakeholders, much like you define metrics. You define them in YAML. When you visualize them, they look kind of like a tree.

Defining the metric tree *a priori* solves two big problems, both related to reducing the search space when you go looking for the root cause. Consider the two implicit strategies on your late-night call with the CFO: slicing the metrics and searching the upstream metrics. You probably combine those strategies, slicing the concerning metric many ways, then slicing all the upstream metrics several ways. Without a metric tree, you could try naively looking at all your metrics, and seeing what else changed last Friday, and slicing all of those, looking for changes that correlate with your concerning metric in time. Maybe you calculate a correlation coefficient between your concerning metric and every other metric and each of its possible slices. Problem one is that the more metrics you examine, the more spurious correlations you are likely to observe. You'll spend your time chasing correlations and then trying to assess causation. Problem two is that the combinatorial explosion of metrics and all their possible slices can start to be computationally intensive. It's not a time you want to be slow. And a third problem is that you may miss any complex, conditional relationships between metrics.

A metric tree dramatically reduces the search space when you slice up metrics to try to locate the anomaly, and it constrains the space to the metrics that could feasibly cause the observed change.

Both halves of that late-night process are now first-class in breakdown. The traversal is `POST /rca/{name}`: walk the ancestor tree, attribute the gap edge by edge — with lagged edges compared over the right earlier windows, so "trial starts one trial period ago" is exactly what gets examined. The slicing is `POST /rca/{name}/slices`: declare the dimensions worth slicing (`dimensions:` on the metric — region, plan tier, app version), and the gap at any node decomposes exactly across slices, ranked by how much more of the gap each slice carries than its size predicts. An AI assistant connected over MCP runs the whole loop — `run_rca`, then `slice_metric` on the top causes — before the Monday meeting.


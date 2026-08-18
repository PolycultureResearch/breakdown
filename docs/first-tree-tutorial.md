# Your first metric tree

This tutorial takes you from an empty file to a running root-cause analysis in
about half an hour. You will build a four-metric tree from scratch — the same
tree that ships with the package as
[`breakdown/examples/jaffle_shop_tree.yml`](../breakdown/examples/jaffle_shop_tree.yml),
so at every step you can compare your file against a finished one — then serve
it, look at it, and ask it why revenue moved.

No credentials are needed: the tutorial tree uses the `mock` provider, which
synthesizes deterministic data for whatever tree you declare. The last section
shows how to point the same tree at your real data, which changes the
`provider:` block and nothing else.

If you have not installed breakdown yet, the [README's installation
section](../README.md#installation) covers it; everything below assumes
`breakdown` is on your path (or prefix each command with `uv run`).

## See the end first

Before writing anything, look at where you are going:

```bash
breakdown serve
```

Open <http://localhost:9090/ui>. That is the bundled example — the tree this
tutorial builds — rendered as a graph of stat cards: sessions feeding orders,
orders and order value multiplying into revenue. Click a node, run the **Root
cause** tab if you are curious, then stop the server. Now you know what the
YAML below is *for*.

## Step 1 — two metrics and a learned edge

Create `my_tree.yml`:

```yaml
provider:
  type: mock

metrics:
  - name: daily_sessions
    description: "Total count of website sessions per day"
    source: jaffle_shop.metrics.sessions

  - name: order_count
    description: "Total number of orders placed"
    source: jaffle_shop.metrics.order_count
    parents:
      - daily_sessions
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
```

Three things worth understanding here, because they are the core of every tree
you will ever write:

- **`source`** is the metric's address in your data platform. The mock
  provider ignores everything but the name; real providers resolve it (the
  last segment, for the semantic-layer providers).
- **`parents`** declares a causal hypothesis: sessions drive orders. breakdown
  will *learn* the strength of that relationship from data — this is a
  **probabilistic edge**, fitted with Bayesian structural time series, and
  every number it produces carries a credible interval rather than a point
  estimate.
- **`priors`** states what you believe before seeing data, in business units:
  here, roughly a 10% session-to-order conversion (`mu: 0.1`), give or take a
  couple of points (`sigma: 0.02`). The prior is not decoration — it
  stabilizes the fit when history is short, and when data arrives it gets
  updated, not discarded. If you have no belief, omit `priors` and the edge
  gets a weakly-informative default.

## Step 2 — a rate is not a flow

Add average order value:

```yaml
  - name: average_order_value
    description: "Average revenue per order"
    source: jaffle_shop.metrics.average_order_value
    kind: rate
    denominator: order_count
    format: currency
```

Two declarations here prevent a whole class of silently wrong numbers, which
is why the parser will lint you about the first if you forget it:

- **`kind: rate`** says this metric never adds up. Sessions sum across days; the
  *average* of a week of daily AOVs is not the week's AOV. Declaring the kind
  is what stops breakdown from ever aggregating it as though it were a count.
- **`denominator: order_count`** says what it is an average *per*. With this
  declared, a window's AOV is computed as Σrevenue / Σorders — the
  order-weighted answer — instead of a plain mean of daily ratios, which is a
  different number whenever daily volumes differ. Every payload that carries a
  rate says which arithmetic produced it (`window_aggregate`), so a reader can
  always tell.

The other two `kind`s are `flow` (the default: sums, like orders) and `stock`
(a level, like subscriber count — a week's value is where it *ended*, not the
sum of its days).

## Step 3 — an identity is exact

Add revenue:

```yaml
  - name: revenue
    description: "Total revenue"
    source: jaffle_shop.metrics.revenue
    format: currency
    formula: "order_count * average_order_value"
    parents:
      - order_count
      - average_order_value
    seasonality:
      - period: 7
        name: "weekly"
```

This is the second of breakdown's two edge types. `formula` declares an
**arithmetic identity**: revenue *is* orders times order value, exactly, not
statistically. breakdown decomposes identities with exact Shapley attribution
— when revenue moves, the split between "order volume did it" and "order value
did it" is computed, not estimated, and the `unexplained` remainder on a clean
identity is zero to floating point. Learned edges are for relationships you
believe; formula edges are for arithmetic you know.

The `seasonality` entry declares a weekly cycle so weekday shape is explained
as *seasonal* rather than misattributed to a parent. Only declare cycles your
data can actually see — a component needs at least two full periods inside the
fit window, and the fit will warn you (`seasonality_warnings`) if it cannot
identify one you declared.

## Step 4 — run it, and let doctor look at it

Your file should now match the bundled example. Serve it:

```bash
breakdown serve --tree my_tree.yml --start-date 2024-01-01 --end-date 2024-04-09
breakdown doctor --tree my_tree.yml --start-date 2024-01-01 --end-date 2024-04-09
```

`doctor` is the trust gate — run it after any change. On this tree it verifies
the file parses, every rate answers what it is a rate of, and (given the date
flags) that every metric has enough history to fit: each node needs at least
10 whole periods at its grain before breakdown will fit a model to it.

While the server runs, `GET /meta` is the machine-readable view of what
loaded — the window, each metric's grain and kind, and how far its data
actually reaches:

```bash
curl "http://localhost:9090/meta"
```

## Step 5 — your first root-cause analysis

An RCA compares two windows and explains the difference. Three windows are in
play, and confusing them is the most common first-user mistake:

- the **training window** is everything loaded before the analysis starts —
  the model learns "normal" from all of it, untouched by the anomaly;
- the **reference window** is the comparison baseline the gap is measured
  against (left blank, breakdown defaults it to the matched block just before
  the analysis window);
- the **analysis window** is the period you want explained.

In the UI: open the **Root cause** tab, pick the last two weeks of the loaded
window, and click **Run RCA**. Or from the shell:

```bash
curl -X POST "http://localhost:9090/rca/revenue?reference_start=2024-03-13&reference_end=2024-03-26&analysis_start=2024-03-27&analysis_end=2024-04-09"
```

The first run takes a minute — it is fitting real Bayesian models for the
probabilistic nodes; repeat runs reuse them. How to read what comes back:

- Each node reports its **gap** (analysis mean minus reference mean) and
  **contributions** from its parents, each with a 95% credible interval
  (`ci_95`) and a direction probability. An interval that spans zero means
  that parent's role is genuinely not established — that is the tool being
  honest, not broken.
- **`unexplained`** is a first-class finding: the part of the move the modeled
  parents do not account for. On the `revenue` identity it is ~0 by
  construction; on a learned edge, a large `unexplained` means "look outside
  the tree", and the honest story is exactly that.
- **`ranked_causes`** is a triage order — where to look first — not a verdict.
  On this synthetic tree, expect the two revenue parents to offset (shares
  past 100% are normal when parents pull in opposite directions) and neither
  leg's direction to be firmly established over a fortnight: fourteen
  observations is thin evidence, and the intervals say so.

Before trusting output on real questions, read
[docs/model.md](model.md) — it is the practitioner's guide to what these
numbers mean and where the model's assumptions bend.

## Step 6 — point it at your data

The tree you wrote is the analysis; the `provider:` block is only the data
connection. Swap it and everything else stays.

**A dbt project with semantic models** is the shortest path — breakdown reads
`target/semantic_manifest.json` directly and generates its own SQL over the
project's `profiles.yml` connection. No dbt Cloud, no new credentials:

```yaml
provider:
  type: dbt
  project_path: /path/to/your/dbt/project
```

Run `dbt parse` in the project first (that writes the manifest), point each
metric's `source` at a metric name the manifest declares, and run
`breakdown doctor` — it walks the whole chain (manifest → profile → connection
→ per-metric bindings) and tells you exactly which metrics translate and which
are refused, by name and reason.

**dbt Cloud's Semantic Layer**, if that is what you run:

```yaml
provider:
  type: cloud
  environment_id: "12345"
  host: "semantic-layer.cloud.getdbt.com"
  token: "..."
```

**A warehouse with hand-written SQL** (Databricks today), where each metric
carries its own query — see the [YAML
reference](yaml-reference.md#providers) for the `sql:` contract:

```yaml
provider:
  type: warehouse
  host: "dbc-your-workspace.cloud.databricks.com"
  http_path: "/sql/1.0/warehouses/your-warehouse-id"
  token: "..."
  catalog: analytics
  schema: marts
```

Never put a real secret in the file: any provider value may be written as
`${DBT_SL_TOKEN}`-style environment references, which resolve at load.
Whichever provider you choose: set `--start-date` as early as your data
allows (the fit trains on everything loaded, so more history means tighter
intervals — `doctor`'s *history headroom* check tells you if more exists), and
run `doctor` before trusting anything. Fetched series are cached as parquet
snapshots next to the tree, so restarts are cheap and analyses re-run
reproducibly.

## Where to go next

**Slice a gap across a dimension.** Declare `dimensions` on a metric and the
RCA can tell you not just *that* signups fell but *where* — which region,
which plan:

```yaml
- name: signups
  source: my.metrics.signups
  dimensions:
    region: customer__region
```

- **Ask what-if.** The **What-if** tab runs do-operator interventions through
  the fitted tree: move a driver 10% and see the propagated effect on every
  downstream metric, with credible intervals.
- **Let an assistant ask.** The server exposes MCP tools at `/mcp`, so an AI
  assistant can run the same RCAs — with interpretation caveats travelling
  inside every response. See the [README](../README.md#mcp).
- **The full schema** — every field a tree may declare, and the rules on each
  — is [docs/yaml-reference.md](yaml-reference.md); every route is
  [docs/api-reference.md](api-reference.md); serving several trees, auth, and
  Docker are [docs/deploying.md](deploying.md).

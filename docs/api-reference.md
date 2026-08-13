# API reference


Every date parameter is validated as a date at the boundary: anything that is
not a real `YYYY-MM-DD` — including an **empty string**, which a cleared date
field in a form submits — is a 422 naming the parameter, never a 500.

Every route the server answers, its parameters, and what comes back. The UI and
the MCP server are both built on this surface and nothing else, so anything
either of them can show you, a `curl` can too.

For installing and running the server see the [README](../README.md); for the
YAML the tree behind these routes is written in see the
[YAML reference](yaml-reference.md); for how to read the numbers they return see
[the model and its assumptions](model.md).

The **tree-scoped** routes below also answer at **`/trees/{tree_id}/…`** when the
process serves [several trees](../README.md#serving-several-trees); the bare
paths are aliases for the default tree. The process-wide routes have one form
only — a `run_id` is already unique, and the index and the health probe are
about the whole process rather than one tree.

**Tree-scoped** (each also at `/trees/{tree_id}/…`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/meta` | Metric names, data window, provider type, mode (`fitted` \| `cold_start`), per-metric `grains`/`kinds`/`data_through`, fitted models, per-metric `earliest_available` history discovery (UI bootstrap) |
| `GET` | `/dag` | Full metric DAG (nodes + edges), each node carrying its whole definition. `sql` and `bind` come back `null` to a caller that presents no token when one is configured — see [Authentication](../README.md#authentication) |
| `GET` | `/series` | Every metric's series at its native grain, `{name: {grain, dates, values}}` — one call, hydrates the UI's node cards. Mixed-grain trees have no shared date axis, so dates are per metric |
| `GET` | `/metrics/{name}` | Metric definition, time series, posterior summary and fit diagnostics |
| `GET` | `/metrics/{name}/query` | **The query behind a metric's numbers**, when the provider knows it — the provenance surface. Optional `dimension` for the sliced form |
| `POST` | `/analyze/{name}` | Run Bayesian sampling for a metric |
| `GET` | `/shapley/{name}` | Shapley attribution for a formula metric |
| `POST` | `/rca/{name}` | Root cause analysis over the metric's ancestors |
| `POST` | `/rca/{name}/slices` | Attribute one metric's gap across a declared dimension's values — the traverse-then-slice follow-up |
| `POST` | `/simulate` | Do-operator what-if scenario (fitted posteriors, or declared beliefs on a cold-start tree) |

**Process-wide:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | A one-line "the API is running" banner carrying no tree data. Open even under `BREAKDOWN_REQUIRE_AUTH` |
| `GET` | `/health` | Always 200. `{"status": "ok", provider, metrics}`, or `{"status": "degraded", "error": …}` when the default tree can't serve. Liveness for orchestrators — the body, not the status code, carries degraded-ness. Open even under `BREAKDOWN_REQUIRE_AUTH` |
| `GET` | `/trees` | Every tree: title, owner, metric count, `state` (`loaded` \| `not_loaded` \| `loading` \| `error`), plus `period`/`goal` where declared and `progress` for a loaded tree that has a goal. Reads parsed YAML only — never triggers a data load |
| `POST` | `/trees/{id}/load` | Fetch one tree's data now, and return its updated index card |
| `GET` | `/progress/{run_id}` | Live stage of an in-flight RCA or simulation started with that `run_id` |
| `GET` | `/ui` | Interactive DAG visualization |
| — | `/mcp` | [MCP server](../README.md#mcp-server-ai-assistants) for AI assistants (streamable HTTP). Gated by `BREAKDOWN_API_TOKEN` whenever one is set |

## `GET /metrics/{name}/query`

**Never ship a number the engine can't defend.** For most providers a reader
could not see what was actually asked of the warehouse, which left every number
unfalsifiable by exactly the person being asked to trust it. This route closes
that hole: it returns the query behind a metric, so an analyst can check the
number against the definition they think they have.

| Param | Description |
|-------|-------------|
| `dimension` | *(optional)* Show the **sliced** query for one of the metric's declared `dimensions` instead of the plain one |

```bash
curl "http://localhost:9090/metrics/revenue/query"
curl "http://localhost:9090/metrics/signups/query?dimension=region"
```

```json
{
  "metric": "revenue",
  "dimension": null,
  "provider": "dbt",
  "sql": "SELECT DATE_TRUNC('day', ordered_at) AS date, SUM(order_total) AS value ...",
  "dialect": "duckdb",
  "executed": true,
  "note": null
}
```

- **`sql: null` is a real answer, not an error.** The `mock` provider synthesizes
  its data, and the `local`/`cloud` semantic-layer providers hand a metric name
  to someone else's planner and never see SQL. `note` says which case it is —
  "we never see the query" and "no query is run" are different facts about how
  much a reader can verify, and the response keeps them apart rather than
  flattening both to *unavailable*.
- **`executed`** distinguishes the statement that *ran* from the statement that
  *would* run for the loaded window. A snapshot hit serves the number without
  executing anything; the binding still determines it exactly, so the query is
  real provenance either way — but you are told which, rather than left to
  assume. `note` repeats it in words.
- `warehouse` returns the author's own `sql`; `dbt` returns what it generated;
  `SnapshotFetcher` delegates to whichever provider it wraps.
- 404 for an unknown metric, or a `dimension` the metric doesn't declare.

## `POST /analyze/{name}`

Query parameters:

| Param | Default | Description |
|-------|---------|-------------|
| `inference_method` | `nuts` | `nuts` (full MCMC) or `advi` (variational inference — faster, less accurate) |
| `draws` | `500` | Posterior draws — but it buys different things per method. Under `nuts` it is draws **per chain** after `tune` discarded steps, so 500 × 4 chains = 2,000 draws, and more of them tighten the Monte-Carlo error. Under `advi` the optimization is a fixed 20,000 steps regardless, and this only sets how many samples are drawn **from the already-fitted approximation** — more is nearly free and does not make the answer more accurate. |
| `tune` | `500` | Tuning steps (NUTS only) |
| `chains` | `4` | Number of NUTS chains (NUTS only) |
| `fit_end` | none | Exclusive date cutoff (`YYYY-MM-DD`): fit only on rows before it. Defaults to the full window; pass the analysis-window start to reproduce what RCA fits. |

```bash
# Full MCMC (use for post-mortem analysis)
curl -X POST "http://localhost:9090/analyze/order_count?inference_method=nuts&draws=1000"

# Fast variational inference (use for live incident triage)
curl -X POST "http://localhost:9090/analyze/order_count?inference_method=advi"
```

## `GET /shapley/{name}`

Returns how much of the target metric's gap between two time windows is attributable to each parent. Requires a `formula` on the metric definition.

Query parameters:

| Param | Description |
|-------|-------------|
| `analysis_start` | Start of the analysis window (`YYYY-MM-DD`) |
| `analysis_end` | End of the analysis window (`YYYY-MM-DD`) |
| `reference_start` | *(optional)* Start of the baseline window (`YYYY-MM-DD`) |
| `reference_end` | *(optional)* End of the baseline window (`YYYY-MM-DD`) |

Omit **both** reference dates (passing exactly one is a 422) and the engine
defaults to the **matched adjacent block**: the window ending the day before
`analysis_start`, 4× the analysis length (min 28 days, whole weeks when
seasonality is in the target's scope), clamped to the loaded data. The
response echoes the resolved `reference_window`/`analysis_window` and sets
`reference_defaulted`. The reference is only the comparison baseline — the
model always fits on all loaded history before `analysis_start` — see
[model.md](model.md).

Example response:

```json
{
  "target": "revenue",
  "formula": "order_count * average_order_value",
  "grain": "day",
  "effective_windows": {
    "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
    "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
  },
  "baseline": 50000.0,
  "actual": 42000.0,
  "gap": -8000.0,
  "attribution": {
    "order_count": -6200.0,
    "average_order_value": -1800.0
  },
  "decomposition": {
    "order_count": {"means": -6100.0, "covariance_analysis": -80.0, "covariance_reference": 20.0},
    "average_order_value": {"means": -1700.0, "covariance_analysis": -80.0, "covariance_reference": 20.0}
  }
}
```

`baseline` and `actual` are each the **mean of the formula evaluated period by period** (at the target's grain) over the reference and analysis windows respectively (so both windows' within-window co-movement of the parents is included); `gap = actual − baseline`. Each `attribution` value is the sum of three exact Shapley games, reported per parent in `decomposition`: `attribution = means + covariance_analysis − covariance_reference` (the window-means bridge plus the parent's share of each window's within-window co-movement term). The attributions are guaranteed to sum to `gap`. Windows are snapped to whole periods at the target's grain (`effective_windows`); a window with no whole period is a 422.

## `POST /rca/{name}`

Walks the ancestor DAG of `name` and attributes the change between a reference window and an analysis window to upstream metrics. Any probabilistic node in scope that hasn't been fit yet is fit on demand with ADVI and its trace is cached (a second call is much faster).

Query parameters (`YYYY-MM-DD`): `analysis_start`, `analysis_end` (required),
`reference_start`, `reference_end` (optional — omitting both uses the matched
adjacent block, exactly as on `GET /shapley/{name}` above; the response carries
`reference_defaulted`).

```bash
# explicit reference
curl -X POST "http://localhost:9090/rca/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
# defaulted reference
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-02-16&analysis_end=2024-04-09"
```

Trimmed response:

```json
{
  "target": "revenue",
  "reference_window": {"start": "2024-01-01", "end": "2024-02-15"},
  "analysis_window": {"start": "2024-02-16", "end": "2024-04-09"},
  "nodes": {
    "revenue": {
      "status": "ok", "status_reason": null, "grain": "day",
      "effective_windows": {
        "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
        "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
      },
      "baseline": 25000.0, "actual": 27000.0, "gap": 2000.0, "relative_change": 0.08,
      "attribution_method": "shapley",
      "ci_status": "ok",
      "unexplained": 12.0,
      "components": null,
      "contributions": [
        {"parent": "order_count", "estimate": 1600.0, "share_of_gap": 0.8,
         "ci_95": [1450.0, 1740.0], "prob_same_direction": 0.998,
         "prob_same_direction_censored": true},
        {"parent": "average_order_value", "estimate": 388.0, "share_of_gap": 0.19,
         "ci_95": [210.0, 560.0], "prob_same_direction": 0.99}
      ]
    },
    "order_count": {
      "status": "ok", "grain": "day",
      "effective_windows": {
        "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
        "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54}
      },
      "baseline": 500.0, "actual": 540.0, "gap": 40.0, "relative_change": 0.08,
      "attribution_method": "posterior",
      "ci_status": "ok",
      "unexplained": 1.4,
      "components": {
        "trend": {"estimate": 0.5, "ci_95": [-1.1, 2.2]},
        "seasonal": {"estimate": 0.1, "ci_95": [-0.6, 0.8]}
      },
      "contributions": [
        {"parent": "daily_sessions", "estimate": 38.0, "share_of_gap": 0.95,
         "ci_95": [30.0, 46.0], "prob_same_direction": 0.99}
      ]
    }
  },
  "ranked_causes": [
    {"metric": "order_count", "score": 0.8, "via": "revenue"},
    {"metric": "daily_sessions", "score": 0.76, "via": "order_count"}
  ]
}
```

Per-node fields added by grain support: `grain` (the grain the node was analyzed at) and `effective_windows` (the whole periods the requested windows snapped to at that grain). Gaps are mean-per-period at each node's own grain, so compare nodes via `share_of_gap` and `ranked_causes` scores, not raw gaps, in mixed-grain trees.

### Per-node `status` — one bad node does not end the analysis

Every node in scope carries a `status`. Anything other than `"ok"` means the
node is reported **without attribution** while the rest of the tree comes back
normally, with the reason in `status_reason` (`null` when the status is `"ok"`).
Read `status` first and branch on it; every other key is always present, so a
skipped node has the same shape as an attributed one.

| `status` | What it means to you |
|---|---|
| `ok` | Attributed normally. |
| `window_shorter_than_grain` | Your windows hold no whole period at this node's grain — e.g. a 3-day window on a monthly node. Nothing is wrong with the data; widen the window, or accept that this node can't speak to a change this short. |
| `fit_failed` | The node's own model could not be fitted. Overwhelmingly this is **a series with no variance across the fit window** — a parent held at zero the whole time, which for a seasonal business is simply its off-season. A constant series cannot be normalized, so there is no coefficient to attribute with. |
| `attribution_failed` | A formula node whose exact decomposition is not a finite number over these windows — in practice **a zero denominator** somewhere in the window. Note what survives: the node's own `baseline`, `actual` and `gap` are read off the data, not the model, so they are real and are still reported. Only the split across parents is missing. |

`fit_failed` and `attribution_failed` exist because the alternative was worse:
one unfittable node used to abort the entire tree analysis and return nothing at
all. `status_reason` carries the engine's own diagnostic — for
`attribution_failed` it names the offending parent series, the window, and the
dates that are zero, so you can narrow the window past them or fix the series at
the source.

The **RCA target itself is the exception**: the whole response is about that
node, so a failure there is raised as a 422 carrying the same diagnostic rather
than buried in a status nobody would find useful.

### `ci_status`

`ci_status` reports the health of a node's credible intervals, independently of
`status`:

| `ci_status` | Meaning |
|---|---|
| `ok` | Intervals computed normally. |
| `degenerate_single_period` | A formula node whose windows snapped to one period. The block bootstrap would return identical replicates, so intervals are **withheld** rather than reported at a falsely-zero width. |
| `posterior_only_single_period` | The same for a posterior node — coefficient uncertainty remains, but the window-sampling component is absent, so the interval is narrower than the truth. |
| `nonfinite_bootstrap_replicates` | At least one interval on this node was computed from a **subset** of the bootstrap replicates, or withheld because too few survived. A resampled denominator can land on ~0 even when no single period is zero. **The point estimates are unaffected** — they are the exact Shapley values, never bootstrap means; only the intervals lost resolution. |

**Two-level attribution (formula nodes).** Each formula-node contribution also carries a `decomposition` — `{"means": {estimate, ci_95}, "comovement": {estimate, ci_95}}` with `means + comovement = estimate` exactly per bootstrap replicate — and the node carries an `interaction` summary (the summed co-movement shift across parents, with its own CI). The UI's default **Headline** view is the classic price/volume/mix bridge built from these: one row per parent showing its means-bridge contribution, plus one explicit *co-movement shift* row, plus unexplained — rows total to the gap. The **Detailed** toggle expands each parent to its full split. The interaction is shown as its own labeled row rather than silently folded into the factors; for products it is exactly the parents' covariance delta, for other formulas the full within-window co-movement/Jensen shift.

## Root cause analysis

`POST /rca/{name}` combines the two attribution methods across a metric tree:

- **Formula nodes** get `attribution_method: "shapley"` — exact symmetric per-day Shapley values (a window-means bridge plus each parent's share of the within-window co-movement term of each window, analysis added and reference subtracted), so shifts in the parents' within-window co-movement are attributed to parents. `unexplained` is only the target's own measurement noise around the formula — for an exact identity it is zero.
- **Probabilistic nodes** get `attribution_method: "posterior"` — each contribution is the posterior over the parent's raw-scale coefficient (`beta_raw`) times the parent's window-over-window change. Lagged parents are compared over windows shifted back by the lag, and each lagged contribution reports `lag` and `parent_windows` — the parent's own shifted `{reference, analysis}` windows — so you can see (and reuse, e.g. for `POST /rca/{parent}/slices`) exactly which parent periods were examined. These nodes also report a `components` block: the fitted model's own trend and seasonal terms as window-over-window deltas with CIs, so they no longer hide inside `unexplained`. `components` carries only the terms the model actually contains — every fit has a local level, so `trend` is always there, but a node that declares no [`seasonality`](yaml-reference.md#seasonality) has no `seasonal` key at all rather than a 0.0 with a zero-width interval.

Every contribution is reported as an `estimate` (mean), a 95% interval (`ci_95`), and `prob_same_direction` (mass on the dominant side of zero). The intervals combine coefficient uncertainty (probabilistic nodes) with **window-sampling uncertainty** — the window means themselves are resampled with a circular moving-block bootstrap (≤7-day blocks, jointly across metrics, seeded so responses are deterministic). This is what keeps a 3-day analysis window honest: its CIs are visibly wider than a 4-week window's.

`prob_same_direction` is a proportion over those 500 replicates, so **it is never published as 1.0** — there is no representable value between 0.998 and 1, and a saturated count is the estimator running out of resolution rather than a measurement of certainty. A saturated estimate publishes the ceiling alongside `prob_same_direction_censored: true`, and the UI and the exported report both render it as the bound it is: `>99.8%`. `prob_concentrated` (slices) and `prob_direction` (what-if) follow the same rule with their own sample sizes. Read it as "no replicate crossed zero", not as "the sign is certain".

Unfitted probabilistic nodes in scope are fit with ADVI on demand — on data strictly before the analysis window — and cached, so the endpoint works without a prior `/analyze` call.

`ranked_causes` is a documented heuristic: it propagates an influence score from the target up the ancestor tree, weighting each hop by the parent's `|share_of_gap|` (capped at 1) divided by the child's total gross parent movement — the sum of every parent's `|share_of_gap|`, floored at 1 so a decomposition that sums tidily is never penalized. That divisor is what stops a parent scoring full marks on a gap its siblings cancelled: two parents at +165% and −62% both rank *below* a lone parent cleanly explaining 80%. Each row carries `via`, the child it was reached through, so a score can be traced back to the hop that produced it; a node no hop ever reached is omitted rather than listed at zero (`nodes` remains the full inventory of what was in scope). Use it as a triage ordering, not as a probability.

See [model.md](model.md) for how to read `components`, `unexplained`, and the bootstrap's assumptions.

## `GET /progress/{run_id}` — live progress

RCA and simulation can spend a minute or more fitting ancestor models. Pass any
opaque `run_id` you like to `POST /rca/{name}` or `POST /simulate` and poll this
endpoint while the request is in flight to see what the engine is actually doing:

```bash
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-04-03&analysis_end=2024-04-09&run_id=abc123" &
curl -s "http://localhost:9090/progress/abc123"
# {"stage":"fitting","metric":"order_count","current":1,"total":3}
```

Stages are `waiting` (queued behind another analysis), `resolving`, `fitting`
(with `metric`, `current`, `total`), then `attributing` or `simulating`. An
unknown or finished id returns `{"stage": null}` with a 200 — to a poller a
finished run and a never-started one are the same answer.

Progress is entirely optional: **omit `run_id` and nothing is tracked**, which is
the default for every non-UI caller. It never affects the analysis or its result.

## `POST /rca/{name}/slices`

The traverse-then-slice follow-up: attribute one metric's window-over-window gap across a declared dimension's values. The reference dates are optional here too (same defaulting rule; the response carries `reference_defaulted`) — but when slicing a **lagged parent** surfaced by an RCA, pass its `parent_windows` explicitly: the default matches the metric's own timeline, not a lag-shifted one.

```bash
curl -X POST "http://localhost:9090/rca/signups/slices?dimension=region&reference_start=2024-02-05&reference_end=2024-03-03&analysis_start=2024-03-04&analysis_end=2024-03-10"
```

```json
{
  "metric": "signups", "dimension": "region", "grain": "day", "kind": "flow",
  "effective_windows": {
    "reference": {"start": "2024-02-05", "end": "2024-03-03", "n_periods": 28},
    "analysis": {"start": "2024-03-04", "end": "2024-03-10", "n_periods": 7}
  },
  "baseline": 1240.0, "actual": 1130.0, "gap": -110.0,
  "attribution_method": "slice_sum",
  "slices": [
    {"value": "emea", "baseline": 273.0, "actual": 178.0,
     "contribution": -95.0, "share_of_gap": 0.86, "baseline_share": 0.22,
     "excess": -70.8, "ci_95": [-84.1, -57.9], "prob_concentrated": 0.99,
     "noise_level": false},
    {"value": "__other__", "n_values": 2, "contribution": -9.0, "...": "..."}
  ],
  "reconciliation": {"mean_residual": 0.0, "max_abs_residual": 0.0,
                     "residual_share_of_baseline": 0.0, "status": "ok"},
  "ci_status": "ok", "caveats": []
}
```

- `contribution` is the slice's own window-mean change; contributions sum exactly to the sliced gap (flows/stocks are sum identities over slices).
- `excess = contribution − baseline_share × gap` is the **localization signal**: how much more of the gap the slice carries than its size predicts. Excesses sum to zero — concentration is a reallocation of the gap. `prob_concentrated` is the bootstrap probability the excess direction is real; `noise_level: true` rows should not be narrated as localized.
- Rate metrics return `attribution_method: "slice_blend"`: each slice splits into `within` (its own rate moved) and `mix` (traffic shifted between slices), summing exactly to the blended gap, with the total composition effect in `mix_total`.
- `reconciliation` compares the slices' sum (or weighted blend) against the metric's own series; `"discrepant"` means the dimension doesn't cleanly partition the metric — attributions are then approximate, and say so.
- When slicing a **lagged parent** surfaced by an RCA, pass the parent's lag-shifted windows — its RCA contribution carries them as `parent_windows`; those are the periods that influenced the child.

Sliced series are fetched from the provider on demand for just these windows and cached per (metric, dimension, window); nothing about slicing touches the startup data or the fits.

**Both windows must lie inside the loaded data window.** Because slicing reads
from the provider for whatever window you ask for, an out-of-range request is a
422 naming the loaded window, checked **before any provider call**. Previously
nothing bounded these dates beyond "they parse", so a typo could ask a warehouse
for a 200-year scan — holding the tree's lock for the duration — and only then
fail for having no data in it. If you need a window outside what is loaded,
restart with a wider `--start-date`/`--end-date` for that tree.

## `POST /simulate`

Do-operator what-if: intervene on one or more metrics, propagate the change
through the downstream subgraph per posterior draw, and report the steady-state
effect with credible intervals. The scenario is a **JSON body**; the only query
parameter is the optional `run_id`.

| Body field | Type | Description |
|---|---|---|
| `baseline_start` | date | Start of the window defining "current normal". **Required** on a tree with data; **rejected** on a [cold-start tree](yaml-reference.md#cold-start-mode-what-if-with-no-data), where operating points come from each node's declared `baseline` instead |
| `baseline_end` | date | End of that window. Same rule |
| `interventions` | list | `{metric, mode, value}` — `mode` is `set` (absolute level), `delta` (absolute change), or `pct` (fractional change, `0.1` = +10%). One intervention per metric |
| `assumptions` | list | `{source, target, effect: {kind, low, high}, id?, note?}` — a user-asserted effect on an edge the tree doesn't encode. `kind` is `relative` (scaled by the target's baseline) or `absolute` (the target's business units); `low`/`high` are read as the **central 90% interval** of a Normal |
| `levers` | list | `{name, value?, unit?}` — display metadata only; levers have no dynamics of their own in v1 |

A scenario needs **at least one** intervention or assumption, and at most **10**
of the two combined — the source decomposition enumerates coalitions exactly as
[formula attribution](yaml-reference.md#formula) does, and is capped for the same reason.

`baseline_start`/`baseline_end` are the window the simulation measures *from*:
each node's operating point is its mean over that window, at the node's own
grain. It is not a fit window — coefficients come from posteriors fitted on all
loaded history, or from declared priors on a cold-start tree.

```bash
curl -X POST "http://localhost:9090/simulate" \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline_start": "2024-03-13",
    "baseline_end": "2024-04-09",
    "interventions": [{"metric": "daily_sessions", "mode": "pct", "value": 0.10}]
  }'
```

The response carries `mode` (`fitted` | `cold_start`), the resolved
`baseline_window` (null in cold start), `n_draws`, `seed`, a `sources`
decomposition (each intervention's and assumption's signed share, summing
exactly to the point delta by Shapley efficiency), per-node results
(`status` — `baseline` | `affected` | `intervened` — `baseline`, `simulated`,
`delta` with `ci_95`, `relative_delta`, `prob_direction`, `fit_quality`,
`extrapolation`, `contributions`), plus `warnings` and always-on `caveats`. The
run is seeded, so identical calls are byte-identical.

Pass an optional `run_id` query parameter to follow a long simulation with
[`GET /progress/{run_id}`](#get-progressrun_id--live-progress), exactly as for
RCA.

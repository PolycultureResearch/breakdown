# API reference


Every date parameter is validated as a date at the boundary. Anything that is
not a real `YYYY-MM-DD` is a 422 naming the parameter, never a 500. That
includes an empty string, which a cleared date field in a form submits.

Every route the server answers, its parameters, and what comes back. The UI and
the MCP server are built on these routes and nothing else, so anything either
of them can show you, a `curl` can too.

For installing and running the server see the [README](../README.md); for the
YAML the tree behind these routes is written in see the
[YAML reference](yaml-reference.md); for how to read the numbers they return see
[the model and its assumptions](model.md).

The **tree-scoped** routes below also answer at `/trees/{tree_id}/…` when the
process serves [several trees](deploying.md#serving-several-trees); the bare
paths are aliases for the default tree. The process-wide routes have one form
only. A `run_id` is already unique, and the index and the health probe are
about the whole process rather than one tree.

**Tree-scoped** (each also at `/trees/{tree_id}/…`):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/meta` | Metric names, data window, provider type, mode (`fitted` \| `cold_start`), per-metric `grains`/`kinds`/`data_through`, fitted models, per-metric `earliest_available` history discovery (UI bootstrap) |
| `GET` | `/dag` | Full metric DAG (nodes + edges), each node carrying its whole definition. `sql` and `bind` come back `null` to a caller that presents no token when one is configured. See [Authentication](deploying.md#authentication) |
| `GET` | `/series` | Every metric's series at its native grain, `{name: {grain, dates, values}}`. One call hydrates the UI's node cards. Mixed-grain trees have no shared date axis, so dates are per metric |
| `GET` | `/metrics/{name}` | Metric definition, time series, posterior summary and fit diagnostics |
| `GET` | `/metrics/{name}/query` | The query behind a metric's numbers, when the provider knows it. Optional `dimension` for the sliced form |
| `POST` | `/analyze/{name}` | Run Bayesian sampling for a metric |
| `GET` | `/shapley/{name}` | Shapley attribution for a formula metric |
| `POST` | `/rca/{name}` | Root cause analysis over the metric's ancestors |
| `POST` | `/rca/{name}/slices` | Attribute one metric's gap across a declared dimension's values, the traverse-then-slice follow-up |
| `POST` | `/simulate` | Do-operator what-if scenario (fitted posteriors, or declared beliefs on a cold-start tree) |

**Process-wide:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | A one-line "the API is running" banner carrying no tree data. Open even under `BREAKDOWN_REQUIRE_AUTH` |
| `GET` | `/health` | Always 200. `{"status": "ok", provider, metrics}`, or `{"status": "degraded", "error": …}` when the default tree can't serve. Liveness for orchestrators. The body, not the status code, says whether the tree is degraded. Open even under `BREAKDOWN_REQUIRE_AUTH` |
| `GET` | `/trees` | Every tree: title, owner, metric count, `state` (`loaded` \| `not_loaded` \| `loading` \| `error`), plus `period`/`goal` where declared and `progress` for a loaded tree that has a goal. Reads parsed YAML only and never triggers a data load |
| `POST` | `/trees/{id}/load` | Fetch one tree's data now, and return its updated index card |
| `GET` | `/progress/{run_id}` | Live stage of an in-flight RCA or simulation started with that `run_id` |
| `GET` | `/ui` | Interactive DAG visualization |
| — | `/mcp` | [MCP server](mcp.md) for AI assistants (streamable HTTP). Gated by `BREAKDOWN_API_TOKEN` whenever one is set |

## `GET /metrics/{name}/query`

Never ship a number the engine can't defend. Most providers gave a reader no
way to see what was actually asked of the warehouse, which left every number
unfalsifiable by exactly the person being asked to trust it. This route returns
the query behind a metric, so an analyst can check the number against the
definition they think they have.

| Param | Description |
|-------|-------------|
| `dimension` | *(optional)* Show the sliced query for one of the metric's declared `dimensions` instead of the plain one |

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
  to someone else's planner and never see SQL. `note` says which case it is.
  "We never see the query" and "no query is run" are different facts about how
  much a reader can verify, so the response keeps them apart instead of
  flattening both to *unavailable*.
- **`executed`** distinguishes the statement that *ran* from the statement that
  *would* run for the loaded window. A snapshot hit serves the number without
  executing anything; the binding still determines it exactly, so the query is
  real provenance either way. You are told which, rather than left to assume.
  `note` repeats it in words.
- `warehouse` returns the author's own `sql`; `dbt` returns what it generated;
  `SnapshotFetcher` delegates to whichever provider it wraps.
- 404 for an unknown metric, or a `dimension` the metric doesn't declare.

## `POST /analyze/{name}`

Query parameters:

| Param | Default | Description |
|-------|---------|-------------|
| `inference_method` | `nuts` | `nuts` (full MCMC) or `advi` (variational inference, faster but measurably less accurate — see [`docs/model.md`](model.md#assumptions-and-limitations-to-keep-in-mind)). Every route takes you at your word: an `advi` fit is never re-run as NUTS behind your back, and it reports its PSIS k̂ in `diagnostics` so you can see how far off it landed. Same values and same default on `POST /rca/{name}` and `POST /simulate`. |
| `draws` | `500` | Posterior draws, which buy different things per method. Under `nuts` this is draws per chain after `tune` discarded steps, so 500 × 4 chains = 2,000 draws, and more of them tighten the Monte-Carlo error. Under `advi` the optimization is a fixed 20,000 steps regardless. There it only sets how many samples are drawn from the already-fitted approximation, so more is nearly free and does not make the answer more accurate. |
| `tune` | `1000` | Warm-up steps, discarded (NUTS only). This is where NUTS adapts its step size and mass matrix, so it is not a cosmetic tail: a fit warmed up differently is a different posterior. The default is the engine's single budget, shared with `POST /rca/{name}` and `POST /simulate` — through to v0.1.1 this route declared `500` while the analysis routes warmed up for `1000`, so the same node over the same window answered differently depending on which route you asked through. |
| `chains` | `4` | Number of NUTS chains (NUTS only) |
| `fit_end` | none | Exclusive date cutoff (`YYYY-MM-DD`). The fit uses only rows before it. Defaults to the full window; pass the analysis-window start to reproduce what RCA fits. |

The fit is **seeded**, like the ones `POST /rca/{name}` and `POST /simulate`
run: the same request twice returns the same posterior and the same
diagnostics. Through v0.1.1 this route passed no seed while both analysis
routes did, so an `advi` fit here reported a different PSIS k̂ each time it
was asked (1.23, then 1.91, on one demo node).

```bash
# Full MCMC — the default
curl -X POST "http://localhost:9090/analyze/order_count?draws=1000"

# Fast variational inference, when exact sampling is impractical.
# Read the k̂ it reports before quoting anything it produced.
curl -X POST "http://localhost:9090/analyze/order_count?inference_method=advi"
```

The response's `diagnostics` block carries the engine's verdict on the fit.
NUTS reports `max_rhat`, `divergences` and `min_ess_bulk`; a variational fit
reports `elbo_drop` (did the optimizer settle?) and **`khat` / `khat_status`**
(did it settle anywhere near the posterior?) — the PSIS diagnostic of Yao et
al. (2018). `khat_status` is one of `ok` (k̂ ≤ 0.5), `suspect` (≤ 0.7),
`unusable` (> 0.7 — the intervals are not evidence about how wide the real
ones are) or `unavailable` (the check could not run — an unchecked fit, not a
clean one). Anything other than `ok` also carries `khat_warnings`, a list of
self-contained sentences. A NUTS fit has no `khat` at all, and that absence is
not a missing check: NUTS samples the posterior rather than approximating it.

k̂ is itself estimated from a finite sample, so it travels with its own error:
**`khat_se`** is its Monte-Carlo standard error (null where the estimate has
one but its error is not computable — never zero, which would claim exactness),
and **`khat_borderline`** is `true` when k̂ is within one `khat_se` of a band
edge. `khat_status` still names the band the point estimate falls in; the flag
is what says that band is not resolved, and a `true` there means read the worse
of the two adjacent bands. `fit_quality` stays the two-valued gate
(`ok` | `suspect`) and goes `suspect` when either check fails — including on a
borderline `ok` k̂, which has not shown the approximation to be close. Full
interpretation in
[`docs/model.md`](model.md#assumptions-and-limitations-to-keep-in-mind).

Whichever sampler ran, every fit also reports **`ppc_status`** — `ok`,
`moderate`, `severe` or `unavailable` — plus `ppc` (the evidence: `n_draws` and
every `statistics` entry with its `statistic`, two-sided `p_value`, `observed`,
`replicated_mean` and band) and, when anything was flagged, `ppc_warnings`. It
is a posterior predictive check: series are simulated from the fitted posterior
and compared with the fitted series on `min`, `max`, `resid_max` and
`resid_acf1`. Unlike the collinearity fields below, **`severe` does move
`fit_quality` to `suspect`** — and on a NUTS fit it is the only thing that can,
so `suspect` there means the model is wrong for the data rather than that the
sampler struggled. See [the model guide](model.md#can-the-model-generate-its-own-data).

Whichever sampler ran, a fit with two or more parents also reports
**`collinearity_status`** — `ok`, `moderate`, `high` or `unavailable` — plus
`collinearity` (the evidence: `max_abs_correlation`, the flagged `pairs` with
their correlations, and the flagged `vif` entries) and, when anything was
flagged, `collinearity_warnings`. It measures whether the parents move
together over the window the model trained on, i.e. whether the per-parent
*split* of this node's gap is a quantity the data determines at all. It does
**not** move `fit_quality`: a collinear fit is a correct fit that is properly
unsure about the split. A node with fewer than two parents carries none of
these fields, because there is no split to be unstable. See
[`docs/model.md`](model.md#parents-that-move-together).

## `GET /shapley/{name}`

Returns how much of the target metric's gap between two time windows is attributable to each parent. Requires a `formula` on the metric definition.

Query parameters:

| Param | Description |
|-------|-------------|
| `analysis_start` | Start of the analysis window (`YYYY-MM-DD`) |
| `analysis_end` | End of the analysis window (`YYYY-MM-DD`) |
| `reference_start` | *(optional)* Start of the baseline window (`YYYY-MM-DD`) |
| `reference_end` | *(optional)* End of the baseline window (`YYYY-MM-DD`) |

Omit both reference dates (passing exactly one is a 422) and the engine
defaults to the matched adjacent block: the window ending the day before
`analysis_start`, 4× the analysis length (min 28 days, whole weeks when
seasonality is in the target's scope), clamped to the loaded data. The
response echoes the resolved `reference_window`/`analysis_window` and sets
`reference_defaulted`. The reference is only the comparison baseline; the
model always fits on all loaded history before `analysis_start`. See
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

`baseline` and `actual` are each the mean of the formula evaluated period by period, at the target's grain, over the reference and analysis windows respectively, so both windows' within-window co-movement of the parents is included. `gap = actual − baseline`. Each `attribution` value is the sum of three exact Shapley games, reported per parent in `decomposition`: `attribution = means + covariance_analysis − covariance_reference` (the window-means bridge plus the parent's share of each window's within-window co-movement term). The attributions sum to `gap` exactly. Windows are snapped to whole periods at the target's grain (`effective_windows`); a window with no whole period is a 422.

## `POST /rca/{name}`

Walks the ancestor DAG of `name` and attributes the change between a reference window and an analysis window to upstream metrics. Any probabilistic node in scope that hasn't been fit yet is fit on demand and its trace is cached (a second call is much faster). Those fits use **NUTS** unless you pass `?inference_method=advi`, and a cached fit is reused only when it is at least as good as the one your request would produce — a NUTS fit answers an `advi` request, but an approximation cached by someone else's triage run does not silently answer yours.

Expect the first call on a cold cache to take a minute or more per learned node. That is the trade the default makes: mean-field ADVI fails its PSIS check on essentially every real node in this engine and moves point estimates by tens of percent, so exact sampling is what the numbers are worth. `?inference_method=advi` is there for a tree wide or fine-grained enough that NUTS is genuinely impractical; every node it fits then carries its k̂ and the warning that goes with it.

Query parameters (`YYYY-MM-DD`): `analysis_start` and `analysis_end` are
required; `reference_start` and `reference_end` are optional. Omitting both
uses the matched adjacent block, exactly as on `GET /shapley/{name}` above,
and the response carries `reference_defaulted`. `inference_method` (`nuts` |
`advi`, default `nuts`) picks the sampler for any node this call has to fit;
`run_id` opts into progress polling.

```bash
# explicit reference
curl -X POST "http://localhost:9090/rca/revenue?reference_start=2024-01-01&reference_end=2024-02-15&analysis_start=2024-02-16&analysis_end=2024-04-09"
# defaulted reference
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-02-16&analysis_end=2024-04-09"
# triage speed on a wide tree, with k̂ reported per node
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-02-16&analysis_end=2024-04-09&inference_method=advi"
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
      "unexplained": 12.0, "unexplained_status": "measured",
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
      "inference_method": "nuts",
      "fit_quality": "ok",
      "khat": null, "khat_se": null, "khat_status": null,
      "khat_borderline": null, "khat_warnings": null,
      "ci_status": "ok",
      "unexplained": 1.4, "unexplained_status": "measured",
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

Every fitted (`attribution_method: "posterior"`) node also reports which model produced its numbers and how much that model can be trusted: `inference_method`, `fit_quality` (`ok` | `suspect`), and the PSIS fields `khat` / `khat_se` / `khat_status` / `khat_borderline` / `khat_warnings` described under [`POST /analyze/{name}`](#post-analyzename). On the NUTS default they are `null` throughout. Where they are not — because the request asked for `advi` — read `khat_status` before quoting a `ci_95` from that node: `unusable` means the interval is not a measurement of the real one, and `khat_borderline: true` means the check could not separate that verdict from the next band along.

Every fitted node also carries `ppc_status` / `ppc` / `ppc_warnings`, described under [`POST /analyze/{name}`](#post-analyzename). On `severe`, that node's model does not reproduce the series it was fitted on, so its contributions and `share_of_gap` are conditional on a likelihood the data argues against — read the direction, not the magnitude. `severe` also sets that node's `fit_quality` to `suspect`.

A fitted node with two or more parents also carries `collinearity_status` / `collinearity` / `collinearity_warnings`, described under [`POST /analyze/{name}`](#post-analyzename). On `moderate` or `high`, the node's per-parent `contributions` are a split the data does not fully determine: read the flagged parents as one cause and do not rank them against each other. The fields are absent (null) on nodes with fewer than two parents.

Grain support adds two per-node fields: `grain` (the grain the node was analyzed at) and `effective_windows` (the whole periods the requested windows snapped to at that grain). Gaps are mean-per-period at each node's own grain, so in mixed-grain trees compare nodes via `share_of_gap` and `ranked_causes` scores, not raw gaps.

### Per-node `status` — one bad node does not end the analysis

Every node in scope carries a `status`. Anything other than `"ok"` means the
node is reported without attribution while the rest of the tree comes back
normally, with the reason in `status_reason` (`null` when the status is `"ok"`).
Read `status` first and branch on it; every other key is always present, so a
skipped node has the same shape as an attributed one.

| `status` | What it means to you |
|---|---|
| `ok` | Attributed normally. |
| `window_shorter_than_grain` | Your windows hold no whole period at this node's grain, e.g. a 3-day window on a monthly node. `status_reason` names the grain and the windows. Nothing is wrong with the data; widen the window, or accept that this node can't speak to a change this short. When the **target** itself has no whole period, the request is a 422 before any fitting, because with no measured movement on the target there is nothing to attribute anywhere. The error names the grain and the most recent whole period the data holds. Through a parser-built tree that is the only way this case can arise (the target is always the coarsest node in its own scope), so on a served tree you will meet the 422, not the status. |
| `fit_failed` | The node's own model could not be fitted. Overwhelmingly this is a series with no variance across the fit window: a parent held at zero the whole time, which for a seasonal business is simply its off-season. A constant series cannot be normalized, so there is no coefficient to attribute with. |
| `attribution_failed` | A formula node whose exact decomposition is not a finite number over these windows, in practice a zero denominator somewhere in the window. The node's own `baseline`, `actual` and `gap` are read off the data, not the model, so they are real and are still reported. Only the split across parents is missing. |

`fit_failed` and `attribution_failed` exist because the alternative was worse.
One unfittable node used to abort the entire tree analysis and return nothing
at all. `status_reason` carries the engine's own diagnostic. For
`attribution_failed` it names the offending parent series, the window, and the
dates that are zero, so you can narrow the window past them or fix the series at
the source.

The RCA target itself is the exception. The whole response is about that
node, so a failure there is raised as a 422 carrying the same diagnostic rather
than buried in a status nobody would find useful.

### `ci_status`

`ci_status` reports the health of a node's credible intervals, independently of
`status`:

| `ci_status` | Meaning |
|---|---|
| `ok` | Intervals computed normally. |
| `degenerate_single_period` | A formula node whose windows snapped to one period. The block bootstrap would return identical replicates, so intervals are withheld rather than reported at a falsely-zero width. |
| `posterior_only_single_period` | The same for a posterior node. Coefficient uncertainty remains, but the window-sampling component is absent, so the interval is narrower than the truth. |
| `nonfinite_bootstrap_replicates` | At least one interval on this node was computed from a subset of the bootstrap replicates, or withheld because too few survived. A resampled denominator can land on ~0 even when no single period is zero. The point estimates are unaffected, because they are the exact Shapley values, never bootstrap means. Only the intervals lost resolution. |

**Two-level attribution (formula nodes).** Each formula-node contribution *usually* carries a `decomposition`, `{"means": {estimate, ci_95}, "comovement": {estimate, ci_95}}` with `means + comovement = estimate` exactly per bootstrap replicate, and the node carries an `interaction` summary (the summed co-movement shift across parents, with its own CI). The UI's default **Headline** view is the classic price/volume/mix bridge built from these: one row per parent showing its means-bridge contribution, plus one explicit *co-movement shift* row, plus unexplained. The rows total to the gap. The **Detailed** toggle expands each parent to its full split. The interaction is shown as its own labeled row rather than silently folded into the factors; for products it is exactly the parents' covariance delta, for other formulas the full within-window co-movement/Jensen shift.

The exception is a `kind: rate` node whose `formula` is `num / den` over its own declared `denominator`. Its window value *is* the formula of the window aggregates (`Σnum / Σden = mean(num) / mean(den)`), so the decomposition is the window-means bridge and nothing else: `aggregation: "components"` on `GET /shapley`, and both the per-contribution `decomposition` and the node's `interaction` are absent rather than reported as zero. A term the decomposition does not contain must not be published as a term measured to be zero.

## Root cause analysis

`POST /rca/{name}` combines the two attribution methods across a metric tree:

- **Formula nodes** get `attribution_method: "shapley"`, exact symmetric per-day Shapley values (a window-means bridge plus each parent's share of the within-window co-movement term of each window, analysis added and reference subtracted), so shifts in the parents' within-window co-movement are attributed to parents. `unexplained` is only the target's own measurement noise around the formula; for an exact identity it is zero.
- **`unexplained_status`** says what that number is, because `0` means two opposite things. `"measured"` means the node has its own `source`, it was fetched, and the decomposition was compared against it; zero means it reconciled. `"definitional"` means the node is [derived](yaml-reference.md#kinds) (no `source`, so its series *is* the formula); zero means nothing was checked. `null` when no attribution ran. The UI labels the second case *unexplained — none by definition* and the exported report spells it out; never read a definitional zero as a passed identity check.
- **A node whose window value does not exist** reports `status: "undefined_over_window"` with `baseline`/`actual`/`gap` all `null` when every period of one of its windows was undefined (a rate whose denominator is zero has no rate). A window merely *containing* undefined periods is fine, because rates aggregate as `Σnumerator / Σdenominator`, so those periods drop out of both sums.
- **A rate's `baseline`/`actual`** are `Σnumerator / Σdenominator` over the window whenever the metric declares a [`denominator`](yaml-reference.md#kinds), not the average of its per-period ratios. A rate that declares none falls back to that average over its defined periods, and the startup log names every such node.
- **`window_aggregate`** says which of those two you are looking at, on every `kind: rate` node (`null` on a flow or a stock, which have one aggregation and it is not in question). `"components"` is the real `Σnumerator / Σdenominator`. The `period_mean_*` values are all the same arithmetic, the mean of the defined per-period ratios, and differ in *why*, which is the part that changes what you should do: `"period_mean_none_exists"` (the metric declares [`no_denominator`](yaml-reference.md#grains), so no pair of series makes this quantity, e.g. a median; this is the only number there is and nothing is missing), `"period_mean_undeclared"` (nobody has said what the rate is a rate of; declaring one gets you the real aggregate), `"period_mean_weights_unavailable"` (declared, but its series does not cover these windows). `window_aggregate_reason` carries the author's own words for the first and an explanation for the others; it is `null` for `"components"`.
- **Probabilistic nodes** get `attribution_method: "posterior"`. Each contribution is the posterior over the parent's raw-scale coefficient (`beta_raw`) times the parent's window-over-window change. Lagged parents are compared over windows shifted back by the lag, and each lagged contribution reports `lag` and `parent_windows`, the parent's own shifted `{reference, analysis}` windows, so you can see (and reuse, e.g. for `POST /rca/{parent}/slices`) exactly which parent periods were examined. These nodes also report a `components` block: the fitted model's own trend and seasonal terms as window-over-window deltas with CIs, so they no longer hide inside `unexplained`. `components` carries only the terms the model actually contains. Every fit has a local level, so `trend` is always there, but a node that declares no [`seasonality`](yaml-reference.md#seasonality) has no `seasonal` key at all rather than a 0.0 with a zero-width interval.

Every contribution is reported as an `estimate` (mean), a 95% interval (`ci_95`), and `prob_same_direction` (mass on the dominant side of zero). The intervals combine coefficient uncertainty (probabilistic nodes) with window-sampling uncertainty: the window means themselves are resampled with a circular moving-block bootstrap (≤7-day blocks, jointly across metrics, seeded so responses are deterministic). This is what keeps a 3-day analysis window honest. Its CIs are visibly wider than a 4-week window's.

`prob_same_direction` is a proportion over those 500 replicates, so it is never published as 1.0. There is no representable value between 0.998 and 1, and a saturated count is the estimator running out of resolution, not a measurement of certainty. A saturated estimate publishes the ceiling alongside `prob_same_direction_censored: true`, and the UI and the exported report both render it as the bound it is: `>99.8%`. `prob_concentrated` (slices) and `prob_direction` (what-if) follow the same rule with their own sample sizes. Read it as "no replicate crossed zero", not as "the sign is certain".

Unfitted probabilistic nodes in scope are fitted on demand, on data strictly before the analysis window, and cached, so the endpoint works without a prior `/analyze` call — with the same `inference_method` parameter, the same NUTS default and the same reuse rule described above.

`ranked_causes` is a documented heuristic. It propagates an influence score from the target up the ancestor tree, weighting each hop by the parent's `|share_of_gap|` (capped at 1) divided by the child's total gross parent movement, meaning the sum of every parent's `|share_of_gap|`, floored at 1 so a decomposition that sums tidily is never penalized. That divisor is what stops a parent scoring full marks on a gap its siblings cancelled: two parents at +165% and −62% both rank *below* a lone parent cleanly explaining 80%. Each row carries `via`, the child it was reached through, so a score can be traced back to the hop that produced it. A node no hop ever reached is omitted rather than listed at zero; `nodes` remains the full inventory of what was in scope. Use it as a triage ordering, not as a probability.

See [model.md](model.md) for how to read `components`, `unexplained`, and the bootstrap's assumptions.

## `GET /progress/{run_id}`

RCA and simulation can spend a minute or more fitting ancestor models. Pass any
opaque `run_id` you like to `POST /rca/{name}` or `POST /simulate` and poll this
endpoint while the request is in flight to see what the engine is doing:

```bash
curl -X POST "http://localhost:9090/rca/revenue?analysis_start=2024-04-03&analysis_end=2024-04-09&run_id=abc123" &
curl -s "http://localhost:9090/progress/abc123"
# {"stage":"fitting","metric":"order_count","current":1,"total":3}
```

Stages are `waiting` (queued behind another analysis), `resolving`, `fitting`
(with `metric`, `current`, `total`), then `attributing` or `simulating`. An
unknown or finished id returns `{"stage": null}` with a 200. To a poller a
finished run and a never-started one are the same answer.

Progress is optional. Omit `run_id` and nothing is tracked, which is the
default for every non-UI caller. It never affects the analysis or its result.

## `POST /rca/{name}/slices`

The traverse-then-slice follow-up: attribute one metric's window-over-window gap across a declared dimension's values. The reference dates are optional here too (same defaulting rule; the response carries `reference_defaulted`). When slicing a lagged parent surfaced by an RCA, though, pass its `parent_windows` explicitly, because the default matches the metric's own timeline, not a lag-shifted one.

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
  "localization": "localized", "localized": true, "localization_threshold": 0.25,
  "ci_status": "ok", "caveats": []
}
```

- `contribution` is the slice's own window-mean change; contributions sum exactly to the sliced gap (flows/stocks are sum identities over slices).
- `excess = contribution − baseline_share × gap` is the localization signal: how much more of the gap the slice carries than its size predicts. Excesses sum to zero, because concentration is a reallocation of the gap. `prob_concentrated` is the bootstrap probability the excess direction is real; `noise_level: true` rows should not be narrated as localized.
- Rate metrics return `attribution_method: "slice_blend"`: each slice splits into `within` (its own rate moved) and `mix` (traffic shifted between slices), summing exactly to the blended gap, with the total composition effect in `mix_total`.
- `reconciliation` compares the slices' sum (or weighted blend) against the metric's own series. `"discrepant"` means the dimension doesn't cleanly partition the metric; attributions are then approximate, and say so.
- **`localization`** is the headline verdict, in three states, and the UI, MCP and the test suite all read this one field rather than re-deriving the rule:
  - `"localized"` — the leading slice's excess reaches `localization_threshold` of the gap with its evidence intact (not `noise_level`, shares not withheld) **and** it is a real value of the dimension. The UI's "*X carries N% of the gap*" line renders exactly this state.
  - `"not_localized"` — no slice carries enough of the gap beyond its own size. Narrate the gap as spread across slices rather than naming the top slice, which exists only because ranking always produces a first row.
  - `"long_tail"` — the leader clears the threshold but *is* `__other__`, the roll-up of the values outside `top_k`. The tail genuinely moved, so this is not "not localized"; but `__other__` is the set of values nobody enumerated, so it is not a segment to go and act on either. Say the gap is concentrated in the long tail, and hand back **`localization_remedy`** — one sentence naming what to change to see inside the roll-up (raise this dimension's `top_k`, pin `values:`, or slice another dimension), present only in this state and `null` otherwise. Never narrate it as "`__other__` is the cause".
- **`localized`** (boolean) is the same verdict in its older two-state form, kept for consumers written against it: it answers only *may I print "X carries N% of the gap"?*, so it is `true` exactly when `localization == "localized"` and `false` under both other states. A reader that knows only the boolean therefore stays restrained instead of naming the roll-up.
- **`additivity`** (`"exact"` | `"overlapping"` | `"unknown"`) comes from the binding, never from the residual. `"overlapping"` means an entity may hold several values of this dimension inside a period, so the slices overstate the metric by the amount in `overlap`. That is arithmetic, not a defect. Per-slice `share_of_gap` is then `null` (they would be shares of a total the slices don't sum to), and `reconciliation.status` is `"not_applicable"` rather than `"discrepant"`, which keeps `discrepant` meaning *unexplained* divergence.
- **`entity_flows`** (when the provider can classify entities; otherwise `null`) sits beside the attribution, never inside it: `totals` of new / churned / retained / migrated entities across the two windows, the top `migrations` with `migrations_total`/`migrations_truncated`, and `reconciles_to_gap: false`, because window-level sets do not reconcile to a window-mean gap. A migration nets to zero across slices; naive slicing reads the same event as two large offsetting causes.
- When slicing a lagged parent surfaced by an RCA, pass the parent's lag-shifted windows. Its RCA contribution carries them as `parent_windows`; those are the periods that influenced the child.

Sliced series are fetched from the provider on demand for just these windows and cached per (metric, dimension, window); nothing about slicing touches the startup data or the fits.

**Both windows must lie inside the loaded data window.** Because slicing reads
from the provider for whatever window you ask for, an out-of-range request is a
422 naming the loaded window, checked before any provider call. Previously
nothing bounded these dates beyond "they parse", so a typo could ask a
warehouse for a 200-year scan, hold the tree's lock for the duration, and only
then fail for having no data in it. If you need a window outside what is
loaded, restart with a wider `--start-date`/`--end-date` for that tree.

## `POST /simulate`

Do-operator what-if: intervene on one or more metrics, propagate the change
through the downstream subgraph per posterior draw, and report the steady-state
effect with credible intervals. The scenario is a JSON body; the query
parameters are `inference_method` (`nuts` | `advi`, default `nuts` — the
sampler for any node this scenario has to fit, with the same meaning and the
same reuse rule as on [`POST /rca/{name}`](#post-rcaname)) and the optional
`run_id`.

| Body field | Type | Description |
|---|---|---|
| `baseline_start` | date | Start of the window defining "current normal". Required on a tree with data; rejected on a [cold-start tree](yaml-reference.md#cold-start-mode-what-if-with-no-data), where operating points come from each node's declared `baseline` instead |
| `baseline_end` | date | End of that window. Same rule |
| `interventions` | list | `{metric, mode, value}`. `mode` is `set` (absolute level), `delta` (absolute change), or `pct` (fractional change, `0.1` = +10%). One intervention per metric |
| `assumptions` | list | `{source, target, effect: {kind, low, high}, id?, note?}`. A user-asserted effect on an edge the tree doesn't encode. `kind` is `relative` (scaled by the target's baseline) or `absolute` (the target's business units); `low`/`high` are read as the central 90% interval of a Normal |
| `levers` | list | `{name, value?, unit?}`. Display metadata only; levers have no dynamics of their own in v1 |

A scenario needs at least one intervention or assumption, and at most 10 of
the two combined. The source decomposition enumerates coalitions exactly as
[formula attribution](yaml-reference.md#formula) does, and is capped for the same reason.

`baseline_start`/`baseline_end` are the window the simulation measures *from*:
each node's operating point is its mean over that window, at the node's own
grain. It is not a fit window. Coefficients come from posteriors fitted on all
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
(`status`, one of `baseline` | `affected` | `intervened`, plus `baseline`,
`simulated`, `delta` with `ci_95`, `relative_delta`, `prob_direction`,
`fit_quality`, `khat_status`, `khat_borderline`, `khat_warnings` — the last
three null on the NUTS default — `collinearity_status`,
`collinearity_warnings` (the S4 verdict on the fit behind this node's slope;
null where the node has fewer than two parents), `ppc_status`, `ppc_warnings`
(the S3 verdict on whether that fit's model reproduces its own history; a
`severe` node's scenario magnitude rests on a model the data argues against),
`extrapolation`,
`non_physical`, `contributions`), plus
`warnings` and always-on `caveats`. The run is seeded, so identical calls are
byte-identical.

The two honesty flags are different claims and both travel per node, with the
sentence for each in `warnings`. **`extrapolation`** is empirical: the value is
outside the loaded history (or, in cold start, outside the declared `plausible`
band). **`non_physical`** says the value cannot exist — either the tree
declares a bound it breaks ([`share: true`](yaml-reference.md#grains)
on a `kind: rate` node bounds it to `[0, 1]`, in both directions and with or
without history), or the metric has never been negative and this scenario made
it so. A node can carry both; a node carrying only `non_physical` is one whose
number should not be quoted at all.

On a fitted tree, every `kind: rate` node also carries `window_aggregate` and
`window_aggregate_reason`, the same labels an RCA response puts beside a
rate's `baseline`/`actual` ([above](#root-cause-analysis)),
because the baseline here is the same window arithmetic and a `period_mean_*`
fallback should be read the same way wherever it appears. A scenario whose
arithmetic produces a non-finite number anywhere (a zero denominator inside a
formula over the baseline window) is refused with a 422 naming the nodes
rather than encoded. There is no partial result worth keeping, because deltas
propagate. On a cold-start tree the same policy fires one step earlier. A
formula that divides by a belief whose draws cross zero is refused with the
divisor and the remedies named (a `plausible` floor above zero, or a
`LogNormal` baseline), because the ratio's Monte-Carlo mean would not exist
and its centre would be an artifact of the seed.

Pass an optional `run_id` query parameter to follow a long simulation with
[`GET /progress/{run_id}`](#get-progressrun_id), exactly as for
RCA.

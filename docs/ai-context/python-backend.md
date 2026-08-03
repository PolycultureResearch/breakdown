# Backend Architecture: Bayesian Engine

The `breakdown` backend uses PyMC to perform Bayesian inference on metric relationships defined in a YAML metric tree.

For the statistical assumptions and how to interpret results, see `docs/model.md`.

---

## Module map

```
breakdown/
  parser.py        # YAML → Pydantic models → NetworkX DAG (typed nodes) + cross-node grain rules
  formula.py       # Shared formula AST validation + safe eval (used by parser, engine, data_fetch)
  grains.py        # ALL grain arithmetic: period floors/snapping/steps, kind-aware
                   # resample_up, GrainedData (per-grain frames), BOOT_BLOCK
  data_fetch.py    # BaseDataFetcher + Mock / Local / Cloud / Warehouse implementations
  engine/
    model.py       # fit_metric() — BSTS via PyMC; compute_shapley(); summarize_trace()
    rca.py         # run_rca() + shapley_attribution() — all window-over-window attribution
    simulate.py    # run_scenario() — do-operator what-if; fitted (posterior draws) or cold start (data=None)
  api/
    main.py        # FastAPI app — routes, lifespan, state (owns the trace cache)
  mcp/
    server.py      # MCP server — 4 tools over the same engine/state (mounted at /mcp)
    shaping.py     # MCP response compaction, how_to_read caveats, UI deep links
  cli.py           # Console entry point (`breakdown serve` / `breakdown doctor`)
  doctor.py        # Provider connectivity checks — reuses the real fetchers
  snapshots.py     # Parquet read-through cache at the fetcher boundary (roadmap 2.4)
  static/          # UI files (inside the package so the wheel ships them)
  examples/        # Bundled default tree (jaffle_shop_tree.yml)
```

Design rules:

- The engine is **stateless**: `fit_metric` is a pure function (DAG + data + target → trace). The only trace cache lives in `app.state.traces`; `run_rca` receives it as an argument and adds on-demand fits to it in place.
- All attribution (window means, Shapley over windows, posterior attribution) lives in `engine/rca.py`. `engine/model.py` keeps only the pure Shapley enumeration.
- **Parent order is load-bearing:** everywhere, parents come from `list(dag.predecessors(name))`; this is the axis order of `beta`/`beta_raw`. Any new component must use the same call.
- **Grain logic lives only in `grains.py`.** Every node is fetched/fitted/attributed at its own declared grain (`fit_grain(dag, node)` == the node's grain, guaranteed by parse-time rules); engine entry points accept `Union[pd.DataFrame, GrainedData]` via `ensure_grained()` — a plain wide daily frame wraps as all-day flow metrics, which keeps tests and legacy call sites working unchanged.

---

## `parser.py`

**`MetricDefinition`** — Pydantic model for one metric node. Fields: `name`, `source`, `grain` (`day`|`week`|`month`, default day), `kind` (`flow`|`stock`|`rate`, default flow), `description`, `sql` (warehouse provider only), `parents`, `formula`, `priors: Dict[str, Prior]`, `lags: Dict[str, int]` (grain steps at the node's grain), `seasonality: List[Seasonality]` (periods ≥ 2, in grain steps), `trend: Optional[TrendConfig]` (local-level random-walk step-size prior), `baseline: Optional[AssertedBaseline]` (cold-start asserted operating point — `{low, high}` read as a central-90% Normal interval, numeric shorthand coerced to a degenerate point; units are mean per native-grain period, i.e. what a fitted `window_mean` baseline would be), `plausible: Optional[PlausibleRange]` (cold-start honesty band — optional `min`/`max`, at least one required, stands in for historical min/max in what-if extrapolation flags), `format: Optional[MetricFormat]` (UI display hint — presentation only, coerced from the `format: currency` string shorthand), `direction` (`up_is_good`|`down_is_good`|`neutral`, UI goodness coloring only — never touches modeling). Validators enforce: formula is arithmetic-only AST and references only parents; prior keys are `"coefficient"` or a parent name; lag keys are parents, values are ints ≥ 1 (with `formula`, lags declare a cohort-aligned lagged identity — `A[t] = f(parents shifted back by their lags)`); `expected_signs` keys are parents with values `positive`/`negative` and are rejected on formula nodes (no learned coefficients to check); `baseline` is rejected on formula nodes (theirs derive per-draw from parents so the identity holds — an asserted one could contradict it); classic day-grain seasonality periods (7/30/365) on a non-day node warn.

**Cross-node grain rules** (`Parser._validate_grains`, needs both edge endpoints so it runs after the DAG is built): a parent may never be coarser than its child (downward disaggregation undefined); a finer parent must be an auto-aggregatable `flow`/`stock` whose grain **nests** in the child's (days tile weeks/months; weeks straddle month boundaries, so week-under-month is rejected); finer `rate` parents are rejected (declare the rate at the child's grain).

**`Parser`** — wraps `MetricTreeConfig` + a `networkx.DiGraph`.
- `parser.dag` — the compiled DAG. **Each node stores its validated model under the `definition` key**: `dag.nodes[name]["definition"]` is a `MetricDefinition` (attribute access, not dict `.get`). This is the single source of truth downstream.
- `parser.get_metric(name)` — O(1) lookup via the DAG.
- `parser.get_topological_order()` — nodes in dependency order.

---

## `data_fetch.py`

### `BaseDataFetcher` (ABC)

All fetchers implement:
```python
def fetch_metric(self, metric_name: str, start_date: str, end_date: str,
                 grain: str = "day", kind: str = "flow") -> pd.DataFrame
```
Returns a DataFrame with columns `["date", metric_name]`, sorted by date, no NaNs, with **period-start** date labels at the requested grain (day midnight, week Monday, month 1st). `kind` drives gap-filling where the provider reindexes onto a period spine: flow → 0, stock → forward-fill (leading gap errors), rate → missing period errors.

### `MockDataFetcher`
Constructed with an optional metric DAG (`MockDataFetcher(dag=parser.dag)`). With a DAG, series are generated in topological order **at each node's declared grain** so they respect the tree: formula nodes satisfy their formula against parents aggregated to the node's grain plus ~2% noise, probabilistic nodes are a coefficient-weighted sum of aligned parents (coefficient from the `coefficient` prior's `mu` when available; lag-shifted parents when `lags` is set) plus ~5% noise, and roots are random walks with weekly seasonality on their native period spine. Finer rate parents resample by per-period mean (mock-only convenience). Without a DAG (or for names not in it), falls back to an independent random walk at the requested grain. Seeded per metric name — deterministic across calls; all-day trees are byte-identical to the pre-grain generator (pinned by golden tests). Per-metric series per window are cached.

### `WarehouseDataFetcher`
Runs each metric's own `sql` against Databricks SQL. The SQL owns the aggregation to the declared grain (one row per period, period-start labels — misaligned labels error); the engine reindexes onto the spine of whole periods inside the window, drops partial edge periods, fills **interior** gaps by kind, and **trims trailing** gaps (periods after the last returned row are not-yet-loaded data, not zeros — except when the query returns no rows at all, which keeps the full zero spine for flows).

### `LocalDataFetcher`
Invokes `mf query --metrics <name> --group-by metric_time__<grain> --start-time ... --end-time ... --csv <tmpfile>` as a subprocess. `project_path` becomes the working directory. Raises `RuntimeError` on non-zero exit code or OS errors (e.g., path not found).

### `CloudDataFetcher`
Uses the `dbtsl.SemanticLayerClient` sync API; the Arrow result is converted to pandas and the `metric_time__<grain>` column renamed to `date`.

The correlated jaffle-shop dataset used by tests lives in `tests/synthetic.py` (`generate_mock_data`), not in production code.

### `snapshots.py` — `SnapshotStore` + `SnapshotFetcher`

A read-through cache **at the `BaseDataFetcher` boundary**: `SnapshotFetcher` wraps the real fetcher; a hit returns the stored frame without touching the provider, a miss fetches, writes, and returns. One parquet file per `(metric, grain, kind, window)` plus a human-facing `manifest.json` (provider class, fetched_at, rows). Wiring lives in `api/main.py:_wrap_snapshots`, called in `lifespan` after `_build_fetcher`: mock is never wrapped; directory = `BREAKDOWN_SNAPSHOT_DIR` (`"off"` disables) or tree-adjacent `.breakdown/snapshots`; `BREAKDOWN_REFRESH=1` skips reads but still writes (one forced refetch pass). Failure-soft by design: an unwritable directory logs one warning and serves uncached (`/config` is read-only in the container, so `compose.yaml` mounts `./snapshots` and points `BREAKDOWN_SNAPSHOT_DIR` at it). Snapshots capture the **normalized** post-gap-fill frame, so what refits is byte-identical to what was originally served — and a tree whose metrics all have snapshots boots with the warehouse down. The doctor deliberately bypasses snapshots (it constructs raw fetchers) — its job is proving the provider path.

---

## `engine/model.py`

### `fit_metric(dag, data, target, draws=1000, tune=1000, inference_method="nuts", fit_end=None, chains=4, random_seed=None) -> FitResult`

The single fitting entry point (stateless). `data` may be a `GrainedData` or a plain daily frame; the frame actually fitted is `ensure_grained(data).fit_frame(target, parents, grain)` — the target native at its own grain, finer flow/stock parents resampled up, aligned on whole periods — so `t`, lags, and seasonality periods are grain steps. `fit_end` keeps only whole periods that *end* on/before the cutoff (≡ `date < fit_end` for day grain). In normalized space the model is

```
y[t] = alpha + trend[t] + seasonal[t] + (X @ beta)[t] + eps[t]
trend = cumsum(HalfNormal(trend.sigma) * z), non-centered;  seasonal = Fourier pairs (2 harmonics per entry)
```

Internals are three helpers, each documented in-code:
- `_prepare_series(defn, parents, data, target)` → `(y, X, scale, y_mean, y_std, x_stds, dates)`. Formula nodes fit the z-scored residual (X is None); probabilistic nodes get one z-scored regressor per parent, lag-shifted and trimmed by `max(lags)` (raises if < 10 rows remain); `scale[i] = x_std_i / y_std`.
- `_seasonal_component(seasonality, t)` — Fourier terms. Unidentifiable periods (`len(y) < 2·period`) land in `diagnostics["seasonality_warnings"]`.
- After fitting, `expected_signs` declarations are checked against the `beta_raw` posterior: < 10% mass on the declared side → `diagnostics["sign_warnings"]` (passed through per-node in RCA responses and shown in the UI). Not a constraint — a diagnostic.
- `_regression_component(defn, parents, X, scale)` — one `beta_{parent}` RV per parent (parent-specific prior → shared `coefficient` prior → `Normal(0, 1)`), stacked into `beta = Deterministic(...)` plus `beta_raw = beta / scale` (business units). Priors are stated in business units and rescaled via `scale_prior_params(distribution, params, scale)`; unknown distributions raise.

Inference: `nuts` → `pm.sample(draws, tune, target_accept=0.9, chains=chains)`; `advi` → `pm.fit(n=20_000).sample(draws)`. Returns a `FitResult` (trace + normalization constants + fitted period-start `dates` + `grain` + `fit_end` + diagnostics).

### `compute_shapley(formula, parent_names, baselines, actuals) -> Dict[str, float]`

Pure Shapley enumeration (O(2ⁿ)): distributes `formula(actuals) − formula(baselines)` across parents; values sum to the gap exactly.

### `summarize_trace(trace) -> pd.DataFrame`

`az.summary(trace, hdi_prob=0.95)`.

---

## `engine/rca.py`

All window-over-window attribution lives here.

### `shapley_attribution(dag, data, target, reference_start, reference_end, analysis_start, analysis_end)`

Symmetric per-period Shapley decomposition for a formula metric at the target's grain: each parent's attribution is `means + covariance_analysis − covariance_reference` (three exact games; both windows evaluated period-by-period), so `attribution` sums to `gap = actual − baseline` exactly and the per-parent parts are returned under `decomposition`. Windows snap to whole periods (`grain` + `effective_windows` in the response); a window with no whole period raises `ValueError`. This is the `GET /shapley` contract. Raises `ValueError` if the metric has no formula.

### `run_rca(dag, data, traces, target, reference_start, reference_end, analysis_start, analysis_end, advi_draws=500)`

Root cause analysis over `nx.ancestors(dag, target) | {target}`. `traces` is the caller's cache (`app.state.traces` in the API); missing probabilistic fits are added to it in place (ADVI, `fit_end=analysis_start`, keyed `(node, analysis_start)`).

1. **Fit what's missing.** Probabilistic (non-formula, non-root) nodes in scope without a trace are fitted with ADVI — skipped when their windows hold no whole period at their grain.
2. **Per-node attribution at the node's own grain.** Each node snaps the requested windows (`snap_window`); no whole period → `status: "window_shorter_than_grain"` with null numbers and empty contributions (the RCA proceeds). Otherwise the node reports `status: "ok"`, `grain`, `effective_windows`, `baseline`, `actual`, `gap` (mean-per-period at the node's grain), `relative_change` (None if `|baseline| < 1e-12`), `ci_status`, plus:
   - **Formula node** → `attribution_method="shapley"`: the three-game decomposition, bootstrapped with the grain's block length (`BOOT_BLOCK`: day 7, week 4, month 2) for `ci_95`/`prob_same_direction`; single-period windows withhold CIs (`ci_status: "degenerate_single_period"`). Each contribution carries `decomposition: {means: {estimate, ci_95}, comovement: {estimate, ci_95}}` (parts sum to `estimate` exactly per replicate) and the node carries `interaction` (summed co-movement shift + CI) — the data behind the UI's Headline/Detailed views. `unexplained = gap − shapley gap` (measurement residual only).
   - **Probabilistic node** → `attribution_method="posterior"`: `arr = trace.posterior["beta_raw"].reshape(-1, n_parents)`; for parent `i`, `samples = arr[:, i] * bootstrapped parent delta` → `estimate` (mean), `ci_95` (2.5/97.5 pct), `prob_same_direction`. Window period-starts map to the fitted index via `steps_between(dates, fit.dates[0], grain)`; lagged parents measure their delta over windows shifted back by `shift_periods(·, −lag, grain)` (whole periods, correct across month/year bounds). Trend/seasonal deltas are reported in `components`. `unexplained = gap − Σ estimates − trend − seasonal`. Single-period windows flag `ci_status: "posterior_only_single_period"`.
   - **Root node** → `attribution_method=None`, empty contributions, `unexplained=None`.
   - Every contribution carries `share_of_gap = estimate / gap` (None if `|gap| < 1e-12`).
3. **`ranked_causes`** (documented heuristic): `score[target]=1.0`, propagated in reverse topological order; `score[p] += score[c] * min(|share_of_gap|, 1.0)`. All scoped nodes except the target, sorted desc, each `{"metric", "score", "via"}`. Scores (not raw gaps) are the cross-grain-comparable quantity.

`window_mean(data, col, start, end)` is the shared helper (inclusive bounds; raises on empty window).

Response contract: `{"target", "reference_window", "analysis_window", "nodes", "ranked_causes"}` — the top level echoes the *requested* windows; snapped ones are per-node.

---

## `engine/simulate.py`

### `run_scenario(dag, data, traces, scenario, advi_draws=500, n_draws=2000)`

Do-operator what-if behind `POST /simulate` and the MCP `run_whatif` tool. A `ScenarioRequest` carries `interventions` (set/delta/pct on a node — do-operator: severs inbound influence), `assumptions` (user-stated Normal effect ranges on edges the tree doesn't encode, central-90% `[low, high]` convention), and `levers` (display metadata only — no dynamics in v1). Steady-state deltas propagate per-draw through the affected downstream subgraph in topological order — draw index preserved end-to-end, so an optimistic coefficient draw stays optimistic through every hop — with an exact Shapley decomposition over sources (each intervention/assumption) using point means. Cross-grain edges scale deltas by the edge's periods-per-period factor.

**Fitted mode** (`data` present): baselines are `window_mean` over `baseline_start/end` (required; whole periods at each node's grain, no whole period → `ValueError`); coefficient draws index each needed node's `beta_raw` posterior, fit on demand with ADVI into the caller's `traces` cache (keyed `(node, fit_end)`, `fit_end = baseline_end + 1 day`, or the full-window `None` key when the baseline runs to the data edge). Extrapolation honesty flags compare simulated levels against full-history min/max and ±2σ.

**Cold-start mode** (`data=None` — a tree with no data): the same machinery with zero rows. Baselines come from each node's asserted `baseline` declaration — sampled `Normal(mu, (high−low)/2z₉₀)` per non-formula node, formula nodes derived per-draw from parents in topological order so identities hold under the stated beliefs; `beta_raw` is sampled directly from each edge's YAML prior in business units (`_sample_prior`, with analytic `_prior_mean` for the Shapley point pass) — the `x_std/y_std` rescaling exists only to reach normalized space for fitting, so with nothing to fit the prior IS the coefficient distribution. Extrapolation flags come from declared `plausible` bounds (no bounds → no flag). The scenario must omit `baseline_start/end`; `traces` is untouched. `validate_cold_start(dag) -> List[str]` returns every blocker — a non-formula node without `baseline`, a probabilistic edge without an explicit prior (the fitted-mode `Normal(0, 1)` fallback is meaningless with no data to set the scale) — and `run_scenario` raises on a non-empty list.

Response contract: `{"mode": "fitted"|"cold_start", "baseline_window" (null in cold-start mode), "n_draws", "seed", "sources", "nodes", "warnings", "caveats"}`. Per-node: `status` (`baseline`|`affected`|`intervened`), `baseline`, `simulated`, `delta: {estimate, ci_95}`, `relative_delta`, `prob_direction`, `fit_quality`, `extrapolation`, `contributions`; cold-start mode adds `baseline_ci_95` (null for point baselines) and swaps `CAVEATS` for `COLD_START_CAVEATS`. Seeded rng (`seed: 0`) with fixed draw order — identical calls are byte-identical.

---

## `cli.py` (+ `doctor.py`)

`cli.py` is the console entry point (`[project.scripts] breakdown = "breakdown.cli:main"`; repo-root `main.py` is a shim to it). `serve` translates flags to the env vars below and calls `uvicorn.run` — host defaults to `127.0.0.1` and reload is **off** unless `--reload` (dev). It also exports `BREAKDOWN_PORT`/`BREAKDOWN_HOST` (MCP deep links + transport security). `doctor` runs `doctor.run_doctor(tree)` → `print_report` → exit code.

`doctor.py` walks the provider auth chain as `CheckResult`s (`pass`/`fail`/`skip` + copy-paste remediation): tree file → raw YAML → unset `${VAR}` scan (via `parser._ENV_REF`, before the full parse would abort on the first one) → `Parser` parse → per-provider chain (`warehouse`: auth mode / CLI / profile host / `_connect()` + `USE` / per-metric `fetch_metric` over a 7-day probe window; `cloud`: config fields, `client.metrics()` inside a session — one call that proves token + cell host + environment + SL credential mapping — then tree `source`s ⊆ SL metrics; `local`: `mf` on PATH, `dbt_project.yml`, `mf list metrics`). All checks run; failed prerequisites mark dependents `skip`. Connection logic is the real fetchers' — never a duplicate.

---

## `api/main.py`

### Startup configuration (env vars, set by `breakdown serve` flags)

| Env var | CLI flag | Default |
|---------|----------|---------|
| `BREAKDOWN_TREE` | `--tree` | bundled `breakdown/examples/jaffle_shop_tree.yml` |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` |

Dates are validated (ISO format, start ≤ end) both at the CLI and in `lifespan`. `lifespan` builds the fetcher from the tree's `provider` config and fetches every metric for the window **at its declared grain/kind**, assembling per-grain frames via `build_grained` (inner join on `date` within each grain only — a monthly metric no longer drops daily rows tree-wide). For `local`/`cloud` the queried metric name is the last segment of `source`, renamed to the tree `name`; mock generates by tree name directly.

**Degraded startup:** the whole parse/build/fetch is wrapped in one try/except. On failure the app still serves — `app.state.startup_error` holds `"ExcType: message"`, `parser`/`fetcher`/`data` stay `None`, data routes reject via `_require_ready` (503 with the error + a `breakdown doctor` hint), MCP tools reject the same way in `_state()`, and the UI shows a banner. `/ui`, `/`, and `/health` keep working; a container never crash-loops on a bad token. Per-metric diagnosis is deliberately not here — that's `doctor.py`'s job.

Static files and the default tree resolve via `importlib.resources` (`files("breakdown")`), not repo-relative paths, so an installed wheel behaves like a checkout.

### State (set in `lifespan`)

| `app.state` key | Type | Description |
|-----------------|------|-------------|
| `parser` | `Parser \| None` | Parsed metric tree (`None` while degraded) |
| `fetcher` | `BaseDataFetcher \| None` | Fetcher matching the provider type |
| `data` | `GrainedData \| None` | Per-grain frames + `grain_of`/`kind_of`/`last_observed` maps (`last_observed` is captured per metric before the within-grain join; `data_through(m)` converts it to the inclusive last covered date) |
| `startup_error` | `str \| None` | Set when the startup data load failed; gates every data route |
| `traces` | `Dict[Tuple[str, Optional[str]], FitResult]` | **The** trace cache, keyed `(name, fit_end)` (single source of truth) |
| `lock` | `asyncio.Lock` | Serializes sampling (analyze + RCA fits) |

### Routes

**`GET /health`** — always 200: `{status: "ok", provider, metrics}` or `{status: "degraded", error}`. Liveness for orchestrators (the body, not the code, carries degraded-ness) and the UI's first request.

**`GET /meta`** — metrics, data window, provider, per-metric `grains`/`kinds`/`data_through` maps (`data_through` = each metric's honest data edge, which may lag the requested window), fitted list (UI bootstrap).

**`GET /dag`** — nodes (`[name, definition.model_dump()]`) and edges.

**`GET /series`** — every metric's series at its native grain: `{metrics: {name: {grain, dates, values}}}` (mixed grains have no shared date axis, so dates are per-metric); hydrates the UI's node cards in a single request (NaN → null).

**`GET /metrics/{name}`** — definition, time series, and posterior summary via `summarize_trace` (non-finite values serialized as `null`).

**`POST /analyze/{name}`** — `inference_method` (`nuts`|`advi`), `draws`, `tune` (50–5000). Runs `fit_metric` via `asyncio.to_thread` under the lock; stores the trace in `app.state.traces`.

**`GET /shapley/{name}`** — window params; thin wrapper over `rca.shapley_attribution`. 422 if no formula or bad windows.

**`POST /rca/{name}`** — window params (required). Runs `run_rca` via `asyncio.to_thread` under the lock, passing `app.state.traces` directly — on-demand fits land in the cache with no copying. 404 unknown metric; `ValueError` → 422.

---

## `mcp/` — MCP server for AI assistants

`mcp/server.py` defines an `MCPServer` ("breakdown") with four async tools — `get_tree` (compact `/meta` + `/dag`), `explain_metric` (definition + neighbors + series summary + fit status), `run_rca`, and `run_whatif` (`/simulate`'s engine with `Intervention`/`Assumption` as typed params). Tools own no state: they read the FastAPI `app.state` (lazy import to avoid the cycle — `api/main.py` imports `server.mcp` to mount it) and run engine calls exactly like the endpoints do: `async with state.lock: await asyncio.to_thread(...)`. Engine `ValueError`s propagate as MCP tool errors so the calling model can self-correct windows.

`mcp/shaping.py` shapes engine results for LLM consumption: `round_floats` (4 significant figures, non-finite → null), `compact_rca` (drops per-contribution `decomposition` and window detail, collapses `components` to point estimates, shrinks skipped nodes, omits null node fields — but keeps a null `ci_95` inside contributions: withheld-interval semantics), `compact_scenario` (baseline nodes shrink to `{status, baseline}`, extrapolation stats collapse to the flag), `RCA_HOW_TO_READ`/`WHATIF_HOW_TO_READ` (docs/model.md caveats attached to every analysis response), and `rca_link`/`whatif_link`/`metric_link` (UI deep links matching `applyDeepLink()`'s hash params in `static/app.js`; base URL from `BREAKDOWN_PUBLIC_URL`, default `http://127.0.0.1:$BREAKDOWN_PORT`).

Transport: streamable HTTP mounted at `/mcp`, stateless with plain-JSON responses. Two SDK quirks the wiring handles: a mounted sub-app's lifespan never runs, so the host `lifespan` drives `mcp.session_manager.run()`; and the SDK's session manager is single-use per instance, so the mount is a shim (`_McpMount`) and each lifespan startup rebuilds the transport app (tests open several `TestClient`s per process). The SDK's default Host-header validation admits localhost only; when `BREAKDOWN_HOST` (set by `serve --host`) is non-loopback, `rebuild()` disables DNS-rebinding protection so containers/shared hosts can reach `/mcp`.

---

## Data flow

```
YAML file
  → Parser (Pydantic + NetworkX DAG; nodes carry MetricDefinition incl. grain/kind)
    → lifespan: fetch_metric(name, window, grain, kind) per metric
        → build_grained() → app.state.data (GrainedData: per-grain frames)
        → POST /analyze/{name} → fit_metric() at the node's grain → FitResult → app.state.traces
        → POST /rca/{name}     → run_rca(dag, data, app.state.traces, ...)
                                   ├─ fits missing probabilistic nodes (ADVI, node grain)
                                   ├─ snaps windows per node; three-game Shapley (formula)
                                   │  / beta_raw posterior (probabilistic)
                                   └─ {"nodes" (status/grain/effective_windows/...), "ranked_causes", ...}
        → GET /shapley/{name}  → shapley_attribution() → {"gap", "attribution", "decomposition", ...}
        → GET /metrics/{name}  → summarize_trace(best cached fit for name)
```

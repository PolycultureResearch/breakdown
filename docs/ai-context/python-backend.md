# Backend Architecture: Bayesian Engine

The `breakdown` backend uses PyMC to perform Bayesian inference on metric relationships defined in a YAML metric tree.

For the statistical assumptions and how to interpret results, see `docs/model.md`.

---

## Module map

```
breakdown/
  parser.py        # YAML → Pydantic models → NetworkX DAG (typed nodes)
  formula.py       # Shared formula AST validation + safe eval (used by parser, engine, data_fetch)
  data_fetch.py    # BaseDataFetcher + Mock / Local / Cloud implementations
  engine/
    model.py       # fit_metric() — BSTS via PyMC; compute_shapley(); summarize_trace()
    rca.py         # run_rca() + shapley_attribution() — all window-over-window attribution
  api/
    main.py        # FastAPI app — routes, lifespan, state (owns the trace cache)
```

Design rules:

- The engine is **stateless**: `fit_metric` is a pure function (DAG + data + target → trace). The only trace cache lives in `app.state.traces`; `run_rca` receives it as an argument and adds on-demand fits to it in place.
- All attribution (window means, Shapley over windows, posterior attribution) lives in `engine/rca.py`. `engine/model.py` keeps only the pure Shapley enumeration.
- **Parent order is load-bearing:** everywhere, parents come from `list(dag.predecessors(name))`; this is the axis order of `beta`/`beta_raw`. Any new component must use the same call.

---

## `parser.py`

**`MetricDefinition`** — Pydantic model for one metric node. Fields: `name`, `source`, `description`, `sql` (warehouse provider only), `parents`, `formula`, `priors: Dict[str, Prior]`, `lags: Dict[str, int]`, `seasonality: List[Seasonality]`, `trend: Optional[TrendConfig]` (local-level random-walk step-size prior), `format: Optional[MetricFormat]` (UI display hint — presentation only, coerced from the `format: currency` string shorthand). Validators enforce: formula is arithmetic-only AST and references only parents; prior keys are `"coefficient"` or a parent name; lag keys are parents, values are ints ≥ 1, and `lags` is mutually exclusive with `formula`.

**`Parser`** — wraps `MetricTreeConfig` + a `networkx.DiGraph`.
- `parser.dag` — the compiled DAG. **Each node stores its validated model under the `definition` key**: `dag.nodes[name]["definition"]` is a `MetricDefinition` (attribute access, not dict `.get`). This is the single source of truth downstream.
- `parser.get_metric(name)` — O(1) lookup via the DAG.
- `parser.get_topological_order()` — nodes in dependency order.

---

## `data_fetch.py`

### `BaseDataFetcher` (ABC)

All fetchers implement:
```python
def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame
```
Returns a DataFrame with columns `["date", metric_name]`, sorted by date, no NaNs.

### `MockDataFetcher`
Constructed with an optional metric DAG (`MockDataFetcher(dag=parser.dag)`). With a DAG, series are generated in topological order so they respect the tree: formula nodes satisfy their formula plus ~2% noise, probabilistic nodes are a coefficient-weighted sum of parents (coefficient from the `coefficient` prior's `mu` when available; lag-shifted parents when `lags` is set) plus ~5% noise, and roots are random walks with weekly seasonality. Without a DAG (or for names not in it), falls back to an independent random walk. Seeded per metric name — deterministic across calls. The full frame per window is cached.

### `LocalDataFetcher`
Invokes `mf query --metrics <name> --group-by metric_time__<grain> --start-time ... --end-time ... --csv <tmpfile>` as a subprocess. `project_path` becomes the working directory. Raises `RuntimeError` on non-zero exit code or OS errors (e.g., path not found).

### `CloudDataFetcher`
Uses the `dbtsl.SemanticLayerClient` sync API; the Arrow result is converted to pandas and the `metric_time__<grain>` column renamed to `date`.

The correlated jaffle-shop dataset used by tests lives in `tests/synthetic.py` (`generate_mock_data`), not in production code.

---

## `engine/model.py`

### `fit_metric(dag, data, target, draws=1000, tune=1000, inference_method="nuts") -> InferenceData`

The single fitting entry point (stateless). In normalized space the model is

```
y[t] = alpha + trend[t] + seasonal[t] + (X @ beta)[t] + eps[t]
trend ~ GaussianRandomWalk(HalfNormal(1));  seasonal = Fourier pairs (2 harmonics per entry)
```

Internals are three helpers, each documented in-code:
- `_prepare_series(defn, parents, data, target)` → `(y, X, scale)`. Formula nodes fit the z-scored residual (X is None); probabilistic nodes get one z-scored regressor per parent, lag-shifted and trimmed by `max(lags)` (raises if < 10 rows remain); `scale[i] = x_std_i / y_std`.
- `_seasonal_component(seasonality, t)` — Fourier terms.
- `_regression_component(defn, parents, X, scale)` — one `beta_{parent}` RV per parent (parent-specific prior → shared `coefficient` prior → `Normal(0, 1)`), stacked into `beta = Deterministic(...)` plus `beta_raw = beta / scale` (business units). Priors are stated in business units and rescaled via `scale_prior_params(distribution, params, scale)`; unknown distributions raise.

Inference: `nuts` → `pm.sample(draws, tune, target_accept=0.9, chains=2)`; `advi` → `pm.fit(n=20_000).sample(draws)`.

### `compute_shapley(formula, parent_names, baselines, actuals) -> Dict[str, float]`

Pure Shapley enumeration (O(2ⁿ)): distributes `formula(actuals) − formula(baselines)` across parents; values sum to the gap exactly.

### `summarize_trace(trace) -> pd.DataFrame`

`az.summary(trace, hdi_prob=0.95)`.

---

## `engine/rca.py`

All window-over-window attribution lives here.

### `shapley_attribution(dag, data, target, reference_start, reference_end, analysis_start, analysis_end)`

Exact Shapley decomposition for a formula metric; `baseline`/`actual`/`gap` come from the formula on parent window means, so `attribution` sums to `gap` exactly. This is the `GET /shapley` contract. Raises `ValueError` if the metric has no formula.

### `run_rca(dag, data, traces, target, reference_start, reference_end, analysis_start, analysis_end, advi_draws=500)`

Root cause analysis over `nx.ancestors(dag, target) | {target}`. `traces` is the caller's cache (`app.state.traces` in the API); missing probabilistic fits are added to it in place (ADVI).

1. **Fit what's missing.** Probabilistic (non-formula, non-root) nodes in scope without a trace are fitted with ADVI.
2. **Per-node attribution.** Each node reports `baseline`, `actual`, `gap`, `relative_change` (None if `|baseline| < 1e-12`), plus:
   - **Formula node** → `attribution_method="shapley"`: delegates to `shapley_attribution`. `ci_95`/`prob_same_direction` are None. `unexplained = gap − shapley gap`.
   - **Probabilistic node** → `attribution_method="posterior"`: `arr = trace.posterior["beta_raw"].reshape(-1, n_parents)`; for parent `i`, `samples = arr[:, i] * parent_gap` → `estimate` (mean), `ci_95` (2.5/97.5 pct), `prob_same_direction` (max mass either side of 0). Lagged parents measure their gap over windows shifted back by `pd.Timedelta(days=lag)`. `unexplained = gap − Σ estimates`.
   - **Root node** → `attribution_method=None`, empty contributions, `unexplained=None`.
   - Every contribution carries `share_of_gap = estimate / gap` (None if `|gap| < 1e-12`).
3. **`ranked_causes`** (documented heuristic): `score[target]=1.0`, propagated in reverse topological order; `score[p] += score[c] * min(|share_of_gap|, 1.0)`. All scoped nodes except the target, sorted desc, each `{"metric", "score", "via"}`.

`window_mean(data, col, start, end)` is the shared helper (inclusive bounds; raises on empty window).

Response contract: `{"target", "reference_window", "analysis_window", "nodes", "ranked_causes"}`.

---

## `api/main.py`

### Startup configuration (env vars, set by `main.py serve` flags)

| Env var | CLI flag | Default |
|---------|----------|---------|
| `BREAKDOWN_TREE` | `--tree` | `examples/jaffle_shop_tree.yml` |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` |

Dates are validated (ISO format, start ≤ end) both at the CLI and in `lifespan`. `lifespan` builds the fetcher from the tree's `provider` config and fetches every metric for the window, inner-joining on `date`. For `local`/`cloud` the queried metric name is the last segment of `source`, renamed to the tree `name`; mock generates by tree name directly.

### State (set in `lifespan`)

| `app.state` key | Type | Description |
|-----------------|------|-------------|
| `parser` | `Parser` | Parsed metric tree |
| `fetcher` | `BaseDataFetcher` | Fetcher matching the provider type |
| `data` | `pd.DataFrame` | Time-series data for all metrics |
| `traces` | `Dict[str, InferenceData]` | **The** trace cache (single source of truth) |
| `lock` | `asyncio.Lock` | Serializes sampling (analyze + RCA fits) |

### Routes

**`GET /meta`** — metrics, data window, provider, fitted list (UI bootstrap).

**`GET /dag`** — nodes (`[name, definition.model_dump()]`) and edges.

**`GET /series`** — every metric's daily series in one aligned columnar payload (`{dates, columns: {name: [values]}}`) from `app.state.data`; hydrates the UI's node cards in a single request (NaN → null).

**`GET /metrics/{name}`** — definition, time series, and posterior summary via `summarize_trace` (non-finite values serialized as `null`).

**`POST /analyze/{name}`** — `inference_method` (`nuts`|`advi`), `draws`, `tune` (50–5000). Runs `fit_metric` via `asyncio.to_thread` under the lock; stores the trace in `app.state.traces`.

**`GET /shapley/{name}`** — window params; thin wrapper over `rca.shapley_attribution`. 422 if no formula or bad windows.

**`POST /rca/{name}`** — window params (required). Runs `run_rca` via `asyncio.to_thread` under the lock, passing `app.state.traces` directly — on-demand fits land in the cache with no copying. 404 unknown metric; `ValueError` → 422.

---

## Data flow

```
YAML file
  → Parser (Pydantic + NetworkX DAG; nodes carry MetricDefinition)
    → lifespan: BaseDataFetcher.fetch_metric() per metric → app.state.data
        → POST /analyze/{name} → fit_metric() → trace → app.state.traces
        → POST /rca/{name}     → run_rca(dag, data, app.state.traces, ...)
                                   ├─ fits missing probabilistic nodes (ADVI)
                                   ├─ Shapley (formula) / beta_raw posterior (probabilistic)
                                   └─ {"nodes", "ranked_causes", ...}
        → GET /shapley/{name}  → shapley_attribution() → {"gap", "attribution"}
        → GET /metrics/{name}  → summarize_trace(app.state.traces[name])
```

# Backend Architecture: Bayesian Engine

The `breakdown` backend uses PyMC to perform Bayesian inference on metric relationships defined in a YAML metric tree.

---

## Module map

```
breakdown/
  parser.py        # YAML → Pydantic models → NetworkX DAG
  formula.py       # Shared formula AST validation + safe eval (used by parser, engine, data_fetch)
  data_fetch.py    # BaseDataFetcher + Mock / Local / Cloud implementations
  engine/
    model.py       # ModelBuilder (BSTS via PyMC) + compute_shapley()
  api/
    main.py        # FastAPI app — routes, lifespan, state
```

---

## `parser.py`

### Key classes

**`MetricDefinition`** — Pydantic model for one metric node. Fields:
- `name`, `source`, `description`
- `parents: List[str]` — names of causal parents
- `formula: Optional[str]` — arithmetic expression over parents (validated by AST at load time)
- `priors: Dict[str, Prior]` — coefficient priors for probabilistic nodes
- `seasonality: List[Seasonality]` — Fourier seasonality specs

`formula` validation uses `ast.walk` to allow only `BinOp` (arithmetic), `Name` (parent references), `Constant`, and `UnaryOp`. Any other node type — including `Call` — raises `ValueError`.

**`Parser`** — wraps `MetricTreeConfig` + a `networkx.DiGraph`.
- `parser.dag` — the compiled DAG; nodes carry their full `MetricDefinition` dict as attributes
- `parser.get_metric(name)` — look up a `MetricDefinition` by name
- `parser.get_topological_order()` — returns nodes in dependency order

---

## `data_fetch.py`

### `BaseDataFetcher` (ABC)

All fetchers implement:
```python
def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame
```
Returns a DataFrame with columns `["date", metric_name]`, sorted by date, no NaNs.

### `MockDataFetcher`
Constructed with an optional metric DAG (`MockDataFetcher(dag=parser.dag)`). With a DAG, series are generated in topological order so they respect the tree: formula nodes satisfy their formula plus ~2% noise, probabilistic nodes are a coefficient-weighted sum of parents (coefficient taken from the prior's `mu` when available) plus ~5% noise, and roots are random walks with weekly seasonality. Without a DAG (or for names not in it), falls back to an independent random walk. Seeded per metric name — deterministic across calls. The full frame per window is cached.

### `LocalDataFetcher`
Invokes `mf query --metrics <name> --group-by metric_time__<grain> --start-time ... --end-time ... --csv <tmpfile>` as a subprocess. `project_path` becomes the working directory. Raises `RuntimeError` on non-zero exit code or OS errors (e.g., path not found).

### `CloudDataFetcher`
Uses the `dbtsl.SemanticLayerClient` sync API:
```python
with self.client.session():
    table = self.client.query(
        metrics=[metric_name],
        group_by=[f"metric_time__{grain}"],
        where=["metric_time >= '...'", "metric_time <= '...'"],
    )
df = table.to_pandas()
```
The Arrow table is converted to a pandas DataFrame; the `metric_time__<grain>` column is renamed to `date`.

---

## `engine/model.py`

### `compute_shapley(formula, parent_names, baselines, actuals) -> Dict[str, float]`

Standalone function. Distributes the gap between `formula(actuals)` and `formula(baselines)` across each parent using exact Shapley values (full coalition enumeration — O(2ⁿ)).

For each player `i`, the Shapley value is:
```
φᵢ = Σ_{S ⊆ N\{i}} |S|!(n-|S|-1)!/n! × [v(S∪{i}) - v(S)]
```
where `v(S)` is the formula evaluated with coalition members at their actuals and all others at their baselines.

The values are guaranteed to sum to `formula(actuals) - formula(baselines)`.

### `ModelBuilder`

**Constructor:** `ModelBuilder(dag: nx.DiGraph, data: pd.DataFrame)`
- `data` must have a `date` column and one column per metric
- `dag` nodes must carry the serialized `MetricDefinition` dict (set by `Parser`)

**`build_and_sample(target, draws, tune, inference_method)`**

Fits a Bayesian Structural Time Series model for `target`. Two code paths:

**Formula node** (`formula` is set in the DAG node dict):
1. Evaluates `y_formula = eval(formula, {parent: data[parent].values})` — vectorized via numpy
2. Fits BSTS to `residual = y - y_formula` (normalized)
3. No `beta` regressor — structural relationship is captured by the formula
4. Registers `target` in `self.formula_nodes`

**Probabilistic node** (no formula):
1. Normalizes `y` and all parent columns independently
2. Fits BSTS with a `beta` regressor on stacked parent columns
3. Prior on `beta` comes from the YAML `priors.coefficient` field (defaults to `Normal(0, 1)` in normalized space). User priors are stated in business units and translated into normalized space by `scale_prior_params(distribution, params, scale)` where `scale = x_std / y_std` per parent. All four distributions (`Normal`, `HalfNormal`, `Exponential`, `LogNormal`) are honored; anything else raises `ValueError`.
4. Adds `beta_raw = beta / scale` as a `pm.Deterministic`, so the trace and `az.summary` report the coefficient in business units — this survives trace caching without needing the builder's `scale_params`.

**Common model components (both paths):**
```python
sigma_trend ~ HalfNormal(1)
trend        ~ GaussianRandomWalk(sigma=sigma_trend, shape=T)    # local level
sin/cos_<name>_h<k> ~ Normal(0, 1)                               # Fourier seasonality
alpha        ~ Normal(0, 10)                                      # intercept
sigma_obs    ~ HalfNormal(1)
obs          ~ Normal(alpha + trend + seasonal + regression, sigma_obs)
```

**Inference methods:**
- `nuts` (default): `pm.sample(draws, tune, target_accept=0.9, chains=2)` — exact MCMC
- `advi`: `pm.fit(n=20_000, method="advi").sample(draws)` — variational approximation, 5–10× faster

Both return an `arviz.InferenceData` object stored in `self.traces[target]`.

**`compute_shapley(target, reference_start, reference_end, analysis_start, analysis_end)`**

Computes Shapley attribution for a formula node using the data already held by the builder:
1. Slices `self.data` into reference and analysis windows by `date`
2. Computes per-column means in each window
3. Delegates to the standalone `compute_shapley()` function
4. Returns `{"target", "formula", "baseline", "actual", "gap", "attribution"}`

**`get_summary(target)`**

Returns `az.summary(trace, hdi_prob=0.95)` as a DataFrame. Raises if no trace exists for the target.

---

## `api/main.py`

### Startup configuration (env vars, set by `main.py serve` flags)

| Env var | CLI flag | Default |
|---------|----------|---------|
| `BREAKDOWN_TREE` | `--tree` | `examples/jaffle_shop_tree.yml` |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` |

`lifespan` builds the fetcher from the tree's `provider` config and fetches every metric in the tree for the configured window, inner-joining on `date`. For `local`/`cloud` providers the queried metric name is the last segment of `source`; the column is renamed to the tree `name`. The mock provider generates by tree name directly.

### State (set in `lifespan`)

| `app.state` key | Type | Description |
|-----------------|------|-------------|
| `parser` | `Parser` | Parsed metric tree from the examples YAML |
| `fetcher` | `BaseDataFetcher` | Fetcher matching the provider type |
| `data` | `pd.DataFrame` | Time-series data for all metrics |
| `traces` | `Dict[str, InferenceData]` | Cached traces per metric name |
| `lock` | `asyncio.Lock` | Serializes concurrent sampling requests |

### Routes

**`GET /dag`** — Returns DAG nodes and edges. Node data is the full `MetricDefinition` dict.

**`GET /metrics/{name}`** — Returns metric definition, time series, and posterior summary (if a trace exists).

**`POST /analyze/{name}`** — Query params: `inference_method` (`nuts`|`advi`), `draws` (50–5000), `tune` (50–5000). Acquires the lock, builds a `ModelBuilder`, samples, and caches the trace.

**`GET /shapley/{name}`** — Query params: `reference_start`, `reference_end`, `analysis_start`, `analysis_end` (all `YYYY-MM-DD`). Returns Shapley attribution. Requires the metric to have a `formula`. Returns 422 if not.

---

## Data flow

```
YAML file
  → Parser (Pydantic + NetworkX DAG)
    → lifespan: BaseDataFetcher.fetch_metric() per metric
      → app.state.data (DataFrame)
        → POST /analyze/{name}
          → ModelBuilder.build_and_sample()
            → PyMC model (NUTS or ADVI)
              → InferenceData (cached in app.state.traces)
                → GET /metrics/{name} returns az.summary()

app.state.data
  → GET /shapley/{name}
    → ModelBuilder.compute_shapley()
      → compute_shapley() [Shapley enumeration]
        → {"gap", "attribution": {parent: φ}}
```

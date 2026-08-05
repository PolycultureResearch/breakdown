# Backend Architecture: Bayesian Engine

The `breakdown` backend uses PyMC to perform Bayesian inference on metric relationships defined in a YAML metric tree.

For the statistical assumptions and how to interpret results, see `docs/model.md`.

---

## Module map

```
breakdown/
  __init__.py      # `__version__`, from importlib.metadata — pyproject.toml is the only source
  parser.py        # YAML → Pydantic models → NetworkX DAG (typed nodes) + cross-node grain rules
  formula.py       # Shared formula AST validation + safe eval (used by parser, engine, data_fetch)
  grains.py        # ALL grain arithmetic: period floors/snapping/steps, kind-aware
                   # resample_up, GrainedData (per-grain frames), BOOT_BLOCK
  data_fetch.py    # BaseDataFetcher + Mock / Local / Cloud / Warehouse implementations
                   # (provider SDKs are optional extras — imported lazily, never at module scope)
  engine/
    model.py       # fit_metric() — BSTS via PyMC; compute_shapley(); summarize_trace()
    rca.py         # run_rca() + shapley_attribution() — all window-over-window attribution
    slices.py      # slice_attribution() — dimensional slicing of one metric's gap (pure, no I/O)
    simulate.py    # run_scenario() — do-operator what-if; fitted (posterior draws) or cold start (data=None)
  api/
    main.py        # FastAPI app — routes, lifespan, state (owns the trace + slice caches)
  mcp/
    server.py      # MCP server — 5 tools over the same engine/state (mounted at /mcp)
    shaping.py     # MCP response compaction, how_to_read caveats, UI deep links
  cli.py           # Console entry point (`breakdown serve` / `breakdown doctor` / `--version`)
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

**`MetricDefinition`** — Pydantic model for one metric node. Fields: `name`, `source`, `grain` (`day`|`week`|`month`, default day), `kind` (`flow`|`stock`|`rate`, default flow), `description`, `sql` (warehouse provider only), `parents`, `formula`, `priors: Dict[str, Prior]`, `lags: Dict[str, int]` (grain steps at the node's grain), `seasonality: List[Seasonality]` (periods ≥ 2, in grain steps), `trend: Optional[TrendConfig]` (local-level random-walk step-size prior), `baseline: Optional[AssertedBaseline]` (cold-start asserted operating point — `{low, high}` read as a central-90% Normal interval, numeric shorthand coerced to a degenerate point; units are mean per native-grain period, i.e. what a fitted `window_mean` baseline would be), `plausible: Optional[PlausibleRange]` (cold-start honesty band — optional `min`/`max`, at least one required, stands in for historical min/max in what-if extrapolation flags), `dimensions: Dict[str, DimensionSpec]` (declared slicing dimensions — `source` is the provider dimension id, `top_k`/`values` bound cardinality, `weight` names the blend metric for rates, `sql` is reserved for the warehouse contract; the `region: customer__region` string shorthand coerces; analysis-time only, never touches fetching/fitting/attribution), `format: Optional[MetricFormat]` (UI display hint — presentation only, coerced from the `format: currency` string shorthand), `direction` (`up_is_good`|`down_is_good`|`neutral`, UI goodness coloring only — never touches modeling). Validators enforce: formula is arithmetic-only AST and references only parents; prior keys are `"coefficient"` or a parent name; lag keys are parents, values are ints ≥ 1 (with `formula`, lags declare a cohort-aligned lagged identity — `A[t] = f(parents shifted back by their lags)`); `expected_signs` keys are parents with values `positive`/`negative` and are rejected on formula nodes (no learned coefficients to check); `baseline` is rejected on formula nodes (theirs derive per-draw from parents so the identity holds — an asserted one could contradict it); dimension names are identifiers, `weight` is rate-only and required on rates (defaulting from a simple `num / den` formula's denominator; cross-checked against the tree in `Parser._validate_dimension_weights`); classic day-grain seasonality periods (7/30/365) on a non-day node warn.

**Cross-node grain rules** (`Parser._validate_grains`, needs both edge endpoints so it runs after the DAG is built): a parent may never be coarser than its child (downward disaggregation undefined); a finer parent must be an auto-aggregatable `flow`/`stock` whose grain **nests** in the child's (days tile weeks/months; weeks straddle month boundaries, so week-under-month is rejected); finer `rate` parents are rejected (declare the rate at the child's grain).

**`Parser`** — wraps `MetricTreeConfig` + a `networkx.DiGraph`.
- `parser.dag` — the compiled DAG. **Each node stores its validated model under the `definition` key**: `dag.nodes[name]["definition"]` is a `MetricDefinition` (attribute access, not dict `.get`). This is the single source of truth downstream.
- `parser.get_metric(name)` — O(1) lookup via the DAG.
- `parser.get_topological_order()` — nodes in dependency order.

---

## `data_fetch.py`

### Provider SDKs are optional extras

Only `pymc`/`pandas`/`fastapi`-class dependencies are unconditional. `dbtsl` (cloud), the MetricFlow `mf` binary (local) and `databricks-*` (warehouse) ship as the `dbt` and `databricks` extras, so **nothing provider-specific may be imported at module scope** — `api/main.py` imports `data_fetch`, and a module-level `import dbtsl` would make a base install unable to start the server.

The rules, all in `data_fetch.py`:

- `PROVIDER_EXTRAS` maps provider type → extra name; `MissingProviderExtra` (a `RuntimeError`, deliberately **not** an `ImportError`) is what a missing extra raises, carrying the literal `pip install 'metric-breakdown[…]'` to run.
- `_require_module(module, provider, extra)` is the only way a provider SDK enters the process. `provider_extra_missing(provider)` is the non-raising form, used by `doctor.check_provider_extra` and by tests to skip themselves.
- **The check belongs at the point of use, not in `__init__`.** Constructing a fetcher is pure config and must stay free of SDK requirements: a tree can name `local` and be served entirely from committed snapshots (the white-cube demo does exactly this), so `LocalDataFetcher.__init__` must not demand `mf`. `LocalDataFetcher` checks in `_run_mf_query`, `WarehouseDataFetcher` in `_connect`. `CloudDataFetcher` is the exception — it builds a live `SemanticLayerClient` in `__init__`, so that is its point of use.

`doctor.py` reports a missing extra as its own `CheckResult` before the provider chain runs and skips `_DOWNSTREAM_CHECKS[provider]`; otherwise every connectivity check fails with a remediation pointing at the wrong problem.

### `BaseDataFetcher` (ABC)

All fetchers implement:
```python
def fetch_metric(self, metric_name: str, start_date: str, end_date: str,
                 grain: str = "day", kind: str = "flow") -> pd.DataFrame
```
Returns a DataFrame with columns `["date", metric_name]`, sorted by date, no NaNs, with **period-start** date labels at the requested grain (day midnight, week Monday, month 1st).

**Every provider reaches that shape through the same two module-level helpers** — this is a contract, not a convention, and it is enforced in one place because the alternative was tried and failed (roadmap C1/C2, fixed):

- `_to_naive_dates(df, metric_name)` parses `date` and **drops any timezone**, keeping the wall-clock label. It must run first: `floor_period` normalizes but *preserves* tzinfo, so a tz-aware midnight satisfies every period-start check and then matches nothing against the tz-naive spine. That path used to return a full spine of zeros, silently, and snapshot them. The zone is dropped rather than converted — a row labelled midnight `+09:00` means that calendar date, and converting through UTC moves it back a day.
- `_align_to_spine(df, metric_name, grain, kind, start, end, value_col)` reindexes onto the spine of whole periods inside the window and fills by `kind`: partial edge periods dropped, **trailing** gaps trimmed (not-yet-loaded, not zero), **interior** gaps filled — flow → 0, stock → forward-fill (leading gap errors), rate → error, with a warning naming the periods it invented. Rows that *all* miss the spine raise (a query ignoring its bound window); *no rows at all* keeps the full fill for flows, since an all-quiet window is a legitimate flow series.

Label policy stays per-provider on purpose: the warehouse fetcher **errors** on a misaligned label because the SQL author owns the aggregation, while the semantic-layer fetchers **floor with a warning** via `_floor_labels` because a dbt project may legitimately use non-Monday weeks.

A second, non-abstract method backs dimensional slicing:
```python
def fetch_metric_sliced(self, metric_name: str, dimension_source: str,
                        start_date: str, end_date: str,
                        grain: str = "day", kind: str = "flow") -> pd.DataFrame
```
Returns **long format** `["date", "slice", "value"]` — one row per (period, dimension value), NULL dimension values mapped to `"__null__"`. The base implementation raises the typed `SliceNotSupported` (API → 422 naming the provider); `local`/`cloud` implement it by appending `dimension_source` to the existing time-grain `group_by` and reshaping via `_sliced_long`; the warehouse provider does not support it yet; `SnapshotFetcher` passes it through uncached (sliced snapshot persistence deferred). Sliced frames are analysis-time only — they never enter `GrainedData` or the fit path.

### `MockDataFetcher`
Constructed with an optional metric DAG (`MockDataFetcher(dag=parser.dag)`). With a DAG, series are generated in topological order **at each node's declared grain** so they respect the tree: formula nodes satisfy their formula against parents aggregated to the node's grain plus ~2% noise, probabilistic nodes are a coefficient-weighted sum of aligned parents (coefficient from the `coefficient` prior's `mu` when available; lag-shifted parents when `lags` is set) plus ~5% noise, and roots are random walks with weekly seasonality on their native period spine. Finer rate parents resample by per-period mean (mock-only convenience). Without a DAG (or for names not in it), falls back to an independent random walk at the requested grain. Seeded per metric name — deterministic across calls; all-day trees are byte-identical to the pre-grain generator (pinned by golden tests). Per-metric series per window are cached.

Mock slicing (`fetch_metric_sliced`): slice shares are smooth **date-anchored** seeded curves per `(dimension, slice)` — identical across metrics and fetch windows, which is what makes a mock rate's weighted blend reconcile *exactly* against its weight metric's slices (rate slices deviate around the blended rate, orthogonalized against the shares). A slice fetch first looks for a cached `_tree_data` window covering the request and splits *those* numbers (the covering-cache path), so sub-window slice fetches reconcile exactly against the served startup data.

### `WarehouseDataFetcher`
Runs each metric's own `sql` against Databricks SQL. The SQL owns the aggregation to the declared grain (one row per period, period-start labels — misaligned labels **error** here rather than being floored, unlike the semantic-layer providers). Spine, trim and gap-fill are the shared `_align_to_spine` contract above; this fetcher is where those rules were first worked out, which is why its docstring carries the reasoning.

### `LocalDataFetcher`
Invokes `mf query --metrics <name> --group-by metric_time__<grain> --start-time ... --end-time ... --csv <tmpfile>` as a subprocess. `project_path` becomes the working directory. Raises `RuntimeError` on non-zero exit code or OS errors (e.g., path not found), and `MissingProviderExtra` when `mf` is not on `PATH` — a `PATH` check rather than an import check, because `uv tool install dbt-metricflow` satisfies this provider just as well as the `dbt` extra.

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
trend = cumsum(HalfNormal(trend.sigma) * z), non-centered;  seasonal = Fourier pairs (up to 2 harmonics per entry)
```

Internals are three helpers, each documented in-code:
- `_prepare_series(defn, parents, data, target)` → `(y, X, scale, y_mean, y_std, x_stds, dates)`. Formula nodes fit the z-scored residual (X is None); probabilistic nodes get one z-scored regressor per parent, lag-shifted and trimmed by `max(lags)` (raises if < 10 rows remain); `scale[i] = x_std_i / y_std`.
- `_seasonal_component(seasonality, t)` — Fourier terms, Nyquist-filtered by `identifiable_harmonics(period)` (harmonic `k` is kept only when `2k < period`; below that the column is identically zero or collinear, so the parameter would be pure prior — period 3–4 keep one harmonic, ≥ 5 keep both). `seasonal_window_delta` reads posterior variables by name and **must** apply the same filter. Both unidentifiability modes land in `diagnostics["seasonality_warnings"]`: too little data (`len(y) < 2·period`) and dropped harmonics (a property of the period, which more data never fixes). Period < 3 is rejected at parse time.
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

### Window validation (both entry points)

Two guards, called by `shapley_attribution` and `run_rca` alike, because a window that is merely *wrong* rather than *empty* produces a plausible number:

- `_validate_windows(...)` — grain- and data-independent ordering: `reference_start <= reference_end < analysis_start <= analysis_end`. Overlap is an error, not a warning (a shared period counts as both the normal regime and the departure from it); an inverted window is rejected here rather than silently snapping to an empty one.
- `_validate_coverage(frame, node, grain, snapped_ref, snapped_an, lags)` — the snapped windows must lie *fully* inside the node's own grain frame. A window entirely outside the data already raised in `_window_values`; this catches the partial overlap, which silently averages whichever periods happen to exist. Lagged parents are checked against their shifted windows and reported with the parent, its lag, and the shifted dates — the caller never typed that window, so naming the one they did type would send them looking in the wrong place. Called per node in `run_rca` (after the `window_shorter_than_grain` check, so grain mismatch still degrades gracefully rather than raising).

### `shapley_attribution(dag, data, target, reference_start, reference_end, analysis_start, analysis_end)`

Symmetric per-period Shapley decomposition for a formula metric at the target's grain: each parent's attribution is `means + covariance_analysis − covariance_reference` (three exact games; both windows evaluated period-by-period), so `attribution` sums to `gap = actual − baseline` exactly and the per-parent parts are returned under `decomposition`. Windows snap to whole periods (`grain` + `effective_windows` in the response); a window with no whole period raises `ValueError`. This is the `GET /shapley` contract. Raises `ValueError` if the metric has no formula.

### `run_rca(dag, data, traces, target, reference_start, reference_end, analysis_start, analysis_end, advi_draws=500)`

Root cause analysis over `nx.ancestors(dag, target) | {target}`. `traces` is the caller's cache (`app.state.traces` in the API); missing probabilistic fits are added to it in place (ADVI, `fit_end=analysis_start`, keyed `(node, analysis_start)`).

1. **Fit what's missing.** Probabilistic (non-formula, non-root) nodes in scope without a trace are fitted with ADVI — skipped when their windows hold no whole period at their grain.
2. **Per-node attribution at the node's own grain.** Each node snaps the requested windows (`snap_window`); no whole period → `status: "window_shorter_than_grain"` with null numbers and empty contributions (the RCA proceeds). Otherwise the node reports `status: "ok"`, `grain`, `effective_windows`, `baseline`, `actual`, `gap` (mean-per-period at the node's grain), `relative_change` (None if `|baseline| < 1e-12`), `ci_status`, plus:
   - **Formula node** → `attribution_method="shapley"`: the three-game decomposition. Point estimates are the **exact** Shapley values (`sh["attribution"]`); the bootstrap supplies only `ci_95`/`prob_same_direction`, using the grain's block length (`BOOT_BLOCK`: day 7, week 4, month 2). Each contribution carries `decomposition: {means: {estimate, ci_95}, comovement: {estimate, ci_95}}` — parts sum to `estimate` exactly, as exact values — and the node carries `interaction` (the summed co-movement shift, already *inside* the contributions; never a term to add on top). `unexplained = gap − shapley gap` (measurement residual only), so contributions reconcile with the node's own gap to machine precision.
     - **Interval honesty** (`_window_mean_correction`, `_widen`, `_degenerate_spread`): replicate spread is rescaled by `1/√((1 − ℓ/n)(1 − 1/n))` to undo the circular-MBB attenuation and the empirical-distribution ddof gap — applied to the **means bridge only**, since the two co-movement games pair each replicate's means with that replicate's own daily values and widening one side would break the pairing. Intervals are withheld whenever the *resampled spread* is degenerate, not merely when the window holds one period: `ci_status` is `"degenerate_single_period"`, `"degenerate_constant_window"` (a parent flat across the window — judged per contribution, so a parent that moved keeps its interval), or `"ok"`. The UI prefix-matches `degenerate_*`. **This improves short-window coverage without fixing it** — the measured table is in [`docs/model.md`](../model.md); the residual is roadmap S6/S18.
   - **Probabilistic node** → `attribution_method="posterior"`: `arr = trace.posterior["beta_raw"].reshape(-1, n_parents)`; for parent `i`, `samples = arr[:, i] * bootstrapped parent delta` → `estimate` (mean), `ci_95` (2.5/97.5 pct), `prob_same_direction`. Window period-starts map to the fitted index via `steps_between(dates, fit.dates[0], grain)`; lagged parents measure their delta over windows shifted back by `shift_periods(·, −lag, grain)` (whole periods, correct across month/year bounds), and each lagged contribution (both attribution methods; `shapley_attribution` carries a top-level map) reports `lag` + `parent_windows` — the shifted `{reference, analysis}` windows, the dates to narrate the parent with and to reuse for follow-up analysis. Both keys are absent entirely on unlagged contributions, so unlagged responses are unchanged. Trend/seasonal deltas are reported in `components`. `unexplained = gap − Σ estimates − trend − seasonal`. Single-period windows flag `ci_status: "posterior_only_single_period"`.
   - **Root node** → `attribution_method=None`, empty contributions, `unexplained=None`.
   - Every contribution carries `share_of_gap = estimate / gap` (None if `|gap| < 1e-12`).
3. **`ranked_causes`** (documented heuristic): `score[target]=1.0`, propagated in reverse topological order; `score[p] += score[c] * _hop_weight(share_of_gap)` where `_hop_weight = min(|s|, 1/|s|)` — peaked at 1, decaying either side, symmetric in log space. The decay above 1 is load-bearing (roadmap C5): the old `min(|s|, 1.0)` saturated, so a parent whose contribution dwarfed its child's gap — i.e. one cancelled by a sibling — scored the same as one that explained the gap exactly, and a quiet node handed full influence upward. Paired with it, `share_of_gap` is `None` unless `|gap| >= _GAP_EPS * max(|baseline|, |actual|, 1)`, so "didn't move" is judged against the node's own level rather than an absolute floor. All scoped nodes except the target, sorted desc, each `{"metric", "score", "via"}`. Scores (not raw gaps) are the cross-grain-comparable quantity.

`window_mean(data, col, start, end)` is the shared helper (inclusive bounds; raises on empty window).

Response contract: `{"target", "reference_window", "analysis_window", "nodes", "ranked_causes"}` — the top level echoes the *requested* windows; snapped ones are per-node.

---

## `engine/slices.py`

### `slice_attribution(defn, dimension, sliced, unsliced, reference_start, reference_end, analysis_start, analysis_end, weight_sliced=None)`

Attributes one metric's window-over-window gap across the values of one declared dimension (behind `POST /rca/{name}/slices` and the MCP `slice_metric` tool). **Pure — no I/O**: the caller (the API's `_run_slice`) fetches the long-format frames and passes them in, so the stateless-engine rule holds; the endpoint owns the read-through `app.state.slice_cache`.

Closed forms, not coalition enumeration (the module docstring states the Shapley equivalences):

- **Flows/stocks** (`attribution_method: "slice_sum"`): the sum identity is linear, so per-slice attribution collapses to `contribution_g = mean_an(x_g) − mean_ref(x_g)`, summing exactly to the sliced gap.
- **Rates** (`"slice_blend"`, requires `weight_sliced` — the `weight` metric sliced the same way): exact symmetric Bennet split per slice at the window-aggregate level, `within_g = s̄·Δr` + `mix_g = r̄·Δs`, summing exactly to the blended gap; `mix_total` is the composition-effect line (mix shares sum to zero). A slice with zero weight in one window keeps its other window's rate, so a new/vanished slice flows entirely through mix.

Ranking is by **excess concentration**: `excess_g = contribution_g − baseline_share_g × gap` (Σ excess = 0), with `ci_95`/`prob_concentrated`/`noise_level` from the same seeded circular moving-block bootstrap as RCA (`_block_bootstrap_indices`, `BOOT_BLOCK`, joint resampling across slices; single-period windows withhold CIs via `ci_status`). Cardinality: top-`top_k` by mean |value| (or the `values` pin-list), rest folded into a full `__other__` row (sum of *fetched* non-top slices — never `unsliced − Σ topK`); hard cap 100 distinct values. A `reconciliation` block compares Σ slices (or the per-date weighted blend) against the metric's own series — `"discrepant"` above 0.5% of |baseline| is a caveat, never a silent correction. Windows snap per the metric's grain exactly as in RCA; slicing a lagged parent means the caller passes the parent's lag-shifted windows.

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

`doctor.py` walks the provider auth chain as `CheckResult`s (`pass`/`fail`/`skip` + copy-paste remediation): tree file → raw YAML → unset `${VAR}` scan (via `parser._ENV_REF`, before the full parse would abort on the first one) → `Parser` parse → per-provider chain (`warehouse`: auth mode / CLI / profile host / `_connect()` + `USE` / per-metric `fetch_metric` over a 7-day probe window; `cloud`: config fields, `client.metrics()` inside a session — one call that proves token + cell host + environment + SL credential mapping — then tree `source`s ⊆ SL metrics; `local`: `mf` on PATH, `dbt_project.yml`, `mf list metrics`; `none`: `validate_cold_start(dag)` — no connection to prove, readiness means every baseline/edge-prior belief is declared, same check the server runs at startup). A final **fit readiness** check (`check_fit_readiness`) runs when both dates are explicit (the default 7-day probe window would always fail it): per-metric whole-period counts over the window vs `model.MIN_FIT_PERIODS`, through the real fetcher path — the graduation report for a tree migrating from cold start to fitted. Skipped for `provider: none`, without an explicit window, or when provider checks failed. All checks run; failed prerequisites mark dependents `skip`. Connection logic is the real fetchers' — never a duplicate.

---

## `api/main.py`

### Startup configuration (env vars, set by `breakdown serve` flags)

| Env var | CLI flag | Default |
|---------|----------|---------|
| `BREAKDOWN_TREE` | `--tree` | bundled `breakdown/examples/jaffle_shop_tree.yml` |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` |

Dates are validated (ISO format, start ≤ end) both at the CLI and in `lifespan`. `lifespan` builds the fetcher from the tree's `provider` config and fetches every metric for the window **at its declared grain/kind**, assembling per-grain frames via `build_grained` (inner join on `date` within each grain only — a monthly metric no longer drops daily rows tree-wide). For `local`/`cloud` the queried metric name is the last segment of `source`, renamed to the tree `name`; mock generates by tree name directly.

`build_grained` then requires each grain frame to be a **gap-free run of periods** (`_check_contiguous`, grain-aware via `_FREQ`), raising with up to 10 named missing dates plus a count. This is not tidiness: everything downstream indexes by position — the model's `t = arange(len(y))` dates the rows, lags shift by *rows*, and the bootstrap resamples contiguous runs — so a hole compresses the calendar and silently shifts every date rather than failing. Periods dropped by the inner join (present for only some metrics) are logged as a warning even when the survivors stay contiguous.

**Cold-start startup (`provider: none`):** `lifespan` fetches nothing — `app.state.data` stays `None` with no `startup_error`; a stated mode, not degraded. Readiness is checked up front (`validate_cold_start`); missing declarations raise into the degraded path with the full blocker list. Time-series routes (`/series`, `/analyze`, `/shapley`, `/rca`) reject via `_require_data` (422 pointing at `/simulate`); `/meta` reports `mode: "cold_start"` with null window; `/metrics/{name}` serves the definition with an empty series; `/simulate` passes `data=None` through to the engine's cold-start branch unchanged.

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
| `slice_cache` | `Dict[Tuple[str, str, str, str, str], pd.DataFrame]` | On-demand sliced frames, keyed `(metric, dimension_source, grain, start, end)` — deliberately separate from `GrainedData` |
| `lock` | `asyncio.Lock` | Serializes sampling (analyze + RCA fits) |

### Routes

**`GET /health`** — always 200: `{status: "ok", provider, metrics}` or `{status: "degraded", error}`. Liveness for orchestrators (the body, not the code, carries degraded-ness) and the UI's first request.

**`GET /meta`** — `mode` (`"fitted"` | `"cold_start"` — which surface the UI should boot), metrics, data window (null in cold start), provider, per-metric `grains`/`kinds`/`data_through` maps (`data_through` = each metric's honest data edge, which may lag the requested window), fitted list (UI bootstrap).

**`GET /dag`** — nodes (`[name, definition.model_dump()]`) and edges.

**`GET /series`** — every metric's series at its native grain: `{metrics: {name: {grain, dates, values}}}` (mixed grains have no shared date axis, so dates are per-metric); hydrates the UI's node cards in a single request (NaN → null).

**`GET /metrics/{name}`** — definition, time series, and posterior summary via `summarize_trace` (non-finite values serialized as `null`).

**`POST /analyze/{name}`** — `inference_method` (`nuts`|`advi`), `draws`, `tune` (50–5000). Runs `fit_metric` via `asyncio.to_thread` under the lock; stores the trace in `app.state.traces`.

**`GET /shapley/{name}`** — window params; thin wrapper over `rca.shapley_attribution`. 422 if no formula or bad windows.

**`POST /rca/{name}`** — window params (required). Runs `run_rca` via `asyncio.to_thread` under the lock, passing `app.state.traces` directly — on-demand fits land in the cache with no copying. 404 unknown metric; `ValueError` → 422.

**`POST /rca/{name}/slices`** — `dimension` + window params (all required, dates validated). 404 unknown metric; 422 for an undeclared dimension, a provider that raises `SliceNotSupported`, or engine `ValueError`s. `_run_slice` (sync, via `asyncio.to_thread` under the lock) computes the fetch span (`min(starts)..max(ends)` — lag-shifted windows are the *caller's* to pass when slicing a lagged parent), reads through `slice_cache` (querying by the same `source`-last-segment rule as startup for SL providers), fetches the `weight` metric's slices too for rates (must share the rate's grain), and calls the pure `slice_attribution`.

**`POST /simulate`** — `ScenarioRequest` body. Runs `run_scenario` via `asyncio.to_thread` under the lock; `app.state.data` is passed straight through, so a cold-start tree (data `None`) selects the engine's cold-start branch with no route logic. `ValueError` → 422.

`/series`, `/analyze`, `/shapley`, `/rca`, and `/rca/{name}/slices` guard with `_require_data` (503 degraded, then 422 on a cold-start tree — those analyses consume history that deliberately doesn't exist); everything else guards with `_require_ready` alone.

---

## `mcp/` — MCP server for AI assistants

`mcp/server.py` defines an `MCPServer` ("breakdown") with five async tools — `get_tree` (compact `/meta` + `/dag`; carries `mode`, each metric's declared `dimensions`, and, cold start, asserted baselines instead of a data window), `explain_metric` (definition + neighbors + series summary + fit status; series summary is null on a cold-start tree, with `baseline`/`plausible` declarations in the definition), `run_rca`, `slice_metric` (the traverse-then-slice follow-up: localizes a metric's gap within one declared dimension via the same `_run_slice` path as the endpoint, lag-shifted windows per its docstring), and `run_whatif` (`/simulate`'s engine with `Intervention`/`Assumption` as typed params; `baseline_start`/`baseline_end` are Optional — required on a fitted tree, omitted on a cold-start one). Tools own no state: they read the FastAPI `app.state` (lazy import to avoid the cycle — `api/main.py` imports `server.mcp` to mount it) and run engine calls exactly like the endpoints do: `async with state.lock: await asyncio.to_thread(...)`. Engine `ValueError`s propagate as MCP tool errors so the calling model can self-correct windows. `run_rca` guards cold start via `_require_data` — a tool error naming `run_whatif` as the tool that does work.

`mcp/shaping.py` shapes engine results for LLM consumption: `round_floats` (4 significant figures, non-finite → null), `compact_rca` (drops per-contribution `decomposition` and window detail, collapses `components` to point estimates, shrinks skipped nodes, omits null node fields — but keeps a null `ci_95` inside contributions: withheld-interval semantics — and passes `lag`/`parent_windows` through on lagged contributions), `compact_slice` (window detail → period counts, per-slice nulls and empty caveats trimmed, reconciliation collapsed to status + residual share) with `SLICE_HOW_TO_READ` (excess-vs-contribution, zero-sum excess, `noise_level`, `__other__`, mix-is-composition, reconciliation, and the lag-shifted-window rule), `compact_scenario` (baseline nodes shrink to `{status, baseline}`, extrapolation stats collapse to the flag; `mode` and any non-null `baseline_ci_95` belief intervals pass through), `RCA_HOW_TO_READ`/`whatif_how_to_read(mode)` (docs/model.md caveats attached to every analysis response; cold-start results append `COLD_START_HOW_TO_READ`, which reframes every number as a stated belief), and `rca_link`/`whatif_link`/`metric_link` (UI deep links matching `applyDeepLink()`'s hash params in `static/app.js`; base URL from `BREAKDOWN_PUBLIC_URL`, default `http://127.0.0.1:$BREAKDOWN_PORT`).

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
        → POST /rca/{name}/slices → _run_slice(): fetch_metric_sliced (slice_cache read-through)
                                   → slice_attribution() → {"slices" (excess-ranked), "reconciliation", ...}
        → GET /metrics/{name}  → summarize_trace(best cached fit for name)
```

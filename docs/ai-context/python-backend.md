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
  dbt_manifest.py  # in-tree Pydantic models for dbt's semantic_manifest.json (no MSI dependency)
  dbt_bridge.py    # dbt's target/semantic_manifest.json → BindingSpec per node (no dbt Cloud)
  dbt_sql.py       # BindingSpec + grain + window (+ dimension) → dialect SQL via sqlglot
  dbt_provider.py  # the `dbt` provider: profiles.yml → connection → generated SQL → spine
  engine/
    model.py       # fit_metric() — BSTS via PyMC; compute_shapley(); summarize_trace()
    rca.py         # run_rca() + shapley_attribution() — all window-over-window attribution
    slices.py      # slice_attribution() — dimensional slicing of one metric's gap (pure, no I/O)
    simulate.py    # run_scenario() — do-operator what-if; fitted (posterior draws) or cold start (data=None)
    progress.py    # report() — advisory progress callbacks; swallows callback exceptions
  api/
    main.py        # FastAPI app — routes, lifespan, the bearer-token gate, per-tree state wiring
    trees.py       # TreeState (one per tree) + the process-wide TraceStore, discovery
  mcp/
    server.py      # MCP server — 6 tools over the same engine/state (mounted at /mcp)
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

**`MetricDefinition`** — Pydantic model for one metric node. Fields: `name`, `source` (**`Optional`** — omitting it declares a *derived* node, legal only on a `formula` node; the computed field `derived` == `source is None` and travels through `model_dump()` so `/dag` carries it), `denominator: Optional[str]` (rate-only: the tree metric whose per-period values weight the rate over a window — see 1.11b below), `no_denominator: Optional[str]` (rate-only: **the third state of that field** — its presence is "asked and answered, this rate has no denominator" and its value is the reason, which is refused empty. A separate field rather than a sentinel on `denominator`, because PyYAML parses `none` as the string `'none'`, `null`/`~` as `None` (indistinguishable from unset) and `no` as the *boolean* `False`; and a plain field rather than `model_fields_set`, because that does not survive `model_dump()` and `/dag` serializes that way — the C21 defect exactly), `grain` (`day`|`week`|`month`, default day), `kind` (`flow`|`stock`|`rate`, default flow), `description`, `sql` (warehouse provider only), `parents`, `formula`, `priors: Dict[str, Prior]`, `lags: Dict[str, int]` (grain steps at the node's grain), `seasonality: List[Seasonality]` (periods ≥ 2, in grain steps), `trend: Optional[TrendConfig]` (local-level random-walk step-size prior), `baseline: Optional[AssertedBaseline]` (cold-start asserted operating point — `{low, high}` read as a central-90% Normal interval, numeric shorthand coerced to a degenerate point; units are mean per native-grain period, i.e. what a fitted `window_mean` baseline would be), `plausible: Optional[PlausibleRange]` (cold-start honesty band — optional `min`/`max`, at least one required, stands in for historical min/max in what-if extrapolation flags), `dimensions: Dict[str, DimensionSpec]` (declared slicing dimensions — `source` is the provider dimension id, `top_k`/`values` bound cardinality, `weight` names the blend metric for rates, `sql` is reserved for the warehouse contract; the `region: customer__region` string shorthand coerces; analysis-time only, never touches fetching/fitting/attribution), `format: Optional[MetricFormat]` (UI display hint — presentation only, coerced from the `format: currency` string shorthand), `direction` (`up_is_good`|`down_is_good`|`neutral`, UI goodness coloring only — never touches modeling; **`Optional`, defaulting to `None`**, because `/dag` serializes with `model_dump()` and a display default would reach the browser indistinguishable from a declaration — the UI renders `None` like `neutral`, and `_validate_goal` reads `is not None` rather than `model_fields_set`). Validators enforce: formula is arithmetic-only AST and references only parents; prior keys are `"coefficient"` or a parent name; lag keys are parents, values are ints ≥ 1 (with `formula`, lags declare a cohort-aligned lagged identity — `A[t] = f(parents shifted back by their lags)`); `expected_signs` keys are parents with values `positive`/`negative` and are rejected on formula nodes (no learned coefficients to check); `baseline` is rejected on formula nodes (theirs derive per-draw from parents so the identity holds — an asserted one could contradict it); dimension names are identifiers, `weight` is rate-only and required on rates — but it now **defaults from the node-level `denominator`** rather than the other way round (roadmap 1.11b), and a `weight` disagreeing with it is a parse error, so the blend weights and the window aggregate cannot be built from two beliefs about one ratio; `Parser._validate_denominators` resolves the remaining case (a `bind` ratio naming a tree metric), checks the denominator exists, is not itself a rate, and is not coarser than the rate, and returns **two** inventories — `Parser.rates_denominator_unanswered` (the lint: nobody has said) and `Parser.rates_denominator_none` (name -> the author's reason). Two, not one, because a node in the second is a finding rather than outstanding work: `doctor` passes and quotes the reasons instead of advising a denominator that for a median cannot exist, and `tests/test_project_invariants.py` holds the unanswered count of every shipped tree at zero. `no_denominator` is refused beside a declared `denominator`, on a non-rate, and whenever a derivation source (formula, `dimensions[].weight`, `bind` ratio) names a denominator anyway — the sources are evidence and the field is a claim about the evidence, so a contradiction is the author's to resolve); classic day-grain seasonality periods (7/30/365) on a non-day node warn; and a **ratio-shaped name that never declared `kind`** warns (`_RATE_SHAPED_NAME` matches `rate`/`ratio`/`pct`/`percent`/`share`/`ctr` as a word, an `average_`/`avg_`/`mean_`/`median_` or `time_to_` prefix, or an infix `_per_`). That last one is gated on `"kind" not in self.model_fields_set`, so it fires only when the default was inherited — writing `kind: flow` explicitly silences it. Deliberately a warning: it is a naming heuristic, a genuinely additive `orders_per_day` would match it, and refusing to parse a valid tree is worse than the default it is guarding against.

**Cross-node grain rules** (`Parser._validate_grains`, needs both edge endpoints so it runs after the DAG is built): a parent may never be coarser than its child (downward disaggregation undefined); a finer parent must be an auto-aggregatable `flow`/`stock` whose grain **nests** in the child's (days tile weeks/months; weeks straddle month boundaries, so week-under-month is rejected); finer `rate` parents are rejected (declare the rate at the child's grain).

**`Parser`** — wraps `MetricTreeConfig` + a `networkx.DiGraph`.
- `parser.dag` — the compiled DAG. **Each node stores its validated model under the `definition` key**: `dag.nodes[name]["definition"]` is a `MetricDefinition` (attribute access, not dict `.get`). This is the single source of truth downstream.
- `parser.get_metric(name)` — O(1) lookup via the DAG.
- `parser.get_topological_order()` — nodes in dependency order.

---

## `formula.py`

AST validation (`validate_formula`), name extraction (`referenced_names`) and safe evaluation (`eval_formula`) — shared by the parser, the engine and `data_fetch`. The expression is arithmetic-only over parent names; calls and attribute access are rejected, and `eval` runs with `{"__builtins__": {}}` as its globals.

A zero denominator yields numpy's own `inf`/`nan` rather than raising; callers decide what a non-finite value means for them (`shapley_attribution` refuses to attribute one, naming the offending series).

⚠️ **The `np.errstate` block around the `eval` is load-bearing, not cosmetic.** numpy reports divide-by-zero and invalid-value conditions through Python's *warnings* machinery, which resolves `__import__` from the **calling frame's globals** — and those globals are deliberately `{"__builtins__": {}}`, which is exactly what makes the `eval` safe alongside the AST allow-list. So the very first zero denominator used to die with `KeyError: '__import__'` instead of producing `inf`. Silencing the conditions means that path is never entered. Do **not** "fix" a future recurrence by putting `__import__` (or any other builtin) back into the eval globals — that reopens the sandbox. Keep the allow-list and the empty builtins exactly as they are.

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
- `_align_to_spine(df, metric_name, grain, kind, start, end, value_col)` reindexes onto the spine of whole periods inside the window and fills by `kind`. **A `rate` is never filled and, since roadmap 1.11, never refused either**: its missing periods stay `NaN` (undefined) and are warned about by name. The refusal was the right judgement attached to the wrong remedy — a metric whose denominator is legitimately zero in a few periods took its whole tree down. Nothing is invented either way; the difference is that the metric can be served. This boundary cannot tell an undefined period from an unloaded one, so `api/main._report_undefined_periods` classifies them after the fetch, where the denominator's series is in hand. Partial edge periods are dropped, and the three edges are treated differently on purpose:

  | Gap position | `flow` | `stock` | `rate` |
  |---|---|---|---|
  | **Leading** (before the source's first row) | fill `0` + **warning naming the invented periods** | raise (nothing to forward-fill from) | raise |
  | **Interior** | fill `0` + warning | forward-fill + warning | raise |
  | **Trailing** | trim | trim | trim |

  Rows that *all* miss the spine raise (a query ignoring its bound window); *no rows at all* keeps the full fill for flows, since an all-quiet window is a legitimate flow series — and that case draws **no** leading warning, because the provider that knows the result was empty says so itself.

  **The leading warning is the recent half of the same defect the interior one guards** (previously silent). A metric that started partway into the window — a product launched in March, a channel switched on in week 3 — trains on a run of fabricated zeros, so the fit sees a manufactured level shift *and* a manufactured trend on a node RCA will happily rank as a cause. The warning names the fabricated periods and the source's actual first row, and points at a later `--start-date`.

  **Leading gaps are filled rather than trimmed, unlike trailing ones**, even though the arguments rhyme, because the two edges cost different things downstream. Trailing trim shortens a series by an ETL lag — days — whereas a flow that genuinely was all-quiet before it started is a legitimate series that trimming would silently discard. More decisively: per-grain frames are assembled by **inner** join (`build_grained`), so trimming one node's leading run would delete those periods for *every* metric at that grain and narrow the windows `_validate_coverage` accepts tree-wide — a whole tree losing January because one node launched in March is a larger and stranger failure than the one being fixed. Narrowing only the late node's own window is the honest version and needs a per-metric window the frames do not carry yet; until then the warning names exactly which periods are invented.

Label policy stays per-provider on purpose: the warehouse fetcher **errors** on a misaligned label because the SQL author owns the aggregation, while the semantic-layer fetchers **floor with a warning** via `_floor_labels` because a dbt project may legitimately use non-Monday weeks.

A second, non-abstract method backs dimensional slicing:
```python
def fetch_metric_sliced(self, metric_name: str, dimension_source: str,
                        start_date: str, end_date: str,
                        grain: str = "day", kind: str = "flow") -> pd.DataFrame
```
Returns **long format** `["date", "slice", "value"]` — one row per (period, dimension value), NULL dimension values mapped to `"__null__"`. The base implementation raises the typed `SliceNotSupported` (API → 422 naming the provider); `local`/`cloud` implement it by appending `dimension_source` to the existing time-grain `group_by` and reshaping via `_sliced_long`; the warehouse provider does not support it yet; `SnapshotFetcher` passes it through uncached (sliced snapshot persistence deferred). Sliced frames are analysis-time only — they never enter `GrainedData` or the fit path.

A third, non-abstract method backs history discovery (roadmap 1.10):
```python
def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]
```
A **capability, not a contract**: the earliest period-start the source has, or `None` when the provider can't answer — and implementations never raise (log + `None`). Mock returns its `_EPOCH` (`2020-01-01`); warehouse wraps the metric's own SQL in `SELECT MIN(date) FROM (…)` bound over an effectively unbounded window; local runs `mf query --order <grain dim> --limit 1`; cloud queries the SDK with `order_by`/`limit=1`; `SnapshotFetcher` passes through with a belt-and-braces try/except (a snapshot-only deployment has no SDK and must simply not know). Consumed by `lifespan`'s background `_discover_earliest` task, which fills `app.state.earliest` metric by metric off the startup path (one provider round-trip per metric would roughly double cold boot; `/meta` reports whatever has arrived) and is cancelled-and-awaited on shutdown, and by the doctor's **history headroom** check, which probes synchronously through the same fetcher as fit readiness.

### `MockDataFetcher`
Constructed with an optional metric DAG (`MockDataFetcher(dag=parser.dag)`). With a DAG, series are generated in topological order **at each node's declared grain** so they respect the tree: formula nodes satisfy their formula against parents aggregated to the node's grain plus ~2% noise, probabilistic nodes are a coefficient-weighted sum of aligned parents (lag-shifted when `lags` is set) plus ~5% noise, and roots are random walks with weekly seasonality on their native period spine.

Generation is **kind-aware** (C11). A `kind: rate` root is drawn on a rate's scale by `_mock_rate_scale` — a share in (0,1), or a per-unit magnitude when the name looks like a duration/average/`_per_` — with a damped walk and a clip to its band, because an undamped walk drifts a share negative over a long window. Without this, a rate leaf was drawn from the same `uniform(50, 5000)` as an impression count and a funnel of `count × rate` identities compounded once per level (10²⁵ on the reference tree). Coefficients come from `_mock_coef`, which reads the **per-parent** prior first and falls back to `coefficient` then `uniform(0.1, 0.5)`, mapping `Normal` → `mu` (so a declared *negative* edge is generated negative) and `HalfNormal` → its mean; previously every parent shared `coefficient`'s `mu` and no coefficient could be negative, so a correct `expected_signs: negative` always warned. A probabilistic node whose own `kind` is `rate` sums coefficient-weighted parent **deviations** around a rate-scaled level and rescales the composite by a positive scalar (signs and per-parent shares preserved), rather than `sum(coef × parent)` which would put a conversion rate on its regressors' scale. **Flows and stocks are unchanged**, which is what keeps all-flow trees byte-identical. Finer rate parents resample by per-period mean (mock-only convenience). Without a DAG (or for names not in it), falls back to an independent random walk at the requested grain. Seeded per metric name — deterministic across calls; all-day trees are byte-identical to the pre-grain generator (pinned by golden tests). Per-metric series per window are cached.

Mock slicing (`fetch_metric_sliced`): slice shares are smooth **date-anchored** seeded curves per `(dimension, slice)` — identical across metrics and fetch windows, which is what makes a mock rate's weighted blend reconcile *exactly* against its weight metric's slices (rate slices deviate around the blended rate, orthogonalized against the shares). A slice fetch first looks for a cached `_tree_data` window covering the request and splits *those* numbers (the covering-cache path), so sub-window slice fetches reconcile exactly against the served startup data.

### `WarehouseDataFetcher`
Runs each metric's own `sql` against Databricks SQL. The SQL owns the aggregation to the declared grain (one row per period, period-start labels — misaligned labels **error** here rather than being floored, unlike the semantic-layer providers). Spine, trim and gap-fill are the shared `_align_to_spine` contract above; this fetcher is where those rules were first worked out, which is why its docstring carries the reasoning.

### `LocalDataFetcher`
Invokes `mf query --metrics <name> --group-by metric_time__<grain> --start-time ... --end-time ... --csv <tmpfile>` as a subprocess. `project_path` becomes the working directory. Raises `RuntimeError` on non-zero exit code or OS errors (e.g., path not found), and `MissingProviderExtra` when `mf` is not on `PATH` — a `PATH` check rather than an import check, because `uv tool install dbt-metricflow` satisfies this provider just as well as the `dbt` extra.

A metric matching **no rows** is not an error here. `mf` writes a zero-byte file — not even a header — and `pd.read_csv` raises `EmptyDataError`, so `_run_mf_query` catches it and synthesizes the empty frame with the columns the callers expect (from `group_by` plus the metric name). That is a fix, not a special case: `_align_to_spine` has always specified the behavior ("a source returning no rows at all keeps the full fill for flows"), and every other provider reaches it because their drivers return an empty result *with* its schema. Only the CSV round-trip loses the columns. The warning matters — an all-quiet window and a filter that silently matches nothing are indistinguishable from here, which is the same reasoning as the interior gap-fill warning.

### `CloudDataFetcher`
Uses the `dbtsl.SemanticLayerClient` sync API; the Arrow result is converted to pandas and the `metric_time__<grain>` column renamed to `date`.

The correlated jaffle-shop dataset used by tests lives in `tests/synthetic.py` (`generate_mock_data`), not in production code.

### `snapshots.py` — `SnapshotStore` + `SnapshotFetcher`

A read-through cache **at the `BaseDataFetcher` boundary**: `SnapshotFetcher` wraps the real fetcher; a hit returns the stored frame without touching the provider, a miss fetches, writes, and returns. One parquet file per `(metric, grain, kind, window)` plus a human-facing `manifest.json` (provider class, fetched_at, rows, **`definition_sha`**). That last field is roadmap C16: the filename keys on the *window*, and the per-metric `sql:`/`bind:` block that actually determines the values was in neither the name nor the record, so editing a query to exclude refunds and restarting served the pre-edit numbers forever — while `query_provenance`, delegated straight through, displayed the *new* statement beside them. `definition_sha` is compared on read and a mismatch warns and refetches, so a hit can only be served when the statement that would be shown is the statement that produced it. It fingerprints the definition itself (window-independent, which the sliced path requires) rather than `query_provenance`, whose dbt output is generated per window and would turn every hit into a miss. Records written before the field existed are **served with one warning per file** naming `BREAKDOWN_REFRESH=1` — refusing them would break every committed snapshot dir and every snapshot-only deployment, which have no provider to refetch from; the sin in C16 was the silence, not the serving. Wiring lives in `api/main.py:_wrap_snapshots`, called in `lifespan` after `_build_fetcher`: mock is never wrapped; directory = `BREAKDOWN_SNAPSHOT_DIR` (`"off"` disables) or tree-adjacent `.breakdown/snapshots`; `BREAKDOWN_REFRESH=1` skips reads but still writes (one forced refetch pass). Failure-soft by design: an unwritable directory logs one warning and serves uncached (`/config` is read-only in the container, so `compose.yaml` mounts `./snapshots` and points `BREAKDOWN_SNAPSHOT_DIR` at it). Snapshots capture the **normalized** post-gap-fill frame, so what refits is byte-identical to what was originally served — and a tree whose metrics all have snapshots boots with the warehouse down. The doctor deliberately bypasses snapshots (it constructs raw fetchers) — its job is proving the provider path.

---

`local` is **superseded but not deprecated** (roadmap 2.13). It hands a metric
name to MetricFlow, which plans the SQL, so it serves what `dbt_sql.py` refuses
rather than approximates — cumulative metrics, offset windows, non-decomposable
aggregations, conversion metrics, `non_additive_dimension`. That gap is real
(2 of 24 and 8 of 86 metrics on two real projects), so there is no blanket
deprecation warning: `doctor._check_local_migration` runs the bridge against
the specific tree and either clears it to move or names the metrics that need
MetricFlow. Do not add a general warning without closing the gap first.

## `dbt_bridge.py`

Translates a dbt project's own `target/semantic_manifest.json` — written by
plain `dbt parse` on **dbt Core**, with no dbt Cloud, SL credential or plan tier
— into `BindingSpec` objects (roadmap 2.10). `translate(manifest)` returns a
`BridgeResult` of `bindings`, `formulas`, `skipped`, plus inferred `grains` and
`kinds`.

**The manifest models are in tree** (`dbt_manifest.py`), so the `dbt-bridge`
extra depends on nothing from dbt Labs. It used to parse with
`metricflow_semantic_interfaces`, which ships inside the `metricflow` wheel —
twelve transitive packages and a `<3.15` Python ceiling for one `parse_obj`
call. The manifest is a *resolved* JSON artifact (dbt has already expanded every
`ref()`, default and Jinja expression), so reading it is parsing a versioned
document, not reimplementing dbt's parser — a different thing from what
Sidemantic does with source YAML, and the reason this trade is defensible where
that one was not.

`metricflow` stays in the **dev group** as a differential oracle:
`tests/test_dbt_manifest.py` parses the same fixtures both ways and asserts every
field the bridge consumes agrees, plus that our reading survives MSI's own
normalisation. Schema drift fails a test instead of reaching a user. Never make
it a runtime dependency again without deleting that reasoning first.

⚠️ Unknown keys are **ignored**, which is exactly how the older
`dbt_semantic_interfaces` returned every new-spec metric with no aggregation
while validating cleanly. That is safe here *only* because `_translate_simple`
hard-fails when a metric resolves to neither `measure` nor
`metric_aggregation_params`, and every unknown aggregation and metric type is
reported rather than defaulted. The models cannot catch that class of drift;
the refusals must.

Both manifest shapes are supported and must stay so: the **classic** spec puts
the aggregation on a `measure` the metric points at, while the **new spec** (and
Fusion) drops the measure layer and puts it inline on
`type_params.metric_aggregation_params`, with the aggregated column mirrored on
`type_params.expr`. A metric resolving to neither is reported, not defaulted.
Note `is_private` — dbt's marker for metrics auto-created during the new-spec
migration — lives on `type_params`, not on the metric.

`ratio` and `derived` metrics become **formula candidates, not bindings**: a
MetricFlow ratio references two other *metrics*, so it maps onto a formula edge
whose parents carry their own bindings, which is both more faithful and exactly
the "fetch numerator and denominator separately" that ratio decomposition needs.
Candidate formulas are checked against breakdown's own `validate_formula` before
being accepted, because MetricFlow `expr` is raw SQL and breakdown formulas are
arithmetic over metric names — `mrr / nullif(subs, 0)` is a real example that
does not translate, and dropping the null guard would change behaviour at zero.

Everything untranslatable lands in `skipped` with a reason naming the construct
rather than raising, so one run reports every problem: aggregations with no
additive decomposition (`min`/`max`/`median`/`percentile`), `cumulative` and
`conversion` metrics, `non_additive_dimension` (its MIN/MAX filter is applied
per grain window, so it is query-grain-dependent), offset inputs, models with no
primary entity (nothing to assert the grain against), and granularities coarser
than `month`.

**`join_to_timespine` and `fill_nulls_with` are refused, and that refusal is a
fix rather than a limitation** (roadmap C15). Until 2026-08-12 the manifest
models didn't declare these fields — nor `filter` — and `_Node` sets
`extra: "ignore"`, so a filtered dbt metric translated into a *filterless*
`BindingSpec`, never appeared in `skipped`, and served the whole relation under
the governed metric's name, with `doctor` green because the grain assertion sees
one row per grain key either way.

Three things about the check are easy to get wrong and are pinned by tests:

- **`fill_nulls_with: 0` is falsy.** A truthiness test would let through the
  single most common value; the check is `is not None`.
- **An empty filter intersection is truthy.** `{"where_filters": []}` is a legal
  serialisation of *no filter* and a Pydantic model is truthy whatever it holds,
  so refusal keys on a non-empty predicate, not on the object.
- **dbt writes these in four places** — `metric.filter`, `type_params`, the
  measure input, and each metric input — depending on spec version and whether
  the manifest has been through dbt's flattening transform (which merges a
  measure input's filter up onto `metric.filter` while leaving the input
  carrying it). All four are checked; checking one is how the defect survived.

### Filter resolution (roadmap 2.17)

Filters are no longer refused wholesale. `_resolve_filters` turns a metric's
`filter:` into `BindingSpec.where`, and the invariant that makes that safe is
the whole design: **total resolution or skip.** Every predicate of every filter
a metric carries resolves to a column on that metric's own relation, or the
metric stays in `skipped` exactly where C15 left it. No partial translation, no
best effort, no dropped conjunct — so this is a strict superset of the refusing
behaviour, whose only failure mode is refusal.

⚠️ **`where_sql_template` is Jinja, not SQL.** dbt writes
`{{ Dimension('order__is_food_order') }} = true`, and `order__` is an *entity
link*, not a table alias: MetricFlow resolves it through the semantic graph into
either a column on the measure's own relation or a **join**, and which one it is
cannot be read off the string. That is why this is a resolver rather than a
field assignment, and why the field alone was the small part of the item.

`_resolve_predicate` works by substitution-then-verification, which is stricter
than it may look:

1. Every `{{ … }}` must be a plain `Dimension('<ref>')` with one string
   argument. A `TimeDimension`, `Entity`, `Metric`, or a `.grain('week')` call
   is quoted back in the skip reason.
2. `<ref>` resolves only through the model's **own primary entity**
   (`order__region`) or unlinked (`region`), and only to a **categorical**
   dimension. A foreign entity link is refused by name; so is a time dimension,
   because MetricFlow renders those at a declared granularity and whether it
   truncates here can only be *verified* ([2.14](../../knowledge/roadmap.md)),
   not reasoned about.
3. Each reference becomes a `bd_where_ref_N` placeholder, the text is parsed in
   the **target dialect**, and then **every `exp.Column` in the tree must be one
   of those placeholders**. This is what makes "total resolution" literal: a
   legacy raw-SQL predicate (`amount > 0`) is refused rather than qualified
   against the fact relation and hoped for, because the name a filter is written
   with need not be the column MetricFlow resolves it to.
4. A subquery, set operation or second statement refuses the metric.
5. A dimension whose `expr` is an expression (`is_paid: paid_at IS NOT NULL`) is
   spliced in **parenthesised**. sqlglot generates from the tree and adds no
   parentheses of its own, so `<ref> = TRUE` would otherwise become
   `NOT paid_at IS NULL = TRUE` — a different expression.

`translate(manifest, dialect)` therefore takes a dialect, and
`fetcher_from_project` resolves the profile *before* bridging so it has one.
A filter on a `ratio` or `derived` metric stays refused, and not for want of
machinery: those become formula edges over metrics referenced **by name**, and a
name carries no scope, so the edge would be over the unfiltered metric. That is
a modelling change, not a SQL change.

**`where` is the first import-only field.** §4.1 of the connectivity design
admits a new binding field only when `sql:` genuinely cannot express the thing;
`bind.sql` expresses every hand-written filter, so a `where:` an author could
write would be pure convenience. But the *importer* has no `sql:` escape hatch —
composing a SELECT around a manifest predicate means rendering the Jinja anyway,
and then the predicate is hidden inside a subquery where `doctor` cannot see it.
So the line moved from *which fields exist* to *which fields an author may
write*, under a rule with teeth: **an import-only field must be fully derivable
from the source artifact with no information from the author.** The moment one
needs a hint, an override or a disambiguation it is an authoring field again and
faces the stop rule in full. `MetricDefinition.check_bind` enforces it — manifest
bindings are constructed directly and never pass through YAML, so a `where`
arriving on a `MetricDefinition` was written by hand and is a parse error naming
`bind.sql`.

Ships in the `dbt-bridge` extra (`metricflow`, `sqlglot`) — deliberately *not*
the `dbt` extra, since it needs neither dbt-core, an adapter, nor the `mf`
binary. The `dbt` extra's `dbt-metricflow` floor is `>=0.13.0` precisely so the
two can coexist: 0.10.1 pinned `metricflow==0.208.1`, which predates MSI.

## `dbt_sql.py`

Compiles a `BindingSpec` into the one query the engine ever asks for:
`build_query(bind, grain=…, start_date=…, end_date=…, dialect=…, dimension=…)`
returns dialect SQL selecting `[date, value]`, or `[date, slice, value]` when a
dimension is named. `build_grain_assertion(bind)` returns the fan-out check.
sqlglot does the dialect transpilation; `dialect_for_adapter` maps a dbt adapter
type onto a sqlglot dialect and warns (rather than guessing) on an unknown one.

Three details are load-bearing, and all three are pinned by tests that **execute
the generated SQL against DuckDB** rather than matching strings — a builder that
emits plausible SQL with the wrong bound is exactly the failure class the engine
exists to avoid:

- **Weeks are forced to ISO Monday per dialect.** `DATE_TRUNC('week', …)` is
  Monday on DuckDB/Postgres and Spark's `TRUNC(…, 'WEEK')` is too, but BigQuery
  defaults `WEEK` to *Sunday* and Snowflake honours the session's `WEEK_START`.
  Either would shift every bucket's composition by up to six days while still
  producing exactly one label per week, so nothing downstream could detect it —
  and `grains.floor_period` would relabel the bucket to the previous Monday,
  landing it on the spine and hiding the shift completely. So BigQuery gets
  `ISOWEEK` and Snowflake a `DAYOFWEEKISO` offset, both session-independent.
- **BigQuery reverses `DATE_TRUNC`'s arguments, so *every* grain needs an
  override — not just the ones whose date part differs.** Its signature is
  `DATE_TRUNC(date_expression, date_part)`, with the part a bare keyword rather
  than a quoted string: the mirror of everyone else's `DATE_TRUNC('PART', expr)`.
  Because `_parse_dialect` reads in the **target** dialect, sqlglot never
  rewrote the portable form, so day and month grain emitted
  `DATE_TRUNC('DAY', col)` and BigQuery rejected the whole query with *"No
  matching signature for function DATE_TRUNC"*. Week worked only because its
  ISOWEEK override happened to be written the right way round. `_TRUNC_OVERRIDES`
  now spells out `day`, `week` and `month` for BigQuery.

  **The `CAST(… AS DATE)` in those overrides is load-bearing.** BigQuery's
  `DATE_TRUNC` takes a DATE; a TIMESTAMP needs `TIMESTAMP_TRUNC` and a DATETIME
  `DATETIME_TRUNC` — and a dbt `agg_time_dimension` is very often a TIMESTAMP, so
  the week override was wrong for the common case too. Picking the right function
  would need the column's SQL type, which nothing on this path has
  (`BindingSpec.time_column` is a bare string, the manifest's time dimension
  carries a granularity but no data type, and the value may be an arbitrary
  `expr` rather than a column name). The cast is correct for DATE, TIMESTAMP and
  DATETIME alike and needs nothing we don't have. It is free of both costs one
  might fear: partition pruning is unaffected, because the window predicates
  compare the **raw** column and not this expression; and UTC is the reference
  zone either way, since BigQuery's TIMESTAMP→DATE cast and `TIMESTAMP_TRUNC`
  both default to UTC, so no bucket differs from the type-aware form. Tests pin
  the argument order, the cast, every builder that truncates, and that the cast
  stays out of the window predicates.
- **The window bound is half-open on `end + 1 day`.** breakdown windows are
  inclusive, but `<= end_date` against a *timestamp* column drops everything
  after midnight on the last day — roughly 1/31 of a monthly figure, silently.
- **`agg: count` emits `COUNT(measure)`, never `COUNT(*)`.** MetricFlow's
  `count` is null-guarded (it desugars to `SUM(CASE WHEN x IS NOT NULL …)`), and
  `COUNT(x)` is exactly that. `COUNT(*)` would include rows the source excludes.

`agg: ratio` divides two separately-aggregated measures with a `NULLIF` guard,
so a zero denominator stays NULL and reaches `_align_to_spine`, which leaves the
period **undefined** rather than inventing a zero (roadmap 1.11 — it used to
refuse the whole series). `agg: last` raises
`UnsupportedBinding`: a stock's per-period last snapshot needs a window function
and, with an entity, the stock-and-flow treatment of the design doc §8 — so it
is refused rather than approximated, with `bind.sql` as the escape hatch.

Joins are many-to-one only and emitted as `LEFT JOIN`, so fan-out is
definitionally impossible; `build_grain_assertion` proves the claim by comparing
`COUNT(*)` against `COUNT(DISTINCT grain_key)`.

**`bind.where` is applied by `_bounded`, and every builder routes through it**
(roadmap 2.17). `_windowed` adds the window bounds, `_filtered` adds the
predicates, `_bounded` is both — and `build_query`, `build_resolved_slice_query`,
`build_entity_flow_query`, `build_multivalue_assertion` and
`build_grain_assertion` all call it. That is deliberate structure rather than
five remembered call sites: a filter applied to the total query but not to the
sliced one produces slices that do not sum, which the slicing maths reads as an
unexplained residual — a wrong *finding* rather than a wrong number, and harder
to spot than either. If you add a builder, route it through `_bounded`.

`_where_predicates` compiles the predicate through sqlglot's AST and never
pastes it as text, for two independent reasons. **Qualification:** `build_query`
LEFT JOINs a dimension table when a slice is requested, so an unqualified
`region` is ambiguous against `bd_dim.region` and resolves differently — or
errors — per warehouse; walking `exp.Column` and setting the fact alias is the
only fix, which is the same reason `_qualified` refuses to prefix anything that
is not a lone identifier. **Dialect:** the stored predicate is parsed in the
*target* dialect, the same discipline that closed the Spark `trunc` and quoted
`"date"` bugs. A predicate that does not parse, or that carries a subquery, set
operation or second statement, raises `UnsupportedBinding` here too — the bridge
already refused such a metric at import, and this is the same rule at the
builder for a binding that arrived some other way.

`build_filter_probe` is the one builder that deliberately **does not** apply the
predicate: it emits `COUNT(*)` against
`SUM(CASE WHEN <predicate> THEN 1 ELSE 0 END)` over the window, so `doctor` can
measure it. `CASE WHEN` matches `WHERE`'s three-valued semantics exactly — a
NULL predicate falls to `ELSE 0` — and `SUM` over an empty window is NULL rather
than 0, which the provider reports as "no rows here" rather than as
"everything dropped".

## `dbt_provider.py`

The `dbt` provider. Joins the three preceding pieces — manifest → binding → SQL
— to a connection resolved from the project's **own `profiles.yml`**, so the
practitioner supplies no new credentials. `fetcher_from_project(path, target=…,
profiles_dir=…, overrides=…)` is the entry point; `_build_fetcher` calls it for
`provider.type == "dbt"`.

It lives outside `data_fetch.py` on purpose: `dbt_sql` and `dbt_bridge` both
import from there, so a fetcher in `data_fetch` would close an import cycle.
`SnapshotFetcher` in `snapshots.py` is the same shape.

`connect` is a **zero-argument callable**, not a live connection, so the fetcher
constructs without touching the warehouse — a tree whose metrics all have
snapshots has to boot with the warehouse down, the same rule `LocalDataFetcher`
follows for `mf`.

`resolve_profile` reads `dbt_project.yml` for the `profile:` name, then that
profile's target from `profiles.yml` (searching `profiles_dir` →
`$DBT_PROFILES_DIR` → the project dir → `~/.dbt`). It renders `env_var()` and
**refuses any other Jinja** rather than passing a template through to a driver,
because `{{ var('x') }}` arriving as a literal password fails unreadably.
Connectors are one function per adapter (`bigquery`, `databricks`, `duckdb`,
`postgres`, `snowflake`), each importing its driver lazily and naming the package
to install — breakdown ships no warehouse drivers of its own beyond the
`databricks` and `bigquery` extras, since the driver a user needs is the one
their dbt adapter already depends on.

**`CONNECTORS` and `ADAPTER_DIALECTS` are separate maps, and the gap between
them is a real failure mode.** A dialect entry only means the *generator* emits
correct SQL for that warehouse; without a connector nothing can run it, and the
user is told to bind by hand and pick another provider. BigQuery sat in exactly
that gap from 2.10 until 2026-08-11 — correct SQL, including the ISOWEEK week
override, with no way to execute it — which pushed BigQuery shops onto `local`
and its `mf` subprocess. Redshift, Spark, Trino, Athena and ClickHouse are still
there. When adding one, add both halves or say which is missing.

BigQuery is the one adapter whose credential is selected by a profile `method`
rather than carried in fixed fields: `oauth` leaves `credentials=None` so the
driver finds Application Default Credentials itself, and the two service-account
methods differ only in whether the key is a path or inline. An unrecognized
method raises instead of falling back to ADC — quietly authenticating as
whoever the environment happens to be is worse than stopping. It connects
through `google.cloud.bigquery.dbapi` rather than the native client API so the
cursor satisfies `_frame` like every other connector; BigQuery populates
`cursor.description` from the result *schema*, so a zero-row result still
carries its columns and reaches `_align_to_spine` intact.

`fetch_metric_sliced` builds `[date, slice, value]` **directly** rather than via
`_sliced_long`, which finds its date column by looking for `metric_time` — a
MetricFlow name this provider never emits, because it names the column itself.

Slicing a **non-additive** binding logs why its slices will not sum: measured on
a real warehouse, `active_subscription_count` sliced by status gave 2,106
against an unsliced 2,069, because one subscription changing status inside a day
is counted once in the total and once per status. That is deduplication overlap
rather than an unexplained cause, and saying so is what keeps the slice panel
honest until roadmap 3.8 decomposes at the grain where the metric becomes a sum.

Both diagnostic queries — `build_grain_assertion` and
`build_multivalue_assertion` — take an optional window, and `doctor` passes its
probe window rather than scanning whole relations twice per metric. That makes
each a **sample**: fan-out and multi-valuedness are properties of the data, and
absence over seven days is not proof of absence, so the check result names the
dates it looked at (`_over`). Do not drop the label to tidy the output.

`check_grain(name)` runs the fan-out assertion; `check_filter(name)` returns
`(rows, kept)` for a filtered binding; `last_sql` records the statement behind
each number, which is the hook 2.11 reads.

**The grain claim runs post-filter** (2.17 §3.4). Fan-out is a property of the
relation, so filtering first is the *less* conservative check — and it is
conservative in the wrong direction: a `fct_order_lines` relation is one row per
order under `line_number = 1` and multi-row without it, so a pre-filter pass
would fail a binding whose every number is correct. The assertion protects the
aggregate this node computes, and that aggregate is the filtered one. What it
gives up (warning the author that the relation is unsafe if the filter is ever
widened) is covered by naming the predicate count in the check output; what it
structurally cannot catch (a mis-translated predicate still leaves one row per
grain key) is what `check_filter` is for.

**Entity-grain resolution (3.8 §4).** A binding with `entity_grain` slices
through `build_resolved_slice_query`, which collapses the relation to one row
per (entity, period) with a `ROW_NUMBER` window before grouping — so every
entity lands in exactly one slice and `Σ_g slices` is the distinct-entity count,
which *is* the metric. Proven numerically against DuckDB rather than asserted on
SQL strings: the naive slices come to 5 where the metric is 4, the resolved ones
to 4. `resolve: error` generates the plain query unchanged — it asserts
single-valuedness rather than correcting it, and `doctor`'s `entity grain
resolves` check is what holds the assertion to account via
`build_multivalue_assertion`. `slice_additivity` then answers `exact` rather
than `overlapping`, which is what stops the overlap warning and restores
contribution shares.

**Entity flows (3.8 §6).** `build_entity_flow_query` resolves each entity to one
slice per *window* and FULL OUTER JOINs the two sides, returning a transition
matrix `[reference_slice, analysis_slice, entities]`. `engine.slices.entity_flows`
classifies it: absent→g is new, g→absent is churned, g→g is retained, g₁→g₂ is
migration — and migration nets to exactly zero across slices, the same
reallocation property a rate's `mix_total` has. Absence is detected from the
joined key rather than a NULL slice, so an entity present with a NULL dimension
value stays distinguishable from one that was never there.

Two things the classification depends on, both learned by running it against a
real warehouse. **Never emit a quoted identifier as a column reference:**
`"date"` is an identifier on DuckDB and Postgres but a *string literal* on Spark
and BigQuery, so the outer SELECT returned a constant column of the word "date"
on Databricks — invisible locally, because DuckDB reads it the other way.
Internal projections use plain `bd_*` names and the public aliases are applied
once. And **presence only means membership if the relation is entity-per-period
grained**: on an event table an entity appears only where something happened to
it, so `entity_flows` reports `retention_share` and caveats below 5%.

This is a **diagnostic, not a decomposition**, and the code says so:
`reconciles_to_gap` is `False`, and the API attaches it best-effort so a failing
flow query can never cost the attribution. Flows are cached per
(metric, dimension, both windows) in `app.state.flow_cache` — they cannot share
the slice cache's key, which carries one window — and folded to the same
`top_k`/`values` as the attribution, since two panels disagreeing about which
slices exist reads worse than either alone. ⚠️ Folding both endpoints of a
movement would produce `__other__ → __other__`, which classifies as *retained* —
turning movement into stability, the opposite of the panel's purpose — so those
are re-tagged `__other_moved__` and stay migrations. Flows compare window-level *sets*,
and `mean_t |E_t|` is not `|∪_t E_t|`, so they do not reconcile to the
window-mean gap and must never be rendered as though they do.

`doctor`'s `check_dbt` walks the chain in the order failures actually cascade —
semantic manifest → dbt profile → warehouse connection → tree metrics bind →
declared dimensions exist → grain claims hold → filters narrow → entity grain
resolves — skipping the rest rather than reporting the same root cause six
times. Three of those are worth their place: **declared dimensions** turns a 500
on the first *slice by* click into a startup failure (the same too-late class as
C12); **grain claims** is the check no other semantic layer makes, since
MetricFlow and Cube accept declared relationships on trust; and **filters
narrow** (2.17 §8.2) is the same idea applied to an imported predicate —
`0 < kept < rows` proves the filter is live on *this* warehouse in *this*
dialect against *these* columns.

`filters narrow` is the reason `CheckResult` grew a fourth status. `kept == 0`
fails (an empty or all-zero series, the signature of a dialect-hostile
predicate) and a query error fails with the predicate quoted, but `kept == rows`
is genuinely ambiguous: either a vacuous filter over a seven-day probe window,
which must not block a correct tree, or a constant-true predicate, which is
C15's defect through a new door and must not read as clean. So it **warns** —
printed `[WARN]`, counted separately, and *not* reflected in the exit code,
which gates CI and deploys. Use `CheckResult.warn` only where both halves of
that are true; a warning nobody can act on is how a real one gets scrolled past.

`BaseDataFetcher.query_provenance(metric, dimension=None, *, grain, start_date,
end_date)` is the 2.11 surface, served by `GET /metrics/{name}/query`. It takes
the window because provenance must not depend on whether a fetch happened to run
in *this* process: a snapshot hit serves the number without executing anything,
and reporting "no query" there understates how defensible the number is, since
the binding still determines it exactly. `warehouse` returns the author's own
`sql`; `dbt` returns what ran, else generates for the loaded window and flags
`executed: false`; `SnapshotFetcher` delegates. `None` is a legitimate answer —
`mock` synthesizes and the semantic-layer providers never see SQL — so the
endpoint returns the provider's reason rather than an error.

**`provider_query_name`** (in `data_fetch.py`) replaced a ternary duplicated at
three call sites — startup fetch, sliced fetch, and doctor's fit-readiness check
— which had to be edited in all three to add a provider. Missing one shipped a
provider that worked at startup and failed on slice.

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

`FitResult` also carries **`summary_json`** — the memoized JSON-safe `az.summary` of its own trace, filled in on first request by the API's `_fit_summary`. The trace is immutable once fitted, so its summary is too. It is declared on the dataclass rather than attached ad hoc from outside for a specific reason: summarizing an 830-day trace costs ~1.1s, so adding `slots=True` here later would silently turn the memo off and put that second back on every `GET /metrics/{name}`, a defect no test would name.

### `compute_shapley(formula, parent_names, baselines, actuals, node=None) -> Dict[str, float]`

Pure Shapley enumeration (O(2ⁿ)): distributes `formula(actuals) − formula(baselines)` across parents; values sum to the gap exactly. `node` names the metric being attributed and is used **only** in the refusal message below, so callers that have it should pass it — `shapley_attribution` and `run_rca` both do, at all six call sites.

**`_MAX_SHAPLEY_PARENTS = 10`, and more than that raises.** The enumeration is O(2ⁿ) and RCA runs it six times per formula node (three exact games, three over the bootstrap replicates), all while holding the caller's per-tree lock — so the cost doubles per parent *and* serializes every other request behind it. End to end through `run_rca` on a developer laptop: 10 parents ~3.5s, 12 ~20s, 14 ~80s. Refusing is deliberate rather than degrading to a sampled or truncated Shapley value, which is a **different number** than the one the author asked for. The message names the node and prescribes the fix — split it into intermediate sums, which preserves the identity and keeps every attribution exact. The constant matches `_MAX_SOURCES` in `engine/simulate.py`, which caps the identical enumeration over scenario sources.

The coalition loop wraps its subtraction in `np.errstate(invalid/divide/over="ignore")`. `eval_formula` already silences numpy's warnings internally (they are *fatal* under its restricted globals — see `formula.py` below), but a formula that produced an `inf` there makes this subtraction `inf - inf` and warns here instead. A non-finite result is caught and reported by name in `shapley_attribution`, so the warning would only land in an operator's log beside a 422 that already explains itself.

### `summarize_trace(trace) -> pd.DataFrame`

`az.summary(trace, hdi_prob=0.95)`.

---

## `engine/rca.py`

All window-over-window attribution lives here.

### Window resolution and validation (both entry points)

Window args on both entry points are **keyword-only**, and the reference pair is optional. `resolve_reference_window(dag, data, target, analysis_start, analysis_end, reference_start, reference_end)` runs first: both refs omitted → the **matched adjacent block** from `grains.default_reference_window` (4× the analysis length, min 28 days, whole-week length when any node in `nx.ancestors(dag, target) | {target}` declares seasonality — `_reference_alignment` — extended to hold ≥ 1 whole period at the scope's coarsest grain, clamped to `GrainedData.date_start`; adjacency and rationale in [`knowledge/reference_window_design.md`](../../knowledge/reference_window_design.md)). Exactly one ref passed → `ValueError`. The resolved dates and a `reference_defaulted` flag are echoed top-level. The reference never affects the *fit* window — that is always all loaded history before `analysis_start`.

Two guards then run on the resolved dates, called by `shapley_attribution` and `run_rca` alike, because a window that is merely *wrong* rather than *empty* produces a plausible number:

- `_validate_windows(...)` — grain- and data-independent ordering: `reference_start <= reference_end < analysis_start <= analysis_end`. Overlap is an error, not a warning (a shared period counts as both the normal regime and the departure from it); an inverted window is rejected here rather than silently snapping to an empty one.
- `_validate_coverage(frame, node, grain, snapped_ref, snapped_an, lags)` — the snapped windows must lie *fully* inside the node's own grain frame. A window entirely outside the data already raised in `_window_values`; this catches the partial overlap, which silently averages whichever periods happen to exist. Lagged parents are checked against their shifted windows and reported with the parent, its lag, and the shifted dates — the caller never typed that window, so naming the one they did type would send them looking in the wrong place. Called per node in `run_rca` in the **pre-fit scope pass** (after the `window_shorter_than_grain` check, so grain mismatch still degrades gracefully rather than raising) — see step 0 of `run_rca` below for why it moved ahead of the fits.

### `shapley_attribution(dag, data, target, *, analysis_start, analysis_end, reference_start=None, reference_end=None)`

Symmetric per-period Shapley decomposition for a formula metric at the target's grain: each parent's attribution is `means + covariance_analysis − covariance_reference` (three exact games; both windows evaluated period-by-period), so `attribution` sums to `gap = actual − baseline` exactly and the per-parent parts are returned under `decomposition`. Windows snap to whole periods (`grain` + `effective_windows` in the response); a window with no whole period raises `ValueError`. This is the `GET /shapley` contract. Raises `ValueError` if the metric has no formula.

**Non-finite results are refused, not emitted.** If `baseline`, `actual` or any attribution value is not finite (in practice a zero denominator somewhere in a window), the function raises **`NonFiniteAttribution`** — a `ValueError` subclass, so the API still turns it into a 422 carrying the message. Emitting the NaN instead reaches Starlette's `allow_nan=False` encoder as an unhandled **500 with no diagnostic at all**, and over MCP turns every number in the node into `null`. Every field downstream (gap, shares, CIs, unexplained, every ranked-cause score) would inherit it anyway.

It is its own subclass rather than a bare `ValueError` so `run_rca` can degrade *this* condition to a per-node status without also swallowing the unrelated `ValueError`s the same call raises (an over-wide parent set, a window that misses the data).

`_nonfinite_diagnosis` builds the message in terms the analyst can act on: which parent series holds zeros or non-finite values, in which window, and on which dates (truncated at `_MAX_SHOWN_DATES = 5`, as `_align_to_spine` does). Deciding which parent *is* the denominator would mean interpreting the formula, so every zero-or-non-finite parent series is named and the reader picks.

### `run_rca(dag, data, traces, target, *, analysis_start, analysis_end, reference_start=None, reference_end=None, advi_draws=500)`

Root cause analysis over `nx.ancestors(dag, target) | {target}`. `traces` is the caller's cache (`app.state.traces` in the API); missing probabilistic fits are added to it in place (ADVI, `fit_end=analysis_start`, keyed `(node, analysis_start)`).

0. **Resolve and validate every node's scope first, before any fitting.** One pass over the sorted scope builds `scoped[node] = (grain, frame, snapped_ref, snapped_an)` and calls `_validate_coverage` there. Coverage used to be checked per node *inside* the attribution loop, which runs after the fits — so a window outside the loaded data paid for an ADVI fit of every ancestor (minutes, holding the caller's lock, leaving a cached trace each) and only then 422'd. A window holding no whole period at a node's grain is **not** a coverage failure: that node gets `(grain, frame, None, None)` and is reported with a status below, exactly as before.
1. **Fit what's missing.** Probabilistic (non-formula, non-root) nodes in scope without a trace are fitted with ADVI — skipped when their windows hold no whole period at their grain.
2. **Per-node attribution at the node's own grain.** Each node reads its snapped windows from `scoped`. Every record is built by **`_node_out(**fields)`**, which starts from a template with **every key present and null** and lets the caller override what it knows — so a node that was skipped or failed answers the same shape as one that was attributed, and consumers (the UI, the MCP compaction) branch on `status`, never on which keys happen to exist. Nodes report `status`, `status_reason`, `grain`, `effective_windows`, `baseline`, `actual`, `gap` (mean-per-period at the node's grain), `relative_change` (None if `|baseline| < 1e-12`), `ci_status`, `window_aggregate` + `window_aggregate_reason` (`rate_window_method`: `components` for a real `Σnum / Σden`, else `period_mean_none_exists` | `period_mean_undeclared` | `period_mean_weights_unavailable` — same arithmetic, three different facts, and the reason is the tree author's own words for the first. Both windows are asked and the fallback wins any disagreement, since a payload claiming `components` while one of its two numbers is a period mean would misdescribe its own arithmetic. `null` on a flow or a stock), plus:
   - **Formula node** → `attribution_method="shapley"`: the three-game decomposition, bootstrapped with the grain's block length (`BOOT_BLOCK`: day 7, week 4, month 2) for `ci_95`/`prob_same_direction`; single-period windows withhold CIs (`ci_status: "degenerate_single_period"`). Each contribution carries `decomposition: {means: {estimate, ci_95}, comovement: {estimate, ci_95}}` (parts sum to `estimate` exactly per replicate) and the node carries `interaction` (summed co-movement shift + CI) — the data behind the UI's Headline/Detailed views. `unexplained = gap − shapley gap` (measurement residual only) with `unexplained_status: "measured"` — **unless the node is derived**, in which case it is exactly `0.0` with `unexplained_status: "definitional"`: its series *is* the formula, so the residual is zero because nothing was checked, not because a check passed (roadmap 1.11a). Where the node is a `kind: rate` with `formula: num / den` over its declared denominator (`aggregates_from_components`), the window value is the formula of the window aggregates, so the decomposition is the window-means bridge alone: `aggregation: "components"`, and both the per-contribution `decomposition` and the node's `interaction` are **absent** rather than reported as zero.
   - **Probabilistic node** → `attribution_method="posterior"`: `arr = trace.posterior["beta_raw"].reshape(-1, n_parents)`; for parent `i`, `samples = arr[:, i] * bootstrapped parent delta` → `estimate` (mean), `ci_95` (2.5/97.5 pct), `prob_same_direction`. Window period-starts map to the fitted index via `steps_between(dates, fit.dates[0], grain)`; lagged parents measure their delta over windows shifted back by `shift_periods(·, −lag, grain)` (whole periods, correct across month/year bounds), and each lagged contribution (both attribution methods; `shapley_attribution` carries a top-level map) reports `lag` + `parent_windows` — the shifted `{reference, analysis}` windows, the dates to narrate the parent with and to reuse for follow-up analysis. Both keys are absent entirely on unlagged contributions, so unlagged responses are unchanged. Trend/seasonal deltas are reported in `components`. `unexplained = gap − Σ estimates − trend − seasonal`. Single-period windows flag `ci_status: "posterior_only_single_period"`. Posterior nodes also report `fit_window: {start, end, n_periods}` (what the model actually trained on — all loaded whole periods before `analysis_start`, never the reference window) and `seasonality_warnings` (the fit's identifiability diagnostics, previously log-only); both are `null` on formula/root nodes.
   - **Root node** → `attribution_method=None`, empty contributions, `unexplained=None`.
   - Every contribution carries `share_of_gap = estimate / gap` (None if `|gap| < 1e-12`).
3. **`ranked_causes`** (documented heuristic): `score[target]=1.0`, propagated in reverse topological order; `score[p] += score[c] * min(|share_of_gap|, 1.0)`. All scoped nodes except the target, sorted desc, each `{"metric", "score", "via"}`. Scores (not raw gaps) are the cross-grain-comparable quantity. ⚠️ The weight is `0.0 if share is None or not np.isfinite(share)`: a **non-finite share slips straight through `min(abs(share), 1.0)`** because NaN compares false against everything, and one NaN term then poisons the score of every ancestor above it — the whole ranking, from one node. An undefined share carries no evidence about influence, so it weighs nothing, exactly like the `None` case.

### Per-node `status` — one bad node does not end the analysis

Every node in scope carries a `status`; anything other than `"ok"` reports the node **without attribution** and lets the rest of the tree through, with the engine's own diagnostic in **`status_reason`** (`null` when `ok`).

| `status` | Cause | What survives on the node |
|---|---|---|
| `ok` | — | everything |
| `window_shorter_than_grain` | the windows hold no whole period at the node's grain | `grain` only |
| `fit_failed` | the node's own `fit_metric` raised — overwhelmingly a series with **no variance across the fit window** (a parent held flat, e.g. a seasonal business whose default state is zero), which cannot be normalized | `grain`, `effective_windows`, `baseline`, `actual`, `gap`, `relative_change` — these are read off the data, not the model, so only the attribution is missing |
| `attribution_failed` | `shapley_attribution` raised `NonFiniteAttribution` for this node (a zero denominator over these windows) | as above, plus `attribution_method` |

Both new statuses replace a whole-analysis abort: **one unfittable node used to end the RCA and return nothing**. The `try` around `fit_metric` wraps that single call and nothing else, so unrelated failures elsewhere in the loop still surface; failures are collected into `fit_failures: Dict[str, str]` and consumed in the attribution loop.

**The RCA target is the exception for `attribution_failed`.** The whole response is about that node, so an empty answer for it is no answer at all — `NonFiniteAttribution` on the target is re-raised and becomes a 422 carrying the diagnostic.

### `ci_status`

Independent of `status`, and reported per node:

| `ci_status` | Meaning |
|---|---|
| `ok` | intervals computed normally |
| `degenerate_single_period` | formula node, single-period window — the block bootstrap would return identical replicates, so intervals are withheld rather than reported at a falsely-zero width |
| `posterior_only_single_period` | the same for a posterior node: coefficient uncertainty remains, the window-sampling component is absent |
| `nonfinite_bootstrap_replicates` | at least one interval on this node was computed from a **subset** of the replicates, or withheld because fewer than `_MIN_CI_REPLICATES = 100` survived |

`nonfinite_bootstrap_replicates` exists because individual replicates can come out non-finite where the *exact* decomposition did not — a resampled denominator mean can land on ~0 even when no single period is zero. NaN propagates through `np.percentile` into Starlette's `allow_nan=False` encoder as an unhandled 500 (and into `null`s over MCP), so the `_finite`/`_ci` helpers drop those replicates and report on what survives, withholding the interval entirely if too few do — the same posture as `slices._excess_fields` and as `single_period`. **The point estimates are unaffected**: they are the exact Shapley values, never bootstrap means. `prob_same_direction` is computed over the surviving replicates too, or `None` when the interval was withheld.

**No direction probability is published at exactly 1.** `rca.prob_same_direction(samples, n_effective=None)` is the one estimator behind all three of them — `prob_same_direction` (both attribution paths), `slices.prob_concentrated`, `simulate.prob_direction` — and it caps a saturated count at the resolution the sample actually has, `1 − 1/n`. A proportion over `_N_BOOT = 500` replicates has nothing between 0.998 and 1, so `1.00` was the estimator running out of resolution rendered as certainty, most often exactly where the evidence was thinnest. `direction_fields(samples, key, n_effective)` builds the payload pair: the capped value plus `<key>_censored: true`, present only when it saturated (the `lag`/`parent_windows` idiom), so uncensored responses are byte-identical to before and both renderers can print the bound (`>99.8%`) rather than a value. `n_effective` is the coarser factor where the sample size overstates the information: the posterior path passes `_N_BOOT` (its `samples` is one value per posterior draw, but the window delta multiplying them takes only 500 distinct values), and `run_scenario` passes the smallest posterior draw count behind any propagated coefficient (`beta_draws` resamples *with replacement*, so 2,000 draws from a 500-draw ADVI fit add no information about the sign). A sample with no spread at all is exempt and keeps its honest `1.0` — an exact propagation through an identity from a pinned intervention is not an estimate of a proportion.

`window_mean(data, col, start, end)` is the shared helper (inclusive bounds; raises on empty window).

Response contract: `{"target", "reference_window", "analysis_window", "reference_defaulted", "nodes", "ranked_causes"}` — the top level echoes the *resolved* windows (identical to the requested ones when both refs were passed); snapped ones are per-node.

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
| `BREAKDOWN_DEFAULT_TREE` | `--default-tree` | the only tree, else the alphabetically first |
| `BREAKDOWN_EAGER` | `--eager` | unset (a directory of trees loads lazily) |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` |
| `BREAKDOWN_HOST` / `BREAKDOWN_PORT` | `--host` / `--port` | `127.0.0.1` / `9090` (also read by MCP deep links + transport security) |
| `BREAKDOWN_SNAPSHOT_DIR` | `--snapshot-dir` / `--no-snapshots` | tree-adjacent `.breakdown/snapshots`; `"off"` disables |
| `BREAKDOWN_REFRESH` | `--refresh` | unset (skip snapshot reads for one pass, still write) |

Not settable from a flag — deployment concerns with no laptop equivalent:

| Env var | Default | Read by |
|---------|---------|---------|
| `BREAKDOWN_API_TOKEN` | unset | the bearer gate (below) and `/dag`'s `sql`/`bind` redaction |
| `BREAKDOWN_REQUIRE_AUTH` | unset | the bearer gate — extends it from `/mcp` to every non-open route |
| `BREAKDOWN_MAX_TRACE_BYTES` | `512 * 1024 * 1024` | `trees._byte_budget()`; `0` disables the byte bound, leaving only the entry count |
| `BREAKDOWN_PUBLIC_URL` | `http://127.0.0.1:$BREAKDOWN_PORT` | `mcp/shaping.py`'s deep links |

Dates are validated (ISO format, start ≤ end) both at the CLI and in `lifespan`. `load_tree(tree)` builds the fetcher from the tree's `provider` config and fetches every metric for the window **at its declared grain/kind**, assembling per-grain frames via `build_grained` (inner join on `date` within each grain only — a monthly metric no longer drops daily rows tree-wide). For `local`/`cloud` the queried metric name is the last segment of `source`, renamed to the tree `name`; mock generates by tree name directly.

`build_grained` then requires each grain frame to be a **gap-free run of periods** (`_check_contiguous`, grain-aware via `_FREQ`), raising with up to 10 named missing dates plus a count. This is not tidiness: everything downstream indexes by position — the model's `t = arange(len(y))` dates the rows, lags shift by *rows*, and the bootstrap resamples contiguous runs — so a hole compresses the calendar and silently shifts every date rather than failing. Periods dropped by the inner join (present for only some metrics) are logged as a warning even when the survivors stay contiguous.

**Cold-start startup (`provider: none`):** `load_tree` fetches nothing — the tree's `data` stays `None` with no `startup_error`; a stated mode, not degraded. Readiness is checked up front (`validate_cold_start`); missing declarations raise into the degraded path with the full blocker list. Time-series routes (`/series`, `/analyze`, `/shapley`, `/rca`) reject via `_require_data` (422 pointing at `/simulate`); `/meta` reports `mode: "cold_start"` with null window; `/metrics/{name}` serves the definition with an empty series; `/simulate` passes `data=None` through to the engine's cold-start branch unchanged.

**Startup cost and the deferred inference stack.** `pymc`/`arviz`/`pytensor` are imported *inside* the five functions in `engine/model.py` that use them, never at module scope, because `engine.model` sits on `api.main`'s import path and those three are ~80% of the process's import time (2.2s of 2.5s locally). Uvicorn cannot bind the port until module import finishes, so on a shared-CPU VM that was a ~43s boot — past the point where Fly's proxy gives up and returns **503**, which is how the public demo greeted the first visitor after every idle period. Deferring them takes local `import breakdown.api.main` to ~1.0s. `lifespan` then starts a daemon thread running `warm_inference_imports()` *after* the data load, so the cost lands between the page rendering and the first analysis rather than on the first *Run analysis* click — moving it there would have been worse than the slow boot. Three tests pin the arrangement (`test_api_import_does_not_load_pymc`, `test_warm_inference_imports_actually_loads_the_stack`, `test_lifespan_warms_the_inference_stack_in_the_background`); one convenient top-level `import pymc` silently undoes it. `_PRIOR_DISTRIBUTIONS` is a frozenset of *names* rather than PyMC classes for the same reason, resolved via `getattr(pm, ...)` at its one call site.

**Degraded startup:** the parse and the load are each wrapped in a try/except, and the failure is recorded **per tree** on `TreeState.load_error`. The app still serves — that tree's data routes reject via `_require_ready` (503 with the error + a `breakdown doctor` hint), MCP tools reject the same way in `_state()`, the UI shows a banner, and *the other trees are unaffected*. `/ui`, `/`, `/trees` and `/health` keep working; a container never crash-loops on a bad token. A failure with no tree to hang it on — `--tree` naming nothing loadable — is the one global case (`app.state.discovery_error`), and then `_tree()` 503s rather than 404ing, since "there is no tree" is not "you asked for the wrong one". Per-metric diagnosis is deliberately not here — that's `doctor.py`'s job.

Static files and the default tree resolve via `importlib.resources` (`files("breakdown")`), not repo-relative paths, so an installed wheel behaves like a checkout.

### The bearer-token gate (`bearer_token` middleware)

Two levels, both opt-in through the environment, both enforced in **one HTTP middleware** rather than per route.

- **`BREAKDOWN_API_TOKEN` alone gates `/mcp`.** The MCP endpoint runs whole analyses, so exposing it off loopback without a gate hands anyone who finds the URL the tree and its data. Unset (the laptop default) keeps the loopback workflow friction-free; set closes that one surface and nothing else, which is what existing deployments already depend on.
- **`BREAKDOWN_REQUIRE_AUTH` extends the same check to every route** but `_OPEN_PATHS`/`_OPEN_PREFIXES`.

**Gating in the middleware rather than per route is what keeps the two mounts from drifting.** The router is included twice (bare and under `/trees/{tree_id}`), but the middleware sees one *resolved* path — so an alias cannot be gated differently from the route it aliases. A test enumerates every data route in both mounts against exactly that risk.

Four details are load-bearing:

- **The open list is an *allow*-list, so the gate fails closed.** `_OPEN_PATHS = {"/", "/health"}` and `_OPEN_PREFIXES = ("/ui",)`; a route added tomorrow is gated by default rather than open until someone remembers it. That is why `/openapi.json` and `/docs` are gated. `/health` is open because `compose.yaml`'s healthcheck calls it with no credentials and orchestrators can't present one — gating it makes a correctly configured deployment look dead. `/ui` is a JS bundle, not data; its **fetches** are gated, which is the intended consequence: this mode assumes a reverse proxy injecting the header, or an operator who accepts that the browser needs one. A login, a cookie or a token-in-the-URL would be hosted mode (roadmap 3.5) and is deliberately not built here.
- **`_under(path, prefix)` matches on path-segment boundaries.** `path.startswith("/mcp")` also matches `/mcphony`, which is the wrong shape of test for a security decision even when no such route exists today: the day someone adds `/metadata`, a `startswith("/meta")` open-list would hand it out. Only `/mcp` itself and genuine children match.
- **`_require_auth()` treats anything but an explicit off as on.** `_AUTH_OFF_VALUES = {"", "0", "false", "no", "off"}`, compared case-insensitively after stripping — so `BREAKDOWN_REQUIRE_AUTH=ture` closes the door rather than opening it.
- **`_presents_token` compares *bytes*, not `str`.** `hmac.compare_digest` raises `TypeError` on a `str` containing non-ASCII, so a header of `Bearer sécret` used to be a **500 from inside the middleware** rather than the 401 every other wrong token gets — a trivially reachable error-page-vs-401 oracle. Starlette decodes header values latin-1 (HTTP's byte-to-str mapping), so latin-1 is the exact inverse and round-trips the bytes the client actually sent; the token comes from the environment, which Python decoded utf-8. A value that cannot round-trip is not a token we issued, so it compares as empty rather than raising — still one constant-time comparison, on the same path.

**`_auth_config_error()` names the one configuration that must not be served:** `BREAKDOWN_REQUIRE_AUTH` set with no `BREAKDOWN_API_TOKEN` would check every request against an empty secret and pass everything — it fails *open*. So non-open routes return **503**, `lifespan` records it on `app.state.auth_error` and logs it loudly, and `startup_error` reads it so `/health` reports `degraded` with the reason. Same degraded-startup discipline as a bad provider credential: loud, diagnosable, not a crash-loop with the reason only in a log that scrolled past.

`app.state.startup_error` is consequently a **three-way** composite, in this order: `auth_error` (the operator must fix this before anything else the process says about itself matters) → `discovery_error` (`--tree` named nothing loadable, so there is no tree to hang it on) → the default tree's own `load_error`.

**`/dag` redaction is separate from the flag.** Whenever `BREAKDOWN_API_TOKEN` is set and the caller doesn't present it, `_SENSITIVE_DEFINITION_FIELDS = ("sql", "bind")` are replaced with `None` in each node's dump. `/dag` is open by design — the UI is unauthenticated and needs the shape to draw anything — but on a deployment that bothered to configure a token, "the graph is public" should not also mean "our fully-qualified table names and WHERE-clause logic are public". Redacted to `null` rather than dropped, so a client reading `def.sql` sees an absent query rather than a `KeyError`. Unset (the laptop default) behaves exactly as before, and the UI's *show query* panel reads `GET /metrics/{name}/query`, not this route, so it loses nothing.

This is a down payment on hosted mode (roadmap 3.5), not a substitute: one shared secret, no per-user identity, no revocation short of a redeploy.

### State: one `TreeState` per tree (roadmap 2.16)

A process serves **several** trees, and they are peers: a wide tree with revenue at the top, a marketing tree detailing channels and campaigns, a product tree about feature adoption and retention, a tree standing behind a target — any of them durable or disposable, any with a goal or without. So everything that used to sit directly on `app.state` is per-tree and lives on a `TreeState` in `app.state.trees[id]` (`api/trees.py`). Design spec: [`multi_tree_design.md`](../../knowledge/multi_tree_design.md).

| `TreeState` field | Type | Description |
|-----------------|------|-------------|
| `id` / `path` | `str` | The filename stem and the file. The id is never declared in YAML: two files could then claim one, and a filename collision is impossible |
| `meta` | `TreeMeta \| None` | The parsed `tree:` block (title/description/owner/period/goal) |
| `parser` | `Parser \| None` | Parsed metric tree (`None` if its YAML failed to parse) |
| `fetcher` | `BaseDataFetcher \| None` | Fetcher matching the provider type |
| `data` | `GrainedData \| None` | Per-grain frames + `grain_of`/`kind_of`/`last_observed` maps (`last_observed` is captured per metric before the within-grain join; `data_through(m)` converts it to the inclusive last covered date) |
| `load_error` | `str \| None` | This tree's parse or load failure; gates its data routes only |
| `loaded` / `loading` | `bool` | Drive `state` (`loaded` \| `loading` \| `not_loaded` \| `error`) on the index |
| `traces` | `TraceView` | This tree's view of the shared trace cache, keyed `(name, fit_end)` — the engine's own key |
| `slice_cache` | `BoundedCache` (64) | On-demand sliced frames, keyed `(metric, dimension_source, grain, start, end)` — deliberately separate from `GrainedData` |
| `flow_cache` | `BoundedCache` (64) | Entity-flow transition matrices, keyed by a *pair* of windows |
| `lock` | `asyncio.Lock` | Serializes sampling (analyze + RCA fits) **on this tree** |
| `earliest` / `earliest_task` | `Dict[str, str \| None]` / `Task` | Background history discovery |

App-wide state is what genuinely isn't a tree's: `trees`, `default_tree`, `trace_store`, `discovery_error`, `auth_error`, and `progress`.

Four things about this shape are load-bearing:

- **The lock is per-tree.** One global lock is right when there is one tree and one trace cache; with eight it would make an RCA on the revenue tree wait behind a simulation on an unrelated marketing tree, for no reason — the caches they mutate are disjoint. (The `waiting` progress stage stays meaningful: it now means "queued behind another run *on this tree*".)
- **The trace cap is global, and it is a byte budget.** `MAX_CACHED_TRACES = 256` *per tree* would be 256 × N `InferenceData` objects, each holding every posterior draw. One `TraceStore` keyed `(tree_id, metric, fit_end)` is shared, and each tree gets a `TraceView` — a `MutableMapping` speaking the engine's `(metric, fit_end)` key — so `fit_metric` stays a pure function and nothing in `engine/` learns that more than one tree exists. `TraceView.__iter__` snapshots with `list(...)` before filtering, for the same reason `/meta` does (C8): a lazy generator over a dict a worker thread is inserting into raises "dictionary changed size during iteration".

  ⚠️ **An entry count cannot bound memory, because an entry's size scales with the loaded window.** One ADVI fit (1000 draws) of the demo tree's `order_count` over an 830-day window measures **13.4 MB** of posterior, so 256 of them is ~3.4 GB against `demo/fly.toml`'s `memory = "2gb"` — and tuning the count down just moves the cliff to a wider window. So the real bound is `MAX_CACHED_TRACE_BYTES` (512 MiB, overridable with `BREAKDOWN_MAX_TRACE_BYTES`; `0` disables the byte bound), with the count kept as a secondary backstop against a pathological number of tiny fits. Eviction is insertion-ordered — oldest first — until **both** bounds fit.

  Four details: `_trace_nbytes` sums `nbytes` across the `InferenceData`'s groups, which reads the arrays' own shape/dtype metadata and **touches no data** (the honest alternative, `pickle.dumps`, materializes a second full copy of the very object we are trying not to hold two of); an unknown shape measures 0 and is bounded by the count alone. `__setitem__` calls `_forget` before inserting, since a refit must re-insert at the end *and* drop the old entry's bytes rather than overwriting in place. `_evict` stops at `len > 1`: **the newest entry is never evicted**, because the caller is holding it and about to serve from it, and a single fit larger than the whole budget should degrade to "cache of one", not "cache of none". A non-integer `BREAKDOWN_MAX_TRACE_BYTES` warns and falls back to the default; a negative one clamps to 0.

- **The two per-tree frame caches are bounded too**, by `BoundedCache` — a `dict` subclass whose only added behaviour is evicting the oldest entry on write past `max_entries` (`MAX_CACHED_SLICES` / `MAX_CACHED_FLOWS`, 64 each). Both are keyed by *caller-chosen windows*, so on a public deployment where every visitor picks their own they grew without limit and nothing ever evicted from them. Counts rather than bytes is right here: a cached slice frame is one metric by one dimension over one window — 9,648 rows measured 435 KB, two orders of magnitude under a trace — so 64 of each per tree is a few tens of MB at worst.
- **`progress` is not tree state.** Run ids are already unique, and a poller shouldn't need to know which tree it is watching — which is why `GET /progress/{run_id}` is the one data route with no tree-scoped form.
- **`app.state.<attr>` still reads and writes the *default* tree's.** `BreakdownState` (a `starlette` `State` subclass) aliases every field above onto `app.state`, which is what keeps the MCP server, the README's examples and the whole test suite addressing the same attributes they always have. `app.state.startup_error` is the one composite: `auth_error or discovery_error or default.load_error`.

**Lazy loading (§5.1).** Boot parses **every** tree's YAML (cheap, no I/O beyond the file) and fetches **none**, so `GET /trees` is a complete, instant index without touching a warehouse. A tree's data loads on the first request that needs it, in `_ensure_loaded` — under that tree's lock, off the event loop, with the double check inside the lock so two viewers opening the same cold tree don't both fetch. A tree that failed to load keeps its `load_error` rather than being retried on every request, which would hammer a down warehouse once per click. The exception is a **single-file** `--tree`, which loads eagerly: lazy buys nothing with one tree, and the port being up should mean the data is too (`_eager_trees`; `--eager` asks for the same from a directory).

### Routes

**`GET /health`** — always 200: `{status: "ok", provider, metrics}` or `{status: "degraded", error}` (the error being `startup_error`, so an auth misconfiguration surfaces here too). Liveness for orchestrators (the body, not the code, carries degraded-ness) and the UI's first request. Reports on the **default** tree, like every unprefixed route; the per-tree view is `/trees`. One of the three paths that stay open under `BREAKDOWN_REQUIRE_AUTH` — orchestrators cannot present a credential.

**`GET /trees`** — the index's data source: `{default, trees: [{id, title, description, owner, period, goal, provider, metric_count, state, load_error, progress}]}` (`period` and `goal` are null on the trees that declare none). Answers from parsed YAML alone and **never triggers a load** — the lazy loading above is worthless if the index pays for it. `progress` is `{current, target, as_of}` for a loaded tree that declares a goal and `null` otherwise — including for the many trees that declare none, which is normal rather than a gap. The pairing with `state` is what keeps it honest: `progress: null` + `not_loaded` means *we haven't looked* and must not render as a zero. `current` is read at the tree's own data edge (the anchor the node cards use, only periods fully completed by it), so the index agrees with what the tree itself shows.

**`POST /trees/{id}/load`** — explicit load behind the index's *Load* button; returns the updated card. A second caller arriving mid-fetch waits on the same lock rather than starting a second one.

**Tree-scoped routes.** Every data route below is registered **twice** from one `APIRouter`: bare, and under `/trees/{tree_id}`. Handlers never see the id — `_tree(request)` reads it off `request.path_params` — so there is exactly one implementation of each endpoint and the aliases cannot drift from the routes they alias. The bare paths mean the default tree, which is what keeps existing deep links, the README's curl examples, the MCP tools and the test suite working unchanged. A path prefix beats `?tree=` or a header: cache-friendly, unambiguous in logs, and a shared URL is self-describing.

`test_project_invariants.py` asserts the mounting itself — every route on the shared `router` reachable at both its bare and its `/trees/{tree_id}` path — by enumerating `router.routes` rather than listing the endpoints, so a route added tomorrow is covered on the day it is added. **Check that kind of property against `app.openapi()`, never by walking `app.routes`.** `app.routes` is not a flat list of routes: since FastAPI **0.137.0**, `include_router` appends one lazy `_IncludedRouter` node per include instead of copying each route into the parent, so a walk counting `.path` there sees two pathless objects where it used to see twenty. That is an upstream representation change and nothing more — routing, the OpenAPI schema and the operation ids are identical across it (measured 0.135.2 → 0.141.1, and independent of the starlette version) — but it reads exactly like an app that lost every data route, and it briefly did.

**`GET /meta`** — `mode` (`"fitted"` | `"cold_start"` — which surface the UI should boot), metrics, data window (null in cold start), provider, per-metric `grains`/`kinds`/`data_through` maps (`data_through` = each metric's honest data edge, which may lag the requested window), `earliest_available` (per-metric earliest provider date from the background discovery task — `{}` until it fills, null per metric when the provider can't say; drives the UI's "history exists before --start-date" nudge), fitted list (UI bootstrap).

**`GET /dag`** — nodes (`[name, definition.model_dump()]`) and edges. When `BREAKDOWN_API_TOKEN` is set and the caller presents no valid token, each definition's `sql` and `bind` are replaced with `None` — see the bearer-token gate above.

**`GET /series`** — every metric's series at its native grain: `{metrics: {name: {grain, dates, values}}}` (mixed grains have no shared date axis, so dates are per-metric); hydrates the UI's node cards in a single request (NaN → null).

**`GET /metrics/{name}`** — definition, time series, and posterior summary via `summarize_trace` (non-finite values serialized as `null`).

The summary goes through **`_fit_summary(fit)`**, which memoizes it on the `FitResult`'s own `summary_json` field and runs under `asyncio.to_thread`. `az.summary` is the one heavy engine call this route makes and it scales with `draws` — 1.1s on an 830-day ADVI trace at 1000 draws, and the UI's box goes to 5000 — yet nothing memoized it, so it was paid on **every** GET, and `clearRCA` in `app.js` re-fetches every fitted metric after wiping its own cache, issuing N of these back to back. The trace is immutable once fitted, so the answer is too; caching it on the `FitResult` collects it with the fit it describes rather than leaving it to outlive it in a side table. Even memoized it stays off the event loop — it must not be the thing that decides whether `/health` answers.

**`GET /metrics/{name}/query`** — the roadmap-2.11 provenance surface, over `BaseDataFetcher.query_provenance` (see `dbt_provider.py` above). Optional `dimension` selects the sliced query. `sql: null` is a legitimate answer carrying the provider's own `note` (mock synthesizes; the semantic-layer providers never see SQL), and `executed` distinguishes the statement that ran from the one that *would* run for the loaded window — a snapshot hit serves the number without executing anything. 404 for an unknown metric or an undeclared dimension; `_require_ready` 503s a tree that didn't load, since provenance still needs a parsed tree.

**`POST /analyze/{name}`** — `inference_method` (`nuts`|`advi`), `draws`, `tune` (50–5000). Runs `fit_metric` via `asyncio.to_thread` under the lock; stores the trace in `app.state.traces`.

**Every date parameter is an `IsoDate` / `OptionalIsoDate`**, not a `str`: `Annotated[str, AfterValidator(_iso_date)]`, so FastAPI 422s a malformed date in request validation before the handler body runs. `str` was not a date type and `pd.Timestamp(value)` was not a validator — `pd.Timestamp("")` is `NaT`, which satisfies the annotation, passes every `is None` guard and reaches `snap_window`, where `NaT.normalize()` is an `AttributeError` and a **500** for any client submitting a cleared date field (`banana` raised and correctly 422'd, which is why it went unnoticed). `/analyze` and `/rca/{name}/slices` had run the ISO check inline and their siblings had not; it is one annotated type now, and `tests/test_project_invariants.py` enumerates the routes to check it. Write the `Query(...)` **inside** the `Annotated`, never as the default value — FastAPI rebuilds the field from a default `Query` and silently drops the validator with it. `ScenarioRequest.baseline_start`/`baseline_end` carry the same check as a `field_validator` (the body half), and `grains.to_date(value, label)` refuses `NaT` inside the engine so a non-HTTP caller (the MCP tools) gets a `ValueError` naming the parameter instead of an `AttributeError`.

**`GET /shapley/{name}`** — analysis params required, reference params optional (omit both → engine default; exactly one → 422); thin wrapper over `rca.shapley_attribution`. 422 if no formula or bad windows.

**`POST /rca/{name}`** — analysis params required, reference params optional (same rule). Runs `run_rca` via `asyncio.to_thread` under the lock, passing `app.state.traces` directly — on-demand fits land in the cache with no copying. 404 unknown metric; `ValueError` → 422. Optional `run_id` opts into progress reporting (below).

**`POST /rca/{name}/slices`** — `dimension` + analysis params required, reference params optional (resolved in `_run_slice` via `resolve_reference_window`, since `slice_attribution` keeps concrete dates — the engine stays pure; result carries `reference_defaulted`). 404 unknown metric; 422 for an undeclared dimension, a provider that raises `SliceNotSupported`, or engine `ValueError`s. `_run_slice` (sync, via `asyncio.to_thread` under the lock) computes the fetch span (`min(starts)..max(ends)` — lag-shifted windows are the *caller's* to pass when slicing a lagged parent), reads through `slice_cache` (querying by the same `source`-last-segment rule as startup for SL providers), fetches the `weight` metric's slices too for rates (must share the rate's grain), and calls the pure `slice_attribution`.

⚠️ **`_require_window_loaded(data, span_start, span_end)` runs before any provider call**, raising `ValueError` → 422. Nothing checked these dates beyond "they parse", so a caller could ask for `1900-01-01…2100-12-31` and get a 73,000-day warehouse scan, **held under the tree's lock**, whose frame then sat in the slice cache forever — even though the request went on to 422 for having no data in it. It is checked inside `_run_slice` rather than in the endpoint because the reference window may be *defaulted* there: what matters is the span about to be fetched, not the span that was passed. `_loaded_window(data)` supplies the bound as `(date_start, max data_through)` — `date_start`/`date_end` are period *starts*, so a month-grain tree's `date_end` is the 1st of its last month, and using `data_through` (the same anchor node cards and goal progress use) keeps a legitimate request for the end of the last month from being mistaken for one past the end of the data.

**`POST /simulate`** — `ScenarioRequest` body. Runs `run_scenario` via `asyncio.to_thread` under the lock; `app.state.data` is passed straight through, so a cold-start tree (data `None`) selects the engine's cold-start branch with no route logic. `ValueError` → 422. Optional `run_id` opts into progress reporting (below).

**`GET /progress/{run_id}`** — the live stage of an in-flight RCA or simulation. Exists because a minute-long fit behind a spinner is indistinguishable from a hung one, and it needs no job queue: the analysis already runs in `asyncio.to_thread`, so the event loop is free to answer. Three deliberate choices:

- **No lock.** The analysis holds `app.state.lock` for its whole duration, so taking it here would deadlock the report against the thing being reported on. Cheap and unreadiness-checked, but **not ungated**: it is a data route like any other to the bearer middleware, so under `BREAKDOWN_REQUIRE_AUTH` a poller carries the same header the request it is polling for did.
- **Unknown id → `{"stage": null}` with 200**, not 404. To a poller a finished run and a never-started one are the same answer, and neither is an error worth client-side handling.
- **`app.state.progress` is bounded** (`MAX_PROGRESS_ENTRIES`, insertion-ordered eviction) for the same reason as `traces`: a client that navigates away mid-run never sends the request that would clean its entry up, and on the public demo that is every visitor.

`_progress_reporter` registers the id *before* acquiring the lock (stage `waiting`), so a queued run says so. The returned callback is handed to the engine explicitly — `run_rca(..., progress=)` — never read from a global, so `fit_metric` and friends stay pure functions of their arguments and the no-`run_id` path is byte-identical to the old behavior. The callback runs on the worker thread while the poll reads from the event loop, so it **replaces** the dict rather than mutating it: a reader sees the old update or the new one, never a half-written one. `engine/progress.py`'s `report()` swallows callback exceptions — progress must never be able to fail an analysis.

`/series`, `/analyze`, `/shapley`, `/rca`, and `/rca/{name}/slices` guard with `_require_data` (503 degraded, then 422 on a cold-start tree — those analyses consume history that deliberately doesn't exist); everything else guards with `_require_ready` alone.

---

## `mcp/` — MCP server for AI assistants

`mcp/server.py` defines an `MCPServer` ("breakdown") with six async tools — `list_trees` (the `/trees` index, so an assistant asked "why did paid signups stall" can *find* the tree that models paid acquisition before analysing it; a sibling tool rather than a second return shape on `get_tree`), `get_tree` (compact `/meta` + `/dag`; carries `mode`, each metric's declared `dimensions`, and, cold start, asserted baselines instead of a data window), `explain_metric` (definition + neighbors + series summary + fit status; series summary is null on a cold-start tree, with `baseline`/`plausible` declarations in the definition), `run_rca`, `slice_metric` (the traverse-then-slice follow-up: localizes a metric's gap within one declared dimension via the same `_run_slice` path as the endpoint, lag-shifted windows per its docstring), and `run_whatif` (`/simulate`'s engine with `Intervention`/`Assumption` as typed params; `baseline_start`/`baseline_end` are Optional — required on a fitted tree, omitted on a cold-start one). Tools own no state: they read the FastAPI `app.state` (lazy import to avoid the cycle — `api/main.py` imports `server.mcp` to mount it) and run engine calls exactly like the endpoints do: `async with state.lock: await asyncio.to_thread(...)`. Every tool takes an optional `tree`; `await _state(tree)` resolves it to a `TreeState` (default tree when omitted) and loads it on the way, since the tools are the one caller with no page to show a `loading` state to. `report_url` carries `#tree=`, so a link an assistant hands over keeps naming that tree even if the server's default changes. Engine `ValueError`s propagate as MCP tool errors so the calling model can self-correct windows. `run_rca` guards cold start via `_require_data` — a tool error naming `run_whatif` as the tool that does work.

`mcp/shaping.py` shapes engine results for LLM consumption: `round_floats` (4 significant figures, non-finite → null), `compact_rca` (drops per-contribution `decomposition` **and the node-level `interaction`** — the latter is a readout of the co-movement already inside each contribution's `estimate`, and shipping the summary without the detail that says so invites an agent to add it on top and double-count the co-movement term (roadmap C9); trims `components` to `{estimate, ci_95}` so the trend number keeps its uncertainty, collapses `fit_window` to `fit_periods`, passes `seasonality_warnings` and top-level `reference_defaulted` through, omits null node fields — but keeps a null `ci_95` inside contributions and components: withheld-interval semantics — passes `lag`/`parent_windows` through on lagged contributions, and keeps a degraded node's `status_reason` plus its `baseline`/`actual`/`gap`/`window_aggregate` where they exist), `compact_slice` (window detail → period counts, per-slice nulls and empty caveats trimmed, reconciliation collapsed to status + residual share) with `SLICE_HOW_TO_READ` (excess-vs-contribution, zero-sum excess, `noise_level`, `__other__`, mix-is-composition, reconciliation, and the lag-shifted-window rule), `compact_scenario` (baseline nodes shrink to `{status, baseline}`, extrapolation stats collapse to the flag; `mode` and any non-null `baseline_ci_95` belief intervals pass through), `RCA_HOW_TO_READ`/`whatif_how_to_read(mode)` (docs/model.md caveats attached to every analysis response; cold-start results append `COLD_START_HOW_TO_READ`, which reframes every number as a stated belief), and `rca_link`/`whatif_link`/`metric_link` (UI deep links matching `applyDeepLink()`'s hash params in `static/app.js`; base URL from `BREAKDOWN_PUBLIC_URL`, default `http://127.0.0.1:$BREAKDOWN_PORT`).

Transport: streamable HTTP mounted at `/mcp`, stateless with plain-JSON responses. Two SDK quirks the wiring handles: a mounted sub-app's lifespan never runs, so the host `lifespan` drives `mcp.session_manager.run()`; and the SDK's session manager is single-use per instance, so the mount is a shim (`_McpMount`) and each lifespan startup rebuilds the transport app (tests open several `TestClient`s per process). The SDK's default Host-header validation admits localhost only; when `BREAKDOWN_HOST` (set by `serve --host`) is non-loopback, `rebuild()` disables DNS-rebinding protection so containers/shared hosts can reach `/mcp`.

---

## Data flow

```
YAML file(s)            (--tree may be a directory: one tree per *.yml, id = filename stem)
  → Parser (Pydantic + NetworkX DAG; nodes carry MetricDefinition incl. grain/kind)
    → lifespan: parse every tree, load none  →  app.state.trees[id] (TreeState)
    → load_tree(tree), on first use: fetch_metric(name, window, grain, kind) per metric
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

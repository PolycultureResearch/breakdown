# Changelog

Notable changes to **breakdown**, distributed as `metric-breakdown`.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Pre-1.0 contract.** While the major version is `0`, the public API is the CLI,
the tree YAML schema, and the HTTP/MCP surface — and breaking changes to any of
them land in a **minor** bump (`0.1 → 0.2`), with patch releases reserved for
fixes. Callers who need stability should pin the minor series they tested
against (e.g. `metric-breakdown~=0.1.0`) until 1.0.

## [0.1.0] — unreleased

**The first release, not yet published.** `pip install metric-breakdown` does
not work today: nothing has been published to PyPI under that name and no
`v0.1.0` tag or GitHub release exists yet, so installation is from source until
the release is cut. `0.1.0` **is** the version that will be published — the
number is settled; the release is not.

Everything below ships in it. The **Added** section describes the surface;
**Changed** and **Fixed** are relative to the `0.0.1` pre-release, which was
tagged on GitHub (`c0.0.1`) and likewise never published to an index — so for
anyone installing from an index, all of this is new.

### Added

- **Engine.** Per-node Bayesian Structural Time Series with contemporaneous and
  lagged regressors and business-unit priors. Root-cause analysis over the
  ancestor DAG combining exact per-day Shapley attribution on `formula` nodes
  with posterior attribution on probabilistic ones, block-bootstrap credible
  intervals, an explicit `unexplained` term, and convergence diagnostics on
  every fit.
- **Per-node grains** (`grain`, `kind`) so ratio and cohort metrics are fitted at
  the grain they are meaningful at, with cohort-aligned lagged identities.
- **Dimensional slicing** — attribute a node's gap across a declared dimension,
  ranked by excess concentration, with a UI panel, MCP tool and API endpoint.
- **What-if simulation** — do-operator interventions, assumption links, Shapley
  source attribution, and a **cold-start mode** (`provider: none`) that runs the
  whole what-if machine on declared beliefs with no data at all.
- **Providers** — `mock`, `local` (MetricFlow), `cloud` (dbt Cloud Semantic
  Layer), `warehouse` (direct SQL), plus a parquet snapshot cache so a tree
  refits without touching the warehouse.
- **UI** at `/ui` — Cytoscape DAG, per-metric series and posteriors,
  point-and-click RCA, what-if builder, exportable HTML report, deep links.
- **MCP server** at `/mcp` for AI assistants, with interpretation caveats and
  deep links back into the UI. Optional bearer-token gate via
  `BREAKDOWN_API_TOKEN`.
- **`breakdown doctor`** — walks a provider's auth chain and prints copy-paste
  remediation for each failure.
- **Documentation.** [`docs/model.md`](docs/model.md) (what is fitted and how to
  read it), a [statistics white paper](knowledge/statistics_whitepaper.md)
  covering every model in the engine with its limitations and an honest rigor
  assessment, and architecture docs under `docs/ai-context/`.
- **Input validation across the analysis path** (roadmap 1.1 / T9). Every case
  below previously produced a *plausible wrong number* rather than an error,
  which is the worst failure mode this engine has:
  - **Window ordering.** `run_rca` and `shapley_attribution` both require
    `reference_start <= reference_end < analysis_start <= analysis_end`.
    Overlap is an error, not a warning — a shared period would count as both
    the normal regime and the departure from it.
  - **Window coverage.** The snapped windows must lie *fully* inside the node's
    own grain frame. A window entirely outside the data already raised; the new
    check catches the partial overlap, which silently averaged whichever
    periods happened to exist. Lagged parents are validated on their *shifted*
    windows and the error names the parent, its lag, and the shifted dates —
    the window the caller never typed but the engine actually read.
  - **Gap-free date spine.** `build_grained` rejects a grain frame with holes,
    naming up to 10 missing periods. Model time (`t = arange(len(y))`), lags,
    and bootstrap blocks are all positional, so a hole compresses the calendar
    and shifts every downstream date. Periods dropped by the inner join are
    logged even when the survivors stay contiguous.
- **`breakdown --version`** and **`breakdown.__version__`**, both read from the
  installed distribution metadata so `pyproject.toml` stays the only place a
  version is written and the two can never disagree.
- **CI matrix over Python 3.11, 3.12 and 3.13** with every provider extra
  installed, plus a **3.14 job that installs the wheel with no extras** and runs
  the same suite — so `requires-python = ">=3.11"` is tested at both ends
  instead of advertised.


- **Optional reference window with a defensible default** (roadmap 1.10). `reference_start`/`reference_end` are now optional on `POST /rca/{name}`, `GET /shapley/{name}`, `POST /rca/{name}/slices`, and the MCP `run_rca` tool: omitting both uses the *matched adjacent block* — the window ending the day before `analysis_start`, 4× the analysis length (min 28 days, whole weeks when seasonality is in the target's scope), clamped to the loaded data. Responses echo the resolved `reference_window` plus `reference_defaulted`. The reference was never the training window — the fit always uses all loaded history before `analysis_start` — and the UI, docs, and MCP tool descriptions now say so explicitly. The UI is analysis-first: pick the analysis window; the reference auto-fills (with an **auto** chip) and stays editable.
- **Per-node fit provenance in RCA output.** Posterior nodes report `fit_window` (`{start, end, n_periods}` — what the model actually trained on) and `seasonality_warnings` (identifiability diagnostics, previously log-only); the MCP `compact_rca` carries `fit_periods` and the warnings with a matching `how_to_read` bullet.
- **Provider history discovery.** `BaseDataFetcher.earliest_date(metric, grain)` — a never-raising capability implemented by all four providers — surfaces as `earliest_available` in `GET /meta`, a **history headroom** check in `breakdown doctor`, and a UI nudge to widen `--start-date` when more history exists upstream.
- **BigQuery execution for the `dbt` provider** (`pip install 'metric-breakdown[bigquery]'`). There was no connector, so a BigQuery shop was pushed onto `local` and its `mf` subprocess. The profile's `method` is honoured: `oauth` (Application Default Credentials), `service-account` (`keyfile`), and `service-account-json` (`keyfile_json`); an unsupported method is refused by name rather than falling back to ADC as somebody else. Supported adapters are now BigQuery, Databricks, DuckDB, Postgres and Snowflake. *(An earlier draft of this entry said the generated SQL "was already BigQuery-shaped". It was not — see the `DATE_TRUNC` fix below. The dialect was mapped; the SQL was malformed at two of three grains.)*
- **Opt-in authentication on every route** — `BREAKDOWN_REQUIRE_AUTH`. `BREAKDOWN_API_TOKEN` alone still gates `/mcp` only, unchanged; setting `BREAKDOWN_REQUIRE_AUTH` extends the same bearer check to every route except `/`, `/health` and `/ui` (matched on path-segment boundaries). Set without a token it refuses to serve rather than failing open. Note that it also gates the browser UI's own fetches, so that mode assumes a reverse proxy injecting the header — there is no login or cookie, which is hosted mode (roadmap 3.5).
- **A dbt metric's `filter:` is imported, not refused** (roadmap 2.17). Filters appear on roughly four of five real dbt metrics, and until now a filtered metric was skipped by name — correct, and a capability the tree author could see they did not have. `BindingSpec` gained a `where` list and `dbt_bridge` gained the resolver that fills it: `where_sql_template` is **Jinja, not SQL**, so each `{{ Dimension('order__region') }}` is resolved against the metric's own semantic model, parsed in the target dialect, and column-qualified through sqlglot's AST rather than pasted. The invariant is **total resolution or skip** — every predicate of every filter resolves or the metric stays exactly as refused as it was, so this is a strict superset of the old behaviour whose only failure mode is refusal. Still refused, by name and with a reason: a reference across a join (`{{ Dimension('customer__country') }}`), a time dimension or `metric_time` (MetricFlow renders those at a granularity), a `Metric()` call, a filter on a `ratio`/`derived` metric or one of its inputs (those become formula edges over metrics referenced by name, and a name carries no scope), and a legacy raw-SQL predicate with no `Dimension()` to resolve. **`where` is import-only** — a hand-written `bind: {where: …}` is a parse error pointing at `bind.sql`, which already expresses every filter an author could write.
- **`breakdown doctor` proves an imported filter actually narrows** — a new `filters narrow` check counts kept-vs-total rows over the probe window. It **fails** when a predicate keeps nothing (an empty or all-zero series: the signature of a predicate the warehouse accepts but reads differently) and **warns** when it excludes nothing (either vacuous over a short window, or constant-true — the silently-dropped filter this exists to prevent). Like the grain claim, it asks the warehouse rather than the metadata. `[WARN]` is a fourth result status; it is counted separately and does not change the exit code. The grain claim itself now runs **post-filter** and says how many relations it checked under one, since a line-level relation can be one row per grain under a filter and multi-row without it.
- **Two new per-node RCA statuses, `fit_failed` and `attribution_failed`**, each with a `status_reason`, plus a `ci_status` value `nonfinite_bootstrap_replicates`. A node that cannot be fitted or attributed is now reported as such and the rest of the tree still answers, where previously either condition failed the whole analysis or produced a 500.

### Fixed

- **A constant parent no longer ships a zero-width interval flagged `ok`** (roadmap C4). A parent held at the same value across the window — an unlaunched feature, a stock held flat, a seasonal business's off-season — made every bootstrap replicate resample the same number. The interval and its `prob_same_direction` are now withheld with `ci_status: "degenerate_bootstrap_spread"`; **no published `ci_95` is zero-width by any route**. Confirmed in production before the fix: an intensity node identically zero across a reference window shipped `ci_95 = [0.00, 24.94]` with `ci_status: "ok"`, on the parent the analysis then ranked first at ~89% of the gap.
- **Duplicate metric names are refused** (roadmap C6), before the DAG is built, naming each definition's position and source. Two definitions sharing a name used to collapse into one node carrying the *union* of both their parents while only the last one's priors were validated — so declared priors landed on the wrong coefficient axis. Asking what else collided the same way found three more: **duplicate YAML mapping keys** (PyYAML keeps the last, so a repeated `priors:` or `dimensions:` entry vanished silently), **a parent listed twice** (which made the UI display every later coefficient against the wrong parent), and **duplicate seasonality names** (which died at fit time with a PyMC error that never mentioned the tree).
- **The auto-defaulted reference window accounts for declared lags.** A defaulted block starting within `max_lag` of the data start was rejected by the engine's own coverage check, 422-ing on a date the user never typed — a short-history trigger, so a new client's first weeks. Explicit windows still fail coverage as before.
- **`MIN_FIT_PERIODS` is enforced on every fit path.** It was checked only when `fit_end` was passed or the node declared lags, so `POST /analyze`'s default fitted a **three-observation** series and reported `fit_quality: "ok"` while `breakdown doctor` called the same metric not fittable. Now one check on the one path, counting whole periods at the node's grain after every trim the fit actually applies.
- **A date column with per-row timezone offsets no longer crashes the fetch** — i.e. any daily window crossing a DST boundary on psycopg2 or Snowflake. Each row's own zone is dropped, keeping the date it labels; converting to a common zone (which pandas' own error message suggests) would move rows either side of the boundary by a day, turning a crash into a silent wrong number.
- **`POST /simulate` refuses in terms of the scenario you ran** instead of leaking `Column 'x' has zero variance — cannot normalize`. It still refuses rather than degrading the way `POST /rca` does, and deliberately: a scenario propagates along the DAG, so a node with no estimable coefficient would make every node downstream report a delta missing one of its parents' effects.
- **UI honesty pass.** A metric whose gap is exactly zero rendered **green with an upward arrow** (`0 >= 0`); the live attribution table omitted `components.trend`, which the exported report already showed, so shares summed to 108.5% and `unexplained` was understated; `fit_quality` was rendered nowhere at all, and the Metric tab told users an ADVI fit had "no convergence diagnostics" when the engine had checked and *failed* it; a `direction: down` goal missed by 3x filled its progress bar to 100%; a withheld `prob_same_direction` drew an edge at full opacity, identical to certainty; `share_of_gap: null` rendered as `0.0%`. Window advisories, `reference_defaulted`, `seasonality_warnings` and `ci_status` explanations now reach the exported report, which is read without its author present.
- **A blocked CDN no longer reports "can't reach the server".** `isTransient` treated any error without a `.status` as a dropped connection, so a `ReferenceError` from an unloaded `cytoscape` produced a retry banner against a perfectly healthy server. Library failures are now diagnosed by name and host, and all four vendored scripts carry `integrity` hashes.
- **`GET /meta.data_through` reports `null` for a metric whose data edge is unknown** instead of omitting it, so "we don't know" stops being indistinguishable from "no such metric" in the tree-wide freshness anchor.

- **Seasonality no longer fits unidentifiable Fourier harmonics.** A harmonic
  `k` carries `k / period` cycles per grain step, so Nyquist requires
  `2k < period`; below that the design column is identically zero or collinear
  with another, and the parameter is pure prior — sampled but never informed by
  data. Unconditionally fitting 2 harmonics meant `period: 2` fit three such
  parameters, `period: 3` two, and `period: 4` one. Harmonics are now filtered
  by `identifiable_harmonics(period)` (periods 3–4 keep one, ≥ 5 keep both) and
  dropped harmonics are reported in the `seasonality_warnings` diagnostic,
  alongside the existing not-enough-data warning.
- **The bundled example stopped shipping a documented pitfall.** The `period:
  365` annual component is gone from `jaffle_shop_tree.yml` and the B2B MRR
  reference tree: identifying it needs two full years inside the *fit* window,
  and RCA fits stop at `analysis_start`, so it only ever soaked up degrees of
  freedom the parents needed.


- **A zero denominator in a formula no longer 500s.** `formula: "revenue / order_count"` with a single zero-denominator period returned a bare 500 with `KeyError: '__import__'` — the restricted-globals sandbox around formula evaluation broke numpy's own warning machinery — and, once past that, a NaN through every attribution, interval, `unexplained`, `interaction` and `ranked_causes` score, reaching the JSON encoder as a second 500 and the MCP payload as `null`s. Now a 422 naming the offending parent series, its window and the dates; an ancestor that fails this way degrades to `attribution_failed` with its own gap still reported. `_align_to_spine` manufactures exactly this zero by design for a `kind: flow` denominator, so a seasonal business hit it in normal operation (roadmap C17).
- **A `flow` metric that starts partway into the loaded window no longer fills silently.** Periods before the source's first row were zero-filled with no warning of any kind — 19 invented zeros in the reproduction — and the model trained on them, giving a node that did not exist yet a manufactured level shift and trend that RCA could rank as a cause. The fill still happens (trimming would delete those periods for every metric at that grain, since per-grain frames inner-join), but it now warns and names the periods it invented (roadmap C18).
- **BigQuery `DATE_TRUNC` arguments were reversed at day and month grain**, so every day- and month-grain query generated for a BigQuery warehouse was malformed and only week was usable. The week override was wrong too for the common case, since BigQuery's `DATE_TRUNC` takes a `DATE` and a dbt fact table's time dimension is typically a `TIMESTAMP` — so BigQuery had no fully working grain rather than one. All three grains now emit `DATE_TRUNC(CAST(col AS DATE), PART)`.
- **One zero-variance series no longer aborts the whole tree RCA.** A parent held identically at zero across the window — an unlaunched feature, a seasonal business's off-season — made the fit raise and returned nothing for any node. That node is now reported `fit_failed` and every other node is still attributed.
- **An out-of-range RCA window is refused before fitting**, not after paying for every ancestor's fit and then returning 422.
- **`GET /metrics/{name}` no longer blocks the event loop.** The posterior summary ran synchronously in an async handler — 1.1s of process-wide outage per call on an 830-day fit, on every call, scaling with `draws`. Now off-thread and memoized on the fit (1.19s → 0.02s on repeat).
- **`GET /dag` no longer publishes `sql` and `bind` to unauthenticated callers** when `BREAKDOWN_API_TOKEN` is set — those blocks carry fully-qualified table names and WHERE-clause business logic.
- A non-ASCII `Authorization` header returns 401 rather than raising `TypeError` and returning 500.
- **The MCP payload no longer hides why a node was skipped.** `compact_rca` shrank every non-`ok` node to `{status, grain}`, dropping the `status_reason` that names the offending parent and dates, and the `gap` an `attribution_failed` node still measures — so an assistant saw a bare label and could only narrate it as "nothing happened there", which is the one reading it must not reach. Both are kept now, and `how_to_read` carries a bullet saying a non-`ok` node is a gap in the analysis and that `ranked_causes` is incomplete whenever one is present.
- **The UI no longer renders a node it could not analyze as an improvement.** `applyRcaOverlay` tinted on `node.gap >= 0`, and `null >= 0` is `true` in JavaScript, so a `window_shorter_than_grain` node had always shown green with an upward arrow. All three non-`ok` statuses now render in one *not analyzed* vocabulary across the canvas, ranked causes, attribution detail and the exported report, with a count in the RCA summary — which matters because `ranked_causes` includes every node in scope, so a failed node can top the ranking while everything upstream of it silently scores zero. Separately, `ci_status: "posterior_only_single_period"` had never been rendered at all, so a withheld interval read as a clean one.
- **FastAPI 0.137.0 and newer are confirmed supported, and `fastapi` stays unbounded.** 0.137.0 changed `include_router` to append one lazy `_IncludedRouter` node per include rather than copying each route into the parent, so `app.routes` is no longer a flat list of routes — which made a CI check that walked it report a fresh no-extras install as missing every data route. Nothing was missing: all 25 paths and all 25 unique operation ids are present, and every endpoint serves, on every fastapi from 0.135.2 to 0.141.1 and against starlette 1.0.0 through 1.6.0. The check now reads `app.openapi()`, and a new invariant asserts that every route on the shared router is reachable at both its bare and its `/trees/{tree_id}` path — the property the single-router design exists to guarantee — so a genuinely missing alias fails as a missing route rather than as a documentation problem.
- **A metric matching no rows no longer kills startup on the `local` provider.** `mf` writes a zero-byte CSV — not even a header — when a metric's filter matches nothing in the window, and `pd.read_csv` raised `EmptyDataError` before the frame ever reached `_align_to_spine`, whose contract already covers the case ("a source returning no rows at all keeps the full fill for flows"). A seasonal tree with a product stream that has not gone on sale yet could not load at all. The empty result is now warned about and passed through, so a flow fills to zero and a rate still errors, matching every other provider. Reported from an outside deployment.

### Changed

- **`ranked_causes` no longer saturates, and no longer lists non-causes** (roadmap C5). A hop now weights a parent by `min(|share|, 1)` divided by the node's *cancellation factor* — how much gross parent movement the decomposition needed in order to net out to the gap — so a parent explaining 165% of a gap scores **below** one explaining a clean 80%, which is what a share far past 100% always meant. A node's parents' weights sum to at most 1, so a hop can no longer inflate influence. Rows for nodes no hop ever reached are dropped (`via` is never `null`); a node that *was* reached and explains nothing stays, scored 0.0, with its provenance.
- **The bootstrap's block-length cap is `n // 4`, not `n // 2`** (roadmap C4). Measured: the old cap gave a resampled-to-true variance ratio of 0.46-0.63 for every window of 16 periods or fewer and was non-monotone in window length; the new one gives 0.74-0.90 and is monotone. **Intervals on windows shorter than ~28 periods are now wider**, including slice intervals, because they were previously too narrow - this changes published numbers.
- A structurally absent `components.seasonal` is **omitted** rather than reported as `{estimate: 0.0, ci_95: [0.0, 0.0]}`. A zero-width 95% interval asserted infinite precision about a term the model never contained.

- **`seasonality.period` must now be >= 3** (was >= 2). A period of 2 is at the
  Nyquist limit of its own grain and is unidentifiable at any sample size — a
  config error, not a data shortage, so it is rejected at parse time.
- **Provider SDKs are now extras.** `pip install metric-breakdown` installs the
  engine, API, UI, MCP server and the `mock` provider — 70 packages / ~390 MB,
  down from 138 / ~640 MB. Real providers opt in:
  `metric-breakdown[dbt]` for `local` and `cloud`, `metric-breakdown[databricks]`
  for `warehouse`, `metric-breakdown[all]` for both. Selecting a provider whose
  extra is absent raises `MissingProviderExtra` naming the exact `pip install`,
  and `breakdown doctor` reports it as its own check with the downstream
  connectivity checks skipped rather than failing misleadingly.
- **The base package supports Python 3.14.** It could not before, because
  dbt-core was a hard dependency and its `mf` binary does not run there. The
  `dbt` extra is still 3.13-and-earlier.
- **Dependency floors lowered** to versions the suite is actually verified
  against (`numpy>=1.26`, `pandas>=2.1`, `pymc>=5.16`, `pydantic>=2.7`,
  `fastapi>=0.115`, `networkx>=3.1`, `uvicorn>=0.27`, `arviz>=0.22`), so
  breakdown stops forcing a resolver conflict on stacks that are a few months
  behind. `mcp>=2.0` is unchanged — the server genuinely uses 2.x APIs.


- Engine entry points `run_rca` / `shapley_attribution` take their window arguments as keyword-only (Python callers passing windows positionally must update; the HTTP/MCP surface is unchanged and fully backward compatible).
- UI window presets are analysis-only (`Last 7 days`, `Last 14 days`, `Last full week`); the `First 60% vs rest` and `vs prior 28d` pairs are gone — the auto reference subsumes them. RCA deep links are rewritten from the resolved windows after each run; existing four-param links replay unchanged.

- A formula node may have **at most 10 parents**. Exact Shapley is a full coalition enumeration, so cost doubles per parent (10 ≈ 3.5s, 12 ≈ 20s) and RCA runs it six times per node while holding the tree's lock. Wider nodes are refused by name; the remedy is to group parents under an intermediate `formula` node, which preserves the identity and keeps every attribution exact.
- `POST /rca/{name}/slices` now requires its windows to lie inside the loaded data window, and refuses with a 422 naming that window instead of asking the provider for the range you typed.
- The trace cache is bounded by **bytes** rather than entry count (`BREAKDOWN_MAX_TRACE_BYTES`, default 512 MiB). A cached fit scales with the loaded window — 13.4 MB on an 830-day one — so 256 entries could reach ~3.4 GB and no entry count was safe. `slice_cache` and `flow_cache` are bounded too.

### Known limitations

Stated up front rather than discovered later; all are documented in
[`docs/model.md`](docs/model.md) and assessed in §3 of the
[statistics white paper](knowledge/statistics_whitepaper.md):

- **RCA defaults to mean-field ADVI**, whose credible intervals are
  systematically narrower than the truth. Confirm anything load-bearing with
  `POST /analyze/{name}?inference_method=nuts&fit_end=<analysis_start>`. Fixing
  this is the first item in the roadmap's
  [statistical rigor workstream](knowledge/roadmap.md#statistical-rigor-s--a-standing-workstream).
- **`ranked_causes` is a documented heuristic**, not a probability — a triage
  ordering. Read the per-node contributions and their intervals for rigor.
- **The DAG is your hypothesis.** breakdown quantifies the edges you declare; it
  does not discover them, detect confounders, or prove causality.
- **The `dbt` extra requires Python ≤ 3.13** (dbt-core's `mf` binary does not run
  on 3.14). The base package supports 3.11–3.14.

<!-- `0.1.0` is the settled version for the first release, but no `v0.1.0` tag or
GitHub release exists yet, so `releases/tag/v0.1.0` would 404. On cutting the
release, tag `v0.1.0`, point the link below at
`releases/tag/v0.1.0`, and open a fresh `## [Unreleased]` section above it with
`[Unreleased]: .../compare/v0.1.0...HEAD`. Note that cutting the GitHub release
*is* the publish: `.github/workflows/publish.yml` triggers on `release:
published` and uploads via Trusted Publishing. -->
[0.1.0]: https://github.com/PolycultureResearch/breakdown/commits/main

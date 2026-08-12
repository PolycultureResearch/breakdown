# Changelog

Notable changes to **breakdown** (published on PyPI as `metric-breakdown`).

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Pre-1.0 contract.** While the major version is `0`, the public API is the CLI,
the tree YAML schema, and the HTTP/MCP surface — and breaking changes to any of
them land in a **minor** bump (`0.1 → 0.2`), with patch releases reserved for
fixes. Callers who need stability should pin `metric-breakdown~=0.1.0` until 1.0.

## [Unreleased]

### Added

- **Optional reference window with a defensible default** (roadmap 1.10). `reference_start`/`reference_end` are now optional on `POST /rca/{name}`, `GET /shapley/{name}`, `POST /rca/{name}/slices`, and the MCP `run_rca` tool: omitting both uses the *matched adjacent block* — the window ending the day before `analysis_start`, 4× the analysis length (min 28 days, whole weeks when seasonality is in the target's scope), clamped to the loaded data. Responses echo the resolved `reference_window` plus `reference_defaulted`. The reference was never the training window — the fit always uses all loaded history before `analysis_start` — and the UI, docs, and MCP tool descriptions now say so explicitly. The UI is analysis-first: pick the analysis window; the reference auto-fills (with an **auto** chip) and stays editable.
- **Per-node fit provenance in RCA output.** Posterior nodes report `fit_window` (`{start, end, n_periods}` — what the model actually trained on) and `seasonality_warnings` (identifiability diagnostics, previously log-only); the MCP `compact_rca` carries `fit_periods` and the warnings with a matching `how_to_read` bullet.
- **Provider history discovery.** `BaseDataFetcher.earliest_date(metric, grain)` — a never-raising capability implemented by all four providers — surfaces as `earliest_available` in `GET /meta`, a **history headroom** check in `breakdown doctor`, and a UI nudge to widen `--start-date` when more history exists upstream.

### Changed

- Engine entry points `run_rca` / `shapley_attribution` take their window arguments as keyword-only (Python callers passing windows positionally must update; the HTTP/MCP surface is unchanged and fully backward compatible).
- UI window presets are analysis-only (`Last 7 days`, `Last 14 days`, `Last full week`); the `First 60% vs rest` and `vs prior 28d` pairs are gone — the auto reference subsumes them. RCA deep links are rewritten from the resolved windows after each run; existing four-param links replay unchanged.

## [0.1.0] — 2026-08-05

**First public release** — the first version published to PyPI as
`metric-breakdown`. The **Added** section describes the surface that ships;
**Changed** and **Fixed** are relative to the `0.0.1` pre-release, which was
tagged on GitHub and never published to an index.

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

### Fixed

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

### Changed

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

[Unreleased]: https://github.com/PolycultureResearch/breakdown/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PolycultureResearch/breakdown/releases/tag/v0.1.0

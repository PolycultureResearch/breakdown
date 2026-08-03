# Design: Dimensional slicing inside the tree (roadmap 3.2)

Status: v1 shipped (schema + engine + mock/semantic-layer providers +
API/MCP), plus the **UI** and **sliced snapshots** built for the White Cube
demo ([`white_cube_demo_plan.md`](white_cube_demo_plan.md)); warehouse contract,
doctor checks, and automated tree×slice deferred (§9). Companion to [`grain_design.md`](grain_design.md)
(window snapping the slice path reuses), [`grain_research.md`](grain_research.md)
(§ tool survey — the prior art), and
[`rca_lag_assessment.md`](rca_lag_assessment.md) (lag-correct slice windows).

## 1. Product framing

Tree RCA answers *which upstream metric* moved; slicing answers *where inside
that metric*. The combined workflow — "revenue was down over the weekend" →
`run_rca` → top cause `signups` → slice `signups` by `region` over the same
windows → "the gap is 87% EMEA, far above its 22% baseline share" — is the
**traverse-then-slice** loop the README's weekend narrative describes, and no
flat-slicing competitor has it, because they have no tree to traverse
(Power BI's decomposition tree, ThoughtSpot SpotIQ, Sundial, Kausa are all
flat two-period slicers — `grain_research.md`). Flat slicing is commoditized;
*tree × slice* is the differentiator.

One sentence carried into the response guidance: **slices localize; the tree
explains.** A concentrated slice says where to look next — an app version, a
locale, a plan tier — not why it moved. The honesty posture carries over
wholesale: every slice gets a credible interval, `__other__` is a first-class
row, and slices that don't sum to the metric are *reported*, never silently
rescaled.

## 2. Declaration: explicit per-metric `dimensions`

```yaml
- name: signups
  kind: flow
  dimensions:
    region: customer__region            # shorthand
    plan:
      source: subscription__plan_tier   # provider dimension identifier
      top_k: 6                          # kept individually; rest -> __other__ (default 8, bounds 2..20)
      values: [pro, team, enterprise]   # optional pin-list, overrides top_k

- name: trial_conversion_rate
  kind: rate
  formula: "conversions / trial_starts"
  parents: [conversions, trial_starts]
  dimensions:
    region: customer__region            # weight defaults to trial_starts (the denominator)
```

`DimensionSpec` (`breakdown/parser.py`): `source` (the provider's dimension
id — a MetricFlow dimension like `customer__region` for the SL providers),
`top_k`, `values`, `weight`, and a **reserved** `sql` field for the warehouse
contract (§6). Rate metrics require a `weight` — the tree metric whose sliced
shares blend the per-slice rates — defaulting from a simple `num / den`
formula's denominator; cross-checked against the tree in `Parser`.

**Explicit declaration over SL auto-discovery**, deliberately: declaration
keeps the mock and warehouse providers first-class, encodes analyst intent
(which slices are *meaningful*, not just queryable), and bounds cardinality
up front. Discovering candidate dimensions from the SL belongs to the tree
scaffolder (roadmap 2.3), which can *write* these declarations.

Slicing is **analysis-time only**: a `dimensions` declaration never changes
fetching at startup, fitting, or tree attribution.

## 3. Statistics — flows and stocks: the sum identity, in closed form

A sliced flow is an exact identity, `signups[t] = Σ_g signups_g[t]` — the same
class of object as a formula node, so slices-as-parents inherit the tree's
attribution semantics. Because the identity is **linear**, the per-day Shapley
machinery collapses: the means-bridge game gives each slice exactly its own
window-mean change and both within-window co-movement games vanish, so the
exact, order-independent, efficiency-satisfying answer is closed-form:

```
contribution_g = mean_an(x_g) − mean_ref(x_g)        Σ_g contribution_g = gap   (exactly)
```

Stocks: levels sum across slices and RCA compares window means of levels, so
the same math applies. (A non-additive "stock" — a distinct count over
overlapping slices — is exactly what reconciliation §7 surfaces.)

Implementation note: this deliberately does **not** route through
`shapley_attribution` — that function wants DAG nodes, formula-identifier
names, and O(2^n) coalitions, all pointless here. `breakdown/engine/slices.py`
implements the closed forms and reuses the tree's bootstrap helpers
(`_block_bootstrap_indices`, block lengths per grain, joint resampling across
slices so cross-slice correlation is preserved) for CIs, with the same
single-period degeneracy convention (`ci_status`).

## 4. Statistics — rates: mix vs within (Bennet split)

A sliced rate blends: `r[t] = Σ_g s_g[t]·r_g[t]`, shares `s_g` from the
declared `weight` metric sliced over the same dimension. At the
window-aggregate level (weights summed per window, rates weight-averaged) the
exact symmetric per-slice split is the **Bennet decomposition** — precisely
the two-player Shapley value of each product term, the same index-number
result `grain_research.md` already cites:

```
Δr = Σ_g [ s̄_g·Δr_g + r̄_g·Δs_g ]        s̄, r̄ = two-window means

within_g = s̄_g·Δr_g     this slice's own rate moved
mix_g    = r̄_g·Δs_g     traffic shifted toward/away from this slice
```

Exact, zero remainder. `Σ_g Δs_g = 0` makes the mix terms a pure reallocation
signal, so `mix_total` is reported as its own line — the analogue of the
tree's `interaction` row; "a mix shift is a composition effect, not any
slice's fault" ships in the how-to-read. A slice with zero weight in one
window (a brand-new app version) keeps its other window's rate, so Δr = 0 and
its whole effect flows honestly through mix. The weighted fold into
`__other__` preserves per-window products, so the split still telescopes
exactly after folding. Two headline notes surface automatically: the sliced
gap is weight-blended while the node's RCA gap is the unweighted window-mean
difference (a material difference becomes a caveat), and the per-day
within-window covariance of the blend is not decomposed in v1 — it lands in
reconciliation, labeled.

## 5. Ranking: excess concentration, not raw size

The biggest slice always has the biggest raw contribution; that is not news.
Each slice reports both:

```
baseline_share_g = mean_ref(x_g) / mean_ref(Σ x)
excess_g         = contribution_g − baseline_share_g × gap
```

`excess_g ≈ 0` means the slice moved in proportion to its size — a uniform
cause upstream. Large `|excess_g|` is localization. `Σ_g excess_g = 0`:
concentration is a zero-sum reallocation of the gap, which makes excess
self-normalizing. Slices rank by `|excess|` (rates by within-excess, with
`mix_total` separate).

Uncertainty stays probabilistic without per-slice model fits: excess is
recomputed per bootstrap replicate, giving `ci_95` and `prob_concentrated`
(a direction probability in the house style of `prob_same_direction` — no
p-values); `prob_concentrated < 0.8` flags the row `noise_level`. Per-slice
BSTS fits are deliberately deferred — the bootstrap gives honest
window-sampling uncertainty at zero fit cost.

`__other__` is a full row (a long-tail regression is a real finding), built as
the **sum of the fetched non-top-K slices** — never as `unsliced − Σ topK`,
which would launder the reconciliation residual into it. Guards: hard cap of
100 distinct fetched values (error with remediation), caveat when `__other__`
holds > 50% of baseline (the dimension is too fragmented to localize).

## 6. Data plumbing: on demand, never touching `GrainedData`

The load-bearing decision. `GrainedData` and the `(name, fit_end)` trace cache
assume **one series per metric**; threading a dimension axis through them
would be a structural rewrite of the storage and fit layers for no modeling
benefit (slice attribution needs no fits). So:

- `BaseDataFetcher.fetch_metric_sliced(metric, dimension_source, start, end,
  grain, kind)` returns long `[date, slice, value]`; the default
  implementation raises a typed `SliceNotSupported` (→ 422 naming the
  provider).
- Fetches span only the two (possibly lag-shifted) windows — no history, no
  fits — cached in `app.state.slice_cache` keyed
  `(metric, dimension_source, grain, start, end)`.
- The engine (`breakdown/engine/slices.py`) is **pure**: the API endpoint owns
  fetch + cache (`_run_slice` in `breakdown/api/main.py`), preserving the
  stateless-engine agreement.
- Per-slice gap-fill mirrors the warehouse rules per kind: flow → 0, stock →
  carry-forward (0 before the slice's first observation), rate → absence is
  weight-0 (a rate present-with-weight-but-missing is an error).

Per provider (v1):

- **cloud** (`dbtsl` SDK) and **local** (`mf` CLI): the business dimension is
  appended to the already-present time-grain `group_by`, and the result is
  reshaped to long (`_sliced_long`). NULL dimension values become `__null__`.
- **mock**: deterministic and exactly additive. Slice shares are smooth,
  date-anchored seeded curves per `(dimension, slice)` — identical across
  metrics and fetch windows, so a rate's blend reconciles *exactly* against
  its weight metric's slices — and slice fetches split the very series a
  covering startup fetch produced (the covering-cache path), so demos and
  tests reconcile exactly. The drift term in the share curves gives demos
  genuine mix shifts.
- **snapshots**: `SnapshotFetcher` passes sliced fetches through to the inner
  provider (sliced snapshot persistence deferred).
- **warehouse**: raises `SliceNotSupported` in v1. The designed contract
  (reserved `DimensionSpec.sql`): per-dimension author-owned SQL returning
  exactly `date, slice, value` with the same `:start_date`/`:end_date`
  params. Chosen over a `:dimension` bind-param convention (a GROUP BY column
  cannot be safely parameterized) and over allowing a dimension column in the
  main metric SQL (which would change every unsliced fetch's shape).

## 7. Reconciliation: slices must sum/blend back to the metric

Per date over both windows: flows/stocks compare `Σ_g` all fetched slices vs
the metric's served series; rates compare the per-date weighted blend vs the
unsliced rate. The response block —
`{mean_residual, max_abs_residual, residual_share_of_baseline, status}` —
flags `discrepant` above 0.5% of |baseline|. A discrepancy is a **measurement
caveat, never a silent correction**: typical causes are a non-additive
dimension (overlapping membership, distinct counts), a sliced query diverging
from the governed metric definition, or freshness skew between the startup
snapshot and the live slice query. The guidance line: treat slice attributions
as approximate and say so.

## 8. Surfaces

**API** — `POST /rca/{name}/slices?dimension=…&reference_start=…&…` beside
`POST /rca/{name}`: the traverse-then-slice follow-up. Response:
`{metric, dimension, grain, kind, effective_windows, baseline, actual, gap,
attribution_method: slice_sum | slice_blend, slices[], mix_total?,
reconciliation, ci_status, caveats}`. 422s: undeclared dimension, provider
without slicing, bad dates, rate-weight problems.

**MCP** — `slice_metric(name, dimension, windows…)` with `compact_slice`
shaping and `SLICE_HOW_TO_READ` (excess-vs-contribution, zero-sum excess,
`prob_concentrated`, `__other__`, mix-is-composition, reconciliation, and the
lag-window rule for slicing a lagged parent). `get_tree`/`explain_metric`
advertise each metric's `dimensions`, so an agent knows what is sliceable
before it asks.

**UI** — deferred. The natural v-next: a "slice" action on RCA contribution
rows deep-linking to a slice table via the existing `applyDeepLink` pattern.

## 9. Explicitly deferred

- **Warehouse sliced SQL** (contract designed in §6; `sql` field reserved).
- **Doctor checks** per declared dimension (shape, cardinality,
  reconciliation probe) with `CheckResult` remediations.
- **Sliced snapshot persistence** (dimension-keyed parquet filenames).
- **Automated tree×slice** — `POST /rca/{name}?slice_top_causes=N`:
  auto-slice the top ranked causes that declare dimensions, each over its
  lag-correct windows, attached under `nodes[metric]["slices"]`. Deferred for
  query fan-out cost and MCP token budget, and because the interactive loop
  should prove the ranking first; the response shape is identical, so merging
  later is additive.
- **Per-slice BSTS fits** (posterior per slice); the bootstrap carries v1.
- **Multi-dimension cross-products** — slice one dimension at a time; an
  agent can run two calls.
- **SL dimension auto-discovery** (scaffolder territory, roadmap 2.3) and
  tree-level dimension defaults.
- **Sub-slicing `__other__`** (raise `top_k` or pin `values:` instead).

# Design: Per-node aggregation grain (roadmap 1.7)

Decisions and rationale for grain support, resolving the open questions in the
roadmap 1.7 sketch. Research backing: [`grain_research.md`](grain_research.md).
Status and remaining work are tracked in [`roadmap.md`](roadmap.md).

## The decision in one paragraph

Every metric node declares an optional **`grain: day | week | month`**
(default `day` — existing trees are unchanged) and a **`kind: flow | stock |
rate`** (default `flow`). A node is fetched, fitted, and attributed **at its
own declared grain, never below it** (MetricFlow's coarsen-only invariant).
Finer *flow* and *stock* parents are automatically resampled up to a coarser
child's grain (flows sum, stocks take the last value); *rate* parents can
never be auto-aggregated — averaging per-day ratios is wrong — so a rate
finer than its child is a parse-time error telling the author to declare the
rate at the child's grain, recomputed from its components. A parent *coarser*
than its child is always an error: downward disaggregation is undefined.

## Why per-node, not a global tree grain

- The motivating tree (New-MRR) is intrinsically mixed-grain: daily signup
  flows under weekly cohort rates under monthly MRR snapshots. A global grain
  either destroys the daily detail or manufactures fake monthly detail.
- Every precedent is per-metric: MetricFlow's metric-level `time_granularity`
  with column-level granularity floors, Tableau Pulse's per-metric minimum
  grain. No surveyed tool models below a metric's declared grain.
- The temporal-aggregation literature ("Granularity Paradox") is unambiguous
  that fitting below a series' natural grain inflates sample size and
  manufactures spurious covariance — a monthly snapshot forced onto a daily
  spine is 30 step-function "observations" carrying one observation of
  information, so daily posteriors are spuriously tight and the block
  bootstrap papers over degenerate per-day ratios.

## Why `kind` must ship with `grain`

"Resample up" is only well-defined given a temporal aggregation operator:
flows sum, snapshots take a boundary value (we use `last`), and rates are
non-additive in time — the only correct coarse rate is recomputed from its
re-aggregated components (MetricFlow's ratio metrics and
`non_additive_dimension` encode exactly this). `kind` is the flow/stock/rate
half of roadmap 1.3's config-hardening metadata; the sign-convention half of
1.3 remains separate.

## Contracts

**Provider contract.** Each metric is fetched at its native grain.

- Semantic-layer providers (Cloud/Local) pass `metric_time__{grain}` through —
  the SL re-aggregates correctly by construction. Returned labels are floored
  to period starts with a warning if any label moved (dbt week-start is
  project-configurable; breakdown assumes ISO Monday weeks).
- The warehouse provider drops its daily-only restriction: the SQL author
  writes SQL returning one row per period at the declared grain. The engine
  reindexes onto a spine of **whole periods** and gap-fills by kind: flow → 0,
  stock → forward-fill (leading gap is an error), rate → any missing period is
  an error at fetch time (a rate cannot be invented).
- The mock provider generates grain-aware series such that identities hold at
  each node's declared grain.

**Period labels** are period-start timestamps everywhere: day = midnight,
week = Monday (ISO), month = the 1st. Partial edge periods are dropped, never
zero-filled into fake periods.

**Storage.** The single tree-wide daily frame (inner-joined on date) becomes
per-grain frames: metrics inner-join only against other series at the same
grain, and derived coarser views of flow/stock metrics are materialized at
startup for the grains their children need. This removes the failure mode
where one monthly metric would drop 29 of every 30 days from the whole tree.

**Fit contract (aggregate-then-fit).** A node's BSTS fit runs at the node's
own grain, with finer parents resampled up. Fewer observations per fit is the
*honest* posterior width, not a regression. Lags, seasonality periods, trend
steps, and bootstrap block lengths are all **grain-relative** (a `lags: 4` on
a weekly node means 4 weeks; `period: 7` on a monthly node draws a warning).
Bootstrap block lengths: day 7, week 4, month 2. The principled future
upgrade for mixed-frequency edges is a latent state-space (MIDAS/Kalman)
model at the fine grain with coarse observations — deliberately not the MVP.

**RCA windows.** Analysis/reference windows remain day-resolution dates in
the API; each node interprets them at its own grain by snapping to the whole
periods fully inside. A node whose window contains no whole period is
reported with a `window_shorter_than_grain` status (and skipped from
attribution) rather than failing the whole RCA; each node reports its
effective (snapped) windows. Windows snapped to a single period get their
window-sampling CI suppressed (`ci_status: degenerate_single_period`) instead
of reporting a falsely-zero-width interval.

## Relationship to the covariance-symmetry fix (1.8, shipped)

The 1.8 fix decomposed formula attribution into three exact Shapley games —
`means + covariance_analysis − covariance_reference` — which is precisely the
two-level structure the 1.7 sketch asked for: the **means bridge** is the
window-aggregate headline (the Bennet/Sun decomposition), and the
**covariance parts** are the explicit interaction/co-movement term, exposed
per parent in `GET /shapley`'s `decomposition`. Grain changes *what the
periods and windows are*; the decomposition structure is unchanged. The
remaining 1.7 UI work is presentational: make the means bridge the headline
view and the covariance shares the drill-down.

## Resolutions to the sketch's open questions

- **Grain-relative seasonality**: periods are declared in grain steps;
  parse-time warning when a non-day node declares a classic day-grain period
  (7/30/365); fit-time identifiability warning when the data span is under
  two full periods.
- **Snapshot-store interaction (2.4)**: unchanged — the store keys on
  `(metric, window, grain)` and a node's grain is a property of the node, so
  the trace cache key `(node, analysis_start)` stays compatible.
- **Mixed-grain contract**: resample-up only, by the parent's kind; rates
  never auto-resample; coarser-parent edges rejected. Cross-grain simulate
  interventions scale flow deltas by periods-per-child-period before applying
  β (β is fitted against the summed parent).
- **`formula` + `lags` relaxation**: accepted as a clearly-separable final
  phase — a lagged identity `A[t] = f(parents shifted by their lags)` in
  grain steps gives cohort conversion an exact deterministic form
  (`conversions[w] = trial_starts[w−k] × cohort_rate[w]`).

## Not in scope

Sub-daily grains, custom (fiscal/4-4-5) calendars, quarter/year grains,
latent mixed-frequency state-space models, downward disaggregation.

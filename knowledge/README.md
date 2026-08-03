# Knowledge

Product and design specs for breakdown — the _what_ and _why_ behind features and
roadmap. For how the codebase is built, see [`../AGENTS.md`](../AGENTS.md) and
[`../docs/ai-context/`](../docs/ai-context/); to use breakdown, see the
[README](../README.md).

## Roadmap

- [`roadmap.md`](roadmap.md) — **start here** for what to build next: a prioritized,
  status-tagged list of engineering/product work (no go-to-market).

## Design specs

The _what_ and _why_ behind shipped features (the _how_ lives in the code and in
[`../docs/ai-context/`](../docs/ai-context/)):

- [`ui_design_spec.md`](ui_design_spec.md) — UI design spec
- [`what_if_design.md`](what_if_design.md) — what-if simulation design spec
- [`grain_design.md`](grain_design.md) — per-node aggregation grain (roadmap
  1.7): decisions and contracts for `grain`/`kind`, resample-up, window
  snapping, and the two-level attribution view
- [`grain_research.md`](grain_research.md) — the external research behind the
  grain design: MetricFlow precedent, temporal-aggregation literature,
  Sun/Shapley/Bennet index theory, tool survey
- [`dimensional_slicing_design.md`](dimensional_slicing_design.md) — dimensional
  slicing inside the tree (roadmap 3.2): declared `dimensions`, exact
  sum/Bennet slice attribution, excess-concentration ranking, on-demand
  fetch that never touches the fit path
- [`white_cube_demo_plan.md`](white_cube_demo_plan.md) — the deployable
  synthetic-data demo: the White Cube scenario and its four planted stories, the
  demo metric tree, build-live/serve-hermetic runtime shape, and the Fly.io
  deployment. Companion: [`demo_guided_tour.md`](demo_guided_tour.md), the
  client-facing script (exact RCA windows and what each should conclude)
- [`rca_lag_assessment.md`](rca_lag_assessment.md) — how RCA handles time lags
  today (declared lags shift fit and attribution windows correctly), and the
  planned improvements: surfacing lag-shifted parent windows, a Bayesian lag
  scan, distributed lags assessed and deferred

## Archive

- [`archive/`](archive/) — executed implementation plans (statistical T1–T12, UI
  U1–U6, connectivity analysis), kept for rationale. Their open items are carried
  forward in [`roadmap.md`](roadmap.md).

## Authoring guides

- [`authoring_deterministic_decompositions.md`](authoring_deterministic_decompositions.md)
  — practical, generalizable lessons for adding deterministic identity edges
  (`formula` sums/products): the per-day identity rule, the parent-SQL +
  factor-SQL node shape, zero-denominator handling, and how to validate a tree
  against a warehouse before trusting attribution. Read alongside the
  [tree-authoring reference](../README.md).

## Example trees

- [`b2b_mrr_tree.yml`](b2b_mrr_tree.yml) — a full B2B SaaS "Total MRR" metric tree (107 metrics, single apex), adapted from Metrics Labs' [B2B Metrics Canvas](https://miro.com/app/board/uXjVNq48sQI=/?share_link_id=353173494684) on Miro. A worked reference for mapping a real-world metric tree onto breakdown's `formula` (deterministic) and probabilistic (`priors`/`lags`) edges.

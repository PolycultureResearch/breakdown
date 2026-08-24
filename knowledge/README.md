# Knowledge

Product and design specs for breakdown — the _what_ and _why_ behind features and
roadmap. For how the codebase is built, see [`../AGENTS.md`](../AGENTS.md) and
[`../docs/ai-context/`](../docs/ai-context/); to use breakdown, see the
[README](../README.md).

## Roadmap

- [`roadmap.md`](roadmap.md) — **start here** for what to build next: a prioritized,
  status-tagged list of engineering/product work (no go-to-market).
- [`milestone_readiness_2026_08_17.md`](milestone_readiness_2026_08_17.md) — the
  readiness assessment for the three 2026 milestones (Northern Nights deployment,
  0.1.0, PyPI): the four recent policy decisions audited from decision to code to
  tests, per-milestone punch lists, and the time-to-first-RCA analysis. Its
  wrong-number-class findings became roadmap C23–C25 and 2.20 (all shipped
  2026-08-17; the addendum carries the read-the-numbers verification).
- [`bigquery_end_to_end_test_plan.md`](bigquery_end_to_end_test_plan.md) —
  **plan, not executed**: stand up a real BigQuery and run breakdown against it
  end to end, with twelve failure predictions derived from the code. Cited by
  roadmap 2.10 (the still-unverified BigQuery side) and 2.14 (its §6 is the
  acceptance criteria for differential verification).

## White papers

- [`statistics_whitepaper.md`](statistics_whitepaper.md) — **the statistics of
  breakdown**, written for data professionals who are not Bayesian
  statisticians and intended as a public document. The high-level approach and
  the five commitments behind it; every statistical model in the engine (BSTS,
  NUTS/ADVI, Shapley attribution, the block bootstrap, do-operator simulation,
  cold start, slice attribution) with why it fits its job and where it breaks;
  cited sources; and an honest assessment of the engine's current rigor with a
  prioritized list of improvements. Complements
  [`../docs/model.md`](../docs/model.md), which is the practitioner's guide to
  *reading* output — this paper is the *why* underneath it.
- [`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md) — a deep dive
  on the white paper's #1 identified weakness: RCA defaults to mean-field ADVI,
  which is underdispersed by construction, and breakdown's β-vs-trend posterior
  ridge is the geometry it handles worst. Covers the mechanism (reverse KL, the
  `σ²(1−ρ²)` result), why this engine is unusually exposed, a worked example of
  a decision it would send the wrong way, when to confirm with NUTS, and the
  two candidate fixes (PSIS k̂, full-rank ADVI). **Historical framing since
  2026-08-24**: the mechanism it describes is unchanged, but the default it
  argues against is gone — NUTS is now the default sampler everywhere. The
  page carries a banner saying so.
- [`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md) — the
  measurement that closed roadmap S1 (2026-08-18): full-rank ADVI vs
  mean-field vs NUTS on fit time and interval width, across the calibration
  DGP, a ridge-geometry suite, and the White Cube tree. Outcome: full-rank
  reproduces NUTS on the synthetic ridge but is slower than NUTS and badly
  over-dispersed on real nodes — not adopted; S2 (k̂) is the path, and shipped
  2026-08-22, closing 2026-08-24 with NUTS as the default sampler. Reproducible
  via
  [`benchmarks/s1_fullrank_advi.py`](benchmarks/s1_fullrank_advi.py).

## Design specs

The _what_ and _why_ behind shipped features (the _how_ lives in the code and in
[`../docs/ai-context/`](../docs/ai-context/)):

- [`ui_design_spec.md`](ui_design_spec.md) — UI design spec
- [`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
  — node bindings and semantic-layer connectivity (roadmap 2.9–2.13): the
  per-node `bind:` fetch descriptor and the scope boundary that keeps it from
  becoming a semantic layer (§4.1 — this amends a standing non-goal); why
  breakdown reads dbt's own `target/semantic_manifest.json` and generates the SQL
  itself, then *proves* agreement with MetricFlow rather than pushing execution
  down; the grain-claim assertion that turns silent fan-out into a startup error;
  non-additive decomposition at entity grain; and the evaluations of Boring
  Semantic Layer (MIT, but zero dbt interop) and Sidemantic (technically
  strongest, AGPL-3.0 — incompatible with our Apache-2.0)
- [`reference_window_design.md`](reference_window_design.md) — reference-window
  defaulting (the matched adjacent block) and provider history discovery
  (roadmap 1.10): why the reference is not the training window, and why "all
  history" is the wrong reference
- [`what_if_design.md`](what_if_design.md) — what-if simulation design spec
- [`cold_start_design.md`](cold_start_design.md) — cold-start mode: what-if
  with zero data, replacing the fitted machine's data-derived inputs with
  declared baselines, slopes and assumption ranges. Shipped as a demo mode
  (see roadmap C7 for its bounds); referenced from
  `breakdown/engine/simulate.py`; companion:
  [`cold_start_founder_intro.md`](cold_start_founder_intro.md), the
  audience-facing pitch the bundled `cold_start_tree.yml` example points at
- [`rate_denominator_policy.md`](rate_denominator_policy.md) — decided and
  shipped (2026-08-15, roadmap 1.12): should a rate be *required* to declare
  its denominator? Permissive parser, `doctor` fails on an unanswered rate,
  revisit mandatory at 1.0
- [`reading_the_numbers.md`](reading_the_numbers.md) — why a data product needs
  a review step that reads the *output*, not just the code: the defect class
  both hostile reviews missed because they inferred from artifacts. Now
  operational as the `read-the-numbers` project skill
- [`multi_tree_design.md`](multi_tree_design.md) — **designed, not built**
  (roadmap 2.16): serving many metric trees from one process, framed around a
  tree per company goal per quarter. The optional `tree:` block (title, owner,
  period, goal) and why every field is optional; filename-stem ids and
  directory discovery; the `TreeState` refactor, per-tree locks and the
  global trace cap; lazy loading, and why the index says *not loaded* rather
  than showing a blank that reads as zero; tree-scoped routes with the current
  paths aliased so nothing existing breaks. Records the three decisions taken
  with their rejected alternatives, and the open questions (archiving a goal
  tree after its deadline, grouping by `period` vs `deadline`)
- [`filter_support_design.md`](filter_support_design.md) — **designed, not built**
  (roadmap 2.17): carrying a dbt metric's `filter` onto the binding and compiling
  it into the generated SQL, replacing the blanket refusal
  [C15](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) had to
  ship. Records the boundary decision this needed —
  [`semantic_layer_connectivity_design.md`](semantic_layer_connectivity_design.md)
  §4.1's line moves from *which fields exist* to *which fields an author may
  write*, because `bind.sql` already expresses every hand-written filter, so
  `where:` is import-only and hand-authoring one is a parse error. Why a filter's
  `{{ Dimension('order__is_food_order') }}` is a resolution problem against the
  semantic graph rather than a column (and why v1 resolves only same-relation
  categorical dimensions, refusing cross-join references because a filter that
  fans out is invisible where a slice that fans out is not); why the predicate
  goes through sqlglot's AST rather than string interpolation; why the grain
  claim moves post-filter; the new `filters narrow` warehouse check that turns a
  constant-true or everything-drops predicate into a startup failure; why slices
  still sum; and an explicit account of what is and is not proven while
  [2.14](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard)
  differential verification stays blocked on `mf` and Python 3.14
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
- [`non_additive_slicing_design.md`](non_additive_slicing_design.md) —
  non-additive metrics at entity grain (roadmap 3.8): why a `count_distinct`
  metric's slices overstate the total, why that is a property of the
  (metric, dimension) pair rather than of the metric, the three tiers of
  capability by what the author declared, and the entity-flow diagnostic that
  labels a platform switch as *migration* instead of two offsetting causes
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

- [`b2b_mrr_tree.yml`](b2b_mrr_tree.yml) — a full B2B SaaS "Total MRR" metric tree (106 metrics, single apex), adapted from Metrics Labs' [B2B Metrics Canvas](https://miro.com/app/board/uXjVNq48sQI=/?share_link_id=353173494684) on Miro. A worked reference for mapping a real-world metric tree onto breakdown's `formula` (deterministic) and probabilistic (`priors`/`lags`) edges, and — since [C10](roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) — for the rest of the schema too: `grain`/`kind` on every node (a deliberate day + month cut), `dimensions` on the sliceable ones, `expected_signs` on every learned edge. Its apex is monthly, so run it over years: `--start-date 2022-01-01 --end-date 2024-12-31`.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

# Smoke tier: bristlecone on its canonical tree

> Smoke tier on a canonical tree (fake_companies feat/canonical-trees, cc8caac), not a finding: three seeds of the generator's canon set, do not cite. The first run with slices served from the series file (Q4). Ran with another job holding the load at 12 to 41.

**Tier:** smoke · **Vertical:** cpg_wholesale (bristlecone) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Verdict column `verdict` (two-sided 1%) decides targets and attribution eligibility; world-set schema_version 2
- Engine commit `821907f5628dc26cd73d1489e8185004378f054f+dirty`; generator version `0.1.0`; manifest `config_sha256` `2c6b531e0cfc4fb8fa068044e5b8a04818f39a1e922980c4ba7cb7aa86cef4f6` (configs/bristlecone_cpg.yaml)
- Tree `/Users/devon/Documents/code/fake_companies-canonical-trees/examples/breakdown_bristlecone_tree.yml` (sha256 `796097eeea9c7de3`, canonical); 9 nodes served
- Run 2026-11-24T07:22:18+00:00; 177 of 177 jobs done, 11 process-minutes; load average at plan 24.48 on 12 cores; wall time 4 min over 2 invocation(s) on [4] shard(s)
- Null windows per world: 8 every 70 days
- Calibration windows: 168 scored
- Single worlds: 9 ranked of 9; off-tree 0; skipped 0
- `brand_budget_cut`: root ['marketing_spend'] (on_tree); target s0: `wholesale_gross_revenue` (fallback_deepest_descendant), s1: `wholesale_gross_revenue` (fallback_deepest_descendant), s2: `wholesale_gross_revenue` (fallback_deepest_descendant)
- `summit_mart_capsules_delisting`: root ['pos_units'] (on_tree); target s0: `wholesale_gross_revenue` (set:verdict), s1: `wholesale_gross_revenue` (set:verdict), s2: `wholesale_gross_revenue` (set:verdict)
- `summit_mart_destock`: root ['shipped_cases'] (on_tree); target s0: `wholesale_gross_revenue` (set:verdict), s1: `wholesale_gross_revenue` (set:verdict), s2: `wholesale_gross_revenue` (set:verdict)
- Served with a caveat: `trade_inventory_build`: 'trade_inventory_build' is served at week grain as breakdown's providers return it (MetricFlow's `first_day`: the week's first day), not by the manifest's 'end_of_period' rule, which gives a different number

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

Calibrated: mean z 0, sd z 1, 4.6% of windows beyond 2, PIT distance 0, lag-1 correlation 0.

**7-day horizon**

| Role | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS | lag-1 corr of z |
|---|---|---|---|---|---|---|---|
| all | 168 | -0.02 ± 0.06 | 0.82 ± 0.18 | 2% ± 1 | 2% ± 3 | 0.09 ± 0.02 | -0.16 ± 0.29 |
| rate | 24 | 0.20 ± 0.37 | 0.62 ± 0.05 | 0% ± 0 | 0% ± 0 | 0.32 ± 0.11 | -0.32 ± 0.52 |
| flow, no parents | 48 | -0.11 ± 0.13 | 0.71 ± 0.16 | 2% ± 4 | 2% ± 4 | 0.26 ± 0.02 | 0.01 ± 0.12 |
| flow, learned parent | 72 | -0.11 ± 0.22 | 0.91 ± 0.20 | 4% ± 0 | 3% ± 2 | 0.16 ± 0.07 | -0.24 ± 0.24 |
| stock | 24 | 0.19 ± 0.07 | 0.88 ± 0.36 | 0% ± 0 | 4% ± 7 | 0.26 ± 0.09 | -0.14 ± 0.52 |

### Q9 Calibration split by the engine's own flags

| Horizon | Flag | n | share of windows | mean z | sd z | \|z\|>2 |
|---|---|---|---|---|---|---|
| 7 | fit_quality=ok | 125 | 74% ± 4 | 0.00 ± 0.11 | 0.86 ± 0.22 | 3% ± 2 |
| 7 | fit_quality=suspect | 43 | 26% ± 4 | -0.10 ± 0.10 | 0.42 ± 0.32 | 0% ± 0 |
| 7 | ppc_status=moderate | 20 | 12% ± 3 | -0.07 ± 0.20 | 1.02 ± 0.22 | 5% ± 8 |
| 7 | ppc_status=ok | 106 | 63% ± 6 | 0.04 ± 0.16 | 0.85 ± 0.15 | 3% ± 0 |
| 7 | ppc_status=severe | 42 | 25% ± 5 | -0.12 ± 0.06 | 0.42 ± 0.32 | 0% ± 0 |

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 9 | 3.28 ± 0.49 | 0.24 ± 0.24 | 56% ± 19 | 8 ± 0 |
| 7 | flow, no parents | 3 | 7.11 ± 0.25 | 0.00 ± 0.00 | 100% ± 0 | 8 ± 0 |
| 7 | flow, learned parent | 6 | 1.36 ± 0.81 | 0.35 ± 0.36 | 33% ± 29 | 8 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 9 | 6.0 ± 0.0 | 56% ± 38 | 89% ± 19 | 0.75 ± 0.22 | 44% ± 19 | 33% ± 0 |
| innovation_contribution | flow, no parents | 3 | 6.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 100% ± 0 | 0% ± 0 |
| innovation_contribution | flow, learned parent | 6 | 6.0 ± 0.0 | 33% ± 58 | 83% ± 29 | 0.62 ± 0.33 | 17% ± 29 | 50% ± 0 |
| innovation_evidence | all | 9 | 6.0 ± 0.0 | 56% ± 19 | 78% ± 19 | 0.70 ± 0.08 | 56% ± 19 | 11% ± 19 |
| innovation_evidence | flow, no parents | 3 | 6.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 100% ± 0 | 0% ± 0 |
| innovation_evidence | flow, learned parent | 6 | 6.0 ± 0.0 | 33% ± 29 | 67% ± 29 | 0.55 ± 0.11 | 33% ± 29 | 17% ± 29 |
| engine_ranked_causes | all | 9 | 6.0 ± 0.0 | 0% ± 0 | 33% ± 0 | 0.32 ± 0.00 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 6.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.25 ± 0.00 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, learned parent | 6 | 6.0 ± 0.0 | 0% ± 0 | 50% ± 0 | 0.35 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 9 | 5.3 ± 0.0 | 33% ± 0 | 100% ± 0 | 0.61 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, no parents | 3 | 6.0 ± 0.0 | 0% ± 0 | 100% ± 0 | 0.50 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, learned parent | 6 | 5.0 ± 0.0 | 50% ± 0 | 100% ± 0 | 0.67 ± 0.00 | 0% ± 0 | 100% ± 0 |

### Q4 Slice localization: the root's slice inside the truth node

The engine's slice attribution on the truth node, over the anomaly's declared window against the default reference window, every member served from the series file. Rank is the root member's place in the engine's order (by excess in the direction of the node's window-over-window gap); |z| is its excess over its interval's half-width / 1.96; a first place lacks evidence unless its `noise_level` is false. For a sliced rate, the engine's call for the root member (`within` when its own rate's term is larger, else `mix`) is checked against the set's verdicts for the member's rate and weight.

| Node kind | rows | slices ranked | top-1 | top-3 | mean reciprocal rank | root \|z\| | top-1 with evidence | first place lacks evidence | engine says localized | top-1 by \|excess\| (contrast) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 12 | 4.0 ± 0.0 | 33% ± 0 | 50% ± 0 | 0.49 ± 0.00 | 21.5 ± 8.7 | 33% ± 0 | 0% ± 0 | 56% ± 19 | 100% ± 0 |
| flow | 12 | 4.0 ± 0.0 | 33% ± 0 | 50% ± 0 | 0.49 ± 0.00 | 21.5 ± 8.7 | 33% ± 0 | 0% ± 0 | 56% ± 19 | 100% ± 0 |

### Q5 Counterfactual attribution: attributed sizes against the twin difference

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval), against the counterfactual: what the anomaly changed, single world against its null twin. The engine answers a different question, window over window, so a formula edge that carries its parents' drift since the reference window is expected to exceed the counterfactual; that excess is the gap innovation-based RCA (branch design/innovation-rca) aims to close. Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / truth); error share is (engine − truth) / |the target's measured counterfactual change|; a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 1.3 ± 0.0 | 0.0 ± 0.0 | 2.7 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign | mean error share | mean \|error share\| |
|---|---|---|---|---|---|---|---|---|---|
| all | 12 | -0.03 ± 0.09 | 0.35 ± 0.04 | 0.39 ± 0.01 | 0% ± 0 | 0% ± 0 | 33% ± 0 | 2.16 ± 0.14 | 2.16 ± 0.14 |
| formula_edge | 6 | -2.29 ± 0.22 |  |  | 0% ± 0 | 0% ± 0 | 100% ± 0 | 3.24 ± 0.21 | 3.24 ± 0.21 |
| root_gap | 3 | 2.03 ± 0.02 | 0.71 ± 0.01 | 0.71 ± 0.01 |  | 0% ± 0 | 0% ± 0 |  |  |
| root_unexplained | 3 | 0.98 ± 0.08 | -0.02 ± 0.08 | 0.06 ± 0.01 |  | 0% ± 0 | 0% ± 0 | 0.00 ± 0.00 | 0.00 ± 0.00 |

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

| Role | fits | median fit seconds | ESS < 400 | any divergence | suspect |
|---|---|---|---|---|---|
| all | 168 | 3.5 ± 0.1 | 10% ± 3 | 20% ± 1 | 26% ± 4 |
| rate | 24 | 3.0 ± 0.1 | 0% ± 0 | 38% ± 0 | 25% ± 25 |
| flow, no parents | 48 | 3.0 ± 0.2 | 4% ± 4 | 6% ± 0 | 50% ± 0 |
| flow, learned parent | 72 | 3.7 ± 0.2 | 18% ± 5 | 24% ± 2 | 18% ± 13 |
| stock | 24 | 4.3 ± 0.7 | 8% ± 7 | 21% ± 14 | 0% ± 0 |

Per single-world analysis: innovation ranking 0 ± 0 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 0 ± 0 fits.

### Q4 Slice rows (cpg_wholesale)

| Anomaly | Truth node | Root slice | rank per seed | engine call | truth call | not run because |
|---|---|---|---|---|---|---|
| `brand_budget_cut` | `marketing_spend` | channel=paid_social | s0: 1/3 (gap −), s1: 1/3 (gap −), s2: 1/3 (gap −) | — | — |  |
| `summit_mart_capsules_delisting` | `pos_units` | category=capsules | s0: 3/3 (gap +), s1: 3/3 (gap +), s2: 3/3 (gap +) | — | — |  |
| `summit_mart_capsules_delisting` | `pos_units` | retailer_banner=summit_mart | s0: 5/5 (gap +), s1: 5/5 (gap +), s2: 5/5 (gap +) | — | — |  |
| `summit_mart_destock` | `shipped_cases` | channel_type=mass | s0: 5/5 (gap +), s1: 5/5 (gap +), s2: 5/5 (gap +) | — | — |  |

### Q5 Which counterfactual attribution sizes the world set can ground (cpg_wholesale)

Truth averages every seed the set holds ([0, 1, 2]). Basis is which set effect matches the engine's window value: `rel_change` (window mean of daily values, for a stock) or `rel_change_aggregated` (the metric's own rule: Σnum/Σden for a rate).

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | basis | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `brand_budget_cut` | `marketing_spend->pos_units` | learned_edge | exactly_zero | rel_change_aggregated | 3 | 0.000 |  | — | 3 | no | verdict exactly_zero |
| `brand_budget_cut` | `pos_units->shipped_cases` | learned_edge | exactly_zero | rel_change_aggregated | 3 | 0.000 |  | — | 3 | no | verdict exactly_zero |
| `brand_budget_cut` | `root:marketing_spend` | root_gap | established_down | rel_change_aggregated | 3 | -0.328 | 1.00 | ±1% | 3 | size | established and precise |
| `brand_budget_cut` | `shipped_cases->wholesale_net_revenue` | formula_edge | exactly_zero | rel_change_aggregated | 3 | 0.000 |  | — | 3 | no | verdict exactly_zero |
| `brand_budget_cut` | `wholesale_net_revenue->wholesale_gross_revenue` | formula_edge | exactly_zero | rel_change_aggregated | 3 | 0.000 |  | — | 3 | no | verdict exactly_zero |
| `summit_mart_destock` | `root:shipped_cases` | root_unexplained | established_down | rel_change_aggregated | 3 | -0.082 | 1.00 | ±2% | 3 | size | established and precise |
| `summit_mart_destock` | `shipped_cases->wholesale_net_revenue` | formula_edge | established_down | rel_change_aggregated | 3 | -0.084 | 0.97 | ±2% | 3 | size | established and precise |
| `summit_mart_destock` | `wholesale_net_revenue->wholesale_gross_revenue` | formula_edge | established_down | rel_change_aggregated | 3 | -0.083 | 1.00 | ±2% | 3 | size | established and precise |

- Attribution not run on `bristlecone.baseline.s0.only.summit_mart_capsules_delisting`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `bristlecone.baseline.s1.only.summit_mart_capsules_delisting`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `bristlecone.baseline.s2.only.summit_mart_capsules_delisting`: error: ValueError: analysis_start/analysis_end is not a valid date

## Not scored yet

- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

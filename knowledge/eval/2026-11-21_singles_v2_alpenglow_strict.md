# Single worlds, schema 2: Alpenglow, strict verdict (demo tree)

> Proof of the schema 2 runner, not a finding: the generator's proof_v2 set (seeds 0-2, null plus two singles each, fake_companies 01bf895), single worlds only (attribution tier), on a demo tree, not a canonical evaluation tree. Do not cite. The engine commit's +dirty is uncommitted docs; breakdown/ was clean, as the engine key records. tees_demand_slump is rooted at units[tees], and the tree has only total units, whose verdict is exactly_zero: its localization asks about a node the anomaly did not move.

**Tier:** attribution · **Vertical:** retail_dtc (alpenglow) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Verdict column `verdict` (two-sided 1%) decides targets and attribution eligibility; world-set schema_version 2
- Engine commit `a97567d9959bd58b75737e0bb92a9a4eacedeb28+dirty`; generator version `0.1.0`; manifest `config_sha256` `e4fe407bc07f846fede320572f28267a7ec3937ecacfba35cc77bb4de80fd32f` (configs/alpenglow_retail_dtc.yaml)
- Tree `demo/alpenglow_tree.yml` (sha256 `c6d0f0660d744aec`, **not a canonical evaluation tree**, bindings `knowledge/benchmarks/evalrunner/bindings/alpenglow_tree.yml`); 19 nodes served
- Run 2026-11-21T05:10:22+00:00; 6 of 6 jobs done, 14 process-minutes; load average at plan 16.22 on 12 cores; wall time 4 min over 1 invocation(s) on [4] shard(s)
- Null windows per world: none
- Calibration windows: 0 scored
- Single worlds: 6 ranked of 6; off-tree 0; skipped 0
- `paid_social_budget_cut`: root ['marketing_spend'] (on_tree); target s0: `refunds` (set:verdict), s1: `refunds` (set:verdict), s2: `refunds` (set:verdict)
- `tees_demand_slump`: root ['units'] (on_tree); target s0: `net_revenue` (fallback_deepest_descendant), s1: `net_revenue` (fallback_deepest_descendant), s2: `net_revenue` (fallback_deepest_descendant)

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

No null windows were scored on this run (its tier scores none).

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 6 | 4.04 ± 0.27 |  |  | 0 ± 0 |
| 7 | flow, no parents | 3 | 6.89 ± 0.81 |  |  | 0 ± 0 |
| 7 | flow, learned parent | 3 | 1.19 ± 0.47 |  |  | 0 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 6 | 16.5 ± 0.0 | 33% ± 29 | 50% ± 0 | 0.46 ± 0.14 | 33% ± 29 | 50% ± 0 |
| innovation_contribution | flow, no parents | 3 | 14.0 ± 0.0 | 67% ± 58 | 100% ± 0 | 0.83 ± 0.29 | 67% ± 58 | 0% ± 0 |
| innovation_contribution | flow, learned parent | 3 | 19.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.09 ± 0.01 | 0% ± 0 | 100% ± 0 |
| innovation_evidence | all | 6 | 16.5 ± 0.0 | 0% ± 0 | 50% ± 0 | 0.33 ± 0.04 | 0% ± 0 | 0% ± 0 |
| innovation_evidence | flow, no parents | 3 | 14.0 ± 0.0 | 0% ± 0 | 100% ± 0 | 0.50 ± 0.00 | 0% ± 0 | 0% ± 0 |
| innovation_evidence | flow, learned parent | 3 | 19.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.16 ± 0.08 | 0% ± 0 | 0% ± 0 |
| engine_ranked_causes | all | 6 | 16.5 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.09 ± 0.02 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 14.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.10 ± 0.00 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, learned parent | 3 | 19.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.08 ± 0.03 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 6 | 16.5 ± 0.0 | 0% ± 0 | 50% ± 0 | 0.19 ± 0.00 | 0% ± 0 | 67% ± 29 |
| naive_relative_change | flow, no parents | 3 | 14.0 ± 0.0 | 0% ± 0 | 100% ± 0 | 0.33 ± 0.00 | 0% ± 0 | 33% ± 58 |
| naive_relative_change | flow, learned parent | 3 | 19.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.05 ± 0.00 | 0% ± 0 | 100% ± 0 |

### Q5 Attribution: attributed sizes against the measured effect

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval). Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / measured); a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 1.5 ± 0.0 | 1.0 ± 0.0 | 6.0 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign |
|---|---|---|---|---|---|---|---|
| all | 9 | 1.27 ± 0.06 | 0.30 ± 0.04 | 0.30 ± 0.04 | 0% ± 0 | 0% ± 0 | 0% ± 0 |
| formula_edge | 6 | 1.39 ± 0.04 | 0.32 ± 0.03 | 0.32 ± 0.03 | 0% ± 0 | 0% ± 0 | 0% ± 0 |
| root_gap | 3 | 1.27 ± 0.06 | 0.24 ± 0.04 | 0.24 ± 0.04 |  | 0% ± 0 | 0% ± 0 |

Direction only (verdict established, size not scorable: see the table): the engine's sign agrees with the measured one in 0% ± 0 of 6 items.

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

Per single-world analysis: innovation ranking 109 ± 28 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 18 ± 0 fits.

### Q5 Which attribution sizes the world set can ground (retail_dtc)

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|
| `paid_social_budget_cut` | `converting_sessions->orders` | formula_edge | open | 3 | -0.050 | 1.12 | — | 0 | no | verdict open |
| `paid_social_budget_cut` | `marketing_spend->paid_sessions` | learned_edge | None | None |  | 1.00 | — | 0 | no | no set verdict |
| `paid_social_budget_cut` | `orders->units` | learned_edge | open | 3 | -0.053 | 1.00 | — | 0 | no | verdict open |
| `paid_social_budget_cut` | `paid_sessions->sessions` | formula_edge | established_down | 3 | -0.162 | 1.00 | ±2% | 0 | size | established and precise |
| `paid_social_budget_cut` | `returned_units->refunds` | learned_edge | established_down | 3 | -0.080 | 1.00 | ±32% | 0 | direction | precision ±32% at the set's 3 seeds (needs ±20%) |
| `paid_social_budget_cut` | `root:marketing_spend` | root_gap | established_down | 3 | -0.286 | 1.00 | ±4% | 3 | size | established and precise |
| `paid_social_budget_cut` | `sessions->converting_sessions` | formula_edge | established_down | 3 | -0.105 | 1.60 | ±16% | 0 | size | established and precise |
| `paid_social_budget_cut` | `units->returned_units` | formula_edge | established_down | 3 | -0.040 | 1.35 | ±40% | 0 | direction | precision ±40% at the set's 3 seeds (needs ±20%) |
| `tees_demand_slump` | `refunds->net_revenue` | formula_edge | open | 3 | 0.049 | -0.02 | — | 3 | no | verdict open |
| `tees_demand_slump` | `returned_units->refunds` | learned_edge | open | 3 | 0.011 | 1.00 | — | 3 | no | verdict open |
| `tees_demand_slump` | `root:units` | root_unexplained | exactly_zero | 3 | 0.000 |  | — | 3 | no | verdict exactly_zero |
| `tees_demand_slump` | `units->returned_units` | formula_edge | open | 3 | 0.006 | 0.00 | — | 3 | no | verdict open |

## Not scored yet

- **Q4 slices:** whether the true slice is the top slice of the true node (spec §5.3)
- **Q5 error share:** attribution error as a share of the target's measured change (spec §5.4)
- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

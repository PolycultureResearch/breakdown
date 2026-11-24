# Single worlds: Alpenglow on its canonical tree

> Smoke tier on a canonical tree (fake_companies feat/canonical-trees, cc8caac), not a finding: three seeds of the generator's canon set, do not cite. The first run with slices served from the series file (Q4). Ran with another job holding the load at 12 to 41. Single worlds only (attribution tier, seeds 0-2): the tees slump, the pants price rise, the email checkout bug and the paid-search suspension, chosen so the slice question has rate, mix and flow cases.

**Tier:** attribution · **Vertical:** retail_dtc (alpenglow) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Verdict column `verdict` (two-sided 1%) decides targets and attribution eligibility; world-set schema_version 2
- Engine commit `821907f5628dc26cd73d1489e8185004378f054f+dirty`; generator version `0.1.0`; manifest `config_sha256` `0fda12e1d04ba35326c50c580f19b60028a69f6c5cdac951751d17cd81d902f4` (configs/alpenglow_retail_dtc.yaml)
- Tree `/Users/devon/Documents/code/fake_companies-canonical-trees/examples/breakdown_alpenglow_tree.yml` (sha256 `7dfe84e47f913f74`, canonical); 18 nodes served
- Run 2026-11-24T07:22:23+00:00; 12 of 12 jobs done, 0 process-minutes; load average at plan 23.96 on 12 cores; wall time 5 min over 2 invocation(s) on [4] shard(s)
- Null windows per world: none
- Calibration windows: 0 scored
- Single worlds: 12 ranked of 12; off-tree 0; skipped 0
- `email_checkout_link_bug`: root ['conversion_rate'] (on_tree); target s0: `converting_sessions` (set:verdict), s1: `converting_sessions` (set:verdict), s2: `converting_sessions` (set:verdict)
- `paid_search_account_suspended`: root ['marketing_spend'] (on_tree); target s0: `net_revenue` (set:verdict), s1: `net_revenue` (set:verdict), s2: `net_revenue` (set:verdict)
- `pants_price_increase`: root ['avg_unit_price'] (on_tree); target s0: `net_revenue` (set:verdict), s1: `net_revenue` (set:verdict), s2: `net_revenue` (set:verdict)
- `tees_demand_slump`: root ['avg_unit_price'] (on_tree_mix); target s0: `net_revenue` (fallback_deepest_descendant), s1: `net_revenue` (fallback_deepest_descendant), s2: `net_revenue` (fallback_deepest_descendant)

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

No null windows were scored on this run (its tier scores none).

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 12 | 2.88 ± 0.37 |  |  | 0 ± 0 |
| 7 | rate | 9 | 1.56 ± 0.31 |  |  | 0 ± 0 |
| 7 | flow, no parents | 3 | 6.84 ± 0.57 |  |  | 0 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 12 | 12.0 ± 0.0 | 75% ± 0 | 83% ± 14 | 0.81 ± 0.02 | 75% ± 0 | 17% ± 14 |
| innovation_contribution | rate | 9 | 11.0 ± 0.0 | 67% ± 0 | 78% ± 19 | 0.75 ± 0.03 | 67% ± 0 | 22% ± 19 |
| innovation_contribution | flow, no parents | 3 | 15.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 100% ± 0 | 0% ± 0 |
| innovation_evidence | all | 12 | 12.0 ± 0.0 | 42% ± 14 | 75% ± 0 | 0.62 ± 0.08 | 42% ± 14 | 0% ± 0 |
| innovation_evidence | rate | 9 | 11.0 ± 0.0 | 56% ± 19 | 67% ± 0 | 0.66 ± 0.11 | 56% ± 19 | 0% ± 0 |
| innovation_evidence | flow, no parents | 3 | 15.0 ± 0.0 | 0% ± 0 | 100% ± 0 | 0.50 ± 0.00 | 0% ± 0 | 0% ± 0 |
| engine_ranked_causes | all | 12 | 12.0 ± 0.0 | 25% ± 0 | 50% ± 0 | 0.43 ± 0.01 | 25% ± 0 | 75% ± 0 |
| engine_ranked_causes | rate | 9 | 11.0 ± 0.0 | 33% ± 0 | 67% ± 0 | 0.55 ± 0.02 | 33% ± 0 | 67% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 15.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.08 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 12 | 11.0 ± 0.0 | 25% ± 0 | 50% ± 0 | 0.39 ± 0.01 | 25% ± 0 | 67% ± 14 |
| naive_relative_change | rate | 9 | 10.3 ± 0.0 | 0% ± 0 | 33% ± 0 | 0.19 ± 0.02 | 0% ± 0 | 89% ± 19 |
| naive_relative_change | flow, no parents | 3 | 13.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 100% ± 0 | 0% ± 0 |

### Q4 Slice localization: the root's slice inside the truth node

The engine's slice attribution on the truth node, over the anomaly's declared window against the default reference window, every member served from the series file. Rank is the root member's place in the engine's order (by excess in the direction of the node's window-over-window gap); |z| is its excess over its interval's half-width / 1.96; a first place lacks evidence unless its `noise_level` is false. For a sliced rate, the engine's call for the root member (`within` when its own rate's term is larger, else `mix`) is checked against the set's verdicts for the member's rate and weight.

| Node kind | rows | slices ranked | top-1 | top-3 | mean reciprocal rank | root \|z\| | top-1 with evidence | first place lacks evidence | engine says localized | top-1 by \|excess\| (contrast) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 12 | 5.2 ± 0.0 | 83% ± 14 | 83% ± 14 | 0.86 ± 0.12 | 27.1 ± 2.1 | 75% ± 0 | 17% ± 14 | 75% ± 0 | 75% ± 0 |
| rate | 9 | 6.3 ± 0.0 | 78% ± 19 | 78% ± 19 | 0.82 ± 0.16 | 27.0 ± 3.6 | 67% ± 0 | 22% ± 19 | 67% ± 0 | 67% ± 0 |
| flow | 3 | 2.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 27.2 ± 3.0 | 100% ± 0 | 0% ± 0 | 100% ± 0 | 100% ± 0 |

Within or mix, sliced rates: the engine's call matches the set's in 100% ± 0 of 6 rows whose truth is one or the other.

### Q5 Counterfactual attribution: attributed sizes against the twin difference

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval), against the counterfactual: what the anomaly changed, single world against its null twin. The engine answers a different question, window over window, so a formula edge that carries its parents' drift since the reference window is expected to exceed the counterfactual; that excess is the gap innovation-based RCA (branch design/innovation-rca) aims to close. Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / truth); error share is (engine − truth) / |the target's measured counterfactual change|; a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 1.0 ± 0.0 | 1.5 ± 0.0 | 3.8 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign | mean error share | mean \|error share\| |
|---|---|---|---|---|---|---|---|---|---|
| all | 12 | 1.06 ± 0.06 | 0.01 ± 0.06 | 0.26 ± 0.02 | 50% ± 0 | 0% ± 0 | 0% ± 0 | 0.01 ± 0.02 | 0.02 ± 0.01 |
| formula_edge | 3 | 0.97 ± 0.07 | -0.03 ± 0.08 | 0.05 ± 0.06 | 100% ± 0 | 0% ± 0 | 0% ± 0 | 0.03 ± 0.07 | 0.04 ± 0.06 |
| learned_edge | 3 | 1.59 ± 0.08 | 0.46 ± 0.05 | 0.46 ± 0.05 | 0% ± 0 | 0% ± 0 | 0% ± 0 | -0.03 ± 0.00 | 0.03 ± 0.00 |
| root_gap | 6 | 0.84 ± 0.06 | -0.20 ± 0.07 | 0.26 ± 0.04 |  | 0% ± 0 | 0% ± 0 | 0.01 ± 0.00 | 0.01 ± 0.00 |

Direction only (verdict established, size not scorable: see the table): the engine's sign agrees with the measured one in 17% ± 17 of 18 items.

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

Per single-world analysis: innovation ranking 1 ± 0 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 0 ± 0 fits.

### Q4 Slice rows (retail_dtc)

| Anomaly | Truth node | Root slice | rank per seed | engine call | truth call | not run because |
|---|---|---|---|---|---|---|
| `email_checkout_link_bug` | `conversion_rate` | channel=email | s0: 1/5 (gap −), s1: 1/5 (gap −), s2: 1/5 (gap −) | within, within, within | within |  |
| `paid_search_account_suspended` | `marketing_spend` | channel=paid_search | s0: 1/2 (gap −), s1: 1/2 (gap −), s2: 1/2 (gap −) | — | — |  |
| `pants_price_increase` | `avg_unit_price` | category=pants | s0: 1/7 (gap +), s1: 1/7 (gap +), s2: 1/7 (gap +) | mix, mix, mix | both |  |
| `tees_demand_slump` | `avg_unit_price` | category=tees | s0: 5/7 (gap +), s1: 7/7 (gap +), s2: 1/7 (gap +) | mix, mix, mix | mix |  |

### Q5 Which counterfactual attribution sizes the world set can ground (retail_dtc)

Truth averages every seed the set holds ([0, 1, 2]). Basis is which set effect matches the engine's window value: `rel_change` (window mean of daily values, for a stock) or `rel_change_aggregated` (the metric's own rule: Σnum/Σden for a rate).

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | basis | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `email_checkout_link_bug` | `conversion_rate->converting_sessions` | formula_edge | established_down | rel_change_aggregated | 3 | -0.095 | 1.00 | ±19% | 3 | size | established and precise |
| `email_checkout_link_bug` | `root:conversion_rate` | root_gap | established_down | rel_change_aggregated | 3 | -0.095 | 1.00 | ±19% | 3 | size | established and precise |
| `paid_search_account_suspended` | `converting_sessions->orders` | formula_edge | established_down | rel_change_aggregated | 3 | -0.065 | 0.94 | — | 0 | direction | effect_after_window: the measured onset is 152 days after the declared start, past the 14-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `paid_search_account_suspended` | `gross_revenue->net_revenue` | formula_edge | established_down | rel_change_aggregated | 3 | -0.072 | 1.02 | ±35% | 0 | direction | precision ±35% at the set's 3 seeds (needs ±20%) |
| `paid_search_account_suspended` | `marketing_spend->sessions` | learned_edge | established_down | rel_change_aggregated | 3 | -0.129 | 1.00 | ±13% | 0 | size | established and precise |
| `paid_search_account_suspended` | `orders->units` | learned_edge | established_down | rel_change_aggregated | 3 | -0.068 | 1.00 | — | 0 | direction | effect_after_window: the measured onset is 21 days after the declared start, past the 14-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `paid_search_account_suspended` | `refunds->net_revenue` | formula_edge | established_down | rel_change_aggregated | 3 | -0.072 | -0.02 | ±35% | 0 | direction | precision ±35% at the set's 3 seeds (needs ±20%) |
| `paid_search_account_suspended` | `returned_units->refunds` | learned_edge | open | rel_change_aggregated | 3 | -0.018 | 1.00 | — | 0 | no | effect_after_window: the measured onset is 172 days after the declared start, past the 14-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `paid_search_account_suspended` | `root:marketing_spend` | root_gap | established_down | rel_change_aggregated | 3 | -0.397 | 1.00 | ±11% | 3 | size | established and precise |
| `paid_search_account_suspended` | `sessions->converting_sessions` | formula_edge | established_down | rel_change_aggregated | 3 | -0.147 | 0.86 | — | 0 | direction | effect_after_window: the measured onset is 146 days after the declared start, past the 14-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `paid_search_account_suspended` | `units->gross_revenue` | formula_edge | established_down | rel_change_aggregated | 3 | -0.068 | 1.00 | — | 0 | direction | effect_after_window: the measured onset is 172 days after the declared start, past the 14-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `paid_search_account_suspended` | `units->returned_units` | formula_edge | open | rel_change_aggregated | 3 | -0.002 | -1.21 | — | 0 | no | verdict open |
| `tees_demand_slump` | `avg_unit_price->gross_revenue` | formula_edge | open | rel_change_aggregated | 3 | 0.046 | 1.00 | — | 3 | no | verdict open |
| `tees_demand_slump` | `gross_revenue->net_revenue` | formula_edge | open | rel_change_aggregated | 3 | 0.049 | 1.02 | — | 3 | no | verdict open |
| `tees_demand_slump` | `root:avg_unit_price` | root_gap | open | rel_change_aggregated | 3 | 0.046 | 1.00 | — | 3 | no | verdict open |

- Attribution not run on `alpenglow.baseline.s0.only.pants_price_increase`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `alpenglow.baseline.s1.only.pants_price_increase`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `alpenglow.baseline.s2.only.pants_price_increase`: error: ValueError: analysis_start/analysis_end is not a valid date

## Not scored yet

- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

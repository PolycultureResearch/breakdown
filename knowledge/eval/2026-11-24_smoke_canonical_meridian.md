# Smoke tier: meridian on its canonical tree, stocks at period end

> Smoke tier on a canonical tree, not a finding: three seeds of a canon set rebuilt at fake_companies 817de23 (stocks answer MetricFlow by their last day, spec §10.10), do not cite. Stocks are their week-end level; the 2026-11-23 run served Meridian's summed. Another job held the load at up to 28 during the run.

**Tier:** smoke · **Vertical:** b2b_services (meridian) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Verdict column `verdict` (two-sided 1%) decides targets and attribution eligibility; world-set schema_version 2
- Engine commit `af29690ba24860d42135ab9945d42e320be1107b+dirty`; generator version `0.1.0`; manifest `config_sha256` `0c2fed812821dbda3643ec93159283a7e4e76423eaf98d40e028b1f56b710db0` (configs/meridian_b2b_services.yaml)
- Tree `/Users/devon/Documents/code/fake_companies-canonical-trees/examples/breakdown_meridian_tree.yml` (sha256 `14379d3f5da1172f`, canonical); 15 nodes served
- Run 2026-11-24T15:59:37+00:00; 297 of 297 jobs done, 18 process-minutes; load average at plan 9.79 on 12 cores; wall time 5 min over 2 invocation(s) on [4] shard(s)
- Null windows per world: 8 every 70 days
- Calibration windows: 192 scored; 72 not scored (unassessed, fit failed: Cannot fit over undefined periods: 'sales_cycle_days'); 24 not scored (unassessed, fit failed: Cannot fit over undefined periods: 'avg_deal_size')
- Single worlds: 9 ranked of 9; off-tree 0; skipped 0
- `delivery_capacity_crunch`: root ['invoiced_revenue'] (on_tree); target s0: `collected_cash` (fallback_deepest_descendant), s1: `collected_cash` (fallback_deepest_descendant), s2: `collected_cash` (fallback_deepest_descendant)
- `discounting_creep`: root ['avg_deal_size'] (on_tree); target s0: `bookings` (set:verdict), s1: `bookings` (set:verdict), s2: `bookings` (set:verdict)
- `referral_partner_exit`: root ['leads'] (on_tree); target s0: `closed_deals` (set:verdict), s1: `closed_deals` (set:verdict), s2: `closed_deals` (set:verdict)

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

Calibrated: mean z 0, sd z 1, 4.6% of windows beyond 2, PIT distance 0, lag-1 correlation 0.

**7-day horizon**

| Role | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS | lag-1 corr of z |
|---|---|---|---|---|---|---|---|
| all | 192 | 0.01 ± 0.08 | 1.10 ± 0.13 | 10% ± 2 | 7% ± 4 | 0.11 ± 0.02 | -0.06 ± 0.06 |
| flow, no parents | 24 | 0.43 ± 0.17 | 1.22 ± 0.25 | 17% ± 7 | 8% ± 14 | 0.33 ± 0.04 | 0.37 ± 0.27 |
| flow, learned parent | 144 | -0.10 ± 0.10 | 1.10 ± 0.17 | 9% ± 4 | 8% ± 3 | 0.14 ± 0.04 | -0.11 ± 0.07 |
| stock | 24 | 0.29 ± 0.40 | 0.90 ± 0.20 | 8% ± 14 | 0% ± 0 | 0.29 ± 0.02 | -0.19 ± 0.11 |

### Q9 Calibration split by the engine's own flags

| Horizon | Flag | n | share of windows | mean z | sd z | \|z\|>2 |
|---|---|---|---|---|---|---|
| 7 | fit_quality=ok | 159 | 83% ± 10 | 0.03 ± 0.06 | 1.09 ± 0.08 | 10% ± 1 |
| 7 | fit_quality=suspect | 33 | 17% ± 10 | 0.02 ± 0.34 | 1.27 ± 0.47 | 12% ± 13 |
| 7 | ppc_status=moderate | 43 | 22% ± 7 | 0.06 ± 0.22 | 0.97 ± 0.23 | 6% ± 6 |
| 7 | ppc_status=ok | 117 | 61% ± 16 | 0.05 ± 0.08 | 1.07 ± 0.17 | 10% ± 0 |
| 7 | ppc_status=severe | 32 | 17% ± 10 | 0.01 ± 0.32 | 1.27 ± 0.47 | 12% ± 13 |

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 6 | 0.86 ± 0.77 | 0.54 ± 0.34 | 0% ± 0 | 8 ± 0 |
| 7 | flow, no parents | 3 | 0.94 ± 0.96 | 0.67 ± 0.36 | 0% ± 0 | 8 ± 0 |
| 7 | flow, learned parent | 3 | 0.78 ± 0.71 | 0.42 ± 0.40 | 0% ± 0 | 8 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 9 | 4.7 ± 0.0 | 44% ± 19 | 67% ± 0 | 0.56 ± 0.10 | 0% ± 0 | 100% ± 0 |
| innovation_contribution | rate | 3 | 4.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| innovation_contribution | flow, no parents | 3 | 5.0 ± 0.0 | 33% ± 58 | 100% ± 0 | 0.67 ± 0.29 | 0% ± 0 | 100% ± 0 |
| innovation_contribution | flow, learned parent | 3 | 5.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| innovation_evidence | all | 9 | 4.7 ± 0.0 | 0% ± 0 | 22% ± 38 | 0.19 ± 0.08 | 0% ± 0 | 67% ± 0 |
| innovation_evidence | rate | 3 | 4.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.00 ± 0.00 | 0% ± 0 | 67% ± 58 |
| innovation_evidence | flow, no parents | 3 | 5.0 ± 0.0 | 0% ± 0 | 33% ± 58 | 0.32 ± 0.16 | 0% ± 0 | 33% ± 58 |
| innovation_evidence | flow, learned parent | 3 | 5.0 ± 0.0 | 0% ± 0 | 33% ± 58 | 0.26 ± 0.07 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | all | 9 | 5.7 ± 0.0 | 56% ± 19 | 100% ± 0 | 0.74 ± 0.12 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | rate | 3 | 5.0 ± 0.0 | 67% ± 58 | 100% ± 0 | 0.83 ± 0.29 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 5.0 ± 0.0 | 0% ± 0 | 100% ± 0 | 0.39 ± 0.10 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, learned parent | 3 | 7.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 9 | 7.7 ± 0.0 | 22% ± 19 | 44% ± 19 | 0.41 ± 0.07 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | rate | 3 | 7.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.19 ± 0.06 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, no parents | 3 | 7.0 ± 0.0 | 0% ± 0 | 33% ± 58 | 0.27 ± 0.20 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, learned parent | 3 | 9.0 ± 0.0 | 67% ± 58 | 100% ± 0 | 0.78 ± 0.38 | 0% ± 0 | 100% ± 0 |

### Q4 Slice localization: the root's slice inside the truth node

The engine's slice attribution on the truth node, over the anomaly's declared window against the default reference window, every member served from the series file. Rank is the root member's place in the engine's order (by excess in the direction of the node's window-over-window gap); |z| is its excess over its interval's half-width / 1.96; a first place lacks evidence unless its `noise_level` is false. For a sliced rate, the engine's call for the root member (`within` when its own rate's term is larger, else `mix`) is checked against the set's verdicts for the member's rate and weight.

| Node kind | rows | slices ranked | top-1 | top-3 | mean reciprocal rank | root \|z\| | top-1 with evidence | first place lacks evidence | engine says localized | top-1 by \|excess\| (contrast) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 3 | 4.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.25 ± 0.00 | 28.8 ± 6.9 | 0% ± 0 | 0% ± 0 | 100% ± 0 | 100% ± 0 |
| flow | 3 | 4.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.25 ± 0.00 | 28.8 ± 6.9 | 0% ± 0 | 0% ± 0 | 100% ± 0 | 100% ± 0 |

### Q5 Counterfactual attribution: attributed sizes against the twin difference

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval), against the counterfactual: what the anomaly changed, single world against its null twin. The engine answers a different question, window over window, so a formula edge that carries its parents' drift since the reference window is expected to exceed the counterfactual; that excess is the gap innovation-based RCA (branch design/innovation-rca) aims to close. Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / truth); error share is (engine − truth) / |the target's measured counterfactual change|; a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 0.0 ± 0.0 | 1.0 ± 0.0 | 1.3 ± 0.0 |

No item's size could be scored on this run: see the eligibility table.

Direction only (verdict established, size not scorable: see the table): the engine's sign agrees with the measured one in 100% ± 0 of 9 items.

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

| Role | fits | median fit seconds | ESS < 400 | any divergence | suspect |
|---|---|---|---|---|---|
| all | 192 | 3.4 ± 0.1 | 1% ± 1 | 16% ± 5 | 17% ± 10 |
| flow, no parents | 24 | 2.8 ± 0.1 | 0% ± 0 | 8% ± 14 | 0% ± 0 |
| flow, learned parent | 144 | 3.4 ± 0.1 | 1% ± 1 | 17% ± 5 | 23% ± 13 |
| stock | 24 | 3.6 ± 0.2 | 4% ± 7 | 12% ± 12 | 0% ± 0 |

Per single-world analysis: innovation ranking 18 ± 1 s, engine RCA 11 ± 0 s (after the fits the innovation pass already made), 15 ± 0 fits.

### Q4 Slice rows (b2b_services)

| Anomaly | Truth node | Root slice | rank per seed | engine call | truth call | not run because |
|---|---|---|---|---|---|---|
| `referral_partner_exit` | `leads` | source=referral | s0: 4/4 (gap +), s1: 4/4 (gap +), s2: 4/4 (gap +) | — | — |  |

### Q5 Which counterfactual attribution sizes the world set can ground (b2b_services)

Truth averages every seed the set holds ([0, 1, 2]). Basis is which set effect matches the engine's window value: `rel_change` (window mean of daily values, for a stock) or `rel_change_aggregated` (the metric's own rule: Σnum/Σden for a rate).

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | basis | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `delivery_capacity_crunch` | `invoiced_revenue->collected_cash` | learned_edge | open | rel_change_aggregated | 3 | 0.005 | 1.00 | — | 0 | no | effect_after_window: the measured onset is 136 days after the declared start, past the 56-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `delivery_capacity_crunch` | `root:invoiced_revenue` | root_unexplained | established_down | rel_change_aggregated | 3 | -0.070 | 1.00 | — | 3 | direction | effect_after_window: the measured onset is 63 days after the declared start, past the 56-day window the set's effect covers; measuring the shifted window from the twins (generator spec §10.7) is not built |
| `discounting_creep` | `avg_deal_size->bookings` | formula_edge | established_down | rel_change_aggregated | 3 | -0.077 | 1.00 | ±35% | 3 | direction | precision ±35% at the set's 3 seeds (needs ±20%) |
| `discounting_creep` | `root:avg_deal_size` | root_gap | established_down | rel_change_aggregated | 3 | -0.077 | 1.00 | ±35% | 3 | direction | precision ±35% at the set's 3 seeds (needs ±20%) |

- Attribution not run on `meridian.baseline.s0.only.referral_partner_exit`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `meridian.baseline.s1.only.referral_partner_exit`: error: ValueError: analysis_start/analysis_end is not a valid date
- Attribution not run on `meridian.baseline.s2.only.referral_partner_exit`: error: ValueError: analysis_start/analysis_end is not a valid date

## Not scored yet

- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

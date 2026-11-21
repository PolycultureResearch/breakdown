# Smoke-tier baseline: White Cube on the demo tree (not a canonical evaluation tree)

> A baseline for the pipeline, not for the engine. Three seeds, eight null windows per node and three anomalies are too few to conclude anything about the engine, and the tree is the demo tree, not a canonical evaluation tree. Do not cite these numbers as findings. It replaces the 2026-11-19 baseline after the runner moved to the generator's stated contract (fake_companies b51d70e); the three anomalies are pinned with --singles to the ones that baseline ran, so the two pair world for world. Q5 attribution was added on 2026-11-21 by rerunning the nine single worlds only (calibration rows and fits reused). The engine commit's +dirty is the then-uncommitted runner fix that became 3656f58, and docs; breakdown/ was clean, as the engine key records.

**Tier:** smoke · **Vertical:** b2c_saas (white_cube) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Engine commit `711826ec0e4ea6ab1334623e970b3445d3713173+dirty`; generator version `0.1.0`; manifest `config_sha256` `6517547081b833f9c872f2663578a89a84dcfdac3424bedafc13003d21234c1a` (configs/white_cube_b2c_app.yaml)
- Tree `demo/white_cube_tree.yml` (sha256 `d8e50fe756cba33a`, **not a canonical evaluation tree**); 23 nodes served
- Run 2026-11-21T04:31:20+00:00; 393 of 393 jobs done, 27 process-minutes; load average at plan 4.86 on 12 cores; wall time 10 min over 3 invocation(s) on [4] shard(s)
- Null windows per world: 8 every 77 days
- Calibration windows: 360 scored; 24 not scored (unassessed, fit failed: Cannot fit over undefined periods: 'churn_arpu')
- Single worlds: 9 ranked of 9; off-tree 0; skipped 0
- `latam_campaign_spend_ramp`: root ['marketing_spend'] (on_tree); target s0: `sessions` (set_verdict), s1: `sessions` (set_verdict), s2: `sessions` (set_verdict)
- `mobile_signup_cta_regression`: root ['visit_signup_rate'] (on_tree); target s0: `signups` (set_verdict), s1: `signups` (set_verdict), s2: `signups` (set_verdict)
- `professional_churn_spike`: root ['customer_churn_rate'] (on_tree_rate); target s0: `net_new_mrr` (fallback_deepest_descendant), s1: `net_new_mrr` (fallback_deepest_descendant), s2: `net_new_mrr` (fallback_deepest_descendant)
- Served with a caveat: `active_subscriptions`: 'active_subscriptions' is a distinct count per day; at grain 'week' it is served as the last day's count, not MetricFlow's distinct count over the period

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

Calibrated: mean z 0, sd z 1, 4.6% of windows beyond 2, PIT distance 0, lag-1 correlation 0.

**7-day horizon**

| Role | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS | lag-1 corr of z |
|---|---|---|---|---|---|---|---|
| all | 360 | 0.35 ± 0.09 | 1.05 ± 0.15 | 10% ± 3 | 6% ± 3 | 0.15 ± 0.03 | -0.16 ± 0.07 |
| rate | 168 | 0.07 ± 0.08 | 0.69 ± 0.09 | 1% ± 1 | 0% ± 0 | 0.15 ± 0.02 | -0.20 ± 0.02 |
| flow, no parents | 120 | 0.59 ± 0.21 | 1.45 ± 0.29 | 20% ± 4 | 13% ± 8 | 0.26 ± 0.09 | -0.14 ± 0.14 |
| flow, learned parent | 48 | 0.16 ± 0.17 | 1.09 ± 0.35 | 15% ± 10 | 10% ± 13 | 0.23 ± 0.06 | -0.23 ± 0.17 |
| stock | 24 | 1.52 ± 0.02 | 0.35 ± 0.03 | 12% ± 12 | 0% ± 0 | 0.85 ± 0.01 | 0.09 ± 0.37 |

### Q9 Calibration split by the engine's own flags

| Horizon | Flag | n | share of windows | mean z | sd z | \|z\|>2 |
|---|---|---|---|---|---|---|
| 7 | fit_quality=ok | 234 | 65% ± 3 | 0.31 ± 0.03 | 0.84 ± 0.06 | 6% ± 2 |
| 7 | fit_quality=suspect | 126 | 35% ± 3 | 0.42 ± 0.21 | 1.37 ± 0.34 | 16% ± 5 |
| 7 | ppc_status=moderate | 77 | 21% ± 1 | 0.32 ± 0.14 | 1.06 ± 0.27 | 12% ± 3 |
| 7 | ppc_status=ok | 157 | 44% ± 2 | 0.30 ± 0.11 | 0.69 ± 0.06 | 4% ± 2 |
| 7 | ppc_status=severe | 126 | 35% ± 3 | 0.42 ± 0.21 | 1.37 ± 0.34 | 16% ± 5 |

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 9 | 0.46 ± 0.14 | 0.53 ± 0.15 | 11% ± 19 | 8 ± 0 |
| 7 | rate | 6 | 0.45 ± 0.09 | 0.44 ± 0.11 | 17% ± 29 | 8 ± 0 |
| 7 | flow, no parents | 3 | 0.46 ± 0.23 | 0.71 ± 0.26 | 0% ± 0 | 8 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 9 | 8.0 ± 0.0 | 89% ± 19 | 100% ± 0 | 0.94 ± 0.10 | 11% ± 19 | 89% ± 19 |
| innovation_contribution | rate | 6 | 11.5 ± 0.0 | 83% ± 29 | 100% ± 0 | 0.92 ± 0.14 | 17% ± 29 | 83% ± 29 |
| innovation_contribution | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| innovation_evidence | all | 9 | 8.0 ± 0.0 | 67% ± 0 | 89% ± 19 | 0.77 ± 0.07 | 0% ± 0 | 67% ± 0 |
| innovation_evidence | rate | 6 | 11.5 ± 0.0 | 50% ± 0 | 83% ± 29 | 0.66 ± 0.10 | 0% ± 0 | 50% ± 0 |
| innovation_evidence | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | all | 9 | 8.7 ± 0.0 | 56% ± 19 | 67% ± 0 | 0.68 ± 0.11 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | rate | 6 | 12.5 ± 0.0 | 33% ± 29 | 50% ± 0 | 0.52 ± 0.17 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 9 | 8.7 ± 0.0 | 56% ± 19 | 67% ± 0 | 0.67 ± 0.14 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | rate | 6 | 12.5 ± 0.0 | 33% ± 29 | 50% ± 0 | 0.50 ± 0.22 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |

### Q5 Attribution: attributed sizes against the measured effect

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval). Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / measured); a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 1.3 ± 0.0 | 0.0 ± 0.0 | 2.7 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign |
|---|---|---|---|---|---|---|---|
| all | 12 | 0.64 ± 0.14 | -0.48 ± 0.23 | 0.48 ± 0.23 | 67% ± 29 | 0% ± 0 | 0% ± 0 |
| formula_edge | 3 | 0.73 ± 0.15 | -0.33 ± 0.22 | 0.33 ± 0.22 | 100% ± 0 | 0% ± 0 | 0% ± 0 |
| learned_edge | 3 | 0.54 ± 0.15 | -0.64 ± 0.26 | 0.64 ± 0.26 | 33% ± 58 | 0% ± 0 | 0% ± 0 |
| root_gap | 6 | 0.64 ± 0.15 | -0.47 ± 0.24 | 0.47 ± 0.24 |  | 0% ± 0 | 0% ± 0 |

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

| Role | fits | median fit seconds | ESS < 400 | any divergence | suspect |
|---|---|---|---|---|---|
| all | 360 | 3.5 ± 0.0 | 1% ± 0 | 19% ± 3 | 35% ± 3 |
| rate | 168 | 3.6 ± 0.1 | 1% ± 1 | 35% ± 7 | 8% ± 7 |
| flow, no parents | 120 | 3.2 ± 0.2 | 0% ± 0 | 4% ± 1 | 80% ± 2 |
| flow, learned parent | 48 | 8.3 ± 0.5 | 0% ± 0 | 8% ± 4 | 33% ± 13 |
| stock | 24 | 3.1 ± 0.2 | 0% ± 0 | 0% ± 0 | 0% ± 0 |

Per single-world analysis: innovation ranking 0 ± 0 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 0 ± 0 fits.

### Q5 Which attribution sizes the world set can ground (b2c_saas)

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|
| `latam_campaign_spend_ramp` | `marketing_spend->sessions` | learned_edge | established_up | 3 | 0.058 | 1.00 | ±14% | 0 | size | established and precise |
| `latam_campaign_spend_ramp` | `root:marketing_spend` | root_gap | established_up | 3 | 0.091 | 1.00 | ±6% | 3 | size | established and precise |
| `mobile_signup_cta_regression` | `root:visit_signup_rate` | root_gap | established_down | 3 | -0.065 | 1.00 | ±9% | 3 | size | established and precise |
| `mobile_signup_cta_regression` | `visit_signup_rate->signups` | formula_edge | established_down | 3 | -0.064 | 1.00 | ±10% | 3 | size | established and precise |
| `professional_churn_spike` | `churned_mrr->net_new_mrr` | formula_edge | open | 3 | -0.247 | 1.04 | — | 0 | no | verdict open |
| `professional_churn_spike` | `churned_subscriptions->churned_mrr` | formula_edge | open | 3 | 0.528 | 0.62 | — | 0 | no | verdict open |
| `professional_churn_spike` | `customer_churn_rate->churned_subscriptions` | formula_edge | open | 3 | 0.307 | 1.06 | — | 0 | no | verdict open |
| `professional_churn_spike` | `root:customer_churn_rate` | root_unexplained | open | 3 | 0.326 | 1.00 | — | 0 | no | verdict open |

## Not scored yet

- **Q4 slices:** whether the true slice is the top slice of the true node (spec §5.3)
- **Q5 error share:** attribution error as a share of the target's measured change (spec §5.4)
- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

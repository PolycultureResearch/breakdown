# Smoke tier, schema 2: White Cube, loose verdict (demo tree)

> Proof of the schema 2 runner, not a finding: the generator's proof_v2 set (seeds 0-2, null plus two singles each, fake_companies 01bf895) on a demo tree, not a canonical evaluation tree. Do not cite. The engine commit's +dirty is uncommitted docs; breakdown/ was clean, as the engine key records. Loose verdict (verdict_10pct, two-sided 10%): calibration rows and fits are the strict run's, reused; only the single worlds were rerun.

**Tier:** smoke · **Vertical:** b2c_saas (white_cube) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Verdict column `verdict_10pct` (two-sided 10%) decides targets and attribution eligibility; **a loose verdict: about one in ten pairs with no effect is established by chance**; world-set schema_version 2
- Engine commit `a97567d9959bd58b75737e0bb92a9a4eacedeb28+dirty`; generator version `0.1.0`; manifest `config_sha256` `6517547081b833f9c872f2663578a89a84dcfdac3424bedafc13003d21234c1a` (configs/white_cube_b2c_app.yaml)
- Tree `demo/white_cube_tree.yml` (sha256 `d8e50fe756cba33a`, **not a canonical evaluation tree**); 23 nodes served
- Run 2026-11-21T05:10:11+00:00; 390 of 390 jobs done, 31 process-minutes; load average at plan 17.62 on 12 cores; wall time 12 min over 2 invocation(s) on [4] shard(s)
- Null windows per world: 8 every 77 days
- Calibration windows: 360 scored; 24 not scored (unassessed, fit failed: Cannot fit over undefined periods: 'churn_arpu')
- Single worlds: 6 ranked of 6; off-tree 0; skipped 0
- `onboarding_revamp`: root ['trial_days_active'] (on_tree); target s0: `net_new_mrr` (set:verdict_10pct), s1: `net_new_mrr` (set:verdict_10pct), s2: `net_new_mrr` (set:verdict_10pct)
- `professional_churn_spike`: root ['customer_churn_rate'] (on_tree_rate); target s0: `net_new_mrr` (set:verdict_10pct), s1: `net_new_mrr` (set:verdict_10pct), s2: `net_new_mrr` (set:verdict_10pct)

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

Calibrated: mean z 0, sd z 1, 4.6% of windows beyond 2, PIT distance 0, lag-1 correlation 0.

**7-day horizon**

| Role | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS | lag-1 corr of z |
|---|---|---|---|---|---|---|---|
| all | 360 | 0.35 ± 0.09 | 1.05 ± 0.15 | 9% ± 2 | 6% ± 3 | 0.15 ± 0.03 | -0.16 ± 0.07 |
| rate | 168 | 0.07 ± 0.08 | 0.69 ± 0.09 | 1% ± 1 | 0% ± 0 | 0.15 ± 0.02 | -0.20 ± 0.02 |
| flow, no parents | 120 | 0.59 ± 0.21 | 1.45 ± 0.29 | 20% ± 4 | 13% ± 8 | 0.26 ± 0.09 | -0.14 ± 0.14 |
| flow, learned parent | 48 | 0.16 ± 0.17 | 1.09 ± 0.35 | 15% ± 10 | 10% ± 13 | 0.23 ± 0.06 | -0.23 ± 0.17 |
| stock | 24 | 1.48 ± 0.03 | 0.32 ± 0.02 | 0% ± 0 | 0% ± 0 | 0.85 ± 0.02 | -0.19 ± 0.47 |

### Q9 Calibration split by the engine's own flags

| Horizon | Flag | n | share of windows | mean z | sd z | \|z\|>2 |
|---|---|---|---|---|---|---|
| 7 | fit_quality=ok | 233 | 65% ± 3 | 0.30 ± 0.04 | 0.84 ± 0.06 | 5% ± 1 |
| 7 | fit_quality=suspect | 127 | 35% ± 3 | 0.42 ± 0.21 | 1.37 ± 0.34 | 16% ± 5 |
| 7 | ppc_status=moderate | 74 | 21% ± 1 | 0.33 ± 0.14 | 1.09 ± 0.30 | 12% ± 4 |
| 7 | ppc_status=ok | 160 | 44% ± 2 | 0.29 ± 0.11 | 0.68 ± 0.06 | 2% ± 0 |
| 7 | ppc_status=severe | 126 | 35% ± 3 | 0.42 ± 0.21 | 1.37 ± 0.34 | 16% ± 5 |

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 6 | 3.08 ± 0.69 | 0.06 ± 0.06 | 67% ± 29 | 8 ± 0 |
| 7 | rate | 6 | 3.08 ± 0.69 | 0.06 ± 0.06 | 67% ± 29 | 8 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 6 | 20.0 ± 0.0 | 50% ± 0 | 100% ± 0 | 0.72 ± 0.05 | 33% ± 29 | 33% ± 29 |
| innovation_contribution | rate | 6 | 20.0 ± 0.0 | 50% ± 0 | 100% ± 0 | 0.72 ± 0.05 | 33% ± 29 | 33% ± 29 |
| innovation_evidence | all | 6 | 20.0 ± 0.0 | 50% ± 0 | 67% ± 29 | 0.64 ± 0.10 | 50% ± 0 | 0% ± 0 |
| innovation_evidence | rate | 6 | 20.0 ± 0.0 | 50% ± 0 | 67% ± 29 | 0.64 ± 0.10 | 50% ± 0 | 0% ± 0 |
| engine_ranked_causes | all | 6 | 22.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.15 ± 0.02 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | rate | 6 | 22.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.15 ± 0.02 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 6 | 22.0 ± 0.0 | 17% ± 29 | 50% ± 0 | 0.44 ± 0.16 | 17% ± 29 | 67% ± 29 |
| naive_relative_change | rate | 6 | 22.0 ± 0.0 | 17% ± 29 | 50% ± 0 | 0.44 ± 0.16 | 17% ± 29 | 67% ± 29 |

### Q5 Attribution: attributed sizes against the measured effect

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval). Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / measured); a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 1.0 ± 0.0 | 4.0 ± 0.0 | 5.0 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign |
|---|---|---|---|---|---|---|---|
| all | 6 | 2.26 ± 0.05 | 0.62 ± 0.03 | 0.65 ± 0.01 | 0% ± 0 | 0% ± 0 | 0% ± 0 |
| formula_edge | 3 | 3.53 ± 0.08 | 1.26 ± 0.02 | 1.26 ± 0.02 | 0% ± 0 | 0% ± 0 | 0% ± 0 |
| root_gap | 3 | 0.98 ± 0.04 | -0.02 ± 0.04 | 0.03 ± 0.01 |  | 0% ± 0 | 0% ± 0 |

Direction only (verdict established, size not scorable: see the table): the engine's sign agrees with the measured one in 100% ± 0 of 24 items.

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

| Role | fits | median fit seconds | ESS < 400 | any divergence | suspect |
|---|---|---|---|---|---|
| all | 360 | 4.0 ± 0.2 | 1% ± 0 | 19% ± 1 | 35% ± 3 |
| rate | 168 | 4.1 ± 0.2 | 1% ± 1 | 35% ± 3 | 9% ± 8 |
| flow, no parents | 120 | 3.5 ± 0.2 | 0% ± 0 | 4% ± 1 | 80% ± 2 |
| flow, learned parent | 48 | 9.1 ± 0.8 | 0% ± 0 | 8% ± 4 | 33% ± 13 |
| stock | 24 | 3.5 ± 0.3 | 0% ± 0 | 0% ± 0 | 0% ± 0 |

Per single-world analysis: innovation ranking 1 ± 0 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 0 ± 0 fits.

### Q5 Which attribution sizes the world set can ground (b2c_saas)

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|
| `onboarding_revamp` | `new_mrr->net_new_mrr` | formula_edge | established_up | 3 | 0.270 | 0.92 | ±32% | 0 | direction | precision ±32% at the set's 3 seeds (needs ±20%) |
| `onboarding_revamp` | `new_subscriptions->new_mrr` | formula_edge | established_up | 3 | 0.199 | 1.03 | ±11% | 0 | size | established and precise |
| `onboarding_revamp` | `root:trial_days_active` | root_gap | established_up | 3 | 0.900 | 1.00 | ±0% | 0 | size | established and precise |
| `onboarding_revamp` | `trial_conversion_rate->trial_conversions` | formula_edge | established_up | 3 | 0.336 | 1.00 | ±35% | 0 | direction | precision ±35% at the set's 3 seeds (needs ±20%) |
| `onboarding_revamp` | `trial_conversions->new_subscriptions` | formula_edge | established_up | 3 | 0.206 | 0.96 | ±44% | 0 | direction | precision ±44% at the set's 3 seeds (needs ±20%) |
| `onboarding_revamp` | `trial_days_active->trial_conversion_rate` | learned_edge | established_up | 3 | 0.332 |  | — | 0 | no | several_parents_moved: ['trial_activation_rate'] |
| `professional_churn_spike` | `churned_mrr->net_new_mrr` | formula_edge | established_down | 3 | -0.247 | 1.04 | ±82% | 0 | direction | precision ±82% at the set's 3 seeds (needs ±20%) |
| `professional_churn_spike` | `churned_subscriptions->churned_mrr` | formula_edge | established_up | 3 | 0.528 | 0.62 | ±56% | 0 | direction | precision ±56% at the set's 3 seeds (needs ±20%) |
| `professional_churn_spike` | `customer_churn_rate->churned_subscriptions` | formula_edge | established_up | 3 | 0.307 | 1.05 | ±80% | 0 | direction | precision ±80% at the set's 3 seeds (needs ±20%) |
| `professional_churn_spike` | `root:customer_churn_rate` | root_unexplained | established_up | 3 | 0.326 | 1.00 | ±82% | 0 | direction | precision ±82% at the set's 3 seeds (needs ±20%) |

## Not scored yet

- **Q4 slices:** whether the true slice is the top slice of the true node (spec §5.3)
- **Q5 error share:** attribution error as a share of the target's measured change (spec §5.4)
- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

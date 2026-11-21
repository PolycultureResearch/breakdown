# Attribution tier: White Cube ten-seed set on the demo tree (not a canonical evaluation tree)

> The first attribution scores: dev seeds 0-4 of the generator's ten-seed White Cube set (fake_companies b51d70e build, study d28b29f), two anomalies the study says can ground sizes (onboarding_revamp, professional_churn_spike), ten single worlds, no calibration. Eligibility is derived from the set's verdicts and cross-seed precision, not listed. The tree is the demo tree, not a canonical evaluation tree, and ten worlds are not a finding: do not cite. The engine commit's +dirty is the then-uncommitted runner fix that became 3656f58, and docs; breakdown/ was clean, as the engine key records. Cost of the computing invocation: 6.1 min wall on four shards, 19 process-minutes; later invocations reused its fits.

**Tier:** attribution · **Vertical:** b2c_saas (white_cube) · **Seeds:** [0, 1, 2, 3, 4] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Engine commit `711826ec0e4ea6ab1334623e970b3445d3713173+dirty`; generator version `0.1.0`; manifest `config_sha256` `6517547081b833f9c872f2663578a89a84dcfdac3424bedafc13003d21234c1a` (configs/white_cube_b2c_app.yaml)
- Tree `demo/white_cube_tree.yml` (sha256 `d8e50fe756cba33a`, **not a canonical evaluation tree**); 23 nodes served
- Run 2026-11-21T04:31:30+00:00; 10 of 10 jobs done, 0 process-minutes; load average at plan 5.71 on 12 cores; wall time 6 min over 3 invocation(s) on [4] shard(s)
- Null windows per world: none
- Calibration windows: 0 scored
- Single worlds: 10 ranked of 10; off-tree 0; skipped 0
- `onboarding_revamp`: root ['trial_days_active'] (on_tree); target s0: `net_new_mrr` (set_verdict), s1: `net_new_mrr` (set_verdict), s2: `net_new_mrr` (set_verdict), s3: `net_new_mrr` (set_verdict), s4: `net_new_mrr` (set_verdict)
- `professional_churn_spike`: root ['customer_churn_rate'] (on_tree_rate); target s0: `net_new_mrr` (set_verdict), s1: `net_new_mrr` (set_verdict), s2: `net_new_mrr` (set_verdict), s3: `net_new_mrr` (set_verdict), s4: `net_new_mrr` (set_verdict)
- Served with a caveat: `active_subscriptions`: 'active_subscriptions' is a distinct count per day; at grain 'week' it is served as the last day's count, not MetricFlow's distinct count over the period

A cell is the mean over seeds ± the standard deviation across seeds. `n` counts windows, worlds or fits.

### Q1 Calibration on null worlds

No null windows were scored on this run (its tier scores none).

### Q2 Detection: the root's own score at onset

Empirical tail: share of the same node's null-world windows (same seed, same horizon) at least as extreme. Its resolution is one over the null windows per node.

| Horizon | Root role | worlds | \|z\| at onset | empirical tail | tail < 5% | null windows per node |
|---|---|---|---|---|---|---|
| 7 | all | 10 | 3.64 ± 1.38 |  |  | 0 ± 0 |
| 7 | rate | 10 | 3.64 ± 1.38 |  |  | 0 ± 0 |

### Q3 Localization: rank of the true root

A first place lacks evidence when its own \|z\| is under 2. Nodes ranked is how many candidates the root was ranked among: where the anomaly reaches only one node below its root, first of one is not a finding.

| Method | Root role | worlds | nodes ranked | top-1 | top-3 | mean reciprocal rank | top-1 with evidence | first place lacks evidence |
|---|---|---|---|---|---|---|---|---|
| innovation_contribution | all | 10 | 20.0 ± 0.0 | 60% ± 22 | 100% ± 0 | 0.77 ± 0.14 | 40% ± 42 | 30% ± 27 |
| innovation_contribution | rate | 10 | 20.0 ± 0.0 | 60% ± 22 | 100% ± 0 | 0.77 ± 0.14 | 40% ± 42 | 30% ± 27 |
| innovation_evidence | all | 10 | 20.0 ± 0.0 | 60% ± 22 | 80% ± 27 | 0.71 ± 0.18 | 60% ± 22 | 0% ± 0 |
| innovation_evidence | rate | 10 | 20.0 ± 0.0 | 60% ± 22 | 80% ± 27 | 0.71 ± 0.18 | 60% ± 22 | 0% ± 0 |
| engine_ranked_causes | all | 10 | 22.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.15 ± 0.02 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | rate | 10 | 22.0 ± 0.0 | 0% ± 0 | 0% ± 0 | 0.15 ± 0.02 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 10 | 22.0 ± 0.0 | 10% ± 22 | 60% ± 22 | 0.41 ± 0.13 | 10% ± 22 | 70% ± 27 |
| naive_relative_change | rate | 10 | 22.0 ± 0.0 | 10% ± 22 | 60% ± 22 | 0.41 ± 0.13 | 10% ± 22 | 70% ± 27 |

### Q5 Attribution: attributed sizes against the measured effect

Over the anomaly's declared window, each edge on the path from the root to the target (the engine's contribution of the parent to the child's change, with its 95% interval) and the root (the part of its change the engine leaves unexplained, or for a root with no parents its change itself; no interval). Only items whose size the world set can ground are scored: set verdict established and the set's cross-seed mean precise to ±20% at its own seed count (the eligibility table says why each other item is not). Log error is ln(engine / measured); a withheld estimate counts as withheld, never as zero.

| size-eligible items per world | direction-only items per world | items per world |
|---|---|---|
| 4.0 ± 0.0 | 1.0 ± 0.0 | 5.0 ± 0.0 |

| Item | scored | median ratio | mean log error | mean \|log error\| | interval covers truth | withheld | wrong sign |
|---|---|---|---|---|---|---|---|
| all | 40 | 1.83 ± 0.16 | 0.52 ± 0.09 | 0.58 ± 0.04 | 38% ± 0 | 0% ± 0 | 0% ± 0 |
| formula_edge | 30 | 2.44 ± 0.11 | 0.66 ± 0.07 | 0.72 ± 0.05 | 38% ± 0 | 0% ± 0 | 0% ± 0 |
| root_gap | 5 | 0.99 ± 0.03 | -0.02 ± 0.03 | 0.03 ± 0.01 |  | 0% ± 0 | 0% ± 0 |
| root_unexplained | 5 | 1.09 ± 0.28 | 0.06 ± 0.26 | 0.23 ± 0.09 |  | 0% ± 0 | 0% ± 0 |

Direction only (verdict established, size not scorable: see the table): the engine's sign agrees with the measured one in 100% ± 0 of 10 items.

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

Per single-world analysis: innovation ranking 1 ± 0 s, engine RCA 0 ± 0 s (after the fits the innovation pass already made), 0 ± 0 fits.

### Q5 Which attribution sizes the world set can ground (b2c_saas)

Mean relative effect, seeds and precision are the set's, for the item's child, over the anomaly's declared window: precision is the 95% half-width of the cross-seed mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). Share is the item's mean fraction of its child's twin difference over the run's worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds are those whose twins were identical until the anomaly; there one twin's difference is the truth.

| Anomaly | Item | Kind | Set verdict | seeds | mean relative effect | share | precision | aligned worlds | scored on | why |
|---|---|---|---|---|---|---|---|---|---|---|
| `onboarding_revamp` | `new_mrr->net_new_mrr` | formula_edge | established_up | 10 | 0.207 | 1.04 | ±20% | 0 | size | established and precise |
| `onboarding_revamp` | `new_subscriptions->new_mrr` | formula_edge | established_up | 10 | 0.166 | 0.98 | ±14% | 0 | size | established and precise |
| `onboarding_revamp` | `root:trial_days_active` | root_gap | established_up | 10 | 0.892 | 1.00 | ±1% | 0 | size | established and precise |
| `onboarding_revamp` | `trial_conversion_rate->trial_conversions` | formula_edge | established_up | 10 | 0.289 | 1.00 | ±14% | 0 | size | established and precise |
| `onboarding_revamp` | `trial_conversions->new_subscriptions` | formula_edge | established_up | 10 | 0.169 | 1.06 | ±16% | 0 | size | established and precise |
| `onboarding_revamp` | `trial_days_active->trial_conversion_rate` | learned_edge | established_up | 10 | 0.290 |  | — | 0 | no | several_parents_moved: ['trial_activation_rate'] |
| `professional_churn_spike` | `churned_mrr->net_new_mrr` | formula_edge | established_down | 10 | -0.212 | 1.09 | ±21% | 0 | direction | precision ±21% at the set's 10 seeds (needs ±20%) |
| `professional_churn_spike` | `churned_subscriptions->churned_mrr` | formula_edge | established_up | 10 | 0.489 | 0.63 | ±14% | 0 | size | established and precise |
| `professional_churn_spike` | `customer_churn_rate->churned_subscriptions` | formula_edge | established_up | 10 | 0.276 | 1.07 | ±18% | 0 | size | established and precise |
| `professional_churn_spike` | `root:customer_churn_rate` | root_unexplained | established_up | 10 | 0.296 | 1.00 | ±17% | 0 | size | established and precise |

## Not scored yet

- **Q4 slices:** whether the true slice is the top slice of the true node (spec §5.3)
- **Q5 error share:** attribution error as a share of the target's measured change (spec §5.4)
- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

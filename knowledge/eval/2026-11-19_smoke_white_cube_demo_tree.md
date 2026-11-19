# Smoke-tier baseline: White Cube on the demo tree (not a canonical evaluation tree)

> The first scoreboard. Its job is to show that the pipeline runs end to end and what it costs. Three seeds, eight null windows per node and three anomalies are too few to conclude anything about the engine, and the tree is the demo tree, not a canonical evaluation tree. Do not cite these numbers as findings.

**Tier:** smoke · **Vertical:** b2c_saas (white_cube) · **Seeds:** [0, 1, 2] · **Regimes:** ['baseline'] · **Horizons (days):** [7]

- Engine commit `97f0286e1d026fdf53ff27174a4a1cf8df50ecd8`; generator version `0.1.0`; manifest `config_sha256` `6517547081b833f9c872f2663578a89a84dcfdac3424bedafc13003d21234c1a` (configs/white_cube_b2c_app.yaml)
- Tree `demo/white_cube_tree.yml` (sha256 `d8e50fe756cba33a`, **not a canonical evaluation tree**); 23 nodes served
- Run 2026-11-19T20:56:38+00:00; 393 of 393 jobs done, 52 process-minutes; load average at plan 21.58 on 12 cores; wall time 14 min over 1 invocation(s) on [4] shard(s)
- Null windows per world: 8 every 77 days
- Calibration windows: 360 scored; 24 not scored (unassessed, fit failed: Cannot fit over undefined periods: 'churn_arpu')
- Single worlds: 9 ranked of 9; off-tree 0; skipped 0

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
| innovation_contribution | all | 9 | 7.6 ± 4.2 | 89% ± 19 | 89% ± 19 | 0.91 ± 0.15 | 11% ± 19 | 89% ± 19 |
| innovation_contribution | rate | 6 | 10.8 ± 6.3 | 83% ± 29 | 83% ± 29 | 0.87 ± 0.23 | 17% ± 29 | 83% ± 29 |
| innovation_contribution | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| innovation_evidence | all | 9 | 7.6 ± 4.2 | 56% ± 19 | 100% ± 0 | 0.72 ± 0.15 | 0% ± 0 | 67% ± 0 |
| innovation_evidence | rate | 6 | 10.8 ± 6.3 | 33% ± 29 | 100% ± 0 | 0.58 ± 0.22 | 0% ± 0 | 50% ± 0 |
| innovation_evidence | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | all | 9 | 8.1 ± 4.4 | 33% ± 0 | 67% ± 33 | 0.53 ± 0.07 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | rate | 6 | 11.7 ± 6.6 | 0% ± 0 | 50% ± 50 | 0.30 ± 0.10 | 0% ± 0 | 100% ± 0 |
| engine_ranked_causes | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | all | 9 | 8.1 ± 4.4 | 33% ± 0 | 56% ± 19 | 0.53 ± 0.05 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | rate | 6 | 11.7 ± 6.6 | 0% ± 0 | 33% ± 29 | 0.30 ± 0.08 | 0% ± 0 | 100% ± 0 |
| naive_relative_change | flow, no parents | 3 | 1.0 ± 0.0 | 100% ± 0 | 100% ± 0 | 1.00 ± 0.00 | 0% ± 0 | 100% ± 0 |

### Q8 Cost

Per fit, over the null worlds' fits. Wall seconds were taken on a shared machine: read them as ratios between roles and between scoreboards, not as absolutes.

| Role | fits | median fit seconds | ESS < 400 | any divergence | suspect |
|---|---|---|---|---|---|
| all | 360 | 5.3 ± 0.2 | 1% ± 0 | 19% ± 3 | 35% ± 3 |
| rate | 168 | 5.3 ± 0.2 | 1% ± 1 | 35% ± 7 | 8% ± 7 |
| flow, no parents | 120 | 4.7 ± 0.3 | 0% ± 0 | 4% ± 1 | 80% ± 2 |
| flow, learned parent | 48 | 12.0 ± 0.9 | 0% ± 0 | 8% ± 4 | 33% ± 13 |
| stock | 24 | 4.3 ± 0.4 | 0% ± 0 | 0% ± 0 | 0% ± 0 |

Per single-world analysis: innovation ranking 52 ± 28 s, engine RCA 11 ± 10 s (after the fits the innovation pass already made), 10 ± 6 fits.

## Not scored yet

- **Q4 slices:** whether the true slice is the top slice of the true node (spec §5.3)
- **Q5 attribution:** contribution sizes against the measured twin difference (spec §5.4)
- **Q6 abstention:** off-tree, data-quality and null-world false alarms (spec §5.5)
- **Q7 power:** the magnitude ladder (spec §5.2); needs generator phase 2
- **Q9 catch rate:** share of badly calibrated nodes a flag catches; needs a per-node verdict

---

*Generated by `knowledge/benchmarks/evaluate.py`. This document is written and maintained by an AI agent (Claude), with human oversight.*

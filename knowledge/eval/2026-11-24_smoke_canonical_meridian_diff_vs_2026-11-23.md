# Scoreboard difference (B − A)

- **A:** engine `821907f5628dc26cd73d1489e8185004378f054f+dirty`, tree `/Users/devon/Documents/code/fake_companies-canonical-trees/examples/breakdown_meridian_tree.yml`, tier smoke, seeds [0, 1, 2], 2026-11-24T07:22:10+00:00
- **B:** engine `af29690ba24860d42135ab9945d42e320be1107b+dirty`, tree `/Users/devon/Documents/code/fake_companies-canonical-trees/examples/breakdown_meridian_tree.yml`, tier smoke, seeds [0, 1, 2], 2026-11-24T15:59:37+00:00
- Shared worlds: 12; only in A: 0; only in B: 0

Differences are paired by seed on the shared worlds; ± is the standard deviation of the per-seed differences.

| Question | Metric | Horizon | Method | Split | Role | Vertical | A | B | B − A | seeds |
|---|---|---|---|---|---|---|---|---|---|---|
| Q1 calibration | mean_z | 7 |  |  | all | b2b_services | -0.012 | 0.012 | +0.024 ± 0.021 | 3 |
| Q1 calibration | sd_z | 7 |  |  | all | b2b_services | 1.091 | 1.100 | +0.009 ± 0.013 | 3 |
| Q1 calibration | abs_z_over_2 | 7 |  |  | all | b2b_services | 0.089 | 0.099 | +0.010 ± 0.018 | 3 |
| Q1 calibration | abs_z_over_2_centred | 7 |  |  | all | b2b_services | 0.068 | 0.068 | +0.000 ± 0.000 | 3 |
| Q1 calibration | pit_ks | 7 |  |  | all | b2b_services | 0.088 | 0.108 | +0.020 ± 0.017 | 3 |
| Q1 calibration | lag1_z | 7 |  |  | all | b2b_services | -0.058 | -0.058 | -0.000 ± 0.013 | 3 |
| Q1 calibration | mean_z | 7 |  |  | flow, learned parent | b2b_services | -0.103 | -0.103 | +0.000 ± 0.000 | 3 |
| Q1 calibration | sd_z | 7 |  |  | flow, learned parent | b2b_services | 1.103 | 1.103 | +0.000 ± 0.000 | 3 |
| Q1 calibration | abs_z_over_2 | 7 |  |  | flow, learned parent | b2b_services | 0.090 | 0.090 | +0.000 ± 0.000 | 3 |
| Q1 calibration | abs_z_over_2_centred | 7 |  |  | flow, learned parent | b2b_services | 0.076 | 0.076 | +0.000 ± 0.000 | 3 |
| Q1 calibration | pit_ks | 7 |  |  | flow, learned parent | b2b_services | 0.139 | 0.139 | +0.000 ± 0.000 | 3 |
| Q1 calibration | lag1_z | 7 |  |  | flow, learned parent | b2b_services | -0.111 | -0.111 | +0.000 ± 0.000 | 3 |
| Q1 calibration | mean_z | 7 |  |  | flow, no parents | b2b_services | 0.426 | 0.426 | +0.000 ± 0.000 | 3 |
| Q1 calibration | sd_z | 7 |  |  | flow, no parents | b2b_services | 1.216 | 1.216 | +0.000 ± 0.000 | 3 |
| Q1 calibration | abs_z_over_2 | 7 |  |  | flow, no parents | b2b_services | 0.167 | 0.167 | +0.000 ± 0.000 | 3 |
| Q1 calibration | abs_z_over_2_centred | 7 |  |  | flow, no parents | b2b_services | 0.083 | 0.083 | +0.000 ± 0.000 | 3 |
| Q1 calibration | pit_ks | 7 |  |  | flow, no parents | b2b_services | 0.326 | 0.326 | +0.000 ± 0.000 | 3 |
| Q1 calibration | lag1_z | 7 |  |  | flow, no parents | b2b_services | 0.371 | 0.371 | +0.000 ± 0.000 | 3 |
| Q1 calibration | mean_z | 7 |  |  | stock | b2b_services | 0.093 | 0.286 | +0.193 ± 0.170 | 3 |
| Q1 calibration | sd_z | 7 |  |  | stock | b2b_services | 0.820 | 0.899 | +0.079 ± 0.096 | 3 |
| Q1 calibration | abs_z_over_2 | 7 |  |  | stock | b2b_services | 0.000 | 0.083 | +0.083 ± 0.144 | 3 |
| Q1 calibration | abs_z_over_2_centred | 7 |  |  | stock | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q1 calibration | pit_ks | 7 |  |  | stock | b2b_services | 0.196 | 0.292 | +0.096 ± 0.026 | 3 |
| Q1 calibration | lag1_z | 7 |  |  | stock | b2b_services | -0.212 | -0.189 | +0.023 ± 0.126 | 3 |
| Q9 diagnostics | share_of_windows | 7 |  | ppc_status=moderate | all | b2b_services | 0.219 | 0.224 | +0.005 ± 0.009 | 3 |
| Q9 diagnostics | mean_z | 7 |  | ppc_status=moderate | all | b2b_services | 0.044 | 0.060 | +0.016 ± 0.028 | 3 |
| Q9 diagnostics | sd_z | 7 |  | ppc_status=moderate | all | b2b_services | 0.966 | 0.966 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | abs_z_over_2 | 7 |  | ppc_status=moderate | all | b2b_services | 0.060 | 0.059 | -0.001 ± 0.002 | 3 |
| Q9 diagnostics | share_of_windows | 7 |  | ppc_status=ok | all | b2b_services | 0.615 | 0.609 | -0.005 ± 0.009 | 3 |
| Q9 diagnostics | mean_z | 7 |  | ppc_status=ok | all | b2b_services | 0.020 | 0.048 | +0.028 ± 0.024 | 3 |
| Q9 diagnostics | sd_z | 7 |  | ppc_status=ok | all | b2b_services | 1.048 | 1.065 | +0.017 ± 0.016 | 3 |
| Q9 diagnostics | abs_z_over_2 | 7 |  | ppc_status=ok | all | b2b_services | 0.088 | 0.103 | +0.014 ± 0.023 | 3 |
| Q9 diagnostics | share_of_windows | 7 |  | ppc_status=severe | all | b2b_services | 0.167 | 0.167 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | mean_z | 7 |  | ppc_status=severe | all | b2b_services | 0.015 | 0.015 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | sd_z | 7 |  | ppc_status=severe | all | b2b_services | 1.275 | 1.275 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | abs_z_over_2 | 7 |  | ppc_status=severe | all | b2b_services | 0.120 | 0.120 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | share_of_windows | 7 |  | fit_quality=ok | all | b2b_services | 0.823 | 0.828 | +0.005 ± 0.009 | 3 |
| Q9 diagnostics | mean_z | 7 |  | fit_quality=ok | all | b2b_services | -0.004 | 0.029 | +0.034 ± 0.030 | 3 |
| Q9 diagnostics | sd_z | 7 |  | fit_quality=ok | all | b2b_services | 1.079 | 1.087 | +0.009 ± 0.011 | 3 |
| Q9 diagnostics | abs_z_over_2 | 7 |  | fit_quality=ok | all | b2b_services | 0.084 | 0.095 | +0.011 ± 0.020 | 3 |
| Q9 diagnostics | share_of_windows | 7 |  | fit_quality=suspect | all | b2b_services | 0.177 | 0.172 | -0.005 ± 0.009 | 3 |
| Q9 diagnostics | mean_z | 7 |  | fit_quality=suspect | all | b2b_services | 0.051 | 0.024 | -0.027 ± 0.047 | 3 |
| Q9 diagnostics | sd_z | 7 |  | fit_quality=suspect | all | b2b_services | 1.275 | 1.275 | +0.000 ± 0.000 | 3 |
| Q9 diagnostics | abs_z_over_2 | 7 |  | fit_quality=suspect | all | b2b_services | 0.120 | 0.120 | +0.000 ± 0.000 | 3 |
| Q2 detection | abs_z_at_onset | 7 |  |  | all | b2b_services | 0.859 | 0.859 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail | 7 |  |  | all | b2b_services | 0.542 | 0.542 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail_under_5pct | 7 |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q2 detection | null_windows_per_node | 7 |  |  | all | b2b_services | 8.000 | 8.000 | +0.000 ± 0.000 | 3 |
| Q2 detection | abs_z_at_onset | 7 |  |  | flow, learned parent | b2b_services | 0.776 | 0.776 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail | 7 |  |  | flow, learned parent | b2b_services | 0.417 | 0.417 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail_under_5pct | 7 |  |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q2 detection | null_windows_per_node | 7 |  |  | flow, learned parent | b2b_services | 8.000 | 8.000 | +0.000 ± 0.000 | 3 |
| Q2 detection | abs_z_at_onset | 7 |  |  | flow, no parents | b2b_services | 0.942 | 0.942 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail | 7 |  |  | flow, no parents | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q2 detection | empirical_tail_under_5pct | 7 |  |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q2 detection | null_windows_per_node | 7 |  |  | flow, no parents | b2b_services | 8.000 | 8.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | engine_ranked_causes |  | all | b2b_services | 5.667 | 5.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | engine_ranked_causes |  | all | b2b_services | 0.556 | 0.556 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | engine_ranked_causes |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | engine_ranked_causes |  | all | b2b_services | 0.741 | 0.741 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | engine_ranked_causes |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | engine_ranked_causes |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 7.000 | 7.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | engine_ranked_causes |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | engine_ranked_causes |  | flow, no parents | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | engine_ranked_causes |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | engine_ranked_causes |  | flow, no parents | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | engine_ranked_causes |  | flow, no parents | b2b_services | 0.389 | 0.389 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | engine_ranked_causes |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | engine_ranked_causes |  | flow, no parents | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | engine_ranked_causes |  | rate | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | engine_ranked_causes |  | rate | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | engine_ranked_causes |  | rate | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | engine_ranked_causes |  | rate | b2b_services | 0.833 | 0.833 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | engine_ranked_causes |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | engine_ranked_causes |  | rate | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_contribution |  | all | b2b_services | 4.667 | 4.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_contribution |  | all | b2b_services | 0.444 | 0.444 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_contribution |  | all | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_contribution |  | all | b2b_services | 0.556 | 0.556 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_contribution |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_contribution |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_contribution |  | flow, learned parent | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_contribution |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_contribution |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_contribution |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_contribution |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_contribution |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_contribution |  | flow, no parents | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_contribution |  | flow, no parents | b2b_services | 0.333 | 0.333 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_contribution |  | flow, no parents | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_contribution |  | flow, no parents | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_contribution |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_contribution |  | flow, no parents | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_contribution |  | rate | b2b_services | 4.000 | 4.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_contribution |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_contribution |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_contribution |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_contribution |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_contribution |  | rate | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_evidence |  | all | b2b_services | 4.667 | 4.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_evidence |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_evidence |  | all | b2b_services | 0.222 | 0.222 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_evidence |  | all | b2b_services | 0.193 | 0.193 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_evidence |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_evidence |  | all | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_evidence |  | flow, learned parent | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_evidence |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_evidence |  | flow, learned parent | b2b_services | 0.333 | 0.333 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_evidence |  | flow, learned parent | b2b_services | 0.261 | 0.261 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_evidence |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_evidence |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_evidence |  | flow, no parents | b2b_services | 5.000 | 5.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_evidence |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_evidence |  | flow, no parents | b2b_services | 0.333 | 0.333 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_evidence |  | flow, no parents | b2b_services | 0.317 | 0.317 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_evidence |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_evidence |  | flow, no parents | b2b_services | 0.333 | 0.333 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | innovation_evidence |  | rate | b2b_services | 4.000 | 4.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | innovation_evidence |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | innovation_evidence |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | innovation_evidence |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | innovation_evidence |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | innovation_evidence |  | rate | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | naive_relative_change |  | all | b2b_services | 7.667 | 7.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | naive_relative_change |  | all | b2b_services | 0.222 | 0.222 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | naive_relative_change |  | all | b2b_services | 0.444 | 0.444 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | naive_relative_change |  | all | b2b_services | 0.411 | 0.411 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | naive_relative_change |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | naive_relative_change |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | naive_relative_change |  | flow, learned parent | b2b_services | 9.000 | 9.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | naive_relative_change |  | flow, learned parent | b2b_services | 0.667 | 0.667 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | naive_relative_change |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | naive_relative_change |  | flow, learned parent | b2b_services | 0.778 | 0.778 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | naive_relative_change |  | flow, learned parent | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | naive_relative_change |  | flow, learned parent | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | naive_relative_change |  | flow, no parents | b2b_services | 7.000 | 7.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | naive_relative_change |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | naive_relative_change |  | flow, no parents | b2b_services | 0.333 | 0.333 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | naive_relative_change |  | flow, no parents | b2b_services | 0.270 | 0.270 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | naive_relative_change |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | naive_relative_change |  | flow, no parents | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | candidates |  | naive_relative_change |  | rate | b2b_services | 7.000 | 7.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1 |  | naive_relative_change |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | top3 |  | naive_relative_change |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | reciprocal_rank |  | naive_relative_change |  | rate | b2b_services | 0.187 | 0.187 | +0.000 ± 0.000 | 3 |
| Q3 localization | top1_with_evidence |  | naive_relative_change |  | rate | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q3 localization | first_place_lacks_evidence |  | naive_relative_change |  | rate | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | slice_rows_refused |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1_by_abs_excess |  |  |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1 |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top3 |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | reciprocal_rank |  |  |  | all | b2b_services | 0.250 | 0.250 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | candidates |  |  |  | all | b2b_services | 4.000 | 4.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | root_abs_z |  |  |  | all | b2b_services | 28.797 | 28.797 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1_with_evidence |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | first_place_lacks_evidence |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | localized |  |  |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1_by_abs_excess |  |  |  | flow | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1 |  |  |  | flow | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top3 |  |  |  | flow | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | reciprocal_rank |  |  |  | flow | b2b_services | 0.250 | 0.250 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | candidates |  |  |  | flow | b2b_services | 4.000 | 4.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | root_abs_z |  |  |  | flow | b2b_services | 28.797 | 28.797 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | top1_with_evidence |  |  |  | flow | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | first_place_lacks_evidence |  |  |  | flow | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q4 slice localization | localized |  |  |  | flow | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q5 counterfactual attribution | items_size_eligible |  |  |  | all | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q5 counterfactual attribution | items_direction_only |  |  |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q5 counterfactual attribution | items_total |  |  |  | all | b2b_services | 1.333 | 1.333 | +0.000 ± 0.000 | 3 |
| Q5 counterfactual attribution | direction_agrees |  |  |  | all | b2b_services | 1.000 | 1.000 | +0.000 ± 0.000 | 3 |
| Q8 cost | fit_seconds_median |  |  |  | all | b2b_services | 3.463 | 3.350 | -0.113 ± 0.098 | 3 |
| Q8 cost | ess_under_400 |  |  |  | all | b2b_services | 0.016 | 0.010 | -0.005 ± 0.024 | 3 |
| Q8 cost | any_divergence |  |  |  | all | b2b_services | 0.151 | 0.156 | +0.005 ± 0.024 | 3 |
| Q8 cost | suspect |  |  |  | all | b2b_services | 0.177 | 0.172 | -0.005 ± 0.009 | 3 |
| Q8 cost | fit_seconds_median |  |  |  | flow, learned parent | b2b_services | 3.472 | 3.362 | -0.110 ± 0.105 | 3 |
| Q8 cost | ess_under_400 |  |  |  | flow, learned parent | b2b_services | 0.007 | 0.007 | +0.000 ± 0.000 | 3 |
| Q8 cost | any_divergence |  |  |  | flow, learned parent | b2b_services | 0.174 | 0.174 | +0.000 ± 0.000 | 3 |
| Q8 cost | suspect |  |  |  | flow, learned parent | b2b_services | 0.229 | 0.229 | +0.000 ± 0.000 | 3 |
| Q8 cost | fit_seconds_median |  |  |  | flow, no parents | b2b_services | 2.917 | 2.818 | -0.098 ± 0.073 | 3 |
| Q8 cost | ess_under_400 |  |  |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q8 cost | any_divergence |  |  |  | flow, no parents | b2b_services | 0.083 | 0.083 | +0.000 ± 0.000 | 3 |
| Q8 cost | suspect |  |  |  | flow, no parents | b2b_services | 0.000 | 0.000 | +0.000 ± 0.000 | 3 |
| Q8 cost | fit_seconds_median |  |  |  | stock | b2b_services | 3.862 | 3.637 | -0.225 ± 0.061 | 3 |
| Q8 cost | ess_under_400 |  |  |  | stock | b2b_services | 0.083 | 0.042 | -0.042 ± 0.191 | 3 |
| Q8 cost | any_divergence |  |  |  | stock | b2b_services | 0.083 | 0.125 | +0.042 ± 0.191 | 3 |
| Q8 cost | suspect |  |  |  | stock | b2b_services | 0.042 | 0.000 | -0.042 ± 0.072 | 3 |
| Q8 cost | analysis_innovation_seconds |  |  |  | all | b2b_services | 0.367 | 17.722 | +17.356 ± 0.844 | 3 |
| Q8 cost | analysis_rca_seconds |  |  |  | all | b2b_services | 0.200 | 11.444 | +11.244 ± 0.567 | 3 |
| Q8 cost | analysis_fits_made |  |  |  | all | b2b_services | 0.000 | 14.556 | +14.556 ± 0.192 | 3 |


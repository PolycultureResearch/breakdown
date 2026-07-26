# Knowledge

Product and design specs for breakdown. Engineering/AI context for the codebase itself lives in `docs/ai-context/`.

## Product & design specs

- [`product_integration_plan.md`](product_integration_plan.md) — data-connectivity plan (from the Narrative pilot)
- [`statistical_improvement_plan.md`](statistical_improvement_plan.md) — statistical hardening tickets (T-series)
- [`ui_design_spec.md`](ui_design_spec.md) — UI design spec (the what & why)
- [`ui_implementation_plan.md`](ui_implementation_plan.md) — UI tickets U1–U6 (the how)

## Example trees

- [`b2b_mrr_tree.yml`](b2b_mrr_tree.yml) — a full B2B SaaS "Total MRR" metric tree (107 metrics, single apex), adapted from Metrics Labs' [B2B Metrics Canvas](https://miro.com/app/board/uXjVNq48sQI=/?share_link_id=353173494684) on Miro. A worked reference for mapping a real-world metric tree onto breakdown's `formula` (deterministic) and probabilistic (`priors`/`lags`) edges.

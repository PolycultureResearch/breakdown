# Archive

Executed implementation plans, kept for the rationale behind decisions already
shipped. These are **historical** — for what to build next, see
[`../roadmap.md`](../roadmap.md), which absorbs their open items.

- [`statistical_improvement_plan.md`](statistical_improvement_plan.md) — the
  Bayesian review + tickets T1–T12. T1–T8 shipped (FitResult, pre-anomaly fits,
  non-centered trend, trend/seasonal components, per-day Shapley, block-bootstrap
  CIs, convergence diagnostics). T9–T12 are carried forward in the roadmap. Part 1
  (the critique) remains the best explanation of *why* the engine is shaped as it is.
- [`ui_implementation_plan.md`](ui_implementation_plan.md) — UI tickets U1–U6.
  U1–U4 shipped; U5–U6 carried forward.
- [`product_integration_plan.md`](product_integration_plan.md) — data-connectivity
  analysis. Env-var secrets and the warehouse provider shipped; the connectivity kit
  (connection doctor, CSV ingest, scaffolder, snapshot store) is carried forward.
- [`grill_2026_08_12.md`](grill_2026_08_12.md) — the second hostile review,
  frozen at `c18d150`. Both blockers and the wider findings shipped or were
  carried forward on the roadmap (its own status banner maps the IDs); kept for
  how each defect was constructed and what made it invisible. Archived
  2026-08-19, joining its triage companion below.
- [`grill_2026_08_12_triage.md`](grill_2026_08_12_triage.md) — the reproduction
  and triage of the second hostile review's 33 findings (companion to the frozen
  [`grill_2026_08_12.md`](grill_2026_08_12.md)). Archived 2026-08-17 with
  everything shipped or carried forward (C15–C18, 2.18, C25, 2.20); kept for how
  each finding was *verified* and what verification corrected about the report.

Internal links in these documents point at paths as they were when archived.

# S1 — Full-rank ADVI, benchmarked

> Roadmap [S1](roadmap.md#statistical-rigor-s--a-standing-workstream): measure
> `pm.fit(method="fullrank_advi")` against mean-field ADVI and NUTS on fit time
> and interval width, on the calibration suite and the White Cube tree; adopt
> as the RCA default if the cost is acceptable. The roadmap row is the source
> of truth for the decision; this page is the measurement it cites.
> Reproduce with
> [`benchmarks/s1_fullrank_advi.py`](benchmarks/s1_fullrank_advi.py).

**Status: complete (2026-08-18). Decision: full-rank is not adopted; S2 is
the path.**

> **Follow-up (2026-08-22): S2 shipped, and it confirmed this benchmark's
> reading of the model while breaking its assumption about scope.** PSIS k̂
> flags mean-field on *all four* White Cube probabilistic nodes (8.55, 1.21,
> 1.01, 0.98) and on the bundled demo's `order_count` (1.18) — including the
> two whose ELBO check came back `ok`, which is the failure this page argued
> S2 was needed for. Per-node escalation therefore is not the occasional rescue
> the "adopt if cheap enough" framing below imagined; it makes RCA on a small
> tree mostly-NUTS, which is where the closing note's "NUTS-by-default for
> small trees is worth a look" landed by a different route. The prices quoted
> here are what makes that survivable.  See
> [`statistics_whitepaper.md`](statistics_whitepaper.md) §4.1.

## Why this measurement exists

Mean-field ADVI — the RCA default — approximates the posterior as independent
Gaussians, and is underdispersed by construction
([`advi_vs_nuts_in_breakdown.md`](advi_vs_nuts_in_breakdown.md)). breakdown's
model deliberately builds the geometry mean-field handles worst: a slowly
drifting parent and the local-level trend compete for the same variance, so
the honest posterior over (β, trend) is a ridge, and a factorized
approximation collapses it to a tight blob. Full-rank ADVI fits a full
covariance matrix — it *can* represent the ridge — and is far cheaper than
NUTS. If it is cheap enough to default to, S2's PSIS-k̂ diagnostic may be
unnecessary for most trees, which is why S1 is sequenced first.

## What is measured

The **`beta_raw` posterior** (the coefficient in business units): its 95%
interval width per method, wall-clock fit time, whether the interval covers
the planted truth, and whether the optimizer converged (`fit_quality`). Not
the RCA contribution interval — a contribution blends the coefficient
posterior with the block bootstrap on window means, and the coefficient term
is the one S1 is about (it dominates exactly when windows are long, i.e. on
the analyses users trust most).

All fits run through `fit_metric` itself — the real model, priors,
normalization and diagnostics — with `draws=1000` and seeded samplers.

## The suites

- **step** — the calibration suite's DGP
  (`tests/test_calibration.py::_planted_step_world`): a stationary parent
  around 100 that steps up +30, `y = 0.5·x + ε`. Easy geometry (no β/trend
  ambiguity); establishes baseline cost and behavior where mean-field is fine.
- **drift** — `x = 100 + cumsum(N(0, 0.8))`, `y = 0.5·x + ε`: the parent
  drifts as a slow random walk, so "the parent caused the drift" and "the
  level drifted on its own" both fit the data. This is the ridge; it is the
  case S1 exists for, and the case the step suite cannot exercise (S17
  records the same blind spot in the calibration coverage test).
- **driftlong** — the drift world at **830 daily periods**, the bundled demo's
  window size. The model carries one latent trend state per period, so this
  is where NUTS gets expensive and ADVI's speed advantage actually lives —
  and where full-rank's own O(d²) covariance (~840 latents → ~350k
  variational parameters) gets stress-tested. A default cannot be chosen from
  130-period timings alone.
- **whitecube** — the three probabilistic nodes of the committed demo tree
  (`sessions` ← marketing_spend, `trials_started` ← signups,
  `new_subscriptions` ← trial_conversions), fitted from the parquet snapshots
  exactly as the deployed demo fits them: weekly grain, declared seasonality,
  113-week window.
- **nsweep** — full-rank at 20k/40k/80k/160k optimizer steps on the drift
  worlds, against NUTS and mean-field on the same worlds. Run first: at the
  engine's default 20,000 steps the full-rank optimizer does **not** converge
  on these models (`fit_quality: "suspect"`, ELBO still falling, interval
  width ~6× NUTS on a smoke run — initialization noise, not a posterior), so
  the main suites must run full-rank at a step count where its ELBO has
  actually settled, and the *time at that step count* is the honest cost of
  adopting it.

## Results

Machine: Apple Silicon macOS (Darwin 25.3), Python 3.14, single process, no
concurrent load. `draws=1000`; NUTS at its engine defaults (tune=1000,
4 chains, target_accept=0.9); mean-field ADVI at its shipped 20k steps.

### nsweep — where full-rank converges (5 drift worlds)

| method | median s | mean 95% width | width ÷ NUTS | suspect rate |
|---|---|---|---|---|
| mean-field @ 20k | 1.4 | 0.114 | **0.78** | 0.00 |
| full-rank @ 20k | 5.1 | 0.268 | 1.83 | **1.00** |
| full-rank @ 40k | 9.8 | 0.147 | **1.00** | 0.00 |
| full-rank @ 80k | 18.9 | 0.142 | 0.97 | 0.00 |
| full-rank @ 160k | 37.6 | 0.145 | 0.99 | 0.00 |
| NUTS | 3.5 | 0.148 | 1.00 | 0.20 |

Three findings, each load-bearing:

1. **At the engine's default 20k steps full-rank has not converged** — every
   fit `suspect`, widths ~1.8× NUTS and falling. Its unconverged width is
   initialization noise, not a posterior. Any adoption must raise
   `vi_iterations` for full-rank; the honest cost of full-rank is the cost at
   convergence.
2. **Converged full-rank reproduces the NUTS interval on the ridge** (ratio
   0.97–1.00, stable across a 4× range of step counts). The missing-capability
   argument is confirmed: a full covariance matrix is what mean-field lacked.
3. **Mean-field is ~22% narrower than NUTS on the same worlds** (0.78) — the
   S1 underdispersion, measured on this engine's own model rather than
   inferred from theory.

### Main suites (10 worlds each; full-rank at 40k steps)

| suite | method | median s | mean 95% width | width ÷ NUTS | coverage | suspect |
|---|---|---|---|---|---|---|
| step | mean-field | 1.4 | 0.058 | 1.56 | 1.00 | 1.00 |
| step | full-rank @ 40k | 9.6 | 0.063 | 1.67 | 1.00 | 0.00 |
| step | NUTS | 3.6 | 0.038 | 1.00 | 1.00 | 0.10 |
| drift | mean-field | 1.3 | 0.109 | **0.81** | 1.00 | 0.10 |
| drift | full-rank @ 40k | 9.6 | 0.139 | **1.04** | 1.00 | 0.00 |
| drift | NUTS | 3.4 | 0.135 | 1.00 | 1.00 | 0.10 |

On the ridge (drift), converged full-rank matches NUTS (1.04) where
mean-field is 19% narrow (0.81) — the synthetic result S1 hoped for. On the
easy geometry (step) both VI variants are mildly *over*-dispersed; absolute
widths there are tiny.

### driftlong — 830 daily periods (3 worlds; full-rank at 40k steps)

| method | median s | mean 95% width | width ÷ NUTS | suspect |
|---|---|---|---|---|
| mean-field @ 20k | 2.1 | 0.058 | 2.14 | **1.00** |
| full-rank @ 40k | 253.8 | 0.331 | **12.17** | 0.00 |
| NUTS | 11.2 | 0.028 | 1.00 | 0.33 |

The scaling completes the verdict. At the bundled demo's window size,
full-rank is **23× slower than NUTS and 12× too wide, with a clean ELBO** —
the O(d²) covariance over ~830 trend latents is both the cost and the
mis-fit. And the reference method it was meant to approximate costs **11
seconds**. Note mean-field's behavior here too: at 20k steps it is
unconverged on every long-window fit (`suspect` across the board) and lands
2.1× *over*-wide — so across the suites, VI error on this model is
unpredictable even in **direction** (0.8× on the 130-period ridge, 2.1× at
830 periods, 7.8× over on a real seasonal node for full-rank), which is
precisely the case for a diagnostic that measures distance-to-posterior
rather than optimizer convergence (S2).

### White Cube — the real nodes (weekly grain, seasonality, 113 periods)

| node ← parent | mean-field | full-rank @ 40k | NUTS |
|---|---|---|---|
| sessions ← marketing_spend | 2.7s, w=0.179 `suspect` | **231s, w=1.047** `ok` | 66s, w=0.134 `ok` |
| trials_started ← signups | 2.2s, w=0.048 `suspect` | **228s, w=0.258** `ok` | 11s, w=0.043 `ok` |
| new_subscriptions ← trial_conversions | 1.4s, w=0.093 `suspect` | 8.6s, w=0.081 `ok` | 52s, w≈0.000 `suspect` |

(The summary table's astronomical width÷NUTS ratios for this suite are an
artifact of dividing by the degenerate ~zero-width NUTS fit on
`new_subscriptions`; read the per-node rows instead.)

**On real nodes, full-rank fails the adoption test on both axes at once.** On
the two seasonal nodes it took ~230s — 3.5–20× slower than NUTS on the same
node, ~100× mean-field — and its `ok`-ELBO interval on `sessions` is **7.8×
the NUTS width** (1.047 vs 0.134): a converged optimizer, far from the
posterior, in the *over*-dispersed direction this time. The 130-period
synthetic suites could not see either failure; the real tree's ~120 latents
(trend states + Fourier terms) are where the O(d²) covariance both slows the
optimizer and mis-fits. Two side findings: mean-field at its shipped 20k
steps is `suspect` on *all three* real nodes (its ELBO check is doing its
job; nothing downstream surfaces it prominently), and one NUTS fit
(`new_subscriptions`) collapses to a ~zero-width `beta_raw`, flagged
`suspect`. The second is investigated and explained: in the committed
snapshots that edge is **exactly deterministic** —
`corr(new_subscriptions, trial_conversions.shift(1)) = 1.0` — so the true
posterior is a point mass (β = 1, σ_obs → 0). NUTS grinds against the
degeneracy (R̂ 4.4, ESS 4, zero divergences) and honestly reports `suspect`;
both VI variants meanwhile report a *non-zero* interval (0.08–0.09) on an
edge with zero uncertainty. Not an engine defect — but the tree's own comment
promises resurrections should put the coefficient "just above 1", and the
generated data delivers none on this edge, which is a demo-data fidelity note
worth its own fix.

## Decision

**Do not adopt full-rank ADVI as the RCA default.** The synthetic promise is
real (it reproduces the NUTS ridge posterior at ~10s on 130-period fits), but
at every realistic size it fails on both axes at once: slower than NUTS
itself (230s vs 11–66s on real weekly nodes; 254s vs 11s at 830 daily
periods) and far from the posterior with a clean ELBO (7.8× too wide on a
real node, 12× at 830 periods) — trading an invisible under-statement for an
invisible over-statement at a higher price than the exact answer. The engine
keeps `inference_method="fullrank_advi"` and `vi_iterations` as the
benchmark's substrate, engine-level only; no default, API surface, or cache
ranking changed. Two implications for the roadmap:

1. **S2 is not made unnecessary — it is made more urgent, and its scope is
   set.** Across the suites, VI error on this model is unpredictable even in
   direction, and the ELBO check misses it both ways. Detection
   (PSIS-k̂) plus escalate-to-NUTS is the path; a richer variational family
   is not.
2. **NUTS is cheaper than the working assumption.** 3.4s on 130-period
   synthetic nodes, 11s at 830 daily periods, 11–66s on real weekly seasonal
   nodes. "Triage with ADVI, confirm with NUTS" stands, but per-node NUTS
   escalation (S2) is affordable, and NUTS-by-default for small trees is
   worth a look while scoping S2.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

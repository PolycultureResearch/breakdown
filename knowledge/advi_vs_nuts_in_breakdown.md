# ADVI vs NUTS in breakdown: why the default RCA is the optimistic one

**A deep dive on the single largest gap between what breakdown's credible
intervals claim and what they deliver.**

RCA fits probabilistic nodes with mean-field ADVI by default. Mean-field
variational inference is *systematically* underdispersed — not occasionally,
not by accident, but by construction — and breakdown's model has exactly the
posterior geometry that makes the effect worst. This page explains the
mechanism, why this engine is unusually exposed to it, what it looks like when
it misleads someone, and what to do about it today.

This expands on §2.2 and §3.2 of the
[statistics white paper](statistics_whitepaper.md). For how to read RCA output
generally, see [`docs/model.md`](../docs/model.md).

---

## 1. What mean-field ADVI actually is

There are two ways to get a posterior.

**Sampling** (NUTS) walks around the posterior and hands you draws from it. Run
it long enough and you have the true posterior — it is asymptotically exact.

**Variational inference** turns the problem into optimization instead: pick a
family of "easy" distributions, then find the member closest to the true
posterior. It is fast, and it is approximate in a way that matters.

ADVI's family is **one Gaussian per parameter, with no correlations between
them**:

```
q(θ) = q(β₁) · q(β₂) · q(σ_trend) · q(trend_z[1]) · …     each an independent Normal(μᵢ, σᵢ)
```

That product-of-independent-marginals structure is the "mean-field" part (the
name comes from statistical physics). `fit_metric` calls
`pm.fit(n=20_000, method="advi", …)`, and mean-field is PyMC's ADVI default.

### The asymmetry that causes the problem

"Closest" is measured by the **reverse** KL divergence, `KL(q ‖ p)` — an
expectation taken over the approximation `q`, not over the true posterior `p`.
That asymmetry is the whole story:

- `q` putting mass where `p` has none is **heavily** penalized.
- `q` **missing** mass that `p` has is barely penalized at all.

So the optimizer's safest move is always to shrink — cover one dense region
tightly rather than risk spilling into low-density space. Variational inference
with reverse KL is **underdispersed by construction**. (Minimizing the *forward*
KL `KL(p ‖ q)` instead is moment-matching and tends to over-disperse, which is
why the direction of the divergence is not a footnote.)

### How much narrower, concretely

There is an exact result worth carrying around. Fit a factorized Gaussian to a
correlated bivariate Gaussian with correlation ρ, and the variational marginal
variance comes out as `σ²(1 − ρ²)` — you recover the **conditional** variance
where you wanted the **marginal** one (Bishop, 2006, §10.1.2). In standard
deviations, the reported spread is `√(1 − ρ²)` of the truth:

| true posterior correlation ρ | reported sd, as a fraction of truth | CI width |
|---|---|---|
| 0.5 | 87% | mildly narrow |
| 0.8 | 60% | noticeably narrow |
| **0.9** | **44%** | **less than half as wide** |
| 0.95 | 31% | roughly a third as wide |

The understatement is driven entirely by **how correlated the true posterior
is**. Which makes the next question the important one.

---

## 2. Why breakdown's model is unusually exposed

breakdown's posteriors are strongly correlated — and one of those correlations
is *deliberately engineered*.

### The trend and the parents compete for the same variance

The BSTS model is:

```
y[t] = α + trend[t] + seasonal[t] + Σᵢ βᵢ·xᵢ[t] + ε[t]
```

When a parent drifts slowly across the fit window, two explanations fit the data
almost equally well: *the parent caused the drift*, or *the level drifted on its
own*. The posterior encodes that ambiguity honestly, as a strong **negative
correlation** between β and the trend states — high β pairs with a flat trend,
low β pairs with a rising one. The posterior is a **ridge**, not a blob.

**That ridge is the uncertainty.** It is the model correctly reporting "I cannot
separate these from this data."

A factorized approximation cannot represent a ridge. It has no vocabulary for
"β is small *whenever* the trend is large" — that is precisely the dependence
the mean-field assumption throws away. So it collapses onto a tight blob near
the middle of the ridge and reports a narrow β.

The irony is sharp. [`docs/model.md`](../docs/model.md) explains that the tight
`HalfNormal(0.05)` trend prior exists specifically so parents win that contest
rather than the trend absorbing everything. That is a defensible modeling
choice — and it deliberately constructs the exact posterior geometry that
mean-field handles worst.

### Three more sources of correlation

- **Correlated parents.** Sessions and ad impressions move together. Their βs
  get a strongly correlated joint posterior with a well-determined *sum* and a
  poorly-determined *split*. RCA reports the split.
- **Seasonal vs. trend**, when the fit window is short relative to the declared
  period.
- **`σ_trend` vs. the trend path.** The non-centered parameterization fixes the
  funnel *geometry* for the sampler, but the parameters remain dependent.

### Two scoping points

Both matter for calibrating how worried to be:

- **Formula nodes are unaffected.** Their credible intervals come entirely from
  the block bootstrap; no coefficient posterior is involved. The exact half of
  the engine does not have this problem at all.
- **Probabilistic node CIs are a blend.** A contribution is `β × Δparent`,
  combining an over-tight coefficient posterior with an honest bootstrap on the
  window mean. Which term dominates decides how badly the result is understated
  — and the coefficient term dominates exactly when the window is **long** (many
  periods → small bootstrap variance). That is to say: on the analyses users
  trust most.

### Nothing flags it

This is what makes it a trap rather than a caveat. `fit_quality` for ADVI checks
only whether the ELBO stopped moving:

```python
suspect = abs(last.mean() - prev.mean()) > 0.5 * last.std()
```

A perfectly converged, badly-wrong approximation passes as `"ok"`. **Convergence
of the optimizer says nothing about the gap between `q` and `p`.**

---

## 3. What it looks like when it misleads someone

Take a node `signups` with parent `paid_clicks`, fitted over a quarter in which
both drift gently downward.

**What the honest (NUTS) posterior says.** `beta_raw` for paid_clicks lies
somewhere in **[0.01, 0.09] signups per click** — genuinely wide, because the
slow decline could be paid clicks doing the work, or underlying demand decay the
trend absorbs. The data does not distinguish them.

**What mean-field ADVI reports.** `beta_raw ≈ 0.05 ± 0.005`. Same center;
interval roughly 40% of the honest width.

Now run the RCA. Signups came in 700 below the reference window; paid_clicks
fell by 10,000.

> **paid_clicks → signups: −500** (95% CI [−560, −440]),
> `prob_same_direction: 0.99`, `share_of_gap: 71%`

That reads as a solved case: paid clicks explain ~70% of the shortfall, tightly
bounded, direction near-certain. The obvious action is to restore paid spend.

**The honest interval on that contribution is closer to [−900, −100].** Which is
a completely different decision problem: paid clicks might account for nearly
the entire shortfall, or for a seventh of it with the rest being demand decay
that ad spend will not fix. Under the honest interval the correct next move is
*"we can't tell — go measure."* Under the reported one it is *"we know — go
spend."*

### Three ways the damage propagates

1. **False positives on the null case.** The action trigger analysts actually
   use is "the CI excludes zero." A genuinely ambiguous parent whose honest
   interval straddles zero can come back narrowed enough to clear it —
   manufacturing a root cause that is not there. This is the most consequential
   form, because the calibration suite specifically tests *restraint*
   (`test_null_case_attributes_nothing_confidently`,
   `test_unrelated_parent_gets_no_credit`). Restraint is a stated design goal
   that mean-field quietly erodes on real trees.
2. **`prob_same_direction` inflates toward 1.0.** It is the posterior mass on
   the dominant side of zero, so shrinking the distribution mechanically pushes
   it up. A coin flip presents as a certainty.
3. **`ranked_causes` inherits it.** The heuristic propagates `|share_of_gap|`
   products up the tree, so a spuriously confident node outranks a genuinely
   larger but honestly-uncertain one. The distortion reaches the most prominent
   number in the UI.

### The unifying problem

**The tool looks more certain precisely where the data is least informative.**
Ambiguity is what produces posterior correlation, and posterior correlation is
what mean-field destroys.

---

## 4. What to do about it today

### Confirm with NUTS

Re-run any load-bearing node on exactly the data RCA used:

```
POST /analyze/{name}?inference_method=nuts&fit_end=<analysis_start>
```

`fit_end=<analysis_start>` is the part people forget — without it, `/analyze`
fits the **full** window including the anomaly, which is a different model, not
a more accurate one. Then compare `beta_raw` interval widths between the two
runs. If NUTS is materially wider, NUTS is right and the RCA number was
optimistic.

### When to bother

| Situation | Why |
|---|---|
| A contribution is about to drive a decision | The whole point |
| Parents are plausibly collinear | Split-of-credit is the unstable quantity |
| A CI *just barely* excludes zero | The most likely false positive |
| `prob_same_direction` ≈ 1.0 on a noisy metric | Suspicious certainty |
| The result looks *too* clean | Usually is |
| Long fit window, long analysis window | Coefficient term dominates the blend |

### When not to bother

- **Formula nodes** — CIs are bootstrap-only, so ADVI is not involved.
- **Triage sweeps** — ADVI is the right tool for "which branch should I look
  at," which is what it was chosen for. The problem is not that ADVI is used;
  it is that its output is presented identically to NUTS output.
- **Very short windows** — the bootstrap term dominates and it is honest.

### What will not save you

`fit_quality: "ok"` on an ADVI fit. It means the optimizer converged. It does
not mean the approximation is good, and a well-converged bad approximation is
exactly the failure mode described here.

---

## 5. The fix, and where it sits on the roadmap

Both candidate fixes live in the roadmap's
[Statistical rigor workstream](roadmap.md#statistical-rigor-s--a-standing-workstream),
which is the source of truth for their status; the white paper's §4.1 holds the
fuller rationale. The first has now been measured.

**S1 — Benchmark full-rank ADVI: ✅ ran 2026-08-18, and full-rank was
rejected.** The mechanism this page describes was confirmed: on
drifting-parent worlds (the ridge), converged full-rank reproduces the NUTS
interval to within 4% while mean-field is ~20% narrow. But on the real White
Cube nodes full-rank was *slower than NUTS itself* (~230s vs 11–66s per node)
and its converged, `ok`-ELBO interval on one node came out 7.8× the NUTS
width. The model carries one latent trend state per fitted period, so a real
window puts the full covariance in hundreds of dimensions — slow to optimize
and badly fit when the optimizer stops, with nothing in the ELBO check able
to see it. Full measurement:
[`s1_fullrank_advi_benchmark.md`](s1_fullrank_advi_benchmark.md).
`fit_metric` keeps `inference_method="fullrank_advi"` as a benchmarked
experimental option; the RCA default is unchanged.

**S2 — A real approximation diagnostic (shipped 2026-08-22).** The PSIS-based
k̂ diagnostic of Yao et al. (2018) now scores every variational fit: it
estimates how far the approximation is from the true posterior — the thing the
ELBO check cannot see, in either direction, as S1 measured. Where k̂ exceeds
0.7, `run_rca` and `run_scenario` discard the approximation and re-fit the node
with NUTS (capped at four per analysis); everywhere else the verdict is
reported as `khat` / `khat_status` and the intervals are labelled.

**And the measurement turned this page's central claim into a stronger one.**
This document argued that mean-field's under-dispersion is *constructional* and
worst on the β-vs-trend ridge. k̂ agrees, and puts a number on how general it
is: on the White Cube tree it flags **all four** probabilistic nodes (k̂ 8.55,
1.21, 1.01, 0.98) and on the bundled demo's `order_count` it reads 1.18. So the
ridge is not an occasional geometry this model wanders into — a local-level
random walk with one latent per period *is* a ridge, everywhere, and mean-field
is not a usable approximation to it at real window sizes. §4's escape hatch is
now the engine's own default behaviour on the paths that chose ADVI for you,
and the honest framing has changed accordingly: the default path is no longer
the optimistic one, at the price of an RCA that is 3–5× slower.

---

## References

Bishop, C. M. (2006). *Pattern Recognition and Machine Learning*, §10.1.2
("Factorized approximations" — the `σ²(1 − ρ²)` result). Springer.

Blei, D. M., Kucukelbir, A., & McAuliffe, J. D. (2017). Variational inference: A
review for statisticians. *Journal of the American Statistical Association*,
112(518), 859–877.

Kucukelbir, A., Tran, D., Ranganath, R., Gelman, A., & Blei, D. M. (2017).
Automatic differentiation variational inference. *Journal of Machine Learning
Research*, 18(14), 1–45.

Hoffman, M. D., & Gelman, A. (2014). The No-U-Turn Sampler: Adaptively setting
path lengths in Hamiltonian Monte Carlo. *Journal of Machine Learning Research*,
15, 1593–1623.

Yao, Y., Vehtari, A., Simpson, D., & Gelman, A. (2018). Yes, but did it work?:
Evaluating variational inference. *International Conference on Machine
Learning*, 35.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

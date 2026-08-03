# Design: Lag handling in RCA — assessment and improvements

Status: v1 in progress (assessment current as of 2026-08; §2 lag-window
surfacing **shipped**; §3 lag scan design-only pending implementation;
distributed lags assessed and deferred). Companion to [`grain_design.md`](grain_design.md) (grain-step lag
semantics) and [`dimensional_slicing_design.md`](dimensional_slicing_design.md)
(the traverse-then-slice workflow consumes the surfaced lag windows).

## The question this answers

"Revenue is down. New member conversions are down. Now look at trial starts —
but trial starts **one trial period ago** relative to the anomaly, because
this week's conversions came from last week's trials." If breakdown is going
to automate the analyst's walk up the tree, the time shift at each lagged edge
has to be handled exactly, everywhere: in the fit, in the attribution windows,
and in the narrative.

## 1. Assessment: the shift is already handled — when declared

The declared-lag machinery is complete and correct:

- **Declaration.** `lags: {parent: N}` on a metric (`breakdown/parser.py`,
  `check_lags`) — per-parent integers in **grain steps at the node's grain**
  (7 is a week of days on a daily node, 7 months on a monthly one). Two forms:
  a lagged probabilistic edge (`parents` + `lags`), and `formula` + `lags`,
  the **cohort-aligned lagged identity**
  (`conversions[t] = trial_starts[t-14] × cohort_rate[t]`) — see
  [`authoring_deterministic_decompositions.md`](authoring_deterministic_decompositions.md).
- **Fit.** `_prepare_series` (`breakdown/engine/model.py`) shifts each parent
  back by its lag before fitting and trims the leading `max(lags)` rows, so
  `beta_raw` is the coefficient on the parent *as it influenced the child*.
- **Attribution.** RCA measures a lagged parent's gap over windows shifted
  back by the lag via `shift_periods` (`breakdown/engine/rca.py`, both the
  Shapley and the posterior paths) — so "trial starts one trial period ago"
  is exactly what the engine compares, and the shift stays on the period
  spine across month/year boundaries.
- **Verified.** `tests/test_calibration.py::test_recovers_planted_lagged_cause`
  (a 5-day-delayed effect is recovered when declared), plus lag-trim,
  month-boundary, and lagged-identity tests in `tests/test_engine.py` and
  `tests/test_grain_rca.py`.

The documented limitation (`docs/model.md`): relationships are linear and
**contemporaneous unless lagged**. A parent whose true effect is delayed by an
*undeclared* lag looks weaker than it is, and the miss lands in `unexplained`.

Three real gaps remain, in priority order.

## 2. Gap 1 — the lag-shifted windows are invisible (v1: surface them) — SHIPPED

Before this change no response said *which* parent periods were examined. `effective_windows`
is node-level only; per-contribution `decomposition` carries means/co-movement
parts but never the lag; MCP's `compact_rca` collapses windows to period
counts. A narrator — human or MCP agent — cannot say "trial starts **Jul 11–17**
explain conversions Jul 25–31", and an agent doing follow-up analysis on a
lagged parent (drill-down RCA, slicing) has no windows to reuse.

**Design.** Each contribution for a parent with `lag > 0` gains:

```json
{
  "parent": "trial_starts",
  "estimate": -412.0,
  "lag": 14,
  "parent_windows": {
    "reference": {"start": "2026-07-04", "end": "2026-07-10"},
    "analysis":  {"start": "2026-07-11", "end": "2026-07-17"}
  }
}
```

computed from the already-snapped node windows via `shift_periods`; **omitted
entirely when `lag == 0`** (unlagged responses stay byte-identical). The same
block lands in `shapley_attribution`'s standalone response. MCP `compact_rca`
passes `lag`/`parent_windows` through, and `RCA_HOW_TO_READ` gains: narrate a
lagged parent using *its* dates, and reuse those dates as the windows for any
follow-up analysis of that parent. That last clause is load-bearing for
traverse-then-slice: it is what makes agent-driven slicing correct on lagged
edges (`SLICE_HOW_TO_READ` points at `parent_windows` as the windows to reuse).

Files: `breakdown/engine/rca.py` (both attribution paths),
`breakdown/mcp/shaping.py`, `docs/model.md`. Tests: extend the lagged-identity
cases in `tests/test_grain_rca.py` (dates asserted across a month boundary),
`tests/test_mcp_shaping.py` (pass-through; omission when unlagged),
`tests/test_rca.py` (unlagged responses unchanged). UI: deferred — a `↤14d`
badge on contribution rows is the natural v-next.

## 3. Gap 2 — nothing checks a declared lag (v1: Bayesian lag scan)

A wrong or missing lag silently weakens an edge. The check: for one declared
probabilistic edge parent→child, refit the child's existing BSTS under
candidate lags `k = 0..K` for that edge (other parents at their declared lags;
trend/seasonality unchanged) and compare fits. ADVI's ELBO lower-bounds the
log evidence, so `softmax(ELBO_k)` is an **approximate posterior over the
lag** — reported as a weight distribution over candidate delays, never a
significance test. Using the full model rather than raw cross-correlation
matters: shared day-of-week cycles would pile spurious cross-correlation mass
at multiples of 7, but the model's own seasonal component absorbs them before
the edge coefficient sees anything.

**Not causal discovery.** The roadmap deliberately excludes edge discovery;
the scan never proposes edges. The analyst declared the relationship; the scan
checks only the *delay parameter* of an already-asserted hypothesis — the same
family as `expected_signs` (declared direction + contradiction diagnostic).

**The one technical trap: comparability.** ELBOs are comparable only on
identical data, and lag-`k` trimming drops `k` leading rows — so every
candidate must fit on the **same rows**: trim `max(K, existing max_lag)`
leading rows for all candidates. Needs a small pure extension:
`fit_metric(..., lag_override: Dict[str, int], min_trim: int)` threaded into
`_prepare_series`. Scan fits are never written to `app.state.traces`.

**Where it runs: an offline CLI, `breakdown lag-scan`.** Not fit-time (K+1
ADVI fits per edge would multiply cold-cache RCA latency ~10×); not a doctor
default (doctor is the fast connectivity check — but the report reuses its
ok/flag/remediation presentation). Default K:
`max(2 × declared_lag, {day: 14, week: 8, month: 6}[grain])`, capped so
`n_rows − K ≥ MIN_FIT_PERIODS`.

Per-edge report shape:

```json
{
  "child": "conversions", "parent": "trial_starts", "grain": "day",
  "declared_lag": 7,
  "lag_weights": {"5": 0.09, "6": 0.22, "7": 0.41, "8": 0.19, "9": 0.06},
  "best_lag": 7,
  "spread": {"hpd_80_lags": [5, 9], "flag_distributed": true},
  "flag": null,
  "caveats": ["ELBO-weight posterior is an ADVI approximation; treat as triage.",
              "This checks the delay of a declared edge, not whether the edge exists."]
}
```

Flags: `declared_lag_outside_hpd` (declared lag outside the smallest candidate
set holding ≥ 80% of weight); `weak_edge_at_all_lags` (no signal at any lag —
the scan refuses to rank noise); `flag_distributed` (≥ 3 adjacent lags in the
80% set — the effect is smeared, which is itself the evidence gate for §4).

Files: new `breakdown/engine/lagscan.py`, `breakdown/engine/model.py`
(`lag_override`/`min_trim`), `breakdown/cli.py`. Tests
(calibration-suite style): true lag recovered; declared-0-vs-true-7 flagged;
null edge → weak-edge flag; smeared ground truth → distributed flag;
determinism under seed. Later: an MCP `check_lag` tool (so an agent seeing
large `unexplained` can self-serve the scan) and formula-node support.

## 4. Gap 3 — distributed lags: assessed, deferred

Real trials convert over days 5–9, not exactly day 7; a point lag underweights
the true effect. A fixed-kernel option is genuinely cheap in the fit path —
`lags: {trial_starts: {center: 7, width: 2}}` replacing the single shift with
a kernel-weighted sum of shifts, one beta, trim by `center+width`; attribution
stays closed-form (kernel-weighted average of shifted-window deltas) and
`parent_windows` becomes the spanning range.

**Deferred anyway**, on the repo's own MVP-first terms:

1. No current tree is blocked on it; the worked trees' lagged edges behave at
   their declared grains — and a **weekly point lag already integrates a 7-day
   conversion spread** (coarsening grain is often the better fix and exists).
2. The lag scan's `flag_distributed` is the instrument that will say when a
   real tree needs it. Ship the detector before the mechanism.
3. Learned kernel weights (Dirichlet simplex) are a real modeling project —
   identifiability against trend/seasonality, per-weight posteriors in RCA —
   and are out regardless.

The `{center, width}` mapping form is **reserved syntax** as of this doc: the
parser keeps rejecting non-integer lags, and nobody should claim that shape
for anything else. On **formula nodes distributed lags stay disallowed
permanently**: a smearing kernel makes the identity exact only if the kernel
equals the true conversion-timing distribution, so the "exact identity" claim
would silently become a model — reject at parse time and point the author at a
probabilistic edge or a coarser grain.

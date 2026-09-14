# Known, dated interventions — a step-change term for the fit

**Status:** design, not implementation. Roadmap
[S24](roadmap.md#statistical-rigor-s--a-standing-workstream) (○). Opened
2026-09-14 against issue #114.

**The ask.** A field user running a ticketed-event tree re-ran an RCA under
0.2.0 and got the honest answer — `fit_quality: suspect`, `ppc_status: SEVERE`
on the target — for a series whose history is made of *steps*: price flips,
on-sale days, announce days. Their words: "a price flip is a regime shift, not
a wander … which is correct but leaves the practitioner with nowhere to go
inside the tool." They asked for a step-change / regime term, or a documented
recipe.

This document settles what the defect is, what the engine can already do about
it (more than the issue assumes, and less than a term would), the candidate
designs, and one recommendation with its YAML shape, disclosure obligations and
test plan.

**Relation to `knowledge/marketing_campaign_lift_design.md`** (same day,
uncommitted at the time of writing, raised by a marketing team). That
document's §5.2 and §6 Tier 1 propose "an event-term fit-window exception":
a declared event regressor whose fit window deliberately spans the campaign,
so the term is identified from the event itself rather than dropped as a
constant. It calls that item new and not on the roadmap, and orders the rest
of its build behind it (3.4 → S16 → S9 → S19). **The two proposals are one
mechanism** — a dated regressor the author declares — with one real
difference: whether the fit is allowed to see the intervention's own periods.
This document supplies the schema, the payload contract and the test plan for
that regressor; it adopts the marketing document's exception as a second,
explicitly opted-in learning mode (§3.2, `learn_from: window`) rather than
the default, and states why the default stays the other way. The roadmap row
this document adds, [S24](roadmap.md#statistical-rigor-s--a-standing-workstream),
is the row for both; the marketing document's Tier 1 item 1 should cite it
rather than add a second one. Where the two disagree is narrow and is stated
in §1.1 and §3.2.

---

## 1. What the defect actually is

There are two different steps in the reporter's story, and only one of them is
a modelling problem.

### 1.1 A step inside the *analysis* window is what RCA measures

RCA fits every node on data **strictly before the analysis window**
(`fit_end = analysis_start`, exclusive; `fit_metric` keeps only whole periods
that end on or before the cutoff — `engine/model.py`, the `fit_end` block).
The price flip in the reporter's analysis window is therefore *not in the
training data at all*. The model learned the normal-regime relationship from
the weeks before, and the RCA is the measurement of the flip's effect: the gap
between the reference and analysis window means, split into what the declared
parents carry (`β × Δparent`), what the fitted trend and seasonality carry
(`components`), and the remainder (`unexplained`).

That is the CausalImpact pattern by design (`docs/model.md`, "What data the
fit sees"), and a step in the analysis window does not violate it. If the
flip's effect reaches `orders` through a declared parent — the reporter's
`flip_comms` is exactly that, a comms-intensity series that moves *because*
there was a flip — the parent's contribution carries it. If it reaches
`orders` by a path the tree does not declare (the price itself, which is not
a parent), it lands in `unexplained`, which is the honest first-class finding
the engine exists to report. **Under the fit-before-analysis rule no
intervention term helps here**: a regressor that is identically zero over the
fit window is not identified (it is a multiple of nothing), and since issue
#113 the engine drops a constant regressor from the design matrix and says so
on `dropped_parents`. A "step at `analysis_start`" declared as a model term
would be dropped, correctly, and the reader would be back where they started.

There is exactly one way through, and the marketing-lift document names it:
let the fit see the intervention's own periods. That is Box and Tiao's
intervention analysis (1975) — fit the whole series with a known-date
indicator, read the indicator's coefficient as the shift — and it is a
different estimator from RCA's, with a different claim attached. Its
coefficient is *the level shift coincident with the declared date, whatever
caused it*; the fit-before-analysis rule exists precisely so that nothing in
the analysis window can explain itself, and this relaxes it for one declared
term on purpose. §3.2 makes that an explicit per-intervention opt-in with the
claim stated on the payload. It is not the default, and it is not what #114
needs: their PPC verdict is about §1.2.

Short of that, what a declaration *can* do for an analysis-window step is
name it: "the unexplained remainder coincides with a declared price flip on
2026-08-10." That is annotation, not modelling, and §4 keeps it as the cheap
half of the recommendation.

### 1.2 A step inside the *fit* history is what the PPC flagged

The posterior predictive check (S3) scores the model **against the fitted
window only** — the window before `analysis_start`, after the lag trim
(`_ppc_band`'s docstring is explicit about the two lengths). So a SEVERE verdict
on the reporter's target is a statement about the *season's history*: the
earlier tier flips, the on-sale day, the announce day, any floor at zero — every
step the model was asked to reproduce and could not.

Why a local level cannot reproduce a step, mechanically. The trend is
`cumsum(σ_trend · z[t])` with `σ_trend ~ HalfNormal(0.05)` in z-scored space and
`z[t] ~ Normal(0, 1)`. A step of one standard deviation of the series in a
single period would need `z[t] ≈ 20` at the default prior — twenty standard
deviations. The posterior does not do that; it spreads the level change over
many periods and inflates `σ_obs` to cover the residuals around the step. Three
of S3's four statistics are built to catch precisely this: `resid_max` (one
period far outside what the model calls noise), `resid_acf1` (residuals on one
side of the step are all one sign, so they autocorrelate), and `max`/`min` when
the step creates a peak or a floor the smoothed level never reaches.

The consequences for the numbers the reader is handed:

- **`σ_obs` is inflated**, so every interval on the node is wider than the
  data warrants everywhere *except* at the step, where it is narrower.
- **β is contaminated where a parent moved at the step.** A step in the target
  with a parent that also stepped (the flip and the flip's comms) is the
  trend-vs-β competition `docs/model.md` limitation #3 describes, at its
  sharpest: the tight trend prior pushes the level change onto the parent, so
  `β_flip_comms` learns "flip effect ÷ comms count" rather than "orders per
  comm". The sign survives; the magnitude and the interval do not mean what
  the payload says they mean.
- **Every RCA on that node inherits both**, because the fit is shared across
  analyses that end at the same `analysis_start`.

This is the defect. It is a **disclosed** limitation — S3 disclosed it, on the
node, on every surface, with a sentence per statistic — which is why it belongs
in the S track and not in Horizon 0. But "disclosed and unaddressable" is a
worse place to leave a client than "disclosed and here is the knob."

### 1.3 What we cannot tell from the issue

Which statistic fired. `ppc.statistics` on the fit carries all four with their
p-values, and the Metric tab's *Posterior predictive check* panel (S10) draws
the fitted series against the replicate bands, so the failing periods are
visible. If the failure is `min` on a count series with a zero floor, the fix
is S20 (a count likelihood), not this document. The reply to #114 should ask
for the four p-values.

---

## 2. What the engine can already do

Checked by reading the code, not the docs. Three recipes, in the order a
reader should try them.

### 2.1 Fit on the last stable regime — `--start-date`

The fit window is *all loaded history before `analysis_start`*, and the
loaded history starts at `--start-date` (`cli.py`, `BREAKDOWN_START_DATE`).
Starting the load after the last known regime change in history gives the
model a history with no step in it. This is the standard CausalImpact answer
and it needs no engine change.

Two floors bound it: `MIN_FIT_PERIODS = 10` whole periods at the node's grain
after the `fit_end` cut and lag trim (`_enforce_fit_length`), and any declared
seasonality needs two full periods inside the fit window or it lands in
`seasonality_warnings`. On a daily tree with weekly seasonality that is
fourteen days of stable regime, which a festival on-sale usually has between
tier flips and usually does not have between announce and on-sale.

Cost: the whole tree loads from that date, so every node loses that history,
not only the stepped one. A per-node `fit_start` would be the honest version
(§3.3), and it is the smallest engine change in this document.

### 2.2 Declare the intervention as a parent metric

A node's regressors are its parents, and a parent is any metric the provider
can fetch. A 0/1 indicator (`1` from the flip onward) or a count (comms per
day, tier number) is a legitimate series: with `provider: warehouse` it is a
`sql:` over a date spine; with `dbt`/`local`/`cloud` it is a metric in the
semantic layer; the `bind:` block lets a single node take SQL while the rest
of the tree stays on the semantic layer (`docs/yaml-reference.md`, the
`dbt provider migration` paragraph). The reporter's `flip_comms` **is this
recipe already** — comms intensity as the flip's proxy — which is why the
ranking is stable across 0.1.0 and 0.2.0.

What it buys: the fit learns the step's size from every instance of it inside
the fit window, the PPC then scores the residual *around* declared steps, and
the RCA reports the step's share of the gap as a contribution with a credible
interval, subject to `expected_signs` and the S4 collinearity check like any
edge.

What it needs: **instances inside the fit window.** An indicator constant over
the fit window (a first-ever flip, or a flip that falls only in the analysis
window) is dropped with a WARNING and named on `dropped_parents` (issue
#113). That is not a limitation of the recipe; it is the identification
condition, and §1.1 says why no design can escape it.

What it costs: the indicator is an edge in the DAG, so it appears in
`ranked_causes`, in `get_tree`, on the canvas, and in what-if as a lever. For
a *mechanism* (comms) that is right. For a bare calendar fact (tier 2 began on
the 10th) it is a metric that is not a metric, fetched from a source that is
not a source, and slicing, `kind`, `direction` and `format` all have to be
answered for it.

### 2.3 Loosen the level — `trend: {sigma: …}`

`trend.sigma` widens the step-size prior so the level can take a jump in
fewer periods (`docs/model.md` limitation #3). It is documented, it is
per-node, and it works — at the documented cost: a looser level competes with
the parents for the same movement, β's interval widens, and S3's own note says
a loosened trend should have its PPC verdict read as weaker evidence. It is
the right knob for a node whose level genuinely wanders and the wrong one for
a node whose level jumps on three known dates a season.

---

## 3. Candidate designs

Cheapest first. Each is judged against the interactions the engine already
has: the fit-before-analysis rule, `expected_signs`, the attribution, the PPC,
what-if, the MCP payload, and cold start.

### 3.1 Annotation only — declared `events`, no model term

A tree-level `events:` list (date, name, optional `nodes`), carried onto every
surface: the RCA payload lists the events that fall inside each window, the
time-series panel marks them, `how_to_read` gains a clause, the export prints
them beside the windows.

- *Fit rule:* untouched. *Attribution / PPC / β:* unchanged — nothing is
  modelled.
- *What it fixes:* the §1.1 case. "Unexplained coincides with a declared
  event" is the sentence the reporter wanted to be able to write, and the
  #114 second ask (the reference window in the headline) is the same family:
  the artefact should carry the facts its reader needs to re-run it.
- *What it does not fix:* the §1.2 defect at all. The PPC still fails and the
  intervals are still wrong.
- *Cost:* a parser field, a payload field, three render sites, `compact_rca`.
  No statistics.

### 3.2 A known regressor — `interventions:` on the node

Per node, a list of dated interventions; each becomes a **known regressor**
column in the design matrix: `1[t ≥ date]` for `kind: step`, `1[date ≤ t ≤
until]` for `kind: pulse`. One coefficient each, with a prior stated in
business units (the step's size in the node's units, the natural thing an
author knows) and rescaled the way parent priors are.

- *Fit rule:* untouched by default (`learn_from: history`). The regressor is
  built over the fitted dates, so an intervention with no instance inside the
  fit window is constant and is **dropped under the #113 mechanism with its
  own reason** ("declared intervention 'tier_2' falls after the fit window;
  nothing to learn"). The drop is on the payload, never silent.
- *The exception, opted into per intervention* (`learn_from: window`) — the
  marketing-lift document's §5.2. The node's RCA fit runs with
  `fit_end` extended past the intervention's period(s) — through
  `analysis_end` for a `step`, through `until` for a `pulse` — with the
  indicator in the design matrix, so its coefficient is identified from the
  event itself. Three things follow and each is a disclosure, not a
  footnote. (i) The estimate is *the shift coincident with the date*: with
  the fit free to see the window, the indicator is the only term that can
  take an abrupt change on that date, so anything else that happened then
  is in it — this is the DAG premise one step further, and it is stated as
  such on the node (`interventions[].claim: "shift coincident with
  2026-08-10; not separable from anything else dated the same"`). (ii) The
  other parents' β are now fitted on a window that contains the anomaly,
  which is the contamination the default rule prevents; the tight trend
  prior limits how much the *level* can absorb, and the β on a parent that
  moved in the window is pulled toward the window's relationship. The
  node's `fit_window` already reports the dates the fit used, and the
  payload adds `fit_window.extended_for: [names]`. (iii) A node fitted this
  way is on a different window from its ancestors — legitimate, the trace
  cache is keyed by `(name, fit_end)`, and it is one more reason the exception
  is per intervention and per node, never tree-wide. **Why it is not the
  default:** the reporter's defect (§1.2) is fixed by `history` alone, and
  `window` answers a different question — a lift number for a one-off event —
  whose honest form is 3.4's counterfactual with S16's variance, which the
  marketing document sequences immediately after this item for exactly that
  reason. Shipping `window` first, without those, would publish a confident
  step size with an interval that reflects `σ_obs` and not the forecast
  uncertainty of the regime it stepped from.
- *Parent order is load-bearing* (AGENTS.md): `beta` / `beta_raw` are indexed
  by `list(dag.predecessors(name))`, and every consumer walks that list. The
  intervention coefficients therefore live on **their own variables**,
  `beta_intervention` / `beta_intervention_raw`, with their own axis
  (`FitResult.interventions`, YAML order), and are never appended to `beta`.
  This is the constraint that decides the implementation shape.
- *Scaling:* the column is 0/1 and is not z-scored — `x_std := 1`, so
  `beta_intervention_raw = beta · y_std` is the step in business units and
  the prior reads as "about +200 orders/day, give or take 100".
- *Grain:* the date snaps to the node's grain; a `step` is 1 from the period
  containing the date, and the parser warns when the date is not period-
  aligned on a coarse grain, because a mid-week step on a weekly node is a
  partial-period effect the author should date to the period start.
- *`expected_signs`:* keys may name an intervention as well as a parent; the
  same posterior-mass check applies (`check_expected_signs` gains one
  branch). A `HalfNormal` prior is the constraint; the sign declaration is
  the diagnostic, as today.
- *Collinearity:* an intervention column enters S4's design-matrix check
  alongside the parents. This is the interaction that matters most for the
  reporter's tree: a declared `flip` step and the `flip_comms` parent are
  both zero except at flips, so the check will flag them — correctly, because
  which of them "caused" the orders is not a determined quantity, and the
  payload already knows how to say so.
- *Attribution:* the window delta of the indicator times its coefficient is
  a real term in the gap. It is reported **on the node under its own key**,
  `interventions: [{name, date, kind, estimate, ci_95, ci_status,
  prob_same_direction, …}]`, next to `components`, and enters the identity
  `unexplained = gap − Σ contributions − trend − seasonal − Σ interventions`.
  It does **not** enter `ranked_causes`: that list ranks *metrics* to drill
  into, and an intervention has no subtree, no slices and no parents. It is
  also not filed under `components` — trend and seasonal are model structure
  "nobody's fault"; a price flip is somebody's decision, declared by the
  author, and the reader should see it as such. `how_to_read` says exactly
  that.
- *PPC:* the replicates are drawn from the full mean function, which now
  contains the step, so a node whose only misspecification was a declared
  step passes. **This is honest** for the same reason the DAG is: the
  intervention is the author's dated claim, carried on every surface as a
  claim, and the check now asks whether the model reproduces the series
  *given* the claims. What would not be honest is a step the engine placed
  itself (§5). The verdict's reading changes in one way the docs must state:
  a passing PPC on a node with declared interventions is evidence about the
  residual regime, not about the steps, whose size the model was told.
- *What-if:* nothing in v1. `run_scenario` works on metrics; an intervention
  is not one. The obvious extension — "what if we flip again: the fitted
  step size as a lever with its posterior" — is real value and is deferred
  until a tree asks, because it reopens the lever-units question the what-if
  spec closed (§2.3 there).
- *MCP:* `compact_rca` keeps the `interventions` list per node (it is small
  and load-bearing), `round_floats` sanitizes it, rule 3 applies: a non-finite
  estimate is withheld with a named `ci_status`, never emitted. A
  `RCA_HOW_TO_READ` clause: "`interventions` are dated changes the tree's
  author declared; their estimate is the model's read of the step's size,
  not a cause to drill into, and it is not in `ranked_causes`."
- *Cold start:* out. Cold start is a demo mode that stops earning surface
  (roadmap, 2026-08-05), and a prior-only step is exactly a belief draw; the
  parser rejects `interventions` under `provider: none` rather than
  half-supporting it.
- *Cost:* `_prepare_series` builds the columns; `_regression_component` gets
  a sibling; `FitResult` carries the axis; `rca.py` computes and reports the
  term and folds it into `unexplained`; `compact_rca`, `disclosures.js` and
  the export render it; the parser validates it. A few hundred lines, most of
  them in the places S4 and #113 just touched.

### 3.3 A level shift in the state equation

Instead of a regressor, add `δ_k` to the level at the declared date:
`trend[t] = cumsum(σ_trend · z[t]) + Σ_k δ_k · 1[t ≥ date_k]`. Statistically it
is 3.2 with the coefficient filed under `trend` rather than under its own
name — the likelihood is identical for a `step`, and there is no `pulse`
form. What changes is the reporting: the step would be inside
`components.trend`, invisible as a declared decision, and the trend's flat
forecast (`trend[-1]`) would silently carry it into the analysis window with
no way to say which part was the walk and which the declaration. That is the
opposite of what a disclosure track wants. Rejected as a shape; 3.2 does the
same arithmetic and keeps the name.

A **per-node `fit_start`** is the honest form of recipe 2.1 and is worth
building alongside 3.2 regardless: a node declares the date its current regime
began, the fit uses whole periods on or after it, `fit_window` on the payload
already exists to report it, and `_enforce_fit_length` already refuses a
window that comes out too short. Two dozen lines.

### 3.4 Automatic changepoint detection

Fit a changepoint model, or a sparse-step prior on the level increments, and
let the data place the steps. Rejected — §5.

---

## 4. Recommendation

**Build 3.2, with the annotation half of 3.1 as the same feature's cheap
surface, and 3.3's `fit_start` beside it.** In that order of certainty: the
annotation and `fit_start` are unconditionally right; the regressor is right
for the node shape the reporter has and is the one the S track should
measure before quoting.

### 4.1 YAML

```yaml
- name: orders
  source: festival.metrics.orders
  parents: [sessions, flip_comms, paid_spend]
  fit_start: 2026-07-30            # this regime began at on-sale; fit from here
  interventions:
    - name: tier_2_flip
      date: 2026-08-10
      kind: step                   # 1 from this period onward
      prior: { distribution: Normal, params: { mu: 150, sigma: 100 } }  # orders/day, optional
    - name: lineup_announce
      date: 2026-08-03
      kind: pulse                  # 1 on this period only …
      until: 2026-08-04            # … or through this one
    - name: spring_push
      date: 2026-09-07
      kind: pulse
      until: 2026-09-20
      learn_from: window           # the exception (§3.2): fit through it; default is history
  expected_signs: { tier_2_flip: positive }
```

Rules the parser enforces: `name` unique among the node's parents and
interventions; `kind ∈ {step, pulse}`; `until` only with `pulse` and not
before `date`; `learn_from ∈ {history, window}`, default `history`; `prior`
from the same four distributions as parent priors, in business units;
rejected on formula nodes (the identity has no regressors; declare the
intervention on the parent that actually moved) and under `provider: none`.
A date not aligned to the node's grain warns and snaps to the containing
period. `learn_from: window` may ship in a second PR behind `history`; the
schema reserves the field so the first PR does not have to be revisited.

### 4.2 What the fit and the payload must say

- `FitResult.interventions` — the axis, YAML order, minus any dropped; and
  `dropped_interventions` with a reason, in the #113 shape. An RCA node
  carries both. A dropped intervention is *named on the node*, not only
  logged: "declared intervention `tier_2_flip` (2026-08-10) has no instance
  inside the fit window (2026-07-30 to 2026-08-09) and was not fitted; if
  the analysis window contains it, its effect is in `unexplained` or in the
  parents that moved with it."
- `interventions[]` on the RCA node with `estimate`, `ci_95`, `ci_status`,
  `prob_same_direction`, and the window delta of the indicator (so a reader
  can see that a step entirely before both windows contributes 0 to the gap
  while still having fixed the fit).
- `fit_quality` is **not** downgraded by the presence of interventions, and
  a PPC that passes only with them declared is reported as passing. What
  changes is the disclosure: `ppc` gains `conditioned_on_interventions:
  [names]`, `how_to_read` carries the clause in §3.2, the UI's PPC panel
  draws the declared dates as vertical rules on the replicate bands, and
  `docs/model.md` gains a subsection under "What gets fitted" that says a
  declared step is a claim the check does not test.
- The collinearity block already names parents; it names interventions the
  same way.
- A `learn_from: window` intervention carries `claim` (§3.2 (i)) on its
  entry and `fit_window.extended_for` on the node; `how_to_read` says the
  estimate is the shift coincident with the date, the fit saw the analysis
  window for this node, and the interval does not include forecast
  uncertainty until 3.4/S16 land.
- The export prints interventions beside the windows in the header — with the
  #114 second ask, the reference window, which is the same header.

### 4.3 Test plan

All against `tests/synthetic.py` worlds, seeded, NUTS at the reduced test
budget. `_planted_step_world` (`tests/test_calibration.py`) plants the step
in the *parent*; these plant one in the *target's own history*.

1. **The defect, reproduced.** `y = β·x + step·1[t ≥ 40] + noise`, `x`
   stationary, AN starts at 91, no declaration. Assert `ppc_status` is
   `moderate` or `severe` with `resid_acf1` or `resid_max` among the flagged
   statistics, and that `σ_obs`'s posterior mean exceeds the planted noise
   by a stated factor. This pins the mechanism §1.2 claims.
2. **Declared, recovered.** Same world, `interventions: [{date: t=40,
   kind: step}]`. Assert PPC `ok`; `beta_raw` within 25% of β with the truth
   inside `ci_95`; `interventions[0].estimate` within 25% of `step` with the
   truth inside its interval; the node's `unexplained` small relative to the
   gap.
3. **Step inside the reference window.** Step at t = 70, REF 63–90, AN
   91–100. Assert the intervention's reported window delta is `1 − 0.25`
   (the fraction of REF after the step) and that the identity
   `gap = Σ contributions + trend + seasonal + Σ interventions +
   unexplained` holds to the C3 tolerance.
4. **Step only in the analysis window.** Step at t = 91. Assert the
   intervention is on `dropped_interventions` with the fit-window reason,
   nothing in `interventions[]`, `beta_raw` unchanged from a fit without the
   declaration, and the payload sentence names it.
5. **Pulse.** A one-period spike at t = 50; assert `max` no longer fires and
   the pulse coefficient recovers the spike.
6. **Collinear with a parent.** `x` itself steps at t = 40 and the same date
   is declared; assert `collinearity_status` is `high` and names both.
7. **Invariants** (`tests/test_project_invariants.py`): `beta`'s length
   equals `len(fit.parents)` on a fit with interventions — the axis is not
   widened; every surface that renders `dropped_parents` renders
   `dropped_interventions` (extend `test_every_rca_node_field_reaches_a_render_site`);
   `compact_rca` keeps `interventions`; `round_floats` never emits a
   non-finite estimate; the parser rejects the field on formula nodes and
   under `provider: none`.
8. **`fit_start`.** A node with `fit_start` after a planted step in its
   history and no declaration passes the PPC and fits on exactly the whole
   periods on or after the date; a `fit_start` leaving fewer than
   `MIN_FIT_PERIODS` is refused with the existing message.
9. **`learn_from: window`.** World 4 again (step only in the analysis
   window) with the exception on: assert the fit's `dates` run through
   `analysis_end`, `interventions[0].estimate` recovers `step` with the
   truth inside `ci_95`, `claim` and `fit_window.extended_for` are on the
   node, and `unexplained` falls by the recovered amount against world 4.
   Then plant a *second* unrelated shift on the same date and assert the
   estimate takes both — the claim in §3.2 (i), pinned rather than asserted.
10. **Read the numbers.** Before closing: run the demo trees, and the
   reporter's shape (a daily count with a step at a known date), through the
   UI and the MCP tool, per `knowledge/reading_the_numbers.md`.

### 4.4 Measurement before quoting

The S track's habit is to measure the size of the thing before describing it.
Two numbers to take on world 1 and publish in the roadmap row when it closes:
the factor by which `σ_obs` is inflated by an undeclared step of one series
SD, and how far `beta_raw` moves when the parent co-steps (world 6 without the
declaration). The second is the figure a reader of the reporter's `β_flip_comms`
needs.

---

## 5. Deliberately out of scope

- **Automatic changepoint detection**, including a sparse or heavy-tailed
  prior on the level increments (a Laplace or horseshoe on `z[t]`), which is
  changepoint detection by another name. It would make every series pass the
  PPC, because a model free to place steps anywhere can reproduce anything,
  and the steps it placed would be findings the tree's author never asserted.
  The roadmap's "Deliberately not on the roadmap" keeps causal *discovery*
  out on the premise that the DAG is the analyst's hypothesis; a step the
  engine discovered is the same category one dimension over. The line: the
  author dates the step; the engine sizes it.
- **A local linear trend** is S8 and stays there. Momentum and a step are
  different objects, and S8's slope would fit a step as a short burst of
  slope — the smoothing of §1.2 with a different shape.
- **Interventions as what-if levers** (§3.2). Deferred, not rejected.
- **A tree-level `events:` registry** that node entries reference by name.
  Useful once one on-sale day is declared on six nodes; v1 repeats the date.
- **Interventions on formula nodes.** The identity has no regressors; the
  step happened to a parent, and that is where it is declared.
- **Cold-start interventions.** A prior-only step is a belief draw, and the
  mode is not earning surface.

---

## 6. Where this sits

It is an **S-track** item, not Horizon 1: a disclosed limitation (S3 says
`severe` and why, on every surface) being given a remedy, in the same family
as S8 (a trend-structure opt-in chosen per node) and S19/S20 (the seasonal,
event-clocked business that raised all three). It is not a C item — no
published number is wrong without saying so. It is ahead of S8 in value per
effort for the one client shape that has asked: their business is dated
interventions, and momentum is not what their PPC is flagging. The
marketing-lift document's Tier 1 order — this item, then 3.4, then S16 —
holds unchanged with S24 in the first slot; its `learn_from: window` half is
what that order is waiting on, and its `history` half is what #114 is.

What #114 gets in the meantime is §2: the fit was never trained on the flip
they analysed; the SEVERE is about the season's earlier steps; `--start-date`
after the last of them is the tool's answer today; and the four `ppc.statistics`
p-values will say whether this document or S20 is the one they are waiting for.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

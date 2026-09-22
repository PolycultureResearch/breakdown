# Design: channel elasticity and campaign lift for a marketing team

Status: **analysis + a worked tree, no engine work yet.** The tree half is
[`white_cube_marketing_tree.yml`](white_cube_marketing_tree.yml), which runs
green under `breakdown doctor` and recovers the generator's planted campaign.
The engine half is a proposed ordering of existing roadmap items, not new ones.

Raised by a marketing team, 2026-09-14. Companion to
[`dimensional_slicing_design.md`](dimensional_slicing_design.md) (the slice
machinery this leans on), [`what_if_design.md`](what_if_design.md) (whose
steady-state decision §5.4 revisits), and the
[roadmap](roadmap.md)'s [3.4](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you),
[S9](roadmap.md#statistical-rigor-s--a-standing-workstream),
[S16](roadmap.md#statistical-rigor-s--a-standing-workstream) and
[S19](roadmap.md#statistical-rigor-s--a-standing-workstream) rows. The engine
item this note first called new is
[S24](roadmap.md#statistical-rigor-s--a-standing-workstream), filed the same
day against issue #114 and designed in
[`step_change_design.md`](step_change_design.md); §5.2 and §6 cite it rather
than proposing a second row.

## 1. The ask

A marketing team tracks signup attribution to channel and campaign, and asks
two questions:

1. **Channel elasticity.** "If we spend more on one channel for a month, what
   happens to the business?"
2. **Campaign lift.** "We ran a multi-channel push around a product release —
   paid social, paid influencers, a conference announcement, all in one week.
   How many signups, how many subscriptions, how much revenue did it drive?"

They look like one question. They are not, and the difference is the whole
finding of this note. Question 1 is about a **coefficient** and breakdown
answers it today, measurably well. Question 2 is about a **counterfactual over
a dated window**, which is a quantity the engine does not currently compute.

## 2. What answers today, measured

A marketing tree over the White Cube demo data was authored, loaded through the
`dbt` bridge against the generator's duckdb, and run. Everything below is
output, not expectation.

### 2.1 The channel coefficients are recovered, and the intervals cover truth

`fake_companies` builds paid sessions from spend at a known CPC per channel, so
sessions per dollar has a true value. Fitting each channel's sessions node on
data through 2025-03-10 (NUTS, seed 7):

| node | `beta_raw` mean | 95% CI | true | `fit_quality` |
|---|---|---|---|---|
| `paid_social_sessions` | 1.1006 | [1.032, 1.160] | 1.109 | ok |
| `paid_search_sessions` | 0.7210 | [0.646, 0.792] | 0.714 | suspect |
| `display_sessions` | 2.0923 | [1.853, 2.330] | 2.005 | ok |

Three for three inside the interval. This is the number question 1 turns on,
and on this data the engine gets it right with an interval a marketer can act
on: paid social buys 1.03–1.16 sessions per dollar, not "about one".

### 2.2 The full chain runs, and the campaign shows up in it

RCA on `new_mrr`, reference 2025-02-03..2025-03-02 against analysis
2025-03-10..2025-04-06, over the planted multi-channel campaign (generator
story C: a paid-social spend ramp plus a Brazil-segmented signup-rate lift
across paid social, organic and referral):

```
spend_paid_social      +$63.07/day   (368.93 → 432.00)
  → paid_social_sessions  +79.89/day, of which +70.27 attributed to spend
                                       (95% CI [31.5, 104.6]), 11.6 unexplained
  → sessions              +62.00/day  (exact identity, unexplained 0.0)
  → signups               +3.86/day   (sessions +2.26, rate +1.60)
  → trials_started        +0.82/day
  → trial_conversions     +1.00/week
  → new_subscriptions     +6.25/week
  → new_mrr               +$76.29/week
```

The implied coefficient at the top of that chain, 70.27 / 63.07 = **1.11
sessions per dollar**, is the generator's truth to three digits.

### 2.3 Slicing localizes the campaign correctly

`POST /rca/signups/slices` over the same windows, `localization: localized` in
both cases:

| by `channel` | contribution | excess | baseline share | `prob_concentrated` |
|---|---|---|---|---|
| paid_social | +2.893 | **+1.891** | 0.260 | 0.994 |
| referral | +0.964 | **+0.620** | 0.089 | 0.978 |
| organic | +1.071 | −0.058 | 0.293 | 0.514 (noise) |
| direct | −0.643 | −1.185 | 0.140 | 0.996 |

And the same gap sliced the other way:

| by `country` | contribution | excess | baseline share | `prob_concentrated` |
|---|---|---|---|---|
| BR | +3.250 | **+2.924** | 0.084 | 0.998 |

Both match the planted ground truth. Note what the channel slice gets *right*
that a naive reading would get wrong: organic's contribution is the second
largest in the table (+1.071) and its excess is **negative**, so the verdict
declines to name it. Organic grew exactly as much as its size predicts. The
generator did lift organic's signup rate, and the honest answer over this
window is still "organic is not where the gap concentrated."

### 2.4 What-if answers question 1 end to end

`POST /simulate`, baseline window 2025-05-05..2025-08-03, one intervention
`spend_paid_social +20%`:

```
spend_paid_social     +$120.83/day       (do-operator, exact)
paid_social_sessions  +132.70/day        95% CI [129.69, 135.56]
signups                 +4.81/day        95% CI [4.70, 4.92]
new_subscriptions       +3.17/week       95% CI [2.90, 3.45]
new_mrr                +$51.28/week      95% CI [46.91, 55.79]
```

$846/week more spend buys $51.28/week of new MRR, in steady state. That is the
shape of answer the team wants. Turning it into a payback period is *their*
arithmetic, not the engine's: new MRR recurs, so the comparison needs a
retention assumption breakdown never made, and the response deliberately stops
at the two flows.

**Read those intervals with care, and this is a disclosed limit rather than a
defect.** They carry coefficient uncertainty and nothing else. `run_scenario`'s
`caveats` block names the two risks they omit in words — that a fitted
coefficient is a local slope which large moves may not extrapolate along, and
that a learned edge is an association an unmodeled confounder can bias. What it
cannot do is *model* the first: there is no saturating response to fit, so the
caveat is the whole of the answer. That is §5.5.

## 3. The authoring shape, and what it cost

Three things about the tree are worth keeping; each was a decision, not a
default.

**One learned edge per channel, never several parents of one node.** The
obvious tree is `sessions ← [spend_paid_social, spend_paid_search,
spend_display]`. It is wrong. Three budgets that ride the same marketing
calendar are collinear, and collinear parents of one node split credit
unstably — the fit sizes their sum far more surely than the split, which is
exactly what [S4](roadmap.md#statistical-rigor-s--a-standing-workstream)'s
newly shipped ridge diagnostic exists to tell you. Giving each channel its own
`*_sessions` child removes the ridge instead of measuring it: every regression
has one regressor, and the exact sum identity above re-assembles the total.
The §2.1 coverage is what that choice buys.

**The spend nodes are hand-written `bind.sql` relations.** dbt declares one
unfiltered `marketing_spend` measure with `channel` as a dimension. A
per-channel node needs a predicate, and `bind.where` is import-only by design
([2.17](roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) —
`MetricDefinition.check_bind` refuses a hand-written one and names `bind.sql`
as the route). Declaring three filtered metrics in the dbt project would be the
better long-term home, and matches `demo/AUTHORING.md`'s stated direction of
travel: the tree tells you which metric the semantic layer is missing.

**Every funnel rate needs its own `agg: ratio` binding, and the reason
generalizes.** The bridge does not produce a binding for a dbt `ratio` or
`derived` metric. It offers a **formula candidate** instead — for this project,
`visit_signup_rate = signups / sessions`, `new_arpu = new_mrr /
new_subscriptions`, and three more. That candidate points the wrong way for a
funnel tree. The semantic layer defines a rate as an *output* of two volumes;
a causal tree needs it as an *input* to one of them (`signups = sessions *
visit_signup_rate`). Taking the candidate inverts the edge and closes a cycle.

This is not a bridge defect and it is not fixable by making the bridge
smarter — the two models genuinely disagree about which way the arrow points,
and the tree is the one making the causal claim. But it is a real onboarding
cost that nothing currently warns about: **a marketing funnel tree on the `dbt`
provider needs a hand-written ratio binding for every rate in it**, five of
twenty nodes here. `docs/yaml-reference.md` does say
that a ratio or derived metric "become[s] formula edges over metrics referenced
by name" — but only inside the paragraph about **filters** on such metrics. The
"not a drop-in for every tree" list beside it names cumulative metrics,
derived-with-offset, `min`/`max`/`median`/`percentile`, conversion metrics,
`non_additive_dimension` and cross-join filters, and does not mention that a
plain `ratio` yields no binding at all. Worth a line there.

**One smaller difference, worth recording.** The bridge reads one relation per
metric and does not follow a foreign entity into a conformed dimension, so
`user__country` on the MRR layer — which the `local` provider serves through
MetricFlow, and which `demo/AUTHORING.md` calls out as a discovery — is not
available here. The MRR nodes in this tree slice by `plan` only.

## 4. What the numbers say that the team will not like

Two findings sit in the output above and both are the engine being honest.

**The signal attenuates and the intervals widen down the funnel.** The
spend → sessions edge is measured on 790 daily observations and its 95% CI
spans about ±6% of the estimate. Four weeks later at the MRR layer it is four weekly observations, and the
RCA contribution of `new_subscriptions` to `new_mrr` comes back **+102.24 with
a 95% CI of [3.12, 223.26]**. Directionally right, practically unusable. No
amount of engine work fixes that: a four-week campaign produces four weekly
data points at the revenue layer, and
[S23](roadmap.md#statistical-rigor-s--a-standing-workstream)'s
reference-window sensitivity would widen it further, not narrow it.

**Half the subscription lift was not the campaign.** `new_subscriptions` rose
6.25/week, decomposed by the measured identity as trial conversions +3.25,
reactivations +2.25, direct conversions +0.75. Reactivations and direct
conversions are 48% of the movement and neither is downstream of any spend node
in this tree. A marketing team reading a topline "subscriptions up 14% during
our campaign" would claim all of it. The tree is what stops that, and it is
probably the single most valuable thing it does for this use case.

**One sampler note, for completeness.** The full-window (790-day) fit of
`paid_social_sessions` reports `fit_quality: "suspect"` — 28 divergences,
R̂ 1.06 — while all four posterior predictive checks pass. The RCA fits, which
end at `analysis_start` and are the ones that matter, come back `ok`. Sweeping
the trend prior (default HalfNormal(0.05), 0.02, 0.15) moved `beta_raw` across
1.1006 / 1.1071 / 1.1022, so the geometry is hard but the answer is stable. Not
a tree problem; noted so nobody rediscovers it as one.

## 5. The five gaps, in the order they hurt

Everything in §2 answers question 1. Question 2 — the dated multi-channel
push — is blocked, and these are what block it.

### 5.1 A campaign is not a node, and half the push is not spend

Influencer payments and a conference keynote produce no ad-platform spend rows.
Nothing in the tree can say "these three things happened together in week W."

The MVP-first answer already exists and is recorded in the roadmap's Northern
Nights note: encode events as **intensity series** at the grain their date
precision supports. That needs no schema. It does need a marketing calendar
that the warehouse can serve as a fact table, which is a prerequisite nobody
states out loud, and which the White Cube data does not have (§7).

So the gap here is documentation, not engine: the intensity-series pattern is a
paragraph inside a roadmap aside, and no shipped tree demonstrates it.

### 5.2 A fit that ends at the window cannot learn a term that only lives inside it

RCA fits strictly before the analysis window (`fit_end=analysis_start`,
`rca.py:1226`) so the anomaly cannot explain itself. Correct for anomaly RCA,
and exactly wrong for a planned event: a campaign flag that is nonzero only
during the push is constant over the fit window.

Since breakdown#123 (issue #113) that is no longer fatal — a constant
parent is **dropped** from the design matrix with a WARNING and a `dropped`
record, rather than failing the node. `_prepare_series` even names this case in
a comment: *"A parent that moves in the analysis window but not before it lands
here too: RCA's fit ends at `analysis_start`, and the coefficient a window it
never saw would have needed is not one it can learn."*

The failure is therefore honest and legible, and it is still a failure. The
campaign term is dropped and its effect lands in `unexplained`. Lifting it
means a deliberate exception to the fit-window policy for declared event
terms — the engine change with the highest leverage here, and the one that
gates the rest. That exception is
[S24](roadmap.md#statistical-rigor-s--a-standing-workstream)'s
`learn_from: window` mode ([`step_change_design.md`](step_change_design.md)
§3.2): a declared `interventions:` entry whose fit is allowed to see its own
periods, with the claim that follows — *the shift coincident with the date,
not separable from anything else dated the same* — stated on the payload.
S24's default (`learn_from: history`) stays the other way, for the
anomaly-contamination reason above, and the design says why.

### 5.3 There is no counterfactual

"The push drove N signups" means observed minus what would have happened
otherwise. Breakdown publishes a contrast of two window means, which is a
different quantity. The nearest thing is `components.trend`, and `docs/model.md`
already states plainly that it forecasts flat at the last fitted state with an
interval that does not widen with horizon
([S16](roadmap.md#statistical-rigor-s--a-standing-workstream), open). Over one
week that understatement is small; over a launch's four-week tail it is not.

[3.4](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you),
counterfactual RCA via posterior-predictive forecast, is precisely this
feature. The white paper already cites Brodersen et al.; this finishes
something the project started.

### 5.4 What-if is steady state, and a campaign is a pulse

`run_scenario`'s own docstring: *"Lags are irrelevant under steady-state
semantics: a lagged effect still fully arrives at equilibrium."* So §2.4's
`+$51.28/week` is an equilibrium rate, not "a one-week burst of $200k produced
X." The team's question is cumulative and time-bound by construction, and
`Intervention` has `metric`, `mode`, `value` — no duration.

### 5.5 Linear, no saturation, no carryover

`paid_social_sessions ← spend_paid_social` is a constant 1.10 sessions per
dollar at any spend level. Triple the budget for a week and the model returns
triple the sessions. Every marketing analyst rejects that in the first meeting
and is right to.
[S9](roadmap.md#statistical-rigor-s--a-standing-workstream) (one declared
transform on one edge, `response: log`) is the scoped fix and is open.

Carryover is worse off: `lags` is `Dict[str, int]`, a fixed integer shift. A
conference with a three-week decaying tail has no representation at all —
geometric adstock is not a lag, it is a different regressor.

## 6. What it would take

**Tier 0 — no engine work.** The tree in this directory, plus a marketing
calendar landed in the warehouse as a fact table and encoded as intensity
series per §5.1. Delivers question 1 with the §2.1 quality, and delivers
question 2 as *"here is the decomposition of that window"* — useful, and not
a lift number.

**Tier 1 — the unlock, in this order.**

1. **[S24](roadmap.md#statistical-rigor-s--a-standing-workstream), the
   declared intervention term, in its `learn_from: window` mode** (§5.2).
   A declared event/exposure regressor whose fit window deliberately spans the
   campaign, with the anomaly-contamination argument written down for why
   RCA's default stays the opposite. Designed in
   [`step_change_design.md`](step_change_design.md), which owns the YAML
   shape, the payload contract and the test plan; this note owns the
   marketing case for the `window` half. Everything else depends on it.
2. **[3.4](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you),
   counterfactual RCA** (§5.3). Turns the output into "the push drove 4,200
   signups above the no-campaign regime, 95% CI 2,900–5,600."
3. **[S16](roadmap.md#statistical-rigor-s--a-standing-workstream)**, forward-
   simulation variance in the trend interval. 3.4 without it publishes a
   confident number over a multi-week horizon the model never earned.

**Tier 2 — credibility with a marketing audience.**
[S9](roadmap.md#statistical-rigor-s--a-standing-workstream)'s saturating edge;
a geometric adstock lag beside the integer one; a duration argument on
`Intervention` so what-if can answer a pulse cumulatively (§5.4).

**And one honest limit that no tier removes.** A single multi-channel push is
one observation. If the team runs four a year,
[S19](roadmap.md#statistical-rigor-s--a-standing-workstream)'s partial pooling
across a repeated cycle is the right shape and is already filed for exactly
this reason — festivals, conferences, product launches. If this is their first
push of its kind, the correct answer is a wide interval, and the product's
job is to say so rather than to narrow it.

## 7. What this needs from `fake_companies`

The demo data cannot tell question 2's story, and the reasons are specific
rather than general.

- **`campaign_id` is not a campaign.** `shared/marketing.py` splits each paid
  channel's daily spend across two fixed ids (`{channel}_camp_1`, `_2`) at a
  constant 60/40 for all 790 days. It is a static bucket, present every day,
  with no start, no end and no cross-channel identity.
- **`utm_campaign` on sessions does not join to it.** `shared/traffic.py`
  writes `{channel}_camp`, while ad spend writes `{channel}_camp_1`/`_2`.
  Nothing relates a session to the campaign that bought it.
- **There is no influencer channel and no event channel.** The configured
  channels are organic, paid_social, paid_search, display, direct, referral,
  email. A conference or an influencer push has nowhere to live, and no
  spend-free intensity series exists anywhere in the schema.

What the demo would need: a `marketing.campaigns` calendar table (campaign key,
name, start, end, channels touched, a per-day intensity, a date-precision
flag), campaign keys that actually join across ad spend and sessions, and one
planted **dated, multi-channel, spend-partly-free** push with its true lift
recorded in `ground_truth.json` so the answer can be scored rather than
eyeballed. That is generator work, and it is the prerequisite for demoing any
of Tier 1.

## 8. What breakdown should not become

Not a marketing mix model. No geo holdouts, no multi-touch attribution, no
incrementality experiments. The causal claim rests on the declared DAG, which
the roadmap treats as the premise rather than a gap, and the team's own channel
attribution enters the tree as a dimension that breakdown slices by and never
audits. For an unbiased answer to "did the conference cause it," they need a
holdout.

What breakdown contributes is the tree-shaped decomposition, the intervals, and
§4's two unwelcome findings — the attenuation down the funnel and the 48% of
the subscription lift that was never marketing's. For a team surrounded by
confident dashboards, that last one is worth more than the elasticity.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

# Innovation-based root cause analysis — "versus expected", and where the surprise entered

**Status:** design plus a measured prototype. Nothing in the engine's default
behavior has changed; the one engine addition since is the opt-in
`trend.kind: local_linear` (roadmap S8, default still an open decision),
measured in §6.2 on 2026-11-17, and together with the opt-in per-node
`likelihood` (roadmap S20) in §6.3 on 2026-11-18. Opened 2026-11-16 from an architecture review of the RCA use case.
Proposes re-scoping roadmap [3.4](roadmap.md#horizon-3--make-it-findable-and-sticky-it-comes-to-you)
and [S16](roadmap.md#statistical-rigor-s--a-standing-workstream), and replacing
S12. The roadmap rows are **not** edited by this document; §8 lists the
proposed changes for Devon to accept or reject.

Prototype: [`benchmarks/innovation_score.py`](benchmarks/innovation_score.py).
Results in §6.

---

## 1. The two questions

An urgent "conversions are down" carries two questions, and the engine
currently answers both with one pair of windows.

1. **Did something change?** Down *versus what*. In practice the stakeholder
   usually means "versus last week", and the better question is "versus what we
   expected". This is detection: an expectation, a departure from it, and a
   date the departure began.
2. **Why did it change?** Given a departure, where in the tree did it enter,
   and in which slice. This is root cause analysis.

Today `run_rca` takes a reference window and an analysis window and decomposes
the difference of their means. The reference window stands in for the
expectation, and the analysis window's start stands in for the onset. Both are
choices the user (or the default) makes, and S23 exists to disclose how much
the answer depends on the first one.

This design separates them. Detection produces an expectation and an onset.
RCA attributes the departure from that expectation. The window-over-window
decomposition stays, as the descriptive answer to "what is different from last
month" (§5).

## 2. What field incidents look like

From Devon's account of real incidents (2026-11-16), roughly in order of
frequency, the root cause has been:

- a **slice of a source node** (most often);
- a **slice of the target node**;
- a **whole source node**;
- something **off the tree**;
- a **data problem** (late or broken pipeline) rather than a business change.

A design for RCA should produce each of these as a distinct verdict. The
current engine produces the third directly, reaches the first two only when a
person or agent chooses the right node and dimension to slice, and has no
verdict for the last two beyond a large `unexplained`.

## 3. Where the current ranking falls short

Four properties, each checked against the code.

**The ranking cannot separate a root from the nodes its effect passed
through.** `_rank_causes` pushes a score of 1.0 up from the target with hop
weights of at most 1, so a node never outscores the child it was reached
through. On a causal chain every member saturates. The White Cube test says so
in a comment: "the volume chain saturates the influence score, so which of its
members sorts first is a tie." Measured in §6: on story A the planted root
ranks 7th, behind six nodes that only transmitted it.

**A parent is credited for moving, whether or not the move was unusual.**
`beta_raw × Δparent` and the Shapley bridge both measure how much a parent's
window means differ. A routine seasonal swing and a break score the same.

**The noise question is answered by a bootstrap of the windows.** It is
attenuated on short windows and withheld on a single period, which is the
"what happened this weekend" case. It also has an unclear estimand: the window
means are observed facts, and the bootstrap treats them as a sample.

**A lagged ancestor is fitted on the periods it is being judged on.**
`_node_fit_end` returns `analysis_start` for every node. A parent read through
a lag is compared over windows shifted *back* by the lag (`parent_windows`), so
its analysis window ends up inside its own fit window. The fit-before-analysis
rule holds for the target and fails for exactly the nodes upstream of a lagged
edge. This is a defect in the current engine independent of this design, and a
candidate C row.

## 4. Design

### 4.1 A node's innovation

For each node, the **innovation** is what the node did minus what its own
model predicted *given what its parents actually did*.

| Node | Prediction | An innovation means |
|---|---|---|
| Source (no parents) | forecast from trend + seasonality | the business changed here, or the data did |
| Probabilistic | forecast level + seasonality + `beta_raw ·` parents' actual values | something not in the tree acted on this node |
| Formula with its own `source` | `formula(parents)` plus the forecast residual | the identity stopped holding: a definition or pipeline change |
| Derived (formula, no `source`) | `formula(parents)` | nothing; zero by construction |

The definition comes from the causal RCA literature (Budhathoki et al., 2022,
*Causal structure-based root cause analysis of outliers*; implemented in
DoWhy-GCM): a root cause is a node whose own noise term is anomalous, not a
node whose value is anomalous. A node that only passed on its parent's shock
has a large gap and no innovation.

The table gives the verdicts §2 asks for. A root is a source node with an
innovation. An off-tree cause is an innovation at a probabilistic node. A data
problem shows as an innovation at a formula node, or as a source innovation
that coincides with a freshness failure (§4.6). Exact identities cannot be
roots, which matches how the tree's author thinks about them.

### 4.2 Each node's own window and fit

A node's window is the target's analysis window shifted back by the lag on
every edge between them. Its fit ends where that window starts. This closes the
lagged-ancestor leak in §3 by construction. A node reached along two paths
with different total lags is an open question (§7); the prototype takes the
hull.

### 4.3 Two numbers per node, kept apart

- **Evidence.** How surprising the node's window is under its own posterior
  predictive: the window-mean innovation over the predictive standard
  deviation of that mean (`z`), and a two-sided tail probability. The level is
  simulated forward from its last fitted state, so the interval widens with
  the horizon. That is roadmap S16, obtained as a side effect.
- **Contribution.** How much of the target's departure this node's innovation
  explains: set the node to its prediction, recompute everything downstream
  through the structural equations (formulas exactly, learned edges through
  `beta_raw`) with every other node keeping its own innovation, and read the
  change at the target.

Evidence answers "is this real". Contribution answers "does it matter for the
target". A large contribution with weak evidence is noise that happens to sit
on a sensitive path, and the prototype shows this happens (§6). The published
ranking should require both, and the payload should carry both.

The target's **departure** is the same computation with every innovation
removed: actual minus what the whole tree expected. It replaces `gap` as the
quantity being explained, and it needs no reference window.

### 4.4 Attribution across nodes

Removing one node at a time is not exactly additive through products. On
White Cube the single-node contributions sum to within 7% of the departure in
every story, so the interaction is small there. The exact answer is a Shapley
value over innovations, which is O(2ⁿ) in the number of nodes in scope and
falls under rule 4. Options: publish single-node contributions with the
interaction as its own row (the same treatment the formula bridge already gives
co-movement), or run exact Shapley only over the nodes that clear the evidence
bar, which will usually be fewer than ten. The second is preferred.

### 4.5 Slices

The most common real root is a slice of a source node, so node-level scoring
is half of the answer. The same definition extends: a slice's innovation is
its actual minus its own forecast. Three ways to get the forecast, in order of
cost:

1. **Share-based.** Expected slice = node forecast × the slice's pre-window
   share. No new fits. Wrong when shares have their own seasonality.
2. **Per-slice fits.** A trend + seasonality fit per slice value. Exact in the
   sense of §4.1 and expensive: one fit per value per dimension.
3. **Hierarchical.** One model per dimension with partial pooling across slice
   values. This also addresses multiplicity across slices (roadmap S15) in the
   Bayesian way, by shrinkage, and handles small slices.

Start with (1) on nodes that clear the evidence bar, and measure it against
the planted segments in the fake_companies ground truth before building (3).

### 4.6 Detection, and data problems

**Detection** is the evidence computation run on a schedule against the latest
complete period. That is roadmap 3.1's "anomaly flagging", and 3.10's step 2
(suggested `analysis_start` dates) becomes the onset estimate from the same
model. The measured cost of getting the onset wrong is in §6: starting story
B's window two weeks after the true onset halves the root's `z`, because the
fit has already absorbed the shift into its level.

**Data problems** are mostly outside the engine. Source freshness, the last
production dbt run's results and row-count checks belong in an agent skill or
MCP tool that runs before attribution, as Devon suggested. The engine's part
is narrow: surface formula-node innovations as "the identity stopped holding",
and carry `data_from` freshness into the RCA payload so an agent can see that
a source's last period is incomplete before it reads a surprise there.

## 5. What stays

- **The declared DAG, and the split between exact and learned edges.** The
  design depends on it. Exact edges propagate innovations exactly and can
  never be roots; learned edges are where off-tree causes show up.
- **Window-over-window RCA.** "Versus last week" is a real question and the
  exact Shapley bridge is the right answer to it. The proposal is a second
  mode with the same payload shape, and `how_to_read` saying which question
  was answered.
- **Fits strictly before the window, NUTS, the marginalized level.** Unchanged.
  The prototype calls `fit_rca_node` as is.

## 6. Prototype results

`benchmarks/innovation_score.py` scores the four planted White Cube anomalies,
each over the guided-tour window and over a window starting at the anomaly's
true onset. It reports the rank of the planted root (1 is first) under four
rankings. Truth is the first tree metric in each anomaly's `affected_metrics`
in `white_cube.ground_truth.json`.

| Story | Target | Innovation contribution | Innovation `z` | Engine `ranked_causes` | Naive relative change |
|---|---|---|---|---|---|
| A (tour) | `new_mrr` | 1 | 1 | 7 | 8 |
| A (onset) | `signups` | 1 | 1 | 1 | 1 |
| B (tour) | `net_new_mrr` | 1 | 5 | 3 | 5 |
| B (onset) | `net_new_mrr` | 1 | 2 | 3 | 4 |
| C (tour) | `signups` | 1 | 1 | 2 | 1 |
| C (onset) | `signups` | 1 | 1 | 1 | 1 |
| D (tour) | `new_mrr` | 1 | 1 | 4 | 1 |
| D (onset) | `trial_conversion_rate` | 1 | 1 | 1 | 1 |

Ranking by innovation contribution put a planted root first in all eight
runs. The engine's `ranked_causes` did so in three, and the naive baseline in
five.

**What the engine's misses have in common.** In every miss the nodes ranked
above the root are intermediate nodes on the path from the root to the target
(story B: `churned_mrr`, `churned_subscriptions`, then the root). The three
runs where it ranks the root first are the three shortest trees. This is the
chain property from §3, and it grows with the distance between target and
root.

**What the prototype does not show.** Read these before the table.

- **Eight runs over four planted anomalies is a small sample**, the runs are
  not independent, and both the tree and the data were built to tell these
  stories. This is evidence the idea is worth building, not a measured
  accuracy.
- **The evidence score is weak and uncalibrated.** A planted 50% drop in one
  segment gives the root `z = −2.1` over a 28-day window (story A). Meanwhile
  unplanted nodes cross `|z| > 2`: `expansion_mrr` at 4.4 and 2.8,
  `contraction_mrr` at 2.7, `reactivations` at −2.4. Two causes are likely.
  The forward-simulated random-walk level widens quickly over a four-week
  horizon, which dilutes real shifts. And the Gaussian likelihood is wrong for
  low weekly counts, which the posterior predictive check already flags on
  some of these nodes. §6.1 measures this on windows with no planted anomaly.
- **Contribution alone ranks noise.** In story A `reactivations` is second
  with `z = −1.2`. Hence §4.3: the ranking needs an evidence gate, and the
  gate needs the calibration above.
- **Late windows leak.** Story B's tour window starts two weeks after onset.
  The root's `z` is 1.09 there and 2.03 at onset.
- **One node was unassessed.** `churn_arpu` has undefined periods early in its
  history, so its fit is refused and its innovation is unknown. Innovation
  scoring needs a fit on every node in scope, which raises the priority of
  S21 (mask the likelihood over undefined periods).
- **It stops at the node.** Story A's true cause is the mobile slice of
  `visit_signup_rate`. §4.5 is not prototyped.
- **Contributions carry no interval**, learned edges use the posterior-mean
  `beta_raw`, declared interventions are ignored, and window values are plain
  period means, including for rates.

### 6.1 Calibration on windows with nothing planted

`innovation_score.py null <csv>` scores every node as its own target over 32
weekly start dates that fall in the gaps between planted anomalies (starting
at least two weeks after the previous one ended), at horizons of 1, 3, 7, 14
and 28 days from one fit per start. That is 1,411 scored windows on 15 nodes;
the 7-day windows do not overlap, the longer ones do. A calibrated score has
`sd(z) = 1`, mean 0, and `|z| > 2` in 4.6% of windows.

**Raw `z` is not calibrated, and the error runs in both directions by kind of
metric.** At the 7-day horizon:

| Metric kind | Windows | mean `z` | sd `z` | `\|z\| > 2` |
|---|---|---|---|---|
| Rates (7 nodes) | 224 | 0.04 | 0.81 | 2.2% |
| Flows (7 nodes) | 224 | 0.54 | 1.46 | 15.2% |
| Stock (`active_subscriptions`) | 32 | 1.31 | 0.24 | 0% |

- **Rates are conservative.** Their predictive interval is about 25% too
  wide, so a real shift is under-scored. Both roots that looked weak in §6
  (`visit_signup_rate`, `customer_churn_rate`) are rates. A likely cause is
  earlier anomalies in the fit history inflating the variance terms, which is
  the effect S24 measured for undeclared steps; the per-gap numbers are
  consistent with it but are six windows each, so this is a hypothesis.
- **Flows are over-confident and biased upward.** `contraction_mrr`,
  `reactivations`, `direct_conversions`, `expansion_mrr` and `marketing_spend`
  have `sd(z)` of 1.3 to 1.9 and exceed `|z| > 3` in 3% to 16% of clean
  windows. These are the unplanted nodes that crossed 2 in §6. They are low
  weekly counts and spiky flows under a Gaussian likelihood.
- **The flat level forecast is biased on a growing business.** Mean `z` is
  positive on every flow and on the stock. `active_subscriptions` sits 1.3
  predictive sds above its forecast in every clean window, with almost no
  spread: the forecast is consistently low because the series has momentum
  the local level does not carry.
- **Longer horizons are worse, not better.** `|z| > 2` rises from 8% at 7 days
  to 16% at 28, because the bias accumulates with the horizon.

**The engine's own posterior predictive check already predicts which nodes
are miscalibrated.** By `ppc_status`, at 7 days: `ok` nodes have `sd(z)` 0.94
(195 windows), `moderate` 1.12 (113), `severe` 1.44 with mean 0.53 (172). So a
raw `z` can be trusted on a node whose PPC is `ok` and not on one flagged
`severe`.

**Scored against each node's own null distribution, the planted roots
separate.** Dividing each root's onset `z` by the same node's null sd at the
same horizon, and counting clean windows on that node that score as high:

| Planted root | 7-day `z` | Over node's null sd | Clean windows as extreme |
|---|---|---|---|
| `visit_signup_rate` (CTA regression) | −1.78 | −2.43 | 0 of 32 |
| `customer_churn_rate` (churn spike) | 1.71 | 2.33 | 1 of 32 |
| `visit_signup_rate` (Brazil lift) | 2.76 | 3.77 | 0 of 32 |
| `trial_activation_rate` (onboarding) | 3.55 | 3.53 | 0 of 32 |
| `trial_days_active` (onboarding) | 5.42 | 5.25 | 0 of 32 |
| `marketing_spend` (spend ramp) | 2.00 | 1.48 | 4 of 32 |

Five of six roots are more extreme than all, or all but one, of their node's
32 clean weeks. The miss is a ramp, which starts small by construction; it
separates at 28 days (1 of 18).

**One day is not enough at node level.** At a 1-day horizon the three daily
roots score −0.58, 0.67 and 1.57. A 50% drop in one device on one channel is a
small move in the node total. This argues for scoring slices (§4.5), where the
same shift is large.

**What this changes in the design.**

1. Publish evidence as an **empirical tail against the node's own backtest**,
   not as a raw `z`. The backtest is the null run above, per node, over the
   node's history; it is one fit per backtest date, so it belongs in the warm
   pass (roadmap 3.10).
2. Gate on `ppc_status` until the model is fixed: a `severe` node's evidence is
   reported as unreliable.
3. The model fixes that would make raw `z` trustworthy are already on the
   roadmap and move up: a count likelihood (S20) for the over-confident flows,
   and a trend with a slope (S8) for the bias. S8 was scheduled as an opt-in
   for rare momentum nodes; here nearly every flow on a growing business shows
   the bias. The bias sits on nodes with no parents: the five parentless flows
   have a 7-day mean `z` of 0.54 to 1.00 and the stock 1.31, while the two
   flows with a learned parent (`sessions`, `trials_started`) have 0.34 and
   −0.15 and no rate exceeds 0.20. A parent that grows carries the growth; a
   source has nothing to carry it.

The backtest is not independent of the anomalies' aftermath, uses one
synthetic company, and 32 windows per node cannot resolve a 5% tail well.

**Cost.** Fitting every node in scope took 11 to 57 seconds per story, cold,
for 4 to 23 nodes. On this tree, fitting source nodes as well as learned ones
is affordable with the current sampler. This has not been measured on the
106-metric B2B tree, so whether a faster fit is required remains open (§7).

### 6.2 The same null run with a slope in the trend (roadmap S8)

§6.1 blamed the upward bias on a trend with no momentum. `trend.kind:
local_linear` now exists as an opt-in (`docs/model.md`), and
`innovation_score.py null --nodes … --trend …` forces a trend kind on the
loaded definitions without editing the tree. Ten nodes were re-scored on the
same 32 windows under three trends: the local level, the local linear trend
at its default (`slope_sigma: 0.001`), and its fixed-drift limit
(`slope_sigma: 0`). The six parentless nodes were also run with a looser
slope (`slope_sigma: 0.01`). The level arm reproduces §6.1's rows (largest
difference in `z` 0.02 over 982 rows).

Mean `z` on clean windows (0 is unbiased). The standard error of a mean is
0.2 to 0.35 at 7 days, so read single cells loosely; the *paired* change from
level to slope is far tighter (standard error 0.03 to 0.05 on the flows).

| Node | Role | 7d level | 7d slope | 7d drift | 7d loose | 28d level | 28d slope | 28d drift | 28d loose |
|---|---|---|---|---|---|---|---|---|---|
| `active_subscriptions` | stock, no parents | 1.31 | 0.20 | 0.96 | 0.06 | 2.25 | 0.02 | 1.43 | −0.27 |
| `contraction_mrr` | flow, no parents | 1.00 | 0.76 | 0.72 | 0.64 | 1.58 | 0.90 | 0.90 | 0.65 |
| `reactivations` | flow, no parents | 0.75 | 0.49 | 0.48 | 0.33 | 1.31 | 0.62 | 0.65 | 0.29 |
| `direct_conversions` | flow, no parents | 0.70 | 0.47 | 0.45 | 0.31 | 1.68 | 0.97 | 1.00 | 0.67 |
| `marketing_spend` | flow, no parents | 0.57 | 0.32 | 0.28 | 0.37 | 0.44 | 0.05 | −0.08 | 0.17 |
| `expansion_mrr` | flow, no parents | 0.54 | 0.18 | 0.16 | 0.05 | 1.52 | 0.64 | 0.64 | 0.39 |
| `sessions` | flow, learned parent | 0.34 | 0.02 | −0.11 | | 0.38 | −0.25 | −0.56 | |
| `trials_started` | flow, learned parent | −0.15 | −0.17 | −0.19 | | −0.13 | −0.20 | −0.22 | |
| `visit_signup_rate` | rate | −0.12 | −0.22 | −0.29 | | 0.04 | −0.25 | −0.38 | |
| `customer_churn_rate` | rate, learned parent | 0.10 | −0.31 | −0.34 | | 0.36 | −0.69 | −0.69 | |

Spread and width, level against the default slope:

| Node | 7d sd `z` | 7d `\|z\| > 2` | 7d predictive sd, slope / level | 28d sd `z` | 28d `\|z\| > 2` | 28d predictive sd, slope / level |
|---|---|---|---|---|---|---|
| `active_subscriptions` | 0.24 → 1.30 | 0% → 16% | 0.22 | 0.24 → 1.47 | 89% → 11% | 0.26 |
| `contraction_mrr` | 1.86 → 1.94 | 19% → 22% | 0.95 | 2.06 → 2.00 | 22% → 22% | 1.00 |
| `reactivations` | 1.56 → 1.68 | 22% → 19% | 0.90 | 1.23 → 1.60 | 28% → 22% | 0.78 |
| `direct_conversions` | 1.53 → 1.64 | 19% → 19% | 0.90 | 0.95 → 1.20 | 39% → 17% | 0.86 |
| `marketing_spend` | 1.36 → 1.40 | 12% → 12% | 0.94 | 0.89 → 0.86 | 6% → 0% | 1.02 |
| `expansion_mrr` | 1.26 → 1.35 | 12% → 12% | 0.92 | 1.77 → 1.85 | 33% → 28% | 0.92 |
| `sessions` | 1.17 → 1.24 | 12% → 12% | 0.87 | 0.88 → 0.81 | 6% → 0% | 0.91 |
| `trials_started` | 1.19 → 1.23 | 9% → 9% | 1.01 | 0.75 → 0.76 | 0% → 6% | 1.36 |
| `visit_signup_rate` | 0.73 → 0.74 | 0% → 0% | 0.99 | 0.72 → 0.61 | 0% → 0% | 1.20 |
| `customer_churn_rate` | 0.74 → 0.80 | 3% → 3% | 0.94 | 0.81 → 0.95 | 6% → 11% | 0.94 |

What the numbers say:

- **The stock is fixed.** `active_subscriptions` goes from 1.31 to 0.20 at 7
  days and from 2.25 to 0.02 at 28. Its predictive interval gets *four times
  narrower*, not wider: under the local level the growth had to be explained
  as level noise, which inflated `σ_trend`; with a slope the level noise is
  small. The old score looked calm (sd 0.24) only because the interval was
  that wide. The new one is slightly over-confident (sd 1.3).
- **Parentless flows lose about a third to a half of the bias, not all of
  it.** Every one moves down, by 0.23 to 0.35 at 7 days and 0.39 to 0.88 at
  28, and every one is still positive. The looser slope takes more off on
  five of six, so part of the remainder is the slope prior holding the slope
  near its long-run average on a business whose growth is still speeding up.
- **Fixed drift is the worse of the two.** It matches the default on the
  flows and fails on the stock (1.43 at 28 days), because one constant slope
  over the whole history lags the recent one. It also over-corrects `sessions`
  (−0.56 at 28 days, 17% beyond 2).
- **The slope does not belong on rates.** `customer_churn_rate` goes from 0.10
  to −0.31 at 7 days and from 0.36 to −0.69 at 28; `visit_signup_rate` drifts
  negative too. A rate's past drift is extrapolated into a window where it
  does not continue.
- **The interval does not blow up with the horizon.** At 28 days the
  predictive sd under the slope is between 0.26 and 1.02 times the level's on
  the six parentless nodes and `sessions`. It is wider on `trials_started` (1.36) and
  `visit_signup_rate` (1.20), which did not need a slope.
- **The spread of `z` on flows is unchanged** (1.3 to 1.9). That is the
  Gaussian likelihood on spiky counts, S20's problem, and a slope does not
  touch it.

**Learned coefficients barely move.** Mean over the 32 fits of each
posterior's mean and sd, level → slope: `sessions ← marketing_spend` 1.149 ±
0.062 → 1.092 ± 0.063; `trials_started ← signups` 0.439 ± 0.020 → 0.432 ±
0.023 (19% wider); `customer_churn_rate ← member_activity_rate` −0.0300 ±
0.0152 → −0.0318 ± 0.0150. No interval that excluded zero stopped doing so.
These three edges are a weak test of the identifiability worry: two are
day-grain with weekly seasonality, so the parent has plenty of movement that
is not drift, and both carry informative priors. A week-grain child of a
smoothly growing parent is the case to fear, and this tree has none.

**Planted roots still score.** 7-day `z`, level → slope: `visit_signup_rate`
(CTA) −1.78 → −1.81; `customer_churn_rate` 1.71 → 1.67; `visit_signup_rate`
(Brazil) 2.76 → 2.59; `trial_activation_rate` 3.55 → 3.68; `trial_days_active`
5.42 → 5.34; `marketing_spend` (ramp) 2.00 → 2.04. The ramp was the worry,
since a slope could absorb one. At onset it cannot, because the fit ends
before the ramp starts, so the windows starting one, two and three weeks
*into* the ramp were scored as well: 28-day `z` 1.69 → 1.51, 2.39 → 1.95 and
3.40 → 3.10. The slope costs the ramp a little evidence and does not hide it.
The one root that gets weaker against its own null is `customer_churn_rate`
(clean windows as extreme: 1 of 32 → 3 of 32 at 7 days, 1 of 18 → 4 of 18 at
28), which is the rate result again.

**Cost.** A fit with the slope takes two to three times as long: about 3 s →
7.5 s on a week-grain node and 10 s → 29 s on day-grain `sessions`, timed
back to back in one process on a machine that was also running other fits.
The two-state filter carries five scalars per period where the level carries
two. The slope's innovation scale also mixes worse than the other
parameters: on `active_subscriptions` the minimum bulk ESS fell below 400 in
69% of fits (0% under the level), and `sessions` showed 4 to 10 divergences
in three timed fits (none under the level).

**What this suggests for the default** (a reading, not a decision): the slope
earns its place on the stock and on parentless flows, costs a rate its
calibration, and on a flow with a learned parent it is a wash at 7 days and a
small loss at 28. That is the role-based rule §8 proposed. `trend_kind` in
`breakdown/engine/model.py` is the one function to change. Before it changes:
the sampler health on the stock, the fit-time bill on a wide tree, a second
vertical, and a week-grain learned edge are all unmeasured or unresolved.

### 6.3 Both changes together: a declared likelihood and a slope by role

§6.2 measured the slope (S8) on Gaussian fits; `roadmap_log.md` records the
declared likelihood (S20) on level trends. This run puts them on the same
windows. A node with both samples its level and its slope as explicit latents
under NUTS, since the Kalman filter cannot integrate a trend out of a
non-Gaussian likelihood (`docs/model.md`).

Same 32 clean weekly windows, 7 and 28 day horizons, ten nodes. Likelihood per
node as S20's diagnosis chose: `negative_binomial` on `reactivations`,
`direct_conversions`, `sessions`; `mean_scaled_gaussian` on `expansion_mrr`,
`trials_started`, `marketing_spend`; Gaussian on `contraction_mrr`, the stock
and the rates. Trend by the role rule: a slope on the stock and the five
parentless flows, a level on everything else. Six arms:

| Arm | Likelihood | Trend |
|---|---|---|
| 1 | Gaussian | level |
| 2 | declared | level |
| 3 | Gaussian | slope by role, `slope_sigma` 0.001 |
| 4 | declared | slope by role, 0.001 |
| 5 | declared | slope by role, 0.01 |
| 6 | declared | slope by role, 0.003 |

A fit depends only on the node's own configuration, so a node whose
configuration is the same in two arms was fitted once (a rate's rows are the
same in all six). Every row here was fitted for this section, one process per
fresh compile cache; none is carried over from §6.2. `mean_scaled_gaussian`
refuses `expansion_mrr` in the 11 earliest windows, where half or more of its
short history is zero, so that node has 21 scored windows in arms 2 and 4 to 6.

**Pooled by role, 7 days.** `sd z` is about each node's own mean, so bias and
spread read separately. Calibrated is mean 0, sd 1, 4.6% beyond 2.

| Role | Arm | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS |
|---|---|---|---|---|---|---|---|
| stock | 1 | 32 | 1.31 | 0.24 | 0% | 0% | 0.84 |
| stock | 3, 4 | 32 | 0.20 | 1.31 | 16% | 16% | 0.18 |
| stock | 5 | 32 | 0.06 | 1.24 | 12% | 12% | 0.15 |
| stock | 6 | 32 | 0.08 | 1.27 | 19% | 16% | 0.16 |
| parentless flow | 1 | 160 | 0.71 | 1.53 | 17% | 14% | 0.21 |
| parentless flow | 2 | 149 | 0.66 | 1.30 | 13% | 11% | 0.25 |
| parentless flow | 3 | 160 | 0.45 | 1.62 | 17% | 18% | 0.15 |
| parentless flow | 4 | 149 | 0.37 | 1.30 | 12% | 11% | 0.17 |
| parentless flow | 5 | 149 | 0.29 | 1.28 | 11% | 11% | 0.13 |
| parentless flow | 6 | 149 | 0.34 | 1.29 | 11% | 11% | 0.16 |
| flow with parent | 1, 3 | 64 | 0.10 | 1.18 | 11% | 9% | 0.09 |
| flow with parent | 2, 4, 5, 6 | 64 | 0.13 | 1.04 | 9% | 6% | 0.11 |
| rate | all | 64 | −0.01 | 0.73 | 2% | 2% | 0.12 |

**Pooled by role, 28 days.**

| Role | Arm | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS |
|---|---|---|---|---|---|---|---|
| stock | 1 | 18 | 2.25 | 0.24 | 89% | 0% | 0.97 |
| stock | 3, 4 | 18 | 0.01 | 1.52 | 17% | 17% | 0.20 |
| stock | 5 | 18 | −0.28 | 1.26 | 6% | 17% | 0.20 |
| stock | 6 | 18 | −0.27 | 1.31 | 6% | 11% | 0.18 |
| parentless flow | 1 | 90 | 1.31 | 1.45 | 26% | 11% | 0.40 |
| parentless flow | 2 | 80 | 1.13 | 1.19 | 16% | 4% | 0.43 |
| parentless flow | 3 | 90 | 0.63 | 1.55 | 17% | 14% | 0.18 |
| parentless flow | 4 | 80 | 0.47 | 1.20 | 9% | 5% | 0.19 |
| parentless flow | 5 | 80 | 0.31 | 1.15 | 6% | 5% | 0.16 |
| parentless flow | 6 | 80 | 0.41 | 1.16 | 6% | 5% | 0.20 |
| flow with parent | 1, 3 | 36 | 0.13 | 0.82 | 3% | 3% | 0.15 |
| flow with parent | 2, 4, 5, 6 | 36 | 0.17 | 0.74 | 3% | 0% | 0.19 |
| rate | all | 36 | 0.20 | 0.77 | 3% | 0% | 0.20 |

**Per node, the six that take a slope: mean z / sd z / share beyond 2.** The
other four nodes do not differ between arms except by likelihood: `sessions`
0.34 / 1.17 / 12% Gaussian and 0.34 / 1.05 / 12% as a count; `trials_started`
−0.15 / 1.19 / 9% and −0.07 / 1.03 / 6%; the two rates are in §6.2's level
column.

| Node, 7 days | arm 1 | arm 2 | arm 3 | arm 4 | arm 5 | arm 6 |
|---|---|---|---|---|---|---|
| `active_subscriptions` | 1.31 / 0.24 / 0% | same | 0.20 / 1.31 / 16% | same as 3 | 0.06 / 1.24 / 12% | 0.08 / 1.27 / 19% |
| `contraction_mrr` | 1.00 / 1.86 / 19% | same | 0.76 / 1.96 / 22% | same as 3 | 0.65 / 1.96 / 22% | 0.73 / 1.95 / 22% |
| `reactivations` | 0.75 / 1.56 / 22% | 0.69 / 0.94 / 12% | 0.49 / 1.69 / 19% | 0.33 / 0.91 / 9% | 0.21 / 0.88 / 6% | 0.26 / 0.88 / 6% |
| `direct_conversions` | 0.70 / 1.53 / 19% | 0.69 / 1.12 / 12% | 0.48 / 1.64 / 19% | 0.34 / 0.99 / 12% | 0.22 / 0.96 / 9% | 0.28 / 0.98 / 9% |
| `marketing_spend` | 0.57 / 1.36 / 12% | 0.49 / 1.31 / 12% | 0.33 / 1.39 / 12% | 0.24 / 1.31 / 12% | 0.30 / 1.28 / 12% | 0.28 / 1.29 / 12% |
| `expansion_mrr` | 0.54 / 1.26 / 12% | 0.32 / 0.90 / 5% | 0.19 / 1.35 / 12% | 0.12 / 0.91 / 0% | −0.07 / 0.84 / 0% | 0.04 / 0.90 / 0% |

The same at 28 days:

| Node, 28 days | arm 1 | arm 2 | arm 3 | arm 4 | arm 5 | arm 6 |
|---|---|---|---|---|---|---|
| `active_subscriptions` | 2.25 / 0.24 / 89% | same | 0.01 / 1.52 / 17% | same as 3 | −0.28 / 1.26 / 6% | −0.27 / 1.31 / 6% |
| `contraction_mrr` | 1.58 / 2.06 / 22% | same | 0.89 / 2.00 / 22% | same as 3 | 0.64 / 2.02 / 22% | 0.83 / 2.00 / 22% |
| `reactivations` | 1.31 / 1.23 / 28% | 1.28 / 0.80 / 22% | 0.61 / 1.59 / 22% | 0.45 / 0.96 / 11% | 0.25 / 0.85 / 0% | 0.35 / 0.89 / 0% |
| `direct_conversions` | 1.68 / 0.95 / 39% | 1.52 / 0.72 / 22% | 0.98 / 1.20 / 17% | 0.70 / 0.80 / 6% | 0.49 / 0.70 / 6% | 0.61 / 0.72 / 6% |
| `marketing_spend` | 0.44 / 0.89 / 6% | 0.43 / 0.87 / 6% | 0.06 / 0.86 / 0% | 0.01 / 0.85 / 0% | 0.15 / 0.74 / 0% | 0.10 / 0.76 / 0% |
| `expansion_mrr` (8 windows where declared) | 1.52 / 1.77 / 33% | 0.51 / 0.37 / 0% | 0.63 / 1.81 / 22% | 0.06 / 0.48 / 0% | −0.34 / 0.30 / 0% | −0.12 / 0.44 / 0% |

**Fit health, one fit per window.** `suspect` is the engine's own
`fit_quality`; a `severe` posterior predictive check sets it.

| Node | Arm | PPC ok / moderate / severe | min ESS < 400 | fits with a divergence | mean divergences | suspect |
|---|---|---|---|---|---|---|
| `active_subscriptions` | 1 | 32 / 0 / 0 | 0% | 0% | 0.0 | 0% |
| `active_subscriptions` | 3, 4 | 32 / 0 / 0 | 62% | 44% | 3.3 | 9% |
| `active_subscriptions` | 5 | 32 / 0 / 0 | 41% | 28% | 3.4 | 3% |
| `active_subscriptions` | 6 | 32 / 0 / 0 | 50% | 41% | 3.9 | 9% |
| `contraction_mrr` | 1 | 0 / 0 / 32 | 0% | 9% | 0.2 | 100% |
| `contraction_mrr` | 3, 4 | 0 / 0 / 32 | 3% | 9% | 0.1 | 100% |
| `reactivations` | 1 | 6 / 11 / 15 | 0% | 3% | 0.3 | 47% |
| `reactivations` | 2 | 32 / 0 / 0 | 0% | 6% | 0.1 | 0% |
| `reactivations` | 3 | 0 / 14 / 18 | 0% | 16% | 0.2 | 56% |
| `reactivations` | 4 | 30 / 2 / 0 | 0% | 41% | 0.8 | 0% |
| `reactivations` | 5 | 30 / 2 / 0 | 3% | 88% | 6.0 | 9% |
| `reactivations` | 6 | 30 / 2 / 0 | 0% | 38% | 1.1 | 0% |
| `direct_conversions` | 1 | 0 / 4 / 28 | 0% | 12% | 0.5 | 88% |
| `direct_conversions` | 2 | 30 / 2 / 0 | 0% | 19% | 0.9 | 0% |
| `direct_conversions` | 3 | 0 / 0 / 32 | 0% | 6% | 0.1 | 100% |
| `direct_conversions` | 4 | 31 / 1 / 0 | 3% | 47% | 0.8 | 0% |
| `direct_conversions` | 5 | 32 / 0 / 0 | 0% | 91% | 4.6 | 0% |
| `direct_conversions` | 6 | 31 / 1 / 0 | 0% | 34% | 0.8 | 0% |
| `expansion_mrr` | 1 | 0 / 0 / 32 | 3% | 6% | 0.1 | 100% |
| `expansion_mrr` | 2 | 0 / 0 / 21 | 0% | 33% | 0.5 | 100% |
| `expansion_mrr` | 3 | 0 / 0 / 32 | 9% | 19% | 0.2 | 100% |
| `expansion_mrr` | 4 | 0 / 0 / 21 | 38% | 24% | 0.5 | 100% |
| `expansion_mrr` | 5 | 0 / 0 / 21 | 57% | 90% | 8.8 | 100% |
| `expansion_mrr` | 6 | 0 / 0 / 21 | 29% | 62% | 1.8 | 100% |
| `marketing_spend` | 1 | 0 / 0 / 32 | 0% | 0% | 0.0 | 100% |
| `marketing_spend` | 2 | 0 / 0 / 32 | 16% | 59% | 2.4 | 100% |
| `marketing_spend` | 3 | 0 / 0 / 32 | 3% | 31% | 0.6 | 100% |
| `marketing_spend` | 4 | 0 / 0 / 32 | 59% | 75% | 3.6 | 100% |
| `marketing_spend` | 5 | 0 / 0 / 32 | 84% | 91% | 10.8 | 100% |
| `marketing_spend` | 6 | 0 / 0 / 32 | 69% | 88% | 20.1 | 100% |
| `sessions` | 1 | 30 / 2 / 0 | 0% | 0% | 0.0 | 0% |
| `sessions` | 2 | 31 / 1 / 0 | 9% | 50% | 2.6 | 3% |
| `trials_started` | 1 | 0 / 16 / 16 | 0% | 31% | 0.6 | 50% |
| `trials_started` | 2 | 29 / 3 / 0 | 31% | 100% | 13.2 | 12% |

**Fit time**, one fit at a time, mean of two windows (2026-04-06 and
2026-07-13, the longest histories), seconds. The arm runs themselves were
three to nine processes at once on twelve cores and their wall times are not
comparable.

| Node | Gaussian, level | declared, level | Gaussian, slope 0.001 | declared, slope 0.001 | declared, 0.003 | declared, 0.01 |
|---|---|---|---|---|---|---|
| `active_subscriptions` (Gaussian) | 2.9 | | 7.7 | | 7.7 | 8.2 |
| `contraction_mrr` (Gaussian) | 2.6 | | 5.4 | | 5.5 | 5.3 |
| `reactivations` | 2.5 | 13.1 | 5.4 | 14.3 | 17.3 | 19.6 |
| `direct_conversions` | 2.5 | 7.2 | 5.3 | 10.0 | 11.1 | 15.4 |
| `expansion_mrr` | 2.5 | 6.9 | 5.3 | 9.4 | 14.2 | 20.2 |
| `marketing_spend` | 6.3 | 33.4 | 16.7 | 110.4 | 111.2 | 111.6 |
| `sessions` (level under the role rule) | 11.2 | 50.2 | | | | |
| `trials_started` (level under the role rule) | 9.6 | 9.7 | | | | |

The slope roughly doubles a Gaussian fit, as in §6.2. On a week-grain count
the likelihood is the larger cost (2.5 s to 7 to 13 s) and the slope adds a
third to it. A day-grain node with both is the expensive case: 110 s.

**The planted roots, arms 1, 4, 5 and 6: z, and clean windows on the same node
as extreme.** Five of the six roots are rates on a level trend with a Gaussian
likelihood under every arm, so their rows do not move.

| Root | Onset | Horizon | arm 1 | arm 4 | arm 5 | arm 6 |
|---|---|---|---|---|---|---|
| `visit_signup_rate` (CTA) | 2026-02-02 | 7 | −1.78 (0/32) | same | same | same |
| | | 28 | −2.14 (0/18) | same | same | same |
| `customer_churn_rate` | 2026-04-27 | 7 | 1.71 (1/32) | same | same | same |
| | | 28 | 2.03 (1/18) | same | same | same |
| `visit_signup_rate` (Brazil) | 2025-03-03 | 7 | 2.76 (0/32) | same | same | same |
| | | 28 | 2.53 (0/18) | same | same | same |
| `trial_activation_rate` | 2025-08-04 | 7 | 3.55 (0/32) | same | same | same |
| | | 28 | 7.63 (0/18) | same | same | same |
| `trial_days_active` | 2025-08-04 | 7 | 5.42 (0/32) | same | same | same |
| | | 28 | 12.14 (0/18) | same | same | same |
| `marketing_spend` (ramp) | 2025-03-03 | 7 | 2.00 (4/32) | 2.07 (4/32) | 2.41 (2/32) | 2.28 (2/32) |
| | | 28 | 2.17 (1/18) | 2.05 (0/18) | 2.33 (0/18) | 2.21 (0/18) |

What the numbers say:

- **The two changes fix different things and add.** The likelihood moves the
  spread and leaves the bias (parentless flows, 7 days: sd 1.53 → 1.30, mean
  0.71 → 0.66). The slope moves the bias and leaves the spread (mean 0.71 →
  0.45, sd 1.53 → 1.62). Together: mean 0.37, sd 1.30, and at 28 days the
  share beyond 2 falls from 26% to 9%.
- **The two count flows are close to calibrated.** `reactivations` and
  `direct_conversions` under arm 4: mean 0.33 and 0.34, sd 0.91 and 0.99,
  9% and 12% beyond 2 at 7 days; 30 and 31 of 32 fits pass their posterior
  predictive check, against 6 and 0 under arm 1. The residual mean is two
  standard errors from zero (0.17 each), still positive.
- **`expansion_mrr` is calibrated where its likelihood is accepted** (0.12 /
  0.91 / 0% over 21 windows) and unscored in the 11 where it is refused. Its
  predictive check stays `severe` in every window on `min`: the model puts no
  mass on the exact zeros the series has.
- **Two parentless flows are not helped.** `contraction_mrr` is zero half the
  time and has no likelihood that fits it (sd 1.96, 22% beyond 2, every
  check `severe`); S20 names a Tweedie-type likelihood as the open item.
  `marketing_spend` wants noise proportional to its level, which
  `mean_scaled_gaussian` is not: sd 1.31 with it and 1.39 without, every
  check `severe`, at seven times the fit time (110 s against 17 s) and a
  sampler that fails its own diagnostics in most windows. On this node the Gaussian with a slope (arm 3)
  is the better fit of the two.
- **The stock's bias is gone and its interval is now too narrow.** Mean 0.20
  (0.06 with the looser slope), sd 1.2 to 1.3, 12% to 19% beyond 2 at 7
  days. Its predictive check passes in all 32 windows, so the in-sample check
  does not see what the forecast gets wrong.
- **Nodes with a parent and rates are untouched by the role rule**, and the
  likelihood alone brings the two parented flows to sd 1.04.

**Is the raw z good enough that the per-node backtest of §6.1 becomes a
refinement?** For three of the seven nodes the changes were aimed at, yes:
the two counts and, where it is fitted, `expansion_mrr` have a raw z a reader
can take at face value, with a small positive lean. For the other four, no.
`contraction_mrr` and `marketing_spend` exceed 2 in 12% to 22% of clean weeks
under every arm, and the stock in 12% to 19%. Pooled over the parentless
flows the share beyond 2 is 11% to 12% at 7 days against a calibrated 4.6%.
So the backtest stays a necessity for now, as a gate: the engine already
marks every fit of `contraction_mrr`, `marketing_spend` and `expansion_mrr`
`suspect`, which is the right signal for those three, but it passes the
stock, where only a backtest shows the problem.

**Which `slope_sigma`.** The three values differ less in what they predict
than in how they sample.

- *Bias.* Looser is lower, by a little. Paired on the same windows, the mean
  z on parentless flows falls by 0.035 from 0.001 to 0.003 and by 0.088 from
  0.001 to 0.01 at 7 days (standard errors 0.006 and 0.010), and by 0.055 and
  0.158 at 28 days. On the stock, 0.12 and 0.15 at 7 days; at 28 days both
  looser values overshoot to −0.27 where 0.001 sits at 0.01.
- *Spread and width.* No real difference in sd z. The predictive is 2% to 3%
  wider at 7 days and 3% (0.003) or 10% (0.01) wider at 28.
- *Sampler health.* This is where they separate. On the explicit-latent
  nodes, 0.01 puts a divergence in about 90% of fits (4.6 to 8.8 a fit)
  against 24% to 47% (0.5 to 0.8) at 0.001, on the two counts and
  `expansion_mrr`; 0.003 is level with 0.001 on the counts and in between
  on `expansion_mrr`. On the Gaussian stock the
  order reverses mildly: ESS under 400 in 62%, 50% and 41% of fits at 0.001,
  0.003 and 0.01.
- *Cost.* On the explicit-latent week-grain fits, 0.003 is 10% to 50%
  slower than 0.001 and 0.01 is 40% to 115% slower; the marginalized fits
  do not change.

On that evidence 0.01 is out for any node that samples its slope explicitly,
and between 0.001 and 0.003 the data do not separate them firmly. 0.003 takes
about a fifth of the remaining bias off the counts at no measured cost in
health and a fifth more fit time,
which is the case for it; the stock's 28-day overshoot, on 18 overlapping
windows, is the case against. If one value has to serve both paths, 0.003.

**Sampler health, and one lever.** The explicit slope under a declared
likelihood diverged at the engine's `target_accept` of 0.9: 18 and 21
divergences on the two planted worlds in `tests/test_local_linear_trend.py`,
and on `reactivations` over eight real windows a divergence in every fit
(11.4 a fit). At 0.97 that is 1 and 1 on the planted worlds and 0.2 a fit on
`reactivations`, so that one path now runs at 0.97; every arm above used it.
The same lever was tried on the Gaussian stock with a marginalized slope,
eight windows, and not adopted: divergences 1.2 a fit → 0, `suspect` 12% →
0, ESS under 400 in 50% → 38% of fits, no slower. What remains slow to mix
there is `sigma_obs` against `sigma_trend` on a series with almost no noise,
which is not the slope. The slope is already non-centred on the explicit
path.

**Limits.** One synthetic company. The 28-day windows overlap. The
`trials_started` and `sessions` fits under their declared likelihoods diverge
in 100% and 50% of windows at 0.9, which is S20's level path and was not
changed here. `marketing_spend` under arms 4 to 6 should not be read as a
result about slopes; it is a likelihood that does not fit, sampled badly.

### 6.4 A second company: Alpenglow (retail, direct to consumer)

§6.1 to §6.3 are one company. White Cube is a subscription business that
only grows, with no annual cycle and no series that switches off. This
section repeats the null run on Alpenglow Supply Co., the `retail_dtc`
vertical of fake_companies, to see which of those conclusions travel.

**Setup.** Alpenglow had no breakdown tree. `demo/alpenglow_tree.yml` is a
benchmark tree written for this section (20 metrics, 14 fetched and not
formulas), on the `dbt` provider against the published
`alpenglow.duckdb`, with its series committed as snapshots. It keeps the
spine of the generator's scenario tree (spend → sessions → orders → revenue,
less discounts and refunds) and adds the three rates the planted anomalies
enter through, sessions split into bought and earned, and the customer base
behind repeat orders. `innovation_score.py --company alpenglow` runs every
mode on it. The clean windows are derived from `alpenglow.ground_truth.json`:
weekly starts at least 14 days after one planted anomaly ends (data-quality
events included) and 26 weeks after the data starts, with the window ending
before the next anomaly begins. That gives 54 starts in five gaps: 7 in
December 2024 and January 2025, one each in March and June 2025, 25 from
July to December 2025 and 20 from January to May 2026. White Cube's 32
hand-written starts are kept as they were; the same rule applied to its
ground truth gives a different list (`innovation_score.py windows`).

**The company in numbers** (`innovation_score.py --company alpenglow
describe`, model-free: the level is a centred four-week mean and the
residual is what an additive weekday effect leaves).

| Node | Role | Grain | Mean | Growth | Largest 13-week fall | Annual swing | Zeros (longest run) | Variance power | Late sd | Dispersion |
|---|---|---|---|---|---|---|---|---|---|---|
| `marketing_spend` | flow, no parents | day | 3,330 | 1.37 | 30% | 0.60 | 0 | 1.85 | 1.12 | |
| `organic_sessions` | flow, no parents | day | 2,600 | 1.45 | 28% | 0.66 | 0 | 2.07 | 1.26 | 25 |
| `email_sessions` | flow, no parents | day | 789 | 1.61 | 28% | 0.71 | 0 | 1.78 | 1.26 | 8.8 |
| `direct_sessions` | flow, no parents | day | 963 | 1.40 | 29% | 0.65 | 0 | 2.36 | 1.23 | 11 |
| `discounted_orders` | flow, no parents | day | 3.3 | 1.77 | 100% | 12 | 97.1% (224) | | 1.50 | 153 |
| `customer_base` | stock, no parents | day | 48,100 | 6.37 | 0% | 0.24 | 0 | −0.30 | 1.29 | |
| `conversion_rate` | rate | day | 0.018 | 1.02 | 6% | 0.10 | 0 | | 0.89 | |
| `aov` | rate | day | 143 | 1.01 | 17% | 0.23 | 0 | | 0.75 | |
| `return_rate` | rate | week | 0.060 | 1.08 | 18% | 1.01 | 0 | | 0.66 | |
| `paid_sessions` | flow ← `marketing_spend` | day | 3,680 | 1.38 | 30% | 0.60 | 0 | 1.64 | 1.11 | 31 |
| `returning_customer_orders` | flow ← `customer_base` | day | 182 | 7.32 | 0% | 0.40 | 4.1% (30) | 0.55 | 1.47 | 3.2 |
| `units` | flow ← `orders` | day | 644 | 3.02 | 13% | 0.42 | 0 | 1.05 | 1.38 | 9.0 |
| `refunds` | flow ← `returned_units` | week | 26,000 | 2.94 | 16% | 0.64 | 0 | 0.91 | 1.28 | |
| `discounts` | flow ← `discounted_orders` | day | 100 | 1.78 | 100% | 15 | 97.1% (224) | | 1.56 | |

*Growth* is the mean of the last third of the history over the first third.
*Largest 13-week fall* is how far the level drops over its worst quarter.
*Annual swing* is the range of the level about a straight line, over the
mean. *Variance power* is the slope of log residual variance on log level (0
constant noise, 1 Poisson-like, 2 noise proportional to the level; not
meaningful for a rate, whose noise shrinks as its denominator grows). *Late
sd* is the residual sd in the last third over the pooled one. *Dispersion* is
residual variance over the mean, for counts (1 is a Poisson).

What this says before any fit:

- **It is not a business that only grows.** Sessions and spend grow about
  30% year on year and swing by 60% to 70% of their mean within the year,
  peaking in late November. Every spring they fall by 30% in a quarter. The
  three rates are flat.
- **Two series only grow:** the customer base (a running total) and the
  repeat orders it drives, both about seven times larger at the end.
- **The noise is proportional to the level.** Variance power is 1.6 to 2.4 on
  spend and every session count, and the counts are 9 to 31 times more
  dispersed than a Poisson. That is the case §6.3 found no likelihood for on
  a currency flow; on a count the negative binomial's quadratic term covers
  it in principle.
- **Structural zeros are present, as a switch and not as a season.**
  `discounted_orders` and `discounts` are zero on 97% of days, with one run
  of 224 days, and non-zero only inside four promotions of four to eight
  days. No series has an off-season: every product category sells every day.
- **There is one stock**, added for this study, and it is a running total. A
  stock that can fall (inventory, active subscribers with churn) is not in
  this data.

**The default model (Gaussian noise, local level), all 54 windows.** Pooled
by role, with the two promotion series kept out of the pools because their
clean windows are almost all exact zeros. `sd z` is about each node's own
mean. Calibrated is mean 0, sd 1, 4.6% beyond 2.

| Role | Horizon | n | mean z | sd z | \|z\|>2 | \|z\|>2 centred | PIT KS | PPC ok / moderate / severe | suspect |
|---|---|---|---|---|---|---|---|---|---|
| stock (1 node) | 7 | 54 | 2.72 | 0.46 | 98% | 0% | 0.98 | 54 / 0 / 0 | 0% |
| | 28 | 43 | 5.60 | 0.88 | 100% | 0% | 1.00 | 43 / 0 / 0 | 0% |
| parentless flow (4) | 7 | 216 | 0.10 | 1.19 | 10% | 10% | 0.08 | 0 / 4 / 212 | 99% |
| | 28 | 172 | 0.31 | 0.89 | 3% | 3% | 0.16 | 0 / 3 / 169 | 99% |
| flow with parent (4) | 7 | 216 | −0.11 | 1.32 | 10% | 10% | 0.09 | 93 / 56 / 67 | 31% |
| | 28 | 172 | −0.10 | 1.25 | 6% | 6% | 0.12 | 76 / 43 / 53 | 31% |
| rate (3) | 7 | 162 | 0.05 | 0.66 | 1% | 1% | 0.15 | 51 / 42 / 69 | 43% |
| | 28 | 129 | 0.12 | 0.51 | 0% | 0% | 0.23 | 41 / 36 / 52 | 41% |
| `discounted_orders` | 7 | 54 | −0.19 | 2.38 | 6% | 6% | 0.46 | 0 / 0 / 54 | 100% |
| `discounts` | 7 | 54 | 0.13 | 0.74 | 4% | 4% | 0.45 | 0 / 0 / 54 | 100% |

Per node, 7 days / 28 days:

| Node | mean z | sd z | \|z\|>2 | PPC ok / moderate / severe (7d) | min ESS < 400 | fits with a divergence (mean count) | suspect |
|---|---|---|---|---|---|---|---|
| `customer_base` | 2.72 / 5.60 | 0.46 / 0.88 | 98% / 100% | 54 / 0 / 0 | 0% | 13% (0.3) | 0% |
| `marketing_spend` | −0.04 / 0.11 | 0.99 / 0.53 | 6% / 0% | 0 / 0 / 54 | 11% | 13% (10.5) | 100% |
| `organic_sessions` | 0.16 / 0.36 | 1.30 / 1.02 | 13% / 5% | 0 / 0 / 54 | 9% | 15% (5.9) | 100% |
| `email_sessions` | 0.11 / 0.42 | 1.25 / 1.05 | 11% / 5% | 0 / 4 / 50 | 7% | 15% (9.1) | 96% |
| `direct_sessions` | 0.16 / 0.35 | 1.21 / 0.85 | 11% / 5% | 0 / 0 / 54 | 7% | 15% (7.7) | 100% |
| `paid_sessions` | −0.04 / −0.10 | 1.01 / 0.77 | 4% / 0% | 41 / 13 / 0 | 0% | 4% (0.1) | 0% |
| `returning_customer_orders` | −0.23 / 0.05 | 1.85 / 2.13 | 24% / 21% | 0 / 0 / 54 | 13% | 78% (13.0) | 100% |
| `units` | 0.00 / −0.05 | 1.21 / 0.51 | 7% / 0% | 2 / 39 / 13 | 0% | 11% (0.2) | 24% |
| `refunds` | −0.15 / −0.30 | 1.04 / 0.92 | 4% / 2% | 50 / 4 / 0 | 9% | 39% (5.6) | 2% |
| `conversion_rate` | 0.09 / 0.34 | 0.77 / 0.66 | 4% / 0% | 0 / 36 / 18 | 0% | 22% (0.4) | 33% |
| `aov` | −0.02 / −0.07 | 0.71 / 0.39 | 0% / 0% | 51 / 3 / 0 | 0% | 4% (0.1) | 0% |
| `return_rate` | 0.09 / 0.10 | 0.47 / 0.43 | 0% / 0% | 0 / 3 / 51 | 11% | 43% (7.5) | 96% |

What the default model does here:

- **The stock is biased upward, worse than White Cube's.** `customer_base`
  sits 2.7 predictive sds above its forecast at 7 days and 5.6 at 28, in
  every window, and passes its posterior predictive check in every fit.
- **The parentless flows are not biased overall, and are biased by season.**
  Pooled mean z is 0.10 at 7 days and 0.31 at 28, against White Cube's 0.71
  and 1.31. Split by where the window sits in the year (below), the bias is
  there on the way up to the November peak and absent on the way down. These
  nodes declare annual seasonality, which carries most of the cycle once
  there is a year of history behind it.
- **Rates are conservative**, as on White Cube: sd z 0.47 to 0.77.
- **Flows are a little over-confident, one of them badly.** sd z is 0.99 to
  1.30 on seven of the eight flows, against White Cube's 1.2 to 1.9.
  `returning_customer_orders`, the one flow that grows sevenfold, is at 1.85
  with 24% of clean weeks beyond 2. It is also the daily child of a smoothly
  growing parent, the edge §6.2 named as the one to fear: 78% of its fits
  have a divergence and 13% fall under 400 effective samples.
- **Nodes with a learned parent and rates are not biased** (−0.11 and 0.05).

**A slope by role (arms B and C).** `--trend role` puts a local linear trend
on the six parentless flows and stocks and leaves the other eight nodes on
the level. Arm B (`slope_sigma` 0.001) ran on all 54 windows; arm C (0.003)
on every second one.

Mean z / sd z / share beyond 2, by where the window sits. *Early* is December
2024 to June 2025, with under a year of history; *rising* is July to December
2025; *falling* is January to May 2026.

| Role | Horizon | Arm | all 54 | early (9) | rising (25) | falling (20) |
|---|---|---|---|---|---|---|
| parentless flow (4 nodes) | 7 | A, level | 0.10 / 1.19 / 10% | −0.03 / 1.26 / 14% | 0.28 / 1.27 / 12% | −0.06 / 1.07 / 6% |
| | | B, slope | −0.12 / 1.29 / 12% | −0.32 / 1.29 / 11% | 0.08 / 1.38 / 15% | −0.28 / 1.19 / 9% |
| | 28 | A, level | 0.31 / 0.89 / 3% | 0.21 / 0.97 / 0% | 0.62 / 0.87 / 7% | −0.06 / 0.70 / 0% |
| | | B, slope | −0.09 / 0.98 / 4% | −0.25 / 0.98 / 6% | 0.20 / 1.00 / 5% | −0.43 / 0.83 / 3% |
| stock | 7 | A, level | 2.72 / 0.46 / 98% | 2.65 / 0.37 / 89% | 2.95 / 0.46 / 100% | 2.47 / 0.34 / 100% |
| | | B, slope | −0.67 / 1.02 / 7% | −0.92 / 1.02 / 11% | −0.25 / 0.82 / 0% | −1.09 / 1.07 / 15% |
| | 28 | A, level | 5.60 / 0.88 / 100% | 5.88 / 0.09 / 100% | 6.00 / 0.89 / 100% | 5.01 / 0.62 / 100% |
| | | B, slope | −0.22 / 1.09 / 2% | −0.25 / 0.51 / 0% | 0.61 / 0.64 / 0% | −1.29 / 0.60 / 6% |

Per node, level → slope (0.001), 54 windows. The change in z is paired on the
same windows; its standard error is 0.01 to 0.04.

| Node | 7d mean z | 7d sd z | 7d change in z | 7d predictive sd, slope / level | 28d mean z | 28d change in z | fits with a divergence | fit s |
|---|---|---|---|---|---|---|---|---|
| `customer_base` | 2.72 → −0.67 | 0.46 → 1.02 | −3.39 | 0.19 | 5.60 → −0.22 | −5.82 | 13% → 4% | 10 → 31 |
| `organic_sessions` | 0.16 → −0.11 | 1.30 → 1.43 | −0.27 | 0.93 | 0.36 → −0.15 | −0.52 | 15% → 61% | 12 → 37 |
| `email_sessions` | 0.11 → −0.11 | 1.25 → 1.36 | −0.22 | 0.93 | 0.42 → 0.01 | −0.41 | 15% → 80% | 12 → 37 |
| `direct_sessions` | 0.16 → −0.09 | 1.21 → 1.31 | −0.25 | 0.94 | 0.35 → −0.10 | −0.45 | 15% → 54% | 11 → 35 |
| `marketing_spend` | −0.04 → −0.18 | 0.99 → 1.01 | −0.14 | 1.01 | 0.11 → −0.13 | −0.24 | 13% → 17% | 13 → 37 |
| `discounted_orders` | −0.19 → −0.20 | 2.38 → 2.39 | 0.00 | 1.01 | −0.02 → −0.02 | 0.00 | 4% → 17% | 9 → 25 |

- **The stock is fixed, as on White Cube, and for the same reason.** Its
  interval gets five times narrower, because the growth no longer has to be
  explained as level noise. It then over-shoots: mean z −0.67 at 7 days, and
  −1.09 in the months when acquisition is slowing after the holidays. A slope
  held near its long-run value lags a growth rate that has a season. Its
  spread is right (sd 1.02) where White Cube's was too narrow (1.3). Its
  predictive check passes in all 54 fits under both trends, with the forecast
  2.7 sds out or 0.7 sds out.
- **On the seasonal flows the slope moves every window down by the same
  amount, whatever the season.** 0.14 to 0.27 at 7 days and 0.24 to 0.52 at
  28. That removes the bias on the way up (rising, 28 days: 0.62 → 0.20) and
  puts an equal one in on the way down (falling: −0.06 → −0.43) and in the
  short-history windows (0.21 → −0.25). The pooled mean improves at 28 days
  only because the two cancel. The absolute z of a window goes up at 7 days
  on every one of them (by 0.02 to 0.10) and the share beyond 2 goes from
  10% to 12%.
- **It does not widen the interval.** The predictive sd is 6% to 7% narrower
  at 7 days on the session counts and unchanged at 28; on spend it is
  unchanged at 7 and 9% wider at 28.
- **It costs sampler health and time.** A divergence in 54% to 80% of the
  session-count fits against 15% under the level (the mean count per fit is
  no higher, 3 to 5 against 6 to 9, because the level's are concentrated in
  a few bad fits). Fit time triples. Minimum ESS is better under the slope.
- **On the series that is nearly all zeros it changes nothing** and costs
  three times the fit.
- **A slope forced on the three rates** (`--trend local_linear`, 27 windows)
  leaves the two flat ones alone (`aov` +0.04, `conversion_rate` −0.10 at 7
  days) and drags `return_rate` down by 0.26 at 7 days and 0.51 at 28. That
  rate climbed over its first weeks, while returns from the first orders
  were still arriving, and the slope extrapolates the climb. It also costs
  the recall its evidence: the planted root's 28-day z goes from 1.67 (0 of
  22 clean windows as extreme) to 0.49 (12 of 22).

**0.001 against 0.003**, on the 27 windows both ran. Paired, the change in z
from 0.001 to 0.003 is within ±0.03 on every node at both horizons. The
predictive sd is 0% to 2% wider at 7 days and 0% to 4% wider at 28. A
divergence in 53% of parentless-flow fits at 0.001 and 47% at 0.003 (3.8 and
4.7 a fit); 4% and 7% on the stock; ESS under 400 in 1% and 3%; the same fit
time. The stock's mean z is −0.71 under both. Nothing here separates them.
All of this is the marginalized Gaussian path; §6.3's argument for 0.003 was
on the explicit path under a declared likelihood, which this run has at
0.001 only (arm E, below).

**A declared likelihood (arm D), level trend.** Chosen from the company
table:
`negative_binomial` on the whole-number counts (`organic_sessions`,
`email_sessions`, `paid_sessions`, `returning_customer_orders`, `units`,
`discounted_orders`), `mean_scaled_gaussian` on the two currency flows whose
residual sd is larger late (`marketing_spend`, `refunds`), Gaussian on the
rates, the stock and `discounts` (97% zeros, which `mean_scaled_gaussian`
refuses). `direct_sessions` was left out to save fits; it has the same
generator as the other two earned-session counts. Every second weekly start
(27), except week-grain `refunds` on all 54. Each row is against the Gaussian
fit of the same node on the same windows, 7 days.

| Node | Likelihood | mean z | sd z | \|z\|>2 | predictive sd, declared / Gaussian | PPC ok / moderate / severe | min ESS < 400 | fits with a divergence (mean count) | suspect | fit s |
|---|---|---|---|---|---|---|---|---|---|---|
| `returning_customer_orders` | Gaussian | −0.20 | 1.82 | 22% | | 0 / 0 / 27 | 7% | 85% (10.9) | 100% | 16 |
| | negative binomial | −0.19 | 1.01 | 7% | 1.97 | 4 / 11 / 12 | 15% | 4% (0.1) | 44% | 79 |
| `units` | Gaussian | −0.31 | 1.26 | 7% | | 1 / 20 / 6 | 0% | 11% (0.2) | 22% | 10 |
| | negative binomial | −0.27 | 1.09 | 4% | 1.01 | 7 / 18 / 2 | 15% | 15% (1.3) | 11% | 114 |
| `paid_sessions` | Gaussian | −0.09 | 1.06 | 7% | | 21 / 6 / 0 | 0% | 0% (0.0) | 0% | 10 |
| | negative binomial | −0.09 | 0.92 | 4% | 1.09 | 25 / 2 / 0 | 33% | 89% (15.5) | 19% | 61 |
| `organic_sessions` | Gaussian | −0.07 | 1.46 | 22% | | 0 / 0 / 27 | 11% | 15% (6.3) | 100% | 12 |
| | negative binomial | −0.08 | 1.23 | 19% | 1.16 | 0 / 4 / 23 | 37% | 100% (16.2) | 93% | 44 |
| `email_sessions` | Gaussian | 0.15 | 1.26 | 11% | | 0 / 3 / 24 | 11% | 15% (8.9) | 96% | 12 |
| | negative binomial | 0.13 | 1.07 | 7% | 1.16 | 7 / 9 / 11 | 22% | 96% (10.2) | 44% | 60 |
| `marketing_spend` | Gaussian | −0.03 | 0.99 | 7% | | 0 / 0 / 27 | 15% | 15% (7.7) | 100% | 12 |
| | mean-scaled | −0.04 | 0.88 | 0% | 1.11 | 0 / 0 / 27 | 44% | 56% (8.0) | 100% | 62 |
| `refunds` (week, 54) | Gaussian | −0.15 | 1.04 | 4% | | 50 / 4 / 0 | 9% | 39% (5.6) | 2% | 4 |
| | mean-scaled | −0.16 | 0.83 | 0% | 1.23 | 54 / 0 / 0 | 87% | 100% (34.3) | 35% | 8 |

Fit seconds are medians from the arm itself, four processes at once on a
machine that others were loading, so read them as ratios.

- **The count likelihood does what §6.3 said it does, on the nodes that
  needed it.** `returning_customer_orders` is the one flow the default gets
  badly wrong (sd 1.82, 22% beyond 2, a divergence in 85% of fits). It grows
  sevenfold, so one noise level is far too narrow late. Under the negative
  binomial its predictive interval doubles, sd z is 1.01 and the divergences
  go. The three session counts and `units` move the same way by less (sd 1.06
  to 1.46 → 0.92 to 1.23). The mean does not move on any node (paired change
  in z under 0.05), as designed.
- **It costs sampler health on a long daily series.** The session counts
  diverge in 89% to 100% of fits under the negative binomial (10 to 16 a fit)
  against 0% to 15% under the Gaussian, and a third of fits fall under 400
  effective samples. §6.3 saw this on White Cube's `sessions` (50%) and
  `trials_started` (100%); it is the explicit level path at `target_accept`
  0.9, not something about this company.
- **`mean_scaled_gaussian` hurt both currency flows it was tried on.**
  `marketing_spend` and `refunds` were already calibrated under the default
  (sd 0.99 and 1.04). The declared likelihood makes both conservative (0.88
  and 0.83), leaves spend's predictive check `severe`, and puts a divergence
  in 56% and 100% of fits. The model-free diagnosis (late sd 1.12 and 1.28)
  said they needed it and the null run says they did not: the diagnosis looks
  at the series alone, and both nodes have something that carries the level
  for the noise to be measured against (annual seasonality on spend, a
  parent on refunds).

**Both together (arm E): the declared likelihood plus the slope by role**,
`slope_sigma` 0.001, on the two nodes that take both and were affordable:
`organic_sessions` and `email_sessions`, 27 windows. (`customer_base` under
arm E is arm B's fit; `marketing_spend` was not run with both, since arm D
shows its declared likelihood is the wrong one; `discounted_orders` was not
run with both, since the slope does nothing to it.) Mean z / sd z / share
beyond 2 at 7 days, and fit health:

| Node | Arm | mean z | sd z | \|z\|>2 | 28d mean z | PPC ok / moderate / severe | min ESS < 400 | fits with a divergence (mean count) | fit s |
|---|---|---|---|---|---|---|---|---|---|
| `organic_sessions` | A: Gaussian, level | −0.07 | 1.46 | 22% | 0.28 | 0 / 0 / 27 | 11% | 15% (6.3) | 12 |
| | B: Gaussian, slope | −0.35 | 1.64 | 26% | −0.24 | 0 / 0 / 27 | 0% | 59% (4.6) | 33 |
| | D: count, level | −0.08 | 1.23 | 19% | 0.29 | 0 / 4 / 23 | 37% | 100% (16.2) | 44 |
| | E: count, slope | −0.33 | 1.28 | 19% | −0.22 | 0 / 3 / 24 | 78% | 100% (16.0) | 222 |
| `email_sessions` | A | 0.15 | 1.26 | 11% | 0.32 | 0 / 3 / 24 | 11% | 15% (8.9) | 12 |
| | B | −0.07 | 1.39 | 11% | −0.10 | 0 / 3 / 24 | 0% | 81% (3.7) | 32 |
| | D | 0.13 | 1.07 | 7% | 0.33 | 7 / 9 / 11 | 22% | 96% (10.2) | 60 |
| | E | −0.09 | 1.09 | 7% | −0.11 | 3 / 12 / 12 | 78% | 96% (31.4) | 222 |

The means on these 27 windows are aliased by the week-to-week correlation
described further down; the differences between arms are not. The two changes add
here exactly as in §6.3: the likelihood moves the spread and not the mean
(D − A: under 0.03 in z), the slope moves the mean and not the spread (E − D:
−0.22 and −0.25 at 7 days, −0.44 and −0.50 at 28, the same shift the
Gaussian slope gives). What does not carry over is the cost. A daily node
with both samples two latent walks of up to 700 steps: 78% of fits fall
under 400 effective samples and each takes 222 seconds on the loaded
machine, four to five times the count likelihood alone and eighteen times
the default.

**Structural zeros.** `discounted_orders` is the question White Cube could
not ask: a count that is zero 97% of the time and 30 to 300 a day inside a
promotion. Three clean 7-day windows touch a promotion (the last day of
Black Friday 2024, the first three days of Black Friday 2025, and its last
day); the other 51 are all zeros. The negative binomial was fitted on every
second window, which holds the first two of those three.

| | Gaussian, level | Negative binomial, level |
|---|---|---|
| Posterior predictive check, 27 fits | 0 ok / 27 severe | 27 ok / 0 severe |
| `suspect` | 100% | 0% |
| Predictive sd of a 7-day mean, all-zero weeks (median) | 14.7 orders a day | 1.5 orders a day |
| \|z\| in an all-zero week (median, largest) | 0.01, 0.04 | 0.10, 0.54 |
| z in the week Black Friday 2025 starts (126 a day above expected) | 9.5 | 154 |
| z in the week Black Friday 2024 ends (one promotion day, then zeros) | −9.6 | 0.2 |
| Fit seconds (median, four processes, loaded machine) | 9 | 163 |

- **The plain negative binomial passes its predictive check on this series,
  in every fit.** It does so by sitting at a level near zero with a large
  overdispersion, which generates the zeros and the occasional burst the
  history contains. A zero-inflated family is not needed to pass the check.
- **Passing the check is not the same as being a usable expectation.** Both
  models say a promotion is impossible: Gaussian z 9.5, negative binomial z
  154. The promotion is a calendar decision. No likelihood can expect it,
  zero-inflated or otherwise; it is a declared input (an intervention, or a
  parent carrying the promotion calendar), and Alpenglow's tree has neither.
  What the negative binomial does fix is the way out: the Gaussian level
  stays up through a promotion's last day and scores the return to zero at
  −10 (−9.6 in 2024, −11.0 in 2025), where the count model scores the 2024
  one at 0.2.
- **Under the Gaussian the score on such a node means nothing.** The
  predictive interval for a week of zeros is ±15 orders a day around zero,
  half of it below zero, so every quiet week scores 0.01 and every switch
  scores 10. `likelihood_warnings` and a `severe` check already say so on
  every fit. `discounts`, the currency twin, behaves the same and has no
  alternative: `mean_scaled_gaussian` refuses a series that is half zeros.
- `z` and the PIT are themselves poor summaries here. With a point mass at
  zero the PIT of a zero week is 0.98 or 0.00 depending on whether any draw
  is exactly zero, so the KS column for this node is not evidence of
  anything.

**What this company breaks in the benchmark.**

- **Three of the six planted anomalies cannot be seen at node level, and the
  ground truth says otherwise.** `affected_metrics` lists `units` for the
  tees slump, but the generator moves demand between categories: tees fall
  30% for two weeks and total units and units per order do not move (3,594
  and 3,743 units in the two slump weeks against 3,510 and 3,666 before).
  The two-day shorts spike is the same. The checkout bug takes about 40% off
  conversion in paid-search mobile, 8% of sessions, which is about 3% off
  the node. So "the first tree metric in `affected_metrics`" is a root in name
  only for half the list, and a null of `z ≈ 0` there is correct.
- **A lag between the anomaly's dates and the metric's.** The jacket recall
  is dated from the orders (2024-10-07); `return_rate` counts returns by
  request date, about two weeks later. Scored from its declared onset the
  7-day z is 0.03. The benchmark's onset has to be the date the root metric
  moves, which the ground truth does not carry.
- **Weekly windows are not independent.** Under a correct model one-step
  errors are uncorrelated. Here the 7-day z of consecutive weeks correlates
  at −0.44 (`units`), −0.40 (`aov`, `marketing_spend`), −0.33
  (`direct_sessions`), −0.19 to −0.25 (the other session counts), over 49
  adjacent pairs: the level chases one week's noise and the next week pays
  it back. Two consequences. A node's mean z over every second week is not
  its mean z (on `units`, −0.31 on even weeks and +0.31 on odd ones), so the
  27-window arms below are read as paired differences only. And the
  standard error of a mean z is smaller than `sd / √n` suggests, while a
  count of `|z| > 2` is not a count of independent events. §6.1 to §6.3 did
  not check this on White Cube.
- **A clean window is not a quiet one.** Black Friday is in no ground-truth
  entry and is in the data: the week of 2025-11-24 scores 2.7 and 4.2 on
  organic and email sessions, and the week after −1.9 and −3.4. Promotions
  and a 40% holiday effect are business as usual that the tree does not
  declare.
- **Under two years of history.** Every fit has fewer than 730 daily periods,
  so the engine's own rule calls the declared annual seasonality
  unidentifiable in every one of them and fits it anyway (as it should; it
  warns). The earliest 8 windows have under a year of it.
- **A stock that is a running total** has almost no noise of its own. The
  score on it is a test of the trend and nothing else.

**The planted roots.** z at the anomaly's first period, and how many of the
same node's clean windows are as extreme. Arm A is the default; arm E is the
declared likelihood plus the slope by role, which changes two of these
nodes: `marketing_spend` (Gaussian with a slope; its clean windows are arm
B's 54) and `units` (negative binomial; arm D's 27).

| Root | Anomaly | From | Horizon | Arm A | Arm E |
|---|---|---|---|---|---|
| `units` | items per order −45%, 12 days | 2025-04-07 | 7 | −21.8 (0/54) | −22.2 (0/27) |
| | | | 28 | −10.0 (0/43) | −12.6 (0/22) |
| `aov` | the same | 2025-04-07 | 7 | −13.3 (0/54) | same |
| | | | 28 | −8.9 (0/43) | same |
| `marketing_spend` | paid social spend halved, 3 weeks | 2025-02-03 | 7 | −6.2 (0/54) | −6.3 (0/54) |
| | | | 28 | −3.7 (0/43) | −3.6 (0/43) |
| `return_rate` | jacket returns ×2.2, 5 weeks | 2024-10-07 (as dated) | 7 | 0.03 (51/54) | same |
| | | | 28 | 1.04 (1/43) | same |
| | | 2024-10-21 (returns arrive) | 7 | 1.21 (1/54) | same |
| | | | 28 | 1.67 (0/43) | same |
| `conversion_rate` | paid-search mobile checkout bug, 12 days | 2024-09-12 | 7 | −0.57 (20/54) | same |
| | | | 28 | 0.00 (42/43) | same |
| `units` | tees demand −30%, 2 weeks | 2025-05-05 | 7 | −0.30 (44/54) | −0.18 (24/27) |
| | | | 28 | −0.33 (24/43) | −0.28 (12/22) |
| `aov` | the same | 2025-05-05 | 7 | 0.77 (15/54) | same |
| `units` | shorts spike, 2 days | 2025-12-25 | 7 | 0.41 (39/54) | 0.47 (16/27) |

The three that move the node are found under either model: two at 6 to 22
sds, and the recall at 1.2 to 1.7, which is weak as a raw z and clear against
the node's own clean windows (0 or 1 as extreme), because `return_rate`'s
interval is twice too wide. That is §6.1's argument for scoring against the
node's own backtest, repeated. The three that do not move the node score as
noise, correctly. Neither model change helps or hurts a planted root here.

**Story-style ranking** (`run_story`), each anomaly over its own span, rank
of the planted root (1 is first):

| Story | Target | Truth | Innovation contribution | Innovation z | Engine `ranked_causes` | Naive relative change |
|---|---|---|---|---|---|---|
| checkout bug | `gross_revenue` | `conversion_rate` | 1 | 3 | 10 | 11 |
| recall | `net_revenue` | `return_rate` | 3 | 4 | 15 | 17 |
| budget cut | `gross_revenue` | `marketing_spend` | 1 | 3 | 7 | 3 |
| items per order | `net_revenue` | `aov`, `units` | 1 | 2 | 2 | 2 |
| tees slump | `net_revenue` | `aov`, `units` | 1 | 7 | 2 | 4 |

With the slope by role forced on (`--trend role`), the recall's root moves to
2 by contribution and 3 by z, the checkout bug's to 5 by z, and the rest are
unchanged.

- **The innovation contribution puts a planted root first in four of five,
  the engine's ranking in none.** The engine's top three are intermediate
  nodes again (`orders`, `converting_sessions`, `gross_revenue`), §3's chain
  property on a deeper tree.
- **Two of those four firsts are not evidence.** The checkout bug's root
  ranks first with z −0.71 and the tees slump's with z 0.53: a contribution
  with no evidence behind it, which is §4.3's warning. They happen to be
  right.
- **The miscalibrated nodes are the false roots.** In the recall story
  `customer_base` ranks first at z 6.9, which is the level forecast's bias
  on a stock and nothing the business did; with a slope it drops out and the
  root moves up. In the budget story `returning_customer_orders` ranks
  second at z 6.0, which is the Gaussian on a count that has grown sevenfold.
  The calibration errors measured above are what pollutes a ranking.
- The recall's root is beaten by `refunds` (z 4.7), a child of the returned
  units. Jackets cost more than the average unit, so the recall raises the
  refund per returned unit as well as the number returned. `refunds` is a
  second place where the same anomaly enters, and the ground truth lists it.

**Does `ppc_status` still find the nodes whose z is off?** Under the default
model, 7 days, all 54 windows:

| | Windows | mean z | sd z about node means | \|z\|>2 |
|---|---|---|---|---|
| PPC `ok` | 198 | 0.68 | 0.81 | 29% |
| PPC `ok`, without the stock | 144 | −0.09 | 0.91 | 3% |
| PPC `moderate` | 102 | 0.00 | 1.00 | 4% |
| PPC `severe`, without the two promotion series | 348 | 0.06 | 1.22 | 11% |

Leaving the stock aside the order is White Cube's (0.94, 1.12, 1.44 there)
and flatter. Node by node it is a poor gate here, in both directions:

- **It passes the worst node.** `customer_base` is `ok` in 54 of 54 fits and
  `suspect` in none, with 98% of clean weeks beyond 2. With the stock in,
  `ok` is the worst class of the three. White Cube's stock did the same.
- **It fails every seasonal parentless flow, calibrated or not.** All four
  are `severe` and `suspect` in 96% to 100% of fits. `marketing_spend` among
  them has mean z −0.04, sd 0.99 and 6% beyond 2. The check fails on `min`,
  `max`, `resid_max` and `resid_acf1`: the series peaks at 1.4 times its
  level on holidays, never goes as low as a symmetric noise model says it
  should, and its daily noise is autocorrelated. Those are true statements
  about the shape of the series and say little about the weekly mean of an
  ordinary week. A declared likelihood does not clear it
  (`organic_sessions` is `severe` in 23 of 27 fits as a count).
- **It fails a rate for being conservative.** `return_rate` is `severe` in 51
  of 54 with sd z 0.47.
- **It does catch the one flow that is badly over-confident**
  (`returning_customer_orders`, 54 of 54 `severe`) and both promotion
  series, and it passes the three nodes that are fine (`paid_sessions`,
  `refunds`, `aov`).

So 61% of all default fits on this tree are `suspect` (54% without the two
promotion series), and the flag separates sd 1.2 from sd 0.9. It cannot gate evidence on this company as
§6.1 proposed. The per-node backtest can: it is what shows the stock, and
what shows that spend is fine.

**Fit time**, one fit at a time on a machine that had gone quiet, seconds,
for the window starting 2026-05-04 (702 daily periods of history, 100 weekly).
Each configuration ran in its own process with a fresh compile cache and
fitted an earlier window first, so compilation is not in these numbers.

| Node | Gaussian, level | Gaussian, slope 0.001 | declared, level | declared, slope 0.001 |
|---|---|---|---|---|
| `organic_sessions` (count) | 10.2 | 25.9 | 37.5 | 195.7 |
| `marketing_spend` (mean-scaled) | 12.3 | 24.2 | 59.3 | |
| `customer_base` | 10.0 | 19.2 | | |
| `paid_sessions` (count) | 10.6 | | 46.1 | |
| `returning_customer_orders` (count) | 18.2 | | 76.4 | |
| `units` (count) | 9.9 | | 88.5 | |
| `refunds` (week, mean-scaled) | 3.1 | | 6.5 | |
| `conversion_rate` | 8.8 | | | |

The slope doubles a Gaussian fit, as in §6.2 and §6.3. A count likelihood on
a daily node costs four to nine times the default, and both together twenty
times.

**The five White Cube claims, on this company.**

| Claim | Here |
|---|---|
| 1a. Rates are conservative under the default | Holds. sd z 0.47 to 0.77. |
| 1b. Flows are over-confident (sd about 1.5) | Weaker. 0.99 to 1.30 on seven of eight flows; 1.85 on the one that grew sevenfold. The over-confidence follows growth in the level: the four parentless flows grew 40% to 60% where White Cube's grew 2 to 13 times, and the two that tripled here have a parent that carries it. |
| 1c. Parentless flows and stocks are biased upward | Holds for the stock (2.7 at 7 days, 5.6 at 28). Fails for the flows as stated: 0.10 and 0.31 pooled. The bias exists in the half of the year the business is climbing (0.28 and 0.62) and not in the half it is falling. |
| 1d. Nodes with a learned parent, and rates, are not biased | Holds. −0.11 and 0.05. |
| 2a. A slope by role removes most of the bias | Holds for the stock, which then over-shoots (−0.67 at 7 days). On the flows there was little to remove; the slope subtracts the same amount in every season and trades a bias on the way up for one on the way down. |
| 2b. The slope harms rates | Holds for a rate with drift in its history (`return_rate`: −0.5 at 28 days, and the planted recall goes from 0 of 22 clean windows as extreme to 12 of 22). The two flat rates are unaffected. |
| 2c. `slope_sigma` 0.01 is out; 0.001 against 0.003 is undecided | 0.01 not run. 0.001 and 0.003 are indistinguishable on the Gaussian path. Still undecided. |
| 3a. A count likelihood fixes count flows | Holds, with the same sampler cost. Large where the count grew a lot, small elsewhere. |
| 3b. `mean_scaled_gaussian` fixes some currency flows | Fails here. Both currency flows were calibrated under the default and it made them conservative and hard to sample. |
| 3c. Nothing fits a currency flow that is half zeros, or noise proportional to the level | Half zeros: holds (`discounts` has no option). Proportional noise: the counts that have it are served by the negative binomial; the currency flow that has it (`marketing_spend`) is calibrated under the default anyway, so this company cannot show the gap. |
| 3d. Structural zeros | Tested for the first time. The plain negative binomial passes its check; see above for why that is not enough. |
| 4. The stock with a slope is over-confident while passing its check | The spread does not repeat (sd 1.02 and 1.09 against 1.3). The blindness does: the check passes in every fit under both trends. |
| 5. `ppc_status` / `suspect` flags most of the badly calibrated nodes | Half holds. It catches the over-confident flow and the promotion series, misses the stock, and fires on four calibrated or mildly over-confident seasonal flows and a conservative rate. |

**What this means for the two open defaults** (a reading, not a decision).

- *The trend by role.* A rule keyed on role alone is right for `kind: stock`
  on both companies and not for parentless flows on this one. On the five
  flows it touches here it turns a 7-day bias of +0.1 into −0.1, raises sd z
  by a tenth, triples the fit time and adds divergences; on one of them it
  does nothing at all. What separates the nodes the slope helped from the
  ones it did not is in the model-free table (`describe`, run on both
  companies): the six White Cube sources and this stock grew 2.3 to 13
  times over their history, last third over first; the five flows here
  grew 1.4 to 1.8 times, less than they swing within a year. A data check
  of that kind (a slope where net growth over the history exceeds the
  within-year swing, or simply exceeds a factor of two), or more directly
  the node's own backtest bias under the level, would have chosen as the
  null runs did on all 12 parentless nodes across the two companies. That
  rule was read off these results and has not been tested as a rule; a
  declining business and a stock that can fall are still unmeasured.
- *`slope_sigma`.* No evidence from this company either way. On the
  marginalized path the two values give the same z to within 0.03 and the
  same sampler health; 0.003 is up to 4% wider at 28 days.
- *The likelihood.* Declaring one is still a per-node decision that the null
  run, not the model-free diagnosis, has to make: of the eight nodes the
  diagnosis picked here, one gained a great deal, three a little, two did
  not need it, two were made worse, and every daily one became at least
  four times slower.

**Limits.** One more synthetic company, from the same generator as the
first. Arms C, D and E ran on every second window, whose means are aliased;
only their paired differences are used. Arm E covers two nodes. The 28-day
windows overlap. The tree was written for this study, and its choices
(annual seasonality on the parentless flows, a running-total stock, the
split of sessions) shape which claims it can test. No flat or declining
flow, no stock that can fall, and no off-season exist in this data; the
other two verticals (`b2b_services`, `cpg_wholesale`) were not looked at.

## 7. Open questions

1. **Calibrating the evidence.** §6.1 answers the first half: the likelihood
   (flows) and the flat forecast (growth bias) are the causes, and rates are
   too wide. Still open: whether declaring past steps (S24) or down-weighting
   old history tightens the rates. §6.4 is the second vertical: the backtest
   holds there, and `ppc_status` as a gate does not.
2. **Fit cost on a wide tree.** If every source node needs a fit, the B2B tree
   needs roughly ten times the fits it does today. The model is linear-Gaussian
   given its two variance parameters, so with Normal priors the coefficients
   could be integrated analytically inside the existing Kalman filter and the
   variances handled on a grid. Unmeasured, and it conflicts with S20's count
   likelihoods.
3. **Multi-path lags.** A node reached along two paths with different total
   lags has two windows. Hull, or one innovation per path?
4. **Lagged formula edges and partial periods** at mixed grain: the prototype
   relies on `fit_frame`'s inner join and has not been checked on a month-grain
   tree.
5. **What the UI shows.** Departure, evidence and contribution are three
   numbers where the current table has one. Rule 5 applies.

## 8. Proposed roadmap changes

For Devon's decision; not applied.

| Row | Proposal |
|---|---|
| New C row | Lagged ancestors are fitted on their own analysis window (§3). Per-node `fit_end` from the shifted window. |
| 3.4, S16 | Re-scope as this design and move out of Horizon 3. S16 is delivered by §4.3's forward-simulated level. |
| S12 | Replace "make the ranking visibly a heuristic" with replacing the ranking by evidence plus contribution. |
| New S row | A localization benchmark: rank of the planted root and segment across every fake_companies vertical, with a null run, in CI. Ahead of S5 and S17. |
| S21 | Promote from optional. An unfittable source is an unassessed node under this design. |
| S20 | Promote. Measured in §6.1: flows under the Gaussian likelihood exceed `\|z\| > 2` in 15% of clean weeks. |
| S8 | Promote, and reconsider "opt-in". §6.1 shows the flat forecast biased upward on every parentless flow and the stock of a growing business, and not on nodes with a learned parent, which suggests a default by node role rather than a global switch. |
| 3.10 | Add a per-node backtest to the warm pass, as the null distribution evidence is scored against (§6.1). |
| 3.1, 3.10 | Build detection on §4.6, so anomaly flagging, suggested windows and RCA share one expectation. |
| 3.2 | Add slice innovations (§4.5), starting share-based. |
| S6 | Deprioritize. The predictive distribution answers the noise question the block bootstrap answers today. |
| MCP | A preflight data-health tool or skill (source freshness, last dbt run), per §4.6. |

## 9. Suggested build order

1. Repeat §6.1's null run on a second vertical, and re-measure with S20 and S8
   prototyped, to see how much of the miscalibration they remove.
2. The lagged-ancestor fit-end fix, which stands on its own.
3. Node-level innovations in `run_rca` behind a mode flag, with interval-bearing
   contributions and the evidence gate.
4. Share-based slice innovations on gated nodes.
5. Detection on the same machinery.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

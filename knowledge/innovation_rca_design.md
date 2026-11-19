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

## 7. Open questions

1. **Calibrating the evidence.** §6.1 answers the first half: the likelihood
   (flows) and the flat forecast (growth bias) are the causes, and rates are
   too wide. Still open: whether declaring past steps (S24) or down-weighting
   old history tightens the rates, and whether the per-node backtest holds on
   a second vertical.
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

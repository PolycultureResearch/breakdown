# Innovation-based root cause analysis — "versus expected", and where the surprise entered

**Status:** design plus a measured prototype; nothing in the engine has
changed. Opened 2026-11-16 from an architecture review of the RCA use case.
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

| Metric kind | Windows | mean `z` | sd `z` | `|z| > 2` |
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

# Evaluation spec: scoring breakdown against generated companies

**Status:** proposal, 2026-11-19. Nothing here is built except the prototype
benchmark it grows out of
([`benchmarks/innovation_score.py`](benchmarks/innovation_score.py)).

**Companion:** the generator side of this lives in the fake_companies repo, at
`docs/eval_worlds_spec.md`. That document says what the generator must emit;
this one says what breakdown does with it. The split is in §9.

## 1. Why this exists

breakdown's engine decisions have been made against one synthetic company at a
time. The first null calibration ran on White Cube and produced five
conclusions. The second, on Alpenglow, confirmed two, weakened one and
overturned two ([`innovation_rca_design.md`](innovation_rca_design.md) §6.4).
Each new company is still changing the answer, which means the sample is too
small to generalize from.

The generator behind those companies is deterministic by seed and builds a
two-year company in about 20 seconds. So the evaluation set does not have to be
four fixed datasets. It can be a family of companies generated on demand, with
the property under test varied on purpose.

This spec defines that family, the questions it answers, the numbers reported,
and how those numbers gate engine decisions.

## 2. Principles

1. **Vary one thing at a time.** A finding is attributable only when the worlds
   that produced it differ in one known respect.
2. **Replicate before believing.** A single seed is an anecdote. Every reported
   number carries the spread across seeds.
3. **Truth is measured, not declared.** The effect of a planted anomaly on a
   metric is taken from the generator's paired run with and without it, not
   from a hand-written list of affected metrics.
4. **Hold some worlds out.** Development worlds are for tuning. Held-out worlds
   are scored only when a decision is being made, and never tuned against.
5. **Report against a baseline.** Every engine change is shown as a difference
   from the previous scoreboard on the same worlds, with the paired spread.
6. **Simulation is not the field.** The generator's noise and structure are
   simpler than a real company's. A result here is necessary for a change, not
   sufficient. Field reports from live trees stay a separate line of evidence.
7. **Abstaining is a scored outcome.** When the root cause is off the tree or
   the data is at fault, the right answer is "not on this tree" or "check the
   data". The evaluation rewards that and penalizes a confident wrong node.

## 3. Questions the evaluation answers

Each maps to a metric in §5.

| # | Question | Engine area |
|---|---|---|
| Q1 | When nothing is planted, is a node's surprise score calibrated? | fit, trend, likelihood |
| Q2 | When something is planted at a node, does that node's score stand out from its own quiet history? | detection |
| Q3 | Is the true root ranked first among the tree's nodes? | ranking |
| Q4 | When the root is a slice of a node, is the slice found? | slicing |
| Q5 | Is the size of each node's contribution to the target's change right? | attribution |
| Q6 | When the root is off the tree, or the data is at fault, does the engine say so? | abstention, data health |
| Q7 | How small an effect can each method find? | power |
| Q8 | What does an answer cost in time and sampler health? | speed |
| Q9 | Does the engine's own fit-quality flag agree with Q1? | diagnostics |

## 4. Worlds

A **world** is one generated company: a vertical, a regime, a seed, and a set
of planted anomalies. The generator's contract for emitting these is in the
companion spec; this section says which worlds the evaluation uses and why.

### 4.1 Kinds of world

| Kind | Planted | Used for |
|---|---|---|
| **Null** | nothing | Q1, Q9, and the quiet history each node is judged against in Q2 |
| **Single** | exactly one anomaly | Q2 to Q7. One anomaly per world keeps aftermath and overlap out of the score |
| **Twin** | a single world paired with the null world of the same seed | Q5: the difference between the two is the anomaly's measured effect |
| **Story** | the scripted demo scenario, several anomalies | continuity with the demos and the guided tour; not used for calibration |

The earlier runs carved "clean windows" out of the gaps between anomalies in a
story world. Those windows contained anomaly aftermath, holidays and
promotions. Null worlds replace them.

### 4.2 What varies

| Axis | Values | Why |
|---|---|---|
| Vertical | `b2c_saas`, `retail_dtc`, `b2b_services`, `cpg_wholesale` | different tree shapes, grains and node kinds |
| Seed | 10 per cell in the full tier, 3 in the smoke tier | the spread on every number |
| Growth | as scripted, flat, declining, accelerating | the slope default was decided on a growing company and failed on a seasonal one; flat and declining have never been tested |
| History | 2 years, 3 years | every Alpenglow fit had under two years, so annual seasonality was never identifiable |
| Seasonality | as scripted, none | separates trend bias from seasonal bias |
| Anomaly shape | level shift, ramp, spike, slice-only, mix shift, off-tree, data-quality | field incidents are mostly slices and sometimes off-tree |
| Anomaly location | each driver the vertical exposes | a root at every depth of the tree |
| Anomaly magnitude | a ladder from barely visible to large | Q7 |

The full cross of these is not affordable (§7). The design is a baseline cell
per vertical plus one-axis departures from it, not a grid.

### 4.3 Trees

Each vertical has one **canonical evaluation tree**, kept with the generator
and versioned with it. The tree is a fixed input: an evaluation run does not
edit it. Changing a canonical tree changes every score that depends on it, so
it is recorded on the scoreboard as a change of its own.

The Alpenglow run had to author a tree, adding rates and a stock that the
scenario tree lacked. That made the tree an uncontrolled variable. Canonical
trees must include at least one rate, one stock, one node with a learned
parent, one formula node and one sliceable dimension, so every claim in §3 can
be tested on every vertical.

### 4.4 Development and held-out worlds

- **Development:** seeds 0 to 4 of every cell. White Cube and Alpenglow story
  worlds are development, since conclusions have already been drawn from them.
- **Held-out:** seeds 5 to 9. Scored when a default is being decided or a
  release is cut. A held-out result is reported once per decision; if a change
  is reworked after seeing it, the next decision uses fresh seeds (10 onward).

Seeds are cheap, so the held-out set is renewable. That is the main reason to
prefer generated worlds over fixed datasets.

## 5. Metrics

Every metric is computed per node or per world, then pooled by **node role**:
rate, flow with a learned parent, flow with no parents, stock, formula. The
earlier runs showed role predicts behaviour better than any other grouping.

### 5.1 Calibration (Q1, Q9)

On null worlds, for each non-formula node, at 1, 7 and 28 day horizons, on
windows spaced so consecutive windows do not overlap:

- mean and standard deviation of the score `z`
- share of windows with `|z| > 2` (4.6% when calibrated), and the same after
  removing the node's own mean
- distance of the probability integral transform from uniform
- lag-1 correlation of `z` between consecutive windows

The last item is new. On Alpenglow the weekly windows were negatively
correlated (−0.19 to −0.44), so counting them as independent overstated the
evidence. Spread is taken across seeds, where worlds are independent, not
across windows.

For Q9, the same table split by the engine's `ppc_status` and `fit_quality`,
with the share of badly calibrated nodes the flag catches and the share of
well-calibrated nodes it wrongly flags.

### 5.2 Detection (Q2, Q7)

On single worlds, for the node the anomaly is planted on:

- its `z` at the anomaly's onset, at each horizon
- its **empirical tail**: the share of that node's null-world windows, same
  seed and horizon, at least as extreme
- across the magnitude ladder, the smallest magnitude at which the tail is
  below 5% in at least 80% of seeds

Onset is the date the node's own series first moves, taken from the twin
difference, not the date the driver was changed. The Alpenglow recall was dated
by order while the return rate moved two weeks later.

### 5.3 Localization (Q3, Q4)

On single worlds, with the target set to the top metric the anomaly reaches:

- rank of the true root node, for each method: innovation contribution,
  innovation evidence, the engine's `ranked_causes`, and a naive
  relative-change ranking
- top-1 and top-3 rates, and mean reciprocal rank, across seeds
- for slice anomalies: whether the true dimension and value are the top slice
  of the true node

A first place carries a flag for whether it had evidence behind it. On
Alpenglow two of four first places had `|z|` under 1, which is a right answer
the reader would have no reason to trust.

### 5.4 Attribution (Q5)

On twin worlds, for each node on the path from root to target:

- the measured effect: the twin difference in the node's window value
- the engine's attributed contribution, with its credible interval
- error as a share of the target's measured change
- coverage: how often the interval contains the measured effect

Formula edges are exact identities, so their share of the error should be zero
and is reported separately from learned edges.

### 5.5 Abstention and data health (Q6)

- **Off-tree worlds:** share of runs where the engine reports the change as
  unexplained by the tree, against the share where it names a tree node with
  evidence.
- **Data-quality worlds:** whether the affected source is flagged before
  root-cause analysis runs, and whether a root-cause answer is withheld.
- **Null worlds:** share of windows where the engine names any root with
  evidence. This is the false-alarm rate, and it is the number a stakeholder
  feels most.

### 5.6 Cost (Q8)

Per fit and per analysis: wall seconds on a quiet machine, share of fits with
bulk effective sample size under 400, share with a divergence, share marked
`suspect`. Timings taken on a loaded machine are reported as ratios only.

## 6. Scoreboard

One table per tier, committed with the code change that produced it, under
`knowledge/eval/`:

- a summary row per question and node role, with the value, the spread across
  seeds, and the difference from the previous scoreboard
- per-vertical breakdowns below it
- the generator version, canonical tree versions, engine commit and the list of
  world identifiers

A change to engine defaults (trend kind, likelihood, sampler settings, ranking
method) is proposed with a scoreboard diff on development worlds and decided
with one on held-out worlds. The roadmap row for the change links the diff.

The raw rows are not committed. They are reproducible from the world
identifiers.

## 7. Cost and tiers

A Gaussian fit takes 3 to 10 seconds and a non-Gaussian or sloped fit up to
100. The Alpenglow run, one company and five arms, took several hours on a
shared machine. So the evaluation is tiered.

| Tier | Worlds | When | Budget |
|---|---|---|---|
| **Smoke** | 1 vertical in rotation, 3 seeds, null plus 3 single worlds, 7-day horizon | every engine pull request, on request | under 15 minutes |
| **Standard** | 4 verticals, 5 development seeds, baseline cell, all question groups | before a default is proposed | a few hours |
| **Full** | standard plus held-out seeds and the one-axis departures | before a default is decided, and per release | overnight |

Rules that keep it affordable:

- Fits are cached by world, node, fit end and model configuration, so a node
  whose configuration did not change between two arms is fitted once.
- Each process has its own compile directory (roadmap C65).
- Arms are compared on the same windows. A subset of windows is never an arm
  of its own, because of the correlation in §5.1.

## 8. Known limits

- **The generator's noise is the model's own.** Entities are drawn as Poisson
  and binomial, so "a negative binomial fits counts" is partly a finding about
  the generator. The companion spec adds overdispersion and heavy tails as
  regime options.
- **The trees are ours.** A real tree has wrong and missing edges. A later
  regime should score the engine on a deliberately mis-specified tree.
- **Four verticals is four structures.** Seeds and regimes multiply the
  realizations, not the kinds of business.
- **Twin differences are realized, not expected.** Two runs of one seed
  diverge in their random draws once an anomaly changes a rate. The measured
  effect is the mean twin difference across seeds; the companion spec reports
  how tight that is.
- **No real data.** Field reports are tracked separately and are not replaced
  by this.

## 9. What lives where

| Concern | Repo | Reason |
|---|---|---|
| Generating null, single and twin worlds; seed and regime sweeps | fake_companies | generator mechanics, useful to any consumer |
| Measured ground truth; root labels in metric and slice terms; calendars | fake_companies | truth about the data belongs with the data |
| Canonical evaluation trees | fake_companies, under `examples/` | must change with the dbt models they bind to |
| World-set manifest format | fake_companies defines it; breakdown reads it | one contract, written by the producer |
| Questions, metrics, tiers, splits, scoreboard, decision rule | breakdown (this document) | they are about the engine |
| The runner and scoring code | breakdown, `knowledge/benchmarks/` to start | it calls engine internals |
| Detection scoring for Tremor | later, in Tremor or fake_companies | not designed here; the world sets are meant to serve it unchanged |

breakdown reads a world set through its manifest and never reaches into the
generator's internals. If the evaluation needs something the manifest lacks,
the manifest grows.

## 10. Build order

1. **Generator:** null, single and twin worlds with seed sweeps and measured
   ground truth (companion spec, phase 1).
2. **Runner:** read a world-set manifest; replace the script's hard-coded
   company and clean-gap logic; add the fit cache and non-overlapping windows.
3. **Baseline scoreboard:** current defaults, standard tier. This is the
   reference every later change is measured against.
4. **Open decisions, each as a scoreboard diff:** innovation ranking against
   `ranked_causes`; the trend slope for stocks; the count likelihood; the slope
   prior width.
5. **Generator, phase 2:** calendars, regime options, off-tree and mix-shift
   anomalies, noise options. Then the abstention and power metrics.
6. **Smoke tier in the contributor loop,** alongside the `read-the-numbers`
   check.

Steps 1 and 2 can proceed in parallel once the manifest format is fixed.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

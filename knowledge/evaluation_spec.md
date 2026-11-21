# Evaluation spec: scoring breakdown against generated companies

**Status:** proposal, 2026-11-19. Built so far: the prototype benchmark it
grows out of ([`benchmarks/innovation_score.py`](benchmarks/innovation_score.py)),
and the runner of §10 step 2
([`benchmarks/evaluate.py`](benchmarks/evaluate.py), §11), with one smoke-tier
scoreboard on a tree that is not yet canonical
([`eval/`](eval/)). Attribution (§5.4), abstention (§5.5), the magnitude
ladder and the slice check are not built. §12 records what building the
runner changed or pinned down in this document.

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

Canonical trees do not exist yet (generator phase 2). Until they do the
runner takes the tree as a parameter and stamps its path and hash on the
scoreboard, marked as not canonical. A tree binds to a world set by metric
name: a node is the metric its `source` names, or what a small bindings file
says (a slice of a metric, a running total). A node the world set cannot
serve is reported by name and the run refuses, unless it is told to run
without those nodes, which the scoreboard records as a change to the tree
(§12).

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

The windows of a world sit on one grid of Monday starts, evenly spaced by a
whole number of weeks, the first one 182 days into the data. Every node and
every arm is scored on the same grid. A horizon longer than the spacing is
scored on every k-th start, so windows at one horizon never overlap.
"Consecutive" means neighbours on the grid; the scoreboard states the spacing.

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

The generator reports two dates per metric. `first_diff` is the first day the
twins differ at all, and `onset` is the first day the difference clears the
metric's noise floor. The runner uses `first_diff` when it is not before the
declared start: the twins were then identical until the anomaly reached this
metric, so the date is exact. That matters for a ramp, whose `onset` comes
two weeks into it, after a fit ending there has absorbed the start. When
`first_diff` is earlier (most of the generator's entity builders re-deal days
long before the anomaly) the runner uses `onset`, and failing both the
declared start. Every detection row says which it used.

The empirical tail has the resolution of the null windows behind it: with
eight windows per node the smallest non-zero tail is 12.5%, so "below 5%"
means "no quiet window was as extreme". The scoreboard prints the count.

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
the reader would have no reason to trust. The bar is `|z|` of 2.

**The true root, in tree terms.** The generator labels a root as a metric and
a slice. The runner maps it to the node bound to that metric at that slice,
else the node bound to the metric's total. If no node carries the metric the
world is **off-tree** for that tree and is counted, not ranked (it is an input
to §5.5). One case needs a rule: the generator roots some anomalies at a
count, because that is what its semantic layer can slice (a spike in the churn
rate is rooted at `churned_subscriptions`). In a tree that count is usually an
exact identity of a stock and a rate, and an identity cannot be a root. The
true root is then the one parent of the formula, among those the measured
effects say moved, with the largest relative change. The stock moves as well,
because churned subscriptions leave it, but by a small fraction of what the
rate moves by.

**The target and its window.** The target is the deepest node downstream of
the root whose metric the measured effects say moved. Its window starts in
the period holding the root's onset and runs to the anomaly's end, at most 28
days, in whole periods of the target's grain.

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
  whose configuration did not change between two arms is fitted once. What is
  kept for every fit is its scored rows, which are small. The trace is kept
  only for single worlds, where a fit has several consumers, in a directory
  capped in bytes (§12).
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

Step 2 is built (§11). Step 3 waits on canonical trees and the generator's
ten-seed sets.

## 11. How to run

The runner is [`benchmarks/evaluate.py`](benchmarks/evaluate.py); its code is
the `evalrunner` package beside it. It needs a world set on disk and nothing
else from the generator: no dbt, no warehouse, no fake_companies checkout.

**Build a world set** (in the fake_companies repo; `--no-raw` keeps only what
the runner reads):

```bash
uv run fake-companies worlds --config configs/white_cube_b2c_app.yaml \
    --seeds 0-2 --null --singles all --no-raw --out /path/to/worlds/white_cube
```

**Run a tier.** Output goes to a run directory outside the repo:

```bash
uv run python knowledge/benchmarks/evaluate.py run \
    --worlds /path/to/worlds/white_cube --tree demo/white_cube_tree.yml \
    --tier smoke --out /path/to/runs/white_cube_smoke
```

It prints the plan (which nodes are served, how each single world's root maps
to the tree, how many fits), runs it in four worker processes, and writes
`scoreboard.json` and `scoreboard.md` into the run directory. `--dry-run`
stops after the plan. Running the same command again resumes: finished jobs
are skipped, so an interrupted run loses at most the jobs in flight.

| Option | Meaning |
|---|---|
| `--tier smoke\|standard\|full\|attribution` | Seeds, regimes, horizons, windows and single worlds as in §7 (table below) |
| `--seeds 0-2` | Override the tier's seeds. Seeds from 5 up are refused without `--held-out` |
| `--held-out` | Also score held-out seeds. The scoreboard says so in its header |
| `--regimes`, `--horizons`, `--singles` | Comma-separated overrides |
| `--spacing-days N` or `--max-windows N` | The null-window grid: a fixed spacing, or at most N windows per world |
| `--bindings FILE` | Node-to-series bindings. Defaults to `evalrunner/bindings/<tree file stem>.yml` when that exists |
| `--drop-unserved` | Run without the nodes the world set cannot serve |
| `--shards N` | Worker processes, default 4. Each fit also runs four chains |
| `--trace-cache-gb G` | Byte cap on the trace cache, default 4; 0 keeps no traces |

What each tier selects:

| Tier | Seeds | Regimes | Horizons | Null windows per world | Single worlds |
|---|---|---|---|---|---|
| smoke | 0 to 2 | baseline | 7 | at most 8, evenly spaced | 3 anomalies, spread over the depths their roots sit at |
| standard | 0 to 4 | baseline | 1, 7, 28 | every 28 days | all |
| full | 0 to 4, plus 5 to 9 with `--held-out` | all in the set | 1, 7, 28 | every 28 days | all |
| attribution | 0 to 4 | baseline | 7 | none | all, or those `--singles` names: for a ten-seed set, the anomalies whose sizes it can ground |

**Trees.** `demo/white_cube_tree.yml` (b2c_saas) binds by metric name and
needs no bindings file. `demo/alpenglow_tree.yml` (retail_dtc) uses
`evalrunner/bindings/alpenglow_tree.yml` and needs `--drop-unserved`: its
`discounted_orders` node has no metric in the world set (rechecked at
generator `b51d70e`: 18 nodes served, that one dropped; the generator lists
the metric under its phase 2).

**Score, publish, compare.**

```bash
# rebuild the scoreboard from a run's rows, and publish a copy into the repo
uv run python knowledge/benchmarks/evaluate.py score /path/to/runs/white_cube_smoke \
    --publish knowledge/eval/2026-11-20_smoke_white_cube_demo_tree
# several verticals as one scoreboard, with per-vertical tables
uv run python knowledge/benchmarks/evaluate.py score RUN_A RUN_B --publish knowledge/eval/NAME
# paired differences between two scoreboards, on the worlds they share
uv run python knowledge/benchmarks/evaluate.py diff before.json after.json
```

**What a run directory holds.**

| Path | What |
|---|---|
| `run.json` | The stamp (engine commit, generator version, manifest `config_sha256`, tree path and hashes, tier, seeds, world ids) and the ordered job list |
| `rows/<type>.<shard>.jsonl` | Raw rows: `calibration`, `detection`, `localization`, `nodes`, `fits`, `analysis`, `skipped`, `done`. Not committed |
| `traces/` | Pickled fits of single worlds, least recently used evicted over the byte cap |
| `logs/`, `invocations.jsonl` | Worker output, and the wall time of each invocation |
| `scoreboard.json`, `scoreboard.md` | The scoreboard |

**The scoreboard file.** `rows` is a list with one entry per question, metric,
horizon, ranking method, role and vertical: `value` (mean over seeds),
`spread` (standard deviation across seeds), `n_seeds`, `n`, and `units`, the
metric's value in each world. `diff` pairs two scoreboards on `units`.

**Tests.** `tests/test_eval_runner.py`, on hand-written world sets
(`tests/eval_worlds_fixture.py`). One test fits; the rest run in two seconds.

## 12. Decisions made while building the runner

Where this document was silent or turned out wrong, the smallest sensible
choice was made. Each is recorded here so the spec and the code agree.

**A world reaches the engine through the provider boundary.** The loader is a
fetcher (`evalrunner/loader.py`, a `BaseDataFetcher`) that answers each node
from the world's daily series file, handed to the same `fetch_all_metrics`
the server calls. The data therefore goes through the alignment every
provider goes through, derived nodes are derived by the engine's own code,
and the identity checks run. It is kept with the benchmark and not in
`breakdown/`: it reads one generator's file format, and "this node cannot be
served" is a result it reports, not a connection someone fixes.

**Coarser grains are rebuilt from days by the manifest's rule, and refused
when they cannot be.** Every rule the loader applies is one the manifest
states (the generator's contract since its commit `83640ca`). A sum adds the
days of each whole week; a stock takes the last day; a ratio aggregates its
`numerator` and `denominator` columns each by the side's rule in
`metrics[].input_time_aggregation`, then divides; an average (`has_sides`)
sums its sum and its row count, then divides; a derived metric aggregates
each input by its own rule (checked against `input_time_aggregation`) and
evaluates `metrics[].formula`, the expression as a tree. The SQL `expr` is
never parsed. A distinct count and a rolling window are served at day grain
and refused at a coarser one, and so is a ratio or derived metric whose
sides' rules the manifest does not state (a set built before `83640ca`).
Nothing is approximated.

**A ratio aggregates each side by that side's own rule.** `customer_churn_rate`
is churned subscriptions over active subscriptions, and active subscriptions
is a stock. Summing a stock over a week gives subscription-days, and the
tree's weekly identity (`churned = active × rate`) then fails by a factor of
seven. The manifest now calls such a ratio `ratio_of_aggregates` and gives
`input_time_aggregation: {numerator: sum, denominator: end_of_period}`; a
`ratio_of_sums` whose sides are stated as anything but sums is refused.
Measured on the 2026-11-20 White Cube set (generator `b51d70e`, 33 worlds):
the rate's daily `numerator` and `denominator` equal the
`churned_subscriptions` and `active_subscriptions` series exactly, and the
weekly identity holds to 2e-16 relative in every week of every world. Summing
both sides would have given a weekly rate 6.9 times too small.

**A weekly value rebuilt from days is not always MetricFlow's weekly value.**
`active_subscriptions` is a distinct count (`measure_agg: count_distinct`).
Asked for a week, MetricFlow counts subscriptions active at any time in it;
the loader takes Sunday's count, which is what a stock node means. The two
differ by the week's cancellations, about one percent. The node is served,
and the plan and the scoreboard carry a caveat that names it.
`demo/white_cube_tree.yml` was written against MetricFlow's weekly numbers, so
on a world set its week-grain stock and churn rate are slightly different
series from the demo's. They are consistent within a world set, which is what
the scores need.

**A tree's `kind` must agree with the generator.** The engine resamples a
parent by its kind, so a node the tree calls a flow and the generator calls a
stock is refused by name.

**Bindings beyond a metric name are explicit.** A node whose `source` is not
a generator metric needs an entry in a bindings file: members of one
dimension of a metric (summed; for a ratio, both sides summed), or the
running total of a summed metric. The Alpenglow tree needs five. This is a
restatement of the tree's own SQL in the world set's terms, so it is a second
definition that can drift; canonical trees should bind by metric name only.

**Unserved nodes refuse the run by default.** `--drop-unserved` removes them
and their edges; a formula node that cannot be fetched stays and is derived
from its parents. A node some remaining formula needs cannot be dropped. The
scoreboard lists what was dropped and carries the hash of the tree actually
run.

**Root on a rate's numerator** (§5.3). The generator roots a rate anomaly at
the rate's numerator count when its semantic layer cannot slice the rate
(White Cube's churn spike at `churned_subscriptions`), and names the rate in
`root_metric_rate`. A tree holding the rate as a non-formula node is scored
at the rate (`on_tree_rate`).

**Root on a formula node**, when `root_metric_rate` does not resolve it:
scored at the formula's moved parent with the largest relative change. If no
parent moved, the label stays on the formula node and the row says so.

**Onset** (§5.2): a `first_diff` not before the declared start, then `onset`,
then the declared start.

**The target is chosen per anomaly, from the set's verdict.** One twin's
`moved` is noisy, so the same anomaly got a different target in different
seeds (White Cube's signup regression reached `trials_started` in two seeds
and `new_mrr` in the third). The set's `effects_summary.parquet` now carries
one `verdict` per regime, anomaly, metric and slice (`established_up` /
`established_down` at two-sided 1% across seeds, `open`, `exactly_zero`,
`undefined`), and a node moved when its verdict is established. A node the
summary has no row for (several slice members summed, a running total)
falls back to the twin's `moved`, and every localization row names its basis
(`set_verdict`, `twin_moved`, or `fallback_deepest_descendant` when nothing
downstream moved). The cost: a world's target depends on which seeds the set
was built with, so the verdicts used are part of a single world's job key.
With three seeds the 1% bar is a t of 9.9, so few effects are established on
a smoke set (see the smoke tier, below).

**A node bound to one slice member reads its own stats.** The twin's
`<world>.effects_summary.parquet` holds every single-dimension slice; the
ground truth's `measured_effects` holds totals only.

**The window grid** (§5.1). Smoke scores at most eight windows per null
world; standard and full score one every 28 days, which is about 20 per
two-year world and lets all three horizons share one grid without overlap.
Every weekly start would be about 78 per world: at 16 fitted nodes and five
seeds that is 6,000 fits per vertical for calibration alone.

**Spread across seeds, computed from per-world units.** Each metric is first
computed per world (a null world's statistic over its windows; a single
world's rank or score), then averaged within a seed, and the spread is the
standard deviation of the per-seed means. The scoreboard keeps the per-world
values so that two scoreboards can be compared on the worlds they share.

**Roles.** Five: rate, stock, flow with no parents, flow with a learned
parent, formula. A rate or stock with a learned parent is pooled with its
kind. Formula nodes are not calibrated (their residual is zero by
construction). A formula node appears in the `nodes` rows of a single world,
where an innovation at it would mean the identity stopped holding.

**Cost per fit is read off the null worlds.** They fit every non-formula node
on the same windows in every run, so the numbers compare across scoreboards.
A single world's fits are whichever nodes its anomaly's scope holds; that
cost is reported per analysis.

**The fit cache is two things** (§7). Scored rows are kept for every fit and
never evicted: they are what a rerun or a ranking-only arm reuses, and a
null-world fit has no other consumer. Traces are megabytes each and grow with
draws times periods, so only single worlds' fits are written, under a byte
cap with least-recently-used eviction. Evicting a trace costs a refit and
never a wrong number.

**What a cache key holds.** The world's id and effective-config hash, the
generator version, the node, the fit end, the node's definition, each
parent's grain and kind, the sampler's draws, chains and seed, and the git
tree hash of `breakdown/` (so a docs-only commit keeps the cache and any
engine change drops it).

**The smoke tier, measured** (2026-11-20, White Cube, `demo/white_cube_tree.yml`,
[`eval/2026-11-20_smoke_white_cube_demo_tree.md`](eval/2026-11-20_smoke_white_cube_demo_tree.md),
replacing the 2026-11-19 baseline; the paired difference is
[`eval/2026-11-20_smoke_white_cube_diff_vs_2026-11-19.md`](eval/2026-11-20_smoke_white_cube_diff_vs_2026-11-19.md)).
393 jobs: 384 calibration fits (16 fitted nodes, 8 windows, 3 seeds) and 9
single worlds, the three anomalies pinned with `--singles` to the ones the
first baseline ran (the default smoke pick now takes
`latam_campaign_brazil_lift_organic` in place of the signup regression,
because the set holds every anomaly). 34 process-minutes and **9 minutes 3
seconds of wall time** on four worker processes. The load average went from
3.5 to 16 during the run with no generator process alive, which is the run's
own 16 sampler processes on 12 cores; the first baseline took 13 minutes 54
seconds with another job holding the load at 20 to 87. Median fit time fell
from 5.3 to 3.5 seconds for the same reason, not because of any change here.

What moved between the two baselines, and why:

- **Calibration (Q1, Q9) and detection (Q2): identical** to three decimals.
  The null worlds and the roots' own series are the same numbers under the
  new contract: the churn rate was already rebuilt from a summed numerator
  over the stock's last day, and is now so by the manifest's statement
  rather than by inference.
- **Localization (Q3): every change is a target change** from the set's
  verdicts. The roots did not change (the churn spike's root is
  `customer_churn_rate` either way; now by `root_metric_rate`, before by the
  largest-change rule). The signup regression's target went from
  `trials_started` (seeds 0, 2) and `new_mrr` (seed 1) to `signups` in every
  seed: only `signups` is established at the set level, so its scope is three
  nodes instead of four or thirteen, and its ranks improved. The churn spike
  in seed 2 went from `churned_mrr` (four nodes) to `net_new_mrr` (twenty),
  by the fallback, and its ranks got worse. With three seeds the 1% bar is a
  t of 9.9, and the churn spike's own metrics (rate +33%, t 5.3) are `open`,
  so nothing below its root is established and every seed falls back to the
  deepest descendant. The spend ramp is unchanged. These are changes in what
  is asked, not in the engine.

Not included: building the set took 13 minutes 19 seconds (33 worlds, every
anomaly, two at a time, 11.5 GB peak). It is deterministic for a generator
version, so it should be built once and kept, not rebuilt per pull request.
Four workers, each fit running four chains, is 16 busy processes on 12 cores.

To hold the budget on a canonical tree: six null windows per world instead
of eight (96 fewer fits, about three minutes), or a quiet machine. The trace
cache held 67 fits in 366 MB (5.7 MB each on average, 11.6 MB at most), so
the 4 GB cap is not reached at smoke scale and would be at standard scale.

**One compile directory per worker process,** created for the run and removed
after it, as §7 requires. Workers are separate processes started with
`PYTENSOR_FLAGS` already set.

**Held-out seeds.** Any seed from 5 up needs `--held-out`. The scoreboard
header then names the held-out seeds scored.

**Data-quality single worlds are skipped, and off-tree ones are counted.**
Both are inputs to §5.5 and are written as rows now so abstention can be
scored from an existing run directory.

**Attribution (§5.4, Q5) is scored where the world set can ground a size.**
The generator's ten-seed study (fake_companies `docs/eval_worlds_spec.md`
§9.2, §9.5, §9.7, commit `d28b29f`) settled that a measured effect can be the
truth for an attributed size when its cross-seed verdict is established and
its cross-seed mean is known to about ±20%, and that one twin's difference
can be the truth only where the twins were identical until the anomaly.
The runner derives both from the data; it keeps no list of anomalies.

- *What is compared* (`evalrunner/attribution.py`). The engine's RCA over the
  anomaly's declared window, as the whole periods of the target that cover
  it: the window the set's cross-seed effects were measured over, the same
  in every seed, and not capped at 28 days as localization's is. On each edge `p -> c` on a
  path from the truth root to the target, the engine's contribution of `p`
  to `c`'s change with its 95% interval; the truth is `p`'s share of `c`'s
  twin difference over `c`'s effective window, by the engine's own window
  rule: the exact Shapley share per period for a formula child, all of the
  difference for a learned child (refused by name, `several_parents_moved`,
  when another parent's verdict is established too). The root: the part of
  its change the engine leaves unexplained (`root_unexplained`), or for a
  root with no parents its change itself (`root_gap`, since the engine
  attributes nothing at a terminal cause); neither has an interval.
- *Eligibility*, from the set's `effects_summary.parquet` for the child
  metric: `verdict` established, and the 95% half-width of
  `rel_change_mean` within 20% of it at the set's own seed count
  (`t · rel_change_sd / √n_seeds / |rel_change_mean|`, Student's t on n − 1
  degrees of freedom; the study uses 1.96, and at three seeds t is 4.30).
  The generator ships both, so nothing is recomputed from per-twin files.
  Established but imprecise: scored on direction only. Anything else: listed
  with its reason and not scored.
- *Truth.* The set's mean relative effect of the child (every seed of the
  set, held-out ones included: it is ground truth, not engine output), times
  the item's share of the child's effect (1 for a learned edge or a root; for
  a formula parent its mean Shapley fraction over the run's worlds), times
  this world's null value of the child. Where this world's twins were
  aligned (`pre_onset_rms_diff` 0 in the twin's summary for the child), its
  own twin difference instead. The set's relative change is of the daily
  series' window mean; the engine's window rule takes a rate as
  Σnumerator / Σdenominator, and that small difference is in the error.
- *Scored.* `ratio` (engine over truth; median per world), `log_error`
  (ln of the ratio) and its absolute value, `coverage` (the interval holds
  the truth; items with an interval only), `wrong_sign`, and `withheld`: an
  estimate the engine withheld (a node status, a non-finite posterior) is
  counted as withheld and never as zero. Formula edges, learned edges and
  roots are reported apart. `items_*` count what each world could score, so
  a run with nothing eligible reads as zero, beside the eligibility table
  that says why.
- *The two questions differ, on purpose.* The engine answers window over
  window (analysis against the adjacent reference window); the truth is a
  counterfactual (world against its null twin). The reference window's own
  drift is in the error, as it is in what a reader of the engine gets.
- *Not built:* the error as a share of the target's measured change (§5.4's
  third bullet).
- *First runs* (2026-11-21; demo tree, not citable). Smoke set (three
  seeds): 4 of 8 items size-eligible, all near the root (the spend ramp's
  root and its sessions edge, the signup regression's root and its signups
  edge; t at two degrees of freedom still admits them); the churn spike's
  four are `open` at three seeds. Ten-seed set, `attribution` tier, dev seeds
  0–4, onboarding revamp and churn spike
  ([`eval/2026-11-21_attribution_white_cube10_demo_tree.md`](eval/2026-11-21_attribution_white_cube10_demo_tree.md)):
  8 of 10 items size-eligible; the churn spike's top edge is ±21% and scored
  on direction only, and the onboarding revamp's learned edge is refused
  (`several_parents_moved`). Roots are sized well (median ratio 0.99 for a
  parentless root's change, 1.09 for a fitted root's unexplained part).
  Formula edges are not: median ratio 2.4, interval coverage 38%, every sign
  right. Over a 17-week window the engine's Shapley shares answer
  window-over-window, so a formula edge carries its parent's growth since the
  reference window as well as the anomaly (onboarding seed 0:
  `new_subscriptions -> new_mrr` attributed 1,088 against a twin difference
  of 270). Whether Q5 should keep scoring the engine's own question against
  the counterfactual, or compare against a reference-matched truth, is a
  decision for the next change. Cost: 6.1 minutes of wall time on four
  shards for the ten worlds.

**Not built:** abstention (§5.5; it does not fall out of attribution: it
needs the off-tree and data-quality worlds scored for what the engine says,
not for sizes); the magnitude
ladder (§5.2); the slice check (§5.3); the share of badly calibrated nodes a
diagnostic flag catches (§5.1, which needs a definition of "badly calibrated"
for one node on a handful of windows); story worlds; nodes at month grain
(skipped: the grid is weekly).

**What each vertical can serve, rechecked at generator `b51d70e`.** White
Cube: all 23 fetched nodes of the demo tree; `active_subscriptions` carries
the distinct-count caveat; `dau`, `paying_active_users` and `wau` are refused
at week grain. Alpenglow: the demo tree minus `discounted_orders`, and no
stock but the running total of new customers its bindings file builds.
Meridian: every one of its 15 metrics is servable at week grain now,
`sales_cycle_days` included (from its sides), so a tree is the only thing
missing. Bristlecone: all 8 servable at week grain, still no stock.

**What the other two verticals' trees would need.** Neither is authored here.
Meridian (`b2b_services`): `win_rate` and average deal size as rates with
their identities (`deals_won = closed_deals × win_rate`,
`bookings = deals_won × avg_deal_size`), `open_deals` or
`open_pipeline_value` as the stock, learned and lagged edges down the funnel
(leads to qualified to proposals to negotiations) and from bookings to
invoiced revenue to collected cash. `sales_cycle_days` is a mean over rows
whose sides the generator now exports, so it can be a week-grain rate. Bristlecone (`cpg_wholesale`): eight metrics and no stock, so the
canonical-tree requirement of one stock needs a new metric from the
generator (retailer inventory, or points of distribution); a learned edge
from scan sales to shipped cases; `net = gross − trade spend` as the formula;
and marketing spend has no path to sales in that vertical, so it would be a
node with no descendants.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*

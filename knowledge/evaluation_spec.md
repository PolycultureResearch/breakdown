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

The canonical trees exist (fake_companies
`examples/breakdown_<company>_tree.yml`, design note `docs/canonical_trees.md`,
branch `feat/canonical-trees`). The runner takes the tree as a parameter and
stamps its path and hash on the scoreboard; it marks a run canonical when the
tree is that company's `examples/breakdown_<company>_tree.yml` run with no
bindings file, and not canonical otherwise. A tree binds to a world set by metric
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
- for slice anomalies (Q4): whether the true dimension and value are the top
  slice of the true node, by the engine's own slice attribution, with the
  root member's |z|, whether the first place carries evidence, and, for a
  sliced rate, whether the engine called the change `within` or `mix` as the
  generator's per-slice verdicts say

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

### 5.4 Counterfactual attribution (Q5)

Scored against the counterfactual, the twin difference (Devon, 2026-11-21).
The engine answers a window-over-window question, so a formula edge that
carries its parents' drift since the reference window is expected to exceed
the counterfactual under this framing; that excess is the gap innovation-based
RCA (branch `design/innovation-rca`) aims to close. No alternative pre/post
truth is built. On twin worlds, for each node on the path from root to
target:

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
    --worlds /path/to/worlds/meridian \
    --tree /path/to/fake_companies/examples/breakdown_meridian_tree.yml \
    --tier smoke --out /path/to/runs/meridian_smoke
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
| `--verdict-column verdict\|verdict_10pct` | Which of the set's verdicts picks targets and attribution eligibility. The strict `verdict` (two-sided 1%) by default; `verdict_10pct` for a small set. Stamped on the scoreboard and on every row |
| `--allow-schema-v1` | Read a schema_version 1 world set, which is refused by default. Stamped on the scoreboard |
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

**Trees.** The default is the company's canonical tree, from the generator
repo: `--tree <fake_companies>/examples/breakdown_<company>_tree.yml`, with no
bindings file (every node binds by metric name). Those trees name the `local`
provider with a relative dbt path; the loader replaces the provider block
with the series file before the tree is parsed, so that provider is never
constructed and the path is never read. Coverage, checked on the
`canon_*` sets (and White Cube on `proof_v2r2_white_cube`; its canonical tree
is the demo tree with another header and provider):

| Tree | Nodes served | Slices | Flagged |
|---|---|---|---|
| White Cube | all 23 | every declared dimension except `country` on the eleven MRR-movement and subscription nodes (the series file exports those by `plan` and `category` only) | none: the tree has no summed stock |
| Alpenglow | all 18 | every declared dimension, `avg_unit_price` by category included | none |
| Meridian | all 15 on a set built at fake_companies `817de23` or later; on an older set `open_deals` and `open_pipeline_value` are refused (summed stock) | every declared dimension | none: the stocks are their week-end level |
| Bristlecone | all 9 | every declared dimension | none on a current set (`trade_inventory_build` is its week-end level); a first-day caveat on an older one |

Legacy trees still run: `demo/white_cube_tree.yml`, and
`demo/alpenglow_tree.yml` with `evalrunner/bindings/alpenglow_tree.yml` for
its four channel-filtered session nodes. They are breakdown's own benchmark
and demo trees, not canonical, and the scoreboard says so.

**World sets must be schema_version 2** (the generator's spec §10.4). A
version 1 set is refused before anything else is read, naming the version
found and the one needed; `--allow-schema-v1` reads one anyway, stamped. The
loader's contradiction checks (a side's stated rule against its input's own)
stay as a second line.

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

**Week values are what breakdown's providers would return.** The evaluation
measures the engine as users run it, so each node carries the number its
provider would hand the engine. Checked in the code: every provider asks its
source at the node's own grain. The local provider runs `mf query
--group-by metric_time__<grain>` (`data_fetch.LocalDataFetcher`); the dbt
and duckdb providers compile `SUM` / `COUNT` / `COUNT(DISTINCT)` / `AVG`
bucketed by `DATE_TRUNC(<grain>)` (`dbt_sql._aggregate`, `build_query`), and
`dbt_bridge` turns a ratio or derived metric into a formula edge over inputs
fetched the same way (it refuses cumulative metrics outright). None fetches
days and resamples them by kind; `grains.resample_up` only lifts a finer
parent to a coarser child inside the engine. So a week node gets
MetricFlow's week number, which the generator states per metric since its
§10.9 (`metricflow_week`, `matches_metricflow_week`,
`input_metricflow_week`), and the loader serves exactly that, each side of a
ratio included. Where it differs from the manifest's business rule the plan
carries a caveat naming both. Since the generator's §10.10 (2026-11-24) it
differs for 5 metrics, all distinct counts or built on one
(`active_subscriptions`, `dau`, `paying_active_users`, `arpu`,
`customer_churn_rate`). The snapshot stocks (`mrr`, `open_deals`,
`open_pipeline_value`) are declared semi-additive and the cumulative ones
(`wau`, `customer_base`, `trade_inventory_build`) carry `period_agg: last`,
so MetricFlow answers each by its period's last day (`last_day`), which the
loader serves at week and month grain and as a ratio side (`arpu`'s `mrr`).
A set built before that says a snapshot stock's week is its seven daily
levels added up (`sum_of_days`); breakdown refuses such a stock at load
since C66 (PR #152), and the loader refuses it by name the same way rather
than serve it at seven times its level. A month node of a metric whose week
rule differs from the business rule is refused (the set states week rules
only). On a set
without `metricflow_week`, the rules below apply. For the demo trees no value
changed with this: their week nodes were already served as MetricFlow does.

- *Distinct counts.* `dau`, `paying_active_users` and `active_subscriptions`
  count distinct entities; MetricFlow at week grain counts them over the
  week, which the days cannot give. Schema 2 ships those week values in
  `<world>.weekly_series.parquet` (`metrics[].has_weekly_series`), and the
  loader serves them from there, whole weeks only (the generator's partial
  edge weeks, `days < 7`, are dropped). A ratio side or derived input that is
  such a count takes its week value too, as MetricFlow aggregates each input
  at the grain asked for: the weekly churn rate is the week's churned over
  the week's distinct active subscriptions, so `churned = active × rate`
  holds exactly (2e-16 on `proof_v2_white_cube`). The distinct week count is
  about 0.7% above the last day's. Without a weekly file (a version 1 set),
  a distinct-count stock is served as the last day's count with a caveat
  naming it.
- *Cumulative metrics* (`wau`, and schema 2's `customer_base` and
  `trade_inventory_build`). Since the generator's §10.10 they carry
  `period_agg: last` and MetricFlow answers them by the period's last day,
  which matches the manifest's rule; served so at week and month grain. On a
  set built before that, MetricFlow took the week's *first* day (its §10.2),
  and the loader still matches it there, with a caveat.

**A tree's `kind` must agree with the generator.** The engine resamples a
parent by its kind, so a node the tree calls a flow and the generator calls a
stock is refused by name.

**A stock is its period-end level, and a summed one is refused.** Until
2026-11-24 MetricFlow and `dbt_sql` added a snapshot stock's daily levels up
over a coarser period (Meridian's weekly `open_deals` and
`open_pipeline_value` read 7.2 to 8.1 times their week-end level), and the
runner served that number, flagged provisional. Breakdown's C66 (PR #152)
now refuses such a stock at load, and the generator declares its snapshot
measures semi-additive (§10.10), so the runner serves the period's last day
and refuses, by name, a stock a set still says is summed. The provisional
flag is gone; scoreboards published before carry it.

**A mix-shift root is scored at the ratio whose mix it moves.** A
category-demand driver is rooted at the member it moves (`units[tees]`),
which moves the total by exactly zero. The generator names the ratio whose
total it moves in `root_metric_mix` (`avg_unit_price`), and the runner maps
it like `root_metric_rate` (`on_tree_mix`). The tees slump maps to
`avg_unit_price` on the canonical Alpenglow tree.

**Slices are served from the series file.** The runner replaces the tree's
provider, so slices used to be the mock provider's. The world fetcher now
answers `fetch_metric_sliced`: every member of the dimension the tree
names, found through the manifest's map from the series file's dimension to
the MetricFlow group-by id the tree's `source` uses, each aggregated to the
node's grain by the same rules as the node's total. A dimension the file
does not export the metric by, or a node that is itself a slice, is refused
by name, and the plan lists what can be sliced. The frame is always whole
(no top-k fold in the source), which the fetcher says.

**Slice localization (Q4).** On a single world, for each dimension of the
root slice that the truth node declares, the engine's `slice_attribution`
runs over the anomaly's declared window against the default reference
window. Scored: the root member's rank in the engine's order and how many
slices were ranked; its |z| (excess over the half-width of its 95% interval
over 1.96); whether the first place carries evidence (`noise_level` false);
whether the engine says `localized`; and for a sliced rate whether the
engine's call for the member (`within` if its own rate's term is the larger,
else `mix`) matches the set's verdicts for the member's rate and its weight:
`within` when the rate's own slice is established, `mix` when only the
weight's is, `both` (not scored) when both are. A dimension the node does
not declare, or that cannot be served, is a refused row with its reason.
The engine orders slices by excess in the direction of the node's
window-over-window gap, so a member that fell while its node rose ranks last:
that is the engine's answer, not a fault of the score. The member's rank by
|excess| alone is kept beside it as a contrast (`top1_by_abs_excess`), and
each detail row shows the gap's sign, so a reader can tell the two failures
apart.

**An effect that lands after its declared window** (Meridian's delivery
crunch: the window dates the signings; the invoices fall due about two months
later, median onset 63 days after a 56-day window) is not sized against the
set's effect over the declared window. Its attribution items are refused by
name (`effect_after_window`, from the set's `onset_median_lag_days`);
measuring the shifted window from the twins (generator §10.7) is not built.
Localization uses the twin's own onset as always.

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
(`set:<verdict column>`, `twin_moved`, or `fallback_deepest_descendant` when
nothing downstream moved). The cost: a world's target depends on which seeds
the set was built with, so the verdicts used are part of a single world's
job key. With three seeds the 1% bar is a t of 9.9, so few effects are
established on a smoke set. Schema 2 adds `verdict_10pct` (two-sided 10%,
t 2.92 at three seeds) beside the strict one; `--verdict-column
verdict_10pct` picks targets and attribution eligibility by it. The strict
column stays the default; the loose one is stamped on the scoreboard header
with its cost (about one pair in ten with no effect is established by
chance), and every row names the column that decided.

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

**Counterfactual attribution (§5.4, Q5) is scored where the world set can ground a size.**
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
- *Basis.* Which of the set's effects matches the engine's window value for
  the child: `rel_change_aggregated` (the metric's own rule, generator §10.6:
  Σnum/Σden for a rate, the window total for a flow) where the set has it;
  `rel_change` (the window mean of daily values) for a stock, whose
  per-period levels the engine averages over the window. A week child served
  by a MetricFlow rule that differs from the business rule the set measured
  by has no matching effect, and its size is refused by name
  (`provider_rule_differs`; on White Cube, `customer_churn_rate` and
  `active_subscriptions`). Every row and the eligibility table name the
  basis.
- *Eligibility*, from the set's `effects_summary.parquet` for the child
  metric on that basis: `verdict` established (the verdict tests
  `rel_change`; target choice is unchanged), and the 95% half-width of the
  mean within 20% of it at the set's own seed count (`t · sd / √n_seeds /
  |mean|`, Student's t on n − 1 degrees of freedom; the study uses 1.96, and
  at three seeds t is 4.30). Established but imprecise: scored on direction
  only. Anything else: listed with its reason and not scored.
- *Truth.* The set's mean relative effect of the child on that basis, times
  the item's share of the child's effect (1 for a learned edge or a root; for
  a formula parent its mean Shapley fraction over the run's worlds), times
  this world's null value of the child. Where the child's twins were aligned
  before the start (the set's `aligned_before_start`, generator §10.5; where
  that is NULL, this twin's own `pre_onset_rms_diff` over its
  `pre_onset_days`, named as the fallback), this world's own twin difference
  instead.
- *Held-out seeds are in the truth, deliberately* (Devon, 2026-11-21). The
  set's cross-seed mean averages every seed it holds, held-out ones
  included: it is ground truth, not engine output, and more seeds make it
  more precise. A run never scores a held-out world without `--held-out`;
  the scoreboard stamps the seeds the truth averages
  (`counterfactual_attribution.truth_seeds`,
  `truth_includes_held_out_seeds`). Known and kept for now.
- *Scored.* `ratio` (engine over truth; median per world), `log_error`
  (ln of the ratio) and its absolute value, `coverage` (the interval holds
  the truth; items with an interval only), `wrong_sign`, `error_share` and
  its absolute value (`(engine − truth) / |the target's measured
  counterfactual change|`, §5.4's third bullet: the target's change is the
  set's cross-seed mean for the target times this world's null value, or
  this world's own twin difference where the set has none on a matching
  basis), and `withheld`: an
  estimate the engine withheld (a node status, a non-finite posterior) is
  counted as withheld and never as zero. Formula edges, learned edges and
  roots are reported apart. `items_*` count what each world could score, so
  a run with nothing eligible reads as zero, beside the eligibility table
  that says why.
- *The two questions differ, on purpose.* The engine answers window over
  window (analysis against the adjacent reference window); the truth is a
  counterfactual (world against its null twin). The reference window's own
  drift is in the error, as it is in what a reader of the engine gets.
- *Not built:* sizes in units: an item whose child's null level is
  zero or negative (`trade_inventory_build`, `discounted_orders` outside a
  promotion) is refused by name, since its relative change is no size.
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

**What each vertical can serve, rechecked on the schema 2 proof sets**
(generator `01bf895`). Every metric of every vertical is servable at week
grain. White Cube: all 23 fetched nodes of the demo tree, with no caveat
(`active_subscriptions` comes from the weekly file); `dau`,
`paying_active_users` (weekly file) and `wau` (first day, with a caveat) are
servable too. Alpenglow: all 19 fetched nodes; the returns anomaly
(`jackets_sizing_recall`, rooted at `returned_units` with
`root_metric_rate: return_rate`) maps to the tree's `return_rate` node
(`on_tree_rate`), checked against its ground-truth record. Meridian: all 15
metrics; a tree is the only thing missing. Bristlecone: all 9, including the
stock `trade_inventory_build` (first day at week grain, with a caveat); its
level can be negative, so a relative attribution size over it is refused by
name until scoring in units is built. A tree is still missing.

**The schema 2 proof runs** (2026-11-21, the generator's `proof_v2_*` sets:
seeds 0–2, null plus two singles each; demo trees; not citable).
White Cube smoke tier, strict and loose
([`eval/2026-11-21_smoke_v2_white_cube_strict.md`](eval/2026-11-21_smoke_v2_white_cube_strict.md),
[`…_loose.md`](eval/2026-11-21_smoke_v2_white_cube_loose.md)): 12 minutes of
wall time for the strict run (the run's own 16 sampler processes held the
load at 6 to 17; nothing else was running); the loose one reused its
calibration and fits and took 12 seconds. Under the strict verdict the churn
spike has no established target and falls back to the deepest descendant
(`net_new_mrr`); under `verdict_10pct` the same node is its target, now
because the set establishes it (basis `set:verdict_10pct`), so the ranks
are unchanged. Attribution: the loose verdict turns 2.5 more items per world
into direction-only items (every one in the right direction); none becomes
size-eligible, because precision at three seeds, not the verdict, is what
holds them back (the churn spike's items are ±56–82%). The two sized
formula edges and roots repeat the ten-seed pattern: roots within 2%,
formula edges 3.5 times the twin difference with no interval covering it.
Alpenglow, single worlds only, strict and loose
([`eval/2026-11-21_singles_v2_alpenglow_strict.md`](eval/2026-11-21_singles_v2_alpenglow_strict.md)):
4.5 minutes. All 19 nodes served without `--drop-unserved`. The budget cut's
target is `refunds` strictly and `net_revenue` loosely. The tees demand
slump is rooted at `units[tees]`, and the tree holds only total units, whose
verdict is `exactly_zero` (the slump moves the mix, not the count): its
root maps to a node the anomaly did not move, which the tree, not the
runner, would have to fix with a slice node.

**The r2 proof run** (2026-11-21, `proof_v2r2_white_cube`, generator
`c68b55d`; [`eval/2026-11-21_smoke_v2r2_white_cube_strict.md`](eval/2026-11-21_smoke_v2r2_white_cube_strict.md),
diff against the schema 2 strict run in
[`eval/2026-11-21_smoke_v2r2_diff_vs_v2.md`](eval/2026-11-21_smoke_v2r2_diff_vs_v2.md)).
11.7 minutes of wall time, with another job holding the load at 24 when it
started. The set's singles are the paid-social Brazil lift and the churn
spike (the earlier proof set had the onboarding revamp instead), so the diff
pairs the null worlds and the churn spike only. On those, calibration,
detection and localization are identical to every digit printed: serving
week nodes as the providers do changed no value of the demo tree, as
expected. Only cost moved (machine load). Q5 rows do not pair, because the
question is now named `Q5 counterfactual attribution`. On attribution: the
churn spike's root is now refused as `provider_rule_differs` (the weekly
churn rate's denominator is the week's distinct active subscriptions; the
set's effect divides by the last day's), and its other three items stay
`open`; the Brazil lift's two items are established but ±35% at three seeds,
so direction only. No item was size-eligible, so the error share has no
number on this run.

**The first canonical runs** (2026-11-23; fake_companies
`feat/canonical-trees` at `cc8caac`; three seeds of the `canon_*` sets; not
citable). 14 minutes of wall time for all three, with another job holding
the load at 12 to 41 (`eval/2026-11-23_*canonical*.md`).

- *Meridian, smoke* (its first run): calibration mean z −0.01, sd 1.09,
  9% of windows beyond 2; 96 of 288 windows not scored because
  `sales_cycle_days` and `avg_deal_size` are undefined in weeks with no
  closed deal, which the engine refuses to fit over. `open_deals` and
  `open_pipeline_value` are flagged provisional. Localization over 4.7
  candidates: innovation contribution top-1 44%, the engine's
  `ranked_causes` 56%, neither with evidence. The delivery crunch's
  attribution items are refused as `effect_after_window`. Slices: the
  referral exit ranks its source last of four in every seed (|z| 29), first
  by |excess|: leads grew over the window.
- *Bristlecone, smoke* (its first run): calibration mean z −0.02, sd 0.82,
  2% beyond 2; detection |z| 3.3 at onset with 56% of roots beyond every
  null window. Localization: innovation evidence top-1 56% (with evidence),
  `ranked_causes` 0%. Attribution: roots fitted with parents within 2%;
  formula edges wrong-signed in every world (the window-over-window gap
  rose while the counterfactual fell). Slices: the budget cut's channel
  first; the capsules delisting (category and banner) and the destock
  (channel type) ranked last in every seed, first by |excess|, all on nodes
  whose gap rose.
- *Alpenglow, single worlds* (the tees slump, the pants price rise, the
  email checkout bug, the paid-search suspension): slice top-1 83% (|z| 27);
  the tees member ranks 5th, 7th and 1st of 7; the engine's within-or-mix
  call is right in all six rows where the truth is one or the other
  (`within` for the email conversion bug, `mix` for the tees slump); the
  pants rise is `both` in the set and the engine calls it `mix`.
- The per-analysis cost lines read zero because the published scoreboards
  come from a rerun of the single worlds over cached fits (to add the
  |excess| contrast); the first invocation's cost is the wall time above.

**Meridian and Bristlecone again, with stocks at their period end**
(2026-11-24; canon sets rebuilt at fake_companies `817de23` with the same
config and seeds; diffs in
`eval/2026-11-24_smoke_canonical_{meridian,bristlecone}_diff_vs_2026-11-23.md`).
4.7 and 3.8 minutes of wall time, another job holding the load at up to 28.
Only the stock nodes' numbers moved, and every change traces to one node:

- *Meridian*: `open_pipeline_value` is now its week-end level instead of
  seven days of it summed. Its calibration moved (stock role mean z 0.09 to
  0.29, sd 0.82 to 0.90, PIT distance 0.20 to 0.29, one window in 24 now
  beyond 2), which carries into the `all` row and the Q9 splits (one window
  moved from `suspect` to `ok`). Every other node's z is identical to every
  digit. `open_deals` is not scored either time: its parent
  `sales_cycle_days` is undefined in weeks with no closed deal, so its fits
  are refused. Detection, localization, slices and attribution are
  unchanged: no smoke anomaly's scope holds a stock.
- *Bristlecone*: `trade_inventory_build` is now its week's last day instead
  of its first; its calibration moved (mean z 0.19 to 0.10, lag-1 −0.14 to
  0.03) and nothing else did.

The first Meridian rerun lost three calibration windows to a race on
ArviZ's `~/Library/Caches/arviz/daily_warning` file between worker
processes, a transient of the machine and not of the data; those three jobs
were rerun.

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

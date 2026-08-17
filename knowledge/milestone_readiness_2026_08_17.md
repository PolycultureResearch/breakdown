# Milestone readiness — Northern Nights, 0.1.0, PyPI

> **Written:** 2026-08-17, against `212b53c` (the merge of PR #73; audited
> pre-squash as `8d311d4`, same content) · **Status:** assessment, with a
> recommended sequence · **Method:** three parallel audits — (1) the four recent
> decision areas traced from policy to code to tests, (2) the creds→first-RCA
> onboarding path walked as each of three client shapes, (3) open-defect and
> packaging verification against the three milestone gates — plus a fresh read
> of the [roadmap](roadmap.md), the [white paper](statistics_whitepaper.md) §3,
> and [`rate_denominator_policy.md`](rate_denominator_policy.md). Every open
> Horizon 0 item and every high-severity new finding was verified against the
> working tree, not taken from the roadmap's description.

The three milestones, in the order they should land: **(1)** first client
deployment (Northern Nights Music Festival), **(2)** the 0.1.0 release,
**(3)** PyPI publication. The brief: as ready as possible without
over-optimizing, because real use will surface things no audit can.

---

## 1. The short version

**The four recent decisions are all sound.** Null handling, the optional
denominator with the `doctor` gate, the three-tier non-additive design, and
weight-blended slicing each have the right policy, argued in writing, with the
primary implementation correct and well tested. This audit did not find a
single decision that should be reversed.

**The failures found are all the same failure.** Fifteen new findings came out
of tracing the four decisions, and ten of them sit on one of three boundaries:
engine→MCP shaping, engine→`app.js`, and metric-path→slice-path. Each is a
policy chosen carefully in one place and not carried to its neighbour — the
exact meta-defect the two hostile reviews named and the four AGENTS.md rules
were written to end. The rules hold at the provider boundary, where they have a
structural test; they are leaking at the three boundaries that don't.

**The three milestones need different work, and less than it looks like.**

- **Northern Nights** is not blocked by the open statistical items (S18/S19/S20
  are disclosed limitations, and C4 — the one failure actually measured in
  production there — is fixed). It is blocked by roughly **2 days** of
  operational honesty: `doctor` fails against a healthy snapshot-served box,
  a one-day window nudge degrades every metric, one likelihood misspecification
  is genuinely undisclosed, and C12 will fire on the first *slice by* click of
  a rates-and-dimensions tree.
- **0.1.0** is blocked by the four open Horizon 0 rows plus five findings from
  this audit that meet the same bar (a number the engine can't defend, or a
  written rule violated in shipped code): roughly **a week**.
- **PyPI** is mostly ready — packaging is the strongest area of the project —
  but has three genuine blockers that are disclosure and hygiene, not code:
  the CHANGELOG is silent on the Apache-2.0 → FSL-1.1 license change, the
  `[project.urls]` demo link points at a dead instance, and no build has been
  verified locally. Roughly **half a day**, plus the tutorial if you want the
  landing to convert.

**The half-day north star is currently unreachable for two of three client
shapes**, and the single biggest lever is not a feature — it's publishing.
`pip install metric-breakdown` does not resolve, so the clock cannot start.
After that, the levers are a `breakdown scaffold` command (the bridge already
computes everything it would emit), a real-provider example tree, and about six
one-hour fixes to defaults and doctor coverage.

---

## 2. The four decisions, judged

### 2.1 Null and gap filling — **sound; one real gap, one policy fork**

The decision — one shared `_align_to_spine` contract, kind-aware (flow
zero-fills with a warning naming every invented period; stock forward-fills or
raises; rate stays undefined), partial edge periods dropped, trailing gaps
trimmed, tz coerced before anything else — is right, and the
leading-fill-rather-than-trim choice is justified on a concrete downstream
consequence rather than symmetry. All five real providers route through it,
and the structural invariant (`tests/test_project_invariants.py:159`) enumerates
fetchers by base class so a new provider can't skip it. The narrow
`fill_nulls_with: 0` acceptance in the dbt bridge is correctly restricted to
flow-shaped aggregations with `average` refused by name.

Two findings that matter:

- **The contract stops at `fetch_metric`; the sliced path never got it**
  (high). `_sliced_long` (`data_fetch.py:548`) — used by `cloud` and `local`
  sliced fetches — calls `_floor_labels` but never `_to_naive_dates`; the
  `dbt` provider's sliced path does call it. Downstream, a tz-aware sliced
  frame reindexed against the tz-naive spine in `slices._fill_by_kind` goes
  silently all-NaN, and the flow branch zero-fills it: **every slice zero**,
  the C1 symptom in a surface C1 never swept. The reconciliation residual will
  flag `discrepant`, so it isn't fully silent, but the on-screen numbers are
  fabricated. The fix is one line plus extending the structural invariant to
  `fetch_metric_sliced`.
- **The kind-aware fill exists twice with different policies** (medium).
  `slices._fill_by_kind` zero-fills flows *with no log line* (C18's shape one
  layer up) and zero-fills a stock's leading gap where `_align_to_spine`
  raises for exactly that case. There's a defensible per-slice argument for
  the divergence ("the plan tier didn't exist yet") — but it's nowhere written
  down as a chosen policy, which is how the last five of these started.
  Also: `_pivot` silently *sums* duplicate `(date, slice)` rows where the
  unsliced path treats a duplicated date as a hard grain-violation error.

One smaller correction worth making: the 2.19 acceptance's safety proof
("zero-fills every missing period unconditionally, so accepting produces the
identical series") is wrong at the trailing edge — breakdown *trims* trailing
gaps where MetricFlow's timespine would report zero. The acceptance is still
right; the stated proof (in `dbt_bridge.py:623` and the roadmap 2.19 row)
should say "identical at interior and leading gaps; divergent at the tail by
C2's deliberate choice."

### 2.2 Rate denominators — **the best-made decision in the set; two propagation gaps**

Endorse the whole chain, including the part that was hardest to resist:
leaving `denominator` optional and putting the teeth in `doctor`.
[`rate_denominator_policy.md`](rate_denominator_policy.md)'s argument survives
this audit's scrutiny — the disclosure work (four distinguishable
`window_aggregate` statuses, labelled *where the number is read*, quoted by
`doctor`) genuinely converts the case from C-class to S-class, and the
evidence that 32 of 35 declared denominators were mechanically derivable means
compulsion would have bought little. The separate `no_denominator` field (vs a
sentinel) is right for the measured PyYAML reason. The `Σnum/Σden` window
aggregate with the components-based Shapley branch is correct, and the
single-entry-point discipline (`node_window_value`, enforced by AST scan) is
exactly how a policy stays propagated. Revisit-mandatory-at-1.0 stands; nothing
found here moves it.

Two gaps, both high:

- **The labelling stops at `/simulate`.** `simulate.py` computes a rate's
  baseline through `node_window_value` (arithmetic right) but publishes no
  `window_aggregate`/`window_aggregate_reason`; the what-if UI renders the bare
  number with no basis label, and `WHATIF_HOW_TO_READ` has no clause. The
  stated rule — "labelled where the number is read, not only in the log" — is
  violated on one of the two surfaces that read it. Mechanical fix: thread the
  two fields into the simulate node payload, call the existing
  `windowBasisHtml()` in the two `app.js` render sites, add one `how_to_read`
  bullet.
- **`period_mean_weights_unavailable` has zero tests.** It's the status that
  exists so a declared denominator whose series misses the window can't
  silently fall back — the payload-misdescribes-its-own-arithmetic guard — and
  it's reachable, rendered, and documented, but the invariant test asserts
  exactly the *other three* statuses. One refactor away from silently
  unreachable.

Plus one stale caveat: `slices.py:869` still says "unweighted window-mean" of a
number that has been weighted since 1.11c landed.

### 2.3 Non-additive metrics — **sound; the flow query breaks its own promise**

The three-tier design is right, and the two hardest calls were made correctly:
`resolve` has no default (because `first` vs `last` measurably disagree on the
same day), and `resolve: error` reports `additivity: unknown` rather than
falsely claiming `exact`. The overlap is quantified signed, `discrepant` now
means only *unexplained* divergence, and entity flows sit beside the
attribution with `reconciles_to_gap: false` — the migration diagnostic is the
single feature most likely to prevent a wrong narration ("two large offsetting
causes" for a platform switch that changed nothing).

Findings:

- **`resolve: error` is honoured in the slice query and silently executed as
  `last` in the flow query** (medium). `build_resolved_slice_query`
  short-circuits under `error` with the reason written out;
  `build_entity_flow_query`'s `order = "ASC" if resolve == "first" else "DESC"`
  lets `error` fall through as `last`, untested in either direction. There may
  be a real argument that flows must resolve per-window even under `error` —
  but it's nowhere, and the author's assertion is silently overridden.
- **MCP loses the entire 3.8 payload** (medium). `compact_slice` drops
  `additivity`, `overlap`, and `entity_flows`; `SLICE_HOW_TO_READ` never
  mentions `not_applicable`. An agent slicing an overlapping dimension sees
  withheld shares with no field explaining why and no flow diagnostic — the
  exact consumer most likely to produce the misreading the flows exist to
  prevent. Instructive contrast: the *same* labelling policy was carried into
  both MCP compaction branches for rate aggregation and into neither for slice
  additivity.
- Small: a query *exception* in doctor's entity-grain check is reported as an
  `error`-assertion *violation* with the wrong remedy; and the roadmap's
  "tier 3: trend only" clause describes behavior that doesn't exist (harmless —
  the parser makes tier 3 unreachable — but the row should say so).

### 2.4 Dimensional slicing — **sound; the concentration verdict is dead for rates**

The Bennet weight-blended split is exact and correctly telescopes after
folding; ranking by signed excess is right for the subtle two-slice-tie reason
written in the code; non-finite filtering and cache bounding are in place and
structurally tested.

The headline finding of the whole audit sits here:

- **A rate's slice panel can never say "localized"** (high). The UI verdict
  gates on `top.baseline_share != null`; the rate attribution path never emits
  `baseline_share` (only the sum path does). So for every `kind: rate`
  dimension the panel unconditionally prints "Not localized" no matter how
  concentrated the movement is — and every existing test of a rate panel
  asserts the negative, so the suite passes for the wrong reason. This
  matters doubly because rate-over-dimension is the product's showcase shape
  ("churn rate fell, concentrated in EMEA"). Fix is one field
  (`share_reference` *is* the baseline share for a rate) plus one positive
  assertion. This is also a fifth-rule lesson: no JS runner, so it survives
  until someone opens the UI on a concentrated rate and looks.
- **The 0.25 concentration floor exists only in the browser** (medium). The
  payload and MCP have no `localized` verdict, so an MCP consumer will
  confidently name the top slice exactly where the UI declines to. Same class
  of judgment that `window_aggregate` deliberately moved into the payload;
  this one should move too.
- **C12 confirmed open, plus a live policy contradiction.** The parser's
  denominator validation accepts a *finer, nesting* grain; `_run_slice`
  demands strict equality — and `dimensions[].weight` defaults *from*
  `denominator`, so a month-grain rate over a day-grain denominator parses
  clean, aggregates correctly, and 422s (not 500s — the roadmap row is stale)
  on the first slice click. Moving the check to the parser forces the policy
  decision first: strict equality (simplest, a breaking schema change under
  the pre-1.0 contract) or accept-and-resample (consistent with
  `weights_for`, breaks nothing, ~40–60 lines). None of the four shipped trees
  currently has a mismatch, so either lands safely now.
- Small: the UI re-truncates migrations to 3 and drops the
  `migrations_truncated`/`migration_net` fields the engine publishes
  specifically so truncation is disclosed.

### 2.5 The meta-finding: three boundaries without a structural test

The four AGENTS.md rules work where they have an enumerating test (the
provider boundary). The fifteen findings above cluster on three boundaries that
don't: **engine→`compact_*`** (findings on `interaction`, slice additivity,
entity flows), **engine→`app.js`** (rate localization, migration truncation,
simulate labelling), and **metric-path→slice-path** (tz coercion, the second
fill implementation, duplicate-row policy). A structural test asserting that
every key the engine publishes is either consumed or *explicitly listed as
dropped* by each `compact_*` function — and extending the rule-1 invariant to
`fetch_metric_sliced` — would have caught roughly half of these before this
audit did. That test is cheaper than any two of the findings it prevents and
is the highest-leverage single item in this report.

---

## 3. Punch list by milestone

### 3.1 Northern Nights (do first; ~2–2.5 days)

The S-items raised by this client (S18 settling, S19 thin panels, S20
zero-inflation) are **not** the deployment risk — they're disclosed, and the
engagement can be framed around them (see §3.4). The risk is operational
falsehood and one undisclosed gap:

| Item | Why it's first | Size |
|---|---|---|
| **`doctor` is blind to snapshots** (Aug-12 review, Op #3) | On the snapshot-served deployment mode the docs recommend, `doctor` FAILs against a perfectly healthy box — and every failure path in the product points the operator at `doctor`. The worst possible first support interaction, now with a meaningful exit code (1.12) that is wrong here. | 4–6h |
| **`SnapshotStore.read` requires an exact window match** (Op #2) | Nudging `--end-date` one day degrades every metric. A first deployment *is* iterative window-fiddling against a live snapshot dir; interacts with the item above (operator sees `degraded`, runs `doctor`, gets a false FAIL). `read_sliced` already implements the containing-window trim — same file, opposite policy. | 3–5h |
| **Disclose the Gaussian-on-zero-inflated exposure** (S20's cheap half) | The one NN number currently undefendable *with nothing saying so*: a mostly-zero series fits, converges, and stamps `fit_quality: "ok"` while putting posterior mass on negative counts. Don't build S20 now — add a `fit_quality` warning / `how_to_read` clause when the fit window contains a long run of exact zeros. | ~4h |
| **C12 — move the weight-grain check to the parser** | A festival tree is rates + dimensions: precisely the shape that hits it. Decide strict-vs-resample first (§2.4). | 1–2h (+decision) |
| **Fix the rate slice-panel localization** (finding 4.1) | If NN's demo moment is "churn concentrated in channel X," the panel will currently refuse to say so, unconditionally. | 1–2h |
| **Expectation brief** | A one-pager for the client: withheld intervals are the tool being honest (C4 working as designed — most of a festival tree, most of the year, is constant-reference); frame the engagement as *within-cycle* RCA, not cross-edition inference (S19); agree the booked/settled basis split in writing at authoring time and schedule `BREAKDOWN_REFRESH=1` (S18). | 2h |

### 3.2 The 0.1.0 release (~1 week, after NN)

The four open Horizon 0 rows, verified live (roadmap line refs are stale for
all four; corrected anchors are in this audit's underlying notes and should be
re-pinned in the roadmap):

| Item | Note | Size |
|---|---|---|
| **C9** — MCP payload keeps `interaction` while dropping the decompositions it duplicates; `components` lose their intervals; the how_to_read sentence overclaims. Worse than filed: `interaction` is emitted and *never mentioned* in `RCA_HOW_TO_READ` at all. | Budget the README MCP-transcript regeneration — the payload shape is pinned by `tests/test_docs_examples.py`. | 3–5h |
| **C7** — cold-start draws ignore `plausible`; no LogNormal baseline; MC mean on ratio nodes. | Truncation alone is 2–3h; the ratio-central-number question is a published-semantics decision touching API/UI/MCP. | 1–2d |
| **C13** — mock difference identity 100%-negative. | Choose subtrahend-as-share-of-minuend, not clipping — clipping yields an identically-zero series that trips C4's degeneracy guard and makes a *worse* demo. Golden re-pin expected. | 4–8h |
| **L4** — `POST /simulate` has no non-finite guard before the encoder. | A live violation of AGENTS.md rule 3, written down with a claimed structural test two commits before this audit. One zero denominator in a cold-start ratio away from a 500. | 1–2h |
| **Findings 1.3, 2.1, 2.2** — sliced-path tz coercion + invariant extension; a test for `period_mean_weights_unavailable`; simulate labelling. | §2 above. | ~1d combined |
| **The boundary structural test** (§2.5) | Do it inside this window, not after — it's the item that keeps this list from regrowing. | 4–6h |
| Mediums as time allows: 1.4 (write down or unify the slice fill policy), 3.1 (`resolve: error` in the flow query), 3.2 (MCP slice payload), 4.2 (publish `localized`). | | ~1d |

### 3.3 PyPI publication (fast follow on 0.1.0; ~0.5 day + tutorial)

Packaging is genuinely strong — trusted publishing, the no-extras CI job that
asserts named degradation, the sdist job that fails on unguarded exclusions,
wheel-content pins, single-source versioning. Three real blockers, none code:

| Item | | Size |
|---|---|---|
| **The CHANGELOG is silent on Apache-2.0 → FSL-1.1-ALv2** (changed 2026-08-13). The wheel will carry `License-Expression: FSL-1.1-ALv2` while the roadmap's principle 2 still says "the engine is the open core" — which FSL does not satisfy on the OSI reading. Settle the wording and disclose the change *before* strangers read both. | blocks | 1–2h |
| **`[project.urls]` Demo points at a dead instance** (Fly trial force-stops at 5 min; deploy workflow 6/6 failed on missing `FLY_API_TOKEN`). The PyPI sidebar is a stranger's first click. Fix the account/CI, or drop the line. | blocks | 1h or 0 |
| **No local `uv build` + `twine check` + fresh-venv sdist install has ever been run here.** | blocks (procedural) | 30m |
| **First-tree tutorial on public data** (2.6 stage 2's last piece — install guide and `docs/deploying.md` are done). The single artifact that walks a stranger from install to a tree on their own data does not exist. | embarrasses; converts | 1–2d |
| C7/C13 land before this (above) — `cold_start_tree.yml` ships *in the wheel* and is the no-credentials first-touch path; the mock is the demo. | | — |

### 3.4 Deliberately deferred (the don't-over-optimize list)

Consistent with the brief: these are real, and they should wait for the client
to make them concrete.

- **S18 as a feature** (`settlement_lag` field, bitemporal snapshots) — the
  documented discipline plus scheduled refresh covers NN; build the field when
  a second restating client shape appears. Do move the restatement warning
  into `docs/yaml-reference.md`'s seasonal section, where authors actually are
  (15 min).
- **S19 partial pooling, S20 real likelihoods** — disclosed; the NN engagement
  frame (within-cycle RCA) routes around them. Let real usage tell you which
  one bites first.
- **S-track proper (S1 full-rank ADVI first)** — the roadmap's sequencing
  (immediately after 0.1.0, before adoption items) remains right and this
  audit found nothing to reorder, with one note: S1 is a config change plus a
  benchmark and could run *concurrently* with the PyPI window if there's slack.
- **Mandatory `denominator`** — the policy doc's revisit-at-1.0 stands.
- **A docs site (3.9), the general `fill:` mechanism (2.19), LLM-assisted tree
  import (2.3b), scheduled monitoring (3.1)** — all correctly gated on demand
  that doesn't exist yet.

---

## 4. Rigor: standing assessment

Nothing in this audit changes the white paper's fair summary — *more rigorous
than the tools it competes with, less than a bespoke statistician* — but two
observations sharpen it:

1. **The honesty machinery is now ahead of its own propagation.** The engine's
   statuses, withheld intervals, and labels are the differentiator, and they
   are *implemented* — the findings here are almost all about a status not
   reaching one of its three consumers (MCP, UI, simulate), not about a wrong
   statistic. That's a much better problem than the 2026-08-05 review found,
   and it's mechanically fixable with the boundary test.
2. **One genuinely undisclosed statistical gap exists**, and it's NN-shaped:
   the Gaussian likelihood on zero-inflated series with an ELBO-only
   `fit_quality` (§3.1). Everything else in white paper §3.2 is honestly
   labelled open. Fix the disclosure now; the likelihood later.

Housekeeping the working agreements themselves require: the white paper is
stale four ways (S21 absent; "current state 2026-08-05: all items open"; "C4
blocks S17" now false; 1.11's Σnum/Σden change — which moved published numbers
for every rate — entirely unrecorded), the roadmap's four open-C line refs
have all drifted (2.16 moved `api/main.py` by ~1,100 lines), the 1.11 row
links a phantom **C21** that isn't in the Horizon 0 table, and
`grill_2026_08_12_triage.md` still lists as open a tranche that has since
mostly shipped (M1, M2, M5 confirmed landed; only L4, Op #2, Op #3 remain).
~4h total, and worth it — these documents are the product's public honesty
claim.

---

## 5. Time-to-first-trusted-RCA

Measured by walking the path, per client shape:

| Client shape | Today | Bottleneck |
|---|---|---|
| dbt + semantic models | **~1 day** | Authoring (2–4h) + iterate-on-refusals + reading 770 lines of YAML reference |
| dbt, no semantic models | **3–5 days** | Must author MetricFlow semantic models first — work outside breakdown entirely |
| No dbt | **No supported path** (unless Databricks; then ~1.5 days, *without slicing*) | `warehouse` is Databricks-only and slicing (2.8) is unbuilt there |

Only shape (a) approaches the half-day target, and nothing self-serve is
possible at all until publication. Ranked by leverage:

**Structural (the real levers):**
1. **Publish.** Every README install command is aspirational until it is.
2. **`breakdown scaffold --dbt-project <path>`** — the bridge already computes
   bindings, formula edges, inferred `kind` and `grain` per metric and holds
   them in `FormulaCandidate` objects whose own docstring says emitting YAML
   "is the scaffolder's job." The CLI has two subcommands and no way to reach
   any of it. This one command converts 2–4h of hand-authoring into minutes
   for the best-case client shape, and it's the *mechanical* slice of 2.3 —
   no LLM, no design work.
3. **The no-dbt on-ramp (2.2)** — a provider type reaching `bind:` blocks with
   no dbt project on disk. Unblocks the largest client segment; the DuckDB
   mechanism already exists.
4. **Slicing on `warehouse` (2.8)** — without it the dbt-free client gets half
   the product, and slicing is the half carrying the thesis.

**Quick wins (~1h each, do inside the 0.1.0 window):**
- `doctor` runs `fit readiness` by default over the loaded window (today it
  silently skips unless both date flags are passed — the single most
  predictive first-RCA check, off by default).
- Stop defaulting every tree's window to jaffle-shop's hard-coded 2024 dates;
  derive from `earliest_date`, which already exists, or at minimum warn when
  the default window meets a non-mock provider.
- A `bind:` reference section in `docs/yaml-reference.md` — the mechanism for
  every non-vanilla case (mixed sources, hand SQL, entity grain, the future
  CSV path) currently has no reference section anywhere; the metrics table is
  also missing `denominator`/`no_denominator` rows.
- Ship `examples/dbt_tree.yml` and `examples/warehouse_tree.yml` — the only
  real-provider tree in the repo uses the superseded `local` provider, and
  copying an example is every client's first move.
- Probe `mf` by importability, not PATH — on the repo's own pinned 3.14 it
  false-passes and then misdirects the user to a credentials remedy for what
  is an interpreter-version failure. Relatedly: `.python-version` pins 3.14,
  where the `dbt` extra and the flagship demo tree's provider cannot run;
  pin 3.13 or document loudly.
- Lift `declared dimensions exist` out of the dbt-only check set so
  `cloud`/`local` trees get it (dimension identifiers are documented as "not
  guessable," so typos are the expected case).
- Re-run `translate()` against the White Cube manifest to close 2.19's
  explicitly-unmeasured claim about what the `fill_nulls_with: 0` acceptance
  recovered.

---

## 6. Recommended sequence

1. **Now → NN deployment:** §3.1's six items (~2–2.5 days). Nothing else.
2. **NN → 0.1.0:** §3.2 (~1 week), including the boundary structural test and
   the quick wins from §5 that touch trust (`fit readiness` default, default
   window, `mf` probe). Docs housekeeping (§4) rides along.
3. **0.1.0 → PyPI:** §3.3's three blockers same-day; tag; publish; tutorial as
   the first post-publication item, informed by whatever NN's onboarding
   actually surfaced.
4. **Post-PyPI:** S1 benchmark (the S-track's scheduled start), then the two
   structural onboarding levers (scaffold command, then 2.2) in whichever
   order the next prospect's shape dictates — which is the roadmap's own
   principle 1, applied.

The order matters for the reason the user's brief gives: NN will resurface
this list reordered. Everything in phase 1 is work that makes the client
deployment *legible* — the tool telling the truth about itself — which is
exactly the property that lets real-world feedback be trusted when it arrives.

---

## Addendum: what shipped, and the read-the-numbers record (2026-08-17)

Phase 1 and part of phase 2 shipped the same day this report landed: **2.20**
(containment snapshot reads + doctor sees snapshots), **C12** (weight grain at
parse time, with the policy fork resolved in writing), **C23** (the sliced
path joins the date contract; `_pivot` refuses duplicates; the fill divergence
chosen in writing and logged), **C24** (the localization verdict published and
reachable for rates, plus the MCP slice payload trio), **C25** (simulate
labels + the non-finite refusal), the **S20 disclosure half**
(`likelihood_warnings`), and the two 3.8 follow-ups (`resolve: error` refuses
flows; doctor separates cannot-check from violated). Roadmap rows carry the
details. The client-facing expectation brief was written and is kept with the
engagement materials rather than in this public repo. C9 and C13 shipped
later the same day, and C7 closed the same evening once the author chose
option 1 (§ "The C7 decision") — **Horizon 0 is closed, every row ✅.** Still
open from this report's lists: the PyPI blockers.

Verified per [`read-the-numbers`](../.claude/skills/read-the-numbers/SKILL.md),
2026-08-17, suite green (1102 passed before the C24+ batch; affected suites
re-run green after):

> **White Cube** (snapshot-served, provider `/nonexistent` by design). Story A
> `new_mrr` ref 2026-01-05→02-01, an 2026-02-09→03-08: 1701.458 → 1449.625,
> gap −251.833 (−14.8%), `unexplained` −2.3e-13 `measured`;
> `new_subscriptions` −324.8 share 1.290 ci [−534.8, −76.8] psd 0.998;
> `new_arpu` +73.0 share −0.290 ci spans zero, psd 0.708; `ranked_causes[0]`
> `new_subscriptions` 0.633 via `new_mrr` — byte-matches the 2026-08-13
> record. **The change probes:** `customer_churn_rate` by country over the
> same windows publishes `localized: true`, and the UI renders *"BR carries
> 55.1% of the gap on a 9.6% baseline share"* — the sentence that was
> structurally unsayable for every rate before C24 — with noise chips on
> US/FR/ES and the additivity caveat beneath; the flow slice
> (`new_subscriptions` by plan) correctly withholds (leader noise-level).
> `/simulate` +10% `new_subscriptions`: every rate carries
> `window_aggregate: "components"` on both node shapes, `new_mrr`'s simulate
> baseline equals the RCA's to the decimal, propagation +10% → net-new +16.3%;
> intervening on `customer_churn_rate` +10% renders *component aggregate*
> under 0.0101 → 0.0111 in the table and paints the churn side worsening.
> **Doctor** on the same deployment: `[WARN] dbt project … not fatal here:
> every metric is snapshot-covered`, `[PASS] snapshots — all 18 sourced
> metrics covered`, fit readiness proves 18/18 through the wrapped path,
> **exit 0** (was `[FAIL]` + exit 1). **Containment:** re-serving with
> `--end-date` narrowed one week boots healthy against the unreachable
> provider — 32 "served from stored window … trimmed" hits, `data_through`
> 2026-07-19 — a boot that was impossible before 2.20a.
>
> **B2B MRR** (106 metrics, mock): boots with the known identity-departure
> warnings (the C13 class, expected); `contract_renewal_rate` by segment over
> two 6-month windows: `localized: false`, every slice noise-level,
> `baseline_share` present, windows snapped to whole months, reconciliation
> ok; a day-grain flow slice localizes. Skipped, with reasons: a whole-tree
> B2B RCA (fit-heavy; slicing was the changed surface), the export-HTML diff
> (the live panel was verified; the export shares `caveatBlock`), and a live
> S20 probe (no shipped tree has a zero-inflated node — the unit and
> fit-integration tests carry it).

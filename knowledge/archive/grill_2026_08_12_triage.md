# Grill triage: what survived, what didn't, and what we're doing about it

> **Archived 2026-08-17.** This document did its job: of the 28 findings it
> confirmed, everything is either shipped or carried on the
> [roadmap](../roadmap.md). Tranche 3, listed as open below, has since mostly
> shipped — C4/C5 (2026-08-13), M1/M2/M5/M6 (`7d4126b` and C6), the UI pass
> (`2d35c19`), and the four written-down rules with their structural tests
> (AGENTS.md). The three findings still open at archival were carried forward
> as roadmap rows the same day: **L4** (the `/simulate` non-finite guard) into
> [C25](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)(b),
> and **Operational #2/#3** (exact-window snapshot reads; `doctor` blind to
> snapshots) as
> [2.20](../roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard).
> Operational #4 (the UI's `isTransient` misdiagnosis) shipped with the CDN
> `integrity` hashes. What this file is still for is what its own status note
> says: *how each finding was verified*, and what verification corrected about
> the original report. The status tables below are frozen as written.

**Companion to** [`grill_2026_08_12.md`](../grill_2026_08_12.md), which is frozen at
`c18d150` and explicitly says so. This document re-checks every one of its
findings against **`e433daa`** (`feat/multi-tree`, 22 commits later, 745 tests
passing in 2m35s) and turns the survivors into a plan.

**Same two gates:** first client deployment · PyPI publication as
`metric-breakdown`.

> **Tranches 1 and 2 shipped 2026-08-12**, the same day this triage was written.
> Tranche 1 — H1, H2, H3, H4-sizing, H5, H6, M3, M4, M7, L3, the `/dag` exposure
> and opt-in full auth. Tranche 2 — H7, H8, M8, M9, L2, L5, L6, L7, L8, L9, L10,
> the CI format gate, and the README parse-and-replay test that should have
> existed. Suite 745 → **836 passing**. Status now lives on the roadmap:
> [C17](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) (H1),
> [C18](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend) (M4),
> [2.18](../roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) (the
> rest), and an amendment to
> [2.10](../roadmap.md#horizon-2--make-it-repeatable-a-stranger-can-onboard) for the
> BigQuery grain defect. Tranches 2 and 3 are open. The plan below is left as
> written rather than ticked off, because the *reasoning* is the part worth
> keeping — same principle the frozen review is kept under.
>
> **One correction to this document's own filing.** The plan below put H6 in
> Horizon 0 alongside H1 and M4. That was wrong on the horizon's own test: an
> unbounded cached window is an availability defect, not a number the engine
> cannot defend. It shipped under 2.18 instead. Recorded rather than edited
> away, because misfiling it is exactly the error the horizon's "punch list with
> an end" rule exists to prevent.

> **Status discipline.** As with the grill itself, this is a **snapshot**. The
> [roadmap](../roadmap.md) remains the source of truth for what is open and what is
> shipped. What this document is for is the part the roadmap deliberately does
> not carry: *how each finding was verified*, what the verification **corrected**
> about the original report, and why the fixes are sequenced the way they are.

**Method.** Reproduced, not read. Every "confirmed" below was demonstrated by
executing code — through the HTTP surface where the finding is an HTTP finding,
through the engine where it is an engine finding. Where a reproduction changed
the story, the correction is recorded rather than quietly folded in.

---

## Summary

| | Count |
|---|---|
| Fixed since `c18d150` — do not re-spend | 3 |
| Confirmed live, **materially worse** than reported | 4 |
| Confirmed live as reported | 24 |
| Confirmed but already an open roadmap row (not new work) | 4 |

Nothing in the original report was found to be *wrong*. Three findings were
overtaken by shipped work; four were understated.

---

## Fixed since `c18d150`

| Finding | Where it went |
|---|---|
| **C1** (doc's) — dbt `filter` silently dropped | Shipped as roadmap [C15](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend). Verified in `dbt_manifest.py`: `_unsupported_semantics` runs before dispatch, all four `WhereFilter` serialisations normalised |
| **C2** (doc's) — snapshot key ignores the definition | Shipped as roadmap [C16](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend). Verified in `snapshots.py`: `definition_sha` compared on read, sliced path covered |
| **L1** — NaT-producing dates are an unhandled 500 | **Fixed.** `rca.py:137` (`_validate_windows`) rejects `2024-02-30` with a 422 naming the field. Reproduced through `POST /rca`: `{"detail": "analysis_start is not a valid date: '2024-02-30'"}` |

And one **half**-fixed, which is its own row below: **H4**.

---

## Confirmed live, and worse than reported

These four are the reason this document exists. In each case the reproduction
found a bigger or differently-shaped defect than the finder described.

### H1 — the failure is a crash *before* it is a NaN

The report predicted: zero denominator → NaN through the attribution → Starlette's
`allow_nan=False` encoder → unhandled 500. That is real, and it is the *second*
thing that happens.

The first is that `eval_formula` (`formula.py:50`) evaluates under
`{"__builtins__": {}}`, and numpy's divide-by-zero **warning** machinery needs
`__import__` from the eval globals. So the very first zero denominator raises:

```
File "breakdown/engine/rca.py", line 381, in shapley_attribution
    actual = float(eval_formula(defn.formula, an_daily).mean())
File "breakdown/formula.py", line 50, in eval_formula
    return eval(formula, {"__builtins__": {}}, values)
KeyError: '__import__'
```

Reproduced end to end: a three-node tree (`aov = revenue / order_count`) with a
single zero-denominator day planted inside the analysis window returns a bare
**500** from `POST /rca/aov`, with `KeyError: '__import__'` in the log and
nothing in the response. Suppress the warning (`np.errstate`) and the same
request 500s again — that is the NaN-to-encoder path the report described.

Three consequences the original write-up did not have:

1. **It is two fixes, not one.** Sanitizing non-finite values does not help while
   `eval_formula` cannot survive producing one.
2. **The blast radius is wider than RCA.** `eval_formula` is also on
   `fit_metric`'s formula-residual path (`model.py:318`) and in `simulate.py`, so
   `POST /analyze` and `POST /simulate` on the same node crash the same way.
3. **The diagnostic is worse than useless.** `KeyError: '__import__'` names
   nothing about the tree, the node, or the zero. An analyst self-serving into
   this has no thread to pull, and neither does whoever they ask.

Generalized: **any** numpy warning raised inside a formula — overflow, invalid
operation — becomes `KeyError: '__import__'`. The restricted-globals sandbox is
correct in intent and is silently coupled to numpy's warning path.

### H4 — enforcement fixed, sizing worse

The **enforcement** half is fixed, and by unrelated work: 2.16's `TraceView`
(`api/trees.py:91`) evicts through the process-wide `TraceStore` on *every*
`__setitem__`, so `rca.py:500` and `simulate.py:424`'s bare
`traces[key] = fit_metric(...)` are now bounded without either call site
changing. The report's "drove the cache to 261 entries" path is closed.

The **sizing** half is not, and is worse than reported. Measured here on the
demo's own 830-day window: one ADVI trace is **13.4 MB** (the report measured
6.66 MB), so a full cache is 256 × 13.4 ≈ **3.4 GB** against `demo/fly.toml:87`'s
`memory = "2gb"`. The conclusion the report drew still holds and is now sharper:
an entry-count cap cannot be made safe by tuning the count, because the entry
size scales with the loaded window. The bound has to be a byte budget.

### M4 — leading gaps are filled with *no warning at all*

Filed as medium; it belongs with the correctness items. `_align_to_spine`
computes `interior` as `s.isna() & (s.index > first_valid_index)`, so periods
*before* the source's first row are outside `interior` by construction — they
are zero-filled by the `kind == "flow"` branch and **nothing logs**. Interior
gaps warn, name their periods, and say "a gap in the source is not the same as a
zero." Leading gaps get none of that.

Reproduced: a source whose first row is `2024-01-20`, fetched from `2024-01-01`,
returns 22 rows of which **19 are fabricated `0.0`** and no warning is emitted.
The fit then trains on 19 fake zeros, which is a manufactured level shift and a
manufactured trend, on a node the tree will happily rank as a cause.

This is the *silent wrong number* class that Horizon 0 exists for, and it is the
single most likely thing to bite a **first** client, whose metrics are exactly
the ones that start partway into the loaded window.

### The auth exposure — not underdocumented, undocumented

The report says the README's API reference doesn't mention authentication. It is
stronger than that: **`BREAKDOWN_API_TOKEN` appears zero times in the README.**
The only access control in the product is documented nowhere at all.

Reproduced with the token set: `GET /series`, `GET /dag` and `GET /meta` all
return **200** unauthenticated, and `/dag` serializes the full
`MetricDefinition` per node — `sql` and `bind` included:

```
/dag node keys: ['baseline', 'bind', 'description', 'dimensions', 'direction',
 'expected_signs', 'format', 'formula', 'grain', 'kind', 'lags', 'name',
 'parents', 'plausible', 'priors', 'seasonality', 'source', 'sql', 'trend']
```

One useful new fact for the fix: the UI's *show query* panel reads
`/metrics/{name}/query`, **not** `/dag` (`app.js:93`), so redacting `sql`/`bind`
from `/dag` costs the UI nothing.

---

## Confirmed live, as reported

### High

- **H2 · formula attribution is O(2ⁿ) with no cap.** Measured on this machine:
  8 parents 0.6s · 10 parents 3.5s · **12 parents 19.7s** — a clean doubling per
  parent, so 14 is ~80s and 15 is ~160s, all of it holding the tree's lock.
  `simulate.py:65` caps the identical enumeration at `_MAX_SOURCES = 10`; the
  formula path still has no equivalent and no documented limit.
- **H3 · BigQuery `DATE_TRUNC` arguments reversed at day and month grain.**
  Confirmed by emitting: `_truncate('order_date', 'day', 'bigquery')` →
  `DATE_TRUNC('DAY', order_date)`, and month likewise. Only `("bigquery", "week")`
  has an override. Blocks a BigQuery client at two of three grains; the dialect
  tests still assert on emitted *text* only, which is the blind spot that let it
  through.
- **H5 · `GET /metrics/{name}` summarizes on the event loop.** Still the one
  heavy engine call in `api/main.py` not wrapped in `asyncio.to_thread`
  (`:1005`). Measured 1.1s of event-loop block per call on an 830-day ADVI trace
  — on **every** call, since nothing memoizes the summary of an immutable trace
  — and it scales with `draws`.
- **H6 · unbounded caller-controlled slice window, cached forever, even on 422.**
  Reproduced: `reference_start=1900-01-01 … analysis_end=2100-12-31` on the
  reference tree returns **422** and leaves a permanent `slice_cache` entry of
  9,648 rows / 435 KB keyed `('total_mrr', …, '1900-01-01', '2100-12-31')`. The
  endpoint still validates only that the four dates *parse*.
  `TreeState.slice_cache` and `.flow_cache` (`api/trees.py:128-129`) are plain
  dicts with no cap, no TTL and no eviction anywhere in the package.
- **H7 · the README's "real, unedited" MCP transcript is unreproducible and
  inverted.** Reproduces the report's numbers exactly. Over the transcript's own
  windows the bundled tree now says revenue **rose 2.26%**, $26,386.5 →
  $26,982.1/day; the README says it fell 1.4%, $735.7K → $725.2K. AOV
  $184.68 → $182.15, not $5,132 → $4,879. `order_count` (142.7 → 148) is the
  only figure that survived.
  **New, and worth its own note:** the same run ranks `order_count` **first at
  score 1.0** on a metric that rose, because its `share_of_gap` of 1.65 is
  clamped by `min(abs(share), 1.0)`. That is [C5](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend)
  reproducing on the **bundled demo tree**, which makes it a free regression
  fixture — C5's row currently has to construct one.
- **H8 · README "YAML reference" examples don't parse.** Five blocks still fail
  on the required `source` field: README `:536`, `:563`, `:649`, `:677`, `:718`.
  The `:649` block is still the one prescribed as the *fix* for rates over
  true-zero denominators, and its `# derived, not fetched` comment is still
  false — `_fetch_all_metrics` (`api/main.py:147`) iterates
  `parser.config.metrics` unconditionally and asks the provider for formula
  nodes like any other.

### Medium

- **M1 · the auto-defaulted reference window ignores declared lags.**
  `default_reference_window` clamps to `data.date_start` only; `_validate_coverage`
  then checks lagged parents on their *shifted* windows and refuses. Short-history
  trigger — a new client's first weeks.
- **M2 · `MIN_FIT_PERIODS` isn't enforced on the default fit path.** Reproduced: a
  **3-observation** series fits and reports `fit_quality: "ok"`. The floor is
  checked only when `fit_end` is passed (`model.py:531`) or lags exist (`:303`) —
  neither is true for `POST /analyze`'s or `run_scenario`'s default. `doctor.py:771`
  calls the same metric unfittable.
- **M3 · one zero-variance series aborts the entire tree RCA.** Reproduced: a
  parent held identically at zero (unlaunched feature — a seasonal business's
  default state) makes `_normalize` raise, the exception escapes `run_rca`'s fit
  loop, and the whole analysis 422s. `nodes_out` already carries a per-node
  `status` used to degrade gracefully for `window_shorter_than_grain`.
- **M5 · `_to_naive_dates` crashes on per-row timezone offsets.** Reproduced:
  a frame whose rows straddle a DST boundary raises `ValueError: Mixed timezones
  detected`. The C1 guard handles a *uniform* zone and cannot see this one.
- **M6 · duplicate metric names silently merge into one DAG node.** No uniqueness
  validator exists in `parser.py`. This is roadmap [C6](../roadmap.md#horizon-0--correctness-numbers-the-engine-cant-defend), still open.
- **M7 · an out-of-range `/rca` window pays for every ancestor's ADVI fit before
  returning 422.** Confirmed structurally: `run_rca`'s `to_fit` loop
  (`rca.py:497`) runs before `_validate_coverage`, which is reached per node in
  the attribution loop below it.
- **M8 · `compose.yaml` passes neither `BREAKDOWN_PUBLIC_URL` nor
  `BREAKDOWN_API_TOKEN`.** Unchanged.
- **M9 · the README's flagship tree trips breakdown's own parse-time lint.**
  Confirmed by parsing it: `average_order_value` still lacks `kind: rate`, so the
  headline example warns the reader that their tree is wrong.

### Low

All ten unchanged except **L1** (fixed, above). Sharper readings on three:

- **L5** — `tests/test_reference_tree.py:18` hard-codes `knowledge/b2b_mrr_tree.yml`,
  which `pyproject.toml`'s sdist `exclude` drops. Confirmed there is still **no CI
  job that builds or installs the sdist** — `ci.yml` has `lint`, `test`,
  `base-install` and `wheel`, all wheel-based.
- **L8** — worse than "missing from the table": `GET /metrics/{name}/query`, the
  entire 2.11 query-provenance surface, is documented **nowhere in the README**
  (zero occurrences). `/health`, `/rca/{name}/slices`, `/progress/{run_id}` and
  `/mcp` are described in prose but absent from the route table.
- **L10** — "Project structure" has improved (it now lists `trees.py`, `slices.py`,
  `mcp/`) and still omits `dbt_bridge.py`, `dbt_manifest.py`, `dbt_provider.py`,
  `dbt_sql.py`, `snapshots.py`, `grains.py` and `engine/simulate.py` — i.e. the
  whole dbt provider and the what-if engine.

### Operational

All four unchanged. `SnapshotStore.read` still requires an exact window match
while `read_sliced` documents why trimming a containing window is right;
`doctor.py` still calls `_build_fetcher` directly and the word "snapshot" still
appears nowhere in it; the UI's `isTransient` (`app.js:163`) still classifies any
error without a `.status` — including a `ReferenceError` from a CDN-blocked
`cytoscape` — as a dropped connection, behind four unpinned scripts from three
CDNs with no `integrity` and no bundled fallback.

---

## Found while fixing, not by either review

Two defects surfaced only once someone had to render the new failure states, and
both are the project's own honesty posture inverted in the one surface a client
actually looks at:

- **`applyRcaOverlay` tinted a node on `node.gap >= 0`, and `null >= 0` is
  `true` in JavaScript.** So a `window_shorter_than_grain` node — one the engine
  explicitly declined to analyze — had always rendered **green, with an upward
  arrow**, i.e. as an improvement. This has been live since that status was
  introduced. Neither review found it, because both read the engine and the
  payload, and the payload was correct: the lie was manufactured in the
  presentation layer from an honest `null`.
- **`ci_status: "posterior_only_single_period"` was never rendered.** The engine
  withholds an interval, `docs/model.md` documents that it does, and the UI
  showed nothing — which reads as an interval that was checked and found fine.

The general lesson is worth more than either fix: **the audit swept the engine,
the providers, the API, packaging and the docs, and stopped at the UI.** Every
number this project is careful about is read by a person through `app.js`, and a
correct payload rendered dishonestly is indistinguishable, to that person, from a
dishonest payload. A UI pass in the register of the engine review is not on any
tranche below and probably should be.

## What Tranche 2 turned up that neither review saw

The tranche's own thesis was that the README drifted because nothing executed
it. Writing the test proved the thesis twice over, on defects nobody had found
by reading:

- **`GET /` was documented nowhere.** Not in the route table, not in prose — it
  appears only in the Authentication allow-list. Found by an assertion that
  every `@app.*`/`@router.*` path in `api/main.py` appears in the reference
  table, which is the mechanical form of the L8 finding rather than its
  hand-checked one.
- **The transcript's `report_url` was a link the server no longer mints.**
  `run_rca` passes `tree=state.id`, so every link now carries `#tree=` — which
  the README *says itself two paragraphs above* while printing a link without
  one. Two true statements, adjacent, contradicting each other; a reader would
  have believed the printed link.
- **`test_reference_tree.py`'s tree path was CWD-relative**, so it failed for
  anyone running pytest from outside the repo root — a second latent bug sitting
  underneath L5's sdist one, invisible because everyone runs pytest from the
  root.
- **`.dockerignore`'s `__pycache__` and `*.pyc` lines matched nothing**, being
  root-anchored in a file where that is not the default reading.
- **`CLAUDE.md` shipped in the sdist as a symlink**, and **`.python-version`
  shipped too** — a `uv` pin for this checkout, silently narrowing what
  `requires-python` promises anyone building from source.
- **`.gitignore` had the same hole as `.dockerignore`**: an operator following
  the deploy recipe inside a clone and running `git add -A` commits their tree
  and warehouse snapshots. Fixed on both sides.

**The one that generalizes** is `C5`. Regenerating the transcript found it live
on the *bundled demo tree*, over an ordinary window, with a gap nowhere near
zero — the roadmap row had framed it as a near-zero-gap defect, and the row is
now amended. Two hostile reviews and a triage pass all had the C5 mechanism in
hand and none of them ran the demo and looked at the top number. That is the
same lesson as the UI findings above, in a different register: **reading the
code finds mechanisms; running the product finds which ones matter.**

## The pattern, restated

The grill's closing argument was that a policy gets chosen carefully in one file
and not propagated to its neighbours, and that **three findings were the same
defect the author had already fixed one file over**. Re-checking has not weakened
that; it has added a fourth axis:

| Policy | Where it is right | Where it is absent |
|---|---|---|
| Refuse rather than approximate | `dbt_sql.py`, and now `dbt_manifest.py` (C15) | `_align_to_spine`'s leading zero-fill (M4) |
| Bound every cache on the tree's state | `traces`, via `TraceStore` (C8, 2.16) | `slice_cache`, `flow_cache` (H6) |
| Sanitize before the encoder | `slices.py:530-534` | `rca.py` (H1) |
| Cap the coalition enumeration | `simulate.py:65` | `compute_shapley` (H2) |

The fourth row is new to this pass and is the same shape as the other three: the
`_MAX_SOURCES = 10` cap exists, is correct, is justified in a comment, and sits
in the file *next to* the unbounded copy of the same loop.

---

## Plan

### Tranche 1 — before the client

1. **H1, both halves.** Make `eval_formula` survive numpy's warning path, *then*
   add the `np.isfinite` guard that `slices.py:530` already documents — in
   `shapley_attribution`, in the contribution `_summary`, and in `_rank_causes`
   (`min(abs(nan), 1.0)` is `nan`, so the NaN escapes the existing clamp). A
   non-finite result withholds the number and sets a node `status`; it never
   reaches the encoder.
2. **One pass over the request lifecycle** — H2, H5, H6, M7 are one shape:
   a parent-count cap in `compute_shapley` mirroring `_MAX_SOURCES`;
   `asyncio.to_thread` plus memoization of `summarize_trace` on the immutable
   `FitResult`; slice windows clamped to the loaded data *before* the fetch, and
   `slice_cache`/`flow_cache` bounded; `_validate_coverage` moved ahead of
   `run_rca`'s fit loop.
3. **M3 and M4** — the two ways a seasonal business's ordinary data breaks the
   engine. Route `fit_metric`'s `ValueError` into the per-node `status` channel
   that already exists; warn on leading gap-fill and count the fabricated
   periods where a reader will see them.
4. **The auth surface.** Redact `sql`/`bind` from `/dag` (the UI does not read
   them), fix the non-ASCII `Authorization` 500 (L3), and document
   `BREAKDOWN_API_TOKEN` and what it does and does not gate. **Whether the token
   should gate the data routes at all is a product decision, not a defect fix**
   — gating them closes the exposure and breaks the open-UI deployment the
   current docstring describes, so it is called out rather than assumed.
5. **H4's sizing half.** A byte budget rather than an entry count.
6. **H3**, and with it a `sqlglot`-executes-the-SQL test rather than another
   text assertion — text assertions are precisely what let this through.

### Tranche 2 — before PyPI

7. **Write the test first:** one test that parses every YAML block in the README
   and issues every documented curl example. It fails on H8 and M9 the moment it
   exists. Four separate findings in the grill are the same drift, and nothing
   catches it.
8. Regenerate the H7 transcript against the current tree — or replace it with a
   generated, tested excerpt, since a hand-copied transcript is what drifted.
9. L5 (ship `knowledge/` in the sdist *or* skip those tests when it is absent —
   plus a CI job that builds and installs the sdist), then L9, L7, L8, L10, L2,
   L6, M8.
10. **CI lints but does not check formatting.** `ci.yml`'s lint job runs
    `ruff check .` only, while AGENTS.md says the codebase is ruff-*formatted* —
    so five files have drifted out of format with nothing to catch it
    (`breakdown/mcp/server.py`, `demo/prewarm.py`, and three test modules, all
    untouched by this work). Add `ruff format --check .` to the same job and
    reformat once. Trivial, and it is the same shape as everything else in this
    tranche: a standard that is written down, believed, and unenforced.

### Tranche 3 — the class fixes

10. **C4 and C5**, the numbers a client reads first. C5 now has a live fixture on
    the bundled demo tree (see H7), which is cheaper and more honest than a
    constructed one. Then M1, M2, M5, M6 (= C6).
11. **A UI pass in the register of the engine review.** Added after Tranche 1,
    not planned before it: fixing the new failure states turned up two live
    honesty inversions in `app.js` that neither review could have found, because
    both stopped at the payload (see *Found while fixing* above). The engine's
    care about `null`, withheld intervals and unexplained residue is only worth
    what the rendering preserves, and nothing checks the rendering. There is no
    JS test runner and that is a deliberate MVP-first choice, so the instrument
    here is a reviewer with the payload contract in hand, not a harness.
12. **Write the rule down**, with a test per clause: *the provider boundary
    refuses rather than approximates; every cache on `TreeState` is bounded; no
    engine result reaches an encoder unsanitized; every coalition enumeration is
    capped.* That page plus four tests would have caught H1, H2, H4, H6, M4 and
    M5 as a group.

---

## What each fix owes the docs

Per [AGENTS.md](../../AGENTS.md), and recorded here so it is not rediscovered per PR:

- **Roadmap rows** are the source of truth for status. The Horizon 0 rule is that
  rows are added only for defects found while closing it — with C15/C16 as the
  stated exception for release-gate review findings. **H1 and M4 are the same
  exception** (shipped as C17 and C18): each hands someone a number the engine
  cannot defend, from a documented entry point, found by the same review against
  the same two gates. H2, H3, H5, H4-sizing, H6, M2, M3 and the auth work are
  availability, performance or product decisions rather than wrong numbers, and
  belong in their own horizons — 2.18 in the event. *(This bullet originally
  listed H6 in Horizon 0; see the correction at the top.)*
- **`docs/model.md`** carries two `**Caveat (open, roadmap Cn)**` markers today,
  for C4 and C5, and both match open rows — the sweep is clean. Anything shipped
  from Tranche 3 must **delete** the matching caveat, not amend it.
- **The white paper** cites C4, C5 and C7 by ID in §3.2, and §3.3 covers the
  provider-boundary defects. M4 is a provider-boundary defect and belongs in
  §3.3's reading when it lands.
- **The README** owes: `BREAKDOWN_API_TOKEN`, `GET /metrics/{name}/query`, the
  four routes missing from the table, and the eight modules missing from the
  structure.

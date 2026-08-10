# Reference-window defaulting and history discovery

Design spec for roadmap item **1.9**. Status lives in the
[roadmap](roadmap.md); this document records the problem, the decisions, and
the rationale.

## The problem

RCA requires four dates. Two observed failure modes, both rooted in one
misconception:

1. **Users conflate the reference window with the training window.** The
   instinct "make the reference as long as possible so the BSTS model has data
   to fit" is wrong twice over: the fit window is *already* all loaded history
   before `analysis_start` (`fit_end=analysis_start` in `run_rca`), so the
   reference has no effect on fit quality — and a very long reference is
   actively bad *as a baseline* (below). Nothing in the product said either
   thing, so users limited the analysis trying to help it.
2. **The thing that should be maximized — loaded history via `--start-date` —
   had no guidance and no visibility.** No provider exposed an earliest
   available date, so the app could never say "18 more months exist upstream;
   widen `--start-date`."

## Decisions

1. **Default reference policy: the matched adjacent block**
   (`default_reference_window` in `grains.py`). When both reference dates are
   omitted, the reference is the block ending the day before `analysis_start`,
   sized `REFERENCE_MULTIPLE` (4)× the analysis length, floored at
   `MIN_REFERENCE_DAYS` (28), rounded up to a whole-week length when any node
   in the target's ancestor scope declares seasonality, extended backwards (when
   the data allows) to hold at least one whole period at the coarsest grain in
   scope, and clamped to the loaded data range.
2. **The default lives in the engine and flows through API + UI.**
   `reference_start`/`reference_end` are optional (keyword-only) on
   `run_rca`/`shapley_attribution` and on `POST /rca/{name}`,
   `GET /shapley/{name}`, `POST /rca/{name}/slices`, and the MCP `run_rca`
   tool. Responses echo the resolved `reference_window` and
   `reference_defaulted`. The UI is analysis-first: the user picks the
   analysis window; the reference auto-fills and stays editable.
3. **Provider history discovery.** `BaseDataFetcher.earliest_date(metric,
   grain)` — a capability, not a contract: it may return `None` ("can't say")
   and must never raise. Surfaced through `GET /meta` (`earliest_available`),
   a doctor "history headroom" check, and a UI nudge to widen `--start-date`.

## Why not "all history before the analysis window" as the reference

- The gap is a **window-mean difference**. On any trending metric, a long
  reference mean sits far from the current level, so the gap becomes "current
  vs long-run average" and the trend component (probabilistic nodes) or the
  fastest-growing parent (formula nodes) absorbs it. Technically coherent,
  useless for incident RCA. [`demo_guided_tour.md`](demo_guided_tour.md)
  documents the live version: an eight-week gap between windows carries ~25%
  underlying growth and would have credited volume rather than the change.
- The block bootstrap assumes rough stationarity **within** each window; a
  months-long reference on a growing business violates it structurally.
- The thing "all history" is intuitively buying — a better-informed model — is
  already bought: the fit uses all loaded history no matter what.

## Why 4× / 28 days / adjacent / whole weeks

- **Adjacent** (ends the day before `analysis_start`): no gap for trend to
  accumulate in; the baseline describes the regime the analysis window
  departed from.
- **~4× the analysis length**: the reference mean's sampling noise shrinks
  like 1/√periods, so a reference several times the analysis length makes the
  baseline side of the gap comparatively stable without reaching into a
  different regime. 4× matches what the shipped presets already encoded
  (7d vs prior 28d).
- **28-day floor**: keeps short analysis windows from inheriting equally short,
  noisy references, and reduces (does not fix — roadmap C4) exposure to the
  bootstrap's short-window attenuation.
- **Whole-week length when seasonality is in scope**: an unbalanced weekday
  mix manufactures a real seasonal gap that is nobody's fault
  ([`docs/model.md`](../docs/model.md), window composition bias). Length is
  rounded to a 7-multiple; per-node Monday alignment stays `snap_window`'s job.
- **Coarse-grain extension**: a 28-day block can contain zero whole months;
  when a month-grain node is in scope the block reaches back to cover one —
  only when the loaded data can actually supply it.

## Edge cases (tested in `tests/test_grains.py`)

| Case | Behavior |
|---|---|
| Analysis starts at `date_start` | `ValueError` → 422: pass explicit refs or load more history |
| History shorter than the target length | Clamp to `[date_start, an_start−1]` |
| Clamped + week-align, ≥7 days left | Trim **down** to whole weeks ending at `an_start−1` |
| Clamped + week-align, <7 days left | Keep the stub; UI advisory owns the warning |
| Month grain in scope, no whole month in block | Extend back to cover one, data permitting; else stand and let nodes report `window_shorter_than_grain` |
| Exactly one reference date passed | `ValueError` → 422 ("both or neither") |
| Both passed | Byte-identical to previous behavior; `reference_defaulted: false` |

## Interplay with open statistical items

- **C4** (bootstrap short-window attenuation): unchanged; the 28-day floor
  narrows how often defaulted references sit in its worst regime. The
  `docs/model.md` caveat now says so.
- **S15** (multiplicity/selection): defaulting *reduces* the free-choice
  surface — a defaulted reference is one fewer knob to retry — but the window
  pair remains a search parameter; S15's disclosure obligation is untouched.

## Rejected alternatives

- **All-history reference** — see above.
- **Same-length adjacent reference** (pure week-over-week): short analysis
  windows produce equally short, noisy references; the 4×/28d shape dominates it.
- **Per-node clamping** of the default to each node's own data range: the
  tree-wide `GrainedData.date_start` is what `/meta` exposes, so UI and engine
  agree exactly; a node whose own frame starts later still fails loudly via
  `_validate_coverage` with a node-named message (pre-existing behavior with
  hand-picked windows). Scope-aware clamping is the follow-up if it bites.
- **Synchronous history discovery at startup**: one provider round-trip per
  metric would roughly double cold startup; discovery runs as a background
  task and `/meta` reports what it has.
- **Snapshot caching of `earliest_date` probes**: one probe per startup
  accepted for the MVP; manifest caching is a noted follow-up.

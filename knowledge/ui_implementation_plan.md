# UI Implementation Plan

Companion to `ui_design_spec.md` (the what & why) — this is the how. Tickets U1–U6,
ordered by value and dependency. Written to be executed by a code assistant one
ticket at a time; design decisions are already made — do not re-open them.

## Conventions for every ticket

- **Stack budget:** vanilla JS in `static/app.js`, one stylesheet
  `static/style.css`, one `static/index.html`. No framework, no build step, no new
  dependencies beyond what's on the CDN list (U2 *swaps* a CDN build).
- **Verification:** there is no JS test infra; each ticket ships with a manual
  acceptance checklist. Verify with the app running against mock data:
  `uv run python main.py serve`, open `http://localhost:9090/ui`. The browser console
  must be free of errors/warnings introduced by the change. Where a checklist item
  says "assert", it means observe and confirm.
- **Backend coordination:** statistical-plan tickets T5–T7 are being implemented in
  `breakdown/engine/` concurrently. UI tickets **must not edit files under
  `breakdown/engine/`**. U4 requires the T5/T7 response fields and U5 makes a small
  `breakdown/api/main.py` addition — both are gated: implement them only when
  `git log` shows the T5–T7 block committed. U1–U3 have no backend dependency and
  come first.
- **Schema tolerance:** the engine gains fields in stages (T5/T7 now, T8 later).
  All rendering must degrade gracefully: `undefined`/`null` field → omit the
  element, never print "undefined" or throw. Use the existing `fmt`/`pct` null
  handling as the pattern.
- **Style:** match the existing code — module-level functions, `$()` helper,
  template literals with `esc()` on all interpolated server strings, CSS tokens in
  `:root`. Update `docs/ai-context/frontend-ui.md` whenever a ticket changes layout,
  visual language, or consumed API fields (each ticket lists its doc touchpoints).

---

## U1 — Window presets, client-side validation, copy link

**No backend dependency. Files:** `static/index.html`, `static/app.js`,
`static/style.css`, `docs/ai-context/frontend-ui.md`.

**Changes.**

1. **Preset select.** In `index.html`, add before the Reference control group:

   ```html
   <div class="control-group">
     <label for="win-preset">Windows</label>
     <select id="win-preset"></select>
   </div>
   ```

   In `app.js`, add a `WINDOW_PRESETS` list; each preset is
   `{ id, label, compute(startDate, endDate) -> {refStart, refEnd, anStart, anEnd} | null }`
   (return `null` when the data window is too short for the preset; such presets are
   omitted from the select). Presets, in order:
   - `last7-prior28`: analysis = last 7 days of the data window; reference = the 28
     days immediately before that. Needs ≥ 35 days.
   - `last14-prior28`: analysis = last 14 days; reference = prior 28. Needs ≥ 42 days.
   - `weeks-1v4`: analysis = last full Monday–Sunday week fully inside the data
     window; reference = the 4 full weeks before it. Needs ≥ 5 full weeks. (Compute
     Mondays via `Date.getUTCDay()`; all date math in UTC to match the ISO strings —
     reuse the existing `iso()` helper pattern from `initControls`.)
   - `split60`: the current default (first 60% reference, remainder analysis).
   - `custom`: label "Custom", no compute.

   Default selection: `last7-prior28` if available, else `split60`. On preset change,
   write the four date inputs and run validation. On **any manual edit** of a date
   input, set the select to `custom`. `initControls` uses the default preset instead
   of the hardcoded 60/40 fill.

2. **Client-side validation** (mirrors backend ticket T9's rules so the UI never
   sends a request the server will reject). New function `validateWindows()` in
   `app.js`, called on every date-input `change` and before `runRCA`:
   - rules: all four dates set; `ref-start ≤ ref-end`; `ref-end < an-start`;
     `an-start ≤ an-end`; all within `[meta.date_start, meta.date_end]`.
   - on violation: add class `invalid` to the offending input(s) and set
     `aria-invalid="true"`; disable `#run-rca`; `setStatus(<specific rule>, "error")`
     — e.g. "Reference window must end before the analysis window starts."
   - on pass: clear all of the above; if either window length (inclusive days) is not
     a multiple of 7 **and** any metric in `state.dag` declares seasonality, show the
     muted advisory (not error): `setStatus("ⓘ Windows aren't whole weeks — weekday
     mix can distort the comparison.")`.
   - CSS: `input.invalid { border-color: var(--down); }`.

3. **Copy link button.** In `index.html`, after Clear:
   `<button id="copy-link" style="display:none">Copy link</button>`. Show it whenever
   `location.hash` is non-empty (set/checked in `selectMetric`, `runRCA`, `clearRCA`,
   and `init`). Click: `navigator.clipboard.writeText(location.href)`, then set the
   button text to "Copied ✓" for 1.5 s (setTimeout to restore). No new styles beyond
   the default button.

**Acceptance checklist.**
- Fresh load on mock data (100-day window): preset shows "Last 7d vs prior 28d" and
  the four inputs hold the computed dates; Run RCA works.
- Editing any date flips the preset to Custom.
- Setting `an-start` before `ref-end`: input outlined red, Run disabled, specific
  message shown; fixing the date restores Run.
- Preset `weeks-1v4` yields a Monday `an-start` and a Sunday `an-end`.
- Copy link after selecting a metric puts the exact `#metric=…` URL on the
  clipboard; pasting it in a new tab restores the view.
- No console errors during all of the above.

## U2 — RCA target strip, run-progress scale, small repairs

**No backend dependency. Files:** `static/app.js`, `static/index.html`,
`static/style.css`, `docs/ai-context/frontend-ui.md`.

**Changes.**

1. **Target strip in the RCA card.** In `renderRcaTab`, add
   `<div id="rca-strip"></div>` inside `.rca-card` (after the `.gap-line`), then call
   a new `renderRcaStrip(res)`: fetch the target's series via the existing
   `/metrics/{name}` cache path (`state.metricCache` / `api()`), and render the same
   Plotly line as `renderTimeSeries` but `height: 120`, no y-axis title, margins
   `{l:38, r:6, t:4, b:18}`, with the two window shading rects built from
   `res.reference_window` / `res.analysis_window` (not from the inputs — the card
   must describe the run, not the current header state). Since `renderRcaTab` is
   synchronous today, make it async-safe: render the card immediately with an empty
   strip div, then fill the plot when the fetch resolves (placeholder text
   "loading series…" meanwhile; on fetch error, remove the div — the card must not
   break).
2. **Run-progress scale.** In `runRCA`, before the request, compute
   `k` = number of ancestors of the target (walk `state.dag.edges` client-side —
   build a reverse-adjacency once in `buildGraph` and store on `state`) that are
   probabilistic (have parents, no formula) and not in `state.meta.fitted`. Status
   becomes: `k > 0` → `Running RCA — fitting ${k} upstream model${k===1?"":"s"}…`,
   else `Running RCA…`.
3. **Clear restores β labels properly.** In `clearRCA`, after `clearRcaStyles()`,
   re-run `labelBetaEdges` for **every** fitted metric: for names in
   `state.meta.fitted` missing from `state.metricCache`, fetch `/metrics/{name}`
   first (sequentially is fine; typically ≤ a handful). Keep the existing cache-only
   pass as the fast path.
4. **Plotly slim build.** In `index.html`, swap
   `plotly-2.32.0.min.js` → `plotly-basic-2.32.0.min.js` (same CDN). Verify both
   charts still render (basic bundle includes scatter/line — all that's used).

**Acceptance checklist.**
- After an RCA run, the Root cause card shows the target series with both windows
  shaded, matching the metric tab's shading colors.
- On a fresh server (nothing fitted), running RCA on `revenue` shows "fitting 1
  upstream model…" (order_count) in the status.
- Run RCA → Clear: β edge labels reappear on fitted probabilistic nodes without
  clicking them first.
- Time-series chart on the metric tab unchanged visually after the Plotly swap; no
  console errors.

## U3 — Graph encoding: certainty opacity, direction glyphs, unexplained badge

**No hard backend dependency** (uses `prob_same_direction`, present on posterior
contributions today; formula contributions gain it with T7 and pick the channel up
automatically). **Files:** `static/app.js`, `static/style.css`,
`static/index.html` (legend), `docs/ai-context/frontend-ui.md`.

**Changes.**

1. **Certainty → edge opacity.** In `applyRcaOverlay`, for each contribution set
   `e.data("op", c.prob_same_direction == null ? 1 : Math.max(0.35, 2 * (c.prob_same_direction - 0.5)))`
   and extend the `edge.rca-up` / `edge.rca-down` styles with
   `"line-opacity": "data(op)", "text-opacity": "data(op)"` (keep arrow color solid —
   Cytoscape's `line-opacity` doesn't cover arrows; acceptable). Reset `op` to 1 in
   `clearRcaStyles`.
2. **Direction glyphs.** In `applyRcaOverlay`, node labels become
   `` `${name}\n${node.gap >= 0 ? "▲" : "▼"} ${signedPct(node.relative_change)}` ``
   (when `relative_change` is null, just the glyph). Legend "moved up/down" rows gain
   the matching glyph before the text.
3. **Unexplained badge.** In `applyRcaOverlay`, for nodes with contributions where
   `node.unexplained != null && Math.abs(node.gap) > 1e-9 &&
   Math.abs(node.unexplained / node.gap) > 0.35`: add class `rca-unexplained` and
   append ` ◌` to the node label. New Cytoscape style:

   ```js
   { selector: "node.rca-unexplained",
     style: { "border-style": "dashed", "border-color": "#d97706", "border-width": 3 } }
   ```

   Remove the class in `clearRcaStyles`. New RCA-only legend row in `index.html`:
   `◌ large unexplained share` with a dashed-amber swatch
   (`.swatch.unexp { border: 2px dashed #d97706; }`).
4. **Promote warn tokens.** In `style.css` `:root`, add `--warn: #d97706;
   --warn-soft: #fef3c7;` and replace the two hardcoded uses (`.diag .warn`,
   `.chip.lag`).

**Acceptance checklist.**
- Run RCA on `revenue` (mock data): the `daily_sessions → order_count` edge (high
  certainty, posterior) is near-fully opaque; artificially verify the channel by
  temporarily hard-coding `op: 0.4` and observing a faint edge, then remove.
- Node labels show ▲/▼ with the signed percent; legend matches.
- A node with dominant unexplained (if the mock tree yields none, temporarily lower
  the threshold to 0.01 to observe, then restore 0.35) shows the dashed amber
  border, the ◌ glyph, and the legend row appears only during an RCA overlay.
- Clear removes glyphs, opacity data, badges; β labels intact (U2.3).

## U4 — Root cause tab: component rows, method/quality chips, ranked-causes restyle

**Gated on the T5–T7 backend commit** (needs `components`, formula-row CIs). T8
fields (`fit_quality`, `inference_method`) are rendered if present, omitted if not.
**Files:** `static/app.js`, `static/style.css`, `docs/ai-context/frontend-ui.md`.

**Changes.**

1. **Component rows.** In `renderRcaTab`'s attribution blocks, after the parent
   rows and before `unexplained`, if `node.components` is an object, render one row
   per key in the fixed order `trend`, `seasonal`, using display names
   `trend (drift)` and `seasonality`:
   estimate → `fmt(comp.estimate)`; share → `pct(comp.estimate / node.gap)` guarded
   for tiny gaps like the existing code; CI from `comp.ci_95`; `P(dir)` column "—".
   Style: reuse row class `dim` plus new class `component` with
   `font-style: italic`. These rows are not clickable and not `<code>`-wrapped
   (they're model terms, not metrics).
2. **Method/quality chips.** The block heading becomes:
   Shapley nodes → `· Shapley` (drop "(exact)" — after T7 the rows carry CIs).
   Posterior nodes → `· posterior${node.inference_method ? " · " +
   esc(node.inference_method.toUpperCase()) : ""}`. If
   `node.fit_quality === "suspect"`, append
   `<span class="chip suspect" title="Convergence diagnostics failed — re-run with
   NUTS from the Metric tab">⚠ fit suspect</span>`; CSS
   `.chip.suspect { background: var(--warn-soft); color: var(--warn); }`.
3. **Ranked causes restyle.** Section heading → "Where to look first"; add under it
   `<p class="section-sub">heuristic triage order — confirm in the attribution
   detail</p>` (`.section-sub { font-size: 11px; color: var(--muted);
   margin: -4px 0 6px; }`). Bar restyle: `.cause-bar-wrap` height 2px; `.cause-bar`
   gains a dot terminator via `::after` (6px circle, accent color, right-aligned).
   Each row appends a certainty cell: find the cause's contribution row in
   `res.nodes[c.via].contributions` where `parent === c.metric`; if it has
   `prob_same_direction`, render `<span class="cause-p ${p < 0.75 ? "warn" : ""}">
   P(dir) ${pct(p)}</span>` (`.cause-p { font-size: 11px; width: 84px;
   text-align: right; } .cause-p.warn { color: var(--warn); }`); else render
   an empty spacer of the same width so rows align.
4. **Fit provenance line.** In the `.rca-card`, add a final `.sub` line:
   `models fitted on data before ${esc(res.analysis_window.start)}` — only when at
   least one node in `res.nodes` has `attribution_method === "posterior"`.
5. **Tooltips.** `P(dir)` table header gets
   `title="posterior probability the contribution has the sign shown"`; the
   `unexplained` row cell gets `title="residual the modeled parents, trend, and
   seasonality don't account for"`.

**Acceptance checklist.**
- RCA on `revenue`: the `order_count` block shows `trend (drift)` and `seasonality`
  rows (seasonality only if declared) with numeric estimates and CIs; parent + component
  + unexplained estimates sum to ≈ the node gap.
- Formula block (`revenue`) shows CIs on its parent rows (post-T7) and its heading
  reads "· Shapley" with no "(exact)".
- Ranked list shows the subtitle, hairline bars, and a P(dir) figure on causes with
  posterior contributions; rows without one still align.
- With a hand-injected `fit_quality: "suspect"` in the response (temporarily patch
  `state.rca` in the console), the amber chip renders with its tooltip; absent the
  field, nothing renders.

## U5 — Metric tab: fit provenance, name-keyed coefficients, fit-window controls

**Gated on the T5–T7 backend commit** (touches `breakdown/api/main.py`, which the
engine agent also edits — do this ticket only when the working tree is clean).
**Files:** `breakdown/api/main.py`, `static/app.js`, `docs/ai-context/frontend-ui.md`,
`tests/test_api.py`.

**Changes.**

1. **API: expose fit metadata.** In `GET /metrics/{name}` (`api/main.py`), when a fit
   is selected (existing `_pick_fit` helper), add alongside `summary`:

   ```python
   "fit": {
       "inference_method": fit.inference_method,
       "fit_end": fit.fit_end,
       "parents": fit.parents,
       "diagnostics": fit.diagnostics,   # {} until T8
   }
   ```

   `"fit": None` when nothing is fitted. Add an API test asserting the block appears
   after an analyze call and is `None` before.
2. **Provenance in the Posterior section.** In `renderPosterior`, when `data.fit`
   exists, render above the coefficient table:
   `<div class="fit-provenance">fitted · ${METHOD} · ${fit_end ? "data before " +
   fit_end : "full window"}</div>` (`.fit-provenance { font-size: 11.5px;
   color: var(--muted); margin-bottom: 6px; }`).
3. **Name-keyed coefficients.** In `renderPosterior` and `labelBetaEdges`, iterate
   `data.fit.parents` (fall back to `def.parents` when `fit` is absent) so
   `beta_raw[i]` is matched to the parent list the model was actually fitted with,
   not the YAML order assumption.
4. **Diagnostics chip.** When `data.fit.diagnostics.fit_quality` exists, replace the
   max-R̂ line with the chip: `✓ converged` (`.chip.fitted` styling) or `⚠ suspect`
   (`.chip.suspect` from U4), `title` listing whichever of
   `divergences / max_rhat / min_ess_bulk / elbo_drop` are present. Keep the current
   max-R̂ computation as the fallback when `diagnostics` is empty (pre-T8).
5. **Analyze controls.** Add to `.analyze-row`: a `chains` number input (default 4,
   min 1, max 8, `title="NUTS chains"`, ignored for ADVI) and a fit-window select
   `#an-fitend`: options `full window` (value "") and
   `before analysis window` (value = current `an-start` input, refreshed on focus;
   disabled when the date inputs are incomplete). `runAnalyze` appends
   `&chains=…` (NUTS only) and `&fit_end=…` (when non-empty) to the query string.

**Acceptance checklist.**
- `curl /metrics/order_count` before any fit: `"fit": null`; after
  `POST /analyze/order_count?inference_method=advi`: fit block populated,
  `fit_end: null`; after an RCA run, the fit selected for display follows the
  existing `_pick_fit` preference and the provenance line states it.
- Coefficient table renders identically to before on the mock tree (order happens to
  match) — verify by diffing the rendered rows.
- Analyze with `NUTS`, `chains=2`, `before analysis window`: network tab shows both
  params on the request; posterior section then shows "data before <an-start>".
- `uv run pytest tests/test_api.py -v` passes including the new test.

## U6 — Accessibility & keyboard pass

**No backend dependency; last because it touches everything lightly.**
**Files:** `static/index.html`, `static/app.js`, `static/style.css`.

**Changes.**

1. Status area: `aria-live="polite"` on `#status` and `#an-status`.
2. Tabs: `role="tablist"` / `role="tab"` / `aria-selected`; Left/Right arrow keys
   move between tabs; `tabindex="0"`.
3. Cause rows: `role="button"`, `tabindex="0"`, Enter/Space triggers the existing
   click handler; `:focus-visible { outline: 2px solid var(--accent);
   outline-offset: 1px; }` applied globally for buttons, tabs, cause rows, inputs.
4. Legend swatches get `aria-hidden="true"`; the graph container gets
   `role="img"` and an `aria-label` summarizing the tree ("Metric DAG: N metrics,
   M relationships") updated in `buildGraph`.
5. Verify all RCA-state information carried by color also appears as text/glyph
   (done by U3/U4 — this ticket audits and fixes any remainder, e.g. the gap-line
   color in the RCA card gains the ▲/▼ glyph if U3 didn't already cover it).

**Acceptance checklist.**
- Entire RCA workflow (pick preset → run → open a cause → clear) completable with
  keyboard only.
- VoiceOver (or any screen reader) announces status changes on run/finish/error.
- No color-only information remains in the RCA overlay or sidebar (spot-check with a
  grayscale filter).

---

## Sequencing summary

| Ticket | Depends on | Touches backend? |
|---|---|---|
| U1 presets/validation/copy-link | — | no |
| U2 strip/progress/repairs | — | no |
| U3 graph encoding | — (improves after T7) | no |
| U4 RCA tab | T5–T7 committed | no |
| U5 metric tab + fit metadata | T5–T7 committed; clean tree | `api/main.py` (small) |
| U6 a11y | U3, U4 | no |

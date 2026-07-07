# UI Design Assessment & Spec

*An expert design review of the breakdown UI — 2026-07-06*

**Scope.** This assesses the current UI (`static/index.html`, `static/app.js`,
`static/style.css`, documented in `docs/ai-context/frontend-ui.md`) against the
product's core job: **rapidly localizing the drivers of an observed change in a key
metric** — the late-night-CFO-call workflow from the README. It also specifies the UI
work required by the statistical improvement plan
(`statistical_improvement_plan.md`), whose tickets change the RCA response schema and
add outputs that currently have no home on screen. The companion document
`ui_implementation_plan.md` turns this spec into implementation tickets.

**Design constraint (inherited from the project):** simple but rigorous. No framework,
no build step, one HTML/JS/CSS file each. Every proposal below fits that budget.

---

## 1. Who is looking at this screen

- **The operator** (analyst / data scientist): runs the RCA, judges whether to trust
  it, digs into fits. Needs speed, honest uncertainty, and diagnostics that don't
  require ArviZ literacy.
- **The reader** (CFO, PM, stakeholder): receives a deep-linked RCA URL on Monday
  morning. Reads the graph and the ranked causes; will never click "Analyze". Needs
  the answer to be legible *without* the operator narrating it — direction, magnitude,
  confidence, and what's still unexplained.

The current UI serves the operator well and the reader adequately. Most of the gaps
below are places where the screen presents a number without the context that makes it
trustworthy — which hurts the reader most.

## 2. Assessment of the current UI

### What's working (protect these)

- **Information architecture is right.** Graph as the primary surface, header as the
  single verb ("Run RCA"), sidebar for depth. The BT-ranked dagre layout reads
  correctly as a KPI tree.
- **Quiet-until-RCA color philosophy.** The resting palette is neutral, so the
  red/green overlay carries real meaning when it appears. This is the UI's best
  decision; nothing below may violate it.
- **Deep links** make an analysis a shareable artifact — exactly right for the
  CFO workflow.
- **Honest tables.** Shares aren't clamped, `unexplained` is a first-class row,
  CIs are shown where they exist. The UI already respects the docs' philosophy.
- **Error surfacing** through the status area, never silent.

### Weaknesses, in priority order

**W1 — Window selection is the weakest link in the core workflow.** Four bare
`<input type="date">` fields are the entire interface to the single most
consequential analytical choice (docs/model.md §5: "window means hide within-window
shape"; the statistical plan adds whole-week-window guidance and hard T9 validation).
There are no presets for the common cases ("last week vs the 4 weeks before it"),
no client-side validation (you learn about a backwards window from a server error
after the click), and nothing shows you *what data you just selected* until after
the run. The upgraded backend will reject overlapping/misordered windows (T9) —
today's UI would relay that as a bare red string.

**W2 — Uncertainty is invisible at the level where decisions are made.** The graph
overlay encodes estimate sign (color) and share magnitude (width), but a contribution
with `prob_same_direction = 0.62` renders identically to one at `0.99`. The ranked
causes list — the first thing the reader scans — shows a bar for a unitless heuristic
score with no confidence signal at all. After statistical tickets T7/T8, every
contribution has an honest CI and every fit has a quality flag; the graph and the
ranked list must reflect them or the UI will radiate false confidence that the
engine no longer has.

**W3 — The new engine outputs have no home.** The statistical plan changes the RCA
schema in ways the UI must absorb, or it will silently mislead:
- **T5:** probabilistic nodes gain a `components` block (trend & seasonal
  contributions with CIs) and `unexplained` is redefined to residual-only. The
  attribution tables must show trend/seasonal rows, or the parent shares will appear
  not to add up.
- **T7:** formula-node contributions gain `ci_95` / `prob_same_direction`. The current
  code prints "exact" when `ci_95` is null — after T7 that label silently disappears,
  and the "Shapley (exact)" method label becomes wrong (the attribution is exact;
  the inputs are not).
- **T8:** nodes gain `fit_quality` ("ok"/"suspect") and `inference_method`. A suspect
  ADVI fit must be visually distinguishable from a confirmed NUTS fit.
- **T2:** fits are keyed by `fit_end`; the metric tab's "fitted" chip and posterior
  table no longer describe one unambiguous fit.

**W4 — Big `unexplained` is a finding the graph hides.** docs/model.md: "a large
unexplained is a finding, not an error." It lives only in a dim table row. A node
whose gap is 70% unexplained looks identical on the graph to one fully accounted
for — the reader draws the wrong conclusion at a glance.

**W5 — Direction is encoded by color alone.** Red/green is the classic
deuteranopia failure, and these two colors carry the entire headline finding.
No glyphs, no text reinforcement on edges; node labels do get a signed percent
(good — extend that pattern).

**W6 — The ranked-causes bar implies more than it means.** A horizontal bar chart
of the heuristic score reads as "82% probable cause." It's a triage ordering
(docs say so), but the visual doesn't. Until statistical T12 replaces the score with
a real probability, the presentation should downgrade its confidence, not the docs.

**W7 — Small correctness/robustness debts.**
- `beta_raw[i]` is mapped to parent names by *position*, relying on NetworkX
  predecessor order matching YAML `parents` order (`app.js labelBetaEdges`,
  `renderPosterior`). True today, but implicit coupling across three layers; the
  backend now carries `FitResult.parents` and should expose it.
- After Clear, β edge labels are restored only for metrics that happen to be in the
  client cache.
- RCA progress is a static string; a run that fits several upstream models gives no
  sense of scale (the client can compute how many fits are needed from `/dag` +
  `/meta.fitted`).
- Plotly full bundle (~3.5 MB) from CDN for one line chart; `plotly-basic` halves it.
  Minor for a localhost tool.

## 3. Design principles for this round

1. **Confidence gets a visual channel.** Sign = hue, magnitude = weight (as today),
   and now *certainty = opacity/texture*. Anything the engine is unsure of looks
   unsure.
2. **Never let a number appear more precise than the engine claims.** Heuristics look
   like heuristics; suspect fits look suspect; unexplained gaps look unexplained.
3. **The window choice is part of the answer.** A shared RCA link must show *which
   data* was compared, not just the result.
4. **Reader-first labeling.** Every glyph/badge added must be self-explanatory or
   have a `title` tooltip; the reader has no operator sitting next to them.
5. **Stay inside the budget.** Vanilla JS, no build step, existing token system
   extended not replaced.

## 4. Specification

### 4.1 Header & window selection (fixes W1)

```
┌────────────────────────────────────────────────────────────────────────────┐
│ breakdown  Target [revenue ▾]  Windows [Last wk vs prior 4 wks ▾]          │
│            Ref [2024-03-04]–[2024-03-31]  vs  [2024-04-01]–[2024-04-07]    │
│            (Run RCA) (Clear) (Copy link)                    status area    │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Preset select** before the date inputs, options:
  `Last 7d vs prior 28d` (default when data allows), `Last 14d vs prior 28d`,
  `Last full week vs prior 4 weeks` (snaps to whole Mon–Sun weeks — the
  statistically recommended shape), `First 60% vs rest` (current behavior), and
  `Custom` (auto-selected the moment any date input is edited by hand). Presets
  compute from `/meta.date_start/date_end`.
- **Client-side validation, mirroring T9's rules** (`reference_start ≤ reference_end
  < analysis_start ≤ analysis_end`, all inside the data window): violated inputs get
  a red border (`aria-invalid`), the Run button disables, and the status area shows
  the specific rule broken. No request is sent that the server would reject.
- **Whole-week nudge:** when either window's length isn't a multiple of 7 and the
  tree declares weekly seasonality anywhere, show a one-line advisory (ⓘ, muted, not
  red) in the status area: "Windows aren't whole weeks — weekday mix can distort the
  comparison."
- **Copy link** button, visible whenever a deep-linkable state exists (metric
  selected or RCA complete): copies the current URL, flashes "Copied". This is the
  hand-off moment of the whole product; it shouldn't require knowing the hash trick.

### 4.2 What-data-am-I-comparing strip (fixes W1, W4-adjacent)

Add the target metric's time series **into the RCA target summary card** (Root cause
tab, top): a ~120px Plotly strip of the target series with the two windows shaded
(reuse the metric-tab shading exactly — gray reference, indigo analysis). The reader
of a shared link sees the shape of the anomaly and the comparison windows before any
numbers. No new visualization vocabulary; pure reuse.

### 4.3 Graph overlay: certainty channel + direction glyphs (fixes W2, W5)

- **Edge opacity encodes sign-certainty.** After T7, every contribution has
  `prob_same_direction`. Map it: `opacity = 0.35 + 0.65 · (2·(p − 0.5))` clamped to
  [0.35, 1] (p = 0.5 → faint, p = 1.0 → full). Formula-node edges before T7 (no p)
  render at full opacity. The existing width (share) and color (sign) channels are
  unchanged — certainty gets its own channel per principle 1.
- **Direction glyphs.** RCA node labels become `name ▲ +8.2%` / `name ▼ −16.2%`
  (glyph before the percent). Legend rows for moved up / moved down gain the same
  glyphs. Color stays; it is no longer the sole carrier.
- **Unexplained badge (fixes W4).** When a node's `|unexplained / gap| > 0.35`
  (and `|gap|` is non-trivial), give the node a **dashed amber border**
  (`#d97706`, the existing warn color) on top of its up/down fill, and append `◌` to
  its label with a `title` of "N% of this gap is unexplained by modeled parents".
  Add a legend row (RCA-only): `◌ dashed = large unexplained`. Amber is already the
  project's "check this" color (R̂ warnings) — same semantics here.

### 4.4 Root cause tab (fixes W2, W3, W6)

Target summary card: unchanged content + the strip from 4.2 + the fit provenance
line "models fitted on data before `analysis_start`" (post-T2 truth; one sentence of
trust-building).

**Ranked causes** — retitle the section "**Where to look first**" with a muted
subtitle "heuristic triage order — confirm in the attribution detail". Bar restyled
from a solid confident bar to a **thin hairline bar with a dot terminator** (score
stays as relative length; the lighter visual weight matches its epistemic weight).
Each row gains, right-aligned, the strongest available certainty statement for that
metric's direct contribution: `P(dir) 0.97` (from its contribution row at its `via`
child), colored by the existing ok/warn scale (≥0.9 normal, <0.75 amber). When T12
lands, this section swaps to real path probabilities with no layout change.

**Attribution detail** per node:
- Method chip: "Shapley" (drop "(exact)" once T7 gives these rows CIs) or
  "posterior · ADVI" / "posterior · NUTS" (from T8's `inference_method`), plus a
  **fit-quality chip** when `fit_quality == "suspect"`: amber chip `⚠ fit suspect`
  with `title` "Convergence diagnostics failed — re-run with NUTS from the Metric
  tab."
- **Component rows (T5):** after the parent rows, render `trend (drift)` and
  `seasonality` rows from `components`, with estimate, share-of-gap, and CI —
  styled like parent rows but with muted italic names (they're model terms, not
  metrics; no click-through). `unexplained` stays the last dim row and now means
  residual-only — update the row's `title` accordingly.
- Column header `P(dir)` gains `title` "posterior probability the contribution has
  the sign shown".

### 4.5 Metric tab (fixes W3, W7)

- **Fit provenance.** The posterior section header shows which fit is displayed:
  `fitted · NUTS · full window` or `fitted · ADVI · data before 2024-02-16`
  (from the fit's `inference_method` + `fit_end`). Requires `/metrics/{name}` to
  expose these fields (small API addition, already on `FitResult`).
- **Coefficient table keys by name, not position:** consume the fit's `parents`
  list from the API instead of zipping `def.parents` with `beta_raw[i]` (kills the
  W7 ordering coupling).
- **Diagnostics line upgrades with T8:** traffic-light chip
  (`✓ converged` green / `⚠ suspect` amber) built from `fit_quality`, with the
  numbers (max R̂, divergences, ESS, or ELBO plateau for ADVI) in a `title` tooltip
  and in the collapsible full summary. The current max-R̂ line is the fallback until
  T8 lands.
- **Analyze controls** gain `chains` (NUTS only, default 4) and a
  `fit window` select: `full window` / `before analysis window` (passes `fit_end`;
  enabled when RCA windows are set). This is the operator's "confirm with NUTS"
  path — one click, same screen.

### 4.6 States, a11y, and small repairs (fixes W5, W7)

- All interactive elements reachable by keyboard: cause rows are `<button>`s (or
  `role="button"` + `tabindex=0` + Enter/Space), tabs get `role="tab"`, date inputs
  already native. Visible `:focus-visible` outline using the accent token.
- `aria-live="polite"` on the status area so runs/errors are announced.
- Clear restores β edge labels from the server, not just the client cache (refetch
  is cheap; or label lazily on next select — either, but not silently missing).
- RCA progress string includes scale: "Fitting k upstream models…" where k =
  probabilistic ancestors of the target minus already-fitted (client-computable).
- Swap Plotly CDN build for `plotly-basic` (line charts only are used).

### 4.7 Explicitly out of scope (this round)

- Draggable window scrubber / brush selection on a timeline (highest-value future
  upgrade to 4.1, but a significant vanilla-JS build; the presets + validation +
  strip deliver most of the value first).
- Dark mode, mobile layout, framework migration, node search/filter for large trees
  (revisit when a real tree exceeds ~30 nodes).
- Any charting of posteriors beyond tables (density plots invite over-reading of
  ADVI approximations).

## 5. Token additions

| Token | Value | Use |
|---|---|---|
| `--warn` / `--warn-soft` | `#d97706` / `#fef3c7` | suspect-fit chips, unexplained badge, low-certainty text (values already used ad hoc for R̂/lag chips — promote to tokens) |
| edge opacity range | 0.35–1.0 | sign-certainty channel (4.3) |
| glyphs | `▲ ▼ ◌ ⚠ ✓ ⓘ` | direction, unexplained, suspect, converged, advisory — text glyphs, no icon font |

Everything else reuses the existing palette; the quiet-by-default rule stands.

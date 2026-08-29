# Frontend UI: Design & Implementation

The breakdown UI is a single-page app served by FastAPI at `/ui`, in the spirit of `dbt docs serve`: no build step, vanilla JS, dependencies from CDN. It is the visual layer over the metric tree and the RCA engine.

## Use cases (in priority order)

**UC1 — Triage an anomaly (the headline).** "Revenue dropped over the weekend — what drove it?" The user picks a target metric and two time windows, runs RCA, and reads the answer from the graph itself: nodes tinted by how much each metric moved, edges weighted by how much of the child's gap each parent explains, and a ranked cause list with uncertainty. This is the late-night-CFO-call workflow from the README, compressed into one screen.

**UC2 — Explore the tree.** A stakeholder or new analyst opens the UI to understand how the business is wired: which metrics exist, which relationships are arithmetic identities vs learned, where the data comes from. The graph must read clearly at a glance without any analysis having been run.

**UC3 — Inspect one metric.** Click a node: see its time series, definition (source, parents, lags), and — if a model has been fitted — the posterior over each causal coefficient *in business units* (`beta_raw`), with credible intervals. Fit a model from here (`/analyze`, NUTS by default or ADVI on request) without touching curl.

**UC4 — Trust the model.** Surface just enough diagnostics (R-hat, observation noise) that a data scientist can tell a healthy fit from a broken one, without drowning a business user in an ArviZ dump. Raw summary stays available behind a collapsible.

**UC5 — Simulate a scenario (what-if).** "If trial→member conversion went up 0.3%, what happens to MRR?" The user picks a baseline window, adjusts one or more metrics (clicking nodes with the What-if tab active), optionally asserts assumption links (effects the tree doesn't know, e.g. a discount lever), and runs a steady-state simulation. Results render on the graph (deltas, pinned nodes, assumption edges) and in the sidebar (outcome cards, source waterfall, per-node table with CIs). Design spec: `knowledge/what_if_design.md`.

## Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ breakdown  Target [revenue ▾]  As of [date]   status · (n)(prov)(win)│
├────────────────────────────────────────────┬─────────────────────────┤
│ (Display ▸)                                │  [Metric] [Root cause]  │
│              DAG canvas                    │  RCA tab: setup panel   │
│   (Cytoscape + dagre, KPIs at top,         │  (windows preset, ref/  │
│    sources at bottom — rankDir BT)         │  analysis dates, Run/   │
│                                            │  Clear) above results   │
│  legend (bottom-left)     (− 100% + Fit) → │  (scrolls)              │
└────────────────────────────────────────────┴─────────────────────────┘
```

- **Header** holds only the globals, **left-to-right by narrowing scope**: the **Tree** switcher (outermost — hidden entirely when the process serves one tree; see *Several trees* below), the target select (the tree-wide focus metric), the **As of** anchor date, a **Share** menu (Copy link — the deep-link URL restoring the exact view: selected metric, RCA run, or what-if scenario — and Download RCA result as JSON, enabled after a run; the future home of the exportable RCA report, roadmap 1.5), and — on the right — **two separate slots** (`#header-right`). `#status` is *transient*: live run progress (see **Run progress** below), errors, and the RCA window advisories. `#context` is *ambient*: a chip row saying which tree is loaded (`18 metrics` · provider · loaded window, plus an amber `data → <date>` chip when the tree-wide data edge lags). They are two elements on purpose — they shared one until 2026-08-11, so every completed run permanently overwrote the tree context, and the single element carried a `max-width: 340px` that truncated the context on windows with room to spare. `setStatus(msg, kind, ttlMs)` takes an optional TTL used for success notices ("RCA complete for revenue.", 6s) so a finished run doesn't leave a stale line; errors and advisories never take one and stand until something replaces them.
- **RCA setup lives in the Root cause tab** (`#rca-setup`, persistent markup above the `#rca-results` render target), and is **analysis-first**: an analysis-window preset select (`last7` / `last14` / `last-full-week` / `custom`; too-short presets omitted) + the analysis date pair, then the reference pair with an **auto** chip. A **Fits** select (`#rca-method`, NUTS default) sits above the buttons and rides on the request as `inference_method`; the What-if tab carries its twin (`#wf-method`, held in `state.whatif.method` because that tab's markup is rebuilt after every run), and `wireMethodNote` paints the shared cost line for both — `.control-note` for NUTS, `.control-note.warn` for ADVI, because "this setting can move a published estimate by tens of percent" is not a `--faint` sentence. Neither goes into the deep link: a shared link replays the analysis at the default, which is the exact sampler. `state.refMode` is the two-state machine: `"auto"` (default) means the reference inputs preview the server's matched adjacent block (`defaultReferenceJS` mirrors `grains.default_reference_window`, with week alignment and coarsest grain derived from the selected target's ancestor scope via `referenceAlignment`) and the request **omits** the reference params — the server's computation is the number of record and the response overwrites the inputs; `"custom"` (any manual reference edit) sends all four dates. Analysis edits flip the preset to Custom but keep the refMode; the auto chip restores auto; target changes recompute the auto reference (scope changes alignment). `validateWindows` keeps the hard rules and adds muted advisories: whole-week (scope-keyed), short reference (< ~4 coarsest-grain periods), and non-adjacent gap (trend contamination). A `#history-nudge` line appears when `/meta.earliest_available` shows provider history before `date_start`. Setup sits with its results, so the windows are unambiguously RCA parameters rather than global filters.
- **Canvas** is the primary surface; the graph is the product. Dagre layered layout with `rankDir: 'BT'` so the tree reads like a KPI tree: outcome metrics on top, drivers below. The card display options (variant / sparkline length / delta length) collapse behind a quiet **Display** toggle in the top-left toolbar.

  The ground is a **dot grid**, not ruled graph paper — the DAG is already made
  of lines and ruling would compete with the edges. `initCanvasGrid()` rewrites
  `background-size`/`-position` from cy's zoom and pan so the dots travel with
  the graph (sitting still while the tree slides reads as a rendering bug), with
  spacing clamped to 12–96px so a zoomed-out tree doesn't moiré. Dot *radius* is
  fixed: it's paper texture, not content.

  **Navigation** is Cytoscape's own: drag the background to pan, wheel to zoom (`wheelSensitivity: 0.2`). Both are kept as-is — the deliberate decision (2026-08-11) was *not* to adopt the Figma convention of scroll-to-pan / ⌘-scroll-to-zoom, because plain-wheel zoom is worth more here than trackpad-native panning. What was missing was the way back, so `#zoom-controls` (bottom-right, `initZoomControls()`) adds `−` / a live zoom-percent button that resets to 100% / `+` / **Fit**, with `F` bound to fit. Zoom steps hold the viewport centre fixed. The `F` handler is guarded on `e.target` so it never eats a keystroke typed into a date input or a scenario note.
- **Sidebar** (410px) has three tabs: **Metric** (UC3/UC4), **Root cause** (UC1), and **What-if** (UC5). Clicking a node opens Metric — unless the What-if tab is active, in which case it opens that node's adjust panel. Finishing an RCA run switches to Root cause.
- **Overlay exclusivity**: the RCA and what-if overlays never coexist; the active tab owns the canvas (switching to Root cause or What-if re-applies that tab's overlay via a shared `clearOverlays()`), and the Metric tab keeps whichever overlay was last showing.

## Run progress

RCA and what-if can spend a minute or more fitting ancestor models. Until
2026-08-12 that was a spinner reading `Simulating — fitting 3 models…`, where
the count was the **frontend's own guess** from walking `state.revAdj` against
`/meta.fitted` — an estimate of work the server was about to do, with nothing
after it. Nothing said which model, how far in, or whether it was wedged.

The server reports real stages now. The client generates an opaque `run_id`,
passes it to `POST /rca/{name}` or `POST /simulate`, and polls
`GET /progress/{run_id}` every 400ms while the request is in flight. This works
without any job queue because the analysis already runs in `asyncio.to_thread`,
so the event loop stays free to answer the poll — and the poll deliberately
takes **no lock**, since the analysis holds `app.state.lock` for its whole
duration and taking it would deadlock the report against the thing it reports on.

Stages: `waiting` (registered before the lock is acquired, so a run queued
behind another says so instead of looking hung) → `resolving` → `fitting`
(carrying `metric`, `current`, `total`) → `attributing` / `simulating`.

- **`countUpstreamFits` / `countWhatifFits` are gone.** They existed only to
  guess the denominator the server now states.
- **The copy rotates every 2.4s, and every phrase is literally true of its
  stage.** `exploring the typical set` is what NUTS does; `permuting
  coalitions` is what exact Shapley attribution does. The `fitting` vocabulary
  is *MCMC*, because since roadmap S2 every on-demand fit is NUTS unless the
  request asked otherwise — `descending the ELBO` sat on screen for 30 of the
  35 seconds a `sessions` fit takes and had to go. If a phrase is moved to a
  stage where it stops being true it is no longer a joke, just wrong. Entering
  a stage resets to the first phrase, so the line a reader lands on names the
  phase just entered.
- **An elapsed timer is always shown**, and the completion notice reports the
  total (`RCA complete for revenue in 0:38.`). A long wait with a number
  attached is far more tolerable than one without.
- **The node being fitted is marked on the canvas** (`.fitting-now`, a heavy
  accent border, ordered after `:selected` so it wins during a run). This is
  the honest version of a progress bar: the position on the tree is real, and
  it shows the reader that RCA fits *ancestors* — something the sidebar never
  says out loud.

Progress is advisory throughout: `engine/progress.py`'s `report()` swallows
callback exceptions, a failed poll is silently ignored, and omitting `run_id`
skips the machinery entirely — which is what every non-UI caller (curl, MCP,
the tests) does, so those exercise the engine's original no-callback path.

## Visual language

**"Notebook"**: a warm paper ground with white cards sitting on it, quiet by
default so RCA color can carry meaning when it appears. Retuned 2026-08-11 —
the previous cool grey `#f6f7f9` was in the same family as the accent hues and
competed with them, and white cards on a near-white canvas barely separated.

**`style.css` `:root` is the single source of truth.** `app.js` reads the custom
properties into a `COL` object at load (the Cytoscape stylesheet, the card SVGs
and the Plotly charts all need real hex, not `var()`), so a palette change is
one edit in one file. It used to be 74 literals spread through `app.js`; adding
a color means adding a `:root` token *and* a `COL` line, never a literal.

| Token | Value | Use |
|---|---|---|
| bg / panel | `#faf7f2` / `#ffffff` | paper canvas / cards, sidebar, header |
| border / rule | `#e8e2d8` / `#f0ebe3` | hairlines / card dividers, chart gridlines |
| text / text-2 / muted / faint | `#1c1917` / `#57534e` / `#78716c` / `#a8a29e` | warm ink, not blue-black |
| grid | `rgba(28,25,23,0.07)` | canvas dot grid |
| source | `#a8a29e` | source-node border — deliberately the *neutral*, "no model" |
| accent (indigo) | `#4f46e5`, soft `#ecedfc` | probabilistic edges, buttons, fitted tint |
| formula (cyan) | `#0891b2`, soft `#e4f4f9` | formula-node border + deterministic edges |
| up / down | `#15803d` / `#b91c1c` (soft `#e2f0e4` / `#fae7e3`) | RCA gap direction |
| warn | `#c47b0c`, soft `#fbf0da`, ink `#8a5209` | extrapolation, assumption edges, caveats |

**The palette is validated, not eyeballed** — the dataviz skill's
`validate_palette.js`, all-pairs, against the paper surface:

- **Identity** (accent ↔ formula): all checks pass — CVD ΔE 16.4, normal-vision
  ΔE 21.4, both ≥3:1 on paper.
- **Status** (up / down / warn): worst normal-vision pair ΔE 18.7, all ≥3:1 —
  but **up ↔ down is deutan ΔE 4.2 and always will be.** Red/green is the
  CVD-hostile pair and no re-step fixes it. That is only legal because
  direction never rides on hue alone: delta pills carry ▲/▼, overlays carry
  ◌/⊙/⚠, and the legend spells out improved/worsened. **Those glyphs are load
  bearing — do not remove them.**

**Formula nodes were violet (`#9333ea`) until 2026-08-11.** Against the indigo
accent that measured normal-vision ΔE 11.8 — below the 15 floor, i.e. hard to
tell apart *with full color vision* — and protan ΔE 0.9, i.e. identical. Edges
survived on solid-vs-dashed, but node borders encode type by color alone, so
"learned" and "arithmetic identity" were not distinguishable. Cyan is far from
indigo and far from the green/red/amber the status channel already claims.

**Nodes** are white round-rects rendered as **stat cards** (see *Node cards* below): a big number, an optional period-over-period delta, and an optional sparkline. Border color encodes type — warm gray for source metrics (no parents), indigo for probabilistic, cyan for formula nodes. A fitted model tints the node background with `--accent-soft`. Selected node gets the accent border.

**Edges** point parent → child. Deterministic edges (child has a `formula`) are **solid cyan**; probabilistic edges are **dashed indigo**. When a probabilistic child is fitted, its incoming edges label with the raw-scale coefficient: `β 0.10 [0.08, 0.13]`.

### Node cards

Each metric node is drawn as a **stat card** rather than a bare label. The card is
an SVG data-URI set as the Cytoscape node's `background-image` with a **transparent
background**, so the node's own border and `background-color` still render behind it
— which is why the RCA / what-if / selection overlays (all class-driven border/fill)
keep working unchanged.

- **Variants** (increasing detail): `num` (big number only) · `delta` (+ period-over-period delta pill) · `spark` (+ trailing sparkline) · `full` (both). Set canvas-wide via the floating toolbar (top-left), or overridden per node from the Metric tab (an indigo dot marks an overridden node). Config + overrides persist to `localStorage` under `breakdown.cardConfig`.
- **As-of anchor.** The toolbar's **As of** date input anchors every card's headline/delta/sparkline: only periods *fully completed* by the date count (a weekly point needs its Sunday ≤ as-of). Defaults to the tree-wide data edge — the min of `/meta`'s per-metric `data_through` — so a lagging source mart shows its true last day instead of a zero-filled tail, and a half-loaded calendar week never becomes a weekly headline. Not persisted (freshness moves daily); the header context row gains an amber `data → <date>` chip when the edge lags the window end, and the Metric tab shows each metric's own `Data through` (with a "lags window end" chip when behind).
- **Numbers.** Big number = latest value. Delta = latest vs `deltaLen` points earlier (both lengths are canvas-wide controls). Sparkline = trailing `sparkLen` points, thin line + area fill + endpoint dot, colored by direction (semantic green/red, never the indigo brand hue). **The delta pill names its own basis** — `▼ -8.3% · 7w`, dimmed inside the pill so it does not compete with the number — and the sparkline draws a hollow ring at the point the delta measured from. The two spans differ by design (7 points against 30) and the card used to state neither, so `new_mrr` on a weekly tree drew a line up 16% across its 23 weeks under a pill down 8.3% across the last 7: both numbers right, the pair unreadable. The basis is counted from the point the backward scan actually landed on, not from `deltaLen` — the scan walks back past nulls, so a gap in the series makes the real lookback longer than the setting, and a pill that printed the setting would be naming a window it did not use. Under an overlay the pill reads `vs ref` (RCA: analysis window against reference window) or `vs base` (what-if: simulated against baseline) and the ring is suppressed, because neither of those is a point on the drawn line. Sparkline and delta lengths stay **independent**: coupling them would either starve the sparkline of the points it needs to show noise and seasonality, or stretch "recent change" to 30 periods — seven months on a weekly metric.
- **Formatting** comes from the metric definition's optional `format` (`{style, unit, decimals, compact, symbol}`); a `unit` renders a small caption under the value and grows the card one line.
- **Goodness coloring** maps through the metric's `direction` via `goodDir`/`goodClass`: for `down_is_good` metrics an upward move colors red (arrow stays ▲); `neutral` **and undeclared** (`direction: null`) metrics color gray and get no RCA/what-if tint. `direction` has no parser default — it used to default to `up_is_good`, which `model_dump()` then shipped indistinguishably from a declaration, so an unclassified metric painted a confident "improved". Applies to card delta pills, sparkline color, RCA node/edge tints, what-if tints, and the sidebar gap headlines — legend swatches read improved/worsened, with arrows carrying direction.
- **Overlay-aware.** While an RCA or what-if overlay is active the card folds in that overlay's numbers (RCA gap %, what-if simulated value) with `◌` / `⊙` / `⚠` / `⊘` marks, so a node never shows two conflicting deltas. `⊘` (physically impossible, roadmap C26) has a legend row of its own: it is the one mark that says the number is not merely unusual, and the canvas card is the surface that gets screenshotted. State lives in `state.cardOverlay`; the renderer is `renderNodeCard` / `buildCardSVG` in `static/app.js`.
- All cards hydrate from a single **`GET /series`** call at load — per-metric `{grain, dates, values}` at each metric's native grain (mixed-grain trees have no shared date axis, so dates are per-metric). Card "points" are grain periods: days for daily metrics, weeks/months for coarser ones.

**RCA overlay** (applied after a run, removed by Clear):
- Node background shifts to the soft up/down color by sign of `relative_change`; the card's delta shows the signed percent (`−16.2%`).
- Edge width scales `2 + 6·min(|share_of_gap|, 1)`; edge color goes up/down by the sign of the contribution `estimate`; edge label shows the share as a percent.
- The legend gains the up/down swatches while the overlay is active.

**What-if overlay** (applied after a simulation, removed by Clear / tab switch):
- Non-baseline nodes tint soft up/down by delta sign; the card's big number becomes the simulated value and its delta the `▲ +3.4%` change from baseline. **Background opacity encodes P(direction)** (same certainty channel as RCA edge opacity).
- **Intervened nodes** get a heavy solid indigo border and a `⊙` mark on the card — visibly pinned (do-operator).
- **Assumption links** are temporary dotted amber edges added to the graph; non-metric sources ("levers", e.g. `discount_pct`) appear as temporary amber-dashed ellipse nodes placed near their target without re-layout.
- Extrapolation-flagged nodes get the dashed amber border + `⚠`; edges into a pinned node stay neutral (the pin severs them).

## Several trees (roadmap 2.16)

One process can serve several trees, and they are peers: a company might keep one wide tree with revenue at the top, a marketing tree detailing channels and campaigns, a product tree about feature adoption and retention, and a tree standing behind a target. Any of them may be durable or disposable; any may declare a goal or not. The UI takes no position on either. Design spec: `knowledge/multi_tree_design.md`.

- **The index** (`#index`, `renderIndex()`) is the landing view at `/ui` whenever there is more than one tree, rendering `GET /trees` as **one flat grid** of cards. It is deliberately not grouped by `period`: grouping by time would file every tree that isn't time-bound under "other", which is the opposite of the point — `period` is one optional label on a card, beside the owner. With exactly **one** tree `/ui` opens that tree directly, exactly as it always has: an index of one card is a toll booth.
- **Four card states, and the difference between them is the point.** A loaded tree that declares a goal shows `143 / 200` with a bar and the `as_of` its number was read at; one that is **not loaded** shows the declared goal, a dash, and a **Load** button — never a blank, which reads as zero, and never a stale number presented as live (`§2.3`). A tree with no `tree.goal` shows its metric count and provider, and that is the common case rather than a deficiency: it is not a failed goal card. An **errored** tree shows its own parse error plus the `breakdown doctor` hint, in the degraded banner's language.
- **The pace read is deliberately not a verdict.** `period` is a free-form label rather than a parsed range, so the *start* of a goal window is nowhere in the tree and elapsed-vs-achieved cannot be computed. `goalPace()` states whichever of the two checkable facts exist — share of target, days to deadline (a goal need not declare one) — in italic muted type, visibly softer than anything the engine computed. "On track"/"behind" would be a forecast the engine never made.
- **The switcher** is `#tree-select`, leftmost in the header. **Switching reloads the page** (`navigateToTree`) rather than resetting state in place: `state.dag`, `series`, `metricCache`, `rca`, `whatif`, card overrides and the RCA window inputs are all keyed on metric names belonging to one tree, and the boot path binds its listeners once. A reload is the version of "re-run `init()`" with no way to leave a stale listener or a half-cleared canvas behind, and the whole view lives in the URL, so nothing is lost. A `hashchange` carrying a different `#tree=` reloads for the same reason; our own hash writes go through `history.replaceState`, which fires no `hashchange`.
- **Every data request is tree-scoped** through `treePath()` (`/trees/<id>/meta`, …). `/progress/{run_id}` and `/trees` are not: run ids are already unique, and the index is about all of them.
- **`localStorage` keys carry the tree id** (`storeKey()`): card config and saved views are keyed by *metric name*, and two trees can name the same metric while meaning different things — a view saved against one tree would otherwise replay against another.

## Cold-start surface (`/meta` says `mode: "cold_start"`)

A `provider: none` tree has no data, so the UI boots **what-if-first** over declared beliefs (`coldStart()` in `app.js` gates every branch):

- **Boot**: `/series` is never requested; the context row is `<n> metrics` plus an amber **cold start** chip (its tooltip explains that what-if runs over declared beliefs); the active tab is What-if. The **Root cause** tab gets `.disabled` (inert, tooltip says why); the **As of** control and the card **Display** toolbar are hidden (both are history controls).
- **Node cards** (`buildColdCardSVG`): name + operating point + a sub-line — `low – high · 90% belief` for range-asserted baselines, `derived from parents` for formula nodes (via `computeColdBase()`, which mirrors the engine's derivation using `evalFormula`, a tiny arithmetic parser — never `eval()`). Variants don't apply; what-if overlay values fold in exactly like fitted cards.
- **Edges**: probabilistic edges label at build time with the stated prior via `beliefEdgeLabel` — `β ~ 0.03 [0.01, 0.05] · belief` for Normal priors, `β ~ HalfNormal(0.2) · belief` otherwise. `clearRcaStyles` restores these labels (not blank) when overlays clear.
- **Metric tab**: definition + `Baseline` / `Plausible` rows and a "Cold start" note; the Card display / Time series / Posterior / Analyze sections are omitted entirely.
- **What-if tab**: no baseline window row (a hint explains operating points come from the tree); `buildScenarioPayload` omits `baseline_start/end` (the engine rejects them); the adjust panel's **range strip renders from declared `plausible` bounds** with the 90% baseline-belief band shaded (`updateAdjustPreview`'s `cold` branch) and the amber marker means "outside plausible". Results label outcome cards "cold start — declared beliefs" and append the `baseline belief [lo, hi]` interval (`baseline_ci_95`). Nothing is ever fitted, so run progress skips `fitting` and goes straight to `simulating`.
- **Unchanged by design**: reader mode, deep links (`#whatif=` carries no dates), the source waterfall, and the per-node table.

## What-if tab

1. **Builder**: baseline window date pair (default: last 28 days of data); adjust panel (opens on node tap — mode select %/delta/set, slider, live preview, and a pure-CSS **historical range strip** showing min→max, the ±2σ band, the baseline tick, and an amber marker when the setting extrapolates); "+ Add assumption" form (source metric-or-lever with datalist, target select, %/absolute effect range, note); scenario item list with remove controls; Run/Clear.
2. **Results**: outcome card per affected sink (`baseline → simulated (+%) · Δ CI · P(direction)`) with a **source waterfall** (signed bar per intervention/assumption; sums exactly to the point delta by Shapley efficiency); per-node table (outcome-first); extrapolation warnings; always-on caveats footer. **Both honesty verdicts render per node, not only in the panel-wide Warnings list** (roadmap C26): `node.non_physical` puts a `⊘ Physically impossible` block on the outcome card and a named chip on the table row, with the engine's sentence — indexed out of `res.warnings` by metric, so it is never copied into the payload twice. `extrapolation.flag` keeps its bare `⚠`. The two are different claims, and the intervened node that carries the impossible value is usually not a sink, so before this it appeared only in a list under the fold.
3. **Reader mode**: entering via a `#whatif=` deep link renders results first with the builder collapsed behind `<details>Edit scenario</details>`.

## Root cause tab

1. **Target summary card**: gap in business units, baseline → actual, relative change, the two windows.
2. **Ranked causes**: one row per cause — rank, metric name, horizontal score bar, "via <child>". Clicking a row selects the node and highlights the path from cause to target.
3. **Slice panel** (per ranked cause, when its metric declares `dimensions` — read from `/dag`, which carries each node's full definition; `/meta` does not): a `slice by <dim>` chip row, toggling `POST /rca/{name}/slices` into an inline panel. Windows come from the contribution that *measured* that metric — `parent_windows` when the edge is lagged, else the node's `effective_windows` — so a slice across a lag compares the shifted periods, not the target's calendar ones. Renders a verdict line, a four-column table (flows: share of gap / baseline share / excess; rates: within / mix / excess, plus the `mix_total` note), the windows used, and any reconciliation or caveat blocks. The verdict is read straight off the payload's `localization` (three states, decided in `engine/slices.py:_localization`; the panel never re-derives the rule — C24 was three copies of it drifting apart): `localized` prints "*X carries N% of the gap*" and tints the leading row; `not_localized` prints the muted refusal, because ranking always yields a first row and without the floor the panel would name a slice even when the gap is spread evenly; `long_tail` prints a warn-toned block saying the concentration is in the `__other__` roll-up — not a segment anyone can act on — with the payload's own `localization_remedy` sentence (raise `top_k`, pin `values:`, or slice another dimension) printed on screen rather than re-worded here (roadmap 2.21). The leading row is tinted **only** under `localized`, so a highlighted row never contradicts the sentence above it. Slice state is scoped to one analysis and cleared on re-run or Clear.
4. **Attribution detail**: per child node, a contributions table plus the `unexplained` remainder. **The `baseline → actual` line names what those two numbers are**, via `windowBasis(node)` / `windowBasisHtml(node)` — shared by all four surfaces that print it (the target card, the degraded-node card, the export's target line and the export's per-node line). "window means" is true of a flow and a stock and of no rate at all: a rate reads *component aggregate* (`Σnum / Σden`) or *period means — <why>*, where the why distinguishes a metric that has **no** denominator (a median: this mean is the only number there is) from a tree nobody has finished declaring one on. The distinction is in the label rather than a tooltip for the same reason `unexplainedRow`'s is — a label survives the export, a hover does not — and the export additionally prints `window_aggregate_reason` in full. **The `unexplained` row is built by `unexplainedRow(node)` in both the live table and the exported report, never from a string literal** (pinned by `tests/test_project_invariants.py`): a derived node's `unexplained_status: "definitional"` renames the row to *unexplained — none by definition* and withholds its share, because a zero that means "the identity reconciled" and a zero that means "nobody checked" are the same character on screen. The export additionally carries the reason as a `caveatBlock` paragraph — a static report has no hover, and the export is what circulates without its author. Formula (Shapley) nodes are **two-level** with a global Headline/Detailed toggle: **Headline** (default) shows each parent's window-means-bridge contribution plus one explicit italic *co-movement shift* row (from the response's per-contribution `decomposition` and node-level `interaction`); **Detailed** shows the full per-parent split (means + co-movement = total Δ, CI, P(dir)). Posterior nodes always show the flat table (estimate, share, 95% CI, P(direction)). Non-day nodes note their grain and snapped windows in the block header; single-period windows render "—" for withheld CIs. A **lagged** contribution carries a `lag N weeks` chip beside the parent name, with the shifted `parent_windows` in its tooltip (`lagChip`/`lagWindowText`) — a row measured over a different fortnight than the header names cannot be the one thing the table leaves out. Every P(dir) cell goes through `pctDir`, which prints a **censored** direction probability as a bound (`>99.8%`) and never lets a value below 1 round up to `100.0%`.

### Degraded nodes (`status` / `ci_status`)

`POST /rca/{name}` degrades a node rather than failing the whole tree: any `status` other than `"ok"` means that node was reported **without attribution**, carrying the engine's own sentence in `status_reason`. Three statuses arrive today — `window_shorter_than_grain`, `fit_failed`, `attribution_failed` — and the distinction the UI must preserve is **which part of the record survived**: a too-short window loses the numbers themselves, whereas a fit or attribution failure keeps real `baseline`/`actual`/`gap` (measured from the data, not the model) and loses only the decomposition.

**The cardinal sin this guards against is rendering a degraded node as an analyzed one that simply found nothing.** An empty contributions table plus a null `attribution_method` otherwise reads as *"posterior, no drivers"* — the engine saying "I couldn't" presented as "there is nothing there", which is the worst available misreading. So:

- **`NODE_STATUS` is one vocabulary, used by every consumer** — canvas overlay, ranked causes, attribution detail, the exported report — with `label` (noun phrase), `short` (chip) and `explains` (which part of the record survived). Four renderers inventing four phrasings for the same condition is how the distinction erodes.
- **`nodeStatus()` surfaces unknown statuses verbatim** rather than swallowing them. A status this build has never heard of is still not `ok`, and treating it as ok is precisely the failure this block exists to prevent.
- **A degraded *target*** gets an explicit line saying the ranked causes below carry no information about it, since nothing was attributed to it.

**The exported report is the surface with no hover.** `buildRcaReportHtml` renders the same run as standalone HTML that circulates without its author, so every caveat the live view hides behind a `title=` must be printed there in full: `ci_status` notes, suspect fits, seasonality warnings, the **fit window** (`fitted on N weeks (start → end)` — the live header has carried it since 2.14 and the export did not, so a node flagged suspect shipped that verdict with no way to see what the fit was made of), and a lagged contribution's shifted windows spelled out as a line under the parent name rather than a chip. When you add a fact to one surface, add it to both.

`CI_STATUS_NOTE` does the same for `ci_status`, and **all non-ok values are surfaced**, including `nonfinite_bootstrap_replicates`: rendering a note for one value and nothing for the others reads as "interval checked and fine" when it means "not said". Each entry carries a `why` explaining that withheld or subset intervals leave the **point estimates unaffected** — they are the exact Shapley values, never bootstrap means.

## Metric tab

Name + type chip (Source / Probabilistic / Formula) + fitted chip. Description, source path, grain + kind row, parents (with lag badges in grain steps — `lag Nd` for daily nodes, `lag N week(s)` etc. for coarser). Time series chart (Plotly line, ~200px) with the reference window shaded gray and the analysis window shaded indigo whenever RCA windows are set.

If fitted: a caption stating how to read a coefficient, then the table (parent, `beta_raw` mean, 95% HDI) mapped from `beta_raw[i]` by parent order, then the diagnostics line, then the full ArviZ summary behind `<details>`.

**Diagnostics say which case they are in.** NUTS — the default, and so the common case — renders `max R̂ … · N divergences · min ESS …`, the same three quantities `_nuts_diagnostics` thresholds on, so the UI and the engine's `fit_quality` agree about what matters. An ADVI fit renders **`PSIS k̂ = … (close to the posterior | measurably off | not usable)`** — roadmap S2's diagnostic, the one that applies to an approximation, always with its band spelled out so the number is never left for the reader to threshold from memory. Only a fit with *neither* — no MCMC diagnostics and a k̂ the engine could not compute — falls through to **"ADVI approximation — unchecked"**, rather than the empty string this used to render: rendering nothing reads as *"no problems found"* when it means *"not checked"*. That distinction is UC4's whole job.

**Every k̂ state is a warning, and the absence of one is not.** Since NUTS became the default (roadmap S2's second half), a k̂ exists only where the request asked for the approximation — so `khatNote()` returns `null` for `ok` *and* for the common case of a node with no k̂ at all, and every state it does return renders as a ⚠. `khatNote()` (beside `ciStatusNote`) is the single lookup behind every surface — node card, RCA header, what-if card, what-if table, exported report — with the same unknown-status fallback: a band this build cannot name is printed verbatim rather than treated as fine. Watch the wording when editing it: the diagnostics hint used to say "a fit with no k̂ was not checked, which is not a clean bill of health", and under the new default that sentence libels the *best* fit on the page. The genuinely unchecked case is `unavailable`, and it is named as such.

**k̂ carries its own error, and `khatNote()` takes the node to see it (roadmap S22).** k̂ is estimated from 1,000 sampled importance ratios, and `khat_se` — the engine's Monte-Carlo standard error for it — is around 0.15–0.2, most of the width of the `suspect` band. So the number is rendered through **`khatFigure(node)`** as `1.36 ± 0.22` everywhere it appears (diagnostics line, node card, RCA header, what-if card, exported report), never bare: an estimate printed without its error is read as exact. Where `khat_borderline` is true the diagnostics line adds *"band unresolved at this error"* and the chip class drops from `ok` to `warn`.

That flag is also why `khatNote()` takes the **node** rather than the bare `khat_status` it used to. A borderline `ok` k̂ is the one state where the status alone says "nothing to report" and the payload says otherwise — a function given only the status would render silence over a fit the engine has flagged `suspect`, which is the fifth rule's failure mode with the sign reversed. It gets its own `KHAT_NOTE.borderline` entry ("approximation check inconclusive"); a *flagged* band that is borderline keeps its own label and gains a sentence in `why`. The `fit_quality: "suspect"` explanation in the diagnostics verdict block gained a matching branch — without it a borderline fit was explained by the ELBO sentence, an account of a check that had passed.

**Analyze controls** are `Method` (NUTS default, matching the route and every other fitting path) and `Draws`, both labelled — the number was previously a bare box, and it does not mean the same thing twice over. A `.control-note` under it always shows what the current setting costs: NUTS → `500 × 4 chains, after 1,000 discarded tuning steps — 2,000 posterior draws`; ADVI → `20,000 optimization steps, then 500 samples drawn from the fitted approximation`. The ADVI wording is the load-bearing one: `pm.fit(n=20_000)` is fixed, so `draws` only samples the *already-fitted* approximation and raising it buys smoothness, not accuracy. Anyone reading the control as "more = better" is wrong in exactly that case.

### Inline help (`HINTS` / `hintHTML` / `hintSlot`)

The product asks a business user to read posteriors, so the UI teaches as it goes. Two mechanisms, and the split is deliberate:

- A **control note** is always visible and states what the current setting concretely does. No interaction, no discovery problem.
- A **hint** is a quiet `ⓘ` that expands an inline `.hint-panel` into a slot the caller placed with `hintSlot(id)`. Explicit placement rather than walking up the DOM for a container; inline expansion rather than a floating popover, which would need positioning code this file otherwise doesn't have. `title=` was rejected outright — undiscoverable, and dead on touch.

`HINTS[id].body` is a **function**, so a hint can read live control state: this is why `draws` describes two different meanings, and why `wireAnalyzeNote()` re-renders an open `draws` panel when the method changes. Entries carry an optional `more` link into `docs/model.md` — explanations short enough to answer the question live here, but anything with real depth links out rather than forking the doc's prose, since the doc is the copy kept true.

One **delegated** click listener handles every hint in the app, bound once at module scope. Per-button listeners would leak on every node click, since the sidebar tabs are re-rendered wholesale rather than mutated.

Current hints: `method` (NUTS vs ADVI, carrying the S2 measurement — mean-field fails PSIS on essentially every real node here, and moves point estimates, not just intervals), `draws`, `beta`, `hdi` (including that an interval straddling zero means *unproven*, not *no effect*), `diagnostics`.

## Tech choices

- **Cytoscape.js + cytoscape-dagre** (CDN) for the graph. Kept over React Flow: already in use, zero build step, dagre gives proper layered DAG layout. This doc supersedes the earlier React Flow plan.
- **Plotly.js** (CDN) for time series — window shading via layout shapes, good hover for free.
- **Vanilla JS** (`breakdown/static/app.js`), one stylesheet (`breakdown/static/style.css`), one `index.html`. No framework until the UI outgrows a single file. The files live *inside* the package so the wheel ships them (served via `importlib.resources`, still no build step).

## API surface consumed

| Endpoint | Used for |
|---|---|
| `GET /health` | first request in `init()`; on `status: "degraded"` show `#degraded-banner` with the startup error + a `breakdown doctor` hint and skip loading the DAG |
| `GET /trees` | second request in `init()`, before any tree is chosen — the index, and which tree the switcher is on |
| `POST /trees/{id}/load` | the index's **Load** button |
| `GET /meta` | metric names, data date range, provider, fitted list — bootstraps header controls |
| `GET /dag` | nodes + edges, each node's full definition (this is where the slice panel reads `dimensions`; `/meta` does not carry them) |
| `GET /series` | every metric's native-grain series, per-metric `{grain, dates, values}` (one call) — hydrates the node cards |
| `GET /metrics/{name}` | definition, time series, posterior summary |
| `GET /metrics/{name}/query` | the Metric tab's **show query** provenance panel (see below) |
| `POST /analyze/{name}` | fit from the Metric tab |
| `POST /rca/{name}` | the RCA run (`run_id` opts into progress reporting) |
| `POST /rca/{name}/slices` | the per-cause slice panel in the Root cause tab |
| `POST /simulate` | the what-if scenario run (JSON body: baseline window, interventions, assumptions, levers; `run_id` opts into progress) |
| `GET /progress/{run_id}` | polled ~2.5×/s while a run is in flight — the live stage of that run |

Every one of these except `GET /trees` and `GET /progress/{run_id}` is requested through `treePath()`, so it carries the tree id.

**Transient failures vs. answers.** `api()` attaches the HTTP status to the error it throws, and `apiWithWake()` retries `502/503/504` and outright fetch rejections with exponential backoff (~23s of patience over 6 attempts); every other status is an answer, not an outage, and is rethrown at once. `init()` uses it for the `/health` probe — on a host that suspends idle instances that request *is* what boots the machine — and reports "Waking the server up…" while it retries instead of leaving a blank page. If it still fails, `#retry-banner` (amber, distinct from `#degraded-banner`'s red, which means *misconfigured* rather than *unreachable*) offers a **Try again** button that re-runs `init()` without a page reload. The case this exists for is the hosted demo taking the server away mid-session, not first load.

States to handle everywhere: loading, empty (no fit yet), error (surface the API `detail` string in the status area, never a silent failure).

### The UI is unauthenticated, and two server-side gates act on that

Neither is a frontend feature; both change what the UI receives, so they belong here.

- **`BREAKDOWN_REQUIRE_AUTH` gates every request in the table above.** `/ui` itself stays open — it is a JS bundle, not data — but every fetch it makes needs `Authorization: Bearer <BREAKDOWN_API_TOKEN>`, and the browser will not add one. That mode therefore assumes **a reverse proxy injecting the header** (Cloudflare Access and the like), or an operator who accepts that the browser cannot use it. There is deliberately no login, no cookie and no token-in-the-URL in `app.js`: that would be hosted mode (roadmap 3.5). If you are debugging a UI that loads and then fails every call with 401, this is why.
- **`GET /dag` redacts `sql` and `bind` to `null`** whenever `BREAKDOWN_API_TOKEN` is set and the caller presents no token — which, per the point above, is the normal state for the browser. Anything rendering a definition must treat those two fields as **absent rather than empty**. The Metric tab's query panel is unaffected because it reads `GET /metrics/{name}/query`, not `/dag`.

## Deep links

The URL hash makes analyses shareable and the UI scriptable:

- `#tree=<id>` — **parsed first**, and gates everything below it: `#metric=`/`#rca=`/`#whatif=` are meaningless without knowing whose metric names they refer to. Omitted when the process serves one tree, so every URL shared before this feature still resolves; absent, it means the default tree.
- `#metric=<name>` — opens the Metric tab for that node on load.
- `#rca=<target>&reference_start=…&reference_end=…&analysis_start=…&analysis_end=…` — sets the controls and re-runs the RCA on load. Reference params are optional: present → custom mode (exact replay); absent → auto mode (the server picks the reference). After every run the hash is rewritten from the **resolved** windows in the response, so a defaulted run's link replays byte-identically even if the server later boots with a different `--start-date`.
- `#whatif=<URI-encoded scenario JSON>` — replays a what-if scenario on load in reader mode (results first, builder collapsed).

The hash is kept in sync via `history.replaceState` as the user selects metrics or completes RCA / what-if runs.

## Query provenance (roadmap 2.11)

The Metric tab's **Source** row carries a `show query` toggle that fills a
`.query-provenance` panel from `GET /metrics/{name}/query`. Wired by
`wireQueryProvenance()` after each write to `#tab-metric`, since the tab is
re-rendered rather than mutated.

The panel is deliberately quiet — evidence a reader goes looking for, not
chrome. Two states matter and both are shown:

- **A query exists.** Header is `provider · dialect`, plus `· not executed`
  when the series came from a snapshot. The note below the SQL says so in
  words. Dropping it would let a reader assume the statement they are looking
  at is the one that ran.
- **No query exists.** The provider's own reason is shown instead of an error.
  `mock` synthesizes; `local` and `cloud` hand a metric name to someone else's
  planner and never see SQL. "We never see the query" and "no query is run" are
  different facts about how much a reader can verify, so the API distinguishes
  them and the UI repeats the distinction rather than flattening it to
  "unavailable".

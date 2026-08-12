# Frontend UI: Design & Implementation

The breakdown UI is a single-page app served by FastAPI at `/ui`, in the spirit of `dbt docs serve`: no build step, vanilla JS, dependencies from CDN. It is the visual layer over the metric tree and the RCA engine.

## Use cases (in priority order)

**UC1 — Triage an anomaly (the headline).** "Revenue dropped over the weekend — what drove it?" The user picks a target metric and two time windows, runs RCA, and reads the answer from the graph itself: nodes tinted by how much each metric moved, edges weighted by how much of the child's gap each parent explains, and a ranked cause list with uncertainty. This is the late-night-CFO-call workflow from the README, compressed into one screen.

**UC2 — Explore the tree.** A stakeholder or new analyst opens the UI to understand how the business is wired: which metrics exist, which relationships are arithmetic identities vs learned, where the data comes from. The graph must read clearly at a glance without any analysis having been run.

**UC3 — Inspect one metric.** Click a node: see its time series, definition (source, parents, lags), and — if a model has been fitted — the posterior over each causal coefficient *in business units* (`beta_raw`), with credible intervals. Fit a model from here (`/analyze`, NUTS or ADVI) without touching curl.

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

- **Header** holds only the globals: the target select (the tree-wide focus metric), the **As of** anchor date, a **Share** menu (Copy link — the deep-link URL restoring the exact view: selected metric, RCA run, or what-if scenario — and Download RCA result as JSON, enabled after a run; the future home of the exportable RCA report, roadmap 1.5), and — on the right — **two separate slots** (`#header-right`). `#status` is *transient*: live run progress (see **Run progress** below), errors, and the RCA window advisories. `#context` is *ambient*: a chip row saying which tree is loaded (`18 metrics` · provider · loaded window, plus an amber `data → <date>` chip when the tree-wide data edge lags). They are two elements on purpose — they shared one until 2026-08-11, so every completed run permanently overwrote the tree context, and the single element carried a `max-width: 340px` that truncated the context on windows with room to spare. `setStatus(msg, kind, ttlMs)` takes an optional TTL used for success notices ("RCA complete for revenue.", 6s) so a finished run doesn't leave a stale line; errors and advisories never take one and stand until something replaces them.
- **RCA setup lives in the Root cause tab** (`#rca-setup`, persistent markup above the `#rca-results` render target), and is **analysis-first**: an analysis-window preset select (`last7` / `last14` / `last-full-week` / `custom`; too-short presets omitted) + the analysis date pair, then the reference pair with an **auto** chip. `state.refMode` is the two-state machine: `"auto"` (default) means the reference inputs preview the server's matched adjacent block (`defaultReferenceJS` mirrors `grains.default_reference_window`, with week alignment and coarsest grain derived from the selected target's ancestor scope via `referenceAlignment`) and the request **omits** the reference params — the server's computation is the number of record and the response overwrites the inputs; `"custom"` (any manual reference edit) sends all four dates. Analysis edits flip the preset to Custom but keep the refMode; the auto chip restores auto; target changes recompute the auto reference (scope changes alignment). `validateWindows` keeps the hard rules and adds muted advisories: whole-week (scope-keyed), short reference (< ~4 coarsest-grain periods), and non-adjacent gap (trend contamination). A `#history-nudge` line appears when `/meta.earliest_available` shows provider history before `date_start`. Setup sits with its results, so the windows are unambiguously RCA parameters rather than global filters.
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
  stage.** `descending the ELBO` is what ADVI does; `permuting coalitions` is
  what exact Shapley attribution does. The vocabulary is *variational*, not
  MCMC, because every on-demand fit is ADVI — `warming up the chains` would be
  a lie here. If a phrase is moved to a stage where it stops being true it is
  no longer a joke, just wrong. Entering a stage resets to the first phrase, so
  the line a reader lands on names the phase just entered.
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
- **Numbers.** Big number = latest value. Delta = latest vs `deltaLen` points earlier (both lengths are canvas-wide controls). Sparkline = trailing `sparkLen` points, thin line + area fill + endpoint dot, colored by direction (semantic green/red, never the indigo brand hue).
- **Formatting** comes from the metric definition's optional `format` (`{style, unit, decimals, compact, symbol}`); a `unit` renders a small caption under the value and grows the card one line.
- **Goodness coloring** maps through the metric's `direction` via `goodDir`/`goodClass`: for `down_is_good` metrics an upward move colors red (arrow stays ▲); `neutral` metrics color gray and get no RCA/what-if tint. Applies to card delta pills, sparkline color, RCA node/edge tints, what-if tints, and the sidebar gap headlines — legend swatches read improved/worsened, with arrows carrying direction.
- **Overlay-aware.** While an RCA or what-if overlay is active the card folds in that overlay's numbers (RCA gap %, what-if simulated value) with `◌` / `⊙` / `⚠` marks, so a node never shows two conflicting deltas. State lives in `state.cardOverlay`; the renderer is `renderNodeCard` / `buildCardSVG` in `static/app.js`.
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
2. **Results**: outcome card per affected sink (`baseline → simulated (+%) · Δ CI · P(direction)`) with a **source waterfall** (signed bar per intervention/assumption; sums exactly to the point delta by Shapley efficiency); per-node table (outcome-first); extrapolation warnings; always-on caveats footer.
3. **Reader mode**: entering via a `#whatif=` deep link renders results first with the builder collapsed behind `<details>Edit scenario</details>`.

## Root cause tab

1. **Target summary card**: gap in business units, baseline → actual, relative change, the two windows.
2. **Ranked causes**: one row per cause — rank, metric name, horizontal score bar, "via <child>". Clicking a row selects the node and highlights the path from cause to target.
3. **Slice panel** (per ranked cause, when its metric declares `dimensions` — read from `/dag`, which carries each node's full definition; `/meta` does not): a `slice by <dim>` chip row, toggling `POST /rca/{name}/slices` into an inline panel. Windows come from the contribution that *measured* that metric — `parent_windows` when the edge is lagged, else the node's `effective_windows` — so a slice across a lag compares the shifted periods, not the target's calendar ones. Renders a verdict line, a four-column table (flows: share of gap / baseline share / excess; rates: within / mix / excess, plus the `mix_total` note), the windows used, and any reconciliation or caveat blocks. The verdict claims localization only when the leader is not `noise_level` **and** its excess is ≥25% of the gap — ranking always yields a first row, so without that floor the panel would name a slice even when the gap is spread evenly. Slice state is scoped to one analysis and cleared on re-run or Clear.
4. **Attribution detail**: per child node, a contributions table plus the `unexplained` remainder. Formula (Shapley) nodes are **two-level** with a global Headline/Detailed toggle: **Headline** (default) shows each parent's window-means-bridge contribution plus one explicit italic *co-movement shift* row (from the response's per-contribution `decomposition` and node-level `interaction`); **Detailed** shows the full per-parent split (means + co-movement = total Δ, CI, P(dir)). Posterior nodes always show the flat table (estimate, share, 95% CI, P(direction)). Non-day nodes note their grain and snapped windows in the block header; single-period windows render "—" for withheld CIs; nodes skipped as `window_shorter_than_grain` are listed in the RCA card.

## Metric tab

Name + type chip (Source / Probabilistic / Formula) + fitted chip. Description, source path, grain + kind row, parents (with lag badges in grain steps — `lag Nd` for daily nodes, `lag N week(s)` etc. for coarser). Time series chart (Plotly line, ~200px) with the reference window shaded gray and the analysis window shaded indigo whenever RCA windows are set.

If fitted: a caption stating how to read a coefficient, then the table (parent, `beta_raw` mean, 95% HDI) mapped from `beta_raw[i]` by parent order, then the diagnostics line, then the full ArviZ summary behind `<details>`.

**Diagnostics say which case they are in.** NUTS renders `max R̂ … · N divergences · min ESS …` — the same three quantities `_nuts_diagnostics` thresholds on, so the UI and the engine's `fit_quality` agree about what matters. An ADVI fit renders **"ADVI approximation — no convergence diagnostics"** rather than the empty string it used to: R̂/divergences/ESS are MCMC-only, and rendering nothing reads as *"no problems found"* when it means *"not checked"*. That distinction is UC4's whole job.

**Analyze controls** are `Method` (ADVI default) and `Draws`, both labelled — the number was previously a bare box, and it does not mean the same thing twice over. A `.control-note` under it always shows what the current setting costs: NUTS → `500 × 4 chains, after 1,000 discarded tuning steps — 2,000 posterior draws`; ADVI → `20,000 optimization steps, then 500 samples drawn from the fitted approximation`. The ADVI wording is the load-bearing one: `pm.fit(n=20_000)` is fixed, so `draws` only samples the *already-fitted* approximation and raising it buys smoothness, not accuracy. Anyone reading the control as "more = better" is wrong in exactly that case.

### Inline help (`HINTS` / `hintHTML` / `hintSlot`)

The product asks a business user to read posteriors, so the UI teaches as it goes. Two mechanisms, and the split is deliberate:

- A **control note** is always visible and states what the current setting concretely does. No interaction, no discovery problem.
- A **hint** is a quiet `ⓘ` that expands an inline `.hint-panel` into a slot the caller placed with `hintSlot(id)`. Explicit placement rather than walking up the DOM for a container; inline expansion rather than a floating popover, which would need positioning code this file otherwise doesn't have. `title=` was rejected outright — undiscoverable, and dead on touch.

`HINTS[id].body` is a **function**, so a hint can read live control state: this is why `draws` describes two different meanings, and why `wireAnalyzeNote()` re-renders an open `draws` panel when the method changes. Entries carry an optional `more` link into `docs/model.md` — explanations short enough to answer the question live here, but anything with real depth links out rather than forking the doc's prose, since the doc is the copy kept true.

One **delegated** click listener handles every hint in the app, bound once at module scope. Per-button listeners would leak on every node click, since the sidebar tabs are re-rendered wholesale rather than mutated.

Current hints: `method` (ADVI vs NUTS, and that ADVI's mean-field assumption understates uncertainty), `draws`, `beta`, `hdi` (including that an interval straddling zero means *unproven*, not *no effect*), `diagnostics`.

## Tech choices

- **Cytoscape.js + cytoscape-dagre** (CDN) for the graph. Kept over React Flow: already in use, zero build step, dagre gives proper layered DAG layout. This doc supersedes the earlier React Flow plan.
- **Plotly.js** (CDN) for time series — window shading via layout shapes, good hover for free.
- **Vanilla JS** (`breakdown/static/app.js`), one stylesheet (`breakdown/static/style.css`), one `index.html`. No framework until the UI outgrows a single file. The files live *inside* the package so the wheel ships them (served via `importlib.resources`, still no build step).

## API surface consumed

| Endpoint | Used for |
|---|---|
| `GET /health` | first request in `init()`; on `status: "degraded"` show `#degraded-banner` with the startup error + a `breakdown doctor` hint and skip loading the DAG |

**Transient failures vs. answers.** `api()` attaches the HTTP status to the error it throws, and `apiWithWake()` retries `502/503/504` and outright fetch rejections with exponential backoff (~23s of patience over 6 attempts); every other status is an answer, not an outage, and is rethrown at once. `init()` uses it for the `/health` probe — on a host that suspends idle instances that request *is* what boots the machine — and reports "Waking the server up…" while it retries instead of leaving a blank page. If it still fails, `#retry-banner` (amber, distinct from `#degraded-banner`'s red, which means *misconfigured* rather than *unreachable*) offers a **Try again** button that re-runs `init()` without a page reload. The case this exists for is the hosted demo taking the server away mid-session, not first load.
| `GET /meta` | metric names, data date range, provider, fitted list — bootstraps header controls |
| `GET /dag` | nodes + edges |
| `GET /series` | every metric's native-grain series, per-metric `{grain, dates, values}` (one call) — hydrates the node cards |
| `GET /metrics/{name}` | definition, time series, posterior summary |
| `POST /analyze/{name}` | fit from the Metric tab |
| `POST /rca/{name}` | the RCA run (`run_id` opts into progress reporting) |
| `POST /simulate` | the what-if scenario run (JSON body: baseline window, interventions, assumptions, levers; `run_id` opts into progress) |
| `GET /progress/{run_id}` | polled ~2.5×/s while a run is in flight — the live stage of that run |

States to handle everywhere: loading, empty (no fit yet), error (surface the API `detail` string in the status area, never a silent failure).

## Deep links

The URL hash makes analyses shareable and the UI scriptable:

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

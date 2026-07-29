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
│ breakdown   Target [revenue ▾]   As of [date]          status text   │
├────────────────────────────────────────────┬─────────────────────────┤
│ (Display ▸)                                │  [Metric] [Root cause]  │
│              DAG canvas                    │  RCA tab: setup panel   │
│   (Cytoscape + dagre, KPIs at top,         │  (windows preset, ref/  │
│    sources at bottom — rankDir BT)         │  analysis dates, Run/   │
│                                            │  Clear) above results   │
│  legend (bottom-left overlay)              │  (scrolls)              │
└────────────────────────────────────────────┴─────────────────────────┘
```

- **Header** holds only the globals: the target select (the tree-wide focus metric), the **As of** anchor date, a **Share** menu (Copy link — the deep-link URL restoring the exact view: selected metric, RCA run, or what-if scenario — and Download RCA result as JSON, enabled after a run; the future home of the exportable RCA report, roadmap 1.5), and a status area for progress ("Fitting upstream models…") and errors.
- **RCA setup lives in the Root cause tab** (`#rca-setup`, persistent markup above the `#rca-results` render target): windows preset + two date-range pairs (prefilled from `/meta`: reference = first 60% of the data window, analysis = the rest) + Run/Clear. Setup sits with its results, so the windows are unambiguously RCA parameters rather than global filters.
- **Canvas** is the primary surface; the graph is the product. Dagre layered layout with `rankDir: 'BT'` so the tree reads like a KPI tree: outcome metrics on top, drivers below. The card display options (variant / sparkline length / delta length) collapse behind a quiet **Display** toggle in the top-left toolbar.
- **Sidebar** (410px) has three tabs: **Metric** (UC3/UC4), **Root cause** (UC1), and **What-if** (UC5). Clicking a node opens Metric — unless the What-if tab is active, in which case it opens that node's adjust panel. Finishing an RCA run switches to Root cause.
- **Overlay exclusivity**: the RCA and what-if overlays never coexist; the active tab owns the canvas (switching to Root cause or What-if re-applies that tab's overlay via a shared `clearOverlays()`), and the Metric tab keeps whichever overlay was last showing.

## Visual language

Light theme, quiet by default so RCA color can carry meaning when it appears.

| Token | Value | Use |
|---|---|---|
| bg / panel | `#f6f7f9` / `#ffffff` | canvas / cards, sidebar |
| border | `#e2e6ea` | hairlines |
| text / muted | `#1a202c` / `#64748b` | |
| accent (indigo) | `#4f46e5`, soft `#eef2ff` | probabilistic edges, buttons, fitted tint |
| formula (violet) | `#9333ea` | formula-node border + deterministic edges |
| up / down | `#16a34a` / `#dc2626` (soft `#dcfce7` / `#fee2e2`) | RCA gap direction |

**Nodes** are white round-rects rendered as **stat cards** (see *Node cards* below): a big number, an optional period-over-period delta, and an optional sparkline. Border color encodes type — gray `#94a3b8` for source metrics (no parents), indigo for probabilistic, violet for formula nodes. A fitted model tints the node background `#eef2ff`. Selected node gets the accent border.

**Edges** point parent → child. Deterministic edges (child has a `formula`) are **solid violet**; probabilistic edges are **dashed indigo**. When a probabilistic child is fitted, its incoming edges label with the raw-scale coefficient: `β 0.10 [0.08, 0.13]`.

### Node cards

Each metric node is drawn as a **stat card** rather than a bare label. The card is
an SVG data-URI set as the Cytoscape node's `background-image` with a **transparent
background**, so the node's own border and `background-color` still render behind it
— which is why the RCA / what-if / selection overlays (all class-driven border/fill)
keep working unchanged.

- **Variants** (increasing detail): `num` (big number only) · `delta` (+ period-over-period delta pill) · `spark` (+ trailing sparkline) · `full` (both). Set canvas-wide via the floating toolbar (top-left), or overridden per node from the Metric tab (an indigo dot marks an overridden node). Config + overrides persist to `localStorage` under `breakdown.cardConfig`.
- **As-of anchor.** The toolbar's **As of** date input anchors every card's headline/delta/sparkline: only periods *fully completed* by the date count (a weekly point needs its Sunday ≤ as-of). Defaults to the tree-wide data edge — the min of `/meta`'s per-metric `data_through` — so a lagging source mart shows its true last day instead of a zero-filled tail, and a half-loaded calendar week never becomes a weekly headline. Not persisted (freshness moves daily); the header status line appends `data → <date>` when the edge lags the window end, and the Metric tab shows each metric's own `Data through` (with a "lags window end" chip when behind).
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

## What-if tab

1. **Builder**: baseline window date pair (default: last 28 days of data); adjust panel (opens on node tap — mode select %/delta/set, slider, live preview, and a pure-CSS **historical range strip** showing min→max, the ±2σ band, the baseline tick, and an amber marker when the setting extrapolates); "+ Add assumption" form (source metric-or-lever with datalist, target select, %/absolute effect range, note); scenario item list with remove controls; Run/Clear.
2. **Results**: outcome card per affected sink (`baseline → simulated (+%) · Δ CI · P(direction)`) with a **source waterfall** (signed bar per intervention/assumption; sums exactly to the point delta by Shapley efficiency); per-node table (outcome-first); extrapolation warnings; always-on caveats footer.
3. **Reader mode**: entering via a `#whatif=` deep link renders results first with the builder collapsed behind `<details>Edit scenario</details>`.

## Root cause tab

1. **Target summary card**: gap in business units, baseline → actual, relative change, the two windows.
2. **Ranked causes**: one row per cause — rank, metric name, horizontal score bar, "via <child>". Clicking a row selects the node and highlights the path from cause to target.
3. **Attribution detail**: per child node, a contributions table plus the `unexplained` remainder. Formula (Shapley) nodes are **two-level** with a global Headline/Detailed toggle: **Headline** (default) shows each parent's window-means-bridge contribution plus one explicit italic *co-movement shift* row (from the response's per-contribution `decomposition` and node-level `interaction`); **Detailed** shows the full per-parent split (means + co-movement = total Δ, CI, P(dir)). Posterior nodes always show the flat table (estimate, share, 95% CI, P(direction)). Non-day nodes note their grain and snapped windows in the block header; single-period windows render "—" for withheld CIs; nodes skipped as `window_shorter_than_grain` are listed in the RCA card.

## Metric tab

Name + type chip (Source / Probabilistic / Formula) + fitted chip. Description, source path, grain + kind row, parents (with lag badges in grain steps — `lag Nd` for daily nodes, `lag N week(s)` etc. for coarser). Time series chart (Plotly line, ~200px) with the reference window shaded gray and the analysis window shaded indigo whenever RCA windows are set. If fitted: coefficient table (parent, `beta_raw` mean, 95% HDI) mapped from `beta_raw[i]` by parent order, small diagnostics line (max R-hat, `sigma_obs`), and the full ArviZ summary behind `<details>`. Analyze controls: method (ADVI default — fast; NUTS for accuracy), draws, run button with inline busy state.

## Tech choices

- **Cytoscape.js + cytoscape-dagre** (CDN) for the graph. Kept over React Flow: already in use, zero build step, dagre gives proper layered DAG layout. This doc supersedes the earlier React Flow plan.
- **Plotly.js** (CDN) for time series — window shading via layout shapes, good hover for free.
- **Vanilla JS** (`breakdown/static/app.js`), one stylesheet (`breakdown/static/style.css`), one `index.html`. No framework until the UI outgrows a single file. The files live *inside* the package so the wheel ships them (served via `importlib.resources`, still no build step).

## API surface consumed

| Endpoint | Used for |
|---|---|
| `GET /health` | first request in `init()`; on `status: "degraded"` show `#degraded-banner` with the startup error + a `breakdown doctor` hint and skip loading the DAG |
| `GET /meta` | metric names, data date range, provider, fitted list — bootstraps header controls |
| `GET /dag` | nodes + edges |
| `GET /series` | every metric's native-grain series, per-metric `{grain, dates, values}` (one call) — hydrates the node cards |
| `GET /metrics/{name}` | definition, time series, posterior summary |
| `POST /analyze/{name}` | fit from the Metric tab |
| `POST /rca/{name}` | the RCA run |
| `POST /simulate` | the what-if scenario run (JSON body: baseline window, interventions, assumptions, levers) |

States to handle everywhere: loading, empty (no fit yet), error (surface the API `detail` string in the status area, never a silent failure).

## Deep links

The URL hash makes analyses shareable and the UI scriptable:

- `#metric=<name>` — opens the Metric tab for that node on load.
- `#rca=<target>&reference_start=…&reference_end=…&analysis_start=…&analysis_end=…` — sets the header controls and re-runs the RCA on load.
- `#whatif=<URI-encoded scenario JSON>` — replays a what-if scenario on load in reader mode (results first, builder collapsed).

The hash is kept in sync via `history.replaceState` as the user selects metrics or completes RCA / what-if runs.

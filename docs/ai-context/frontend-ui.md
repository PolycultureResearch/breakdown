# Frontend UI: Design & Implementation

The breakdown UI is a single-page app served by FastAPI at `/ui`, in the spirit of `dbt docs serve`: no build step, vanilla JS, dependencies from CDN. It is the visual layer over the metric tree and the RCA engine.

## Use cases (in priority order)

**UC1 — Triage an anomaly (the headline).** "Revenue dropped over the weekend — what drove it?" The user picks a target metric and two time windows, runs RCA, and reads the answer from the graph itself: nodes tinted by how much each metric moved, edges weighted by how much of the child's gap each parent explains, and a ranked cause list with uncertainty. This is the late-night-CFO-call workflow from the README, compressed into one screen.

**UC2 — Explore the tree.** A stakeholder or new analyst opens the UI to understand how the business is wired: which metrics exist, which relationships are arithmetic identities vs learned, where the data comes from. The graph must read clearly at a glance without any analysis having been run.

**UC3 — Inspect one metric.** Click a node: see its time series, definition (source, parents, lags), and — if a model has been fitted — the posterior over each causal coefficient *in business units* (`beta_raw`), with credible intervals. Fit a model from here (`/analyze`, NUTS or ADVI) without touching curl.

**UC4 — Trust the model.** Surface just enough diagnostics (R-hat, observation noise) that a data scientist can tell a healthy fit from a broken one, without drowning a business user in an ArviZ dump. Raw summary stays available behind a collapsible.

## Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ breakdown   Target [revenue ▾]  Ref [date]–[date]  Analysis [date]–  │
│             [date]   (Run RCA) (Clear)                 status text   │
├────────────────────────────────────────────┬─────────────────────────┤
│                                            │  [Metric] [Root cause]  │
│              DAG canvas                    │                         │
│   (Cytoscape + dagre, KPIs at top,         │  sidebar content        │
│    sources at bottom — rankDir BT)         │  (scrolls)              │
│                                            │                         │
│  legend (bottom-left overlay)              │                         │
└────────────────────────────────────────────┴─────────────────────────┘
```

- **Header** holds the RCA controls: target select, two date-range pairs (prefilled from `/meta`: reference = first 60% of the data window, analysis = the rest), Run/Clear, and a status area for progress ("Fitting upstream models…") and errors.
- **Canvas** is the primary surface; the graph is the product. Dagre layered layout with `rankDir: 'BT'` so the tree reads like a KPI tree: outcome metrics on top, drivers below.
- **Sidebar** (360px) has two tabs: **Metric** (UC3/UC4) and **Root cause** (UC1). Clicking a node opens Metric; finishing an RCA run switches to Root cause.

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

**Nodes** are white round-rects with the metric name; border color encodes type — gray `#94a3b8` for source metrics (no parents), indigo for probabilistic, violet for formula nodes. A fitted model tints the node background `#eef2ff`. Selected node gets the accent border.

**Edges** point parent → child. Deterministic edges (child has a `formula`) are **solid violet**; probabilistic edges are **dashed indigo**. When a probabilistic child is fitted, its incoming edges label with the raw-scale coefficient: `β 0.10 [0.08, 0.13]`.

**RCA overlay** (applied after a run, removed by Clear):
- Node background shifts to the soft up/down color by sign of `relative_change`; a second label line shows the signed percent (`−16.2%`).
- Edge width scales `2 + 6·min(|share_of_gap|, 1)`; edge color goes up/down by the sign of the contribution `estimate`; edge label shows the share as a percent.
- The legend gains the up/down swatches while the overlay is active.

## Root cause tab

1. **Target summary card**: gap in business units, baseline → actual, relative change, the two windows.
2. **Ranked causes**: one row per cause — rank, metric name, horizontal score bar, "via <child>". Clicking a row selects the node and highlights the path from cause to target.
3. **Attribution detail**: per child node, a table of parent contributions (estimate, share, 95% CI, P(direction)) plus the `unexplained` remainder. Shapley rows show "exact" instead of a CI.

## Metric tab

Name + type chip (Source / Probabilistic / Formula) + fitted chip. Description, source path, parents (with `lag Nd` badges). Time series chart (Plotly line, ~200px) with the reference window shaded gray and the analysis window shaded indigo whenever RCA windows are set. If fitted: coefficient table (parent, `beta_raw` mean, 95% HDI) mapped from `beta_raw[i]` by parent order, small diagnostics line (max R-hat, `sigma_obs`), and the full ArviZ summary behind `<details>`. Analyze controls: method (ADVI default — fast; NUTS for accuracy), draws, run button with inline busy state.

## Tech choices

- **Cytoscape.js + cytoscape-dagre** (CDN) for the graph. Kept over React Flow: already in use, zero build step, dagre gives proper layered DAG layout. This doc supersedes the earlier React Flow plan.
- **Plotly.js** (CDN) for time series — window shading via layout shapes, good hover for free.
- **Vanilla JS** (`static/app.js`), one stylesheet (`static/style.css`), one `index.html`. No framework until the UI outgrows a single file.

## API surface consumed

| Endpoint | Used for |
|---|---|
| `GET /meta` | metric names, data date range, provider, fitted list — bootstraps header controls |
| `GET /dag` | nodes + edges |
| `GET /metrics/{name}` | definition, time series, posterior summary |
| `POST /analyze/{name}` | fit from the Metric tab |
| `POST /rca/{name}` | the RCA run |

States to handle everywhere: loading, empty (no fit yet), error (surface the API `detail` string in the status area, never a silent failure).

## Deep links

The URL hash makes analyses shareable and the UI scriptable:

- `#metric=<name>` — opens the Metric tab for that node on load.
- `#rca=<target>&reference_start=…&reference_end=…&analysis_start=…&analysis_end=…` — sets the header controls and re-runs the RCA on load.

The hash is kept in sync via `history.replaceState` as the user selects metrics or completes RCA runs.

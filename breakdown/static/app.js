/* breakdown UI — metric tree explorer + root cause analysis.
   Vanilla JS over Cytoscape (graph), dagre (layout), Plotly (time series).
   Design doc: docs/ai-context/frontend-ui.md */

const state = {
  meta: null,          // GET /meta
  dag: null,           // GET /dag
  series: null,        // GET /series -> {metrics:{name:{grain, dates:[], values:[]}}}
  cy: null,
  selected: null,      // selected metric name
  metricCache: {},     // name -> GET /metrics/{name} response
  cardConfig: {        // node-card display (canvas-wide, with per-node overrides)
    variant: "full",   // "num" | "delta" | "spark" | "full"
    deltaLen: 7,       // period-over-period comparison length, in data points
    sparkLen: 30,      // sparkline trailing window, in data points
    overrides: {},     // metric name -> variant (per-node override of `variant`)
  },
  cardOverlay: {},     // metric name -> {value,dpct,dir,mark} while RCA / what-if is active (transient)
  asOf: null,          // ISO date anchoring card headlines; defaults to the tree-wide data edge
  rca: null,           // last POST /rca response
  rcaView: "headline", // formula-node attribution view: "headline" | "detailed"
  activeCause: null,   // highlighted ranked cause
  slices: {},          // metric -> {dimension, result|error|loading, lag} for the open slice

  whatif: {            // what-if scenario builder + last POST /simulate result
    baseline: { start: null, end: null },
    interventions: [], // {metric, mode, value} (value already in API units)
    assumptions: [],   // {source, target, effect: {kind, low, high}, note}
    adjusting: null,   // metric name open in the adjust panel
    result: null,      // last POST /simulate response
    readerMode: false, // entered via deep link: results first, builder collapsed
  },
};

const $ = (id) => document.getElementById(id);

/* ---------- helpers ---------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Query provenance (roadmap 2.11). Principle 3 says never ship a number the
// engine can't defend; for most providers a user could not see what was asked,
// which made the number unfalsifiable by the person being asked to trust it.
// `sql: null` is a real answer and gets shown as the provider's reason, not as
// an error — "we never see the SQL" is different from "no query is run", and a
// reader deciding how much to trust a figure needs to know which.
function wireQueryProvenance(root) {
  root.querySelectorAll("button[data-query]").forEach((btn) =>
    btn.addEventListener("click", () => toggleQueryProvenance(btn))
  );
}

async function toggleQueryProvenance(btn) {
  const panel = btn.closest("table")?.parentElement?.querySelector(".query-provenance");
  if (!panel) return;
  if (!panel.hidden) { panel.hidden = true; btn.textContent = "show query"; return; }
  btn.textContent = "hide query";
  panel.hidden = false;
  panel.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const p = await api(`/metrics/${encodeURIComponent(btn.dataset.query)}/query`);
    if (!p.sql) {
      panel.innerHTML = `<p class="muted">${esc(p.note || "No query available.")}</p>`;
      return;
    }
    // The note still matters when there IS SQL: it says whether this statement
    // ran or is what the binding would produce for the loaded window (a
    // snapshot hit serves the number without executing anything). Dropping it
    // would let a reader assume the query they are looking at was the one.
    panel.innerHTML =
      `<div class="query-head">${esc(p.provider)}${p.dialect ? ` · ${esc(p.dialect)}` : ""}` +
      `${p.executed === false ? " · not executed" : ""}</div>` +
      `<pre class="query-sql"><code>${esc(p.sql)}</code></pre>` +
      (p.note ? `<p class="muted">${esc(p.note)}</p>` : "");
  } catch (e) {
    panel.innerHTML = `<p class="muted">Could not load the query: ${esc(e.message)}</p>`;
  }
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch { /* not json */ }
    const err = new Error(detail || `HTTP ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

// A host that suspends idle instances (the hosted demo does) can take the
// server away between one click and the next, and can still be booting when
// the page loads. Both surface as a fetch rejection or a 502/503/504 from the
// edge — transient, and indistinguishable from "down" only if you give up on
// the first try. Retry those with backoff; anything else (404, 422, a real
// 500) is an answer, not an outage, and is rethrown immediately.
const WAKE_STATUSES = new Set([502, 503, 504]);

function isTransient(err) {
  // A rejected fetch (no `status`) is a dropped connection: the machine went
  // away mid-request, which is exactly the case worth retrying.
  return err.status === undefined || WAKE_STATUSES.has(err.status);
}

async function apiWithWake(path, { attempts = 6, onWaiting = null } = {}) {
  let delay = 500;
  for (let i = 0; ; i++) {
    try {
      const result = await api(path);
      if (i > 0 && onWaiting) onWaiting(null); // recovered — clear the notice
      return result;
    } catch (err) {
      if (i >= attempts - 1 || !isTransient(err)) throw err;
      if (onWaiting) onWaiting(i);
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 8000); // ~0.5+1+2+4+8+8s ≈ 23s of patience
    }
  }
}

function fmt(x) {
  if (x === null || x === undefined) return "—";
  const ax = Math.abs(x);
  if (ax >= 10000) return x.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (ax >= 100) return x.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (ax >= 1) return x.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return x.toPrecision(3);
}

function pct(x) {
  if (x === null || x === undefined) return "—";
  return (x * 100).toFixed(1) + "%";
}

function signedPct(x) {
  if (x === null || x === undefined) return "";
  return (x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%";
}

function setStatus(msg, kind = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = kind; // "", "busy", "error"
}

/* ---------- date / window helpers (UTC to match ISO strings) ---------- */

const DAY_MS = 86400000;
const isoUTC = (d) => d.toISOString().slice(0, 10);
const addDays = (d, n) => new Date(d.getTime() + n * DAY_MS);
const daysInclusive = (start, end) => Math.round((end - start) / DAY_MS) + 1;

/* Window presets. compute(startDate, endDate) -> {refStart, refEnd, anStart, anEnd}
   as ISO strings, or null when the data window is too short (preset omitted). */
const WINDOW_PRESETS = [
  {
    id: "last7-prior28",
    label: "Last 7d vs prior 28d",
    compute(start, end) {
      if (daysInclusive(start, end) < 35) return null;
      const anEnd = end;
      const anStart = addDays(end, -6);
      const refEnd = addDays(anStart, -1);
      const refStart = addDays(refEnd, -27);
      if (refStart < start) return null;
      return { refStart: isoUTC(refStart), refEnd: isoUTC(refEnd), anStart: isoUTC(anStart), anEnd: isoUTC(anEnd) };
    },
  },
  {
    id: "last14-prior28",
    label: "Last 14d vs prior 28d",
    compute(start, end) {
      if (daysInclusive(start, end) < 42) return null;
      const anEnd = end;
      const anStart = addDays(end, -13);
      const refEnd = addDays(anStart, -1);
      const refStart = addDays(refEnd, -27);
      if (refStart < start) return null;
      return { refStart: isoUTC(refStart), refEnd: isoUTC(refEnd), anStart: isoUTC(anStart), anEnd: isoUTC(anEnd) };
    },
  },
  {
    id: "weeks-1v4",
    label: "Last full week vs prior 4 weeks",
    compute(start, end) {
      // analysis = last full Mon–Sun week fully inside the window; ref = 4 weeks before.
      const lastSunday = addDays(end, -end.getUTCDay()); // getUTCDay: Sun=0
      const anEnd = lastSunday;
      const anStart = addDays(lastSunday, -6); // Monday
      const refEnd = addDays(anStart, -1); // prior Sunday
      const refStart = addDays(anStart, -28); // Monday, 4 weeks earlier
      if (refStart < start) return null; // needs >= 5 full weeks
      return { refStart: isoUTC(refStart), refEnd: isoUTC(refEnd), anStart: isoUTC(anStart), anEnd: isoUTC(anEnd) };
    },
  },
  {
    id: "split60",
    label: "First 60% vs rest",
    compute(start, end) {
      const splitMs = start.getTime() + 0.6 * (end.getTime() - start.getTime());
      return {
        refStart: isoUTC(start),
        refEnd: isoUTC(new Date(splitMs)),
        anStart: isoUTC(new Date(splitMs + DAY_MS)),
        anEnd: isoUTC(end),
      };
    },
  },
  { id: "custom", label: "Custom", compute: null },
];

function applyPreset(id) {
  const preset = WINDOW_PRESETS.find((p) => p.id === id);
  if (!preset || !preset.compute) return; // custom: leave inputs untouched
  const w = preset.compute(new Date(state.meta.date_start), new Date(state.meta.date_end));
  if (!w) return;
  $("ref-start").value = w.refStart;
  $("ref-end").value = w.refEnd;
  $("an-start").value = w.anStart;
  $("an-end").value = w.anEnd;
}

/* Client-side mirror of the backend window rules. Returns true when valid.
   Marks offending inputs, toggles #run-rca, and writes the status area. */
function validateWindows() {
  const ids = ["ref-start", "ref-end", "an-start", "an-end"];
  ids.forEach((id) => {
    const el = $(id);
    el.classList.remove("invalid");
    el.removeAttribute("aria-invalid");
  });
  const runBtn = $("run-rca");
  const fail = (bad, msg) => {
    bad.forEach((id) => {
      const el = $(id);
      el.classList.add("invalid");
      el.setAttribute("aria-invalid", "true");
    });
    runBtn.disabled = true;
    setStatus(msg, "error");
    return false;
  };

  const rs = $("ref-start").value, re = $("ref-end").value;
  const as = $("an-start").value, ae = $("an-end").value;
  if (!rs || !re || !as || !ae) return fail(ids.filter((id) => !$(id).value), "Set all four window dates.");

  const lo = state.meta.date_start, hi = state.meta.date_end;
  const oob = ids.filter((id) => $(id).value < lo || $(id).value > hi);
  if (oob.length) return fail(oob, `Windows must stay within the data range (${lo} … ${hi}).`);
  if (rs > re) return fail(["ref-start", "ref-end"], "Reference window start must be on or before its end.");
  if (re >= as) return fail(["ref-end", "an-start"], "Reference window must end before the analysis window starts.");
  if (as > ae) return fail(["an-start", "an-end"], "Analysis window start must be on or before its end.");

  runBtn.disabled = false;
  // whole-week advisory (muted, not an error) when seasonality is in play
  const refLen = daysInclusive(new Date(rs), new Date(re));
  const anLen = daysInclusive(new Date(as), new Date(ae));
  const hasSeasonality =
    state.dag && state.dag.nodes.some(([, def]) => def.seasonality && def.seasonality.length);
  if (hasSeasonality && (refLen % 7 !== 0 || anLen % 7 !== 0)) {
    setStatus("ⓘ Windows aren't whole weeks — weekday mix can distort the comparison.");
  } else {
    setStatus("");
  }
  return true;
}

/* Keep the Share menu's items in sync with what is currently shareable. */
function updateShareMenu() {
  $("share-rca-json").disabled = !state.rca;
  const rep = $("share-rca-report");
  if (rep) rep.disabled = !state.rca;
  const save = $("share-save-view");
  // Saving stores the hash, so there has to be one — anything deep-linkable
  // (a metric, an RCA run, a what-if) qualifies.
  if (save) save.disabled = !location.hash || location.hash.length < 2;
}

/* ---------- exportable RCA report (roadmap 1.5) ----------
   Self-contained HTML built entirely client-side: the browser already holds
   the full RCA response, Cytoscape snapshots the annotated tree, Plotly
   rasterizes the target strip. Inline styles, embedded PNGs, no external
   requests — printable straight to PDF. */

async function downloadRcaReport() {
  if (!state.rca) return;
  const res = state.rca;
  let treePng = "";
  try {
    treePng = state.cy.png({ full: true, scale: 2, bg: "#f7f8fa", maxWidth: 2400 });
  } catch { /* snapshot is best-effort */ }
  let stripPng = "";
  try {
    // Plotly.newPlot targets #rca-strip itself, so the strip div IS the plot.
    const stripDiv = $("rca-strip");
    if (stripDiv && stripDiv.classList.contains("js-plotly-plot")) {
      stripPng = await Plotly.toImage(stripDiv, { format: "png", width: 1080, height: 240, scale: 2 });
    }
  } catch { /* chart is best-effort */ }
  const blob = new Blob([buildRcaReportHtml(res, treePng, stripPng)], { type: "text/html" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `rca_report_${res.target}_${res.analysis_window.start}_${res.analysis_window.end}.html`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function buildRcaReportHtml(res, treePng, stripPng) {
  const target = res.nodes[res.target];
  const now = new Date();
  const ciCell = (ci) => (ci ? `[${fmt(ci[0])}, ${fmt(ci[1])}]` : "—");
  const shareOf = (v, gap) => (Math.abs(gap) > 1e-12 ? pct(v / gap) : "—");
  const num = (v) => `<td class="num">${v}</td>`;

  const grainNote = (node) => {
    const bits = [];
    if (node.grain && node.grain !== "day" && node.effective_windows) {
      const ew = node.effective_windows;
      bits.push(`${node.grain} grain, snapped to ${ew.reference.n_periods}+${ew.analysis.n_periods} whole ${node.grain}s`);
    }
    if (node.ci_status === "degenerate_single_period") bits.push("single-period window: no bootstrap CI");
    if (node.sign_warnings && node.sign_warnings.length) bits.push("⚠ learned sign contradicts declared expectation");
    return bits.length ? ` · ${esc(bits.join(" · "))}` : "";
  };

  const order = [res.target, ...res.ranked_causes.map((c) => c.metric)];
  const blocks = order
    .filter((name) => res.nodes[name] && res.nodes[name].contributions.length)
    .map((name) => {
      const node = res.nodes[name];
      const twoLevel = node.contributions.some((c) => c.decomposition);
      const unexpl = node.unexplained !== null
        ? `<tr class="dim"><td>unexplained</td>${num(fmt(node.unexplained))}${num(shareOf(node.unexplained, node.gap))}<td class="num">—</td></tr>`
        : "";
      let tables;
      if (twoLevel) {
        const headRows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code></td>${num(fmt(c.decomposition.means.estimate))}${num(shareOf(c.decomposition.means.estimate, node.gap))}${num(ciCell(c.decomposition.means.ci_95))}</tr>`).join("");
        const interaction = node.interaction
          ? `<tr class="em"><td>co-movement shift</td>${num(fmt(node.interaction.estimate))}${num(shareOf(node.interaction.estimate, node.gap))}${num(ciCell(node.interaction.ci_95))}</tr>`
          : "";
        const detRows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code></td>${num(fmt(c.decomposition.means.estimate))}${num(fmt(c.decomposition.comovement.estimate))}${num(fmt(c.estimate))}${num(ciCell(c.ci_95))}${num(c.prob_same_direction == null ? "—" : pct(c.prob_same_direction))}</tr>`).join("");
        tables = `
          <h4>Headline — window-means bridge</h4>
          <table><tr><th>Driver</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th></tr>${headRows}${interaction}${unexpl}</table>
          <h4>Detailed — per-parent split (means + co-movement = total)</h4>
          <table><tr><th>Parent</th><th class="num">means</th><th class="num">co-movement</th><th class="num">total Δ</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>${detRows}</table>`;
      } else {
        const rows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code></td>${num(fmt(c.estimate))}${num(c.share_of_gap === null ? "—" : pct(c.share_of_gap))}${num(ciCell(c.ci_95))}${num(c.prob_same_direction == null ? "—" : pct(c.prob_same_direction))}</tr>`).join("");
        const comps = node.components
          ? `<tr class="dim"><td>trend</td>${num(fmt(node.components.trend.estimate))}<td class="num">—</td>${num(ciCell(node.components.trend.ci_95))}<td class="num">—</td></tr>
             <tr class="dim"><td>seasonal</td>${num(fmt(node.components.seasonal.estimate))}<td class="num">—</td>${num(ciCell(node.components.seasonal.ci_95))}<td class="num">—</td></tr>`
          : "";
        const unexpl5 = node.unexplained !== null
          ? `<tr class="dim"><td>unexplained</td>${num(fmt(node.unexplained))}${num(shareOf(node.unexplained, node.gap))}<td class="num">—</td><td class="num">—</td></tr>`
          : "";
        tables = `<table><tr><th>Parent</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>${rows}${comps}${unexpl5}</table>`;
      }
      const signWarn = (node.sign_warnings || []).map((w) => `<p class="warn">⚠ ${esc(w)}</p>`).join("");
      return `<section>
        <h3><code>${esc(name)}</code> <span class="meta">${node.attribution_method === "shapley" ? "Shapley (exact)" : "posterior"}${grainNote(node)}</span></h3>
        <p class="meta">gap ${fmt(node.gap)} (${node.relative_change == null ? "—" : signedPct(node.relative_change)}) · ${fmt(node.baseline)} → ${fmt(node.actual)} window means${node.grain && node.grain !== "day" ? ` per ${esc(node.grain)}` : ""}</p>
        ${signWarn}${tables}
      </section>`;
    })
    .join("");

  const skipped = Object.entries(res.nodes)
    .filter(([, n]) => n.status === "window_shorter_than_grain")
    .map(([n, node]) => `<code>${esc(n)}</code> (${esc(node.grain)})`);

  const ranked = res.ranked_causes
    .map((c, i) => `<tr><td>${i + 1}</td><td><code>${esc(c.metric)}</code></td>${num(c.score.toFixed(2))}<td>via ${esc(c.via || "—")}</td></tr>`)
    .join("");

  const dataThrough = state.meta && state.meta.data_through
    ? Object.values(state.meta.data_through).reduce((a, b) => (a < b ? a : b), "9999")
    : null;

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>RCA — ${esc(res.target)} · ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: #1a202c; max-width: 960px; margin: 32px auto; padding: 0 20px; line-height: 1.45; }
  h1 { font-size: 22px; margin-bottom: 2px; } h3 { font-size: 15px; margin: 22px 0 4px; } h4 { font-size: 12.5px; margin: 12px 0 4px; color: #475569; text-transform: uppercase; letter-spacing: 0.4px; }
  .meta { color: #64748b; font-size: 12.5px; font-weight: 400; }
  .gap { font-size: 26px; font-weight: 700; margin: 6px 0 14px; }
  .gap.up { color: #16a34a; } .gap.down { color: #dc2626; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 4px 0 10px; }
  th { text-align: left; color: #64748b; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; padding: 4px 6px; border-bottom: 1px solid #e2e8f0; }
  td { padding: 4px 6px; border-bottom: 1px solid #f0f2f5; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.dim td { color: #94a3b8; } tr.em td { font-style: italic; }
  img { max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px; margin: 8px 0; }
  code { background: #f1f5f9; padding: 1px 4px; border-radius: 4px; font-size: 12px; }
  .warn { font-size: 12px; color: #92400e; background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; }
  .footnote { margin-top: 28px; padding-top: 12px; border-top: 1px solid #e2e8f0; font-size: 11.5px; color: #64748b; }
  @media print { body { margin: 8px auto; } section { break-inside: avoid; } }
</style></head><body>
  <h1>Root cause analysis — <code>${esc(res.target)}</code></h1>
  <p class="meta">reference ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)} vs analysis ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}${dataThrough ? ` · data through ${esc(dataThrough)}` : ""}${state.meta ? ` · provider: ${esc(state.meta.provider)}` : ""} · generated ${now.toISOString().slice(0, 16).replace("T", " ")} UTC</p>
  <div class="gap ${goodDir(res.target, target.gap >= 0 ? "up" : "down")}">${target.gap >= 0 ? "+" : ""}${fmt(target.gap)} (${signedPct(target.relative_change)})</div>
  ${stripPng ? `<img src="${stripPng}" alt="target series with reference and analysis windows shaded">` : ""}
  ${treePng ? `<h3>Metric tree</h3><img src="${treePng}" alt="metric tree with RCA overlay">` : ""}
  <h3>Ranked causes <span class="meta">(triage heuristic, not rigorous multi-hop attribution)</span></h3>
  <table><tr><th>#</th><th>Metric</th><th class="num">score</th><th>via</th></tr>${ranked}</table>
  ${skipped.length ? `<p class="meta">Not analyzed — window shorter than grain: ${skipped.join(", ")}.</p>` : ""}
  <h3 style="margin-top:24px">Attribution detail</h3>
  ${blocks}
  <div class="footnote">
    <strong>Methods.</strong> Changes are window-mean differences at each node's grain, over the whole periods inside the
    requested windows. Formula (identity) nodes use exact symmetric per-period Shapley attribution — a window-means bridge
    plus each parent's share of each window's within-window co-movement term — so attributions sum exactly to the gap and
    <em>unexplained</em> is measurement residual only. Probabilistic nodes multiply the fitted <code>beta_raw</code>
    posterior (BSTS, fit strictly before the analysis window) by the parent's window delta, with trend and seasonal
    components reported separately. Intervals combine coefficient posteriors with a circular moving-block bootstrap of
    the window rows; fits and bootstraps are seeded, so identical requests reproduce identical numbers. Full assumptions:
    docs/model.md in the <a href="https://github.com/PolycultureResearch/breakdown">breakdown</a> repository.
  </div>
</body></html>`;
}

/* ---------- cold start (provider: none — declared beliefs, no data) ---------- */

const coldStart = () => !!(state.meta && state.meta.mode === "cold_start");

/* Tiny arithmetic evaluator for formula strings (+ - * / parentheses,
   numbers, parent names) — enough to derive a formula node's operating point
   from its parents' asserted baselines, mirroring the engine's per-draw
   derivation at the mean. Never eval(): parse or return null. */
function evalFormula(src, vars) {
  let i = 0;
  const peek = () => src[i];
  const ws = () => { while (i < src.length && /\s/.test(src[i])) i++; };
  function atom() {
    ws();
    if (peek() === "(") {
      i++;
      const v = expr();
      ws();
      if (peek() !== ")") throw new Error("paren");
      i++;
      return v;
    }
    if (peek() === "-") { i++; return -atom(); }
    const m = /^(\d+\.?\d*|[A-Za-z_][A-Za-z0-9_]*)/.exec(src.slice(i));
    if (!m) throw new Error("token");
    i += m[0].length;
    if (/^[A-Za-z_]/.test(m[0])) {
      if (!(m[0] in vars)) throw new Error("unknown var");
      return vars[m[0]];
    }
    return Number(m[0]);
  }
  function term() {
    let v = atom();
    for (ws(); peek() === "*" || peek() === "/"; ws()) {
      const op = src[i++];
      const r = atom();
      v = op === "*" ? v * r : v / r;
    }
    return v;
  }
  function expr() {
    let v = term();
    for (ws(); peek() === "+" || peek() === "-"; ws()) {
      const op = src[i++];
      const r = term();
      v = op === "+" ? v + r : v - r;
    }
    return v;
  }
  try {
    const v = expr();
    ws();
    return i === src.length && Number.isFinite(v) ? v : null;
  } catch {
    return null;
  }
}

/* Per-node cold-start operating points, from `baseline` declarations in
   topological order (formula nodes derive from parents — same rule as the
   engine). Returns {name: {mu, lo, hi, derived}}; lo/hi null for points. */
function computeColdBase() {
  const base = {};
  const pending = state.dag.nodes.map(([name]) => name);
  // small tree: iterate until stable instead of a real topo sort
  for (let pass = 0; pass < pending.length + 1; pass++) {
    state.dag.nodes.forEach(([name, def]) => {
      if (base[name]) return;
      if (def.formula) {
        const parents = def.parents || [];
        if (!parents.every((p) => base[p])) return;
        const mu = evalFormula(def.formula, Object.fromEntries(parents.map((p) => [p, base[p].mu])));
        if (mu != null) base[name] = { mu, lo: null, hi: null, derived: true };
      } else if (def.baseline) {
        const { low, high } = def.baseline;
        base[name] = {
          mu: (low + high) / 2,
          lo: low < high ? low : null,
          hi: low < high ? high : null,
          derived: false,
        };
      }
    });
  }
  return base;
}

/* Belief label for a probabilistic edge: the YAML prior in business units.
   Normal priors show the central-90% belief interval; other distributions
   show their name + params (still the exact belief, just not symmetric). */
function beliefEdgeLabel(def, parent) {
  const prior = (def.priors || {})[parent] || (def.priors || {}).coefficient;
  if (!prior) return "";
  const p = prior.params || {};
  if (prior.distribution === "Normal") {
    const mu = p.mu ?? 0, sd = p.sigma ?? 1;
    return `β ~ ${fmt(mu)} [${fmt(mu - 1.645 * sd)}, ${fmt(mu + 1.645 * sd)}] · belief`;
  }
  const args = ["mu", "sigma", "lam"].filter((k) => p[k] != null).map((k) => fmt(p[k]));
  return `β ~ ${prior.distribution}(${args.join(", ")}) · belief`;
}

function nodeType(def) {
  if (!def.parents || def.parents.length === 0) return "source";
  return def.formula ? "formula" : "prob";
}

/* ---------- node cards ----------
   Each metric node is drawn as an SVG "stat card" set as the Cytoscape node's
   background-image. The SVG has a transparent background and draws only text +
   sparkline, so the node's own background-color and border still render behind
   it — RCA / what-if / selection overlays keep working untouched. */

const CARD_W = 200;
const CARD_H = { num: 64, delta: 92, spark: 112, full: 140 };
const UNIT_H = 15; // extra card height when a metric declares a unit caption
const CARD_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif";
const CARD_COL = { up: "#16a34a", down: "#dc2626", flat: "#64748b" };
const CARD_COL_SOFT = { up: "#e7f6ec", down: "#fdeaea", flat: "#eef1f5" };
const VARIANT_LABEL = {
  num: "Number", delta: "Number + Δ", spark: "Number + spark", full: "Number + Δ + spark",
};

function effectiveVariant(name) {
  return state.cardConfig.overrides[name] || state.cardConfig.variant;
}

/* Big-number formatting driven by the metric definition's optional `format`
   object {style, unit, decimals, compact, symbol}. Presentation only. */
function normalizeFormat(f) {
  if (!f) return { style: "number" };
  if (typeof f === "string") return { style: f }; // defensive (backend coerces already)
  return f;
}

/* Display default when a metric declares no `format`: guess from naming
   conventions. Conservative, presentation-only, and an explicit `format`
   always wins — this only saves obvious cases (an *_mrr flow rendering as a
   bare float) from looking wrong out of the box. */
const CURRENCY_NAME = /(^|_)(mrr|arr|revenue|arpu|arpa|aov|gmv|usd|price|cost|costs|spend|ltv|cac)(_|$)/;
const PERCENT_NAME = /(^|_)(rate|pct|percent|share|ratio)(_|$)/;
function guessFormat(name) {
  if (CURRENCY_NAME.test(name)) return { style: "currency" };
  if (PERCENT_NAME.test(name)) return { style: "percent" };
  return { style: "number" };
}

function metricFormat(name) {
  const declared = state.defs && state.defs[name] && state.defs[name].format;
  return declared ? normalizeFormat(declared) : guessFormat(name);
}

/* Map a MOVEMENT direction ("up"/"down") to a COLOR direction through the
   metric's declared `direction` (display-only): for down_is_good metrics an
   upward move colors red, a downward move green; neutral always colors gray.
   Arrows and labels stay directional — only the good/bad coloring flips. */
function goodDir(name, dir) {
  if (dir !== "up" && dir !== "down") return dir;
  const decl = (state.defs && state.defs[name] && state.defs[name].direction) || "up_is_good";
  if (decl === "neutral") return "flat";
  if (decl === "down_is_good") return dir === "up" ? "down" : "up";
  return dir;
}

/* Goodness-mapped overlay class ("<prefix>-up" green / "<prefix>-down" red),
   or null for neutral metrics — they get no judgmental tint at all. */
function goodClass(name, dir, prefix) {
  const g = goodDir(name, dir);
  return g === "up" ? `${prefix}-up` : g === "down" ? `${prefix}-down` : null;
}

/* Card height grows by one line when the metric shows a unit caption. Used for
   both the SVG viewBox and the Cytoscape node size — they must agree. */
function cardHeight(name, variant) {
  return CARD_H[variant] + (metricFormat(name).unit ? UNIT_H : 0);
}

function withDecimals(x, dec) {
  return x.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function compactNum(x, dec) {
  const ax = Math.abs(x), d = dec == null ? 1 : dec;
  if (ax >= 1e9) return (x / 1e9).toFixed(d) + "B";
  if (ax >= 1e6) return (x / 1e6).toFixed(d) + "M";
  if (ax >= 1e4) return (x / 1e3).toFixed(d) + "k"; // compact at 10k+; keeps 4-digit values readable
  return dec == null ? fmt(x) : withDecimals(x, dec);
}

function fmtCardValue(name, value) {
  if (value == null) return "—";
  const f = metricFormat(name);
  const dec = f.decimals;
  if (f.style === "percent") return (value * 100).toFixed(dec == null ? 1 : dec) + "%";
  const compact = f.compact == null ? f.style === "currency" : f.compact;
  const mag = Math.abs(value);
  let s = compact ? compactNum(mag, dec) : dec == null ? fmt(mag) : withDecimals(mag, dec);
  if (f.style === "currency") s = (f.symbol || "$") + s;
  // Sign outside the symbol: -$103, never $-103.
  return (value < 0 ? "-" : "") + s;
}

/* Inclusive end date of the period starting at `iso` for a grain. */
function periodEndISO(iso, grain) {
  if (grain === "day") return iso;
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (grain === "week") dt.setUTCDate(dt.getUTCDate() + 6);
  else { dt.setUTCMonth(dt.getUTCMonth() + 1); dt.setUTCDate(0); }  // month: last day
  return dt.toISOString().slice(0, 10);
}

/* Derive the card's numbers from a metric's native-grain series + the current
   config. Big number = latest value at the as-of anchor; delta = that vs
   `deltaLen` points earlier; sparkline = trailing `sparkLen` points. Points
   are grain periods (days for daily metrics, weeks/months for coarser ones).
   Only periods FULLY completed by `state.asOf` count, so a calendar week the
   data edge cuts in half never becomes a headline number. */
function deriveCardData(name) {
  const cfg = state.cardConfig;
  const m = state.series && state.series.metrics && state.series.metrics[name];
  let s = (m && m.values) || [];
  if (m && state.asOf) {
    let cut = -1;
    for (let i = m.dates.length - 1; i >= 0; i--) {
      if (periodEndISO(m.dates[i], m.grain) <= state.asOf) { cut = i; break; }
    }
    s = s.slice(0, cut + 1);
  }
  let lastIdx = -1;
  for (let i = s.length - 1; i >= 0; i--) {
    if (s[i] != null) { lastIdx = i; break; }
  }
  const value = lastIdx >= 0 ? s[lastIdx] : null;

  let prior = null;
  for (let i = lastIdx - cfg.deltaLen; i >= 0; i--) {
    if (s[i] != null) { prior = s[i]; break; }
  }
  let dpct = null, dir = "flat";
  if (value != null && prior != null && prior !== 0) {
    const dabs = value - prior;
    dpct = dabs / Math.abs(prior);
    dir = dabs > 1e-12 ? "up" : dabs < -1e-12 ? "down" : "flat";
  }

  const spark = s.slice(Math.max(0, s.length - cfg.sparkLen)).filter((v) => v != null);
  return { value, dpct, dir, spark };
}

/* Sparkline: filled area (gradient id="g") + line + endpoint dot, mapped into
   the rectangle [x0,y0]-[x1,y1] (y0 top). */
function sparkPaths(data, x0, x1, y0, y1, col) {
  const n = data.length;
  if (n < 2) return "";
  const min = Math.min(...data), max = Math.max(...data), rng = (max - min) || 1;
  const X = (i) => x0 + (i / (n - 1)) * (x1 - x0);
  const Y = (v) => y1 - ((v - min) / rng) * (y1 - y0);
  const pts = data.map((v, i) => [X(i), Y(v)]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = `${line} L ${X(n - 1).toFixed(1)} ${y1} L ${X(0).toFixed(1)} ${y1} Z`;
  const e = pts[n - 1];
  return (
    `<path d="${area}" fill="url(#g)"/>` +
    `<path d="${line}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>` +
    `<circle cx="${e[0].toFixed(1)}" cy="${e[1].toFixed(1)}" r="2.4" fill="${col}"/>`
  );
}

/* Delta as a colored pill centered on (cx, baseline). `mark` is an optional
   trailing glyph (◌ unexplained, ⊙ set by scenario, ⚠ extrapolated). Width is
   estimated from the text length — good enough at this size. */
function deltaSvg(dpct, dir, mark, cx, baseline, colorDir = dir) {
  const hasVal = dpct != null;
  if (!hasVal && !mark) {
    return `<text x="${cx}" y="${baseline}" text-anchor="middle" font-size="12.5" fill="#8a94a6" font-family="${CARD_FONT}">—</text>`;
  }
  const col = CARD_COL[colorDir] || CARD_COL.flat, bg = CARD_COL_SOFT[colorDir] || CARD_COL_SOFT.flat;
  const tri = dir === "up" ? "▲" : dir === "down" ? "▼" : "▬";
  let txt = hasVal ? `${tri} ${signedPct(dpct)}` : "—";
  if (mark) txt += ` ${mark}`;
  const w = txt.length * 7 + 14;
  const x = cx - w / 2;
  return (
    `<rect x="${x.toFixed(1)}" y="${baseline - 14}" width="${w.toFixed(1)}" height="19" rx="6" fill="${bg}"/>` +
    `<text x="${cx}" y="${baseline}" text-anchor="middle" font-size="12.5" font-weight="600" fill="${col}" font-family="${CARD_FONT}">${esc(txt)}</text>`
  );
}

function buildCardSVG(name, d, variant, isOverride, overlay) {
  const cx = CARD_W / 2;
  const showSpark = variant === "spark" || variant === "full";
  const showDelta = variant === "delta" || variant === "full";
  const dispName = name.length > 26 ? name.slice(0, 25) + "…" : name;

  // Fold in an active RCA / what-if overlay: it can replace the big number
  // (what-if simulated value), the delta, its direction, and add a mark glyph.
  const val = overlay && overlay.value != null ? overlay.value : d.value;
  const dpct = overlay ? overlay.dpct : d.dpct;
  const dir = overlay ? overlay.dir : d.dir;
  const mark = overlay ? overlay.mark : null;

  // Optional unit caption under the value; everything below it shifts down.
  const unit = metricFormat(name).unit;
  const uOff = unit ? UNIT_H : 0;
  const H = CARD_H[variant] + uOff;

  let defs = "";
  let inner =
    `<text x="${cx}" y="20" text-anchor="middle" font-size="13" font-weight="600" fill="#475569" font-family="${CARD_FONT}">${esc(dispName)}</text>` +
    `<text x="${cx}" y="52" text-anchor="middle" font-size="34" font-weight="700" fill="#1a202c" font-family="${CARD_FONT}">${esc(fmtCardValue(name, val))}</text>`;

  if (unit) {
    const u = unit.length > 22 ? unit.slice(0, 21) + "…" : unit;
    inner += `<text x="${cx}" y="66" text-anchor="middle" font-size="11" fill="#8a94a6" font-family="${CARD_FONT}">${esc(u)}</text>`;
  }

  const colorDir = goodDir(name, dir);
  if (showSpark && d.spark.length >= 2) {
    const col = CARD_COL[colorDir] || CARD_COL.flat;
    defs =
      `<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0" stop-color="${col}" stop-opacity="0.15"/>` +
      `<stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>`;
    const y0 = (showDelta ? 64 : 70) + uOff, y1 = (showDelta ? 96 : 104) + uOff;
    inner += sparkPaths(d.spark, 16, CARD_W - 16, y0, y1, col);
  }

  if (showDelta) {
    if (showSpark) {
      inner += `<line x1="16" y1="${110 + uOff}" x2="${CARD_W - 16}" y2="${110 + uOff}" stroke="#eef1f6" stroke-width="1"/>`;
      inner += deltaSvg(dpct, dir, mark, cx, 130 + uOff, colorDir);
    } else {
      inner += deltaSvg(dpct, dir, mark, cx, 82 + uOff, colorDir);
    }
  }

  if (isOverride) {
    // small indigo dot: this node's variant is pinned, ignoring the canvas default
    inner +=
      `<circle cx="${CARD_W - 12}" cy="12" r="6" fill="#4f46e5" fill-opacity="0.18"/>` +
      `<circle cx="${CARD_W - 12}" cy="12" r="3.5" fill="#4f46e5"/>`;
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${H}" viewBox="0 0 ${CARD_W} ${H}">${defs}${inner}</svg>`;
}

/* Cold-start card: asserted operating point (± 90% belief range) instead of
   history-derived numbers — there is no series to spark or delta. Overlay
   (what-if simulated value) folds in exactly like fitted cards. */
function buildColdCardSVG(name, cb, overlay) {
  const cx = CARD_W / 2;
  const dispName = name.length > 26 ? name.slice(0, 25) + "…" : name;
  const val = overlay && overlay.value != null ? overlay.value : cb ? cb.mu : null;
  const H = CARD_H.delta;

  let inner =
    `<text x="${cx}" y="20" text-anchor="middle" font-size="13" font-weight="600" fill="#475569" font-family="${CARD_FONT}">${esc(dispName)}</text>` +
    `<text x="${cx}" y="52" text-anchor="middle" font-size="34" font-weight="700" fill="#1a202c" font-family="${CARD_FONT}">${esc(fmtCardValue(name, val))}</text>`;

  if (overlay) {
    inner += deltaSvg(overlay.dpct, overlay.dir, overlay.mark, cx, 82, goodDir(name, overlay.dir));
  } else {
    const sub = cb && cb.lo != null
      ? `${fmtCardValue(name, cb.lo)} – ${fmtCardValue(name, cb.hi)} · 90% belief`
      : cb && cb.derived ? "derived from parents" : "belief";
    inner += `<text x="${cx}" y="80" text-anchor="middle" font-size="11.5" fill="#8a94a6" font-family="${CARD_FONT}">${esc(sub)}</text>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${H}" viewBox="0 0 ${CARD_W} ${H}">${inner}</svg>`;
}

function svgDataURI(svg) {
  return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

/* Paint one node's card. Sets a fixed size (so dagre spaces cards correctly)
   and blanks the text label (the card draws the name itself). */
function renderNodeCard(name) {
  const node = state.cy.getElementById(name);
  if (!node.length) return;
  const overlay = state.cardOverlay[name] || null;
  let svg, height;
  if (coldStart()) {
    svg = buildColdCardSVG(name, (state.coldBase || {})[name], overlay);
    height = CARD_H.delta;
  } else {
    const variant = effectiveVariant(name);
    const isOverride = name in state.cardConfig.overrides;
    svg = buildCardSVG(name, deriveCardData(name), variant, isOverride, overlay);
    height = cardHeight(name, variant);
  }
  node.style({
    "background-image": svgDataURI(svg),
    "background-fit": "contain",
    "width": CARD_W,
    "height": height,
    "padding": 0,
    "label": "",
    "text-opacity": 0, // card draws the name itself; hide Cytoscape's own label
  });
}

function renderAllCards() {
  if ((!state.series && !coldStart()) || !state.cy) return;
  state.cy.batch(() => {
    state.dag.nodes.forEach(([name]) => renderNodeCard(name));
  });
}

function runLayout(fit = false) {
  state.cy
    .layout({ name: "dagre", rankDir: "BT", nodeSep: 40, rankSep: 70, padding: 24, fit, animate: false })
    .run();
}

const CARD_CFG_KEY = "breakdown.cardConfig";

function loadCardConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(CARD_CFG_KEY) || "{}");
    const c = state.cardConfig;
    if (["num", "delta", "spark", "full"].includes(saved.variant)) c.variant = saved.variant;
    if (Number.isFinite(saved.deltaLen)) c.deltaLen = saved.deltaLen;
    if (Number.isFinite(saved.sparkLen)) c.sparkLen = saved.sparkLen;
    if (saved.overrides && typeof saved.overrides === "object") c.overrides = saved.overrides;
  } catch { /* ignore malformed storage */ }
}

function saveCardConfig() {
  try {
    localStorage.setItem(CARD_CFG_KEY, JSON.stringify(state.cardConfig));
  } catch { /* storage disabled: config just won't persist */ }
}

/* ---------- Saved views ----------
   An RCA run, a what-if scenario, and a selected metric are already fully
   encoded in the URL hash, so "saving" one is just naming its hash. That keeps
   this entirely client-side: the engine stays stateless, several people can use
   one shared instance without seeing each other's work, and a returning visitor
   finds their own analyses. Nothing here is a substitute for real per-user
   storage (it does not survive a cleared browser) — it is the honest amount of
   persistence for a read-only tree, where what a user creates is *analyses*,
   not edits to the tree itself. */

const VIEWS_KEY = "breakdown.workspace";
const MAX_SAVED_VIEWS = 30;

function loadSavedViews() {
  try {
    const saved = JSON.parse(localStorage.getItem(VIEWS_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((v) => v && v.hash && v.name) : [];
  } catch {
    return [];
  }
}

function writeSavedViews(views) {
  try {
    localStorage.setItem(VIEWS_KEY, JSON.stringify(views.slice(0, MAX_SAVED_VIEWS)));
  } catch { /* storage disabled or full: saving just won't persist */ }
}

/* A default name that says what the view *is*, so the list is scannable
   without opening each one. */
function describeCurrentView() {
  if (state.whatif.result) {
    const n = state.whatif.interventions.length;
    return `What-if · ${n} intervention${n === 1 ? "" : "s"}`;
  }
  if (state.rca) {
    return `RCA · ${state.rca.target} · ${state.rca.analysis_window.start}`;
  }
  if (state.selected) return `Metric · ${state.selected}`;
  return "View";
}

/* Naming happens inline rather than through prompt(): a modal dialog blocks
   the whole page, and this is a menu, not an interruption. */
function beginSaveView() {
  const hash = location.hash;
  if (!hash || hash.length < 2) return;
  const host = $("saved-views");
  if (!host || host.querySelector(".saved-name")) return;
  const form = document.createElement("div");
  form.className = "saved-form";
  form.innerHTML =
    `<input class="saved-name" type="text" maxlength="80" spellcheck="false">` +
    `<button class="saved-commit">Save</button>`;
  host.prepend(form);
  const input = form.querySelector(".saved-name");
  input.value = describeCurrentView();
  input.select();

  const commit = () => {
    const name = input.value.trim();
    if (!name) return;
    const views = loadSavedViews().filter((v) => v.hash !== hash);
    views.unshift({ name, hash, savedAt: new Date().toISOString() });
    writeSavedViews(views);
    renderSavedViews();
  };
  form.querySelector(".saved-commit").addEventListener("click", (e) => {
    e.stopPropagation();
    commit();
  });
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") commit();
    if (e.key === "Escape") renderSavedViews();
  });
}

function renderSavedViews() {
  const host = $("saved-views");
  if (!host) return;
  const views = loadSavedViews();
  host.innerHTML = views.length
    ? `<div class="saved-head">Saved in this browser</div>` +
      views
        .map(
          (v, i) => `<div class="saved-row">
             <button class="saved-open" data-i="${i}" title="${esc(v.hash)}">${esc(v.name)}</button>
             <button class="saved-del" data-i="${i}" title="Forget this view">×</button>
           </div>`,
        )
        .join("")
    : "";

  host.querySelectorAll(".saved-open").forEach((btn) => {
    btn.addEventListener("click", () => {
      const v = loadSavedViews()[Number(btn.dataset.i)];
      if (!v) return;
      $("share-menu").style.display = "none";
      // Assigning an identical hash fires no hashchange, so drive the router
      // directly rather than relying on the event.
      location.hash = v.hash.replace(/^#/, "");
      applyDeepLink();
    });
  });
  host.querySelectorAll(".saved-del").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const views = loadSavedViews();
      views.splice(Number(btn.dataset.i), 1);
      writeSavedViews(views);
      renderSavedViews();
    });
  });
}

/* Wire the canvas-wide card controls. Variant changes resize nodes (re-layout,
   preserving the viewport); length changes only repaint. */
function initCardControls() {
  if (coldStart()) {
    // Variants/sparklines/as-of are history controls; cold cards have none.
    const tb = $("card-toolbar");
    if (tb) tb.style.display = "none";
    return;
  }
  const variantSel = $("card-variant");
  const sparkInp = $("card-spark");
  const deltaInp = $("card-delta");
  variantSel.value = state.cardConfig.variant;
  sparkInp.value = state.cardConfig.sparkLen;
  deltaInp.value = state.cardConfig.deltaLen;

  variantSel.addEventListener("change", () => {
    state.cardConfig.variant = variantSel.value;
    saveCardConfig();
    renderAllCards();
    runLayout(false);
  });
  // Collapsible display menu: card options are occasional-use, so they live
  // behind one quiet button instead of a permanent four-row panel.
  const displayToggle = $("display-toggle");
  const displayMenu = $("display-menu");
  displayToggle.addEventListener("click", () => {
    const open = displayMenu.style.display !== "none";
    displayMenu.style.display = open ? "none" : "";
    $("display-caret").textContent = open ? "▸" : "▾";
  });
  const clampInt = (el, lo, hi, fallback) => {
    let v = parseInt(el.value, 10);
    if (!Number.isFinite(v)) v = fallback;
    v = Math.max(lo, Math.min(hi, v));
    el.value = v;
    return v;
  };
  sparkInp.addEventListener("change", () => {
    state.cardConfig.sparkLen = clampInt(sparkInp, 2, 365, 30);
    saveCardConfig();
    renderAllCards();
  });
  deltaInp.addEventListener("change", () => {
    state.cardConfig.deltaLen = clampInt(deltaInp, 1, 365, 7);
    saveCardConfig();
    renderAllCards();
  });

  // As-of anchor. Defaults to the tree-wide data edge (min data_through
  // across metrics); deliberately NOT persisted — freshness moves daily.
  const asofInp = $("card-asof");
  if (asofInp) {
    asofInp.min = state.meta.date_start;
    asofInp.max = state.meta.date_end;
    if (state.asOf) asofInp.value = state.asOf;
    asofInp.addEventListener("change", () => {
      let v = asofInp.value;
      if (!v) v = state.meta.date_end;
      if (v < asofInp.min) v = asofInp.min;
      if (v > asofInp.max) v = asofInp.max;
      asofInp.value = v;
      state.asOf = v;
      renderAllCards();
    });
  }
}

/* ---------- graph ---------- */

const CY_STYLE = [
  {
    selector: "node",
    style: {
      shape: "round-rectangle",
      "background-color": "#ffffff",
      "border-width": 2,
      "border-color": "#94a3b8",
      label: "data(label)",
      "text-wrap": "wrap",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": 12,
      "font-weight": 600,
      color: "#1a202c",
      width: "label",
      height: "label",
      padding: "12px",
    },
  },
  { selector: "node.prob", style: { "border-color": "#4f46e5" } },
  { selector: "node.formula", style: { "border-color": "#9333ea" } },
  { selector: "node.fitted", style: { "background-color": "#eef2ff" } },
  {
    selector: "node:selected",
    style: { "border-width": 3.5, "border-color": "#4f46e5" },
  },
  {
    selector: "node.rca-up",
    style: { "background-color": "#dcfce7", "border-color": "#16a34a" },
  },
  {
    selector: "node.rca-down",
    style: { "background-color": "#fee2e2", "border-color": "#dc2626" },
  },
  {
    selector: "edge",
    style: {
      width: 2,
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "arrow-scale": 1.1,
      "line-color": "#94a3b8",
      "target-arrow-color": "#94a3b8",
      label: "data(label)",
      "font-size": 10,
      color: "#475569",
      "text-background-color": "#f6f7f9",
      "text-background-opacity": 0.92,
      "text-background-padding": 3,
    },
  },
  {
    selector: "edge.formula",
    style: { "line-color": "#c084fc", "target-arrow-color": "#c084fc" },
  },
  {
    selector: "edge.prob",
    style: {
      "line-style": "dashed",
      "line-color": "#818cf8",
      "target-arrow-color": "#818cf8",
    },
  },
  {
    selector: "edge.rca-up",
    style: {
      "line-style": "solid",
      "line-color": "#16a34a",
      "target-arrow-color": "#16a34a",
      width: "data(w)",
      "line-opacity": "data(op)",
      "text-opacity": "data(op)",
    },
  },
  {
    selector: "edge.rca-down",
    style: {
      "line-style": "solid",
      "line-color": "#dc2626",
      "target-arrow-color": "#dc2626",
      width: "data(w)",
      "line-opacity": "data(op)",
      "text-opacity": "data(op)",
    },
  },
  {
    selector: "node.rca-unexplained",
    style: { "border-style": "dashed", "border-color": "#d97706", "border-width": 3 },
  },
  /* what-if overlay: sign=hue, certainty=background opacity, pinned=heavy border */
  {
    selector: "node.sim-up",
    style: { "background-color": "#dcfce7", "border-color": "#16a34a", "background-opacity": "data(bgop)" },
  },
  {
    selector: "node.sim-down",
    style: { "background-color": "#fee2e2", "border-color": "#dc2626", "background-opacity": "data(bgop)" },
  },
  {
    selector: "node.sim-pinned",
    style: { "border-width": 4, "border-style": "solid", "border-color": "#4f46e5" },
  },
  {
    selector: "node.warn-border",
    style: { "border-style": "dashed", "border-color": "#d97706", "border-width": 3 },
  },
  {
    selector: "node.lever",
    style: {
      shape: "ellipse",
      "background-color": "#fef3c7",
      "border-style": "dashed",
      "border-color": "#d97706",
      "font-size": 11,
    },
  },
  {
    selector: "edge.assume",
    style: {
      "line-style": "dotted",
      "line-color": "#d97706",
      "target-arrow-color": "#d97706",
      width: 2.5,
      color: "#92400e",
    },
  },
  {
    selector: "edge.sim-up",
    style: {
      "line-style": "solid",
      "line-color": "#16a34a",
      "target-arrow-color": "#16a34a",
      "line-opacity": "data(op)",
    },
  },
  {
    selector: "edge.sim-down",
    style: {
      "line-style": "solid",
      "line-color": "#dc2626",
      "target-arrow-color": "#dc2626",
      "line-opacity": "data(op)",
    },
  },
  { selector: ".faded", style: { opacity: 0.25 } },
  {
    selector: ".pathhl",
    style: { "border-color": "#d97706", "line-color": "#d97706", "target-arrow-color": "#d97706" },
  },
];

function buildGraph() {
  const defs = Object.fromEntries(state.dag.nodes.map(([name, def]) => [name, def]));
  state.defs = defs;
  // reverse adjacency: child -> [parents], used to size the run-progress status.
  // forward adjacency: parent -> [children], used for what-if descendant walks.
  state.revAdj = {};
  state.fwdAdj = {};
  state.dag.nodes.forEach(([name]) => {
    state.revAdj[name] = [];
    state.fwdAdj[name] = [];
  });
  const elements = [];

  state.dag.nodes.forEach(([name, def]) => {
    elements.push({
      data: { id: name, label: name, bgop: 1 },
      classes: nodeType(def),
    });
  });
  state.dag.edges.forEach(([src, dst]) => {
    const kind = defs[dst].formula ? "formula" : "prob";
    state.revAdj[dst].push(src);
    state.fwdAdj[src].push(dst);
    // Cold start: a probabilistic edge's "fit" is its stated prior — label it
    // up front as a belief (fitted mode labels β only after an /analyze run).
    const label = coldStart() && kind === "prob" ? beliefEdgeLabel(defs[dst], src) : "";
    elements.push({
      data: { id: `${src}->${dst}`, source: src, target: dst, label, w: 2, op: 1 },
      classes: kind,
    });
  });

  state.cy = cytoscape({
    container: $("cy"),
    elements,
    style: CY_STYLE,
    wheelSensitivity: 0.2,
  });
  // Paint the stat cards first (this fixes each node's size), then lay out so
  // dagre spaces the real card footprints. Falls back to label-sized nodes when
  // the series failed to load (renderAllCards is a no-op without state.series).
  if (coldStart()) state.coldBase = computeColdBase();
  renderAllCards();
  runLayout(true);

  // With the What-if tab active, tapping a node adjusts it in the scenario;
  // otherwise it opens the Metric tab as before.
  state.cy.on("tap", "node", (evt) => {
    if (evt.target.hasClass("lever")) return;
    if (activeTab() === "whatif") openAdjustPanel(evt.target.id());
    else selectMetric(evt.target.id());
  });
  markFitted();
}

function markFitted() {
  state.meta.fitted.forEach((name) => {
    const n = state.cy.getElementById(name);
    if (n) n.addClass("fitted");
  });
}

/* How many probabilistic ancestors of `target` still need fitting — drives the
   run-progress status. Walks the reverse adjacency built in buildGraph. */
function countUpstreamFits(target) {
  const defs = state.defs || {};
  const fitted = new Set(state.meta.fitted);
  const seen = new Set();
  const stack = [...(state.revAdj[target] || [])];
  let k = 0;
  while (stack.length) {
    const name = stack.pop();
    if (seen.has(name)) continue;
    seen.add(name);
    const def = defs[name];
    if (def && def.parents && def.parents.length && !def.formula && !fitted.has(name)) k++;
    (state.revAdj[name] || []).forEach((p) => stack.push(p));
  }
  return k;
}

/* Label a fitted probabilistic node's incoming edges with beta_raw. */
function labelBetaEdges(name, metricData) {
  const def = metricData.definition;
  const summary = metricData.summary;
  if (!summary || def.formula || !def.parents.length) return;
  def.parents.forEach((p, i) => {
    const key = def.parents.length > 1 ? `beta_raw[${i}]` : `beta_raw[0]`;
    const mean = summary.mean?.[key];
    const lo = summary["hdi_2.5%"]?.[key];
    const hi = summary["hdi_97.5%"]?.[key];
    if (mean === undefined) return;
    const edge = state.cy.getElementById(`${p}->${name}`);
    if (edge.length && !state.rca) {
      edge.data("label", `β ${fmt(mean)} [${fmt(lo)}, ${fmt(hi)}]`);
    }
  });
}

/* ---------- metric tab ---------- */

async function selectMetric(name) {
  state.selected = name;
  history.replaceState(null, "", `#metric=${encodeURIComponent(name)}`);
  updateShareMenu();
  state.cy.$("node:selected").unselect();
  state.cy.getElementById(name).select();
  switchTab("metric");
  const container = $("tab-metric");
  container.innerHTML = `<p class="placeholder">Loading ${esc(name)}…</p>`;
  try {
    const data = state.metricCache[name] || (await api(`/metrics/${encodeURIComponent(name)}`));
    state.metricCache[name] = data;
    renderMetricTab(name, data);
    labelBetaEdges(name, data);
  } catch (err) {
    container.innerHTML = `<p class="placeholder">Failed to load: ${esc(err.message)}</p>`;
  }
}

function renderMetricTab(name, data) {
  const def = data.definition;
  const type = nodeType(def);
  const typeLabel = { source: "Source", prob: "Probabilistic", formula: "Formula" }[type];
  const fitted = state.meta.fitted.includes(name);

  const grain = def.grain || "day";
  const lagUnit = grain === "day" ? "d" : ` ${grain}(s)`;
  const parentChips = (def.parents || [])
    .map((p) => {
      const lag = def.lags && def.lags[p];
      return `<code>${esc(p)}</code>${lag ? ` <span class="chip lag">lag ${lag}${lagUnit}</span>` : ""}`;
    })
    .join(", ") || "<span style='color:var(--muted)'>none (source)</span>";

  let html = `
    <div class="metric-title">
      <h2>${esc(name)}</h2>
      <span class="chip ${type}">${typeLabel}</span>
      ${fitted ? '<span class="chip fitted">fitted</span>' : ""}
    </div>
    ${def.description ? `<p class="desc">${esc(def.description)}</p>` : ""}
    <table class="kv">
      <tr><td>Source</td><td><code>${esc(def.source)}</code> <button class="linkish" data-query="${esc(name)}">show query</button></td></tr>
      <tr><td>Grain</td><td><code>${esc(grain)}</code> · ${esc(def.kind || "flow")}</td></tr>
      ${state.meta.data_through && state.meta.data_through[name]
        ? `<tr><td>Data through</td><td>${esc(state.meta.data_through[name])}${
            state.meta.data_through[name] < state.meta.date_end
              ? ' <span class="chip lag">lags window end</span>' : ""
          }</td></tr>`
        : ""}
      <tr><td>Parents</td><td>${parentChips}</td></tr>
      ${def.formula ? `<tr><td>Formula</td><td><code>${esc(def.formula)}</code></td></tr>` : ""}
      ${def.baseline
        ? `<tr><td>Baseline</td><td>${
            def.baseline.low < def.baseline.high
              ? `${fmt(def.baseline.low)} – ${fmt(def.baseline.high)} <span class="chip lag">90% belief</span>`
              : `${fmt(def.baseline.low)} <span class="chip lag">asserted</span>`
          }</td></tr>`
        : ""}
      ${def.plausible
        ? `<tr><td>Plausible</td><td>${[
            def.plausible.min != null ? `min ${fmt(def.plausible.min)}` : null,
            def.plausible.max != null ? `max ${fmt(def.plausible.max)}` : null,
          ].filter(Boolean).join(" · ")}</td></tr>`
        : ""}
    </table>
    <div class="query-provenance" hidden></div>`;

  if (coldStart()) {
    // No series, no posterior, no fitting — the declarations above ARE the
    // model. Say so instead of rendering empty history sections.
    const cb = (state.coldBase || {})[name];
    html += `
      <section>
        <h3>Cold start</h3>
        <p class="inline-status">This tree declares no data provider. ${
          def.formula
            ? `The operating point${cb && cb.mu != null ? ` (${fmt(cb.mu)})` : ""} derives from the parents' asserted baselines through the formula.`
            : "The asserted baseline above is this metric's operating point; edge slopes come from the stated priors."
        } Use the What-if tab to simulate over these beliefs.</p>
      </section>`;
    $("tab-metric").innerHTML = html;
    wireQueryProvenance($("tab-metric"));
    return;
  }

  html += `
    <section>
      <h3>Card display</h3>
      <div class="analyze-row">
        <select id="node-variant">
          <option value="">Canvas default (${VARIANT_LABEL[state.cardConfig.variant]})</option>
          <option value="num">Number</option>
          <option value="delta">Number + Δ</option>
          <option value="spark">Number + spark</option>
          <option value="full">Number + Δ + spark</option>
        </select>
      </div>
      <p class="inline-status">Override how just this node is drawn on the canvas.</p>
    </section>

    <section>
      <h3>Time series</h3>
      <div id="ts-chart"></div>
      <div class="chart-caption"><span class="win-ref"></span> reference &nbsp; <span class="win-an"></span> analysis</div>
    </section>

    <section>
      <h3>Posterior</h3>
      <div id="posterior-box"></div>
    </section>

    <section>
      <h3>Analyze</h3>
      <div class="analyze-row">
        <select id="an-method">
          <option value="advi">ADVI (fast)</option>
          <option value="nuts">NUTS (accurate)</option>
        </select>
        <input type="number" id="an-draws" value="500" min="50" max="5000" step="50">
        <button id="an-run" class="primary">Run</button>
      </div>
      <div class="inline-status" id="an-status"></div>
    </section>
  `;
  $("tab-metric").innerHTML = html;
  wireQueryProvenance($("tab-metric"));

  renderTimeSeries(name, data.time_series);
  renderPosterior(name, data);
  $("an-run").addEventListener("click", () => runAnalyze(name));
  wireNodeVariant(name);
}

/* Per-node card-variant override, set from the Metric tab. Empty value clears
   the override (node follows the canvas default again). Re-lays out only when
   the card's height actually changes. */
function wireNodeVariant(name) {
  const sel = $("node-variant");
  if (!sel) return;
  sel.value = state.cardConfig.overrides[name] || "";
  sel.addEventListener("change", () => {
    if (sel.value) state.cardConfig.overrides[name] = sel.value;
    else delete state.cardConfig.overrides[name];
    saveCardConfig();
    const node = state.cy.getElementById(name);
    const before = node.length ? node.height() : 0;
    renderNodeCard(name);
    if (node.length && node.height() !== before) runLayout(false);
  });
}

function renderTimeSeries(name, series) {
  const x = series.map((r) => r.date);
  const y = series.map((r) => r[name]);
  const shapes = [];
  const win = readWindows();
  if (win) {
    shapes.push(
      { type: "rect", xref: "x", yref: "paper", x0: win.reference_start, x1: win.reference_end, y0: 0, y1: 1, fillcolor: "#94a3b8", opacity: 0.16, line: { width: 0 } },
      { type: "rect", xref: "x", yref: "paper", x0: win.analysis_start, x1: win.analysis_end, y0: 0, y1: 1, fillcolor: "#4f46e5", opacity: 0.10, line: { width: 0 } },
    );
  }
  Plotly.newPlot(
    "ts-chart",
    [{ x, y, mode: "lines", line: { color: "#4f46e5", width: 1.8 }, hovertemplate: "%{x|%b %d}: %{y:,.1f}<extra></extra>" }],
    {
      margin: { l: 45, r: 8, t: 6, b: 22 },
      height: 200,
      shapes,
      xaxis: { tickfont: { size: 10 }, gridcolor: "#f0f2f5" },
      yaxis: { tickfont: { size: 10 }, gridcolor: "#f0f2f5" },
      plot_bgcolor: "#ffffff",
      paper_bgcolor: "#ffffff",
    },
    { displayModeBar: false, responsive: true },
  );
}

function renderPosterior(name, data) {
  const box = $("posterior-box");
  const def = data.definition;
  const summary = data.summary;

  if (!summary) {
    box.innerHTML = `<p style="color:var(--muted);font-size:12.5px;margin:4px 0">
      No model fitted yet. Run an analysis below to estimate this metric's
      trend, seasonality${def.parents.length && !def.formula ? " and causal coefficients" : ""}.</p>`;
    return;
  }

  let rows = "";
  if (!def.formula && def.parents.length) {
    def.parents.forEach((p, i) => {
      const key = `beta_raw[${i}]`;
      const mean = summary.mean?.[key];
      if (mean === undefined) return;
      rows += `<tr>
        <td><code>${esc(p)}</code></td>
        <td class="num">${fmt(mean)}</td>
        <td class="num">[${fmt(summary["hdi_2.5%"]?.[key])}, ${fmt(summary["hdi_97.5%"]?.[key])}]</td>
      </tr>`;
    });
  }

  const coefTable = rows
    ? `<table class="data-table">
         <tr><th>Parent</th><th class="num">β (raw units)</th><th class="num">95% HDI</th></tr>
         ${rows}
       </table>`
    : `<p style="color:var(--muted);font-size:12.5px;margin:4px 0">
         ${def.formula ? "Formula node — the structural relationship is exact; the model fits the residual." : "No causal parents — trend and seasonality only."}</p>`;

  const signWarnings = (data.diagnostics && data.diagnostics.sign_warnings) || [];
  const signWarningHtml = signWarnings
    .map((w) => `<p class="sign-warning">⚠ ${esc(w)}</p>`)
    .join("");

  // diagnostics: worst r_hat across all parameters (NUTS only; ADVI has none)
  let diag = "";
  const rhats = Object.values(summary.r_hat || {}).filter((v) => v !== null && !Number.isNaN(v));
  if (rhats.length) {
    const worst = Math.max(...rhats);
    const cls = worst < 1.05 ? "ok" : "warn";
    const note = worst < 1.05 ? "converged" : "check convergence";
    diag = `<div class="diag">max R̂ = <span class="${cls}">${worst.toFixed(3)}</span> (${note})</div>`;
  }

  // full raw summary behind a collapsible
  const params = Object.keys(summary.mean || {});
  const fullRows = params
    .map((k) => `<tr class="${k.startsWith("trend") ? "dim" : ""}">
        <td><code>${esc(k)}</code></td>
        <td class="num">${fmt(summary.mean[k])}</td>
        <td class="num">${fmt(summary.sd?.[k])}</td>
        <td class="num">[${fmt(summary["hdi_2.5%"]?.[k])}, ${fmt(summary["hdi_97.5%"]?.[k])}]</td>
      </tr>`)
    .join("");

  box.innerHTML = `
    ${coefTable}
    ${signWarningHtml}
    ${diag}
    <details>
      <summary>All parameters (${params.length})</summary>
      <table class="data-table">
        <tr><th>Param</th><th class="num">mean</th><th class="num">sd</th><th class="num">95% HDI</th></tr>
        ${fullRows}
      </table>
    </details>`;
}

async function runAnalyze(name) {
  const method = $("an-method").value;
  const draws = $("an-draws").value;
  const btn = $("an-run");
  const status = $("an-status");
  btn.disabled = true;
  status.className = "inline-status";
  status.textContent = method === "nuts" ? "Sampling with NUTS — this can take a minute…" : "Fitting with ADVI…";
  try {
    await api(`/analyze/${encodeURIComponent(name)}?inference_method=${method}&draws=${draws}`, { method: "POST" });
    delete state.metricCache[name];
    state.meta = await api("/meta");
    markFitted();
    status.textContent = "Done.";
    await selectMetric(name);
  } catch (err) {
    status.className = "inline-status error";
    status.textContent = err.message;
    btn.disabled = false;
  }
}

/* ---------- RCA ---------- */

function readWindows() {
  const v = (id) => $(id).value;
  if (!v("ref-start") || !v("ref-end") || !v("an-start") || !v("an-end")) return null;
  return {
    reference_start: v("ref-start"),
    reference_end: v("ref-end"),
    analysis_start: v("an-start"),
    analysis_end: v("an-end"),
  };
}

async function runRCA() {
  const target = $("target-select").value;
  if (!validateWindows()) return;
  const win = readWindows();
  if (!win) {
    setStatus("Set all four window dates first.", "error");
    return;
  }
  const btn = $("run-rca");
  btn.disabled = true;
  const k = countUpstreamFits(target);
  setStatus(
    k > 0 ? `Running RCA — fitting ${k} upstream model${k === 1 ? "" : "s"}…` : "Running RCA…",
    "busy",
  );
  try {
    const qs = new URLSearchParams(win).toString();
    state.slices = {}; // a new analysis invalidates any slice of the old one
    state.rca = await api(`/rca/${encodeURIComponent(target)}?${qs}`, { method: "POST" });
    history.replaceState(null, "", `#rca=${encodeURIComponent(target)}&${qs}`);
    updateShareMenu();
    state.meta = await api("/meta"); // on-demand fits may have been cached
    state.metricCache = {};
    markFitted();
    applyRcaOverlay();
    renderRcaTab();
    switchTab("rca");
    $("clear-rca").style.display = "";
    setStatus(`RCA complete for ${target}.`);
  } catch (err) {
    setStatus(`RCA failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function applyRcaOverlay() {
  const res = state.rca;
  const cy = state.cy;
  clearOverlays();

  const inScope = new Set(Object.keys(res.nodes));
  cy.nodes().forEach((n) => {
    if (!inScope.has(n.id())) n.addClass("faded");
  });
  cy.edges().forEach((e) => {
    if (!inScope.has(e.source().id()) || !inScope.has(e.target().id())) e.addClass("faded");
  });

  Object.entries(res.nodes).forEach(([name, node]) => {
    const n = cy.getElementById(name);
    const tint = goodClass(name, node.gap >= 0 ? "up" : "down", "rca");
    if (tint) n.addClass(tint);
    // large-unexplained badge: dashed amber border + ◌ glyph on the card
    let mark = null;
    if (
      node.contributions.length &&
      node.unexplained != null &&
      Math.abs(node.gap) > 1e-9 &&
      Math.abs(node.unexplained / node.gap) > 0.35
    ) {
      n.addClass("rca-unexplained");
      mark = "◌";
    }
    // the card shows the RCA change (gap over the two windows) while RCA is on
    state.cardOverlay[name] = {
      value: null, // keep the latest value as the big number
      dpct: node.relative_change,
      dir: node.gap >= 0 ? "up" : "down",
      mark,
    };
    node.contributions.forEach((c) => {
      const e = cy.getElementById(`${c.parent}->${name}`);
      if (!e.length) return;
      const share = c.share_of_gap === null ? 0 : Math.min(Math.abs(c.share_of_gap), 1);
      e.data("w", 2 + 6 * share);
      // certainty channel: opacity from prob_same_direction (null -> solid)
      e.data("op", c.prob_same_direction == null ? 1 : Math.max(0.35, 2 * (c.prob_same_direction - 0.5)));
      e.data("label", c.share_of_gap === null ? "" : pct(Math.abs(c.share_of_gap)));
      // Edge color = the effect's goodness for the CHILD it lands on.
      const edgeTint = goodClass(name, c.estimate >= 0 ? "up" : "down", "rca");
      if (edgeTint) e.addClass(edgeTint);
    });
  });

  renderAllCards(); // repaint cards with the RCA overlay folded in
  document.querySelectorAll(".rca-only").forEach((el) => (el.style.display = "flex"));
}

function clearRcaStyles() {
  const cy = state.cy;
  cy.elements().removeClass("faded rca-up rca-down pathhl rca-unexplained");
  cy.nodes().forEach((n) => n.data("label", n.id()));
  cy.edges().forEach((e) => {
    e.data("w", 2);
    e.data("op", 1);
    // cold start: the resting label of a probabilistic edge is its belief,
    // not blank (fitted mode re-labels β from cached fits after this)
    const belief = coldStart() && e.hasClass("prob")
      ? beliefEdgeLabel(state.defs[e.target().id()], e.source().id())
      : "";
    e.data("label", belief);
  });
  state.cardOverlay = {};
  renderAllCards(); // restore base cards
  document.querySelectorAll(".rca-only").forEach((el) => (el.style.display = "none"));
}

function clearWhatifStyles() {
  const cy = state.cy;
  cy.$(".assume").remove();
  cy.$(".lever").remove(); // temporary lever nodes (their edges went with .assume)
  cy.elements().removeClass("sim-up sim-down sim-pinned warn-border");
  cy.nodes().forEach((n) => n.data("bgop", 1));
  state.cardOverlay = {};
  renderAllCards(); // restore base cards
  document.querySelectorAll(".sim-only").forEach((el) => (el.style.display = "none"));
}

/* The RCA and what-if overlays are exclusive: the active tab owns the canvas. */
function clearOverlays() {
  clearWhatifStyles();
  clearRcaStyles();
}

async function clearRCA() {
  state.rca = null;
  state.activeCause = null;
  state.slices = {}; // slices are scoped to one analysis; they never outlive it
  clearRcaStyles();
  // fast path: restore beta labels from cached metric data
  Object.entries(state.metricCache).forEach(([name, data]) => labelBetaEdges(name, data));
  // then fetch any fitted metric we don't yet have cached, so every fitted
  // probabilistic node regains its β labels (not just cached ones)
  for (const name of state.meta.fitted) {
    if (state.metricCache[name]) continue;
    try {
      const data = await api(`/metrics/${encodeURIComponent(name)}`);
      state.metricCache[name] = data;
      labelBetaEdges(name, data);
    } catch { /* skip metrics that fail to load */ }
  }
  $("rca-results").innerHTML = '<p class="placeholder">Set the two windows and run.</p>';
  $("clear-rca").style.display = "none";
  setStatus("");
  updateShareMenu();
}

function highlightCause(causeName) {
  const cy = state.cy;
  cy.elements().removeClass("pathhl");
  state.activeCause = causeName;

  // all edges on any directed path causeName -> target, within RCA scope
  const target = state.rca.target;
  const inScope = new Set(Object.keys(state.rca.nodes));
  const onPath = new Set();
  const walk = (node, trail) => {
    if (node === target) {
      trail.forEach((id) => onPath.add(id));
      return;
    }
    cy.getElementById(node).outgoers("edge").forEach((e) => {
      const next = e.target().id();
      if (inScope.has(next)) walk(next, [...trail, e.id()]);
    });
  };
  walk(causeName, []);

  onPath.forEach((id) => cy.getElementById(id).addClass("pathhl"));
  cy.getElementById(causeName).addClass("pathhl");
  cy.animate({ center: { eles: cy.getElementById(causeName) }, duration: 300 });

  document.querySelectorAll(".cause-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.metric === causeName);
  });
}

/* ---------- Dimensional slicing (roadmap 3.2) ----------
   The tree says which metric moved; a slice says where inside it. Offered
   per ranked cause, because that is the order the question actually gets
   asked in: traverse to the cause, then localize it. */

/* The windows a slice must use are the ones the metric's contribution was
   measured over — which, across a lagged edge, are shifted back by the lag.
   Slicing a lagged parent over the target's calendar window would compare the
   wrong periods and quietly answer a different question. */
function sliceWindowsFor(metric) {
  const res = state.rca;
  for (const node of Object.values(res.nodes)) {
    for (const c of node.contributions || []) {
      if (c.parent === metric && c.parent_windows) {
        return { windows: c.parent_windows, lag: c.lag || null };
      }
    }
  }
  const ew = (res.nodes[metric] || {}).effective_windows;
  return ew ? { windows: ew, lag: null } : null;
}

function declaredDimensions(metric) {
  const def = state.defs && state.defs[metric];
  return def && def.dimensions ? Object.keys(def.dimensions) : [];
}

function sliceControlsHtml(metric) {
  const dims = declaredDimensions(metric);
  if (!dims.length || !sliceWindowsFor(metric)) return "";
  const active = state.slices[metric] || {};
  const buttons = dims
    .map(
      (d) =>
        `<button class="slice-btn${active.dimension === d ? " active" : ""}" data-metric="${esc(metric)}" data-dimension="${esc(d)}">${esc(d)}</button>`,
    )
    .join("");
  return `<div class="slice-controls" data-for="${esc(metric)}">
      <span class="slice-label">slice by</span>${buttons}
      <div class="slice-panel" id="slice-panel-${cssId(metric)}">${sliceResultHtml(metric)}</div>
    </div>`;
}

/* Metric names are author-controlled and can contain characters that are not
   valid in an id; keep the mapping total rather than assuming they are. */
const cssId = (name) => name.replace(/[^A-Za-z0-9_-]/g, "_");

function sliceResultHtml(metric) {
  const s = state.slices[metric];
  if (!s) return "";
  if (s.loading) return '<p class="inline-status">slicing…</p>';
  if (s.error) return `<p class="inline-status error">Slice failed: ${esc(s.error)}</p>`;

  const r = s.result;
  const rate = r.attribution_method === "slice_blend";
  const lagNote = s.lag
    ? ` · windows shifted back ${s.lag} ${esc(r.grain)}${s.lag === 1 ? "" : "s"} for the lag`
    : "";
  // Four columns is what the 410px sidebar fits without clipping. The excess
  // CI rides along as a tooltip rather than a fifth column — it qualifies the
  // ranking, so it belongs on the number it qualifies.
  const head = rate
    ? `<tr><th>${esc(r.dimension)}</th><th class="num">within</th><th class="num">mix</th><th class="num">excess</th></tr>`
    : `<tr><th>${esc(r.dimension)}</th><th class="num">of gap</th><th class="num">baseline</th><th class="num">excess</th></tr>`;

  // Rate contributions land around 1e-4, where fmt's toPrecision(3) produces
  // more digits than a sidebar column can hold. Fall back to an exponent for
  // the very small so the table stays readable instead of ellipsing away the
  // significant figures; full precision rides in the tooltip.
  const fmtTight = (x) =>
    x === null || x === undefined
      ? "—"
      : x !== 0 && Math.abs(x) < 0.01
        ? x.toExponential(1)
        : fmt(x);

  const excessCell = (row) => {
    const ci = row.ci_95 ? `95% CI [${fmt(row.ci_95[0])}, ${fmt(row.ci_95[1])}]` : "no CI";
    const p = row.prob_concentrated == null ? "" : ` · P(direction) ${pct(row.prob_concentrated)}`;
    return `<td class="num" title="${esc(`excess ${row.excess} · ${ci}${p}`)}">${fmtTight(row.excess)}</td>`;
  };
  const rows = r.slices
    .map((row, i) => {
      // Concentration is relative to size: lead with the slice carrying more
      // of the gap than its baseline share predicts, and say when the
      // bootstrap cannot separate that from zero.
      const noise = row.noise_level
        ? ' <span class="slice-noise" title="The bootstrap cannot distinguish this slice&#39;s concentration from zero — the gap is not localized here.">noise</span>'
        : "";
      const lead = i === 0 && !row.noise_level ? ' class="slice-lead"' : "";
      const label = `<td><code>${esc(row.value)}</code>${noise}${row.n_values ? ` <span class="dim">(${row.n_values})</span>` : ""}</td>`;
      return rate
        ? `<tr${lead}>${label}
             <td class="num" title="the slice's own rate moved: ${row.within}">${fmtTight(row.within)}</td>
             <td class="num" title="traffic moved between slices: ${row.mix}">${fmtTight(row.mix)}</td>
             ${excessCell(row)}
           </tr>`
        : `<tr${lead}>${label}
             <td class="num" title="contribution ${fmt(row.contribution)}">${row.share_of_gap == null ? "—" : pct(row.share_of_gap)}</td>
             <td class="num">${row.baseline_share == null ? "—" : pct(row.baseline_share)}</td>
             ${excessCell(row)}
           </tr>`;
    })
    .join("");

  // The headline claim only survives if the leader is genuinely concentrated.
  // Ranking always produces a first row, so without this the panel would name
  // a slice even when the gap is spread evenly — the failure mode that makes
  // flat slicers untrustworthy. Concentration is excess as a share of the gap:
  // scale-free, and unlike a share-vs-baseline ratio it does not punish slices
  // that are already large (mobile is half the traffic and still the culprit).
  const top = r.slices[0];
  const concentration =
    top && top.excess != null && Math.abs(r.gap) > 1e-12
      ? Math.abs(top.excess / r.gap)
      : 0;
  const localized =
    top && !top.noise_level && top.baseline_share != null && top.share_of_gap != null
      && concentration >= 0.25;
  const verdict = localized
    ? `<p class="slice-verdict"><code>${esc(top.value)}</code> carries
         <strong>${pct(top.share_of_gap)}</strong> of the gap on a
         ${pct(top.baseline_share)} baseline share.</p>`
    : `<p class="slice-verdict dim">Not localized by ${esc(r.dimension)} — no slice carries
         enough of the gap beyond its own size to single it out.</p>`;

  // mix_total is an {estimate, ci_95} block, like the tree's interaction row.
  const mixNote =
    rate && r.mix_total
      ? `<p class="inline-status">mix shift total ${fmt(r.mix_total.estimate)} — how much of the move is
         traffic shifting between slices rather than any slice's own rate changing.</p>`
      : "";
  // Two different facts wore one red banner. Overlap is arithmetic — an entity
  // holding several values of this dimension inside a period is counted once in
  // the metric and once per value — and is known from the binding before any
  // query runs. `discrepant` means an *unexplained* divergence the user should
  // go and investigate. Painting the first one red told people their pipeline
  // was broken when nothing was, and made the flag worthless for the second.
  // Entity flows sit *beside* the attribution, never inside it: they compare
  // window-level sets, which do not reconcile to a window-mean gap. Labelled
  // as movement rather than contribution so the two are not read as one
  // decomposition — the migration line is the whole point, because a user
  // switching platform otherwise reads as two large offsetting causes.
  const ef = r.entity_flows;
  const flows = ef
    ? `<div class="entity-flows">
         <div class="flows-head">Movement between windows · ${ef.totals.new} new ·
           ${ef.totals.churned} churned · ${ef.totals.retained} retained ·
           ${ef.totals.migrated} moved between slices</div>
         ${ef.migrations.length
           ? `<p class="inline-status">${ef.migrations
               .slice(0, 3)
               .map((m) => `${esc(m.from)} → ${esc(m.to)}: ${m.entities}`)
               .join(" · ")}${ef.totals.migrated
               ? " — movement between slices nets to zero, so it shifts where the metric sits without changing its total."
               : ""}</p>`
           : ""}
         ${(ef.caveats || []).length
           ? `<p class="inline-status">${ef.caveats.map(esc).join(" ")}</p>`
           : ""}
         <p class="muted">Counts of entities across the two windows. They describe
           what kind of movement happened; they do not sum to the gap above.</p>
       </div>`
    : "";
  const overlap =
    r.additivity === "overlapping" && r.overlap
      ? `<p class="inline-status">These slices share entities, so they overstate the
         metric by ${fmt(r.overlap.mean)} on average
         (${pct(r.overlap.share_of_baseline)} of baseline) — deduplication overlap,
         not an unexplained cause. Contribution shares are withheld.</p>`
      : "";
  const recon =
    r.reconciliation && !["ok", "not_applicable"].includes(r.reconciliation.status)
      ? `<p class="inline-status error">Slices do not sum back to the metric
         (mean residual ${fmt(r.reconciliation.mean_residual)},
         ${pct(r.reconciliation.residual_share_of_baseline)} of baseline) — reported, not rescaled.</p>`
      : "";
  const caveats = (r.caveats || []).length
    ? `<p class="inline-status">${r.caveats.map(esc).join(" ")}</p>`
    : "";
  const degenerate =
    r.ci_status === "degenerate_single_period"
      ? '<p class="inline-status">Single-period window: no bootstrap CI.</p>'
      : "";

  return `${verdict}
    <table class="data-table slice-table">${head}${rows}</table>
    <p class="slice-windows">${esc(r.effective_windows.reference.start)} → ${esc(r.effective_windows.reference.end)}
      vs ${esc(r.effective_windows.analysis.start)} → ${esc(r.effective_windows.analysis.end)}${lagNote}</p>
    ${mixNote}${degenerate}${caveats}${overlap}${flows}${recon}`;
}

async function toggleSlice(metric, dimension) {
  const current = state.slices[metric];
  if (current && current.dimension === dimension && !current.error) {
    delete state.slices[metric];
    renderRcaTab();
    return;
  }
  const win = sliceWindowsFor(metric);
  if (!win) return;
  state.slices[metric] = { dimension, loading: true, lag: win.lag };
  renderRcaTab();

  const qs = new URLSearchParams({
    dimension,
    reference_start: win.windows.reference.start,
    reference_end: win.windows.reference.end,
    analysis_start: win.windows.analysis.start,
    analysis_end: win.windows.analysis.end,
  }).toString();
  try {
    const result = await api(`/rca/${encodeURIComponent(metric)}/slices?${qs}`, { method: "POST" });
    state.slices[metric] = { dimension, result, lag: win.lag };
  } catch (err) {
    state.slices[metric] = { dimension, error: err.message, lag: win.lag };
  }
  renderRcaTab();
}

function renderRcaTab() {
  const res = state.rca;
  const target = res.nodes[res.target];

  if (target.status === "window_shorter_than_grain") {
    $("rca-results").innerHTML = `
      <div class="rca-card">
        <div class="sub">${esc(res.target)}</div>
        <p class="placeholder">The requested windows contain no whole
        <strong>${esc(target.grain)}</strong> period, so this
        ${esc(target.grain)}-grain metric cannot be analyzed. Widen the
        windows to at least one full ${esc(target.grain)}.</p>
      </div>`;
    return;
  }

  const dirCls = goodDir(res.target, target.gap >= 0 ? "up" : "down");
  const skipped = Object.entries(res.nodes)
    .filter(([, n]) => n.status === "window_shorter_than_grain")
    .map(([name, n]) => `<code>${esc(name)}</code> (${esc(n.grain)})`);
  const skippedNote = skipped.length
    ? `<p class="inline-status">Not analyzed — window shorter than grain: ${skipped.join(", ")}.</p>`
    : "";

  const maxScore = Math.max(...res.ranked_causes.map((c) => c.score), 1e-9);
  const causeRows = res.ranked_causes
    .map(
      (c, i) => `
      <div class="cause-row" data-metric="${esc(c.metric)}">
        <span class="cause-rank">${i + 1}</span>
        <span class="cause-name">${esc(c.metric)}</span>
        <span class="cause-bar-wrap"><span class="cause-bar" style="width:${(100 * c.score) / maxScore}%"></span></span>
        <span class="cause-via">via ${esc(c.via || "—")}</span>
      </div>
      ${sliceControlsHtml(c.metric)}`,
    )
    .join("");

  // attribution detail: target first, then ranked order
  const order = [res.target, ...res.ranked_causes.map((c) => c.metric)];
  const view = state.rcaView;
  const shareOf = (v, gap) => (Math.abs(gap) > 1e-12 ? pct(v / gap) : "—");
  const ciCell = (ci) => (ci ? `[${fmt(ci[0])}, ${fmt(ci[1])}]` : "—");
  const anyTwoLevel = order.some((name) => {
    const n = res.nodes[name];
    return n && n.contributions.some((c) => c.decomposition);
  });
  const blocks = order
    .filter((name) => res.nodes[name] && res.nodes[name].contributions.length)
    .map((name) => {
      const node = res.nodes[name];
      const method = node.attribution_method === "shapley" ? "Shapley (exact)" : "posterior";
      const ew = node.effective_windows;
      const snapNote =
        node.grain && node.grain !== "day" && ew
          ? ` · ${esc(node.grain)} grain, snapped to ${ew.reference.n_periods}+${ew.analysis.n_periods} whole ${esc(node.grain)}s`
          : "";
      const ciNote =
        node.ci_status === "degenerate_single_period"
          ? ` · single-period window: no bootstrap CI`
          : "";
      const signNote =
        node.sign_warnings && node.sign_warnings.length
          ? ` · <span class="sign-flag" title="${esc(node.sign_warnings.join("\n\n"))}">⚠ learned sign contradicts expectation</span>`
          : "";
      const twoLevel = node.contributions.some((c) => c.decomposition);
      let header, rows, nCols;

      if (twoLevel && view === "headline") {
        // Headline: the window-means bridge per parent plus one explicit
        // co-movement (interaction) row — the price/volume/mix view.
        nCols = 4;
        header = `<tr><th>Driver</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th></tr>`;
        rows = node.contributions
          .map((c) => {
            const m = c.decomposition.means;
            return `<tr>
              <td><code>${esc(c.parent)}</code></td>
              <td class="num">${fmt(m.estimate)}</td>
              <td class="num">${shareOf(m.estimate, node.gap)}</td>
              <td class="num">${ciCell(m.ci_95)}</td>
            </tr>`;
          })
          .join("");
        if (node.interaction) {
          rows += `<tr class="interaction-row">
            <td>co-movement shift <span class="hint" title="How much of the gap comes from the parents moving together within the window (their covariance) changing between the two windows — rather than from their individual averages moving. Switch to Detailed to see how it splits across parents.">?</span></td>
            <td class="num">${fmt(node.interaction.estimate)}</td>
            <td class="num">${shareOf(node.interaction.estimate, node.gap)}</td>
            <td class="num">${ciCell(node.interaction.ci_95)}</td>
          </tr>`;
        }
      } else if (twoLevel) {
        // Detailed: full per-parent split — means + co-movement = total.
        nCols = 6;
        header = `<tr><th>Parent</th><th class="num">means</th><th class="num">co-movement</th><th class="num">total Δ</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>`;
        rows = node.contributions
          .map(
            (c) => `<tr>
              <td><code>${esc(c.parent)}</code></td>
              <td class="num">${fmt(c.decomposition.means.estimate)}</td>
              <td class="num">${fmt(c.decomposition.comovement.estimate)}</td>
              <td class="num">${fmt(c.estimate)}</td>
              <td class="num">${ciCell(c.ci_95)}</td>
              <td class="num">${c.prob_same_direction == null ? "—" : pct(c.prob_same_direction)}</td>
            </tr>`,
          )
          .join("");
      } else {
        nCols = 5;
        header = `<tr><th>Parent</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>`;
        rows = node.contributions
          .map(
            (c) => `<tr>
              <td><code>${esc(c.parent)}</code></td>
              <td class="num">${fmt(c.estimate)}</td>
              <td class="num">${c.share_of_gap === null ? "—" : pct(c.share_of_gap)}</td>
              <td class="num">${ciCell(c.ci_95)}</td>
              <td class="num">${c.prob_same_direction == null ? "—" : pct(c.prob_same_direction)}</td>
            </tr>`,
          )
          .join("");
      }

      let unexplained = "";
      if (node.unexplained !== null) {
        const dash = '<td class="num">—</td>';
        if (nCols === 6) {
          // Detailed view: unexplained sits in the "total Δ" column.
          unexplained = `<tr class="dim"><td>unexplained</td>${dash}${dash}<td class="num">${fmt(node.unexplained)}</td>${dash}${dash}</tr>`;
        } else {
          unexplained = `<tr class="dim"><td>unexplained</td><td class="num">${fmt(node.unexplained)}</td><td class="num">${shareOf(node.unexplained, node.gap)}</td>${dash.repeat(nCols - 3)}</tr>`;
        }
      }
      return `
        <div class="attr-block">
          <h4>${esc(name)} <span class="method">· ${method}${snapNote}${ciNote}${signNote}</span></h4>
          <table class="data-table">
            ${header}
            ${rows}
            ${unexplained}
          </table>
        </div>`;
    })
    .join("");

  const viewToggle = anyTwoLevel
    ? `<div class="rca-view-toggle">
         <button class="rca-view-btn${view === "headline" ? " active" : ""}" data-view="headline" title="Window-means bridge per parent, with the co-movement shift as its own row">Headline</button>
         <button class="rca-view-btn${view === "detailed" ? " active" : ""}" data-view="detailed" title="Full per-parent split: means + co-movement = total">Detailed</button>
       </div>`
    : "";

  $("rca-results").innerHTML = `
    <div class="rca-card">
      <div class="sub">${esc(res.target)} · ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)} vs ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</div>
      <div class="gap-line ${dirCls}">${target.gap >= 0 ? "+" : ""}${fmt(target.gap)} <span style="font-size:14px">(${signedPct(target.relative_change)})</span></div>
      <div id="rca-strip"></div>
      <div class="sub">${fmt(target.baseline)} → ${fmt(target.actual)} (window means${target.grain && target.grain !== "day" ? ` per ${esc(target.grain)}` : ""})</div>
      ${skippedNote}
    </div>

    <section>
      <h3>Ranked causes</h3>
      ${causeRows || '<p class="placeholder">No upstream causes — target is a source metric.</p>'}
    </section>

    <section>
      <div class="attr-head">
        <h3>Attribution detail</h3>
        ${viewToggle}
      </div>
      ${blocks || '<p class="placeholder">No attributable edges in scope.</p>'}
    </section>`;

  document.querySelectorAll(".cause-row").forEach((row) => {
    row.addEventListener("click", () => highlightCause(row.dataset.metric));
  });
  document.querySelectorAll(".slice-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // the row underneath highlights a cause; this is a different question
      toggleSlice(btn.dataset.metric, btn.dataset.dimension);
    });
  });
  document.querySelectorAll(".rca-view-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.rcaView !== btn.dataset.view) {
        state.rcaView = btn.dataset.view;
        renderRcaTab();
      }
    });
  });

  renderRcaStrip(res);
}

/* Target time-series strip inside the RCA card, shaded with the run's own
   windows (from the response, not the current header inputs). Async-safe:
   the card renders immediately; this fills the plot when the fetch resolves. */
async function renderRcaStrip(res) {
  const strip = $("rca-strip");
  if (!strip) return;
  strip.innerHTML = '<div class="strip-loading">loading series…</div>';
  const name = res.target;
  let data;
  try {
    data = state.metricCache[name] || (await api(`/metrics/${encodeURIComponent(name)}`));
    state.metricCache[name] = data;
  } catch {
    const el = $("rca-strip");
    if (el) el.remove(); // card must not break on a failed series fetch
    return;
  }
  // guard against a newer run (or a Clear) having replaced the card while awaiting
  const el = $("rca-strip");
  if (!el || state.rca !== res) return;
  el.innerHTML = "";

  const series = data.time_series;
  const x = series.map((r) => r.date);
  const y = series.map((r) => r[name]);
  const shapes = [
    { type: "rect", xref: "x", yref: "paper", x0: res.reference_window.start, x1: res.reference_window.end, y0: 0, y1: 1, fillcolor: "#94a3b8", opacity: 0.16, line: { width: 0 } },
    { type: "rect", xref: "x", yref: "paper", x0: res.analysis_window.start, x1: res.analysis_window.end, y0: 0, y1: 1, fillcolor: "#4f46e5", opacity: 0.10, line: { width: 0 } },
  ];
  Plotly.newPlot(
    el,
    [{ x, y, mode: "lines", line: { color: "#4f46e5", width: 1.6 }, hovertemplate: "%{x|%b %d}: %{y:,.1f}<extra></extra>" }],
    {
      margin: { l: 38, r: 6, t: 4, b: 18 },
      height: 120,
      shapes,
      xaxis: { tickfont: { size: 9 }, gridcolor: "#f0f2f5" },
      yaxis: { tickfont: { size: 9 }, gridcolor: "#f0f2f5" },
      plot_bgcolor: "#ffffff",
      paper_bgcolor: "#ffffff",
    },
    { displayModeBar: false, responsive: true },
  );
}

/* ---------- what-if ---------- */

function interventionLabel(iv) {
  if (iv.mode === "pct") return `${iv.metric} ${signedPct(iv.value)}`;
  if (iv.mode === "delta") return `${iv.metric} ${iv.value >= 0 ? "+" : ""}${fmt(iv.value)}`;
  return `${iv.metric} = ${fmt(iv.value)}`;
}

function effectLabel(a) {
  const f = (v) => (a.effect.kind === "relative" ? signedPct(v) : (v >= 0 ? "+" : "") + fmt(v));
  return a.effect.low === a.effect.high
    ? f(a.effect.low)
    : `${f(a.effect.low)}…${f(a.effect.high)}`;
}

function initWhatif() {
  if (coldStart()) {
    // No window to pick: operating points come from the tree's declarations.
    state.whatif.baseline = { start: null, end: null };
    renderWhatifTab();
    return;
  }
  // default baseline: last 28 days of loaded data (clamped to the range)
  const end = state.meta.date_end;
  const start28 = isoUTC(addDays(new Date(end), -27));
  state.whatif.baseline = {
    start: start28 < state.meta.date_start ? state.meta.date_start : start28,
    end,
  };
  renderWhatifTab();
}

function renderWhatifTab() {
  const w = state.whatif;
  const container = $("tab-whatif");
  if (!container || !state.meta) return;

  const items = [
    ...w.interventions.map(
      (iv, i) => `
      <div class="wf-item">
        <span class="kind intervention">adjust</span>
        <span class="what">${esc(interventionLabel(iv))}</span>
        <button class="remove" data-kind="intervention" data-idx="${i}" title="Remove">×</button>
      </div>`,
    ),
    ...w.assumptions.map(
      (a, i) => `
      <div class="wf-item">
        <span class="kind assumption">assume</span>
        <span class="what">${esc(a.source)} → ${esc(a.target)} · ${esc(effectLabel(a))}</span>
        <button class="remove" data-kind="assumption" data-idx="${i}" title="Remove">×</button>
      </div>`,
    ),
  ].join("");

  const metricOptions = state.meta.metrics
    .map((m) => `<option value="${esc(m)}">${esc(m)}</option>`)
    .join("");
  const levers = [
    ...new Set(w.assumptions.map((a) => a.source).filter((s) => !state.meta.metrics.includes(s))),
  ];
  const sourceOptions = [...state.meta.metrics, ...levers]
    .map((s) => `<option value="${esc(s)}"></option>`)
    .join("");

  // Cold start replaces the baseline window with the tree's declarations —
  // there are no dates anywhere in the scenario.
  const baselineRow = coldStart()
    ? `<p class="wf-hint">Cold start: baselines are the tree's asserted operating
       points and slopes are the stated priors — results are consequences of
       those beliefs, not evidence.</p>`
    : `
    <div class="wf-row">
      <label>Baseline</label>
      <input type="date" id="wf-start" value="${esc(w.baseline.start || "")}"
        min="${esc(state.meta.date_start)}" max="${esc(state.meta.date_end)}">
      <span class="wf-to">to</span>
      <input type="date" id="wf-end" value="${esc(w.baseline.end || "")}"
        min="${esc(state.meta.date_start)}" max="${esc(state.meta.date_end)}">
    </div>
    <p class="wf-hint">Date range for baseline metric values.</p>`;

  const builder = `
    ${baselineRow}
    <p class="wf-hint wf-hint-action">Click a node in the graph to adjust it in the scenario.</p>
    <div id="wf-adjust"></div>
    <details>
      <summary>+ Add assumption (an effect the tree doesn't know)</summary>
      <div class="wf-form">
        <p class="wf-hint">Assert a causal effect the tree hasn't learned:
        the <strong>source</strong> drives a change in the <strong>target</strong>
        metric, by an amount you believe.</p>
        <div class="wf-grid">
          <label for="wf-a-source">Source</label>
          <input type="text" id="wf-a-source" list="wf-known-sources" placeholder="e.g. discount_pct">
          <span class="wf-help">What has the effect — a tree metric, or any outside lever (free text).</span>
          <label for="wf-a-target">Target</label>
          <select id="wf-a-target">${metricOptions}</select>
          <span class="wf-help">The metric it affects — the effect lands here and propagates downstream.</span>
          <label for="wf-a-kind">Effect</label>
          <select id="wf-a-kind">
            <option value="relative">% of baseline</option>
            <option value="absolute">absolute</option>
          </select>
          <span class="wf-help">How the target moves: relative to its baseline, or in absolute units.</span>
          <label>Range</label>
          <div class="wf-row" style="margin:0">
            <input type="number" id="wf-a-low" step="any" placeholder="low"> …
            <input type="number" id="wf-a-high" step="any" placeholder="high">
          </div>
          <span class="wf-help">The effect size you're ~90% confident spans the truth (low … high).</span>
          <label for="wf-a-note">Note</label>
          <input type="text" id="wf-a-note" placeholder="optional">
        </div>
        <div class="wf-row" style="margin-top:8px">
          <button id="wf-a-add">Add assumption</button>
          <span class="inline-status" id="wf-a-status"></span>
        </div>
        <datalist id="wf-known-sources">${sourceOptions}</datalist>
      </div>
    </details>
    <section>
      <h3>Scenario</h3>
      ${items || '<p class="placeholder">Empty — adjust a metric or add an assumption.</p>'}
      <div class="wf-row" style="margin-top:10px">
        <button id="wf-run" class="primary" ${w.interventions.length + w.assumptions.length ? "" : "disabled"}>Run simulation</button>
        <button id="wf-clear" title="Remove all adjustments and assumptions (and any result)">Clear scenario</button>
      </div>
    </section>`;

  const results = w.result ? renderWhatifResults() : "";
  // reader mode (deep link): the story first, the edit surface one click away
  if (w.readerMode && w.result) {
    container.innerHTML = `${results}<details style="margin-top:14px"><summary>Edit scenario</summary>${builder}</details>`;
  } else {
    container.innerHTML = builder + results;
  }
  wireWhatifEvents();
  if (w.adjusting) renderAdjustPanel(w.adjusting);
}

function wireWhatifEvents() {
  const w = state.whatif;
  const on = (id, ev, fn) => {
    const el = $(id);
    if (el) el.addEventListener(ev, fn);
  };
  on("wf-start", "change", () => {
    w.baseline.start = $("wf-start").value;
    if (w.adjusting) renderAdjustPanel(w.adjusting);
  });
  on("wf-end", "change", () => {
    w.baseline.end = $("wf-end").value;
    if (w.adjusting) renderAdjustPanel(w.adjusting);
  });
  on("wf-run", "click", () => runWhatif());
  on("wf-clear", "click", clearWhatif);
  on("wf-clear-result", "click", clearWhatifResult);
  on("wf-a-add", "click", addAssumption);
  document.querySelectorAll("#tab-whatif .wf-item .remove").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      if (btn.dataset.kind === "intervention") w.interventions.splice(idx, 1);
      else w.assumptions.splice(idx, 1);
      renderWhatifTab();
    });
  });
}

function openAdjustPanel(name) {
  state.whatif.adjusting = name;
  state.whatif.readerMode = false; // touching the builder = operator mode
  renderWhatifTab();
}

async function renderAdjustPanel(name) {
  const box = $("wf-adjust");
  if (!box) return;
  const w = state.whatif;

  let base, hist, cold = null;
  if (coldStart()) {
    // Baseline = the declared operating point; the honesty band = declared
    // `plausible` bounds (there is no history to derive one from); the
    // shaded band = the 90% belief range when the baseline is an interval.
    const cb = (state.coldBase || {})[name];
    if (!cb) {
      box.innerHTML = `<div class="wf-adjust-card">No operating point for ${esc(name)}.</div>`;
      return;
    }
    base = cb.mu;
    const pl = (state.defs[name] || {}).plausible || null;
    cold = {
      pmin: pl && pl.min != null ? pl.min : null,
      pmax: pl && pl.max != null ? pl.max : null,
      blo: cb.lo,
      bhi: cb.hi,
    };
  } else {
    box.innerHTML = `<div class="wf-adjust-card"><p class="placeholder">Loading ${esc(name)}…</p></div>`;
    let data;
    try {
      data = state.metricCache[name] || (await api(`/metrics/${encodeURIComponent(name)}`));
      state.metricCache[name] = data;
    } catch (err) {
      const el = $("wf-adjust");
      if (el) el.innerHTML = `<div class="wf-adjust-card">Failed to load: ${esc(err.message)}</div>`;
      return;
    }
    if (state.whatif.adjusting !== name || !$("wf-adjust")) return; // stale render

    const vals = data.time_series.map((r) => r[name]).filter((v) => v != null);
    hist = {
      min: Math.min(...vals),
      max: Math.max(...vals),
      mean: vals.reduce((a, b) => a + b, 0) / vals.length,
    };
    hist.std = Math.sqrt(vals.reduce((a, v) => a + (v - hist.mean) ** 2, 0) / vals.length);
    const inWin = data.time_series
      .filter((r) => {
        const d = String(r.date).slice(0, 10);
        return (!w.baseline.start || d >= w.baseline.start) && (!w.baseline.end || d <= w.baseline.end);
      })
      .map((r) => r[name])
      .filter((v) => v != null);
    base = inWin.length ? inWin.reduce((a, b) => a + b, 0) / inWin.length : hist.mean;
  }

  const existing = w.interventions.find((iv) => iv.metric === name);
  const mode = existing ? existing.mode : "pct";
  const initial = existing ? (existing.mode === "pct" ? existing.value * 100 : existing.value) : 0;

  box.innerHTML = `
    <div class="wf-adjust-card">
      <h4>Adjust <code>${esc(name)}</code></h4>
      <div class="wf-row">
        <select id="wf-mode">
          <option value="pct" ${mode === "pct" ? "selected" : ""}>% change</option>
          <option value="delta" ${mode === "delta" ? "selected" : ""}>+/− amount</option>
          <option value="set" ${mode === "set" ? "selected" : ""}>set value</option>
        </select>
        <input type="number" id="wf-value" step="any" value="${initial}">
        <button id="wf-add" class="primary">${existing ? "Update" : "Add to scenario"}</button>
      </div>
      <input type="range" id="wf-slider" min="-50" max="50" step="0.5"
        value="${mode === "pct" ? initial : 0}" ${mode === "pct" ? "" : 'style="display:none"'}>
      <div class="wf-preview" id="wf-preview"></div>
      <div class="range-strip" id="wf-strip"></div>
      <div class="strip-caption">${
        cold
          ? "declared plausible bounds · shaded band = 90% baseline belief · amber marker = outside plausible"
          : "history min → max · shaded band = mean ± 2σ · amber marker = extrapolating"
      }</div>
    </div>`;

  const params = { base, hist, cold };
  $("wf-mode").addEventListener("change", () => {
    const m = $("wf-mode").value;
    $("wf-slider").style.display = m === "pct" ? "" : "none";
    $("wf-value").value = m === "set" ? Number(base.toPrecision(6)) : 0;
    updateAdjustPreview(params);
  });
  $("wf-value").addEventListener("input", () => {
    if ($("wf-mode").value === "pct") $("wf-slider").value = $("wf-value").value;
    updateAdjustPreview(params);
  });
  $("wf-slider").addEventListener("input", () => {
    $("wf-value").value = $("wf-slider").value;
    updateAdjustPreview(params);
  });
  $("wf-add").addEventListener("click", () => {
    const m = $("wf-mode").value;
    let value = Number($("wf-value").value);
    if (!Number.isFinite(value)) return;
    if (m === "pct") value /= 100;
    const iv = { metric: name, mode: m, value };
    const idx = state.whatif.interventions.findIndex((x) => x.metric === name);
    if (idx >= 0) state.whatif.interventions[idx] = iv;
    else state.whatif.interventions.push(iv);
    state.whatif.adjusting = null;
    renderWhatifTab();
  });
  updateAdjustPreview(params);
}

/* Live preview + range strip for the adjust panel. The strip warns about
   extrapolation *before* the run, not after. Fitted mode: history min→max
   with the ±2σ band. Cold start: declared `plausible` bounds with the 90%
   baseline-belief band — the honesty substitutes the engine itself uses. */
function updateAdjustPreview({ base, hist, cold }) {
  const modeEl = $("wf-mode"),
    valEl = $("wf-value");
  if (!modeEl || !valEl) return;
  const mode = modeEl.value;
  const v = Number(valEl.value) || 0;
  const target = mode === "pct" ? base * (1 + v / 100) : mode === "delta" ? base + v : v;
  const rel = Math.abs(base) > 1e-12 ? target / base - 1 : null;

  let out, outMsg, gLo, gHi, bandLo, bandHi;
  if (cold) {
    out = (cold.pmin != null && target < cold.pmin) || (cold.pmax != null && target > cold.pmax);
    outMsg = "⚠ outside declared plausible bounds";
    // strip guide rail: the plausible bounds where declared, else the belief
    // range, else just the neighborhood of the baseline
    gLo = cold.pmin != null ? cold.pmin : Math.min(cold.blo != null ? cold.blo : base, base);
    gHi = cold.pmax != null ? cold.pmax : Math.max(cold.bhi != null ? cold.bhi : base, base);
    bandLo = cold.blo;
    bandHi = cold.bhi;
  } else {
    const lo2 = hist.mean - 2 * hist.std,
      hi2 = hist.mean + 2 * hist.std;
    out = target < hist.min || target > hist.max || (hist.std > 0 && (target < lo2 || target > hi2));
    outMsg = "⚠ outside historical range";
    gLo = hist.min;
    gHi = hist.max;
    bandLo = hist.std > 0 ? Math.max(lo2, hist.min) : null;
    bandHi = hist.std > 0 ? Math.min(hi2, hist.max) : null;
  }

  $("wf-preview").innerHTML =
    `${fmt(base)} → <b class="${out ? "out" : ""}">${fmt(target)}</b>` +
    (rel == null ? "" : ` (${signedPct(rel)})`) +
    (out ? ` <span class="out">${outMsg}</span>` : "");

  // strip scale spans the guide rail (with padding) and always contains the marker
  const span = Math.max(gHi - gLo, 1e-9);
  const lo = Math.min(gLo - 0.1 * span, target);
  const hi = Math.max(gHi + 0.1 * span, target);
  const p = (x) => (100 * (x - lo)) / (hi - lo);
  $("wf-strip").innerHTML = `
    <div class="strip-band" style="left:${p(gLo)}%;width:${p(gHi) - p(gLo)}%;background:#e4e7ec"></div>
    ${bandLo != null && bandHi != null && bandHi > bandLo
      ? `<div class="strip-band" style="left:${p(bandLo)}%;width:${Math.max(p(bandHi) - p(bandLo), 0)}%"></div>`
      : ""}
    <div class="strip-tick" style="left:${p(base)}%" title="baseline ${fmt(base)}"></div>
    <div class="strip-marker ${out ? "out" : ""}" style="left:${Math.min(Math.max(p(target), 0), 99)}%"></div>`;
}

function addAssumption() {
  const status = $("wf-a-status");
  const source = $("wf-a-source").value.trim();
  const target = $("wf-a-target").value;
  const kind = $("wf-a-kind").value;
  let low = Number($("wf-a-low").value);
  let high = $("wf-a-high").value === "" ? low : Number($("wf-a-high").value);
  status.className = "inline-status error";
  if (!source) { status.textContent = "Source is required (a metric or a lever name)."; return; }
  if (!target) { status.textContent = "Pick a target metric."; return; }
  if ($("wf-a-low").value === "" || !Number.isFinite(low) || !Number.isFinite(high)) {
    status.textContent = "Enter a numeric effect range."; return;
  }
  if (low > high) { status.textContent = "Low must be ≤ high."; return; }
  if (kind === "relative") { low /= 100; high /= 100; }
  state.whatif.assumptions.push({
    source, target,
    effect: { kind, low, high },
    note: $("wf-a-note").value.trim() || null,
  });
  state.whatif.readerMode = false;
  renderWhatifTab();
}

function buildScenarioPayload() {
  const w = state.whatif;
  if (!coldStart() && (!w.baseline.start || !w.baseline.end)) {
    setStatus("Set the baseline window first.", "error");
    return null;
  }
  if (!w.interventions.length && !w.assumptions.length) {
    setStatus("Scenario is empty — adjust a metric or add an assumption.", "error");
    return null;
  }
  const levers = [
    ...new Set(w.assumptions.map((a) => a.source).filter((s) => !state.meta.metrics.includes(s))),
  ];
  const payload = {
    interventions: w.interventions,
    assumptions: w.assumptions.map((a, i) => ({ id: `a${i}`, ...a })),
    levers: levers.map((name) => ({ name })),
  };
  if (!coldStart()) {
    // cold start MUST omit the window (the engine rejects one — operating
    // points come from the tree's declarations)
    payload.baseline_start = w.baseline.start;
    payload.baseline_end = w.baseline.end;
  }
  return payload;
}

/* How many probabilistic nodes on affected paths still need fitting — drives
   the run-progress status. Mirrors the engine's fit-on-demand rule. */
function countWhatifFits() {
  if (coldStart()) return 0; // nothing is ever fit — slopes are the stated priors
  const w = state.whatif;
  const fitted = new Set(state.meta.fitted);
  const seeds = [...w.interventions.map((iv) => iv.metric), ...w.assumptions.map((a) => a.target)]
    .filter((m) => m in state.fwdAdj);
  const affected = new Set(seeds);
  const stack = [...seeds];
  while (stack.length) {
    const n = stack.pop();
    (state.fwdAdj[n] || []).forEach((c) => {
      if (!affected.has(c)) { affected.add(c); stack.push(c); }
    });
  }
  let k = 0;
  affected.forEach((n) => {
    const def = state.defs[n];
    if (def && def.parents && def.parents.length && !def.formula && !fitted.has(n)
        && def.parents.some((p) => affected.has(p))) k++;
  });
  return k;
}

async function runWhatif() {
  const scenario = buildScenarioPayload();
  if (!scenario) return;
  const btn = $("wf-run");
  if (btn) btn.disabled = true;
  const k = countWhatifFits();
  setStatus(k > 0 ? `Simulating — fitting ${k} model${k === 1 ? "" : "s"}…` : "Simulating…", "busy");
  try {
    state.whatif.result = await api("/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scenario),
    });
    history.replaceState(null, "", `#whatif=${encodeURIComponent(JSON.stringify(scenario))}`);
    updateShareMenu();
    state.meta = await api("/meta"); // on-demand fits may have been cached
    markFitted();
    renderWhatifTab();
    switchTab("whatif");
    applyWhatifOverlay();
    setStatus("Simulation complete.");
  } catch (err) {
    setStatus(`Simulation failed: ${err.message}`, "error");
    const b = $("wf-run");
    if (b) b.disabled = false;
  }
}

function clearWhatif() {
  const w = state.whatif;
  w.interventions = [];
  w.assumptions = [];
  w.adjusting = null;
  clearWhatifResult();
}

/* Dismiss the result + canvas overlay only; the scenario stays editable so
   the user can tweak and re-run without rebuilding it. */
function clearWhatifResult() {
  const w = state.whatif;
  w.result = null;
  w.readerMode = false;
  clearOverlays();
  // restore β edge labels from cached metric data (same as clearRCA's fast path)
  Object.entries(state.metricCache).forEach(([name, data]) => labelBetaEdges(name, data));
  if (location.hash.startsWith("#whatif=")) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  updateShareMenu();
  renderWhatifTab();
  setStatus("");
}

function applyWhatifOverlay() {
  const res = state.whatif.result;
  if (!res) return;
  const cy = state.cy;
  clearOverlays();

  const active = new Set(
    Object.entries(res.nodes)
      .filter(([, n]) => n.status !== "baseline")
      .map(([k]) => k),
  );
  // assumption source metrics stay visible: they are part of the scenario story
  const keepVisible = new Set(active);
  state.whatif.assumptions.forEach((a) => {
    if (a.source in (state.defs || {})) keepVisible.add(a.source);
  });

  cy.nodes().forEach((n) => {
    if (!keepVisible.has(n.id())) n.addClass("faded");
  });
  cy.edges().forEach((e) => {
    if (!active.has(e.source().id()) || !active.has(e.target().id())) e.addClass("faded");
  });

  Object.entries(res.nodes).forEach(([name, node]) => {
    if (node.status === "baseline") return;
    const n = cy.getElementById(name);
    if (!n.length) return;
    const est = node.delta.estimate;
    if (est !== 0) {
      const tint = goodClass(name, est > 0 ? "up" : "down", "sim");
      if (tint) n.addClass(tint);
    }
    // certainty channel: background opacity from P(direction)
    n.data("bgop", node.prob_direction == null ? 1 : Math.max(0.35, 2 * (node.prob_direction - 0.5)));
    let mark = null;
    if (node.status === "intervened") { n.addClass("sim-pinned"); mark = "⊙"; }
    if (node.extrapolation && node.extrapolation.flag) {
      n.addClass("warn-border");
      mark = (mark || "") + "⚠";
    }
    // the card shows the simulated value + its delta from baseline
    state.cardOverlay[name] = {
      value: node.simulated,
      dpct: node.relative_delta,
      dir: est > 0 ? "up" : est < 0 ? "down" : "flat",
      mark,
    };
  });

  // structural edges between affected nodes colored by the target's direction;
  // edges into a pinned node stay neutral — the pin severs them (do-operator)
  cy.edges().forEach((e) => {
    const s = res.nodes[e.source().id()],
      t = res.nodes[e.target().id()];
    if (!s || !t || s.status === "baseline" || t.status === "baseline") return;
    if (t.status === "intervened") return;
    if (t.delta.estimate !== 0) {
      const tint = goodClass(e.target().id(), t.delta.estimate > 0 ? "up" : "down", "sim");
      if (tint) e.addClass(tint);
    }
    e.data("op", t.prob_direction == null ? 1 : Math.max(0.35, 2 * (t.prob_direction - 0.5)));
  });

  // assumption links drawn as temporary elements; lever sources get a
  // temporary node placed near their target (no re-layout)
  state.whatif.assumptions.forEach((a, i) => {
    const targetNode = cy.getElementById(a.target);
    if (!targetNode.length) return;
    let srcId = a.source;
    if (!(a.source in (state.defs || {}))) {
      srcId = `lever:${a.source}`;
      if (!cy.getElementById(srcId).length) {
        cy.add({
          group: "nodes",
          data: { id: srcId, label: a.source, bgop: 1 },
          classes: "lever",
          position: leverPosition(cy, targetNode, a.source),
        });
      }
    }
    cy.add({
      group: "edges",
      data: { id: `assume:${i}`, source: srcId, target: a.target, label: effectLabel(a), w: 2.5, op: 1 },
      classes: "assume",
    });
  });

  renderAllCards(); // repaint cards with the what-if overlay folded in
  document.querySelectorAll(".sim-only").forEach((el) => (el.style.display = "flex"));
}

/* Collision-free position for a temporary lever node near its target: try a
   ring of candidate offsets and take the first whose padded footprint overlaps
   no existing node's bounding box (including previously placed levers).
   All geometry is in Cytoscape model coordinates, so offsets are derived from
   the target node's own dimensions rather than fixed pixel counts. */
function leverPosition(cy, targetNode, label) {
  const tw = targetNode.width(),
    th = targetNode.height();
  // ellipse footprint estimate: font-size 11 → ~7 model units per character
  const w = label.length * 7 + 30;
  const h = th;
  const pad = th * 0.4;
  const pos = targetNode.position();
  const gapX = tw / 2 + w / 2 + 24; // beside the target
  const gapY = th + 60; // between this rank and the one below (rankSep 70)
  const candidates = [
    [-gapX * 0.7, gapY], [gapX * 0.7, gapY], [-gapX, 0], [gapX, 0],
    [0, gapY * 1.8], [-gapX * 0.7, gapY * 1.8], [gapX * 0.7, gapY * 1.8],
    [-gapX, -gapY], [gapX, -gapY],
  ];
  const boxes = cy.nodes().map((n) => n.boundingBox());
  for (const [dx, dy] of candidates) {
    const x = pos.x + dx,
      y = pos.y + dy;
    const bb = { x1: x - w / 2 - pad, x2: x + w / 2 + pad, y1: y - h / 2 - pad, y2: y + h / 2 + pad };
    const clash = boxes.some((b) => bb.x1 < b.x2 && bb.x2 > b.x1 && bb.y1 < b.y2 && bb.y2 > b.y1);
    if (!clash) return { x, y };
  }
  return { x: pos.x - gapX * 0.7, y: pos.y + gapY }; // crowded graph: first candidate
}

function renderWhatifResults() {
  const res = state.whatif.result;
  const labelFor = (id) => {
    const s = res.sources.find((x) => x.id === id);
    return s ? s.label : id;
  };


  // outcome KPIs: affected nodes with no children
  const sinks = Object.keys(res.nodes).filter(
    (n) => res.nodes[n].status !== "baseline" && !(state.fwdAdj[n] || []).length,
  );

  const isCold = res.mode === "cold_start";
  const cards = sinks
    .map((name) => {
      const node = res.nodes[name];
      const dirCls = goodDir(name, node.delta.estimate >= 0 ? "up" : "down");
      const ci = node.delta.ci_95;
      const sub = isCold
        ? `${esc(name)} · cold start — declared beliefs`
        : `${esc(name)} · baseline ${esc(res.baseline_window.start)} → ${esc(res.baseline_window.end)}`;
      const bci = node.baseline_ci_95;
      return `
      <div class="rca-card">
        <div class="sub">${sub}</div>
        <div class="gap-line ${dirCls}">${fmt(node.baseline)} → ${fmt(node.simulated)}
          <span style="font-size:14px">(${signedPct(node.relative_delta)})</span></div>
        <div class="wf-ci">Δ ${fmt(node.delta.estimate)} · 95% CI [${fmt(ci[0])}, ${fmt(ci[1])}] · P(direction) ${pct(node.prob_direction)}${
          bci ? ` · baseline belief [${fmt(bci[0])}, ${fmt(bci[1])}]` : ""
        }</div>
        ${waterfallHtml(name, node, labelFor)}
      </div>`;
    })
    .join("");

  // per-node table, outcome-first (reverse config order keeps KPIs on top)
  const rows = [...state.meta.metrics]
    .reverse()
    .filter((n) => res.nodes[n] && res.nodes[n].status !== "baseline")
    .map((n) => {
      const node = res.nodes[n];
      const ci = node.delta.ci_95;
      return `<tr>
        <td><code>${esc(n)}</code>${node.status === "intervened" ? ' <span class="chip lag">⊙ set</span>' : ""}${node.extrapolation && node.extrapolation.flag ? " ⚠" : ""}</td>
        <td class="num">${fmt(node.baseline)} → ${fmt(node.simulated)}</td>
        <td class="num">${signedPct(node.relative_delta)}</td>
        <td class="num">[${fmt(ci[0])}, ${fmt(ci[1])}]</td>
        <td class="num">${node.prob_direction == null ? "—" : pct(node.prob_direction)}</td>
      </tr>`;
    })
    .join("");

  const warnings = (res.warnings || [])
    .map((wn) => `<div class="wf-warning">⚠ ${esc(wn.detail)}</div>`)
    .join("");

  return `
    <section>
      <div class="wf-results-head">
        <h3>Simulated outcome</h3>
        <button id="wf-clear-result" title="Dismiss this result and its canvas overlay — the scenario stays for tweaking and re-running">Clear simulation</button>
      </div>
      ${cards || '<p class="placeholder">No downstream outcome affected.</p>'}
    </section>
    <section>
      <h3>All affected metrics</h3>
      <table class="data-table">
        <tr><th>Metric</th><th class="num">baseline → simulated</th><th class="num">Δ%</th><th class="num">95% CI (Δ)</th><th class="num">P(dir)</th></tr>
        ${rows}
      </table>
    </section>
    ${warnings ? `<section><h3>Warnings</h3>${warnings}</section>` : ""}
    <div class="wf-caveats">${(res.caveats || []).map((c) => esc(c)).join("<br>")}</div>`;
}

function waterfallHtml(name, node, labelFor) {
  const contribs = node.contributions || [];
  if (!contribs.length) return "";
  const maxAbs = Math.max(...contribs.map((c) => Math.abs(c.estimate)), 1e-9);
  const rows = contribs
    .map((c) => {
      const width = (50 * Math.abs(c.estimate)) / maxAbs;
      const cls = goodDir(name, c.estimate >= 0 ? "up" : "down");
      return `<div class="wf-bar-row">
      <span class="wf-src" title="${esc(labelFor(c.source))}">${esc(labelFor(c.source))}</span>
      <span class="wf-bar-wrap"><span class="wf-bar ${cls}" style="width:${width}%"></span></span>
      <span class="wf-amt">${fmt(c.estimate)}</span>
    </div>`;
    })
    .join("");
  return `<div style="margin-top:8px">${rows}<div class="wf-shapley-note">contributions sum exactly to the point Δ (Shapley over scenario sources)</div></div>`;
}

/* ---------- tabs & init ---------- */

function activeTab() {
  const el = document.querySelector(".tab.active");
  return el ? el.dataset.tab : "metric";
}

function switchTab(tab) {
  const el = document.querySelector(`.tab[data-tab="${tab}"]`);
  if (el && el.classList.contains("disabled")) return; // cold start: RCA is inert
  const prev = activeTab();
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-content").forEach((c) => c.classList.toggle("active", c.id === `tab-${tab}`));
  if (tab === prev) return;
  // overlay exclusivity: RCA and what-if each own the canvas while active;
  // the Metric tab keeps whichever overlay was last showing.
  if (tab === "rca") {
    clearOverlays();
    if (state.rca) applyRcaOverlay();
  } else if (tab === "whatif") {
    clearOverlays();
    if (state.whatif.result) applyWhatifOverlay();
  }
}

function initControls() {
  const select = $("target-select");
  state.meta.metrics.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    select.appendChild(opt);
  });
  // sensible default target: last metric in the tree (usually the top KPI)
  select.value = state.meta.metrics[state.meta.metrics.length - 1];

  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  if (coldStart()) {
    // No data → no windows, no as-of anchor, no RCA. The Root cause tab
    // stays visible but inert, saying why — a stated mode, not a bug.
    const rcaTab = document.querySelector('.tab[data-tab="rca"]');
    if (rcaTab) {
      rcaTab.classList.add("disabled");
      rcaTab.title = "Root cause analysis needs time-series data — this tree declares none (cold start).";
    }
    const asofGroup = $("card-asof") && $("card-asof").closest(".control-group");
    if (asofGroup) asofGroup.style.display = "none";
    initShareMenu();
    return;
  }

  // window presets: populate the select with those the data window supports
  const presetSelect = $("win-preset");
  const start = new Date(state.meta.date_start);
  const end = new Date(state.meta.date_end);
  const available = new Set();
  WINDOW_PRESETS.forEach((p) => {
    if (p.id !== "custom" && !p.compute(start, end)) return; // omit too-short presets
    available.add(p.id);
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.label;
    presetSelect.appendChild(opt);
  });
  const defaultPreset = available.has("last7-prior28") ? "last7-prior28" : "split60";
  presetSelect.value = defaultPreset;

  // date inputs: bound to the data range; any manual edit -> Custom + revalidate
  const bounds = { min: state.meta.date_start, max: state.meta.date_end };
  ["ref-start", "ref-end", "an-start", "an-end"].forEach((id) => {
    const el = $(id);
    el.min = bounds.min;
    el.max = bounds.max;
    el.addEventListener("change", () => {
      presetSelect.value = "custom";
      validateWindows();
    });
  });

  applyPreset(defaultPreset);
  presetSelect.addEventListener("change", () => {
    applyPreset(presetSelect.value);
    validateWindows();
  });

  $("run-rca").addEventListener("click", runRCA);
  $("clear-rca").addEventListener("click", clearRCA);
  initShareMenu();
  validateWindows();
}

/* Share menu: copy the deep link, download the last RCA response. Wired on
   both boot paths (cold start skips all window/RCA controls above). */
function initShareMenu() {
  const shareMenu = $("share-menu");
  const closeShare = () => { shareMenu.style.display = "none"; };
  $("share-toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    updateShareMenu();
    renderSavedViews();
    shareMenu.style.display = shareMenu.style.display === "none" ? "" : "none";
  });
  $("share-save-view").addEventListener("click", (e) => {
    e.stopPropagation();
    beginSaveView();
  });
  document.addEventListener("click", (e) => {
    if (!$("share-wrap").contains(e.target)) closeShare();
  });
  $("share-copy-link").addEventListener("click", () => {
    navigator.clipboard.writeText(location.href);
    const title = $("share-copy-link").querySelector(".share-title");
    title.textContent = "Copied ✓";
    setTimeout(() => { title.textContent = "Copy link"; closeShare(); }, 900);
  });
  $("share-rca-report").addEventListener("click", () => {
    downloadRcaReport();
    closeShare();
  });
  $("share-rca-json").addEventListener("click", () => {
    if (!state.rca) return;
    const blob = new Blob([JSON.stringify(state.rca, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `rca_${state.rca.target}_${state.rca.analysis_window.start}_${state.rca.analysis_window.end}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    closeShare();
  });
}

/* Deep links: #metric=<name> opens a metric; #rca=<target>&reference_start=…
   (RCA query params) restores a shareable RCA run; #whatif=<json scenario>
   replays a what-if scenario in reader mode (results first, builder collapsed). */
function applyDeepLink() {
  const params = new URLSearchParams(location.hash.slice(1));
  const whatifJson = params.get("whatif");
  if (whatifJson) {
    try {
      const sc = JSON.parse(whatifJson);
      const w = state.whatif;
      w.baseline = { start: sc.baseline_start, end: sc.baseline_end };
      w.interventions = sc.interventions || [];
      w.assumptions = (sc.assumptions || []).map(({ id, ...rest }) => rest);
      w.readerMode = true;
      switchTab("whatif");
      renderWhatifTab();
      runWhatif();
      return;
    } catch { /* malformed scenario hash: fall through to other deep links */ }
  }
  const metric = params.get("metric");
  const rcaTarget = params.get("rca");
  if (rcaTarget && state.meta.metrics.includes(rcaTarget)) {
    $("target-select").value = rcaTarget;
    ["reference_start", "reference_end", "analysis_start", "analysis_end"].forEach((k, i) => {
      const v = params.get(k);
      if (v) $(["ref-start", "ref-end", "an-start", "an-end"][i]).value = v;
    });
    runRCA();
  } else if (metric && state.meta.metrics.includes(metric)) {
    selectMetric(metric);
  }
}

function showDegradedBanner(error) {
  const banner = document.createElement("div");
  banner.id = "degraded-banner";
  banner.innerHTML =
    `<strong>breakdown started without data.</strong> ${esc(error)}<br>` +
    `Fix the tree or provider config, then restart. ` +
    `Diagnose with <code>breakdown doctor --tree &lt;tree.yml&gt;</code>.`;
  document.body.prepend(banner);
}

async function init() {
  setStatus("Loading…", "busy");
  try {
    // The server keeps serving when the startup data load fails (bad
    // credentials, unreachable warehouse); surface that instead of eight
    // opaque 503s.
    //
    // /health is also the wake probe: on a host that suspends idle instances
    // this is the request that boots one, so it gets the retry treatment and
    // says so rather than failing once and leaving a blank page.
    const health = await apiWithWake("/health", {
      onWaiting: (i) =>
        i === null
          ? setStatus("Loading…", "busy")
          : setStatus("Waking the server up… (this takes a few seconds on first visit)", "busy"),
    });
    if (health.status !== "ok") {
      showDegradedBanner(health.error);
      setStatus("No data loaded", "error");
      return;
    }
    [state.meta, state.dag] = await Promise.all([api("/meta"), api("/dag")]);
    // Series hydrates every node card in one request; degrade to name-only
    // nodes if it fails rather than blocking the whole graph. A cold-start
    // tree has no series at all — cards render from declared baselines.
    if (!coldStart()) {
      try {
        state.series = await api("/series");
      } catch (err) {
        state.series = null;
        console.warn("card series unavailable:", err.message);
      }
    }
    // Anchor headlines at the tree-wide data edge: the oldest data_through
    // across metrics. A source mart lagging the requested window then shows
    // its true last day instead of a zero-filled or half-loaded tail.
    const edges = Object.values(state.meta.data_through || {});
    state.asOf = edges.length ? edges.reduce((a, b) => (a < b ? a : b)) : state.meta.date_end;
    loadCardConfig();
    initControls();
    buildGraph();
    initCardControls();
    initWhatif();
    if (coldStart()) {
      // What-if is the only analysis a dataless tree supports — boot into it.
      setStatus(`${state.meta.metrics.length} metrics · cold start — declared beliefs, no data provider`);
      switchTab("whatif");
    } else {
      const edgeNote = state.asOf < state.meta.date_end ? ` · data → ${state.asOf}` : "";
      setStatus(`${state.meta.metrics.length} metrics · provider: ${state.meta.provider} · ${state.meta.date_start} → ${state.meta.date_end}${edgeNote}`);
    }
    applyDeepLink();
    updateShareMenu();
  } catch (err) {
    // Distinguish "could not reach the server" from "the server answered with
    // a problem": the first is worth another click, the second is not.
    if (isTransient(err)) {
      setStatus("Could not reach the server — it may still be starting.", "error");
      showRetryBanner();
    } else {
      setStatus(`Failed to load: ${err.message}`, "error");
    }
  }
}

function showRetryBanner() {
  if ($("retry-banner")) return;
  const banner = document.createElement("div");
  banner.id = "retry-banner";
  banner.innerHTML =
    `<strong>Can't reach the server.</strong> If this is the hosted demo it may ` +
    `be waking from idle. <button id="retry-now" type="button">Try again</button>`;
  document.body.prepend(banner);
  $("retry-now").addEventListener("click", () => {
    banner.remove();
    init();
  });
}

init();

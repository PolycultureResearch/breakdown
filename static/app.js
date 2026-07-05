/* breakdown UI — metric tree explorer + root cause analysis.
   Vanilla JS over Cytoscape (graph), dagre (layout), Plotly (time series).
   Design doc: docs/ai-context/frontend-ui.md */

const state = {
  meta: null,          // GET /meta
  dag: null,           // GET /dag
  cy: null,
  selected: null,      // selected metric name
  metricCache: {},     // name -> GET /metrics/{name} response
  rca: null,           // last POST /rca response
  activeCause: null,   // highlighted ranked cause
};

const $ = (id) => document.getElementById(id);

/* ---------- helpers ---------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch { /* not json */ }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
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

function nodeType(def) {
  if (!def.parents || def.parents.length === 0) return "source";
  return def.formula ? "formula" : "prob";
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
    style: { "line-style": "solid", "line-color": "#16a34a", "target-arrow-color": "#16a34a", width: "data(w)" },
  },
  {
    selector: "edge.rca-down",
    style: { "line-style": "solid", "line-color": "#dc2626", "target-arrow-color": "#dc2626", width: "data(w)" },
  },
  { selector: ".faded", style: { opacity: 0.25 } },
  {
    selector: ".pathhl",
    style: { "border-color": "#d97706", "line-color": "#d97706", "target-arrow-color": "#d97706" },
  },
];

function buildGraph() {
  const defs = Object.fromEntries(state.dag.nodes.map(([name, def]) => [name, def]));
  const elements = [];

  state.dag.nodes.forEach(([name, def]) => {
    elements.push({
      data: { id: name, label: name },
      classes: nodeType(def),
    });
  });
  state.dag.edges.forEach(([src, dst]) => {
    const kind = defs[dst].formula ? "formula" : "prob";
    elements.push({
      data: { id: `${src}->${dst}`, source: src, target: dst, label: "", w: 2 },
      classes: kind,
    });
  });

  state.cy = cytoscape({
    container: $("cy"),
    elements,
    style: CY_STYLE,
    layout: { name: "dagre", rankDir: "BT", nodeSep: 40, rankSep: 70, padding: 24 },
    wheelSensitivity: 0.2,
  });

  state.cy.on("tap", "node", (evt) => selectMetric(evt.target.id()));
  markFitted();
}

function markFitted() {
  state.meta.fitted.forEach((name) => {
    const n = state.cy.getElementById(name);
    if (n) n.addClass("fitted");
  });
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

  const parentChips = (def.parents || [])
    .map((p) => {
      const lag = def.lags && def.lags[p];
      return `<code>${esc(p)}</code>${lag ? ` <span class="chip lag">lag ${lag}d</span>` : ""}`;
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
      <tr><td>Source</td><td><code>${esc(def.source)}</code></td></tr>
      <tr><td>Parents</td><td>${parentChips}</td></tr>
      ${def.formula ? `<tr><td>Formula</td><td><code>${esc(def.formula)}</code></td></tr>` : ""}
    </table>

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

  renderTimeSeries(name, data.time_series);
  renderPosterior(name, data);
  $("an-run").addEventListener("click", () => runAnalyze(name));
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
  const win = readWindows();
  if (!win) {
    setStatus("Set all four window dates first.", "error");
    return;
  }
  const btn = $("run-rca");
  btn.disabled = true;
  setStatus("Running RCA — fitting upstream models where needed…", "busy");
  try {
    const qs = new URLSearchParams(win).toString();
    state.rca = await api(`/rca/${encodeURIComponent(target)}?${qs}`, { method: "POST" });
    history.replaceState(null, "", `#rca=${encodeURIComponent(target)}&${qs}`);
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
  clearRcaStyles();

  const inScope = new Set(Object.keys(res.nodes));
  cy.nodes().forEach((n) => {
    if (!inScope.has(n.id())) n.addClass("faded");
  });
  cy.edges().forEach((e) => {
    if (!inScope.has(e.source().id()) || !inScope.has(e.target().id())) e.addClass("faded");
  });

  Object.entries(res.nodes).forEach(([name, node]) => {
    const n = cy.getElementById(name);
    const dir = node.gap >= 0 ? "rca-up" : "rca-down";
    n.addClass(dir);
    if (node.relative_change !== null) {
      n.data("label", `${name}\n${signedPct(node.relative_change)}`);
    }
    node.contributions.forEach((c) => {
      const e = cy.getElementById(`${c.parent}->${name}`);
      if (!e.length) return;
      const share = c.share_of_gap === null ? 0 : Math.min(Math.abs(c.share_of_gap), 1);
      e.data("w", 2 + 6 * share);
      e.data("label", c.share_of_gap === null ? "" : pct(Math.abs(c.share_of_gap)));
      e.addClass(c.estimate >= 0 ? "rca-up" : "rca-down");
    });
  });

  document.querySelectorAll(".rca-only").forEach((el) => (el.style.display = "flex"));
}

function clearRcaStyles() {
  const cy = state.cy;
  cy.elements().removeClass("faded rca-up rca-down pathhl");
  cy.nodes().forEach((n) => n.data("label", n.id()));
  cy.edges().forEach((e) => {
    e.data("w", 2);
    e.data("label", "");
  });
  document.querySelectorAll(".rca-only").forEach((el) => (el.style.display = "none"));
}

function clearRCA() {
  state.rca = null;
  state.activeCause = null;
  clearRcaStyles();
  // restore beta labels on fitted nodes we have cached data for
  Object.entries(state.metricCache).forEach(([name, data]) => labelBetaEdges(name, data));
  $("tab-rca").innerHTML = '<p class="placeholder">Pick a target and two windows above, then Run RCA.</p>';
  $("clear-rca").style.display = "none";
  setStatus("");
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

function renderRcaTab() {
  const res = state.rca;
  const target = res.nodes[res.target];
  const dirCls = target.gap >= 0 ? "up" : "down";

  const maxScore = Math.max(...res.ranked_causes.map((c) => c.score), 1e-9);
  const causeRows = res.ranked_causes
    .map(
      (c, i) => `
      <div class="cause-row" data-metric="${esc(c.metric)}">
        <span class="cause-rank">${i + 1}</span>
        <span class="cause-name">${esc(c.metric)}</span>
        <span class="cause-bar-wrap"><span class="cause-bar" style="width:${(100 * c.score) / maxScore}%"></span></span>
        <span class="cause-via">via ${esc(c.via || "—")}</span>
      </div>`,
    )
    .join("");

  // attribution detail: target first, then ranked order
  const order = [res.target, ...res.ranked_causes.map((c) => c.metric)];
  const blocks = order
    .filter((name) => res.nodes[name] && res.nodes[name].contributions.length)
    .map((name) => {
      const node = res.nodes[name];
      const method = node.attribution_method === "shapley" ? "Shapley (exact)" : "posterior";
      const rows = node.contributions
        .map(
          (c) => `<tr>
            <td><code>${esc(c.parent)}</code></td>
            <td class="num">${fmt(c.estimate)}</td>
            <td class="num">${c.share_of_gap === null ? "—" : pct(c.share_of_gap)}</td>
            <td class="num">${c.ci_95 ? `[${fmt(c.ci_95[0])}, ${fmt(c.ci_95[1])}]` : "exact"}</td>
            <td class="num">${c.prob_same_direction === null ? "—" : pct(c.prob_same_direction)}</td>
          </tr>`,
        )
        .join("");
      const unexplained =
        node.unexplained !== null
          ? `<tr class="dim"><td>unexplained</td><td class="num">${fmt(node.unexplained)}</td><td class="num">${
              Math.abs(node.gap) > 1e-12 ? pct(node.unexplained / node.gap) : "—"
            }</td><td class="num">—</td><td class="num">—</td></tr>`
          : "";
      return `
        <div class="attr-block">
          <h4>${esc(name)} <span class="method">· ${method}</span></h4>
          <table class="data-table">
            <tr><th>Parent</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>
            ${rows}
            ${unexplained}
          </table>
        </div>`;
    })
    .join("");

  $("tab-rca").innerHTML = `
    <div class="rca-card">
      <div class="sub">${esc(res.target)} · ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)} vs ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</div>
      <div class="gap-line ${dirCls}">${target.gap >= 0 ? "+" : ""}${fmt(target.gap)} <span style="font-size:14px">(${signedPct(target.relative_change)})</span></div>
      <div class="sub">${fmt(target.baseline)} → ${fmt(target.actual)} (window means)</div>
    </div>

    <section>
      <h3>Ranked causes</h3>
      ${causeRows || '<p class="placeholder">No upstream causes — target is a source metric.</p>'}
    </section>

    <section>
      <h3>Attribution detail</h3>
      ${blocks || '<p class="placeholder">No attributable edges in scope.</p>'}
    </section>`;

  document.querySelectorAll(".cause-row").forEach((row) => {
    row.addEventListener("click", () => highlightCause(row.dataset.metric));
  });
}

/* ---------- tabs & init ---------- */

function switchTab(tab) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-content").forEach((c) => c.classList.toggle("active", c.id === `tab-${tab}`));
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

  // default windows: first 60% reference, rest analysis
  const start = new Date(state.meta.date_start);
  const end = new Date(state.meta.date_end);
  const splitMs = start.getTime() + 0.6 * (end.getTime() - start.getTime());
  const split = new Date(splitMs);
  const dayAfter = new Date(splitMs + 86400000);
  const iso = (d) => d.toISOString().slice(0, 10);

  const bounds = { min: state.meta.date_start, max: state.meta.date_end };
  [["ref-start", iso(start)], ["ref-end", iso(split)], ["an-start", iso(dayAfter)], ["an-end", iso(end)]].forEach(
    ([id, val]) => {
      const el = $(id);
      el.value = val;
      el.min = bounds.min;
      el.max = bounds.max;
    },
  );

  $("run-rca").addEventListener("click", runRCA);
  $("clear-rca").addEventListener("click", clearRCA);
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
}

/* Deep links: #metric=<name> opens a metric; #rca=<target>&reference_start=…
   (RCA query params) restores a shareable RCA run. */
function applyDeepLink() {
  const params = new URLSearchParams(location.hash.slice(1));
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

async function init() {
  setStatus("Loading…", "busy");
  try {
    [state.meta, state.dag] = await Promise.all([api("/meta"), api("/dag")]);
    initControls();
    buildGraph();
    setStatus(`${state.meta.metrics.length} metrics · provider: ${state.meta.provider} · ${state.meta.date_start} → ${state.meta.date_end}`);
    applyDeepLink();
  } catch (err) {
    setStatus(`Failed to load: ${err.message}`, "error");
  }
}

init();

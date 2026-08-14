/* breakdown UI — metric tree explorer + root cause analysis.
   Vanilla JS over Cytoscape (graph), dagre (layout), Plotly (time series).
   Design doc: docs/ai-context/frontend-ui.md */

const state = {
  tree: null,          // open tree id — every data request is scoped to it
  trees: [],           // GET /trees -> the index (parsed YAML only, never a load)
  defaultTree: null,   // which tree the unprefixed routes and a bare /ui mean
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
  refMode: "auto",     // "auto": server picks the reference (matched adjacent block); "custom": user-typed
  rcaView: "headline", // formula-node attribution view: "headline" | "detailed"
  winAdvisories: [],   // window caveats from validateWindows — shown with the RESULT, not only before it
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

/* Color lives in style.css and nowhere else. The Cytoscape stylesheet, the
   node-card SVGs and the Plotly charts all need real hex strings rather than
   `var(--x)`, so they read the :root custom properties once at load instead of
   repeating literals — there were 74 of them before 2026-08-11, which is why
   retuning the palette used to mean a sweep through this file. The stylesheet
   is a render-blocking <link> in <head>, so the values are resolved by the
   time this module runs. Add a token to :root, add a line here. */
const COL = (() => {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    bg: v("--bg"), panel: v("--panel"), border: v("--border"), rule: v("--rule"),
    text: v("--text"), text2: v("--text-2"), muted: v("--muted"), faint: v("--faint"),
    source: v("--source"),
    accent: v("--accent"), accentSoft: v("--accent-soft"), edgeProb: v("--edge-prob"),
    formula: v("--formula"), edgeFormula: v("--edge-formula"),
    up: v("--up"), upSoft: v("--up-soft"),
    down: v("--down"), downSoft: v("--down-soft"),
    warn: v("--warn"), warnSoft: v("--warn-soft"), warnInk: v("--warn-ink"),
    track: v("--track"), trackStrong: v("--track-strong"),
    chipBg: v("--chip-bg"), warnBorder: v("--warn-border"),
  };
})();

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
    const p = await api(treePath(`/metrics/${encodeURIComponent(btn.dataset.query)}/query`));
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

/* Every data request names its tree. `/trees/<id>/meta` and `/meta` are the
   same endpoint — the bare path aliases the default tree — but naming it means
   two tabs open on two trees never race each other through one alias, and a
   URL in a server log says which tree it was. `/progress/{run_id}` and
   `/trees` are deliberately *not* scoped: run ids are already unique, and the
   index is about all of them. */
function treePath(path) {
  return state.tree ? `/trees/${encodeURIComponent(state.tree)}${path}` : path;
}

/* `#tree=<id>` leads every hash once there is more than one tree, because
   `#metric=`/`#rca=`/`#whatif=` are meaningless without knowing whose metric
   names they refer to. With one tree it is omitted, so links minted by a
   single-tree instance stay exactly as short as they were. */
function hashPrefix() {
  return state.trees.length > 1 && state.tree
    ? `tree=${encodeURIComponent(state.tree)}&`
    : "";
}

function setHash(rest) {
  history.replaceState(null, "", `#${hashPrefix()}${rest}`);
}

function hashParams() {
  return new URLSearchParams(location.hash.slice(1));
}

async function api(path, opts) {
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (netErr) {
    // A rejected fetch is the only thing that genuinely means "the connection
    // dropped". Tag it, because `err.status === undefined` is *also* true of
    // every ReferenceError and TypeError thrown by our own rendering code —
    // which is how a CDN-blocked library used to be reported as an unreachable
    // server. See `isTransient`.
    const err = new Error(netErr.message || "Network request failed");
    err.network = true;
    throw err;
  }
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
  // Only two things are transient: a rejected fetch (tagged `network` by
  // `api()`) and a gateway status from the edge. This used to read
  // `err.status === undefined`, which is true of *every* error that never
  // touched the network — a ReferenceError from a CDN-blocked `cytoscape`
  // included. That reported a healthy server as unreachable and offered a
  // Try again button that could never succeed. An error we did not raise
  // ourselves is a bug or a missing dependency, and must say so.
  return err.network === true || WAKE_STATUSES.has(err.status);
}

/* The four CDN libraries this page needs, checked before anything is drawn.
   A corporate proxy, an ad blocker, an offline laptop or a failed
   Subresource Integrity check all produce the same symptom — the global is
   simply absent — and the failure surfaces much later as a ReferenceError
   inside rendering code. Naming the missing library and its host is the
   difference between a five-second diagnosis and blaming the server. */
const CDN_LIBS = [
  { global: "cytoscape", name: "Cytoscape.js", host: "cdnjs.cloudflare.com" },
  { global: "dagre", name: "dagre", host: "cdnjs.cloudflare.com" },
  { global: "Plotly", name: "Plotly.js", host: "cdn.plot.ly" },
];

function missingLibs() {
  return CDN_LIBS.filter((l) => typeof window[l.global] === "undefined");
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
  // NaN and ±Infinity fall through every branch below and render as literal
  // "NaN" / "Infinity". They mean the same thing a null does here — no number
  // to show — so they get the same em dash.
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  const ax = Math.abs(x);
  if (ax >= 10000) return x.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (ax >= 100) return x.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (ax >= 1) return x.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return x.toPrecision(3);
}

function pct(x) {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return (x * 100).toFixed(1) + "%";
}

/* A direction probability, printed at the resolution it actually has.

   Every one of them (`prob_same_direction`, `prob_concentrated`,
   `prob_direction`) is a proportion over a finite sample, so it can only take
   the values k/n — with 500 bootstrap replicates there is nothing between
   0.998 and 1. When the count saturates, the engine publishes the ceiling and
   sets the matching `*_censored` flag, and this prints it as the bound it is:
   `>99.8%`. Printing the bare ceiling would read as a measured value, and
   printing 100.0% (what the engine used to publish) read as certainty on the
   contributions where the evidence is thinnest. A withheld probability stays
   an em dash — see the edge-opacity note in `applyRcaOverlay`. */
function pctDir(p, censored) {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  // Exactly 1 is reachable only where the sample has no spread at all — a
  // deterministic propagation in what-if, where the sign really is known.
  if (p >= 1) return "100.0%";
  // Truncate rather than round: `(0.9995).toFixed(1)` is "100.0", so a
  // one-decimal rounding would put the ceiling back to certainty, which is the
  // defect this function exists to fix.
  return (censored ? ">" : "") + (Math.floor(p * 1000) / 10).toFixed(1) + "%";
}

/* ---------- Lagged contributions say so, on every surface ----------

   A lagged contribution was measured over the *parent's* own windows, shifted
   back by the lag — not the windows the section header names. The engine has
   always published `lag` and `parent_windows` on those contributions, and both
   attribution tables rendered the parent as a bare name, so a row measured over
   a different fortnight sat in a table headed by the analysis window with
   nothing to distinguish it. Lagged edges are the product's signature feature;
   they cannot be the one thing the tables leave out. */
function lagUnitLabel(lag, grain) {
  const g = grain || "day";
  return `${lag} ${g}${lag === 1 ? "" : "s"}`;
}

/* The full sentence — used verbatim where there is nothing to hover: the
   exported report, and the live chip's tooltip. */
function lagWindowText(c, grain) {
  if (!c || !c.lag) return "";
  const head = `measured over the parent's own windows, shifted back ${lagUnitLabel(c.lag, grain)}`;
  const w = c.parent_windows;
  return w
    ? `${head}: reference ${w.reference.start} → ${w.reference.end}, analysis ${w.analysis.start} → ${w.analysis.end}`
    : head;
}

/* Live tables: a chip beside the parent name, dates in the tooltip. */
function lagChip(c, grain) {
  if (!c || !c.lag) return "";
  return ` <span class="chip lag" title="${esc(lagWindowText(c, grain))}">lag ${lagUnitLabel(c.lag, grain)}</span>`;
}

/* Exported report: the same fact printed in full. A tooltip is simply absent
   from a static report, which is exactly where the reader cannot ask. */
function lagLine(c, grain) {
  if (!c || !c.lag) return "";
  return `<div class="meta">↩ ${esc(lagWindowText(c, grain))}</div>`;
}

/* A withheld percentage is an em dash, not an empty string. The engine nulls
   `relative_change` / `relative_delta` when the baseline is ~0 — a real
   "cannot be expressed as a percentage" — and rendering that as nothing left
   `(-3.06 ())` in the headline and blank cells in the Δ% column, both of which
   read as "nothing to report" rather than "not defined here". */
function signedPct(x) {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return (x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%";
}

/* Two header slots, deliberately separate. `#status` is transient — run
   progress, errors, window advisories — and `#context` is ambient: which tree
   is loaded, from where, over what window. They shared one element until
   2026-08-11, so finishing an RCA overwrote the tree context for the rest of
   the session. `ttlMs` clears a message on its own; use it for success notices,
   never for errors or advisories (those stand until something replaces them). */
let statusTimer = null;

function setStatus(msg, kind = "", ttlMs = 0) {
  const el = $("status");
  clearTimeout(statusTimer);
  el.textContent = msg;
  el.className = kind; // "", "busy", "error"
  if (msg && ttlMs) statusTimer = setTimeout(() => setStatus(""), ttlMs);
}

/* chips: [{text, title?, cls?}] — `cls: "warn"` for anything the reader should
   not take at face value (a lagging data edge, a tree with no provider). */
function setContext(chips) {
  $("context").innerHTML = chips
    .map(
      (c) =>
        `<span class="ctx-chip${c.cls ? " " + c.cls : ""}"` +
        `${c.title ? ` title="${esc(c.title)}"` : ""}>${esc(c.text)}</span>`,
    )
    .join("");
}

/* ---------- run progress ----------

   A spinner reading "Simulating…" for ninety seconds tells the operator
   nothing about whether the thing is working or wedged. The server now reports
   real stages (GET /progress/{run_id}), so this shows what is actually
   happening: which ancestor model is being fitted, how many are left, and how
   long it has been going.

   The copy rotates, in the spirit of Claude Code's "percolating" — but every
   phrase here is *literally true of the stage it belongs to*. `descending the
   ELBO` is what ADVI does; `permuting coalitions` is what exact Shapley
   attribution does. The joke and the status are the same string, so a curious
   reader picks up how the engine works instead of watching a lie. Keep it that
   way: if a phrase moves to a stage where it stops being true, it is no longer
   funny, just wrong. */
const PROGRESS_PHRASES = {
  waiting: ["queued behind another run"],
  resolving: ["snapping windows to grain", "resolving the reference window", "reading the tree"],
  // Every on-demand fit is ADVI, so the vocabulary is variational, not MCMC.
  fitting: [
    "descending the ELBO",
    "fitting the mean field",
    "tuning the approximation",
    "letting the posterior settle",
  ],
  attributing: [
    "permuting coalitions",
    "computing Shapley values",
    "bridging window means",
    "bootstrapping the intervals",
    "walking the DAG",
  ],
  simulating: [
    "applying the do-operator",
    "propagating through the DAG",
    "drawing from the posterior",
  ],
};

const PHRASE_MS = 2400;
const POLL_MS = 400;

const runProg = {
  id: null, poll: null, tick: null, started: 0,
  stage: null, phraseIx: 0, last: {}, pulsing: null,
};

const mmss = (ms) => {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

function paintProgress() {
  const p = runProg.last || {};
  const stage = p.stage || runProg.stage || "resolving";
  const list = PROGRESS_PHRASES[stage] || PROGRESS_PHRASES.resolving;
  const phrase = list[runProg.phraseIx % list.length];
  const bits = [phrase];
  if (p.metric) {
    bits.push(p.total > 1 ? `${p.metric} (${p.current}/${p.total})` : String(p.metric));
  }
  bits.push(mmss(Date.now() - runProg.started));
  setStatus(bits.join(" · "), "busy");
}

/* Mark the node currently being fitted so the tree shows the engine walking up
   it. This is the honest version of a progress bar: the position is real. */
function pulseFitting(name) {
  if (runProg.pulsing === name || !state.cy) return;
  if (runProg.pulsing) state.cy.getElementById(runProg.pulsing).removeClass("fitting-now");
  runProg.pulsing = name;
  if (name) {
    const n = state.cy.getElementById(name);
    if (n && n.length) n.addClass("fitting-now");
  }
}

/* Returns the run id to pass to the server. Polling and the phrase timer are
   independent: the phrase rotates on its own clock so the line stays alive
   even while a single slow fit holds one stage for a minute. */
function startRunProgress(initialStage) {
  stopRunProgress();
  runProg.id = `r${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  runProg.started = Date.now();
  runProg.stage = initialStage;
  runProg.phraseIx = 0;
  runProg.last = { stage: initialStage };
  paintProgress();
  runProg.tick = setInterval(() => {
    runProg.phraseIx++;
    paintProgress();
  }, PHRASE_MS);
  runProg.poll = setInterval(async () => {
    const id = runProg.id;
    try {
      const p = await api(`/progress/${encodeURIComponent(id)}`);
      if (runProg.id !== id || !p || !p.stage) return; // superseded or finished
      // A stage change restarts the vocabulary rather than continuing mid-list,
      // so the first phrase a reader sees names the phase they just entered.
      if (p.stage !== runProg.last.stage) runProg.phraseIx = 0;
      runProg.last = p;
      pulseFitting(p.stage === "fitting" ? p.metric : null);
      paintProgress();
    } catch { /* progress is advisory — a failed poll must not surface */ }
  }, POLL_MS);
  return runProg.id;
}

function stopRunProgress() {
  clearInterval(runProg.poll);
  clearInterval(runProg.tick);
  pulseFitting(null);
  runProg.id = null;
  runProg.poll = runProg.tick = null;
  runProg.last = {};
}

/* ---------- inline help ----------

   breakdown asks a business user to read posteriors, so the UI has to teach a
   little as it goes rather than assume the vocabulary. Two mechanisms, and the
   split matters: a **control note** is always visible and says what the current
   setting concretely does ("500 × 4 chains = 2,000 draws"); a **hint** is the ⓘ
   the reader opens when they want the why. Explanations live here, but anything
   with real depth links to docs/model.md rather than forking its prose — the
   doc is the thing kept true.

   `body` may be a function so a hint can read the current control state; that
   is why `draws` can describe two genuinely different meanings. */
const DOC = "https://github.com/PolycultureResearch/breakdown/blob/main/docs/model.md";

const HINTS = {
  method: {
    title: "ADVI vs NUTS",
    body: () => `
      <p><strong>NUTS</strong> is exact MCMC: it walks the posterior and, given
      enough draws, converges on the true shape. It is the slower one — a minute
      or more per metric is normal — and it is the only method that reports
      convergence diagnostics (R̂, divergences, effective sample size).</p>
      <p><strong>ADVI</strong> fits a simpler stand-in distribution by
      optimization. Seconds instead of minutes, but it is an approximation:
      mean-field ADVI assumes the parameters are independent, so it tends to
      <em>understate</em> uncertainty — intervals come out too narrow.</p>
      <p>Explore with ADVI; quote NUTS.</p>`,
  },
  draws: {
    title: "What the draws number does",
    body: () => {
      const nuts = $("an-method") && $("an-method").value === "nuts";
      return nuts
        ? `<p>Posterior draws to keep <strong>per chain</strong>, after 1,000
           tuning steps that are discarded. The engine runs 4 chains, so 500
           here means 2,000 draws behind every interval you see.</p>
           <p>More draws mean less Monte-Carlo noise in the estimate — the
           interval settles rather than shifts between runs. Cost is linear:
           double the draws, double the wait. If R̂ says the fit converged, more
           draws refine it; they cannot repair a fit that did not.</p>`
        : `<p>Samples taken <strong>from the already-fitted approximation</strong>.
           The ADVI optimization itself is a fixed 20,000 steps and this number
           does not change it.</p>
           <p>So raising it here does <strong>not</strong> make the answer more
           accurate — it only smooths the summary statistics, and it is nearly
           free. If you want a better answer rather than a smoother one, switch
           to NUTS.</p>`;
    },
  },
  beta: {
    title: "Reading a coefficient",
    body: () => `
      <p>β is in <strong>business units</strong>: holding the other parents
      fixed, a one-unit rise in the parent moves this metric by about β.</p>
      <p>It is a posterior mean, not a measurement — the interval beside it is
      the honest part of the answer.</p>`,
    more: `${DOC}#reading-coefficients-beta-vs-beta_raw`,
  },
  hdi: {
    title: "Reading a 95% interval",
    body: () => `
      <p>Given the model and the data, 95% of the posterior for this parameter
      lies inside this interval — the plain-English reading a confidence
      interval famously does not permit.</p>
      <p>If the interval <strong>straddles zero</strong>, the data has not
      resolved even the direction of the effect. Treat that parent as unproven
      rather than as having no effect.</p>`,
  },
  diagnostics: {
    title: "Is this fit trustworthy?",
    body: () => `
      <p><strong>R̂</strong> compares the chains against each other. At 1.00 they
      agree; the engine flags anything above 1.05. Below ~1.01 is healthy.</p>
      <p><strong>Divergences</strong> are steps the sampler could not take
      accurately — a handful is tolerable, and the engine flags more than 1% of
      draws. <strong>ESS</strong> is how many independent draws the correlated
      ones are worth; under 100 gets flagged.</p>
      <p>These are MCMC diagnostics, so an ADVI fit has none. That is not a
      clean bill of health — it is the absence of a check.</p>`,
    more: `${DOC}#what-gets-fitted`,
  },
};

/* The ⓘ button. Pair each with a `hintSlot(id)` where the panel should open —
   explicit placement beats guessing at a container from the DOM. */
function hintHTML(id) {
  return `<button type="button" class="hint" data-hint="${id}" aria-expanded="false"
    title="${esc(HINTS[id].title)}" aria-label="${esc(HINTS[id].title)}">ⓘ</button>`;
}

function hintSlot(id) {
  return `<div class="hint-slot" data-hint-slot="${id}"></div>`;
}

/* One delegated listener for every hint in the app, bound once — the sidebar
   tabs are re-rendered wholesale, so per-button listeners would leak on every
   node click. */
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".hint");
  if (!btn) return;
  const id = btn.dataset.hint;
  const slot = document.querySelector(`[data-hint-slot="${id}"]`);
  if (!slot || !HINTS[id]) return;
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", open ? "false" : "true");
  if (open) {
    slot.innerHTML = "";
    return;
  }
  const h = HINTS[id];
  const more = h.more
    ? `<p><a href="${h.more}" target="_blank" rel="noopener">Read more in the model doc →</a></p>`
    : "";
  slot.innerHTML = `<div class="hint-panel">${h.body()}${more}</div>`;
});

/* ---------- date / window helpers (UTC to match ISO strings) ---------- */

const DAY_MS = 86400000;
const isoUTC = (d) => d.toISOString().slice(0, 10);
const addDays = (d, n) => new Date(d.getTime() + n * DAY_MS);
const daysInclusive = (start, end) => Math.round((end - start) / DAY_MS) + 1;

/* Analysis-window presets. compute(startDate, endDate) -> {anStart, anEnd}
   as ISO strings, or null when the data window is too short (preset omitted).
   The reference is not part of a preset: in auto mode it tracks the analysis
   window (the server's matched adjacent block, mirrored by
   defaultReferenceJS), and a hand-edited reference is "custom". Each preset
   needs at least one day before the analysis for a reference to exist. */
const WINDOW_PRESETS = [
  {
    id: "last7",
    label: "Last 7 days",
    compute(start, end) {
      const anStart = addDays(end, -6);
      if (daysInclusive(start, end) < 8) return null;
      return { anStart: isoUTC(anStart), anEnd: isoUTC(end) };
    },
  },
  {
    id: "last14",
    label: "Last 14 days",
    compute(start, end) {
      const anStart = addDays(end, -13);
      if (daysInclusive(start, end) < 15) return null;
      return { anStart: isoUTC(anStart), anEnd: isoUTC(end) };
    },
  },
  {
    id: "last-full-week",
    label: "Last full week (Mon–Sun)",
    compute(start, end) {
      const lastSunday = addDays(end, -end.getUTCDay()); // getUTCDay: Sun=0
      const anStart = addDays(lastSunday, -6); // Monday
      if (anStart <= start) return null; // needs >= 1 day of history before it
      return { anStart: isoUTC(anStart), anEnd: isoUTC(lastSunday) };
    },
  },
  { id: "custom", label: "Custom", compute: null },
];

function applyPreset(id) {
  const preset = WINDOW_PRESETS.find((p) => p.id === id);
  if (!preset || !preset.compute) return; // custom: leave inputs untouched
  const w = preset.compute(new Date(state.meta.date_start), new Date(state.meta.date_end));
  if (!w) return;
  $("an-start").value = w.anStart;
  $("an-end").value = w.anEnd;
  applyAutoReference();
}

/* ---------- default reference window (display mirror) ----------
   Mirrors the engine's `default_reference_window` (grains.py) so the inputs
   show what the server will use. Display-only: in auto mode the request
   OMITS the reference params — the server's computation is the number of
   record, and the response overwrites these inputs. */

function ancestorScope(target) {
  // target + all its ancestors, walked over the DAG edges.
  const parentsOf = {};
  (state.dag.edges || []).forEach(([p, c]) => {
    (parentsOf[c] = parentsOf[c] || []).push(p);
  });
  const scope = new Set([target]);
  const queue = [target];
  while (queue.length) {
    for (const p of parentsOf[queue.pop()] || []) {
      if (!scope.has(p)) {
        scope.add(p);
        queue.push(p);
      }
    }
  }
  return scope;
}

function referenceAlignment(target) {
  const scope = ancestorScope(target);
  let weekAlign = false;
  let coarsest = "day";
  const order = { day: 0, week: 1, month: 2 };
  (state.dag.nodes || []).forEach(([name, def]) => {
    if (!scope.has(name)) return;
    if (def.seasonality && def.seasonality.length) weekAlign = true;
    const g = (state.meta.grains || {})[name] || "day";
    if (order[g] > order[coarsest]) coarsest = g;
  });
  return { weekAlign, coarsest };
}

function defaultReferenceJS(anStart, anEnd, { weekAlign, coarsest }) {
  const dataStart = new Date(state.meta.date_start);
  const anS = new Date(anStart);
  const refEnd = addDays(anS, -1);
  if (refEnd < dataStart) return null; // no room: the server will 422 too
  let length = Math.max(4 * daysInclusive(anS, new Date(anEnd)), 28);
  if (weekAlign) length = Math.ceil(length / 7) * 7;
  let refStart = addDays(refEnd, -(length - 1));
  if (refStart < dataStart) {
    refStart = dataStart;
    if (weekAlign) {
      const avail = daysInclusive(refStart, refEnd);
      if (avail >= 7) refStart = addDays(refEnd, -(7 * Math.floor(avail / 7) - 1));
    }
  }
  // Coarse-grain extension (month/week trees): reach back to cover the last
  // whole coarse period ending on/before refEnd, when the data allows it.
  if (coarsest !== "day") {
    const needsWhole =
      coarsest === "week"
        ? !wholeWeekInside(refStart, refEnd)
        : !wholeMonthInside(refStart, refEnd);
    if (needsWhole) {
      const last = lastWholePeriodStart(refEnd, coarsest);
      if (last && last >= dataStart && last < refStart) refStart = last;
    }
  }
  return { refStart: isoUTC(refStart), refEnd: isoUTC(refEnd) };
}

function wholeWeekInside(start, end) {
  // first Monday on/after start, plus 6 days, must be on/before end
  const firstMonday = addDays(start, (8 - start.getUTCDay()) % 7);
  return addDays(firstMonday, 6) <= end;
}

function wholeMonthInside(start, end) {
  const firstOfNext = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + (start.getUTCDate() === 1 ? 0 : 1), 1));
  const monthEnd = new Date(Date.UTC(firstOfNext.getUTCFullYear(), firstOfNext.getUTCMonth() + 1, 0));
  return monthEnd <= end;
}

function lastWholePeriodStart(refEnd, grain) {
  if (grain === "week") {
    const monday = addDays(refEnd, -((refEnd.getUTCDay() + 6) % 7));
    const start = addDays(monday, 6) > refEnd ? addDays(monday, -7) : monday;
    return start;
  }
  // month
  let first = new Date(Date.UTC(refEnd.getUTCFullYear(), refEnd.getUTCMonth(), 1));
  const end = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0));
  if (end > refEnd) first = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() - 1, 1));
  return first;
}

function applyAutoReference() {
  if (state.refMode !== "auto") return;
  const as = $("an-start").value, ae = $("an-end").value;
  if (!as || !ae || as > ae) return;
  const target = $("target-select").value;
  const w = defaultReferenceJS(as, ae, referenceAlignment(target));
  $("ref-start").value = w ? w.refStart : "";
  $("ref-end").value = w ? w.refEnd : "";
}

function setRefMode(mode) {
  state.refMode = mode;
  $("ref-auto").classList.toggle("active", mode === "auto");
  if (mode === "auto") applyAutoReference();
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
    state.winAdvisories = [];
    renderWindowAdvisories();
    return false;
  };

  const rs = $("ref-start").value, re = $("ref-end").value;
  const as = $("an-start").value, ae = $("an-end").value;
  if (!as || !ae) return fail(["an-start", "an-end"].filter((id) => !$(id).value), "Set the analysis window dates.");
  if (!rs || !re) {
    // Auto mode with no room for a reference (analysis starts at the data
    // edge) leaves these empty; custom mode requires typing them.
    const msg = state.refMode === "auto"
      ? "No room for a reference before this analysis window — load more history (earlier --start-date) or move the window."
      : "Set the reference window dates (or click auto).";
    return fail(["ref-start", "ref-end"].filter((id) => !$(id).value), msg);
  }

  const lo = state.meta.date_start, hi = state.meta.date_end;
  const oob = ids.filter((id) => $(id).value < lo || $(id).value > hi);
  if (oob.length) return fail(oob, `Windows must stay within the data range (${lo} … ${hi}).`);
  if (rs > re) return fail(["ref-start", "ref-end"], "Reference window start must be on or before its end.");
  if (re >= as) return fail(["ref-end", "an-start"], "Reference window must end before the analysis window starts.");
  if (as > ae) return fail(["an-start", "an-end"], "Analysis window start must be on or before its end.");

  runBtn.disabled = false;
  // Muted advisories, not errors. Scope-keyed to the selected target: only
  // seasonality that can actually shape this comparison triggers the
  // whole-week nudge.
  const refLen = daysInclusive(new Date(rs), new Date(re));
  const anLen = daysInclusive(new Date(as), new Date(ae));
  const target = $("target-select").value;
  const align = state.dag ? referenceAlignment(target) : { weekAlign: false, coarsest: "day" };
  const grainDays = { day: 1, week: 7, month: 30 }[align.coarsest] || 1;
  const advisories = [];
  if (align.weekAlign && (refLen % 7 !== 0 || anLen % 7 !== 0)) {
    advisories.push("windows aren't whole weeks — weekday mix can distort the comparison");
  }
  if (refLen < 4 * grainDays) {
    advisories.push(`short reference — the baseline mean rides on ~${Math.max(1, Math.floor(refLen / grainDays))} ${align.coarsest} period${refLen < 2 * grainDays ? "" : "s"}, so its interval may be optimistic`);
  }
  if (daysInclusive(new Date(re), new Date(as)) > 2) {
    advisories.push("gap between windows — in a trending business a non-adjacent reference absorbs trend into the comparison");
  }
  // Advisories qualify the *result*, so they go in a persistent line inside
  // the setup panel and are remembered for the results card and the export.
  // They used to be written to `#status`, which run progress overwrites on its
  // first 400ms poll and the completion notice overwrites again — so the
  // caveat about the window was always erased by the run it was warning about,
  // and never reached the report a reader sees without its author.
  state.winAdvisories = advisories;
  renderWindowAdvisories();
  setStatus("");
  return true;
}

function renderWindowAdvisories() {
  const el = $("win-advisories");
  if (!el) return;
  const adv = state.winAdvisories || [];
  el.hidden = !adv.length;
  el.textContent = adv.length ? "ⓘ " + adv.join(" · ") : "";
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

/* ---------- degraded RCA nodes ----------
   `POST /rca/{name}` degrades a node instead of failing the whole tree: any
   `status` other than "ok" means that node was reported *without* attribution,
   with the engine's own sentence in `status_reason`. The cardinal sin this
   guards against is rendering one of those as an analyzed node that simply
   found nothing — an empty contributions table plus a null `attribution_method`
   otherwise reads as "posterior, no drivers". Every consumer (canvas overlay,
   ranked causes, attribution detail, the exported report) goes through here so
   the vocabulary is one string, not four.

   `label` is the noun phrase; `short` is the chip; `explains` says which part
   of the record survived, because that differs and it matters: a fit failure
   loses the decomposition, a too-short window loses the numbers themselves. */
const NODE_STATUS = {
  window_shorter_than_grain: {
    label: "not analyzed — window shorter than grain",
    short: "window < grain",
    explains: "The windows hold no whole period at this metric's grain, so it has no measured movement here at all.",
  },
  fit_failed: {
    label: "not analyzed — model fit failed",
    short: "fit failed",
    explains: "Its movement below is measured from the data and stands; what is missing is the decomposition, because this node's model could not be fitted.",
  },
  attribution_failed: {
    label: "not decomposed — attribution failed",
    short: "attribution failed",
    explains: "Its movement below is measured from the data and stands; what is missing is the decomposition, because the formula has no finite value over these windows.",
  },
  undefined_over_window: {
    label: "no value — every period undefined",
    short: "undefined over window",
    explains: "This metric has no value over one of the windows: every period in it is undefined. A rate whose denominator is zero has no rate — nothing happened for it to be an average of — so there is no number to compare, and none is shown. Choose a window containing at least one defined period.",
  },
};

/* The status entry for a node, or null when it is fine. Unknown statuses are
   surfaced verbatim rather than swallowed — a status this build has never
   heard of is still not "ok", and silently treating it as ok is the failure
   mode this whole block exists to prevent. */
function nodeStatus(node) {
  if (!node || !node.status || node.status === "ok") return null;
  return (
    NODE_STATUS[node.status] || {
      label: `not analyzed — ${node.status}`,
      short: node.status,
      explains: "",
    }
  );
}

/* `ci_status` is the interval's own health, independent of the node's status.
   All four values are surfaced: rendering nothing for three of them and a note
   for the fourth reads as "interval checked and fine" when it means "not
   said". */
const CI_STATUS_NOTE = {
  degenerate_single_period: {
    text: "single-period window: no bootstrap CI",
    why: "A single period gives the block bootstrap nothing to resample, so every replicate is identical and the interval would be falsely zero-width. It is withheld instead.",
  },
  posterior_only_single_period: {
    text: "single-period window: posterior-only CI",
    why: "Intervals here carry the coefficient posterior only — the window-resampling component is absent, because a single period cannot be resampled. Read them as narrower than the truth.",
  },
  nonfinite_bootstrap_replicates: {
    text: "intervals withheld: non-finite bootstrap replicates",
    why: "Enough bootstrap replicates came out non-finite (a resampled denominator mean landing on zero) that an interval was withheld entirely, or computed only from the replicates that survived. Point estimates are unaffected: they are the exact Shapley values, never bootstrap means.",
  },
  degenerate_bootstrap_spread: {
    text: "intervals withheld: a parent does not vary in this window",
    why: "At least one parent holds the same value across the whole window — an unlaunched feature, a stock held flat, a seasonal business's off-season — so every bootstrap replicate resamples the same number and the interval would come out exactly zero-width. A zero-width interval is not certainty, it is the absence of information, so it is withheld (roadmap C4).",
  },
};

/* The note for a `ci_status`, or null when there is nothing to say. `"ok"` and
   `null` are the only silent values; everything else is surfaced, including a
   value this build has never heard of.

   This mirrors `nodeStatus()` deliberately. `CI_STATUS_NOTE[x]` on its own
   returns undefined for an unknown status and renders nothing — which is
   indistinguishable from "interval checked and fine". That is exactly how
   `posterior_only_single_period` went unrendered for its whole life: the
   lookup table was the enumeration, and an enumeration with a silent default
   is not one. A status the engine emits and this build cannot name is still
   not `ok`. */
/* How a node's decomposition was computed. Three known answers and no silent
   default: `x === "shapley" ? … : "posterior"` labelled a null or unrecognised
   method "posterior", which is a specific claim about how the numbers were
   produced — the sort of claim that must never be a fallback branch. */
const ATTRIBUTION_LABEL = {
  shapley: "Shapley (exact)",
  posterior: "posterior",
  slice_sum: "slice sum",
  slice_blend: "slice blend",
};

function attributionLabel(method) {
  if (!method) return "attribution method not reported";
  return ATTRIBUTION_LABEL[method] || `attribution method: ${method}`;
}

function ciStatusNote(status) {
  if (!status || status === "ok") return null;
  return (
    CI_STATUS_NOTE[status] || {
      text: `interval flagged: ${status}`,
      why: "This build does not recognise that interval status, so it is shown verbatim. It is not 'ok' — the engine flagged something about this interval that a newer version of the UI would explain. Treat the interval as qualified until you can check the engine's docs.",
    }
  );
}

/* Trend and seasonal rows for a posterior node's attribution table. They are
   part of the arithmetic — `unexplained = gap − Σcontributions − trend −
   seasonal` — so a table without them does not reconcile to the gap and hands
   the reader an unexplained figure that is smaller than the hole in the table.

   A component that is identically zero with a [0, 0] interval is a term the
   model does not carry (an unseasonal metric's seasonal component). Rendering
   `[0.00, 0.00]` as a 95% credible interval asserts a precision that was never
   estimated, so those rows are dropped: they contribute nothing to the sum, and
   silence about a term that does not exist is honest.

   Shared by the live table and the exported report so the two cannot drift —
   the export carried these rows and the live view did not, which is how the
   discrepancy survived. */
/* What the `unexplained` row is called, and whether it needs saying twice.

   `unexplained: 0` has two completely different meanings and one appearance
   (roadmap 1.11a):

   - **measured** — the node's own series was fetched and compared against the
     decomposition, and the two reconciled. That is a *result*, and a good one.
   - **definitional** — the node is derived: its series *is* the formula, so
     there was never anything to check. That is the *absence* of a result.

   Rendering them identically is the defect this project keeps finding — an
   absence wearing a measurement's clothes, like `null >= 0` painting a node
   green. So the row is renamed rather than annotated: a label is in the export,
   in a screenshot and in a copy-paste, where a tooltip is not.

   Returns `null` when there is no `unexplained` to show at all. */
function unexplainedRow(node) {
  if (node.unexplained == null) return null;
  if (node.unexplained_status === "definitional") {
    return {
      label: "unexplained — none by definition",
      title:
        "This node is derived: its series is computed from the formula, so the " +
        "decomposition cannot miss it. Zero here means nothing was checked, not " +
        "that a check passed. Give the node a `source` to have its identity " +
        "measured against the warehouse.",
      definitional: true,
    };
  }
  return {
    label: "unexplained",
    title:
      "The part of the gap the decomposition did not account for, measured " +
      "against this node's own fetched series.",
    definitional: false,
  };
}

/* What "→" is between, for a node whose two numbers are not window means.

   Every surface used to print "window means" under a baseline → actual pair,
   which is true of a flow or a stock and true of no rate at all: a rate's
   window value is `Σnumerator / Σdenominator` (the *component aggregate*), and
   where there is no denominator it is the mean of the per-period ratios — a
   different number, wearing the same words.

   The three fallbacks are not one thing either, which is the whole of roadmap
   1.11's third state. "No component aggregate exists" is a fact about the
   metric — a median is not Σnum/Σden for any pair of series, so this mean is
   the only number there is. "No denominator declared" is a fact about the
   *tree*, and it is fixable. A reader who cannot tell them apart either
   distrusts a number that is fine or trusts a tree that is unfinished.

   So the distinction goes in the label, like `unexplainedRow` and for the same
   reason: a label survives a screenshot, a copy-paste and the export, where a
   tooltip does not. `title` carries the author's own reason for the live
   surfaces; the export prints it as text. */
function windowBasis(node) {
  const agg = node.window_aggregate;
  if (!agg) return { label: "window means", title: "" };
  if (agg === "components")
    return {
      label: "component aggregate",
      title:
        "Σnumerator / Σdenominator over the window's defined periods — what a " +
        "window's rate is. Not the average of the per-period ratios, which is a " +
        "different number whenever the denominators differ.",
    };
  const why =
    {
      period_mean_none_exists: "no component aggregate exists",
      period_mean_undeclared: "no denominator declared",
      period_mean_weights_unavailable: "denominator unusable over these windows",
    }[agg] || "not a component aggregate";
  return { label: `period means — ${why}`, title: node.window_aggregate_reason || "" };
}

/* The inline form for the live surfaces: label, grain, reason on hover. */
function windowBasisHtml(node) {
  const w = windowBasis(node);
  const per = node.grain && node.grain !== "day" ? ` per ${esc(node.grain)}` : "";
  return `<span${w.title ? ` title="${esc(w.title)}"` : ""}>${esc(w.label)}${per}</span>`;
}

function componentRowsHtml(node, nCols, shareOf, ciCell) {
  const comps = node.components;
  if (!comps || nCols !== 5) return "";
  // No `zeroish` filter any more: the engine used to emit a structurally
  // absent `seasonal` as `{estimate: 0, ci_95: [0, 0]}` — a zero-width 95%
  // interval asserting infinite precision about a term the model never had —
  // and this function dropped the row to compensate. C4 fixed it at the
  // source: a term the node does not declare is simply not a key. Absence is
  // the only signal now, which is why the filter is just `comps[k]`.
  return ["trend", "seasonal"]
    .filter((k) => comps[k])
    .map(
      (k) => `<tr class="dim"><td>${k}</td>
        <td class="num">${fmt(comps[k].estimate)}</td>
        <td class="num">${shareOf(comps[k].estimate, node.gap)}</td>
        <td class="num">${ciCell(comps[k].ci_95)}</td>
        <td class="num">—</td></tr>`,
    )
    .join("");
}

/* Always-on footer for the Root cause tab, the counterpart of the what-if
   tab's `res.caveats`. The exported report has carried a Methods footnote and
   the words "triage heuristic, not rigorous multi-hop attribution" since it
   shipped; the live view — where the ranked list is the single most prominent
   thing on screen — carried neither, so the reader most likely to act on the
   ranking was the one told least about what it is. (The deeper tension is
   roadmap S12; this is only the disclosure, not the fix.) */
const RCA_CAVEATS = [
  "Ranked causes are a triage order, not evidence: the score walks the tree multiplying each edge's share of its child's gap. Read it as where to look next, and read the attribution tables for what was actually measured.",
  "Changes are window-mean differences at each node's grain. Formula edges are exact Shapley attributions; probabilistic edges multiply a fitted posterior by the parent's window delta, so they are fitted associations, not experiments.",
  "Intervals are 95% credible intervals combining the coefficient posterior with a moving-block bootstrap of the window rows. Where one is withheld the table says so; the point estimates are unaffected.",
];

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
    treePng = state.cy.png({ full: true, scale: 2, bg: COL.bg, maxWidth: 2400 });
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
  // A withheld numerator is not a zero share: `null / gap` is 0 in JavaScript,
  // so an unguarded division printed "0.0%" for a quantity the engine declined
  // to give.
  const shareOf = (v, gap) =>
    v == null || gap == null || Math.abs(gap) <= 1e-12 ? "—" : pct(v / gap);
  const num = (v) => `<td class="num">${v}</td>`;

  const grainNote = (node) => {
    const bits = [];
    // The fit window — all loaded history before the analysis window, never the
    // reference window. The live header has carried it since 2.14; the export
    // did not, so the one node flagged `⚠ suspect fit` shipped that verdict
    // with no way to see what the fit was made of. It is the most
    // decision-relevant fact on such a node, and a static report is precisely
    // where the reader cannot hover for it.
    if (node.fit_window) {
      bits.push(`fitted on ${node.fit_window.n_periods} ${node.grain}s (${node.fit_window.start} → ${node.fit_window.end})`);
    }
    if (node.grain && node.grain !== "day" && node.effective_windows) {
      const ew = node.effective_windows;
      bits.push(`${node.grain} grain, snapped to ${ew.reference.n_periods}+${ew.analysis.n_periods} whole ${node.grain}s`);
    }
    if (node.fit_quality === "suspect") bits.push("⚠ suspect fit — the engine's own fit check failed for this node's model");
    if (node.sign_warnings && node.sign_warnings.length) bits.push("⚠ learned sign contradicts declared expectation");
    if (node.seasonality_warnings && node.seasonality_warnings.length) bits.push("⚠ seasonality unidentifiable from fitted history");
    return bits.length ? ` · ${esc(bits.join(" · "))}` : "";
  };

  // Every caveat the live view hides behind a `title=` tooltip has to be
  // printed here in full. A static report is read without its author present
  // and with nothing to hover: a caveat rendered as a tooltip is simply absent
  // from it, which turns "we withheld this interval" into silence.
  const caveatBlock = (node) => {
    const out = [];
    const ci = ciStatusNote(node.ci_status);
    if (ci) out.push(`<strong>${esc(ci.text)}.</strong> ${esc(ci.why)}`);
    // A static report has no hover, and the export is what circulates without
    // its author — so the reason a zero is not a reconciliation is written out.
    if (node.unexplained_status === "definitional") {
      out.push(
        "<strong>Derived node: nothing was reconciled.</strong> This metric has no " +
        "<code>source</code> of its own — its series is computed from the formula below — " +
        "so the row reading zero says the decomposition cannot miss it, not that it was " +
        "checked against the warehouse and agreed. Give the node a source to have that " +
        "checked.",
      );
    }
    if (node.fit_quality === "suspect") {
      out.push(
        "<strong>Suspect fit.</strong> The engine's own fit check failed for this node's model " +
        "(NUTS: R̂, divergences or effective sample size; ADVI: an ELBO that had not settled). " +
        "The contributions in this section rest on that fit.",
      );
    }
    (node.seasonality_warnings || []).forEach((w) => out.push(esc(w)));
    return out.map((t) => `<p class="warn">⚠ ${t}</p>`).join("");
  };

  // The movement line: real for every node the engine reached, including the
  // ones it could not decompose.
  const gapLine = (node) =>
    node.gap == null
      ? ""
      : `<p class="meta">gap ${fmt(node.gap)} (${node.relative_change == null ? "—" : signedPct(node.relative_change)}) · ${fmt(node.baseline)} → ${fmt(node.actual)} ${esc(windowBasis(node).label)}${node.grain && node.grain !== "day" ? ` per ${esc(node.grain)}` : ""}${windowBasis(node).title && node.window_aggregate !== "components" ? ` <em>${esc(windowBasis(node).title)}</em>` : ""}</p>`;

  const order = [res.target, ...res.ranked_causes.map((c) => c.metric)];
  const blocks = order
    .filter((name) => res.nodes[name] && (res.nodes[name].contributions.length || nodeStatus(res.nodes[name])))
    .map((name) => {
      const node = res.nodes[name];
      const st = nodeStatus(node);
      // A degraded node gets a section of its own rather than being dropped:
      // an export that omits it is an export that claims the ranked causes
      // below are the whole story. Reasons print in full — a static report
      // has no hover to hide them behind.
      if (st) {
        return `<section>
          <h3><code>${esc(name)}</code> <span class="meta">${esc(st.label)}</span></h3>
          ${gapLine(node)}
          <p class="warn">⚠ ${esc(st.explains)}${node.status_reason ? ` <em>${esc(node.status_reason)}</em>` : ""}</p>
        </section>`;
      }
      const twoLevel = node.contributions.some((c) => c.decomposition);
      // Same row, same words as the live table — through the same function, so
      // a definitional zero cannot be labelled in one surface and not the
      // other. The export is what circulates without its author, so the
      // distinction is in the label rather than a hover.
      const ux = unexplainedRow(node);
      const uxShare = ux && !ux.definitional ? shareOf(node.unexplained, node.gap) : "—";
      const unexpl = ux
        ? `<tr class="dim"><td>${esc(ux.label)}</td>${num(fmt(node.unexplained))}${num(uxShare)}<td class="num">—</td></tr>`
        : "";
      let tables;
      if (twoLevel) {
        const headRows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code>${lagLine(c, node.grain)}</td>${num(fmt(c.decomposition.means.estimate))}${num(shareOf(c.decomposition.means.estimate, node.gap))}${num(ciCell(c.decomposition.means.ci_95))}</tr>`).join("");
        const interaction = node.interaction
          ? `<tr class="em"><td>co-movement shift</td>${num(fmt(node.interaction.estimate))}${num(shareOf(node.interaction.estimate, node.gap))}${num(ciCell(node.interaction.ci_95))}</tr>`
          : "";
        const detRows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code>${lagLine(c, node.grain)}</td>${num(fmt(c.decomposition.means.estimate))}${num(fmt(c.decomposition.comovement.estimate))}${num(fmt(c.estimate))}${num(ciCell(c.ci_95))}${num(pctDir(c.prob_same_direction, c.prob_same_direction_censored))}</tr>`).join("");
        tables = `
          <h4>Headline — window-means bridge</h4>
          <table><tr><th>Driver</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th></tr>${headRows}${interaction}${unexpl}</table>
          <h4>Detailed — per-parent split (means + co-movement = total)</h4>
          <table><tr><th>Parent</th><th class="num">means</th><th class="num">co-movement</th><th class="num">total Δ</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>${detRows}</table>`;
      } else {
        const rows = node.contributions.map((c) => `<tr><td><code>${esc(c.parent)}</code>${lagLine(c, node.grain)}</td>${num(fmt(c.estimate))}${num(c.share_of_gap == null ? "—" : pct(c.share_of_gap))}${num(ciCell(c.ci_95))}${num(pctDir(c.prob_same_direction, c.prob_same_direction_censored))}</tr>`).join("");
        // Same rows, same rule as the live table — via one function, so the two
        // cannot disagree about whether trend and seasonal are part of the sum.
        const comps = componentRowsHtml(node, 5, shareOf, ciCell);
        const unexpl5 = ux
          ? `<tr class="dim"><td>${esc(ux.label)}</td>${num(fmt(node.unexplained))}${num(uxShare)}<td class="num">—</td><td class="num">—</td></tr>`
          : "";
        tables = `<table><tr><th>Parent</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>${rows}${comps}${unexpl5}</table>`;
      }
      const signWarn = (node.sign_warnings || []).map((w) => `<p class="warn">⚠ ${esc(w)}</p>`).join("");
      return `<section>
        <h3><code>${esc(name)}</code> <span class="meta">${esc(attributionLabel(node.attribution_method))}${grainNote(node)}</span></h3>
        ${gapLine(node)}
        ${signWarn}${caveatBlock(node)}${tables}
      </section>`;
    })
    .join("");

  const degraded = Object.entries(res.nodes).filter(([, n]) => nodeStatus(n));

  const ranked = res.ranked_causes
    .map((c, i) => `<tr><td>${i + 1}</td><td><code>${esc(c.metric)}</code></td>${num(Number.isFinite(c.score) ? c.score.toFixed(2) : "—")}<td>via ${esc(c.via || "—")}</td></tr>`)
    .join("");

  // `data_through` carries `null` for a metric whose data edge is unknown.
  // Filter before the min: `null < "9999"` is `true` in JS (null coerces to 0),
  // so an unknown edge would win the comparison and become the tree-wide anchor.
  const dataThrough = state.meta && state.meta.data_through
    ? Object.values(state.meta.data_through)
        .filter((d) => typeof d === "string")
        .reduce((a, b) => (a < b ? a : b), "9999")
    : null;

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>RCA — ${esc(res.target)} · ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: ${COL.text}; max-width: 960px; margin: 32px auto; padding: 0 20px; line-height: 1.45; }
  h1 { font-size: 22px; margin-bottom: 2px; } h3 { font-size: 15px; margin: 22px 0 4px; } h4 { font-size: 12.5px; margin: 12px 0 4px; color: ${COL.text2}; text-transform: uppercase; letter-spacing: 0.4px; }
  .meta { color: ${COL.muted}; font-size: 12.5px; font-weight: 400; }
  .gap { font-size: 26px; font-weight: 700; margin: 6px 0 14px; }
  .gap.up { color: ${COL.up}; } .gap.down { color: ${COL.down}; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 4px 0 10px; }
  th { text-align: left; color: ${COL.muted}; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; padding: 4px 6px; border-bottom: 1px solid ${COL.border}; }
  td { padding: 4px 6px; border-bottom: 1px solid ${COL.rule}; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.dim td { color: ${COL.faint}; } tr.em td { font-style: italic; }
  img { max-width: 100%; border: 1px solid ${COL.border}; border-radius: 8px; margin: 8px 0; }
  code { background: ${COL.chipBg}; padding: 1px 4px; border-radius: 4px; font-size: 12px; }
  .warn { font-size: 12px; color: ${COL.warnInk}; background: ${COL.warnSoft}; border: 1px solid ${COL.warnBorder}; border-radius: 6px; padding: 6px 8px; }
  .footnote { margin-top: 28px; padding-top: 12px; border-top: 1px solid ${COL.border}; font-size: 11.5px; color: ${COL.muted}; }
  @media print { body { margin: 8px auto; } section { break-inside: avoid; } }
</style></head><body>
  <h1>Root cause analysis — <code>${esc(res.target)}</code></h1>
  <p class="meta">reference ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)}${res.reference_defaulted ? " (chosen by the engine, not by the person who ran this)" : ""} vs analysis ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}${dataThrough ? ` · data through ${esc(dataThrough)}` : ""}${state.meta ? ` · provider: ${esc(state.meta.provider)}` : ""} · generated ${now.toISOString().slice(0, 16).replace("T", " ")} UTC</p>
  ${(state.winAdvisories || []).length
    ? `<p class="warn">⚠ About these windows: ${esc(state.winAdvisories.join(" · "))}</p>`
    : ""}
  ${target.gap == null
    ? `<p class="warn">⚠ ${esc((nodeStatus(target) || {}).label || "not analyzed")} — ${esc(target.status_reason || "")}</p>`
    : `<div class="gap ${gapLineParts(res.target, target.gap).cls}">${gapLineParts(res.target, target.gap).sign}${fmt(target.gap)} (${signedPct(target.relative_change)})</div>
       ${nodeStatus(target) ? `<p class="warn">⚠ ${esc(nodeStatus(target).label)} — ${esc(target.status_reason || "")} The ranked causes below carry no information about this target: nothing was attributed to it.</p>` : ""}`}
  ${stripPng ? `<img src="${stripPng}" alt="target series with reference and analysis windows shaded">` : ""}
  ${treePng ? `<h3>Metric tree</h3><img src="${treePng}" alt="metric tree with RCA overlay">` : ""}
  <h3>Ranked causes <span class="meta">(triage heuristic, not rigorous multi-hop attribution)</span></h3>
  <table><tr><th>#</th><th>Metric</th><th class="num">score</th><th>via</th></tr>${ranked}</table>
  ${degraded.length
    ? `<p class="warn">⚠ ${degraded.length} of ${Object.keys(res.nodes).length} metrics in scope could not be analyzed, so this ranking is incomplete — nothing upstream of them was attributed. ${degraded
        .map(([n, node]) => `<code>${esc(n)}</code> (${esc(nodeStatus(node).short)})`)
        .join(", ")}. Each is detailed below.</p>`
    : ""}
  <h3 style="margin-top:24px">Attribution detail</h3>
  ${blocks}
  <div class="footnote">
    <strong>Methods.</strong> Changes are window-mean differences at each node's grain, over the whole periods inside the
    requested windows. Formula (identity) nodes use exact symmetric per-period Shapley attribution — a window-means bridge
    plus each parent's share of each window's within-window co-movement term — so attributions sum exactly to the gap and
    <em>unexplained</em> is measurement residual only — and where a node is <em>derived</em> (no source of its own,
    so its series is the formula) there is no measurement, which the row says instead of reading zero. Probabilistic nodes multiply the fitted <code>beta_raw</code>
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
const CARD_COL = { up: COL.up, down: COL.down, flat: COL.muted };
const CARD_COL_SOFT = { up: COL.upSoft, down: COL.downSoft, flat: COL.track };
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
   Arrows and labels stay directional — only the good/bad coloring flips.

   An **undeclared** direction (`null`) colors gray too. Green here means
   "improved", which is a claim, and nobody made it: the engine used to default
   the field to `up_is_good` in the parser, so the browser could not tell "the
   author said up is good" from "the author said nothing" and painted the
   second as the first — `churn_arpu` up 18.5% rendered green while carrying
   27% of the damage. Refusing to judge is the only honest rendering of an
   absent declaration, and it is the same rendering `neutral` already had. */
function goodDir(name, dir) {
  if (dir !== "up" && dir !== "down") return dir;
  const decl = state.defs && state.defs[name] && state.defs[name].direction;
  if (!decl || decl === "neutral") return "flat";
  if (decl === "down_is_good") return dir === "up" ? "down" : "up";
  return dir;
}

/* Goodness-mapped overlay class ("<prefix>-up" green / "<prefix>-down" red),
   or null for neutral metrics — they get no judgmental tint at all. */
function goodClass(name, dir, prefix) {
  const g = goodDir(name, dir);
  return g === "up" ? `${prefix}-up` : g === "down" ? `${prefix}-down` : null;
}

/* The MOVEMENT direction a gap claims, or null when it claims none.
   `null >= 0` is `true` in JavaScript and so is `0 >= 0`, so testing a gap
   with `>=` painted two different non-claims green: a node the engine could
   not measure, and a node that provably did not move (the engine's own
   threshold for "no movement" is `abs(gap) < 1e-12`, which is exactly when it
   withholds `share_of_gap` and `relative_change`). Green means *improved* in
   this legend; neither of those is an improvement. Callers must treat null as
   "no tint, no arrow, no sign". */
const GAP_EPS = 1e-12;

function gapDir(gap) {
  if (gap == null || !Number.isFinite(gap) || Math.abs(gap) < GAP_EPS) return null;
  return gap > 0 ? "up" : "down";
}

/* The gap headline's colour class and leading sign, withheld together when the
   gap makes no directional claim. `.gap-line` with no up/down class is the ink
   colour — the "no claim" rendering, which is what a null or zero gap is. */
function gapLineParts(name, gap) {
  const d = gapDir(gap);
  return { cls: d ? goodDir(name, d) : "", sign: d === "up" ? "+" : "" };
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
    return `<text x="${cx}" y="${baseline}" text-anchor="middle" font-size="12.5" fill="${COL.faint}" font-family="${CARD_FONT}">—</text>`;
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
    `<text x="${cx}" y="20" text-anchor="middle" font-size="13" font-weight="600" fill="${COL.text2}" font-family="${CARD_FONT}">${esc(dispName)}</text>` +
    `<text x="${cx}" y="52" text-anchor="middle" font-size="34" font-weight="700" fill="${COL.text}" font-family="${CARD_FONT}">${esc(fmtCardValue(name, val))}</text>`;

  if (unit) {
    const u = unit.length > 22 ? unit.slice(0, 21) + "…" : unit;
    inner += `<text x="${cx}" y="66" text-anchor="middle" font-size="11" fill="${COL.faint}" font-family="${CARD_FONT}">${esc(u)}</text>`;
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
      inner += `<line x1="16" y1="${110 + uOff}" x2="${CARD_W - 16}" y2="${110 + uOff}" stroke="${COL.rule}" stroke-width="1"/>`;
      inner += deltaSvg(dpct, dir, mark, cx, 130 + uOff, colorDir);
    } else {
      inner += deltaSvg(dpct, dir, mark, cx, 82 + uOff, colorDir);
    }
  }

  if (isOverride) {
    // small indigo dot: this node's variant is pinned, ignoring the canvas default
    inner +=
      `<circle cx="${CARD_W - 12}" cy="12" r="6" fill="${COL.accent}" fill-opacity="0.18"/>` +
      `<circle cx="${CARD_W - 12}" cy="12" r="3.5" fill="${COL.accent}"/>`;
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
    `<text x="${cx}" y="20" text-anchor="middle" font-size="13" font-weight="600" fill="${COL.text2}" font-family="${CARD_FONT}">${esc(dispName)}</text>` +
    `<text x="${cx}" y="52" text-anchor="middle" font-size="34" font-weight="700" fill="${COL.text}" font-family="${CARD_FONT}">${esc(fmtCardValue(name, val))}</text>`;

  if (overlay) {
    inner += deltaSvg(overlay.dpct, overlay.dir, overlay.mark, cx, 82, goodDir(name, overlay.dir));
  } else {
    const sub = cb && cb.lo != null
      ? `${fmtCardValue(name, cb.lo)} – ${fmtCardValue(name, cb.hi)} · 90% belief`
      : cb && cb.derived ? "derived from parents" : "belief";
    inner += `<text x="${cx}" y="80" text-anchor="middle" font-size="11.5" fill="${COL.faint}" font-family="${CARD_FONT}">${esc(sub)}</text>`;
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

/* Both stores key on the tree id: overrides and saved views are keyed by
   *metric name*, and two trees in one process can name the same metric while
   meaning different things — a view saved against one tree would otherwise
   replay against another. */
const CARD_CFG_KEY = "breakdown.cardConfig";

function storeKey(base) {
  return state.tree ? `${base}:${state.tree}` : base;
}

function loadCardConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(storeKey(CARD_CFG_KEY)) || "{}");
    const c = state.cardConfig;
    if (["num", "delta", "spark", "full"].includes(saved.variant)) c.variant = saved.variant;
    if (Number.isFinite(saved.deltaLen)) c.deltaLen = saved.deltaLen;
    if (Number.isFinite(saved.sparkLen)) c.sparkLen = saved.sparkLen;
    if (saved.overrides && typeof saved.overrides === "object") c.overrides = saved.overrides;
  } catch { /* ignore malformed storage */ }
}

function saveCardConfig() {
  try {
    localStorage.setItem(storeKey(CARD_CFG_KEY), JSON.stringify(state.cardConfig));
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
    const saved = JSON.parse(localStorage.getItem(storeKey(VIEWS_KEY)) || "[]");
    return Array.isArray(saved) ? saved.filter((v) => v && v.hash && v.name) : [];
  } catch {
    return [];
  }
}

function writeSavedViews(views) {
  try {
    localStorage.setItem(storeKey(VIEWS_KEY), JSON.stringify(views.slice(0, MAX_SAVED_VIEWS)));
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
      "background-color": COL.panel,
      "border-width": 2,
      "border-color": COL.source,
      label: "data(label)",
      "text-wrap": "wrap",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": 12,
      "font-weight": 600,
      color: COL.text,
      width: "label",
      height: "label",
      padding: "12px",
    },
  },
  { selector: "node.prob", style: { "border-color": COL.accent } },
  { selector: "node.formula", style: { "border-color": COL.formula } },
  { selector: "node.fitted", style: { "background-color": COL.accentSoft } },
  {
    selector: "node:selected",
    style: { "border-width": 3.5, "border-color": COL.accent },
  },
  {
    // The ancestor being fitted right now. Placed after :selected so it wins
    // while a run is in flight — during a fit, where the engine *is* matters
    // more than what the user last clicked.
    selector: "node.fitting-now",
    style: {
      "border-width": 5,
      "border-color": COL.accent,
      "background-color": COL.accentSoft,
    },
  },
  {
    selector: "node.rca-up",
    style: { "background-color": COL.upSoft, "border-color": COL.up },
  },
  {
    selector: "node.rca-down",
    style: { "background-color": COL.downSoft, "border-color": COL.down },
  },
  {
    selector: "edge",
    style: {
      width: 2,
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "arrow-scale": 1.1,
      "line-color": COL.source,
      "target-arrow-color": COL.source,
      label: "data(label)",
      "font-size": 10,
      color: COL.text2,
      "text-background-color": COL.bg,
      "text-background-opacity": 0.92,
      "text-background-padding": 3,
    },
  },
  {
    selector: "edge.formula",
    style: { "line-color": COL.edgeFormula, "target-arrow-color": COL.edgeFormula },
  },
  {
    selector: "edge.prob",
    style: {
      "line-style": "dashed",
      "line-color": COL.edgeProb,
      "target-arrow-color": COL.edgeProb,
    },
  },
  {
    selector: "edge.rca-up",
    style: {
      "line-style": "solid",
      "line-color": COL.up,
      "target-arrow-color": COL.up,
      width: "data(w)",
      "line-opacity": "data(op)",
      "text-opacity": "data(op)",
    },
  },
  {
    selector: "edge.rca-down",
    style: {
      "line-style": "solid",
      "line-color": COL.down,
      "target-arrow-color": COL.down,
      width: "data(w)",
      "line-opacity": "data(op)",
      "text-opacity": "data(op)",
    },
  },
  {
    selector: "node.rca-unexplained",
    style: { "border-style": "dashed", "border-color": COL.warn, "border-width": 3 },
  },
  {
    // Analysis was attempted and failed. Same amber caveat channel as the
    // unexplained badge, but the whole card goes amber rather than keeping an
    // up/down fill — so at a glance it is distinguishable from a healthy node,
    // from a node with a genuinely small effect, and from `rca-unexplained`
    // (which is green/red *inside* an amber outline: analyzed, with a large
    // residual). Placed after the tint rules so it wins if both ever apply.
    selector: "node.rca-not-analyzed",
    style: {
      "background-color": COL.warnSoft,
      "border-style": "dashed",
      "border-color": COL.warn,
      "border-width": 3,
    },
  },
  /* what-if overlay: sign=hue, certainty=background opacity, pinned=heavy border */
  {
    selector: "node.sim-up",
    style: { "background-color": COL.upSoft, "border-color": COL.up, "background-opacity": "data(bgop)" },
  },
  {
    selector: "node.sim-down",
    style: { "background-color": COL.downSoft, "border-color": COL.down, "background-opacity": "data(bgop)" },
  },
  {
    selector: "node.sim-pinned",
    style: { "border-width": 4, "border-style": "solid", "border-color": COL.accent },
  },
  {
    selector: "node.warn-border",
    style: { "border-style": "dashed", "border-color": COL.warn, "border-width": 3 },
  },
  {
    selector: "node.lever",
    style: {
      shape: "ellipse",
      "background-color": COL.warnSoft,
      "border-style": "dashed",
      "border-color": COL.warn,
      "font-size": 11,
    },
  },
  {
    selector: "edge.assume",
    style: {
      "line-style": "dotted",
      "line-color": COL.warn,
      "target-arrow-color": COL.warn,
      width: 2.5,
      color: COL.warnInk,
    },
  },
  {
    selector: "edge.sim-up",
    style: {
      "line-style": "solid",
      "line-color": COL.up,
      "target-arrow-color": COL.up,
      "line-opacity": "data(op)",
    },
  },
  {
    selector: "edge.sim-down",
    style: {
      "line-style": "solid",
      "line-color": COL.down,
      "target-arrow-color": COL.down,
      "line-opacity": "data(op)",
    },
  },
  { selector: ".faded", style: { opacity: 0.25 } },
  {
    selector: ".pathhl",
    style: { "border-color": COL.warn, "line-color": COL.warn, "target-arrow-color": COL.warn },
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
  initZoomControls();
  initCanvasGrid();
}

/* Lock the paper's dot grid to the graph. Left alone the dots sit still while
   the tree slides underneath them, which reads as a rendering bug rather than
   as a canvas; moving them is also the cheapest hint that this surface pans at
   all. Spacing is halved/doubled to stay in a legible 12–96px band so a
   zoomed-out tree doesn't moiré. Dot *radius* stays fixed — it's paper
   texture, not content, and shouldn't grow when you zoom in. */
function initCanvasGrid() {
  const cy = state.cy;
  const el = $("cy");
  const BASE = 24;
  const paint = () => {
    let step = BASE * cy.zoom();
    while (step < 12) step *= 2;
    while (step > 96) step /= 2;
    const p = cy.pan();
    el.style.backgroundSize = `${step}px ${step}px`;
    el.style.backgroundPosition = `${p.x % step}px ${p.y % step}px`;
  };
  cy.on("pan zoom", paint);
  paint();
}

/* Cytoscape pans on background-drag and zooms on wheel out of the box, but
   neither is discoverable and neither gets you back once a big tree is off
   screen. These are the way home. Zoom steps keep the viewport centre fixed so
   the thing you were looking at stays where it is. */
let zoomWired = false;

function initZoomControls() {
  const ZOOM_STEP = 1.25;
  // Resolved at click time, not captured: the retry banner re-runs init() —
  // and so buildGraph() — without a page reload, replacing the instance.
  const cy = () => state.cy;
  const label = $("zoom-level");
  const paint = () => (label.textContent = `${Math.round(cy().zoom() * 100)}%`);
  const fit = () => cy().fit(undefined, 40);
  const setZoom = (level) => {
    const c = cy();
    c.zoom({
      level: Math.min(c.maxZoom(), Math.max(c.minZoom(), level)),
      renderedPosition: { x: c.container().clientWidth / 2, y: c.container().clientHeight / 2 },
    });
  };

  cy().on("zoom", paint); // per-instance, so it re-binds on every rebuild
  paint();

  // The buttons and `document` outlive the graph, so their listeners bind once
  // — a second buildGraph() would otherwise make every click zoom twice.
  if (zoomWired) return;
  zoomWired = true;
  $("zoom-in").addEventListener("click", () => setZoom(cy().zoom() * ZOOM_STEP));
  $("zoom-out").addEventListener("click", () => setZoom(cy().zoom() / ZOOM_STEP));
  $("zoom-level").addEventListener("click", () => setZoom(1));
  $("zoom-fit").addEventListener("click", fit);
  // `F` fits. Guarded on the event target so it never eats a keystroke meant
  // for a date input, the metric filter, or a scenario note.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
    if (e.key === "f" || e.key === "F") fit();
  });
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
      // A missing bound (the API nulls any non-finite summary stat) used to
      // render `β 0.10 [—, —]`: a point estimate wearing the brackets of an
      // interval it does not have. This project's whole stance is that a
      // relationship without an interval is not a result, so say the interval
      // is missing rather than drawing an empty one.
      const haveCI = Number.isFinite(lo) && Number.isFinite(hi);
      edge.data(
        "label",
        haveCI ? `β ${fmt(mean)} [${fmt(lo)}, ${fmt(hi)}]` : `β ${fmt(mean)} · no interval`,
      );
    }
  });
}

/* ---------- metric tab ---------- */

async function selectMetric(name) {
  state.selected = name;
  setHash(`metric=${encodeURIComponent(name)}`);
  updateShareMenu();
  state.cy.$("node:selected").unselect();
  state.cy.getElementById(name).select();
  switchTab("metric");
  const container = $("tab-metric");
  container.innerHTML = `<p class="placeholder">Loading ${esc(name)}…</p>`;
  try {
    const data = state.metricCache[name] || (await api(treePath(`/metrics/${encodeURIComponent(name)}`)));
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
      ${state.meta.data_through && name in state.meta.data_through
        ? `<tr><td>Data through</td><td>${
            state.meta.data_through[name] === null
              ? '<span class="muted" title="This metric returned no rows the engine could date, so its freshness is unknown — which is not the same as fresh.">unknown</span>'
              : `${esc(state.meta.data_through[name])}${
                  state.meta.data_through[name] < state.meta.date_end
                    ? ' <span class="chip lag">lags window end</span>' : ""
                }`
          }</td></tr>`
        : ""}
      <tr><td>Parents</td><td>${parentChips}</td></tr>
      ${def.formula ? `<tr><td>Formula</td><td><code>${esc(def.formula)}</code>${
        def.derived
          ? ` <span class="chip lag" title="This node has no source of its own: its series is computed from the formula at load, and nothing was fetched to check the identity against. Its RCA 'unexplained' is zero by definition, not by measurement.">derived</span>`
          : ` <span class="chip lag" title="This node is fetched independently and the identity is checked against the fetched series, so its RCA 'unexplained' is a real measurement of how far the identity missed.">measured</span>`
      }</td></tr>` : ""}
      ${def.denominator
        ? `<tr><td>Denominator</td><td><code>${esc(def.denominator)}</code> <span class="muted">— a window's rate is Σnumerator / Σ${esc(def.denominator)}, so a period with none drops out</span></td></tr>`
        : def.kind === "rate"
          ? def.no_denominator
            // Asked and answered (roadmap 1.11): the reason is shown, not
            // hidden behind a hover, and no remedy is offered — there is
            // nothing to fix. Advising "declare a denominator" here is what
            // this state exists to stop, since for a median it cannot be done.
            ? `<tr><td>Denominator</td><td><span class="muted">none — the mean of the defined periods is the only aggregate there is. <em>${esc(def.no_denominator)}</em></span></td></tr>`
            : `<tr><td>Denominator</td><td><span class="muted" title="Without a denominator, a window's value here is the plain average of the per-period ratios — which is not what a window's rate is. Declare denominator: on this metric, or no_denominator: with the reason if it genuinely has none.">not declared — window values average the per-period ratios</span></td></tr>`
          : ""}
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
        <label for="an-method">Method</label>
        <select id="an-method">
          <option value="advi">ADVI — fast, approximate</option>
          <option value="nuts">NUTS — slow, exact</option>
        </select>
        ${hintHTML("method")}
      </div>
      ${hintSlot("method")}
      <div class="analyze-row">
        <label for="an-draws">Draws</label>
        <input type="number" id="an-draws" value="500" min="50" max="5000" step="50">
        ${hintHTML("draws")}
        <button id="an-run" class="primary">Run</button>
      </div>
      <p class="control-note" id="an-draws-note"></p>
      ${hintSlot("draws")}
      <div class="inline-status" id="an-status"></div>
    </section>
  `;
  $("tab-metric").innerHTML = html;
  wireQueryProvenance($("tab-metric"));

  renderTimeSeries(name, data.time_series);
  renderPosterior(name, data);
  $("an-run").addEventListener("click", () => runAnalyze(name));
  wireAnalyzeNote();
  wireNodeVariant(name);
}

/* "500 of what?" answered in the place the question gets asked, without a
   click. The two methods spend the number on completely different things, so
   the note is rebuilt whenever either control changes — and an open `draws`
   hint is re-rendered with it, since its text is method-dependent too. */
function wireAnalyzeNote() {
  const method = $("an-method"), draws = $("an-draws"), note = $("an-draws-note");
  if (!method || !draws || !note) return;
  const paint = () => {
    const n = Math.max(0, parseInt(draws.value, 10) || 0);
    note.textContent =
      method.value === "nuts"
        ? `${n.toLocaleString()} × 4 chains, after 1,000 discarded tuning steps `
          + `— ${(n * 4).toLocaleString()} posterior draws. Expect a minute or more.`
        : `20,000 optimization steps, then ${n.toLocaleString()} samples drawn `
          + `from the fitted approximation. Seconds.`;
    const openHint = document.querySelector('.hint[data-hint="draws"][aria-expanded="true"]');
    if (openHint) {
      const slot = document.querySelector('[data-hint-slot="draws"]');
      if (slot) slot.innerHTML = `<div class="hint-panel">${HINTS.draws.body()}</div>`;
    }
  };
  method.addEventListener("change", paint);
  draws.addEventListener("input", paint);
  paint();
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
    // `readWindows()` omits the reference pair in auto mode — that is right for
    // the *request* (the server computes it) but wrong for shading, and an
    // undefined x0 makes Plotly place the rect in the year 2000, which blows
    // the axis range out and collapses the real series into a spike at the
    // right edge. The inputs still hold the previewed window, so shade from
    // those; skip the band only when there genuinely isn't one.
    const refStart = win.reference_start || $("ref-start").value;
    const refEnd = win.reference_end || $("ref-end").value;
    if (refStart && refEnd) {
      shapes.push({ type: "rect", xref: "x", yref: "paper", x0: refStart, x1: refEnd, y0: 0, y1: 1, fillcolor: COL.source, opacity: 0.16, line: { width: 0 } });
    }
    shapes.push(
      { type: "rect", xref: "x", yref: "paper", x0: win.analysis_start, x1: win.analysis_end, y0: 0, y1: 1, fillcolor: COL.accent, opacity: 0.10, line: { width: 0 } },
    );
  }
  // `connectgaps: false` is Plotly's default, and it is stated here rather than
  // inherited because the honesty of this chart depends on it: an undefined
  // rate period (roadmap 1.11) arrives as `null`, and a line drawn straight
  // through it would assert a value nobody measured. Same on the RCA tab's
  // chart below.
  Plotly.newPlot(
    "ts-chart",
    [{ x, y, mode: "lines", connectgaps: false, line: { color: COL.accent, width: 1.8 }, hovertemplate: "%{x|%b %d}: %{y:,.1f}<extra></extra>" }],
    {
      margin: { l: 45, r: 8, t: 6, b: 22 },
      height: 200,
      shapes,
      xaxis: { tickfont: { size: 10 }, gridcolor: COL.rule },
      yaxis: { tickfont: { size: 10 }, gridcolor: COL.rule },
      plot_bgcolor: COL.panel,
      paper_bgcolor: COL.panel,
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
      const lo = summary["hdi_2.5%"]?.[key];
      const hi = summary["hdi_97.5%"]?.[key];
      // `[—, —]` is a pair of empty brackets pretending to be an interval;
      // one em dash says the interval is absent, which is the fact.
      const hdi = Number.isFinite(lo) && Number.isFinite(hi) ? `[${fmt(lo)}, ${fmt(hi)}]` : "—";
      rows += `<tr>
        <td><code>${esc(p)}</code></td>
        <td class="num">${fmt(mean)}</td>
        <td class="num">${hdi}</td>
      </tr>`;
    });
  }

  const coefTable = rows
    ? `<p class="table-caption">Holding the other parents fixed, a one-unit rise
         in the parent moves ${esc(name)} by about β. ${hintHTML("beta")}</p>
       ${hintSlot("beta")}
       <table class="data-table">
         <tr><th>Parent</th><th class="num">β (raw units)</th>
             <th class="num">95% HDI ${hintHTML("hdi")}</th></tr>
         ${rows}
       </table>
       ${hintSlot("hdi")}`
    : `<p style="color:var(--muted);font-size:12.5px;margin:4px 0">
         ${def.formula ? "Formula node — the structural relationship is exact; the model fits the residual." : "No causal parents — trend and seasonality only."}</p>`;

  const signWarnings = (data.diagnostics && data.diagnostics.sign_warnings) || [];
  const signWarningHtml = signWarnings
    .map((w) => `<p class="sign-warning">⚠ ${esc(w)}</p>`)
    .join("");

  // Diagnostics. These are MCMC-only, so an ADVI fit renders no numbers — but
  // it used to render *nothing at all*, which reads as "no problems found"
  // when it actually means "not checked". Say which one it is: the absence of
  // a convergence check is itself something the reader needs to know (UC4).
  const dx = data.diagnostics || {};
  const rhats = Object.values(summary.r_hat || {}).filter((v) => v !== null && !Number.isNaN(v));
  const bits = [];
  if (rhats.length) {
    const worst = Math.max(...rhats);
    const cls = worst < 1.05 ? "ok" : "warn";
    // "R̂" ends in a combining circumflex, which eats a following plain space;
    // the explicit "=" keeps the number from colliding with the glyph.
    bits.push(`max R̂ = <span class="${cls}">${worst.toFixed(3)}</span> `
      + `(${worst < 1.01 ? "chains agree" : worst < 1.05 ? "acceptable" : "check convergence"})`);
  }
  if (typeof dx.divergences === "number") {
    bits.push(`${dx.divergences} divergence${dx.divergences === 1 ? "" : "s"}`);
  }
  if (typeof dx.min_ess_bulk === "number" && Number.isFinite(dx.min_ess_bulk)) {
    bits.push(`min ESS ${Math.round(dx.min_ess_bulk).toLocaleString()}`);
  }
  // The engine's own verdict on the fit. It is computed for every fit — NUTS
  // thresholds R̂/divergences/ESS, ADVI checks whether the ELBO settled — and
  // was rendered nowhere in this UI, so a model the engine itself called
  // `suspect` looked exactly like one it was happy with. UC4's whole job is
  // telling a healthy fit from a broken one; a verdict the engine reached and
  // the screen withheld is the most expensive kind of silence here.
  let verdict = "";
  if (dx.fit_quality === "suspect") {
    verdict = `<div class="diag"><span class="warn">⚠ The engine flagged this fit as suspect.</span>
      ${dx.method === "advi"
        ? "The ADVI objective (the ELBO) had not settled by the end of optimization, so the approximation may not have converged on anything."
        : "One of R̂, the divergence count or the effective sample size crossed the engine's threshold."}
      Numbers derived from this fit — coefficients, intervals, and any RCA contribution through this node — inherit that.</div>`;
  } else if (dx.fit_quality === "ok") {
    verdict = `<div class="diag">Engine fit check: <span class="ok">ok</span>.</div>`;
  } else if (dx.fit_quality) {
    // Unknown verdict: shown verbatim rather than swallowed into silence,
    // which would read as "nothing to report".
    verdict = `<div class="diag">Engine fit check: <span class="warn">${esc(dx.fit_quality)}</span>.</div>`;
  }

  let diag = "";
  if (bits.length) {
    diag = `<div class="diag">${bits.join(" · ")} ${hintHTML("diagnostics")}</div>${hintSlot("diagnostics")}`;
  } else if (dx.method === "advi") {
    diag = `<div class="diag">ADVI approximation — <span class="warn">no convergence
      diagnostics</span>; R̂, divergences and ESS are MCMC-only. Re-run with NUTS to
      check the fit. ${hintHTML("diagnostics")}</div>${hintSlot("diagnostics")}`;
  } else if (summary) {
    // A fit exists but reported no diagnostics and no method. Saying nothing
    // here is the exact failure the ADVI branch above was written to fix.
    diag = `<div class="diag">This fit reported <span class="warn">no diagnostics</span>
      — that is the absence of a check, not a clean bill of health.
      ${hintHTML("diagnostics")}</div>${hintSlot("diagnostics")}`;
  }
  diag = verdict + diag;

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
    await api(
      treePath(`/analyze/${encodeURIComponent(name)}?inference_method=${method}&draws=${draws}`),
      { method: "POST" }
    );
    delete state.metricCache[name];
    state.meta = await api(treePath("/meta"));
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
  if (!v("an-start") || !v("an-end")) return null;
  // Auto mode omits the reference params: the server's matched adjacent
  // block is the number of record (the inputs only previewed it), so the JS
  // mirror can never drift the analysis.
  if (state.refMode === "auto") {
    return { analysis_start: v("an-start"), analysis_end: v("an-end") };
  }
  if (!v("ref-start") || !v("ref-end")) return null;
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
    setStatus("Set the window dates first.", "error");
    return;
  }
  const btn = $("run-rca");
  btn.disabled = true;
  const runId = startRunProgress("resolving");
  try {
    const qs = new URLSearchParams({ ...win, run_id: runId }).toString();
    state.slices = {}; // a new analysis invalidates any slice of the old one
    state.rca = await api(treePath(`/rca/${encodeURIComponent(target)}?${qs}`), { method: "POST" });
    // The resolved reference comes back either way; reflect it in the inputs
    // and build the deep link from resolved dates so a defaulted run replays
    // identically even if the server later boots with a different range.
    $("ref-start").value = state.rca.reference_window.start;
    $("ref-end").value = state.rca.reference_window.end;
    const resolvedQs = new URLSearchParams({
      reference_start: state.rca.reference_window.start,
      reference_end: state.rca.reference_window.end,
      analysis_start: state.rca.analysis_window.start,
      analysis_end: state.rca.analysis_window.end,
    }).toString();
    setHash(`rca=${encodeURIComponent(target)}&${resolvedQs}`);
    updateShareMenu();
    state.meta = await api(treePath("/meta")); // on-demand fits may have been cached
    state.metricCache = {};
    markFitted();
    applyRcaOverlay();
    renderRcaTab();
    switchTab("rca");
    $("clear-rca").style.display = "";
    setStatus(`RCA complete for ${target} in ${mmss(Date.now() - runProg.started)}.`, "", 6000);
  } catch (err) {
    setStatus(`RCA failed: ${err.message}`, "error");
  } finally {
    stopRunProgress();
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
    const st = nodeStatus(node);
    let mark = null;
    if (st) {
      // A node the engine could not analyze must not read as one it analyzed
      // and found quiet, and it must not vanish either — a disappeared node is
      // as misleading as a green one. Amber fill + dashed amber border + ⚠,
      // and deliberately NO up/down tint: the good/bad channel is the
      // *attribution* overlay's, and there is no attribution here. The card's
      // delta pill still shows the movement, which is real and measured.
      n.addClass("rca-not-analyzed");
      mark = "⚠";
    } else {
      // No tint when the gap claims no direction (null, or below the engine's
      // own 1e-12 "did not move" threshold). A flat metric rendered green is
      // the legend saying `improved` about a metric that stood still.
      const gd = gapDir(node.gap);
      const tint = gd && goodClass(name, gd, "rca");
      if (tint) n.addClass(tint);
      // large-unexplained badge: dashed amber border + ◌ glyph on the card
      if (
        node.contributions.length &&
        node.unexplained != null &&
        Math.abs(node.gap) > 1e-9 &&
        Math.abs(node.unexplained / node.gap) > 0.35
      ) {
        n.addClass("rca-unexplained");
        mark = "◌";
      }
    }
    // the card shows the RCA change (gap over the two windows) while RCA is on
    state.cardOverlay[name] = {
      value: null, // keep the latest value as the big number
      dpct: node.relative_change,
      // `null >= 0` is true, so a node with no measured gap would tint and
      // arrow *upward* — check for it rather than letting it read as growth.
      // A gap of exactly zero is the same non-claim and gets the same ▬.
      dir: gapDir(node.gap) || "flat",
      mark,
    };
    node.contributions.forEach((c) => {
      const e = cy.getElementById(`${c.parent}->${name}`);
      if (!e.length) return;
      const share = c.share_of_gap == null ? 0 : Math.min(Math.abs(c.share_of_gap), 1);
      e.data("w", 2 + 6 * share);
      // Certainty channel: opacity from prob_same_direction. A *withheld* one
      // (null — the block bootstrap could not produce enough finite
      // replicates, so `ci_95` is null too) used to render at full opacity,
      // i.e. identical to P(direction) = 1.0: "we could not establish the
      // direction" drawn as the most certain edge on the canvas. It now sits
      // at the bottom of the scale, where an unestablished direction belongs.
      e.data("op", c.prob_same_direction == null ? 0.35 : Math.max(0.35, 2 * (c.prob_same_direction - 0.5)));
      e.data("label", c.share_of_gap == null ? "" : pct(Math.abs(c.share_of_gap)));
      // Edge color = the effect's goodness for the CHILD it lands on. A
      // contribution of exactly zero makes no directional claim.
      const ed = gapDir(c.estimate);
      const edgeTint = ed && goodClass(name, ed, "rca");
      if (edgeTint) e.addClass(edgeTint);
    });
  });

  renderAllCards(); // repaint cards with the RCA overlay folded in
  ensureRcaLegendExtras();
  document.querySelectorAll(".rca-only").forEach((el) => (el.style.display = "flex"));
}

/* The ⚠ mark is meaningless without a legend line, and the RCA legend rows are
   toggled together by `.rca-only` — so this row is inserted once, next to the
   ◌ row it must be told apart from, and then follows the same toggle. */
function ensureRcaLegendExtras() {
  const legend = $("legend");
  if (!legend || $("legend-not-analyzed")) return;
  const row = document.createElement("div");
  row.className = "legend-row rca-only";
  row.id = "legend-not-analyzed";
  row.style.display = "none";
  row.innerHTML = '<span class="swatch notanalyzed"></span> ⚠ not analyzed';
  legend.appendChild(row);
}

function clearRcaStyles() {
  const cy = state.cy;
  cy.elements().removeClass("faded rca-up rca-down pathhl rca-unexplained rca-not-analyzed");
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
      const data = await api(treePath(`/metrics/${encodeURIComponent(name)}`));
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
    const ci = row.ci_95 ? `95% CI [${fmt(row.ci_95[0])}, ${fmt(row.ci_95[1])}]` : "interval withheld";
    const p = row.prob_concentrated == null ? "" : ` · P(direction) ${pctDir(row.prob_concentrated, row.prob_concentrated_censored)}`;
    return `<td class="num" title="${esc(`excess ${fmt(row.excess)} · ${ci}${p}`)}">${fmtTight(row.excess)}</td>`;
  };

  /* The engine reserves four dimension values as buckets rather than data:
     rendering them raw made `__null__` look like a category someone named. */
  const SLICE_SENTINEL = {
    __other__: "everything else",
    __null__: "no value (NULL)",
    __absent__: "absent from this window",
    __other_moved__: "moved within “everything else”",
  };
  const sliceLabel = (v) =>
    SLICE_SENTINEL[v] ? `<em>${esc(SLICE_SENTINEL[v])}</em>` : `<code>${esc(v)}</code>`;
  const rows = r.slices
    .map((row, i) => {
      // Concentration is relative to size: lead with the slice carrying more
      // of the gap than its baseline share predicts, and say when the
      // bootstrap cannot separate that from zero.
      const noise = row.noise_level
        ? ' <span class="slice-noise" title="The bootstrap cannot distinguish this slice&#39;s concentration from zero — the gap is not localized here.">noise</span>'
        : "";
      const lead = i === 0 && !row.noise_level ? ' class="slice-lead"' : "";
      const label = `<td>${sliceLabel(row.value)}${noise}${row.n_values ? ` <span class="dim">(${row.n_values})</span>` : ""}</td>`;
      return rate
        ? `<tr${lead}>${label}
             <td class="num" title="the slice's own rate moved: ${esc(fmt(row.within))}">${fmtTight(row.within)}</td>
             <td class="num" title="traffic moved between slices: ${esc(fmt(row.mix))}">${fmtTight(row.mix)}</td>
             ${excessCell(row)}
           </tr>`
        : `<tr${lead}>${label}
             <td class="num" title="contribution ${esc(fmt(row.contribution))}">${row.share_of_gap == null ? "—" : pct(row.share_of_gap)}</td>
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
    ? `<p class="slice-verdict">${sliceLabel(top.value)} carries
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
  // `additivity` has three values and only one of them used to say anything.
  // "unknown" — the default for every provider that cannot inspect its own
  // binding, which is most of them — rendered exactly like "exact": a table of
  // shares presented as a partition of the metric, with nothing to say the
  // partition was never verified. Quiet, because `reconciliation` below is the
  // empirical check and usually passes; but not silent, because "we checked
  // and they partition" and "we could not check" are different facts.
  const overlap =
    r.additivity === "overlapping" && r.overlap
      ? `<p class="inline-status">These slices share entities, so they overstate the
         metric by ${fmt(r.overlap.mean)} on average
         (${pct(r.overlap.share_of_baseline)} of baseline) — deduplication overlap,
         not an unexplained cause. Contribution shares are withheld.</p>`
      : r.additivity === "exact"
        ? ""
        : `<p class="inline-status">The provider could not confirm these slices partition
           <code>${esc(r.metric)}</code> without double-counting (<code>additivity:
           ${esc(r.additivity || "unreported")}</code>), so treat the shares below as
           unverified against the metric's own definition. The reconciliation check
           is the empirical test of it.</p>`;
  const recon =
    r.reconciliation && !["ok", "not_applicable"].includes(r.reconciliation.status)
      ? `<p class="inline-status error">Slices do not sum back to the metric
         (mean residual ${fmt(r.reconciliation.mean_residual)},
         ${pct(r.reconciliation.residual_share_of_baseline)} of baseline) — reported, not rescaled.</p>`
      : "";
  const caveats = (r.caveats || []).length
    ? `<p class="inline-status">${r.caveats.map(esc).join(" ")}</p>`
    : "";
  // Through the same vocabulary as the tree's attribution blocks, so this
  // panel cannot fall behind when the engine gains a `ci_status` value — it
  // used to test one literal and say nothing about anything else.
  const ciNote = ciStatusNote(r.ci_status);
  const degenerate = ciNote
    ? `<p class="inline-status"><span class="ci-flag" title="${esc(ciNote.why)}">${esc(ciNote.text)}</span></p>`
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
    const result = await api(treePath(`/rca/${encodeURIComponent(metric)}/slices?${qs}`), {
      method: "POST",
    });
    state.slices[metric] = { dimension, result, lag: win.lag };
  } catch (err) {
    state.slices[metric] = { dimension, error: err.message, lag: win.lag };
  }
  renderRcaTab();
}

function renderRcaTab() {
  const res = state.rca;
  const target = res.nodes[res.target];
  const targetStatus = nodeStatus(target);

  // The target itself failed. Ranked causes and attribution are computed from
  // *its* contributions, so with none there is nothing to rank: showing the
  // usual sections would present a list of zero-score metrics as an answer.
  if (targetStatus) {
    const gp = gapLineParts(res.target, target.gap);
    const gapBlock =
      target.gap == null
        ? ""
        : `<div class="gap-line ${gp.cls}">${gp.sign}${fmt(target.gap)} <span style="font-size:14px">(${signedPct(target.relative_change)})</span></div>
           <div class="sub">${fmt(target.baseline)} → ${fmt(target.actual)} (${windowBasisHtml(target)})</div>`;
    $("rca-results").innerHTML = `
      <div class="rca-card">
        <div class="sub">${esc(res.target)} · ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)} vs ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</div>
        ${gapBlock}
        <p class="degraded-note"><strong>⚠ ${esc(targetStatus.label)}.</strong>
          ${esc(targetStatus.explains)}
          ${target.status_reason ? `<span class="degraded-reason">${esc(target.status_reason)}</span>` : ""}
          No causes can be ranked for a target with no attribution.</p>
      </div>`;
    return;
  }

  const targetGap = gapLineParts(res.target, target.gap);
  // Every node in scope the engine could not analyze. Naming the count in the
  // summary is the point: a reader who scrolls past one otherwise takes the
  // ranked causes for a complete list, when in fact nothing upstream of a
  // failed node was attributed at all.
  const degraded = Object.entries(res.nodes).filter(([, n]) => nodeStatus(n));
  const degradedNote = degraded.length
    ? `<p class="degraded-note"><strong>⚠ ${degraded.length} of ${Object.keys(res.nodes).length} metrics in scope could not be analyzed</strong>,
        so the ranked causes are incomplete — nothing upstream of them was attributed.
        ${degraded
          .map(([name, n]) => {
            const st = nodeStatus(n);
            const why = `${st.explains}${n.status_reason ? `\n\n${n.status_reason}` : ""}`;
            return `<code title="${esc(why)}">${esc(name)}</code> <span class="degraded-why">(${esc(st.short)})</span>`;
          })
          .join(", ")}. Each is detailed under Attribution detail.</p>`
    : "";

  // The advisories that were on screen when Run was pressed belong with the
  // answer, not only with the form that produced it.
  const windowNote = (state.winAdvisories || []).length
    ? `<p class="degraded-note">ⓘ About these windows: ${esc(state.winAdvisories.join(" · "))}</p>`
    : "";

  const maxScore = Math.max(...res.ranked_causes.map((c) => c.score), 1e-9);
  const causeRows = res.ranked_causes
    .map((c, i) => {
      // The score is honest — it comes from the *child's* attribution of this
      // metric, which succeeded. What failed is this metric's own
      // decomposition, so the row stays and says so; without the flag a
      // top-ranked cause looks like a lead the user can follow upward, and it
      // is a dead end.
      const st = nodeStatus(res.nodes[c.metric]);
      const flag = st
        ? ` <span class="cause-flag" title="${esc(`${st.explains}${res.nodes[c.metric].status_reason ? `\n\n${res.nodes[c.metric].status_reason}` : ""}`)}">⚠ ${esc(st.short)}</span>`
        : "";
      return `
      <div class="cause-row${st ? " degraded" : ""}" data-metric="${esc(c.metric)}">
        <span class="cause-rank">${i + 1}</span>
        <span class="cause-name">${esc(c.metric)}</span>${flag}
        <span class="cause-bar-wrap"><span class="cause-bar" style="width:${(100 * c.score) / maxScore}%"></span></span>
        <span class="cause-via">via ${esc(c.via || "—")}</span>
      </div>
      ${sliceControlsHtml(c.metric)}`;
    })
    .join("");

  // attribution detail: target first, then ranked order
  const order = [res.target, ...res.ranked_causes.map((c) => c.metric)];
  const view = state.rcaView;
  // `null / gap` is 0 in JavaScript, so an unguarded division printed "0.0%"
  // for a numerator the engine withheld. A withheld share is an em dash.
  const shareOf = (v, gap) =>
    v == null || gap == null || Math.abs(gap) <= 1e-12 ? "—" : pct(v / gap);
  const ciCell = (ci) => (ci ? `[${fmt(ci[0])}, ${fmt(ci[1])}]` : "—");
  const anyTwoLevel = order.some((name) => {
    const n = res.nodes[name];
    return n && n.contributions.some((c) => c.decomposition);
  });
  const blocks = order
    .filter((name) => res.nodes[name] && (res.nodes[name].contributions.length || nodeStatus(res.nodes[name])))
    .map((name) => {
      const node = res.nodes[name];
      const st = nodeStatus(node);
      // A failed node gets a block of its own rather than being filtered out.
      // `attribution_method` is null on these, so the old code would have
      // labelled it "posterior" over an empty table — a node that could not be
      // analyzed presented as one that was analyzed and found nothing.
      if (st) {
        const movement =
          node.gap == null
            ? ""
            : `<p class="inline-status">gap <strong>${fmt(node.gap)}</strong> (${node.relative_change == null ? "—" : signedPct(node.relative_change)}) ·
                ${fmt(node.baseline)} → ${fmt(node.actual)} (${windowBasisHtml(node)})</p>`;
        return `
        <div class="attr-block degraded">
          <h4>${esc(name)} <span class="method">· ${esc(st.label)}</span></h4>
          ${movement}
          <p class="degraded-note">${esc(st.explains)}
            ${node.status_reason ? `<span class="degraded-reason">${esc(node.status_reason)}</span>` : ""}</p>
        </div>`;
      }
      const method = esc(attributionLabel(node.attribution_method));
      const ew = node.effective_windows;
      const snapNote =
        node.grain && node.grain !== "day" && ew
          ? ` · ${esc(node.grain)} grain, snapped to ${ew.reference.n_periods}+${ew.analysis.n_periods} whole ${esc(node.grain)}s`
          : "";
      const ci = ciStatusNote(node.ci_status);
      const ciNote = ci
        ? ` · <span class="ci-flag" title="${esc(ci.why)}">${esc(ci.text)}</span>`
        : "";
      // The engine's own verdict on the fit behind these numbers. It is
      // computed for every fit (NUTS: R-hat / divergences / ESS; ADVI: whether
      // the ELBO settled) and was rendered nowhere — so a node whose model the
      // engine itself flagged looked exactly like one it was happy with.
      const fitNote2 =
        node.fit_quality === "suspect"
          ? ` · <span class="sign-flag" title="The engine's own fit check failed for this node's model — for NUTS that is R̂, divergences or effective sample size; for ADVI it is an ELBO that had not settled. The contributions below rest on this fit.">⚠ suspect fit</span>`
          : "";
      const signNote =
        node.sign_warnings && node.sign_warnings.length
          ? ` · <span class="sign-flag" title="${esc(node.sign_warnings.join("\n\n"))}">⚠ learned sign contradicts expectation</span>`
          : "";
      // The fit window is all loaded history before the analysis window —
      // surfacing it here is what keeps it from being confused with the
      // reference window.
      const fitNote = node.fit_window
        ? ` · fitted on ${node.fit_window.n_periods} ${esc(node.grain)}s (${esc(node.fit_window.start)} → ${esc(node.fit_window.end)})`
        : "";
      const seasNote =
        node.seasonality_warnings && node.seasonality_warnings.length
          ? ` · <span class="sign-flag" title="${esc(node.seasonality_warnings.join("\n\n"))}">⚠ seasonality unidentifiable from fitted history</span>`
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
              <td><code>${esc(c.parent)}</code>${lagChip(c, node.grain)}</td>
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
              <td><code>${esc(c.parent)}</code>${lagChip(c, node.grain)}</td>
              <td class="num">${fmt(c.decomposition.means.estimate)}</td>
              <td class="num">${fmt(c.decomposition.comovement.estimate)}</td>
              <td class="num">${fmt(c.estimate)}</td>
              <td class="num">${ciCell(c.ci_95)}</td>
              <td class="num">${pctDir(c.prob_same_direction, c.prob_same_direction_censored)}</td>
            </tr>`,
          )
          .join("");
      } else {
        nCols = 5;
        header = `<tr><th>Parent</th><th class="num">Δ contribution</th><th class="num">share</th><th class="num">95% CI</th><th class="num">P(dir)</th></tr>`;
        rows = node.contributions
          .map(
            (c) => `<tr>
              <td><code>${esc(c.parent)}</code>${lagChip(c, node.grain)}</td>
              <td class="num">${fmt(c.estimate)}</td>
              <td class="num">${c.share_of_gap === null ? "—" : pct(c.share_of_gap)}</td>
              <td class="num">${ciCell(c.ci_95)}</td>
              <td class="num">${pctDir(c.prob_same_direction, c.prob_same_direction_censored)}</td>
            </tr>`,
          )
          .join("");
      }

      // Trend and seasonal are part of the decomposition, not decoration: the
      // engine computes `unexplained = gap − Σcontributions − trend − seasonal`
      // (engine/rca.py). Omitting them here — while the exported report showed
      // them — left a table whose own shares summed to 108.5% of a gap with no
      // visible reason, and made `unexplained` look like the only residual when
      // a real, estimated component was sitting outside the table. A component
      // that is identically zero with a [0, 0] interval is not in the model at
      // all; printing it as a 95% credible interval of exactly zero claims a
      // precision nobody measured, so those rows are dropped instead (they add
      // nothing to the sum either way).
      const componentRows = componentRowsHtml(node, nCols, shareOf, ciCell);

      let unexplained = "";
      const ux = unexplainedRow(node);
      if (ux) {
        const dash = '<td class="num">—</td>';
        const cell = `<td title="${esc(ux.title)}">${esc(ux.label)}</td>`;
        if (nCols === 6) {
          // Detailed view: unexplained sits in the "total Δ" column.
          unexplained = `<tr class="dim">${cell}${dash}${dash}<td class="num">${fmt(node.unexplained)}</td>${dash}${dash}</tr>`;
        } else {
          // A definitional zero has no share of the gap either — dividing an
          // absence by the gap would print "0.0%", which reads as measured.
          const share = ux.definitional ? "—" : shareOf(node.unexplained, node.gap);
          unexplained = `<tr class="dim">${cell}<td class="num">${fmt(node.unexplained)}</td><td class="num">${share}</td>${dash.repeat(nCols - 3)}</tr>`;
        }
      }
      return `
        <div class="attr-block">
          <h4>${esc(name)} <span class="method">· ${method}${fitNote}${snapNote}${ciNote}${fitNote2}${signNote}${seasNote}</span></h4>
          <table class="data-table">
            ${header}
            ${rows}
            ${componentRows}
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
      <div class="sub">${esc(res.target)} · ${esc(res.reference_window.start)} → ${esc(res.reference_window.end)}${res.reference_defaulted ? " (auto)" : ""} vs ${esc(res.analysis_window.start)} → ${esc(res.analysis_window.end)}</div>
      <div class="gap-line ${targetGap.cls}">${targetGap.sign}${fmt(target.gap)} <span style="font-size:14px">(${signedPct(target.relative_change)})</span></div>
      <div id="rca-strip"></div>
      <div class="sub">${fmt(target.baseline)} → ${fmt(target.actual)} (${windowBasisHtml(target)})</div>
      ${windowNote}
      ${degradedNote}
    </div>

    <section>
      <h3>Ranked causes <span class="section-note">triage order, not evidence</span></h3>
      ${causeRows || '<p class="placeholder">No upstream causes — target is a source metric.</p>'}
    </section>

    <section>
      <div class="attr-head">
        <h3>Attribution detail</h3>
        ${viewToggle}
      </div>
      ${blocks || '<p class="placeholder">No attributable edges in scope.</p>'}
    </section>
    <div class="wf-caveats">${RCA_CAVEATS.map(esc).join("<br>")}</div>`;

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
    data = state.metricCache[name] || (await api(treePath(`/metrics/${encodeURIComponent(name)}`)));
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
    { type: "rect", xref: "x", yref: "paper", x0: res.reference_window.start, x1: res.reference_window.end, y0: 0, y1: 1, fillcolor: COL.source, opacity: 0.16, line: { width: 0 } },
    { type: "rect", xref: "x", yref: "paper", x0: res.analysis_window.start, x1: res.analysis_window.end, y0: 0, y1: 1, fillcolor: COL.accent, opacity: 0.10, line: { width: 0 } },
  ];
  Plotly.newPlot(
    el,
    [{ x, y, mode: "lines", connectgaps: false, line: { color: COL.accent, width: 1.6 }, hovertemplate: "%{x|%b %d}: %{y:,.1f}<extra></extra>" }],
    {
      margin: { l: 38, r: 6, t: 4, b: 18 },
      height: 120,
      shapes,
      xaxis: { tickfont: { size: 9 }, gridcolor: COL.rule },
      yaxis: { tickfont: { size: 9 }, gridcolor: COL.rule },
      plot_bgcolor: COL.panel,
      paper_bgcolor: COL.panel,
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
      data = state.metricCache[name] || (await api(treePath(`/metrics/${encodeURIComponent(name)}`)));
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
    <div class="strip-band" style="left:${p(gLo)}%;width:${p(gHi) - p(gLo)}%;background:${COL.track}"></div>
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

async function runWhatif() {
  const scenario = buildScenarioPayload();
  if (!scenario) return;
  const btn = $("wf-run");
  if (btn) btn.disabled = true;
  const runId = startRunProgress("resolving");
  try {
    state.whatif.result = await api(treePath(`/simulate?run_id=${encodeURIComponent(runId)}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scenario),
    });
    setHash(`whatif=${encodeURIComponent(JSON.stringify(scenario))}`);
    updateShareMenu();
    state.meta = await api(treePath("/meta")); // on-demand fits may have been cached
    markFitted();
    renderWhatifTab();
    switchTab("whatif");
    applyWhatifOverlay();
    setStatus(`Simulation complete in ${mmss(Date.now() - runProg.started)}.`, "", 6000);
  } catch (err) {
    setStatus(`Simulation failed: ${err.message}`, "error");
    const b = $("wf-run");
    if (b) b.disabled = false;
  } finally {
    stopRunProgress();
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
  if (hashParams().has("whatif")) {
    // Keep `#tree=`: dropping it would silently move the reader to the
    // default tree on the next reload.
    const prefix = hashPrefix().replace(/&$/, "");
    history.replaceState(
      null,
      "",
      prefix ? `#${prefix}` : location.pathname + location.search
    );
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
      // A simulated delta of exactly zero is not an improvement; withhold the
      // colour rather than letting `>= 0` paint it green.
      const dd = gapDir(node.delta.estimate);
      const dirCls = dd ? goodDir(name, dd) : "";
      const ci = node.delta.ci_95 || [];
      const sub = isCold
        ? `${esc(name)} · cold start — declared beliefs`
        : `${esc(name)} · baseline ${esc(res.baseline_window.start)} → ${esc(res.baseline_window.end)}`;
      const bci = node.baseline_ci_95;
      return `
      <div class="rca-card">
        <div class="sub">${sub}</div>
        <div class="gap-line ${dirCls}">${fmt(node.baseline)} → ${fmt(node.simulated)}
          <span style="font-size:14px">(${signedPct(node.relative_delta)})</span></div>
        <div class="wf-ci">Δ ${fmt(node.delta.estimate)} · 95% CI [${fmt(ci[0])}, ${fmt(ci[1])}] · P(direction) ${pctDir(node.prob_direction, node.prob_direction_censored)}${
          bci ? ` · baseline belief [${fmt(bci[0])}, ${fmt(bci[1])}]` : ""
        }</div>${
          node.fit_quality === "suspect"
            ? `<div class="wf-warning">⚠ The engine flagged the model behind <code>${esc(name)}</code> as suspect (ADVI: the ELBO had not settled; NUTS: R̂ / divergences / ESS over threshold). This outcome is propagated through that fit.</div>`
            : ""
        }
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
        <td><code>${esc(n)}</code>${node.status === "intervened" ? ' <span class="chip lag">⊙ set</span>' : ""}${node.extrapolation && node.extrapolation.flag ? " ⚠" : ""}${
          node.fit_quality === "suspect"
            ? ' <span class="cause-flag" title="The engine flagged the model behind this node as suspect — for ADVI, an ELBO that had not settled; for NUTS, R̂ / divergences / ESS over threshold. This row is propagated through that fit.">⚠ suspect fit</span>'
            : ""
        }</td>
        <td class="num">${fmt(node.baseline)} → ${fmt(node.simulated)}</td>
        <td class="num">${signedPct(node.relative_delta)}</td>
        <td class="num">[${fmt(ci[0])}, ${fmt(ci[1])}]</td>
        <td class="num">${pctDir(node.prob_direction, node.prob_direction_censored)}</td>
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
  const defaultPreset = available.has("last7") ? "last7" : available.has("last14") ? "last14" : "custom";
  presetSelect.value = defaultPreset;

  // Date inputs, analysis-first: editing the analysis window flips the
  // preset to Custom and (in auto mode) recomputes the reference; editing a
  // reference date makes the reference custom. The auto chip restores the
  // matched adjacent block.
  const bounds = { min: state.meta.date_start, max: state.meta.date_end };
  ["ref-start", "ref-end", "an-start", "an-end"].forEach((id) => {
    const el = $(id);
    el.min = bounds.min;
    el.max = bounds.max;
  });
  ["an-start", "an-end"].forEach((id) => {
    $(id).addEventListener("change", () => {
      presetSelect.value = "custom";
      applyAutoReference();
      validateWindows();
    });
  });
  ["ref-start", "ref-end"].forEach((id) => {
    $(id).addEventListener("change", () => {
      setRefMode("custom");
      validateWindows();
    });
  });
  $("ref-auto").addEventListener("click", () => {
    setRefMode("auto");
    validateWindows();
  });
  // A different target can change the auto reference (its ancestor scope
  // decides week alignment and the coarsest grain).
  select.addEventListener("change", () => {
    applyAutoReference();
    validateWindows();
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
  const params = hashParams();
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
    // Links carrying explicit reference dates replay exactly (custom mode);
    // analysis-only links let the server pick the reference (auto mode).
    setRefMode(params.get("reference_start") ? "custom" : "auto");
    runRCA();
  } else if (metric && state.meta.metrics.includes(metric)) {
    selectMetric(metric);
  }
}

/* History nudge: when the provider reports history before the loaded
   --start-date, say so once in the RCA setup panel — the fit trains on
   everything loaded, so more history is a free upgrade. Discovery runs in
   the background server-side, so retry briefly if /meta hasn't filled yet. */
function renderHistoryNudge(attempt = 0) {
  const known = Object.entries(state.meta.earliest_available || {}).filter(([, d]) => d);
  if (!known.length) {
    if (attempt < 3) {
      setTimeout(async () => {
        try {
          state.meta = await api(treePath("/meta"));
        } catch { return; }
        renderHistoryNudge(attempt + 1);
      }, 2500);
    }
    return;
  }
  const older = known.filter(([, d]) => d < state.meta.date_start);
  if (!older.length || $("history-nudge")) return;
  const oldest = older.map(([, d]) => d).sort()[0];
  const p = document.createElement("p");
  p.id = "history-nudge";
  p.className = "inline-status";
  p.textContent = `ⓘ Provider has history back to ${oldest}; loaded from ${state.meta.date_start}. Restart with an earlier --start-date to train on more.`;
  $("rca-setup").appendChild(p);
}

function showDegradedBanner(error) {
  const banner = document.createElement("div");
  banner.id = "degraded-banner";
  const back = state.trees.length > 1 ? ` <a href="#" id="degraded-back">All trees</a>.` : "";
  banner.innerHTML =
    `<strong>breakdown started without data.</strong> ${esc(error)}<br>` +
    `Fix the tree or provider config, then restart. ` +
    `Diagnose with <code>breakdown doctor --tree &lt;tree.yml&gt;</code>.${back}`;
  document.body.prepend(banner);
  const link = $("degraded-back");
  if (link) link.addEventListener("click", () => navigateToTree(null));
}

/* ---------- The tree index (roadmap 2.16) ----------
   One process can serve several trees, and they are peers. A company might
   keep one wide tree with revenue at the top, a marketing tree that goes deep
   on channels and campaigns, a product tree about feature adoption and its
   effect on retention, and a tree standing behind a specific goal. Any of them
   may be durable or disposable, and any may carry a goal or not — the index
   makes no claim about which kind a tree is.

   So the cards sit in **one flat grid** rather than grouped by `period`:
   grouping by time would file every tree that isn't time-bound under "other",
   which is the opposite of the point. `period` is one optional label on a
   card, beside the owner.

   It renders `GET /trees`, which reads parsed YAML only and never triggers a
   load. That is what makes a `not loaded` card honest rather than lazy: the
   alternative (fetch everything to fill the index) is the exact cost lazy
   loading exists to avoid. */

/* The derived, uncertain half of a card that declares a goal. Deliberately two
   facts and no verdict: `period` is a free-form label rather than a parsed
   range, so the *start* of the window is not in the tree at all and "on track"
   / "behind" would be a forecast the engine never made. Share of target and
   days left are both checkable; the reader does the arithmetic the data
   supports. A tree with a target and no deadline simply shows the first. */
function goalPace(card) {
  const bits = [];
  const goal = card.goal || {};
  const prog = card.progress;
  if (prog && goal.target) {
    const share = goal.target === 0 ? null : prog.current / goal.target;
    if (share !== null) bits.push(`${Math.round(share * 100)}% of target`);
  }
  if (goal.deadline) {
    const days = Math.round(
      (new Date(`${goal.deadline}T00:00:00Z`) - new Date(isoUTC(new Date()) + "T00:00:00Z")) /
        DAY_MS
    );
    bits.push(days >= 0 ? `${days} days left` : `deadline passed ${goal.deadline}`);
  }
  return bits.join(" · ");
}

function goalBarHtml(card) {
  const goal = card.goal || {};
  const prog = card.progress;
  const target =
    goal.target === null || goal.target === undefined
      ? ""
      : `<span class="goal-target"> / ${esc(fmt(goal.target))}</span>`;
  if (!prog) {
    // §2.3: never a blank that reads as zero, and never a stale number
    // presented as live. A dash, the reason, and a way to fix it.
    // `state` has four values, not two: a tree mid-load is not one nobody has
    // opened, and saying so sent the reader looking for a button that is not
    // on the card.
    const why =
      card.state === "error"
        ? "this tree failed to load"
        : card.state === "loading"
          ? "loading now — its number will appear when the load finishes"
          : card.state === "loaded"
            ? "loaded, but this goal's metric has no completed period to read yet"
            : "not loaded — nobody has opened this tree in this process yet";
    return (
      `<div class="goal-row"><span class="goal-dash">—</span>${target}</div>` +
      `<p class="goal-pace muted">${esc(why)}</p>`
    );
  }
  const share = goal.target ? prog.current / goal.target : null;
  // How much of the goal is *achieved*, which is not `current / target` when
  // the goal is to push a metric DOWN: churn at 5% against a 2% target is 250%
  // of target and 0% achieved, and the bar used to sit clamped at full — the
  // picture of success on a goal being missed by a factor of two and a half.
  const achieved =
    share === null
      ? null
      : goal.direction === "down"
        ? prog.current > 0
          ? goal.target / prog.current
          : 1
        : share;
  const bar =
    achieved === null
      ? ""
      : `<div class="goal-bar"><span class="goal-fill ${goal.direction === "down" ? "down" : "up"}"` +
        ` style="width:${(Math.max(0, Math.min(1, achieved)) * 100).toFixed(1)}%"></span></div>`;
  return (
    bar +
    `<div class="goal-row"><strong>${esc(fmt(prog.current))}</strong>${target}` +
    `<span class="goal-asof muted"> · as of ${esc(prog.as_of)}</span></div>` +
    `<p class="goal-pace muted">${esc(goalPace(card))}</p>`
  );
}

function treeCardHtml(card) {
  const meta = [card.owner, card.period].filter(Boolean).map(esc).join(" · ");
  let body;
  if (card.state === "error") {
    body =
      `<p class="tree-error">${esc(card.load_error || "This tree failed to load.")}</p>` +
      `<p class="muted">Diagnose with <code>breakdown doctor --tree &lt;tree.yml&gt;</code>.</p>`;
  } else if (card.goal) {
    body = goalBarHtml(card);
  } else {
    // A tree without a goal is not a failed goal card. The wide revenue tree,
    // a marketing tree detailing channels, a product tree about feature
    // adoption — none of them owes anyone a target.
    body = `<p class="tree-plain">${card.metric_count} metrics · ${esc(card.provider || "—")}</p>`;
  }
  const load =
    card.state === "not_loaded"
      ? `<button class="tree-load" data-load="${esc(card.id)}">Load</button>`
      : "";
  return (
    `<article class="tree-card${card.state === "error" ? " errored" : ""}">` +
    `<h3><button class="tree-open" data-open="${esc(card.id)}"${card.state === "error" ? " disabled" : ""}>${esc(card.title)}</button></h3>` +
    (card.description ? `<p class="tree-desc">${esc(card.description)}</p>` : "") +
    (meta ? `<p class="tree-meta muted">${meta}</p>` : "") +
    body +
    `<div class="tree-actions"><span class="tree-state ${esc(card.state)}">${esc(card.state.replace("_", " "))}</span>${load}</div>` +
    `</article>`
  );
}

function renderIndex() {
  const host = $("index");
  host.innerHTML =
    `<div class="index-head"><h1>Metric trees</h1>` +
    `<p class="muted">${state.trees.length} tree${state.trees.length === 1 ? "" : "s"}` +
    ` · data loads when you open one</p></div>` +
    `<div class="tree-cards">${state.trees.map(treeCardHtml).join("")}</div>`;
  host.querySelectorAll("[data-open]").forEach((btn) =>
    btn.addEventListener("click", () => navigateToTree(btn.dataset.open))
  );
  host.querySelectorAll("[data-load]").forEach((btn) =>
    btn.addEventListener("click", () => loadTreeFromIndex(btn))
  );
  $("index").style.display = "";
  $("main").style.display = "none";
  document.body.classList.add("index-view");
  setContext([{ text: `${state.trees.length} trees` }]);
  setStatus("");
}

/* The Load affordance: fetch this one tree's data and repaint its card. The
   index stays where it is — someone comparing several trees should not be
   thrown into one of them just for asking what its number is. */
async function loadTreeFromIndex(btn) {
  const id = btn.dataset.load;
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const card = await api(`/trees/${encodeURIComponent(id)}/load`, { method: "POST" });
    state.trees = state.trees.map((t) => (t.id === id ? card : t));
  } catch (err) {
    setStatus(`Could not load ${id}: ${err.message}`, "error");
  }
  renderIndex();
}

/* Switching trees reloads the page rather than resetting state in place.
   Everything keyed on metric names — the DAG, series, metric cache, RCA,
   what-if, card overrides, the RCA window inputs — belongs to one tree, and
   the boot path already binds its listeners once. A reload is the version of
   "re-run init()" with no way to leave a stale listener or half-cleared canvas
   behind, and the whole view is in the URL, so nothing is lost. */
function navigateToTree(id) {
  location.hash = id ? `tree=${encodeURIComponent(id)}` : "";
  location.reload();
}

/* A `#tree=` that arrives *after* load — pasted into the address bar, or the
   back button — changes which tree every cache below belongs to, so it reloads
   for the same reason switching does. Our own hash writes go through
   `history.replaceState`, which fires no hashchange, so this only ever sees a
   real navigation. */
window.addEventListener("hashchange", () => {
  if (state.trees.length < 2) return;
  const wanted = hashParams().get("tree") || null;
  if (wanted !== (state.tree || null)) location.reload();
});

function initTreeSwitcher() {
  const wrap = $("tree-switch");
  if (state.trees.length < 2) return;
  wrap.style.display = "";
  const select = $("tree-select");
  select.innerHTML = state.trees
    .map(
      (t) =>
        `<option value="${esc(t.id)}"${t.id === state.tree ? " selected" : ""}${t.state === "error" ? " disabled" : ""}>${esc(t.title)}</option>`
    )
    .join("");
  select.addEventListener("change", () => navigateToTree(select.value));
  // The brand doubles as the way back to the index once there is one.
  const brand = $("brand");
  brand.classList.add("clickable");
  brand.title = "All trees";
  brand.addEventListener("click", () => navigateToTree(null));
}

async function init() {
  setStatus("Loading…", "busy");
  // Checked before the first request, so a blocked CDN is diagnosed as a
  // blocked CDN rather than surfacing later as a ReferenceError inside
  // buildGraph() that used to be reported as an unreachable server.
  const missing = missingLibs();
  if (missing.length) {
    showLibraryBanner(missing);
    setStatus(`Missing browser dependency: ${missing.map((l) => l.name).join(", ")}`, "error");
    return;
  }
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
    // `/trees` is the one call made before a tree is chosen. It is also
    // cheap by contract (parsed YAML, no provider), so it costs a cold boot
    // nothing. /health reports the *default* tree, so a degraded default must
    // not hide an index full of healthy siblings.
    try {
      const index = await api("/trees");
      state.trees = index.trees;
      state.defaultTree = index.default;
    } catch {
      state.trees = [];
    }
    if (!state.trees.length) {
      showDegradedBanner(health.error || "No metric tree was discovered.");
      setStatus("No data loaded", "error");
      return;
    }

    // `#tree=` is read first and gates everything else: #metric=/#rca=/#whatif=
    // are meaningless without knowing whose metric names they refer to. No
    // `#tree=` means the index — except with a single tree, where an index of
    // one card is a toll booth and `/ui` opens the tree exactly as it always
    // did.
    const wanted = hashParams().get("tree");
    if (wanted && !state.trees.some((t) => t.id === wanted)) {
      renderIndex();
      setStatus(`No tree '${wanted}' on this server.`, "error");
      return;
    }
    state.tree = wanted || (state.trees.length === 1 ? state.trees[0].id : null);
    if (!state.tree) {
      renderIndex();
      return;
    }
    initTreeSwitcher();
    const card = state.trees.find((t) => t.id === state.tree);
    if (card.state === "error") {
      showDegradedBanner(card.load_error);
      setStatus("No data loaded", "error");
      return;
    }
    if (state.trees.length === 1 && health.status !== "ok") {
      showDegradedBanner(health.error);
      setStatus("No data loaded", "error");
      return;
    }
    setStatus(card.state === "loaded" ? "Loading…" : `Loading ${card.title}…`, "busy");
    [state.meta, state.dag] = await Promise.all([
      api(treePath("/meta")),
      api(treePath("/dag")),
    ]);
    // Series hydrates every node card in one request; degrade to name-only
    // nodes if it fails rather than blocking the whole graph. A cold-start
    // tree has no series at all — cards render from declared baselines.
    if (!coldStart()) {
      try {
        state.series = await api(treePath("/series"));
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
      setContext([
        { text: `${state.meta.metrics.length} metrics` },
        {
          text: "cold start",
          cls: "warn",
          title:
            "This tree declares no data provider. What-if runs over the declared " +
            "beliefs in the YAML; there is no history to analyse.",
        },
      ]);
      setStatus("");
      switchTab("whatif");
    } else {
      const chips = [
        { text: `${state.meta.metrics.length} metrics` },
        { text: state.meta.provider, title: "Data provider" },
        {
          text: `${state.meta.date_start} → ${state.meta.date_end}`,
          title: "Loaded data window (set at startup by --start-date / --end-date)",
        },
      ];
      if (state.asOf < state.meta.date_end) {
        chips.push({
          text: `data → ${state.asOf}`,
          cls: "warn",
          title:
            `The tree-wide data edge lags the loaded window: at least one metric ` +
            `has no data after ${state.asOf}, so cards anchor there.`,
        });
      }
      setContext(chips);
      setStatus("");
      renderHistoryNudge();
    }
    applyDeepLink();
    updateShareMenu();
  } catch (err) {
    // Distinguish "could not reach the server" from "the server answered with
    // a problem": the first is worth another click, the second is not.
    if (err.status === 503) {
      // A lazily-loaded tree that failed on *this* request — its provider is
      // unreachable, its credentials are wrong. Same banner as a degraded
      // startup, because it is the same condition arriving later.
      showDegradedBanner(err.message);
      setStatus("No data loaded", "error");
    } else if (isTransient(err)) {
      setStatus("Could not reach the server — it may still be starting.", "error");
      showRetryBanner();
    } else {
      setStatus(`Failed to load: ${err.message}`, "error");
    }
  }
}

/* A blocked CDN is not an outage. Red like `#degraded-banner` because, like a
   misconfigured provider, it is a condition the operator has to fix — but the
   text names the library and the host so the fix is obvious, instead of
   sending someone to check a server that is answering perfectly. */
function showLibraryBanner(libs) {
  if ($("library-banner")) return;
  const banner = document.createElement("div");
  banner.id = "library-banner";
  const list = libs.map((l) => `<code>${esc(l.name)}</code> (${esc(l.host)})`).join(", ");
  const hosts = [...new Set(libs.map((l) => l.host))].map(esc).join(", ");
  banner.innerHTML =
    `<strong>A browser dependency did not load.</strong> ${list}. ` +
    `The server is fine — these come from a CDN, and something between this ` +
    `browser and it (a corporate proxy, an ad blocker, an offline network, or ` +
    `a failed integrity check) stopped them. Allow <code>${hosts}</code>, or ` +
    `open this instance from a network that can reach them.`;
  document.body.prepend(banner);
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

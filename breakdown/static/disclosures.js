/* disclosures.js — the fifth-rule surface, on one screen.

   Every table and helper that turns an engine verdict into words a reader
   sees: node/interval/fit statuses, the sampler axis, the k̂ / collinearity /
   PPC vocabularies, the unexplained-row and window-basis wording, the RCA
   caveats, and the direction/goodness mapping (gapDir and friends). Split out
   of app.js (roadmap grill 2026-08-29, "the single-file frontend question"):
   three render surfaces sat 2,300+ lines apart and no reviewer held all of
   them on screen, which is how `fit_quality` drifted into four wordings (C37)
   and the sampler reached none of them (C35). "Did every renderer get this
   field?" should be a diff you can read.

   A classic script, loaded by index.html BEFORE app.js — no build step, no
   modules; top-level const/function share the global lexical environment.
   Definitions here may reference app.js globals (esc, fmt, state, …): they
   resolve at call time, after app.js has loaded. Nothing here may run at the
   top level. */

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
  frame_unavailable: {
    label: "not analyzed — no aligned data at this grain",
    short: "no aligned frame",
    explains: "This metric's series and its parents' share no whole period at its grain over the loaded window (for example, a monthly node whose daily parent covers no whole month), so there is nothing to measure and nothing was fitted. The reason names the metrics and the grain.",
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
    text: "intervals withheld: the resampling cannot move",
    why: "At least one parent — or, on the slice panel, a slice — holds the same value across the whole window: an unlaunched feature, a stock held flat, a seasonal business's off-season. Every bootstrap replicate then resamples the same number and the interval would come out exactly zero-width. A zero-width interval is not certainty, it is the absence of information, so it is withheld (roadmap C4/C30).",
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

/* The sampler axis (roadmap C35, ui_design_spec "posterior · ADVI"): since S2
   made NUTS the default and ADVI an explicit request, "posterior" alone hides
   the one fact that makes two colleagues' differing numbers legible. Absent
   `inference_method` renders bare "posterior" — a formula node, or an engine
   too old to say. */
const SAMPLER_LABEL = { nuts: "NUTS", advi: "ADVI", fullrank_advi: "full-rank ADVI" };

function attributionLabel(method, inferenceMethod) {
  if (!method) return "attribution method not reported";
  const base = ATTRIBUTION_LABEL[method] || `attribution method: ${method}`;
  if (method === "posterior" && inferenceMethod) {
    return `${base} · ${SAMPLER_LABEL[inferenceMethod] || inferenceMethod}`;
  }
  return base;
}

/* The node-header form: the label plus, on a *clean* ADVI fit, the k̂ figure.
   docs/ui-guide.md promises "every node it fits reports its PSIS k̂", and a
   clean k̂ produced no chip (khatNote returns null for `ok` — nothing wrong,
   nothing to warn about), so the promise held only for suspect fits (grill
   H7). When khatNote *does* fire, the chip already carries the figure and
   repeating it here would print it twice. */
function attributionLabelForNode(node) {
  const label = attributionLabel(node.attribution_method, node.inference_method);
  if (
    node.inference_method &&
    node.inference_method !== "nuts" &&
    !khatNote(node) &&
    khatFigure(node)
  ) {
    return `${label} (k̂ ${khatFigure(node)})`;
  }
  return label;
}

/* The engine's own verdict on a fit, in one place (roadmap C37, grill M12).
   Five surfaces each hand-wrote this sentence and drifted into four wordings
   — ADVI-first in two, NUTS-first in two, no cause at all in one — and every
   one tested `=== "suspect"`, so a third verdict the engine grows would have
   rendered as silence: exactly the failure ciStatusNote/khatNote/nodeStatus
   each carry a fallback for. Cause-aware where the node's fields allow:
   `severe` PPC is the only thing that can set `suspect` on the NUTS default,
   so it gets the model-not-sampler wording (roadmap S3); the Metric tab keeps
   its richer diagnostics-side version, which can also see k̂ figures.
   Surfaces append their own one-line consequence ("the contributions below
   rest on this fit") — the *cause* is what must not drift. */
function fitQualityNote(node) {
  if (!node || !node.fit_quality || node.fit_quality === "ok") return null;
  if (node.fit_quality === "suspect") {
    if (node.ppc_status === "severe") {
      return {
        text: "⚠ suspect fit — the model, not the sampler",
        why:
          "Series simulated from this node's fitted model do not look like the " +
          "series it was fitted on, so the likelihood is wrong for this metric — " +
          "the model check beside this one says which summary failed. The sampler " +
          "may well have converged; that is a different question. Read direction " +
          "rather than magnitude.",
      };
    }
    return {
      text: "⚠ suspect fit",
      why:
        "The engine's own fit check failed for this node's model — for NUTS " +
        "(the default) that is R̂, divergences or effective sample size; for " +
        "ADVI it is an ELBO that had not settled, or a PSIS k̂ saying the " +
        "approximation is far from the posterior.",
    };
  }
  return {
    text: `fit flagged: ${node.fit_quality}`,
    why:
      "This build does not recognise that fit verdict, so it is shown verbatim. " +
      "It is not 'ok' — the engine flagged this fit, and a newer version of the " +
      "UI would explain it. Treat numbers resting on it as qualified.",
  };
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

/* The PSIS k̂ verdict on a variational fit (engine: roadmap S2).
   `fit_quality: "suspect"` already flags a bad approximation, but it flags an
   unconverged optimizer with the same word, and the two have different
   remedies — one is "run the optimizer longer", the other is "this sampler
   cannot represent this posterior, use the exact one". So k̂ gets its own chip,
   in three states — four, counting S22's `borderline` below — plus the
   unknown-status fallback ciStatusNote pioneered: a status this build cannot
   name is shown verbatim rather than silently treated as fine.

   Every state here is a warning, because a k̂ only exists at all when the fast
   approximation was deliberately asked for — NUTS is the default and has no
   k̂. Returns null for `ok` and for a node with no k̂ (a NUTS fit, a formula
   node): nothing to say is the honest render there, and it is the common
   case.

   One exception, and it is roadmap S22's: an `ok` k̂ within one Monte-Carlo
   standard error of the 0.5 edge (`khat_borderline`) is not a clean verdict,
   and rendering nothing there would hand the reader the one thing the estimate
   cannot support. So `khatNote` takes the *node* rather than the bare status —
   the flag is what decides, and a function given only the status could not see
   it. */
const KHAT_NOTE = {
  borderline: {
    text: "⚠ approximation check inconclusive",
    cls: "sign-flag",
    why: "PSIS k̂ landed inside the good band (≤ 0.5), but within one Monte-Carlo standard error of the edge — k̂ is itself estimated from a finite sample of importance ratios, and another sample would plausibly land on the other side. This fit is not shown to be close to its posterior; it is only not shown to be far from it. Re-fit with NUTS for anything that turns on the difference.",
  },
  suspect: {
    text: "⚠ approximation off",
    cls: "sign-flag",
    why: "PSIS k̂ is above 0.5: the ADVI approximation sits measurably away from the posterior it approximates, so the importance ratios against the true posterior have no finite variance. Read this node's intervals as approximate.",
  },
  unusable: {
    text: "⚠ approximation not usable",
    cls: "sign-flag",
    why: "PSIS k̂ is above 0.7: the ADVI approximation is not close to the posterior and cannot be corrected by reweighting. Its credible intervals are not evidence about how wide the real ones are. This node was approximated because the run asked for it — re-run without the fast approximation, or re-fit this node with NUTS from the Analyze panel, before relying on it.",
  },
  unavailable: {
    text: "approximation unchecked",
    cls: "sign-flag",
    why: "The engine could not compute PSIS k̂ for this fit, so how close the approximation is to the posterior is unknown. That is the absence of a check, not a clean bill of health.",
  },
};

function khatNote(node) {
  const status = node && node.khat_status;
  if (!status) return null;
  if (status === "ok") return node.khat_borderline ? KHAT_NOTE.borderline : null;
  const base =
    KHAT_NOTE[status] || {
      text: `approximation check: ${status}`,
      cls: "sign-flag",
      why: "This build does not recognise that approximation status, so it is shown verbatim. It is not 'ok' — a newer engine flagged something about the fit behind this node that this UI cannot explain yet.",
    };
  // A flagged band that the estimate cannot separate from its neighbour is
  // still that band — the status keeps its meaning — but the reader is told
  // the edge is inside the error, not outside it.
  if (!node.khat_borderline) return base;
  return {
    ...base,
    why: `${base.why} And k̂ sits within one Monte-Carlo standard error of a band edge, so which of the two adjacent bands this fit is in has not been resolved — read the worse of them.`,
  };
}

/* k̂ with its own error: "1.23 ± 0.24", or just "1.23" where the engine could
   not estimate the error. Never "1.23" where it could — an estimate printed
   bare is read as exact, which is the whole of roadmap S22. */
function khatFigure(node) {
  const k = fmtKhat(node && node.khat);
  if (!k) return null;
  const se = node && typeof node.khat_se === "number" && Number.isFinite(node.khat_se)
    ? node.khat_se
    : null;
  return se === null ? k : `${k} ± ${se.toFixed(2)}`;
}

/* The k̂ verdict as an inline chip (what-if table) and as a block (what-if
   card). Shared so the table and the card cannot say different things about
   the same node — the drift that let the export carry component rows the live
   table lacked.

   Three of the four labels carry their own ⚠ and `unavailable` does not, so
   anything that prefixes a glyph strips first: `khatLabel` is the one place
   that decides, and "⚠ ⚠ approximation not usable" is what happens without
   it. */
function khatLabel(kn) {
  return kn.text.replace(/^⚠\s*/, "");
}

function khatChipHtml(node) {
  const kn = khatNote(node);
  if (!kn) return "";
  const title = kn.why + (node.khat_warnings || []).map((w) => `\n\n${w}`).join("");
  return ` <span class="cause-flag" title="${esc(title)}">${esc(kn.text)}</span>`;
}

function khatBlockHtml(name, node) {
  const kn = khatNote(node);
  if (!kn) return "";
  const body = (node.khat_warnings || []).length
    ? node.khat_warnings.map((w) => esc(w)).join(" ")
    : esc(kn.why);
  return `<div class="wf-warning">⚠ ${esc(khatLabel(kn))} for <code>${esc(name)}</code>: ${body}</div>`;
}

/* k̂ formatted for display: it ranges over roughly (−1, ∞) and the demo trees
   produce values from −0.79 to 10.2, so two decimals everywhere and no
   thousands separator. A non-finite k̂ never reaches here — the engine
   withholds it and sends `khat_status: "unavailable"` instead — but a null
   still does (an `unavailable` fit, whose k̂ is null by construction), and a
   literal "null" printed as a diagnostic would be worse than silence. */
function fmtKhat(k) {
  return typeof k === "number" && Number.isFinite(k) ? k.toFixed(2) : null;
}

/* Roadmap S4's verdict on a node's parents, shared by the RCA table, the
   metric card and the static export so the three cannot disagree.

   Deliberately absent on `"ok"` and on a null status, and those two are not
   the same fact: `"ok"` means the check ran and the parents are separable,
   null means there was nothing to check (a formula node, one parent, or no
   fit). The metric card prints the `"ok"` case explicitly for the same reason
   it prints the convergence numbers — see `renderPosterior`. What must never
   happen is a `"high"` node rendering like a clean one, which is the shape of
   the `null >= 0` overlay bug the fifth rule exists for. */
const COLLIN_NOTE = {
  moderate: {
    text: "⚠ parents move together — the split is softer than the total",
    cls: "sign-flag",
    why:
      "Two or more of this node's parents move largely together over the window it was "
      + "fitted on. The data determines their combined effect better than the division of "
      + "it between them, so the pair's total is the sound number here and the split "
      + "between them is the soft one. Read the two as one cause, and do not rank them "
      + "against each other on a small difference in share.",
  },
  high: {
    text: "⚠ parents collinear — the split between them is not determined",
    cls: "sign-flag",
    why:
      "Two or more of this node's parents move together over the window it was fitted on. "
      + "The model determines their combined effect much better than the division of it "
      + "between them, so each parent's own contribution and share of the gap is the least "
      + "stable number here — read the pair as one cause, and do not rank them against "
      + "each other.",
  },
  unavailable: {
    text: "⚠ collinearity unchecked",
    cls: "sign-flag",
    why:
      "The engine could not check whether this node's parents are separable. That is an "
      + "unchecked design, not a clean one: if two of them restate each other, the "
      + "per-parent split below is arbitrary and nothing here will say so.",
  },
};

function collinearityNote(status) {
  if (!status || status === "ok") return null;
  return (
    COLLIN_NOTE[status] || {
      text: `⚠ collinearity check: ${status}`,
      cls: "sign-flag",
      why:
        "This build does not recognise that collinearity status, so it is shown verbatim. "
        + "It is not 'ok' — a newer engine flagged something about how separable this "
        + "node's parents are that this UI cannot explain yet.",
    }
  );
}

/* Max |r| for display. Null is a real state — an `unavailable` check has no
   number — and a literal "null" printed beside a diagnostic is worse than
   printing nothing. */
function fmtCorr(r) {
  return typeof r === "number" && Number.isFinite(r) ? r.toFixed(2) : null;
}

/* "which parents", in the smallest space there is — the flag itself, so a
   reader scanning a wide RCA does not have to hover every node to find the
   pair. Names only the worst pair (or the worst VIF-flagged parent when the
   finding is a multi-way one no single pair shows); the tooltip carries the
   rest. Returns escaped HTML. */
function collinPairText(node) {
  const c = node.collinearity;
  if (!c) return "";
  const pair = (c.pairs || [])[0];
  if (pair) {
    const r = fmtCorr(pair.correlation);
    return ` (${pair.parents.join(" ↔ ")}${r ? `, r ${r}` : ""})`;
  }
  const v = (c.vif || [])[0];
  if (v) {
    return ` (${v.parent}${v.vif == null ? ", not identified" : `, VIF ${v.vif.toFixed(1)}`})`;
  }
  return "";
}

function collinPairSuffix(node) {
  return esc(collinPairText(node));
}

/* The S4 verdict as an inline chip (what-if table) and as a block (what-if
   card), shared for the same reason the k̂ pair beside them is: the table and
   the card must not say different things about the same node. A what-if node
   carries the verdict and its sentences but not the numbers — `collinPairText`
   is a no-op there, and the sentence names the parents anyway. */
function collinChipHtml(node) {
  const cn = collinearityNote(node.collinearity_status);
  if (!cn) return "";
  const title = cn.why + (node.collinearity_warnings || []).map((w) => `\n\n${w}`).join("");
  return ` <span class="cause-flag" title="${esc(title)}">${esc(cn.text)}${collinPairSuffix(node)}</span>`;
}

function collinBlockHtml(name, node) {
  const cn = collinearityNote(node.collinearity_status);
  if (!cn) return "";
  const body = (node.collinearity_warnings || []).length
    ? node.collinearity_warnings.map((w) => esc(w)).join(" ")
    : esc(cn.why);
  return `<div class="wf-warning">⚠ ${esc(cn.text.replace(/^⚠\s*/, ""))} for <code>${esc(name)}</code>: ${body}</div>`;
}

/* Roadmap S3. Same three-state reading as the collinearity note beside it:
   `"ok"` means the check ran and the model reproduces the data it was fitted
   on, null means there was nothing to check (a formula node, or no fit). The
   metric card prints the `"ok"` case explicitly, for the same reason it prints
   R-hat — silence there could not be told from a check that never ran.

   The distinction this note has to carry, and the one collinearity does not:
   `severe` is a statement that the *model is wrong for the data*, so it also
   sets `fit_quality: "suspect"`. `moderate` is a caveat on a usable fit. */
const PPC_NOTE = {
  moderate: {
    text: "⚠ the model reproduces its own data imperfectly",
    cls: "sign-flag",
    why:
      "Simulating series from this node's fitted model and comparing them with what was "
      + "actually observed, at least one summary of the real series sits outside the bulk "
      + "of what the model generates. The fit is usable and this is a caveat on it, not a "
      + "verdict against it — but the model is an imperfect description of this metric.",
  },
  severe: {
    text: "⚠ the model cannot generate this node's own data",
    cls: "sign-flag",
    why:
      "Series simulated from this node's fitted model do not look like the series it was "
      + "fitted on. The usual causes are a Gaussian likelihood on a quantity that cannot go "
      + "negative, a heavy-tailed series, or structure the mean function is leaving in the "
      + "noise. Everything this node reports — its coefficients, its contributions, its "
      + "share of the gap — is computed from that model, so read the direction rather than "
      + "the magnitude and treat the model itself as the thing to fix.",
  },
  unavailable: {
    text: "⚠ model check unavailable",
    cls: "sign-flag",
    why:
      "The engine could not check this node's model against its own posterior predictive "
      + "distribution. That is an unchecked model, not a validated one: if the likelihood "
      + "is wrong for this data, nothing here will say so.",
  },
};

function ppcNote(status) {
  if (!status || status === "ok") return null;
  return (
    PPC_NOTE[status] || {
      text: `⚠ model check: ${status}`,
      cls: "sign-flag",
      why:
        "This build does not recognise that posterior predictive status, so it is shown "
        + "verbatim. It is not 'ok' — a newer engine flagged something about whether this "
        + "node's model fits its data that this UI cannot explain yet.",
    }
  );
}

/* "which statistic", in the flag itself, so a reader scanning a wide RCA can
   tell a floor violation from leftover autocorrelation without hovering.
   Names the worst statistic only; the tooltip carries the rest. */
function ppcStatText(node) {
  const s = ((node.ppc || {}).statistics || []).filter((e) => e.status !== "ok")[0];
  if (!s) return "";
  const p = typeof s.p_value === "number" && Number.isFinite(s.p_value) ? s.p_value.toFixed(3) : null;
  return ` (${s.statistic}${p ? `, p ${p}` : ""})`;
}

function ppcStatSuffix(node) {
  return esc(ppcStatText(node));
}

function ppcChipHtml(node) {
  const pn = ppcNote(node.ppc_status);
  if (!pn) return "";
  const title = pn.why + (node.ppc_warnings || []).map((w) => `\n\n${w}`).join("");
  return ` <span class="cause-flag" title="${esc(title)}">${esc(pn.text)}${ppcStatSuffix(node)}</span>`;
}

function ppcBlockHtml(name, node) {
  const pn = ppcNote(node.ppc_status);
  if (!pn) return "";
  const body = (node.ppc_warnings || []).length
    ? node.ppc_warnings.map((w) => esc(w)).join(" ")
    : esc(pn.why);
  return `<div class="wf-warning">⚠ ${esc(pn.text.replace(/^⚠\s*/, ""))} for <code>${esc(name)}</code>: ${body}</div>`;
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

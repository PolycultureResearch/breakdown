/* disclosures.js — the fifth-rule surface, on one screen.

   Every table and helper that turns an engine verdict into words a reader
   sees: node/interval/fit statuses, the sampler axis, the k̂ / collinearity /
   PPC vocabularies, the unexplained-row and window-basis wording, the RCA
   caveats, the windows headline, and the direction/goodness mapping (gapDir
   and friends). Split out
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
  reference_before_fit_window: {
    label: "not decomposed — reference window precedes this node's fit",
    short: "reference before fit",
    explains: "Its movement below is measured from the data and stands; what is missing is the decomposition. The reference window starts before the first period this node's model is fitted on (a declared `fit_start`, or the periods a lagged parent trims), so the model has no level there to compare against, and it was not fitted for this analysis. Move the reference window later, or the node's `fit_start` earlier.",
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

/* Whether an RCA node has anything to put in an Attribution-detail block.

   Both RCA surfaces used to ask `contributions.length || nodeStatus(node)`,
   which was the whole answer until roadmap S24: a parentless node that
   declares `interventions:` is fitted, comes back `status: "ok"` with
   `contributions: []`, and carries its entire decomposition under the other
   keys — the sized interventions, the ones the fit could not size, trend and
   seasonal. `0 || null` dropped the block, and with it the intervention rows,
   the component rows, `unexplained`, the "fit saw the analysis window" chip
   and the `claim` sentence, while the engine had attributed most of the gap to
   the declared step (grill 2026-10-05 H5). One predicate, here, so the live
   tab, the export and the canvas badge cannot each grow their own idea of
   "nothing to show". A term the engine can attach to a node goes in this list
   in the same change that adds the row for it. */
function rcaNodeHasDetail(node) {
  if (!node) return false;
  if (nodeStatus(node)) return true;
  const some = (a) => Array.isArray(a) && a.length > 0;
  const comps = node.components;
  return (
    some(node.contributions) ||
    some(node.interventions) ||
    some(node.dropped_interventions) ||
    some(node.dropped_parents) ||
    !!(comps && typeof comps === "object" && Object.values(comps).some((c) => c != null))
  );
}

/* The two empty states of the Root cause tab, each printed only when it is
   true. "No upstream causes — target is a source metric." used to be the
   fallback for any empty ranking, including a source target whose gap the
   engine had just attributed to a declared step: a sentence about the tree
   read as a sentence about the gap. `isSource` is the caller's reading of the
   tree (the payload does not say whether a node has parents, only whether
   anything was attributed to them); null means the caller could not tell, and
   then nothing is claimed about it. */
function rankedCausesEmptyNote(res, isSource) {
  const target = ((res && res.nodes) || {})[res && res.target] || {};
  const ivs = Array.isArray(target.interventions) ? target.interventions : [];
  const why =
    isSource === true
      ? "the target is a source metric, so there is no upstream metric to rank"
      : isSource === false
      ? "nothing was attributed to the target's parents"
      : "no upstream metric was attributed";
  if (ivs.length) {
    return (
      `No ranked causes — ${why}. That is not the same as nothing explaining the gap: ` +
      `the engine sized the declared intervention${ivs.length === 1 ? "" : "s"} ` +
      `${ivs.map((iv) => iv.name).join(", ")} on this metric, and a declared intervention is ` +
      "a term in the gap, never a ranked cause. See Attribution detail."
    );
  }
  if (rcaNodeHasDetail(target)) {
    return `No ranked causes — ${why}. What the engine did attribute on this metric is under Attribution detail.`;
  }
  return `No ranked causes — ${why}.`;
}

const ATTRIBUTION_EMPTY_NOTE =
  "Nothing to decompose: no metric in scope has a parent, a declared intervention " +
  "or a fitted component for its gap to be attributed to.";

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
  nonfinite_posterior: {
    text: "terms withheld: non-finite posterior",
    why: "This node's fitted posterior holds a non-finite value in at least one term: a parent's coefficient, the trend or seasonal component, or a declared intervention. That term is shown as \u2014 with no estimate, share or interval, and the node's unexplained remainder is withheld with it, because a remainder computed around a missing term would quietly treat that term as zero. The other terms stand.",
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

/* ---------- the Metric tab's diagnostics row, in this file's words ----------
   The diagnostics row prints one short phrase per check and, unlike the RCA
   header chips, prints the *pass* too. Those phrases were written inline in
   `renderPosterior` (grill 2026-10-05 L9), against the 2026-08-31 agreement
   and with the result that agreement predicts: `PPC_NOTE` says "the model
   cannot generate this node's own data" and the row beside it said "cannot
   generate this data"; `moderate` was "reproduces its own data imperfectly"
   in one and "fits imperfectly" in the other. Each helper returns
   `{subject, word, cls}` — the renderer supplies only the markup — and the
   flagged words are *derived from* the note tables rather than copied beside
   them, so there is one wording to edit. An unknown status keeps the
   verbatim fallback, under a subject that does not presume which way it
   points. */
const KHAT_BAND = {
  ok: "close to the posterior",
  suspect: "measurably off",
  unusable: "not usable",
};

/* The band a k̂ landed in, with S22's "the landing was not decisive" beside
   it. A k̂ with no status is "band not reported" — `String(undefined)` used
   to print the word "undefined" as a band. */
function khatBandText(dx) {
  const st = dx && dx.khat_status;
  const band = !st ? "band not reported" : KHAT_BAND[st] || String(st);
  return `${band}${dx && dx.khat_borderline ? ", band unresolved at this error" : ""}`;
}

function khatBandClass(dx) {
  return dx && dx.khat_status === "ok" && !dx.khat_borderline ? "ok" : "warn";
}

function collinDiagBit(status) {
  if (!status) return null;
  if (status === "ok") return { subject: "parents", word: "separable", cls: "ok" };
  const note = COLLIN_NOTE[status];
  // "⚠ parents collinear — the split …" → "collinear".
  const m = note && /^⚠\s*parents\s+(.+?)\s+—/.exec(note.text);
  if (m) return { subject: "parents", word: m[1], cls: "warn" };
  return { subject: "collinearity", word: String(status), cls: "warn" };
}

function ppcDiagBit(status) {
  if (!status) return null;
  if (status === "ok") return { subject: "model", word: "reproduces its data", cls: "ok" };
  const note = PPC_NOTE[status];
  // "⚠ the model cannot generate this node's own data" → the predicate.
  const m = note && /^⚠\s*the model\s+(.+)$/.exec(note.text);
  if (m) return { subject: "model", word: m[1], cls: "warn" };
  return { subject: "model check", word: String(status), cls: "warn" };
}

/* The engine's `fit_quality` verdict as the Metric tab's own block: the same
   causes `fitQualityNote` names, plus the k̂ figure this surface can see.
   Moved here from `renderPosterior` for the reason above. Returns HTML ("" for
   a fit with no verdict), built only from escaped strings.

   One branch is new. An ADVI fit whose k̂ was fine and whose posterior
   predictive check was `severe` used to be explained by the ELBO sentence —
   the residual branch — which names a cause that did not happen; the S3
   comment this block carried made the NUTS side conditional for exactly that
   reason and stopped there. `severe` is checked for both samplers now, after
   the k̂ causes, which are the more specific finding when both apply. */
function fitVerdictDiagHtml(dx) {
  if (!dx || !dx.fit_quality) return "";
  if (dx.fit_quality === "ok") {
    return `<div class="diag">Engine fit check: <span class="ok">ok</span>.</div>`;
  }
  if (dx.fit_quality !== "suspect") {
    // Unknown verdict: shown verbatim rather than swallowed into silence,
    // which would read as "nothing to report".
    return `<div class="diag">Engine fit check: <span class="warn">${esc(String(dx.fit_quality))}</span>.</div>`;
  }
  const advi = dx.method === "advi" || dx.method === "fullrank_advi";
  let cause;
  if (advi && (dx.khat_status === "unusable" || dx.khat_status === "suspect")) {
    cause = `Its PSIS k̂ is ${khatFigure(dx) || "above the threshold"}: the approximation sits away from the posterior it approximates, so its credible intervals are not a measurement of the real ones. Re-run this metric with NUTS.`;
  } else if (advi && dx.khat_borderline) {
    // Roadmap S22. Without this branch a borderline-`ok` fit would be
    // explained by the ELBO sentence below — an explanation of a check that
    // passed, offered for a failure it did not cause.
    cause = `Its PSIS k̂ is ${khatFigure(dx) || "close to a band edge"}, which is nearer the band edge than its own Monte-Carlo error: the check cannot say which side of the threshold this approximation is on. Re-run this metric with NUTS for anything that turns on it.`;
  } else if (dx.ppc_status === "severe") {
    // Roadmap S3: on a NUTS fit this is the only thing that can set
    // `suspect`. One wording with the RCA chip's tooltip.
    cause = fitQualityNote({ fit_quality: "suspect", ppc_status: "severe" }).why;
  } else if (advi) {
    cause = "The ADVI objective (the ELBO) had not settled by the end of optimization, so the approximation may not have converged on anything.";
  } else {
    cause = "One of R̂, the divergence count or the effective sample size crossed the engine's threshold.";
  }
  return `<div class="diag"><span class="warn">⚠ The engine flagged this fit as suspect.</span>
      ${esc(cause)}
      Numbers derived from this fit — coefficients, intervals, and any RCA contribution through this node — inherit that.</div>`;
}

/* The header chip for each list of engine warning sentences an RCA node can
   carry. The sentences themselves are the engine's and print verbatim (hover
   on the live tab, in full in the export); these are only the chips that say
   a list is non-empty. They were string literals at both render sites, and
   the two had already drifted: the export said "contradicts declared
   expectation", the live header "contradicts expectation". */
const NODE_WARNING_CHIP = {
  sign_warnings: "⚠ learned sign contradicts declared expectation",
  seasonality_warnings: "⚠ seasonality unidentifiable from fitted history",
  likelihood_warnings: "⚠ zero-inflated fit window — intervals approximate",
};

/* The chip for one of those lists, or null when the node carries none. */
function nodeWarningChip(node, field) {
  const list = node && node[field];
  return Array.isArray(list) && list.length ? NODE_WARNING_CHIP[field] : null;
}

/* A fit whose posterior predictive band could not be built: the PPC panel's
   one verdict-bearing state. `reason` is the server's. Returns HTML. */
function ppcBandUncheckedHtml(reason) {
  return (
    "This fit was <strong>not checked</strong> against its own posterior predictive " +
    `distribution: ${esc(reason || "no reason given")}. That is the absence of a ` +
    "check, not a clean bill of health — if this node's likelihood is wrong for its " +
    "data, nothing here will say so."
  );
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
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? Number(v.toPrecision(3)).toString() : null);
  const p = typeof s.p_value === "number" && Number.isFinite(s.p_value) ? s.p_value.toFixed(3) : null;
  // The two numbers the p-value compares: what the data shows, and what the
  // model's own replicates average. Either missing, neither is printed — one
  // side of a comparison is not a comparison.
  const obs = num(s.observed), rep = num(s.replicated_mean);
  const versus = obs !== null && rep !== null ? `: observed ${obs}, replicates average ${rep}` : "";
  return ` (${s.statistic}${versus}${p ? `, p ${p}` : ""})`;
}

/* Roadmap S24: a posterior predictive check on a node that declares
   interventions was run *given* them. The replicates come from a mean
   function that already contains the declared steps, so a pass says the model
   reproduces the data around the steps, not that the data shows them. Empty
   for a node that declares none, or an engine too old to say. */
function ppcConditionedText(node) {
  const names = ((node || {}).ppc || {}).conditioned_on_interventions;
  if (!Array.isArray(names) || !names.length) return "";
  return ` · checked given the declared intervention${names.length === 1 ? "" : "s"} ${names.join(", ")}`;
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

/* ---------- a parent the fit left out (issue #113) ----------
   A parent whose series was constant over the fit window is dropped from the
   node's regression: a constant column is not identified and carries no
   information about the gap, so the other parents' numbers are exactly what
   they would have been. The engine names it on the node as
   `dropped_parents: [{parent, reason}]`. What the reader must not be left
   with is a contributions table that is simply one row short — an absent row
   reads as "this parent contributed nothing", which is a finding, when the
   fact is "this parent was not fitted", which is the absence of one. So the
   parent gets a row of its own, labelled, in the live table and the export,
   and a chip in the header; and any movement it made between the windows is
   in `unexplained`, which the wording says.

   One vocabulary, used by every surface (metric tab, RCA table, export). */
const DROPPED_PARENT_WHY =
  "This parent held one value across the whole window the model was fitted on, " +
  "so its coefficient is not identified — a constant column is a multiple of the " +
  "intercept's — and it carries no information about how the metric moved. It was " +
  "left out of the fit and the attribution excludes it. That does not change the " +
  "other parents' contributions, which are the same numbers with or without it; " +
  "but it is not a measured zero either. If this parent moved between the two " +
  "windows, that movement is in the unexplained row.";

/* The one label for a dropped parent's row: the Metric tab's coefficient
   table, the live RCA table and the export. */
const DROPPED_PARENT_LABEL = "not fitted — did not vary over the fit window";

/* The note for a node that dropped a parent, or null when nothing was dropped.
   `names` and `reasons` are the engine's own words; `text` is the chip. */
function droppedParentsNote(node) {
  const dropped = node && node.dropped_parents;
  if (!Array.isArray(dropped) || !dropped.length) return null;
  const names = dropped.map((d) => d.parent);
  return {
    names,
    text: `⚠ attribution excludes ${names.join(", ")} — did not vary over the fit window`,
    cls: "sign-flag",
    why: DROPPED_PARENT_WHY,
    reasons: dropped.map((d) => `${d.parent}: ${d.reason}`).join("\n"),
  };
}

/* One dim row per dropped parent, for the single-level contributions table
   (`nCols` cells wide). Posterior nodes are the only ones that drop parents
   and they are never two-level, so this is the only table shape it needs. */
function droppedParentRowsHtml(node, nCols) {
  const dropped = node && node.dropped_parents;
  if (!Array.isArray(dropped) || !dropped.length) return "";
  const dash = '<td class="num">—</td>';
  return dropped
    .map(
      (d) =>
        `<tr class="dim"><td title="${esc(`${d.reason}\n\n${DROPPED_PARENT_WHY}`)}"><code>${esc(d.parent)}</code>, ${esc(DROPPED_PARENT_LABEL)}</td>${dash.repeat(Math.max(nCols - 1, 0))}</tr>`,
    )
    .join("");
}

/* ---------- a declared, dated intervention (roadmap S24) ----------
   A node may declare `interventions:` — a price flip, an on-sale day — and each
   becomes a known 0/1 regressor the fit sizes on its own coefficient axis. On
   an RCA node the engine reports each as
   `interventions: [{name, date, kind, until, window_delta, estimate, ci_95,
   ci_status, prob_same_direction, claim?}]`: the step's fitted size times how
   much more of the analysis window than the reference window it covered. It
   is a real term in the gap (`unexplained` subtracts it), beside the parents
   and the trend/seasonal components — but it is neither. It is not a parent
   (no subtree, no slices, not in the ranking) and not model structure (trend
   and seasonal are nobody's fault; a flip is somebody's decision, declared by
   the author). So it gets rows of its own, labelled as declared, on every
   table the parents appear in. The same vocabulary serves the metric tab's
   coefficient table, the live RCA table and the export. */
const INTERVENTION_KIND_LABEL = { step: "step", pulse: "pulse" };

/* "flip — step from 2024-02-10" / "sale — pulse 2024-03-01 → 2024-03-02". */
function declaredInterventionLabel(iv) {
  const kind = INTERVENTION_KIND_LABEL[iv.kind] || String(iv.kind);
  if (iv.kind === "pulse") {
    const span = iv.until && iv.until !== iv.date ? `${iv.date} → ${iv.until}` : iv.date;
    return `${iv.name} — declared ${kind} ${span}`;
  }
  return `${iv.name} — declared ${kind} from ${iv.date}`;
}

const INTERVENTION_WHY =
  "A dated change the tree's author declared on this metric. The model was told " +
  "the date and learned the size: this row is that fitted size times how much more " +
  "of the analysis window than the reference window the change covered. It is a " +
  "term in the gap like a parent's contribution, but it is not a cause to drill " +
  "into — it has no subtree and is not in the ranking — and it is not model " +
  "structure like trend or seasonal: it is the author's claim, sized.";

/* Per-intervention `ci_status` — why a row has no interval. */
const INTERVENTION_CI_NOTE = {
  indicator_unchanged: {
    text: "on (or off) throughout both windows",
    why:
      "The change covered the same share of the reference window as of the analysis " +
      "window, so it moved the gap by exactly nothing — while still shaping the fit " +
      "every other row rests on. Zero by construction, not a measured zero, so no " +
      "interval is drawn.",
  },
  degenerate: {
    text: "interval collapsed",
    why: "The coefficient's posterior has no spread at the node's scale; the interval is withheld rather than drawn zero-width.",
  },
  nonfinite_posterior: {
    text: "estimate withheld",
    why: "The coefficient's posterior carried non-finite draws; the term is withheld rather than published as a number that is not one.",
  },
};

/* The note for one intervention's `ci_status`, or null when there is nothing
   to say (`ok`, or no status). The bare `INTERVENTION_CI_NOTE[x]` this
   replaces returned undefined for a value this build has never heard of and
   rendered nothing — a row with an em dash where its interval should be and
   no word on why, which is the silence `ciStatusNote` was written to end. */
function interventionCiNote(status) {
  if (!status || status === "ok") return null;
  return (
    INTERVENTION_CI_NOTE[status] || {
      text: `interval flagged: ${status}`,
      why: "This build does not recognise that status on a declared intervention, so it is shown verbatim. It is not 'ok' — the engine flagged something about this row's estimate or interval that a newer version of the UI would explain.",
    }
  );
}

/* The sentence for a `learn_from: window` intervention, from the engine's own
   `claim` — printed verbatim wherever the row appears, because it is the one
   thing about this number the reader must not lose. */
function interventionClaim(iv) {
  return iv && iv.claim ? iv.claim : "";
}

/* One row per fitted intervention, for the single-level (5-column) table. A
   posterior node is the only kind that carries them and is never two-level. */
/* `window_delta` is the multiplier behind an intervention's row: the share of
   the analysis window's periods it was in force for, minus the reference
   window's. The estimate is the fitted step size times this, so a step that
   was already on for the whole reference contributes nothing however large it
   is, and the row has to say which of the two it is looking at. Silent when
   the engine does not report it. */
function interventionWindowDeltaText(iv) {
  const d = iv && iv.window_delta;
  if (typeof d !== "number" || !Number.isFinite(d)) return "";
  const pctOf = `${Math.round(Math.abs(d) * 100)}%`;
  if (d === 0) return " · in force for the same share of both windows";
  return ` · in force for ${pctOf} ${d > 0 ? "more" : "less"} of the analysis window than the reference`;
}

/* The two numbers behind a rate slice's `within` and `mix` cells: the slice's
   own rate in each window, and its share of the denominator in each. Either
   end missing (a slice with no denominator in one window has no rate there)
   and the pair is not printed: one end is not a movement. */
function sliceRateMoveText(row) {
  const a = row && row.rate_reference, b = row && row.rate_analysis;
  if (typeof a !== "number" || typeof b !== "number" || !Number.isFinite(a) || !Number.isFinite(b)) return "";
  return ` (its rate went ${Number(a.toPrecision(4))} → ${Number(b.toPrecision(4))})`;
}
function sliceShareMoveText(row) {
  const a = row && row.baseline_share, b = row && row.share_analysis;
  if (typeof a !== "number" || typeof b !== "number" || !Number.isFinite(a) || !Number.isFinite(b)) return "";
  return ` (its share of the denominator went ${(a * 100).toFixed(1)}% → ${(b * 100).toFixed(1)}%)`;
}

function interventionRowsHtml(node, nCols, shareOf, ciCell) {
  const ivs = node && node.interventions;
  if (!Array.isArray(ivs) || !ivs.length || nCols !== 5) return "";
  return ivs
    .map((iv) => {
      const note = interventionCiNote(iv.ci_status);
      const claim = interventionClaim(iv);
      const title = [INTERVENTION_WHY, note ? note.why : "", claim].filter(Boolean).join("\n\n");
      // Both, when both apply. The claim tag used to win, so a
      // `learn_from: window` row whose interval had collapsed showed "fit saw
      // this window" and an unexplained em dash.
      const tag = `${interventionWindowDeltaText(iv)}${claim ? " · fit saw this window" : ""}${note ? ` · ${note.text}` : ""}`;
      const est = iv.estimate == null ? "—" : fmt(iv.estimate);
      const share = iv.estimate == null ? "—" : shareOf(iv.estimate, node.gap);
      return `<tr class="intervention-row"><td title="${esc(title)}"><code>${esc(iv.name)}</code> <span class="dim">— ${esc(declaredInterventionLabel(iv).replace(`${iv.name} — `, ""))}${esc(tag)}</span></td>
        <td class="num">${est}</td>
        <td class="num">${share}</td>
        <td class="num">${iv.ci_95 ? ciCell(iv.ci_95) : "—"}</td>
        <td class="num">${pctDir(iv.prob_same_direction, iv.prob_same_direction_censored)}</td></tr>`;
    })
    .join("");
}

/* ---------- a declared intervention the fit left out ----------
   Same shape as a dropped parent (#113), same reason for a row of its own: a
   declared step missing from the table reads as "no effect", when the fact is
   "not fitted" — no instance inside the fit window (the flip is in the
   analysis window, which RCA's fit never sees), or on for every period of it. */
/* The two ways are not one, and the engine says which (`model.py`
   `_intervention_columns` writes a different `reason` for each because they
   have different remedies). Every surface used to say "outside the fit
   window" for both — which, for a regime that was on for the *whole* fit
   window, is the opposite of the fact (grill 2026-10-05 L8). The payload
   carries no code for the case, only the sentence, so the sentence is what is
   matched; one this build cannot place gets the neutral label and is printed
   in full beside it, never guessed at. */
const DROPPED_INTERVENTION_CASE = {
  no_instance: {
    match: "no instance inside the fit window",
    short: "no instance inside the fit window",
    why:
      "This declared change had no instance inside the window the model was fitted " +
      "on, so its size could not be learned and it was left out of the fit. If the " +
      "analysis window contains it, its effect is in the unexplained row or in the " +
      "parents that moved with it. That is not a measured zero: nothing was estimated.",
  },
  always_on: {
    match: "on for every period of the fit window",
    short: "on for every period of the fit window",
    why:
      "This declared change was already on for every period of the window the model " +
      "was fitted on, so its column is the intercept's and its coefficient is not " +
      "identified; it was left out of the fit. The fit saw only the regime with the " +
      "change on, so that level is in the intercept. Nothing was estimated for the " +
      "change itself, which is not a measured zero. A regime that began before the " +
      "fit window is what `fit_start` declares, not an intervention.",
  },
};

const DROPPED_INTERVENTION_UNKNOWN = {
  short: "for a reason this build does not recognise",
  why:
    "The engine left this declared change out of the fit and gave the reason shown. " +
    "This build does not recognise that reason, so it is printed verbatim rather than " +
    "summarised. Nothing was estimated for it: that is not a measured zero.",
};

function droppedInterventionCase(d) {
  const reason = d && typeof d.reason === "string" ? d.reason : "";
  return (
    Object.values(DROPPED_INTERVENTION_CASE).find((c) => reason.includes(c.match)) ||
    DROPPED_INTERVENTION_UNKNOWN
  );
}

/* "not fitted — on for every period of the fit window": the one label for a
   dropped intervention's row, on the Metric tab's coefficient table, the live
   RCA table and the export. */
function droppedInterventionLabel(d) {
  return `not fitted — ${droppedInterventionCase(d).short}`;
}

function droppedInterventionsNote(node) {
  const dropped = node && node.dropped_interventions;
  if (!Array.isArray(dropped) || !dropped.length) return null;
  const names = dropped.map((d) => d.intervention);
  const cases = [...new Set(dropped.map(droppedInterventionCase))];
  // One case: the chip names it. Mixed: the chip says only "not fitted" and
  // each row carries its own.
  const how = cases.length === 1 ? ` — ${cases[0].short}` : "";
  return {
    names,
    text: `⚠ declared intervention${names.length === 1 ? "" : "s"} ${names.join(", ")} not fitted${how}`,
    cls: "sign-flag",
    why: cases.map((c) => c.why).join("\n\n"),
    reasons: dropped.map((d) => d.reason).join("\n"),
  };
}

function droppedInterventionRowsHtml(node, nCols) {
  const dropped = node && node.dropped_interventions;
  if (!Array.isArray(dropped) || !dropped.length) return "";
  const dash = '<td class="num">—</td>';
  return dropped
    .map(
      (d) =>
        `<tr class="dim"><td title="${esc(`${d.reason}\n\n${droppedInterventionCase(d).why}`)}"><code>${esc(d.intervention)}</code> — declared ${esc(d.kind)} ${esc(d.date)}, ${esc(droppedInterventionLabel(d))}</td>${dash.repeat(Math.max(nCols - 1, 0))}</tr>`,
    )
    .join("");
}

/* The chip for a node whose fit sized declared interventions, and — when one
   of them was `learn_from: window` — the fact that the fit saw the analysis
   window for this node (`fit_window.extended_for`). Null when none. */
function interventionsNote(node) {
  const ivs = node && node.interventions;
  if (!Array.isArray(ivs) || !ivs.length) return null;
  const extended = (node.fit_window && node.fit_window.extended_for) || [];
  const names = ivs.map((iv) => iv.name);
  if (extended.length) {
    return {
      names,
      text: `⚠ fit saw the analysis window — sized ${extended.join(", ")} from its own periods`,
      cls: "sign-flag",
      why:
        "One of this node's declared interventions is `learn_from: window`, so its fit " +
        "was extended through the analysis window to size the change from the event " +
        "itself. The other parents' coefficients were therefore fitted on a window that " +
        "contains the anomaly, and the intervention's estimate is the shift coincident " +
        "with its date — whatever caused it.",
    };
  }
  return {
    names,
    text: `${names.length} declared intervention${names.length === 1 ? "" : "s"} sized (${names.join(", ")})`,
    cls: "dim",
    why: INTERVENTION_WHY,
  };
}

/* ---------- the two windows every number is a contrast of ----------
   Everything an RCA publishes — baseline, gap, every share, the ranking —
   is the analysis window measured against the reference window, and the
   reference is usually the engine's own choice (`reference_defaulted`). A
   reader who loses the reference cannot reproduce the result: issue #114's
   author matched a recorded actual against re-runs to recover theirs. Both
   surfaces had carried the dates since 0.1.0, but as one clause of a muted
   subtitle beside the provider and the timestamp, and the export's <title>
   named only the analysis window. This is the headline form: the two
   windows as labelled, first-class lines, with the engine's authorship of
   the reference stated in words rather than a chip. The dates are the ones
   the analysis was requested with; where the target's grain snapped them to
   whole periods, `effective_windows` says what was actually compared, and
   that goes beside them so a weekly report does not headline a Tuesday. */
const REFERENCE_DEFAULTED_NOTE = "chosen by the engine, not by the person who ran this";

function windowsHeadline(res) {
  const target = (res.nodes || {})[res.target] || {};
  const span = (w) => (w && w.start && w.end ? `${w.start} → ${w.end}` : "—");
  const ew = target.effective_windows;
  // Only when snapping changed something: a day-grain target's effective
  // windows are the requested ones, and repeating them is noise.
  const snapped = (w, e) =>
    e && target.grain && target.grain !== "day" && (e.start !== w.start || e.end !== w.end)
      ? ` (${e.n_periods} whole ${target.grain}${e.n_periods === 1 ? "" : "s"}: ${span(e)})`
      : "";
  return {
    analysis: span(res.analysis_window) + snapped(res.analysis_window, ew && ew.analysis),
    reference: span(res.reference_window) + snapped(res.reference_window, ew && ew.reference),
    referenceNote: res.reference_defaulted ? REFERENCE_DEFAULTED_NOTE : "",
  };
}

/* One line, for a <title> or a log: no markup, both windows, authorship. */
function windowsHeadlineText(res) {
  const w = windowsHeadline(res);
  return `analysis ${w.analysis} vs reference ${w.reference}${w.referenceNote ? ` (${w.referenceNote})` : ""}`;
}

/* The labelled two-line form both renderers print. */
function windowsHeadlineHtml(res) {
  const w = windowsHeadline(res);
  return (
    `<div class="win-line"><span class="win-label">Analysis window</span> ${esc(w.analysis)}</div>` +
    `<div class="win-line"><span class="win-label">Reference window</span> ${esc(w.reference)}` +
    `${w.referenceNote ? ` <span class="win-note">— ${esc(w.referenceNote)}</span>` : ""}</div>`
  );
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
  "Intervals are 95% credible intervals combining the coefficient posterior with a moving-block bootstrap of the window rows — except on a declared intervention's row, where the interval is the coefficient's posterior alone: the dates it was on are facts, not samples, so there is nothing to resample. Where an interval is withheld the table says so; the point estimates are unaffected.",
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

/* ---------- the tree-wide data edge ----------
   The oldest known `data_through` across the tree: the as-of anchor the node
   cards default to and the "data through" line the export prints. `/meta`
   deliberately emits `null` for a metric whose edge is unknown (so "we do not
   know when this ends" is not the same absence as "no such metric"), and a
   bare string-min over the values loses to it: `null < "2024-06-28"` is true
   in JavaScript (null coerces to 0), `"2024-06-20" < null` is false, so
   `["2024-06-20", null, "2024-06-28"]` reduced to "2024-06-28" and the lagging
   edge was the one thing dropped. The export filtered and the boot path did
   not — the same policy on one side of the file only — so both go through
   here. Returns null when no edge is known; the caller chooses the fallback
   and must not print one as if it were measured. */
function treeDataEdge(meta) {
  const through = meta && meta.data_through;
  if (!through || typeof through !== "object") return null;
  const known = Object.values(through).filter((d) => typeof d === "string" && d);
  return known.length ? known.reduce((a, b) => (a < b ? a : b)) : null;
}

/* ---------- a metric's own range, and what was filled inside it (GitHub #112) ----------
   `/meta` carries three facts about how much of the loaded window a metric
   really has, and until grill 2026-10-05 (M8) no line of this UI read any of
   them — while MCP `get_tree` worded all three for an agent:

   - `sparse_fills[name]` — `{first_row, last_row, leading, interior, trailing,
     whole_window, filled}`: periods with no source row that the load filled
     with zero because the metric declares `sparse: true`. `data_through` for
     such a metric reports the window's end *by declaration*, so the "lags
     window end" chip can never fire on it, and a feed that went stale three
     weeks ago draws as three weeks of real zeros and an RCA of −100%.
     `last_row` (the label of the last period the source returned a row for) is
     the true edge, and it is what the reader is shown.
   - `short_series[grain].trailing|leading.short[name]` — `{ends|starts,
     periods}` against that edge's `reach`: the metric stops before, or starts
     after, the other metrics at its grain.
   - `data_from[name]` — the first period the metric has, which may be later
     than the loaded window's start even when every sibling agrees with it.

   The sentences reuse the MCP docstring's wording (`mcp/server.py`
   `get_tree`) so a person and an agent are told the same thing. Everything
   here reads `meta` only and never coerces: a count that is not a finite
   number is not printed as one. */
const SPARSE_FILL_WHY =
  "This metric declares `sparse: true`, so periods with no source row were filled " +
  "with zero by that declaration. Those zeros are the tree's own statement that " +
  "nothing happened, not observations, and a run of them at the tail is what a " +
  "stale feed on such a metric would also look like.";

const SHORT_SERIES_WHY =
  "Every other metric keeps its own range; an analysis that reads this metric — it, " +
  "or a child of it — cannot reach past its edge. The metric named is a source to " +
  "widen or repair, not a finding about the business.";

const DATA_FROM_WHY =
  "This metric has no period before that date. Nothing was filled in front of it: " +
  "an analysis that reads it — it, or a child of it — cannot start earlier, and the " +
  "periods before it are absent, not zero.";

/* A finite, non-negative count, or null. `Number(null)` is 0 and `null > 0` is
   false, so an absent count must never reach arithmetic or a template. */
function fillCount(v) {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
}

function isoDateOrNull(v) {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v) ? v.slice(0, 10) : null;
}

/* The period-start label one period after `iso` at `grain` — where a trailing
   fill begins, given the last period that had a row. Null on anything it
   cannot parse or a grain it does not know. */
function nextPeriodStart(iso, grain) {
  const d = isoDateOrNull(iso);
  if (!d) return null;
  const t = new Date(`${d}T00:00:00Z`);
  if (Number.isNaN(t.getTime())) return null;
  if (grain === "day") t.setUTCDate(t.getUTCDate() + 1);
  else if (grain === "week") t.setUTCDate(t.getUTCDate() + 7);
  else if (grain === "month") t.setUTCMonth(t.getUTCMonth() + 1, 1);
  else return null;
  return t.toISOString().slice(0, 10);
}

/* The first whole period at `grain` starting on or after `iso`: the earliest
   label a series at that grain *could* carry inside a window starting there.
   A weekly metric in a window that opens on a Wednesday starts the following
   Monday by construction, and calling that "starts late" would put a chip on
   every weekly node of every such tree. */
function firstWholePeriodStart(iso, grain) {
  const d = isoDateOrNull(iso);
  if (!d) return null;
  const t = new Date(`${d}T00:00:00Z`);
  if (Number.isNaN(t.getTime())) return null;
  if (grain === "week") t.setUTCDate(t.getUTCDate() + ((8 - t.getUTCDay()) % 7)); // Sun=0 → Monday
  else if (grain === "month") {
    if (t.getUTCDate() !== 1) t.setUTCMonth(t.getUTCMonth() + 1, 1);
  } else if (grain !== "day") return null;
  return t.toISOString().slice(0, 10);
}

function periodsPhrase(n, grain) {
  const unit = grain === "day" || grain === "week" || grain === "month" ? `${grain} ` : "";
  return n === null ? `${unit}periods` : `${n} ${unit}period${n === 1 ? "" : "s"}`;
}

/* The sparse-fill disclosure for one metric, or null when nothing was filled
   (the engine only publishes a record where something was). `analysisEnd`,
   when given, is the end of the analysis window an RCA node was measured
   over: `inWindow` then says whether that window reaches into the trailing
   fill, which is the case that turns a quiet (or dead) feed into a measured
   collapse. `tail` is true when the *end* of the series is declared zeros —
   the stale-feed lookalike — and is what makes the note a warning rather
   than a muted fact. */
function sparseFillNote(meta, name, analysisEnd) {
  const rec = meta && meta.sparse_fills && meta.sparse_fills[name];
  if (!rec || typeof rec !== "object") return null;
  const grain = (meta.grains && meta.grains[name]) || null;
  const filled = fillCount(rec.filled);
  const leading = fillCount(rec.leading);
  const interior = fillCount(rec.interior);
  const trailing = fillCount(rec.trailing);
  const whole = fillCount(rec.whole_window);
  const lastRow = isoDateOrNull(rec.last_row);
  const firstRow = isoDateOrNull(rec.first_row);
  if (filled === 0) return null;
  const noRows = (whole !== null && whole > 0) || (!lastRow && !firstRow);
  const tail = noRows || (trailing !== null && trailing > 0);
  const edges = [
    leading ? `${leading} before the first source row${firstRow ? ` (${firstRow})` : ""}` : "",
    interior ? `${interior} between source rows` : "",
    trailing ? `${trailing} after the last source row${lastRow ? ` (${lastRow})` : ""}` : "",
  ].filter(Boolean);
  let text, detail;
  if (noRows) {
    text = `⚠ sparse: the source returned no rows — ${
      filled === null ? "every period is" : `all ${periodsPhrase(filled, grain)} are`
    } a zero by declaration`;
    detail =
      "The source returned no rows at all for the loaded window, so every value this " +
      "metric shows is a declared zero and none is an observation. There is no last " +
      "source row to date its freshness by.";
  } else {
    text = `${tail ? "⚠ " : ""}sparse: ${filled === null ? "some periods" : periodsPhrase(filled, grain)} zero-filled by declaration${
      tail ? ` — last source row ${lastRow || "unknown"}` : ""
    }`;
    detail =
      `${filled === null ? "Some periods" : periodsPhrase(filled, grain)} had no source row and ` +
      `${filled === 1 ? "was" : "were"} filled with zero by declaration` +
      `${edges.length ? ` (${edges.join(", ")})` : ""}.` +
      (tail
        ? ` The last period with a source row starts ${lastRow || "on an unknown date"}; that, ` +
          "not the window's end, is where this metric's observed data stops."
        : "");
  }
  const fillStart = noRows ? null : nextPeriodStart(lastRow, grain);
  const end = isoDateOrNull(analysisEnd);
  // Unknown (no window given, or no way to place the fill) is null, never false.
  const inWindow = !end ? null : noRows ? true : !tail ? false : fillStart ? end >= fillStart : null;
  if (inWindow) {
    detail +=
      ` The analysis window (through ${end}) ${noRows ? "is made of" : "reaches into"} those ` +
      "declared zeros, so part of the movement measured here is the declaration, not the business.";
  }
  return {
    kind: "sparse",
    text,
    short: noRows
      ? "⚠ sparse: no source rows"
      : tail
      ? `⚠ sparse tail: last source row ${lastRow || "unknown"}`
      : "sparse: zero-filled by declaration",
    detail,
    why: SPARSE_FILL_WHY,
    cls: tail ? "sign-flag" : "dim",
    edge: "through",
    tail,
    inWindow,
    lastRow,
    filled,
  };
}

/* The short-series disclosures for one metric: at most one per edge. Scans
   every grain rather than trusting `meta.grains[name]`, so a record filed
   under a grain this build did not expect is still found. */
function shortSeriesNotes(meta, name) {
  const all = meta && meta.short_series;
  if (!all || typeof all !== "object") return [];
  const out = [];
  Object.entries(all).forEach(([grain, rec]) => {
    if (!rec || typeof rec !== "object") return;
    const t = rec.trailing && rec.trailing.short && rec.trailing.short[name];
    if (t) {
      const n = fillCount(t.periods);
      const reach = isoDateOrNull(rec.trailing.reach);
      out.push({
        kind: "short",
        text: `⚠ series ends ${isoDateOrNull(t.ends) || "early"} — ${periodsPhrase(n, grain)} short of its grain's reach${reach ? ` (${reach})` : ""}`,
        short: `⚠ series ends ${isoDateOrNull(t.ends) || "early"}`,
        detail: "",
        why: SHORT_SERIES_WHY,
        cls: "sign-flag",
        edge: "through",
      });
    }
    const l = rec.leading && rec.leading.short && rec.leading.short[name];
    if (l) {
      const n = fillCount(l.periods);
      const reach = isoDateOrNull(rec.leading.reach);
      out.push({
        kind: "short",
        text: `⚠ series starts ${isoDateOrNull(l.starts) || "late"} — ${periodsPhrase(n, grain)} after the earliest series at its grain${reach ? ` (${reach})` : ""}`,
        short: `⚠ series starts ${isoDateOrNull(l.starts) || "late"}`,
        detail: "",
        why: SHORT_SERIES_WHY,
        cls: "sign-flag",
        edge: "from",
      });
    }
  });
  return out;
}

/* `data_from` later than the loaded window allows for, or null. Only said
   when `short_series` has not already said it about the same edge. */
function dataFromNote(meta, name) {
  const from = isoDateOrNull(meta && meta.data_from && meta.data_from[name]);
  const start = isoDateOrNull(meta && meta.date_start);
  if (!from || !start) return null;
  const grain = (meta.grains && meta.grains[name]) || "day";
  const earliest = firstWholePeriodStart(start, grain);
  if (!earliest || from <= earliest) return null;
  return {
    kind: "from",
    text: `⚠ series starts ${from} — later than the loaded window (${start})`,
    short: `⚠ series starts ${from}`,
    detail: "",
    why: DATA_FROM_WHY,
    cls: "sign-flag",
    edge: "from",
  };
}

/* Every range disclosure for one metric, in one list — what the Metric tab's
   freshness rows, the RCA node header and the export each render, so none of
   the three can carry a fact the others lack. */
function seriesRangeNotes(meta, name, analysisEnd) {
  const notes = [];
  const sparse = sparseFillNote(meta, name, analysisEnd);
  if (sparse) notes.push(sparse);
  const short = shortSeriesNotes(meta, name);
  notes.push(...short);
  if (!short.some((n) => n.edge === "from")) {
    const from = dataFromNote(meta, name);
    if (from) notes.push(from);
  }
  return notes;
}

/* The same list for a node of an RCA result, placed against the analysis
   window that node was actually measured over (its own snapped one where the
   grain changed it). */
function rcaNodeRangeNotes(meta, res, name) {
  const node = ((res && res.nodes) || {})[name] || {};
  const ew = node.effective_windows && node.effective_windows.analysis;
  const end = (ew && ew.end) || (res && res.analysis_window && res.analysis_window.end) || null;
  return seriesRangeNotes(meta, name, end);
}

/* The chip form (live surfaces): the sentence on hover. */
function seriesRangeChipsHtml(notes, sep) {
  return notes
    .map(
      (n) =>
        `${sep}<span class="${n.cls}" title="${esc([n.detail, n.why].filter(Boolean).join("\n\n"))}">${esc(n.text)}</span>`,
    )
    .join("");
}

/* The flag form, for a row that *names* a metric without being its block — a
   ranked cause, a parent in a child's contributions table. Warnings only. A
   plain source metric has no Attribution-detail block of its own, so for the
   metric most likely to be sparse (an event feed, a source by construction)
   these rows are the only place an RCA mentions it. */
function seriesRangeFlagsHtml(notes) {
  return notes
    .filter((n) => n.cls === "sign-flag")
    .map(
      (n) =>
        ` <span class="cause-flag" title="${esc([n.text, n.detail, n.why].filter(Boolean).join("\n\n"))}">${esc(n.short)}</span>`,
    )
    .join("");
}

/* The printed form, for the export, which has no hover: one paragraph per
   warning, optionally naming the metric it is about. Returns the inner HTML
   of each paragraph; the caller wraps it in its own caveat markup. */
function seriesRangeParagraphs(notes, name) {
  return notes
    .filter((n) => n.cls === "sign-flag")
    .map(
      (n) =>
        `${name ? `<code>${esc(name)}</code>: ` : ""}<strong>${esc((n.detail ? n.short : n.text).replace(/^⚠\s*/, ""))}.</strong> ${esc([n.detail, n.why].filter(Boolean).join(" "))}`,
    );
}

/* The tree-wide count for the header's context row: how many metrics end in
   declared zeros. Null when none do — leading and interior fills are not a
   freshness question and get no chip up there. */
function sparseTailsSummary(meta) {
  const fills = meta && meta.sparse_fills;
  if (!fills || typeof fills !== "object") return null;
  const names = Object.keys(fills).filter((name) => {
    const n = sparseFillNote(meta, name);
    return n && n.tail;
  });
  if (!names.length) return null;
  return {
    names,
    text: `${names.length} sparse tail${names.length === 1 ? "" : "s"} zero-filled`,
    title:
      `${names.join(", ")}: declared \`sparse: true\`, and the periods after ` +
      `${names.length === 1 ? "its" : "each one's"} last source row were filled with zero by ` +
      "that declaration. Their cards and analyses show those zeros as values; a stale feed " +
      "would look identical. Each metric's own tab names its last source row.",
  };
}

/* ---------- reference-window sensitivity (roadmap S23) ----------
   Every RCA number is a contrast of two window means, and the bootstrap only
   resamples periods *inside* those windows. The engine re-runs the attribution
   under neighbouring reference blocks (over the same fits) and publishes
   whether the top cause and the gap's direction survived the move. One wording
   here for the live card and the export; `RCA_HOW_TO_READ` carries the same
   rule for an agent. `gap_range` is a sensitivity band, never an interval —
   no surface may render it as one or add it to a `ci_95`. */
const REFERENCE_SENSITIVITY_NOTE = {
  // `stable` has no fixed sentence any more (grill 2026-10-05 M4). The engine
  // answers `stable` when the gap's direction held and the top cause did not
  // *change* — which includes a run with no top cause at all
  // (`top_cause_stable: null`) and one where a single block of two answered.
  // The old sentence claimed "the top cause and the gap's direction are the
  // same under each neighbouring block" for all of them. `stableSummary`
  // builds it from the two booleans and the count of blocks that answered.
  stable: {
    label: "Survives a moved reference window",
    explains: "",
  },
  unstable: {
    label: "Depends on the reference window",
    explains:
      "Moving the reference block changes the answer — read the published ranking as one reading among several, not the finding.",
  },
  unavailable: {
    label: "Reference sensitivity not checked",
    explains:
      "No neighbouring reference block could be attributed, so nothing here says whether the answer survives a moved reference.",
  },
};

function fmtWindowRange(w) {
  return w ? `${w.start} → ${w.end}` : "—";
}

/* One line per alternative block: where it was, and what it said. A block
   that could not answer says so with the engine's reason — "not checked" is
   never allowed to read as "checked and fine". */
/* Why one alternative block gave no answer. `unavailable`: the block could not
   be attributed at all (it does not fit the loaded history, or the engine
   refused it). `gap_unavailable`: it was attributed, and the target has no
   finite gap under it. A status this build cannot name is printed verbatim —
   it is still not `ok`. */
const REFERENCE_ALT_STATUS = {
  unavailable: "not checked",
  gap_unavailable: "attributed, but the target has no gap under this block",
};

function referenceAltStatusText(status) {
  return REFERENCE_ALT_STATUS[status] || `not checked (${status == null ? "no status given" : status})`;
}

/* How many neighbouring blocks answered, out of how many were tried — from
   `alternatives[]`, never assumed. Null when the payload lists none. */
function referenceBlocksAnswered(rs) {
  const alts = Array.isArray(rs && rs.alternatives) ? rs.alternatives.filter(Boolean) : [];
  if (!alts.length) return null;
  return { answered: alts.filter((a) => a.status === "ok").length, tried: alts.length };
}

function referenceBlocksPhrase(rs) {
  const b = referenceBlocksAnswered(rs);
  if (!b) return "";
  const blocks = `neighbouring reference block${b.tried === 1 ? "" : "s"}`;
  if (b.answered === b.tried) {
    return b.tried === 1 ? `the one ${blocks} the engine tried` : `${b.tried === 2 ? "both" : `all ${b.tried}`} ${blocks} the engine tried`;
  }
  if (b.answered === 0) return `the ${b.tried} ${blocks} the engine tried, none of which answered`;
  // "the same under 1 of 2" would read as "and different under the other".
  return `the ${b.answered === 1 ? "one" : b.answered} that answered of the ${b.tried} ${blocks} the engine tried`;
}

/* The sentence for the blocks that gave no answer, or "". Its own sentence so
   "not checked" is never a subordinate clause of "the same". */
function referenceBlocksUnanswered(rs) {
  const b = referenceBlocksAnswered(rs);
  if (!b || b.answered === b.tried || b.answered === 0) return "";
  const n = b.tried - b.answered;
  return ` The other${n === 1 ? " block gave" : ` ${n} gave`} no answer, so nothing is claimed about ${n === 1 ? "it" : "them"}.`;
}

/* The `stable` sentence, claiming only what the two booleans support.
   `top_cause_stable: null` means the published run ranked no cause, so there
   was nothing to compare: the direction is all that was checked, and the
   label says so rather than borrowing the ranking's. */
function stableSummary(rs) {
  const under = referenceBlocksPhrase(rs);
  const scope = under ? ` under ${under}` : " under the neighbouring reference blocks the engine tried";
  const rest = referenceBlocksUnanswered(rs);
  const top = rs.top_cause_stable;
  const sign = rs.gap_sign_stable;
  // `compared` is the engine saying which of the two it actually checked
  // (grill 2026-10-05 M4). Where it is present it decides; a payload from an
  // engine too old to send it falls back to reading `top_cause_stable`.
  const compared = Array.isArray(rs.compared) ? rs.compared : null;
  const topCompared = compared ? compared.includes("top_cause") : top !== null;
  if (top === true && sign === true && topCompared) {
    return {
      label: "Survives a moved reference window",
      summary: `The top cause and the gap's direction are the same${scope}.${rest}`,
    };
  }
  // Strictly null, or left out of `compared`: that is the engine saying "no
  // top cause". A field that is simply absent is not that statement and
  // falls through.
  if (sign === true && (compared ? !topCompared : top === null)) {
    return {
      label: "Gap direction survives a moved reference window",
      summary:
        `The gap's direction is the same${scope}. The published run ranked no cause, so there ` +
        `was no top cause to compare: this says nothing about a ranking.${rest}`,
    };
  }
  // `stable` with neither boolean confirming it: a newer engine, or a payload
  // this build misreads. Say what was reported and claim nothing more.
  const facts = referenceFacts(rs);
  return {
    label: "Reported stable under a moved reference window",
    summary:
      `The engine reports this answer as stable${scope}` +
      `${facts ? ` (${facts})` : ", without saying what it compared"}.${rest}`,
  };
}

/* The two booleans in words, for a status this build cannot name. */
function referenceFacts(rs) {
  const word = (v, same, changed) => (v === true ? same : v === false ? changed : null);
  return [
    word(rs.top_cause_stable, "top cause unchanged", "top cause changes"),
    word(rs.gap_sign_stable, "gap direction unchanged", "gap direction changes"),
  ]
    .filter(Boolean)
    .join(", ");
}

function referenceSensitivityDetails(rs, fmtNum) {
  return (rs.alternatives || []).filter(Boolean).map((a) => {
    const where = `${a.label}${a.reference_window ? ` (${fmtWindowRange(a.reference_window)})` : ""}`;
    if (a.status !== "ok") return `${where}: ${referenceAltStatusText(a.status)} — ${a.reason || "no reason given"}`;
    const parts = [];
    if (rs.top_cause != null) {
      parts.push(
        a.top_cause === rs.top_cause
          ? `top cause still ${a.top_cause}`
          : `top cause becomes ${a.top_cause == null ? "none" : a.top_cause}`,
      );
    }
    parts.push(`gap ${fmtNum(a.gap)}`);
    if (a.note) parts.push(a.note);
    return `${where}: ${parts.join(", ")}`;
  });
}

/* The reader-facing reading of `reference_sensitivity`, or null when the
   payload has none (an older engine). `summary` names what changed on an
   unstable verdict rather than only that something did. */
function referenceSensitivityNote(res, fmtNum) {
  const rs = res && res.reference_sensitivity;
  if (!rs) return null;
  const known = REFERENCE_SENSITIVITY_NOTE[rs.status];
  let label, summary;
  if (!known) {
    // A status this build cannot name is shown verbatim, the way every other
    // table in this file does it. It used to fall through to the
    // `unavailable` entry and render as "not checked" — a specific claim
    // about what the engine did, made on behalf of a value nobody here read.
    const facts = referenceFacts(rs);
    const under = referenceBlocksPhrase(rs);
    label = `Reference sensitivity: ${rs.status == null ? "no status given" : rs.status}`;
    summary =
      "This build does not recognise that reference-sensitivity status, so it is shown " +
      "verbatim. It is not a verdict that the answer survives a moved reference window." +
      `${rs.reason ? ` The engine's reason: ${rs.reason}.` : ""}` +
      `${facts ? ` What the engine reported${under ? ` under ${under}` : ""}: ${facts}.` : ""}` +
      referenceBlocksUnanswered(rs);
  } else if (rs.status === "stable") {
    ({ label, summary } = stableSummary(rs));
  } else if (rs.status === "unstable") {
    const what = [];
    if (rs.top_cause_stable === false) what.push("the top cause changes");
    if (rs.gap_sign_stable === false) what.push("the gap changes direction");
    const under = referenceBlocksPhrase(rs);
    label = known.label;
    summary =
      `Moving the reference block: ${what.join(" and ") || "the answer changes"} — read the published ranking as one reading among several, not the finding.` +
      `${under ? ` Compared against ${under}.` : ""}${referenceBlocksUnanswered(rs)}`;
  } else {
    label = known.label;
    summary = rs.reason ? `${known.explains} ${rs.reason}.` : known.explains;
  }
  const gr = rs.gap_range;
  const range =
    Array.isArray(gr) && gr.length === 2 && gr.every((v) => typeof v === "number" && Number.isFinite(v))
      ? `Gap across the blocks tried: ${fmtNum(gr[0])} to ${fmtNum(gr[1])} — a sensitivity band, not an interval.`
      : "";
  return {
    status: rs.status,
    known: !!known,
    label,
    summary,
    range,
    details: referenceSensitivityDetails(rs, fmtNum),
  };
}

/* The block for the live Root cause tab and the export. `cls.warn` is the
   caveat channel of the surface (amber), `cls.ok` its muted one: a stable
   verdict is information, not a warning, and must not shout. The details
   always print — a reader of a circulated report has nothing to hover. */
function referenceSensitivityHtml(res, cls) {
  const n = referenceSensitivityNote(res, cls.fmt);
  if (!n) return "";
  const esc = cls.esc;
  const klass = n.status === "stable" ? cls.ok : cls.warn;
  // An unknown status takes the caveat channel and the "not said" mark: it
  // may be good news, and this build cannot tell.
  const mark = n.status === "stable" ? "✓" : n.status === "unstable" ? "⚠" : "◌";
  const details = n.details.length
    ? `<br><span class="sens-details">${n.details.map((d) => esc(d)).join("<br>")}</span>`
    : "";
  return `<p class="${klass} sens-${n.known ? esc(n.status) : "unknown"}">${mark} <strong>${esc(n.label)}.</strong> ${esc(n.summary)}${n.range ? ` ${esc(n.range)}` : ""}${details}</p>`;
}

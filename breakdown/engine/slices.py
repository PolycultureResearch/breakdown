"""Dimensional slice attribution: where inside a metric did the gap happen?

Tree RCA answers *which upstream metric* moved; slicing answers *where inside
that metric* — which geo, plan, app version. `slice_attribution` attributes a
metric's window-over-window gap across the values of one declared dimension.

Slices localize; the tree explains: a concentrated slice says where to look
next, not why it moved.

The math is the same family as the tree's formula attribution, in closed form:

- **Flows and stocks** are exact sum identities over slices
  (`signups[t] = Σ_g signups_g[t]`). The identity is linear, so the per-day
  Shapley decomposition collapses: each slice's attribution is exactly its own
  window-mean change, and attributions sum to the sliced gap with zero
  remainder.
- **Rates** blend: `r[t] = Σ_g s_g[t]·r_g[t]` with shares `s_g` from the
  dimension's declared `weight` metric sliced the same way. At the
  window-aggregate level the exact symmetric split per slice is the Bennet
  decomposition — precisely the two-player Shapley value of each product term:
  `within_g = s̄_g·Δr_g` (the slice's own rate moved) and `mix_g = r̄_g·Δs_g`
  (traffic shifted toward/away from it), with bars denoting two-window means.
  The parts sum to the blended gap exactly, and `Σ_g Δs_g = 0` makes the mix
  terms a pure reallocation signal, reported as `mix_total` (the analogue of
  the tree's `interaction` row).

Ranking is by **excess concentration**, not raw size: the biggest slice always
has the biggest raw contribution, so each slice also gets
`excess_g = contribution_g − baseline_share_g × gap` — how much more of the
gap it carries than its size predicts. `Σ_g excess_g = 0` (a zero-sum
reallocation of the gap), so excess is self-normalizing — and so slices are
ordered by excess *signed toward the gap*, never by |excess|, which would tie
on a two-value dimension and rank the culprit arbitrarily. Uncertainty comes
from the same circular moving-block window bootstrap as tree RCA (resampled
jointly across slices), giving each excess a credible interval and a
`prob_concentrated` direction probability — no per-slice model fits.

Slices are fetched on demand at analysis time and never enter the startup
`GrainedData` or the fit path. This module is pure — the caller fetches and
passes the long-format frames — so the engine stays stateless.
"""

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from breakdown.engine.rca import (
    _N_BOOT,
    _block_bootstrap_indices,
    _sample_summary,
    _window_info,
)
from breakdown.grains import BOOT_BLOCK, period_spine, snap_window

# More distinct fetched values than this is an authoring problem, not an
# analysis input — the response would localize nothing.
MAX_DISTINCT = 100

# A slice is flagged noise-level when its excess direction probability is
# below this — the bootstrap can't tell its concentration from zero.
_NOISE_PROB = 0.8


def _rank_by_excess(rows: List[Dict[str, Any]], gap: float) -> None:
    """Order slices by excess *in the direction of the gap*, most concentrated
    first.

    Ranking on |excess| looks equivalent but is not: `Σ excess = 0`, so on a
    two-value dimension the excesses are exactly ±x and the magnitudes tie —
    leaving the culprit's position down to dict ordering. Signing by the gap
    also states the right thing generally: when a metric fell, the slice that
    drove it is the one carrying *more of the decline* than its size predicts
    (most negative excess), and when it rose, the most positive. Slices pulling
    against the move sort to the bottom, where they read as the offsets they are.
    """
    direction = -1.0 if gap < 0 else 1.0
    rows.sort(key=lambda r: -direction * (r["excess"] if r["excess"] is not None else 0.0))


# Reconciliation: mean |Σ slices − metric| above this share of |baseline|
# flags the dimension as not cleanly partitioning the metric.
_RECON_TOL = 0.005

_OTHER = "__other__"
_NULL = "__null__"


def _pivot(long_df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Long `[date, slice, value]` → wide dates × slice values."""
    for col in ("date", "slice", "value"):
        if col not in long_df.columns:
            raise ValueError(
                f"Sliced data for {label} must have columns [date, slice, value]; "
                f"got {list(long_df.columns)}."
            )
    df = long_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["slice"] = df["slice"].map(
        lambda v: _NULL if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
    )
    wide = df.pivot_table(index="date", columns="slice", values="value", aggfunc="sum")
    wide.columns = [str(c) for c in wide.columns]
    return wide


def _fill_by_kind(wide: pd.DataFrame, dates: pd.DatetimeIndex, kind: str) -> pd.DataFrame:
    """Reindex onto the window dates and gap-fill per slice by kind: a missing
    (date, slice) is 0 for a flow, carried forward for a stock (0 before the
    slice's first observation — the plan tier didn't exist yet). Rates keep
    NaN; absence is handled through their weights."""
    out = wide.reindex(dates)
    if kind == "flow":
        return out.fillna(0.0)
    if kind == "stock":
        return out.ffill().fillna(0.0)
    return out


def _select_slices(
    wide: pd.DataFrame, top_k: int, pinned: Optional[List[str]]
) -> Tuple[List[str], List[str]]:
    """(kept, folded) slice names. Pinned values win; otherwise the top_k by
    mean |value| (name tiebreak, for determinism), rest folded to __other__."""
    names = list(wide.columns)
    if pinned is not None:
        kept = [str(v) for v in pinned if str(v) in wide.columns]
        folded = [n for n in names if n not in set(kept)]
        return kept, folded
    ranked = sorted(names, key=lambda n: (-float(np.abs(wide[n].to_numpy(float)).mean()), n))
    kept = ranked[:top_k]
    folded = ranked[top_k:]
    return sorted(kept), sorted(folded)


def _reconciliation(residual: np.ndarray, baseline: float) -> Dict[str, Any]:
    scale = max(abs(baseline), 1e-12)
    share = float(np.abs(residual).mean()) / scale
    return {
        "mean_residual": float(residual.mean()),
        "max_abs_residual": float(np.abs(residual).max()),
        "residual_share_of_baseline": share,
        "status": "ok" if share <= _RECON_TOL else "discrepant",
    }


def slice_attribution(
    defn: Any,
    dimension: str,
    sliced: pd.DataFrame,
    unsliced: pd.DataFrame,
    reference_start: str,
    reference_end: str,
    analysis_start: str,
    analysis_end: str,
    weight_sliced: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Attribute `defn`'s window-over-window gap across one dimension's slices.

    `sliced` is the metric's long-format `[date, slice, value]` frame covering
    both windows; `unsliced` is its `[date, <name>]` series over the same range
    (the reconciliation anchor); `weight_sliced` is the dimension's `weight`
    metric sliced the same way, required when `defn.kind == "rate"`.

    Windows are day-resolution inclusive `[start, end]` and snap to whole
    periods at the metric's grain, exactly as in tree RCA. When slicing a
    lagged parent of an RCA target, pass the parent's own lag-shifted windows
    (the ones its contribution was measured over).
    """
    if dimension not in defn.dimensions:
        raise ValueError(
            f"Metric '{defn.name}' declares no dimension '{dimension}' "
            f"(has {sorted(defn.dimensions)})."
        )
    spec = defn.dimensions[dimension]
    grain = defn.grain
    kind = defn.kind

    snapped_ref = snap_window(reference_start, reference_end, grain)
    snapped_an = snap_window(analysis_start, analysis_end, grain)
    if snapped_ref is None or snapped_an is None:
        which, s, e = (
            ("reference", reference_start, reference_end)
            if snapped_ref is None
            else ("analysis", analysis_start, analysis_end)
        )
        raise ValueError(
            f"The {which} window [{s}, {e}] contains no whole '{grain}' period for '{defn.name}'."
        )
    ref_dates = period_spine(snapped_ref.first_start, snapped_ref.last_end, grain)
    an_dates = period_spine(snapped_an.first_start, snapped_an.last_end, grain)
    all_dates = ref_dates.union(an_dates)
    single_period = snapped_ref.n_periods == 1 or snapped_an.n_periods == 1
    block = BOOT_BLOCK[grain]
    rng = np.random.default_rng(0)

    wide = _pivot(sliced, f"'{defn.name}'")
    if wide.shape[1] > MAX_DISTINCT:
        raise ValueError(
            f"Dimension '{dimension}' on '{defn.name}' returned {wide.shape[1]} "
            f"distinct values (max {MAX_DISTINCT}). Declare a `values:` pin-list "
            "or slice by a coarser dimension."
        )
    wide = _fill_by_kind(wide, all_dates, kind)

    u = unsliced.copy()
    u["date"] = pd.to_datetime(u["date"])
    u_series = u.set_index("date")[defn.name].reindex(all_dates)
    if u_series.isna().any():
        missing = [str(d.date()) for d in all_dates[u_series.isna()][:5]]
        raise ValueError(
            f"Unsliced series for '{defn.name}' is missing periods {missing} "
            "inside the requested windows."
        )

    caveats: List[str] = []
    if spec.values is not None:
        absent = [v for v in spec.values if str(v) not in wide.columns]
        if absent:
            caveats.append(f"Pinned values not present in fetched data: {absent}.")

    if kind == "rate":
        if weight_sliced is None:
            raise ValueError(
                f"Rate metric '{defn.name}' needs its weight metric "
                f"'{spec.weight}' sliced over '{dimension}' to recompose the "
                "blended rate."
            )
        result = _rate_attribution(
            defn,
            spec,
            wide,
            _pivot(weight_sliced, f"weight '{spec.weight}'"),
            u_series,
            ref_dates,
            an_dates,
            all_dates,
            rng,
            block,
            single_period,
            caveats,
        )
    else:
        result = _sum_attribution(
            defn,
            spec,
            wide,
            u_series,
            ref_dates,
            an_dates,
            all_dates,
            rng,
            block,
            single_period,
            caveats,
        )

    result.update(
        {
            "metric": defn.name,
            "dimension": dimension,
            "dimension_source": spec.source,
            "grain": grain,
            "kind": kind,
            "effective_windows": {
                "reference": _window_info(snapped_ref),
                "analysis": _window_info(snapped_an),
            },
            "ci_status": "degenerate_single_period" if single_period else "ok",
            "caveats": caveats,
        }
    )
    return result


def _excess_fields(excess_b: Optional[np.ndarray], single_period: bool) -> Dict[str, Any]:
    """CI / direction-probability fields for one slice's excess replicates."""
    if single_period or excess_b is None:
        return {"ci_95": None, "prob_concentrated": None, "noise_level": None}
    prob = float(max((excess_b > 0).mean(), (excess_b < 0).mean()))
    return {
        "ci_95": [
            float(np.percentile(excess_b, 2.5)),
            float(np.percentile(excess_b, 97.5)),
        ],
        "prob_concentrated": prob,
        "noise_level": prob < _NOISE_PROB,
    }


def _sum_attribution(
    defn,
    spec,
    wide,
    u_series,
    ref_dates,
    an_dates,
    all_dates,
    rng,
    block,
    single_period,
    caveats,
) -> Dict[str, Any]:
    """Flows and stocks: the sum identity's closed-form attribution.

    Equivalent to the per-day Shapley decomposition of the identity
    `x = Σ_g x_g`: for a linear formula the means-bridge game gives each slice
    its own window-mean change and both within-window co-movement games vanish,
    so no coalition enumeration is needed.
    """
    kept, folded = _select_slices(wide, spec.top_k, spec.values)
    groups = dict.fromkeys(kept)
    for g in kept:
        groups[g] = wide[g].to_numpy(float)
    if folded:
        groups[_OTHER] = wide[folded].to_numpy(float).sum(axis=1)

    names = list(groups)
    X = np.column_stack([groups[g] for g in names])  # (n_dates, m)
    in_ref = all_dates.isin(ref_dates)
    in_an = all_dates.isin(an_dates)
    X_ref, X_an = X[in_ref], X[in_an]
    total_ref, total_an = X_ref.sum(axis=1), X_an.sum(axis=1)

    baseline = float(total_ref.mean())
    actual = float(total_an.mean())
    gap = actual - baseline

    ref_means = X_ref.mean(axis=0)
    an_means = X_an.mean(axis=0)
    contribution = an_means - ref_means
    have_share = abs(baseline) >= 1e-12
    baseline_share = ref_means / baseline if have_share else None
    excess = contribution - baseline_share * gap if have_share else None

    if single_period:
        contribution_ci = excess_b = None
    else:
        ref_idx = _block_bootstrap_indices(len(X_ref), _N_BOOT, rng, block=block)
        an_idx = _block_bootstrap_indices(len(X_an), _N_BOOT, rng, block=block)
        # One index set per window shared across slices (joint resampling), so
        # cross-slice correlation within a window is preserved — same rationale
        # as tree RCA's cross-parent joint bootstrap.
        ref_means_b = X_ref[ref_idx].mean(axis=1)  # (n_boot, m)
        an_means_b = X_an[an_idx].mean(axis=1)
        total_ref_b = ref_means_b.sum(axis=1)
        total_an_b = an_means_b.sum(axis=1)
        gap_b = total_an_b - total_ref_b
        contribution_ci = an_means_b - ref_means_b
        with np.errstate(divide="ignore", invalid="ignore"):
            share_b = np.where(
                np.abs(total_ref_b)[:, None] >= 1e-12,
                ref_means_b / total_ref_b[:, None],
                np.nan,
            )
        excess_b = contribution_ci - share_b * gap_b[:, None]

    rows = []
    for j, g in enumerate(names):
        row = {
            "value": g,
            "baseline": float(ref_means[j]),
            "actual": float(an_means[j]),
            "contribution": float(contribution[j]),
            "share_of_gap": float(contribution[j] / gap) if abs(gap) >= 1e-12 else None,
            "baseline_share": float(baseline_share[j]) if have_share else None,
            "excess": float(excess[j]) if have_share else None,
        }
        row.update(_excess_fields(excess_b[:, j] if excess_b is not None else None, single_period))
        if g == _OTHER:
            row["n_values"] = len(wide.columns) - len(kept)
        rows.append(row)
    _rank_by_excess(rows, gap)

    if _OTHER in groups and have_share and abs(baseline) >= 1e-12:
        other_share = float(groups[_OTHER][in_ref].mean() / baseline)
        if other_share > 0.5:
            caveats.append(
                f"__other__ holds {other_share:.0%} of the baseline — the "
                "dimension is too fragmented to localize; raise top_k or pin "
                "`values:`."
            )

    residual = X[in_ref | in_an].sum(axis=1) - u_series.to_numpy(float)[in_ref | in_an]
    recon = _reconciliation(residual, float(u_series[in_ref].mean()))
    if recon["status"] == "discrepant":
        caveats.append(
            "Slices do not sum to the metric within tolerance — the dimension "
            "does not cleanly partition it; treat slice attributions as "
            "approximate."
        )

    return {
        "attribution_method": "slice_sum",
        "baseline": baseline,
        "actual": actual,
        "gap": gap,
        "slices": rows,
        "mix_total": None,
        "reconciliation": recon,
    }


def _window_aggregates(W: np.ndarray, R: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(shares, weighted rates) per slice for one window (or, with a leading
    replicate axis, per bootstrap replicate). `W`/`R` are (..., n_dates, m)."""
    weight = W.sum(axis=-2)
    total = weight.sum(axis=-1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(total > 0, weight / total, 0.0)
        r = np.where(weight > 0, (W * R).sum(axis=-2) / weight, np.nan)
    return s, r


def _bennet(
    s_ref: np.ndarray, r_ref: np.ndarray, s_an: np.ndarray, r_an: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Exact symmetric Bennet split of Δ(Σ s·r) per slice: (within, mix).

    A slice with zero weight in one window keeps its other window's rate
    (Δr = 0), so a brand-new or vanished slice contributes entirely through
    mix — the honest reading of a composition change.
    """
    r_ref = np.where(np.isnan(r_ref), r_an, r_ref)
    r_an = np.where(np.isnan(r_an), r_ref, r_an)
    s_bar = (s_ref + s_an) / 2.0
    r_bar = (r_ref + r_an) / 2.0
    within = s_bar * (r_an - r_ref)
    mix = r_bar * (s_an - s_ref)
    # A slice with no weight in either window (possible per bootstrap
    # replicate) genuinely contributes nothing.
    return np.nan_to_num(within), np.nan_to_num(mix)


def _rate_attribution(
    defn,
    spec,
    wide,
    weights,
    u_series,
    ref_dates,
    an_dates,
    all_dates,
    rng,
    block,
    single_period,
    caveats,
) -> Dict[str, Any]:
    """Rates: the weight-blended mix/within decomposition."""
    weights = weights.reindex(all_dates).fillna(0.0)
    common = [c for c in wide.columns if c in set(weights.columns)]
    orphan_rates = [c for c in wide.columns if c not in set(weights.columns)]
    if orphan_rates:
        caveats.append(
            f"Slices with a rate but no weight in '{spec.weight}' were dropped: {orphan_rates[:5]}."
        )
    orphan_weights = [c for c in weights.columns if c not in set(wide.columns)]
    if orphan_weights:
        caveats.append(
            f"Slices with weight in '{spec.weight}' but no rate were dropped: {orphan_weights[:5]}."
        )
    wide = wide[common]
    weights = weights[common]

    R = wide.to_numpy(float)
    W = weights.to_numpy(float)
    bad = np.isnan(R) & (W > 0)
    if bad.any():
        d, j = np.argwhere(bad)[0]
        raise ValueError(
            f"Rate '{defn.name}' slice '{common[j]}' is missing on "
            f"{all_dates[d].date()} while its weight is nonzero — a rate "
            "cannot be invented."
        )
    R = np.nan_to_num(R, nan=0.0)  # weight is 0 wherever the rate was absent

    # Rank rate slices by weight volume — the natural size of a rate's slice.
    kept, folded = _select_slices(weights, spec.top_k, spec.values)
    names = kept + ([_OTHER] if folded else [])
    m = len(names)
    idx_of = {c: j for j, c in enumerate(common)}

    def _group(M: np.ndarray, weighted_by: Optional[np.ndarray] = None) -> np.ndarray:
        out = np.zeros((M.shape[0], m))
        for j, g in enumerate(kept):
            out[:, j] = M[:, idx_of[g]]
        if folded:
            cols = [idx_of[g] for g in folded]
            if weighted_by is None:
                out[:, -1] = M[:, cols].sum(axis=1)
            else:
                w = weighted_by[:, cols]
                with np.errstate(divide="ignore", invalid="ignore"):
                    out[:, -1] = np.where(
                        w.sum(axis=1) > 0,
                        (M[:, cols] * w).sum(axis=1) / w.sum(axis=1),
                        0.0,
                    )
        return out

    # The weighted merge preserves per-window products (s·r), so the Bennet
    # split still telescopes exactly after folding into __other__.
    Wg = _group(W)
    Rg = _group(R, weighted_by=W)

    in_ref = all_dates.isin(ref_dates)
    in_an = all_dates.isin(an_dates)
    s_ref, r_ref = _window_aggregates(Wg[in_ref], Rg[in_ref])
    s_an, r_an = _window_aggregates(Wg[in_an], Rg[in_an])

    both_zero = (Wg[in_ref].sum(axis=0) == 0) & (Wg[in_an].sum(axis=0) == 0)
    baseline = float(np.nansum(s_ref * r_ref))
    actual = float(np.nansum(s_an * r_an))
    gap = actual - baseline

    within, mix = _bennet(s_ref, r_ref, s_an, r_an)
    contribution = within + mix
    within_total = float(within.sum())
    s_bar = (s_ref + s_an) / 2.0
    excess = within - s_bar * within_total

    if single_period:
        excess_b = None
        mix_total = {"estimate": float(mix.sum()), "ci_95": None}
    else:
        ref_idx = _block_bootstrap_indices(int(in_ref.sum()), _N_BOOT, rng, block=block)
        an_idx = _block_bootstrap_indices(int(in_an.sum()), _N_BOOT, rng, block=block)
        # Joint date resampling: weights and rates share each replicate's
        # dates, so share/rate co-movement inside a window is preserved.
        s_ref_b, r_ref_b = _window_aggregates(Wg[in_ref][ref_idx], Rg[in_ref][ref_idx])
        s_an_b, r_an_b = _window_aggregates(Wg[in_an][an_idx], Rg[in_an][an_idx])
        within_b, mix_b = _bennet(s_ref_b, r_ref_b, s_an_b, r_an_b)
        s_bar_b = (s_ref_b + s_an_b) / 2.0
        excess_b = within_b - s_bar_b * within_b.sum(axis=1, keepdims=True)
        mix_total = _sample_summary(mix_b.sum(axis=1))

    rows = []
    for j, g in enumerate(names):
        if both_zero[j]:
            continue
        row = {
            "value": g,
            "share_reference": float(s_ref[j]),
            "share_analysis": float(s_an[j]),
            "rate_reference": None if np.isnan(r_ref[j]) else float(r_ref[j]),
            "rate_analysis": None if np.isnan(r_an[j]) else float(r_an[j]),
            "within": float(within[j]),
            "mix": float(mix[j]),
            "contribution": float(contribution[j]),
            "share_of_gap": float(contribution[j] / gap) if abs(gap) >= 1e-12 else None,
            "excess": float(excess[j]),
        }
        row.update(_excess_fields(excess_b[:, j] if excess_b is not None else None, single_period))
        if g == _OTHER:
            row["n_values"] = len(folded)
        rows.append(row)
    _rank_by_excess(rows, gap)

    # Per-date blend vs the unsliced rate: does Σ s_g·r_g reproduce the metric?
    w_totals = W.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        blend_t = np.where(w_totals > 0, (W * R).sum(axis=1) / w_totals, np.nan)
    mask = (in_ref | in_an) & ~np.isnan(blend_t)
    u_vals = u_series.to_numpy(float)
    recon = _reconciliation(blend_t[mask] - u_vals[mask], float(u_vals[in_ref].mean()))
    if recon["status"] == "discrepant":
        caveats.append(
            "The weight-blended slices do not reproduce the metric within "
            "tolerance — check that `weight` is the rate's true denominator; "
            "treat slice attributions as approximate."
        )

    # The tree's node gap is the unweighted window-mean difference; the sliced
    # gap is weight-blended. Surface a material difference rather than letting
    # the two headline numbers silently disagree.
    node_gap = float(u_vals[in_an].mean() - u_vals[in_ref].mean())
    if abs(gap - node_gap) > 0.02 * max(abs(node_gap), 1e-12):
        caveats.append(
            f"Weight-blended gap ({gap:.4g}) differs from the node's unweighted "
            f"window-mean gap ({node_gap:.4g}); the difference is a weighting "
            "effect, not a slice's doing."
        )

    return {
        "attribution_method": "slice_blend",
        "baseline": baseline,
        "actual": actual,
        "gap": gap,
        "slices": rows,
        "mix_total": mix_total,
        "reconciliation": recon,
    }

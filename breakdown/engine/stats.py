"""The engine's shared uncertainty vocabulary: one implementation each of the
bootstrap, the degeneracy guards, and the direction probability.

Extracted from `rca.py` (roadmap C29/C30, grill 2026-08-29 H1/H6/M8) because
the alternative was measured and failed: `rca.py`, `slices.py` and
`simulate.py` each carried their own copy of "percentile pair, withheld if
degenerate" and "is this gap zero at this node's scale?", and the copies
drifted — the posterior attribution branch had neither the finite filter nor a
non-finite status (H1), `slices` published zero-width intervals C4 retired
(H6a), and every gap test outside `rca.py` was still the absolute `1e-12`
epsilon whose retirement C5's own docstring records (H6b). Three modules
imported five underscore-"private" names across the boundary; the underscores
were lying. These names are public because they are, in fact, the contract.

Nothing here knows about DAGs, windows-as-dates, or payloads: inputs are
arrays and scalars, outputs are numbers, dicts of numbers, or None-meaning-
withheld. The window→scalar vocabulary lives in `engine/windows.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np

# Bootstrap replicates per window; fixed so contribution CIs are comparable
# across nodes and runs.
N_BOOT = 500

# Surviving replicates required before a bootstrap interval is reported at all
# (one threshold for every module that filters replicates to finite).
MIN_CI_REPLICATES = 100

# The moving-block bootstrap's block may not exceed this fraction of the window
# (see `block_bootstrap_indices`).
BLOCK_CAP_DIVISOR = 4

# A published interval is degenerate — withheld rather than reported — when its
# width is this small *relative to the node's own level*. An absolute threshold
# is wrong here: a metric denominated in millions carries float noise of a size
# that a metric denominated in rates never reaches. 1e-9 of the node's level is
# far below any interval a real resampling produces and comfortably above the
# ~1e-13 relative noise of a mean-of-means over 500 replicates, so it catches
# the exactly-zero case and its rounding-artifact neighbours without ever
# swallowing a real interval.
DEGENERATE_CI_REL = 1e-9

# `gap` is treated as zero — and `share_of_gap` withheld — below this fraction
# of the node's own level. Relative, not absolute (roadmap C5): a $1e-6 gap on a
# $26K node and a 1e-6 gap on a rate of 0.4 are not the same claim.
GAP_REL_EPS = 1e-12


def prob_same_direction(
    samples: np.ndarray, n_effective: Optional[int] = None
) -> Tuple[float, bool]:
    """Share of `samples` on the dominant side of zero, published at the
    resolution the estimator actually has.

    This is a proportion over a finite sample, so the only values it can take
    are `k/n`: with `n = N_BOOT` there is **nothing between 1 − 1/500 = 0.998
    and 1.0**. A saturated count — every replicate landing on one side — is
    therefore not a measurement of certainty; it is the estimator running out
    of resolution, and it happens most readily where the evidence is thinnest.
    Publishing it as `1.00` claimed a certainty no bootstrap can express and
    added a decimal place it does not have, which is C5's defect (a saturated
    clamp reading as certainty) on the probability side rather than the
    interval side.

    So a saturated estimate publishes the ceiling and says it is censored
    there: the second return value is True, and every renderer prints the
    number as a bound (`>99.8%`) rather than a value. Callers that hold a
    coarser factor pass `n_effective` — the RCA posterior path multiplies the
    coefficient posterior by only `N_BOOT` distinct resampled window deltas,
    so more posterior draws buy no window-sampling resolution.

    A sample with **no spread at all** is exempt: an identical set is not an
    estimate of a proportion but exact arithmetic (a deterministic propagation
    in `simulate`), and its sign is genuinely known. In RCA that case never
    arrives here — the caller withholds the interval and the probability with
    it, "a confidence read off no information at all".

    Non-finite samples are refused, not averaged over: a NaN fails both `> 0`
    and `< 0`, so it silently deflates the proportion — an arithmetically
    impossible value for an estimator floored at 0.5, which is exactly how
    grill H1 (roadmap C29) was noticed. Engine callers route through
    `direction_fields`, which withholds instead; a direct caller reaching this
    raise has skipped the upstream refuse-or-withhold contract, and a named
    error beats an impossible number.
    """
    if not np.isfinite(samples).all():
        raise ValueError(
            "prob_same_direction received non-finite samples; the caller must "
            "refuse or withhold (roadmap C29) rather than average over them."
        )
    p = float(max((samples > 0).mean(), (samples < 0).mean()))
    n = samples.size if n_effective is None else min(samples.size, n_effective)
    ceiling = 1.0 - 1.0 / n if n else 1.0
    if p <= ceiling or float(samples.min()) == float(samples.max()):
        return p, False
    return ceiling, True


def direction_fields(
    samples: Optional[np.ndarray],
    key: str = "prob_same_direction",
    n_effective: Optional[int] = None,
) -> Dict[str, Any]:
    """`prob_same_direction`'s payload fields, for whichever name a surface
    gives it (`prob_concentrated` on slices, `prob_direction` on what-if).

    `None` samples means withheld — and samples carrying a non-finite value are
    withheld the same way, because a direction probability computed over NaNs
    is not a probability (roadmap C29). The `<key>_censored` flag follows the
    `lag`/`parent_windows` idiom — present only when it is true, so an
    uncensored payload is byte-identical to what it was before.
    """
    if samples is None or not np.isfinite(samples).all():
        return {key: None}
    value, censored = prob_same_direction(samples, n_effective)
    out: Dict[str, Any] = {key: value}
    if censored:
        out[f"{key}_censored"] = True
    return out


def effective_block(n: int, block: int) -> int:
    """The block length actually used on a window of `n` periods.

    Non-decreasing in `n` — the property the previous ``n // 2`` cap lacked,
    which is what let a user widen a window by one period and get a *narrower*
    interval. See `block_bootstrap_indices` for the measurements behind the
    divisor.
    """
    return max(1, min(block, n // BLOCK_CAP_DIVISOR))


def block_bootstrap_indices(n: int, n_boot: int, rng, block: int = 7) -> np.ndarray:
    """(n_boot, n) integer index array; circular moving-block bootstrap.

    Each replicate concatenates ceil(n / block') blocks of block' consecutive
    positions (wrapping circularly) starting at uniform positions, truncated to
    n. The effective block length is capped at ``n // BLOCK_CAP_DIVISOR``
    (min 1): the circular block bootstrap's resampled variance of a window mean
    is deflated by roughly ``1 - block / n`` — with one block covering the whole
    window it degenerates to rotations whose means are all identical and the
    variance collapses to zero — so the cap is what bounds that deflation.

    **Why a quarter** (roadmap C4b; the sweep is in the C4 report and is where
    roadmap S6 starts). Measured as the ratio of the resampled variance of the
    window mean to its true sampling variance, over 400 realizations x 500
    replicates per cell:

    - iid series, under the previous ``n // 2`` cap: 0.46-0.63 for every
      n <= 16, i.e. the interval was routinely *half* the width it should be
      precisely on short windows — and non-monotone in n, so n=13 measured 0.55
      and n=14 measured 0.50. A user widening the window by a day got a
      narrower interval.
    - iid series, under ``n // 4``: 0.74-0.90 across every n from 4 to 56, and
      the block rule ``min(BOOT_BLOCK, n // 4)`` is itself non-decreasing in n.
      Residual wobble in the ratio (<= 0.11) is unavoidable with an integer
      block length; what the cap buys is a *bound*, ~25% attenuation instead of
      ~50%.
    - AR(1), rho=0.6: ``n // 4`` sits on or within 0.03 of the best block length
      at every n measured (n=8, 12, 14, 16, 20, 28), while ``n // 2`` gave up
      0.05-0.07 of the ratio at each. Under serial dependence the cap is not a
      trade-off at all — the old one was simply on the wrong side of the peak.

    Note the AR(1) ratios are 0.17-0.61 in absolute terms whatever the block
    length: a short window carries little information about a serially
    dependent series' long-run variance, and no choice of block recovers it.
    That is a property of window resampling, not of this cap.
    """
    if n < 1:
        raise ValueError("Cannot bootstrap an empty window.")
    block = effective_block(n, block)
    n_blocks = -(-n // block)  # ceil
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(n_boot, n_blocks * block)[:, :n]


def node_scale(baseline: float, actual: float) -> float:
    """The node's own level, used as the yardstick for "is this number zero?".

    Contributions, gaps and interval widths are all in the node's units, so its
    window means are the natural scale to judge them against. Non-finite window
    means score as no scale at all, which makes every relative test fall back to
    exact zero.
    """
    values = [abs(v) for v in (baseline, actual) if v is not None and np.isfinite(v)]
    return max(values) if values else 0.0


def degenerate_means(means: np.ndarray) -> bool:
    """True when a window's resampled means never move (roadmap C4a).

    This is the condition `single_period` was a special case of: with one
    period there is nothing to resample, but a parent that is *constant* over
    any window — an unlaunched feature, an unmoved stock, a seasonal business
    between cycles — resamples to the same mean just as surely, and the window
    contributes no sampling uncertainty at all.

    Judged relative to the parent's own level, so it holds for a metric
    denominated in millions as well as one denominated in rates, and so a
    spread that is a rounding artifact counts as no spread. A parent held
    identically at zero has level zero and needs the spread to be exactly zero,
    which it is.
    """
    lo = float(np.min(means))
    hi = float(np.max(means))
    return hi - lo <= DEGENERATE_CI_REL * max(abs(lo), abs(hi))


def negligible_gap(gap: Optional[float], scale: float) -> bool:
    """Whether `gap` is indistinguishable from zero at the node's own level.

    The single spelling of C5's test. The old absolute `abs(gap) < 1e-12`
    survived in `slices.py` and `simulate.py` long after `rca.py` retired it
    (grill H6b): a $1e-4 gap on a $1e9 node — pure float residue — passed the
    absolute test and published shares in the thousands. Non-finite counts as
    negligible for the purpose of *this* question ("may I divide by it / paint
    a direction off it?"); callers that must distinguish undefined from small
    check finiteness first.
    """
    return gap is None or not np.isfinite(gap) or abs(gap) <= GAP_REL_EPS * scale or gap == 0


def share_of_gap(estimate: float, gap: float, scale: float) -> Optional[float]:
    """`estimate / gap`, or None when the division would not be a measurement.

    Shares are deliberately *not* clamped — two parents pulling in opposite
    directions is the case the field exists to express, and `docs/model.md`
    says so. Withheld in exactly two cases: a gap that is not distinguishable
    from zero relative to the node's level (roadmap C5, `negligible_gap`), and
    a non-finite numerator (roadmap C29) — the old code checked only the gap,
    so a NaN estimate published a NaN share.
    """
    if negligible_gap(gap, scale):
        return None
    if estimate is None or not np.isfinite(estimate):
        return None
    return estimate / gap


def sample_summary(samples: np.ndarray, scale: float = 0.0) -> Dict[str, Any]:
    """Point estimate plus a 95% interval — or no interval at all.

    No caller of this function may publish a zero-width `ci_95` (roadmap C4).
    Two quite different things produce one: a resampling that collapsed (the
    node's `ci_status` says so), and a quantity that is zero *by construction*
    — a term the model does not contain, or an identity with no co-movement.
    Both are honestly reported as "no interval"; only the first is a
    degeneracy, and the second is better prevented upstream by not emitting the
    term at all.

    `scale` is the level to judge "zero width" against — the node's own, for
    quantities in the node's units. Without one, only an exactly zero width
    counts, which is the conservative reading for a caller that has no scale to
    offer.

    Samples carrying a non-finite value summarize to nothing at all —
    `estimate: None, ci_95: None` — because a mean over NaNs is NaN and a
    percentile over them is not a percentile (roadmap C29). Callers that can
    salvage a subset filter to finite *before* calling, and say so in their
    `ci_status`; this function never filters, because a silently-censored
    summary is a different number wearing the same key.
    """
    if not np.isfinite(samples).all():
        return {"estimate": None, "ci_95": None}
    lo = float(np.percentile(samples, 2.5))
    hi = float(np.percentile(samples, 97.5))
    return {
        "estimate": float(samples.mean()),
        "ci_95": None if hi - lo <= DEGENERATE_CI_REL * scale else [lo, hi],
    }

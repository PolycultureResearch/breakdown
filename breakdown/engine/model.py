"""Bayesian structural time series (BSTS) fitting for metric-tree nodes.

`fit_metric` is the entry point: given the metric DAG and a tidy DataFrame of
metric time series, it fits one node's model and returns a `FitResult` (the
trace plus the normalization constants, fitted date index, and fit metadata the
rest of the system needs). It holds no state — callers own the trace cache.

Window-over-window attribution (Shapley and posterior-based) lives in
`breakdown.engine.rca`; only the pure Shapley computation is defined here.
"""

import logging
import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.formula import eval_formula
from breakdown.grains import ensure_grained, fit_grain, next_start
from breakdown.parser import MetricDefinition

# `pymc`, `arviz` and `pytensor` are imported *inside* the five functions that
# use them, not here. They are ~80% of the process's import cost (2.3s of 2.5s
# locally, and this module sits on `breakdown.api.main`'s import path), which on
# a shared-CPU VM is the difference between a server that binds its port in
# seconds and one that takes ~43s — long enough that Fly's proxy gave up on the
# public demo and returned 503 to the first visitor after every idle period.
# Nothing at module scope touches them, so deferring costs nothing at runtime:
# the first fit pays a one-time import, and `warm_inference_imports` below is
# what keeps even that off the user's first click.
#
# `test_api_import_does_not_load_pymc` pins this — it is easy to undo by adding
# one convenient top-level import.

logger = logging.getLogger(__name__)

# Minimum whole periods (at the node's grain, after lag trimming) a fit needs.
# `breakdown doctor` reports per-metric readiness against the same number.
MIN_FIT_PERIODS = 10


def _enforce_fit_length(
    target: str,
    grain: str,
    n_joined: int,
    n_windowed: int,
    max_lag: int,
    fit_end: Optional[str],
) -> None:
    """Refuse a fit that would train on fewer than `MIN_FIT_PERIODS` periods.

    One check on the one path every fit takes, counting what the fit will
    actually train on: whole periods at the node's own grain surviving the
    inner join with its parents (`GrainedData.fit_frame`), then the `fit_end`
    whole-period cut, then the lag trim. It used to be two checks — one behind
    `fit_end is not None`, one behind `lags` — so neither `POST /analyze`'s
    default nor `run_scenario`'s ever ran it, and a three-observation series
    fitted and reported `fit_quality: "ok"` while `breakdown doctor` called the
    same metric "not fittable yet" and the README named 10 periods as the floor.

    Refusing rather than fitting-and-flagging, for three reasons. The model
    carries one latent trend state per observation, so a series this short has
    more latent parameters than data points and its intervals are not a
    measurement of anything — `_advi_diagnostics` only checks that the ELBO
    stopped moving, which such a fit does happily. `run_rca` already degrades a
    raised fit to a per-node `fit_failed` status carrying this message, so one
    thin node reports its own baseline/actual/gap (read off the data, not the
    model) and the rest of the tree still answers: refusal here costs a demo
    nothing and buys the operator the reason. And a flag on a number that is
    still returned is an invitation to use it, which is the failure mode this
    project exists to avoid.
    """
    n_fit = n_windowed - max_lag
    if n_fit >= MIN_FIT_PERIODS:
        return
    how = [f"{n_joined} whole {grain} periods cover '{target}' and its parents"]
    if fit_end is not None:
        how.append(f"{n_windowed} of them end on or before fit_end={fit_end}")
    if max_lag:
        how.append(f"the {max_lag}-period max lag trims {max_lag} more")
    raise ValueError(
        f"Only {n_fit} whole {grain} periods to fit '{target}' on "
        f"(need >= {MIN_FIT_PERIODS})."
        + (" " + "; ".join(how) + "." if len(how) > 1 else "")
        + " The model carries one latent trend state per period, so a series "
        "this short has more latent parameters than observations and its "
        "credible intervals would not mean anything. Widen the window, or wait "
        "for history to accumulate — `breakdown doctor --tree … --start-date … "
        "--end-date …` reports the same count per metric."
    )


@dataclass
class FitResult:
    """Everything a caller needs from one node's fit, not just the trace.

    `fit_metric` used to return a bare `arviz.InferenceData`, discarding the
    normalization constants, the effective date index, and the fit metadata.
    Downstream code (RCA's trend/seasonal decomposition, window-keyed caching,
    business-unit rendering) all need those pieces, so they are carried here.
    """

    trace: Any  # arviz.InferenceData
    target: str
    parents: List[str]  # regressor parents ([] for roots/formula nodes)
    y_mean: float  # of the fitted y series (residual for formula nodes)
    y_std: float
    x_stds: Optional[np.ndarray]  # per-parent stds of the (lag-shifted) regressors, None if no X
    dates: (
        pd.DatetimeIndex
    )  # period starts actually used in the fit (after lag trim and fit_end cut)
    inference_method: str  # "nuts" | "advi"
    fit_end: Optional[str] = None  # exclusive upper date bound of the fit; None = full window
    grain: str = "day"  # the grain the fit ran at (the node's own)
    diagnostics: Dict[str, Any] = field(default_factory=dict)  # populated by T8
    # Memoized JSON-safe `az.summary` of `trace`, filled in on first request by
    # the API's `_fit_summary`. The trace is immutable once fitted, so its
    # summary is too. Declared here rather than attached ad hoc from outside:
    # summarizing an 830-day trace costs ~1.1s, so a `slots=True` added here
    # later would silently turn the memo off and put that second back on every
    # `GET /metrics/{name}` — a defect no test would name.
    summary_json: Optional[Dict[str, Any]] = None
    # Roadmap S10: the per-period observed-vs-replicated band S3's p-values are
    # a summary of (see `_ppc_band`). A field of its own rather than a key in
    # `diagnostics`, and that placement is the design decision: `diagnostics`
    # is copied wholesale onto `GET /metrics/{name}`, and its `ppc` block is
    # copied onto every RCA node and shaped into every MCP payload. This array
    # scales with the fitted window, so riding in there would put ~88 kB on
    # each of a 106-metric RCA's nodes and hand an agent a decomposition it
    # cannot read. Here it can only reach the one route that asks for it.
    ppc_band: Optional[Dict[str, Any]] = None


def scale_prior_params(distribution: str, params: Dict[str, Any], scale: float) -> Dict[str, Any]:
    """
    Translate raw-scale (business-unit) prior parameters into normalized space.

    The model regresses z-scored y on z-scored x, so a raw-scale coefficient
    beta_raw maps to beta_norm = beta_raw * (x_std / y_std). `scale` is that
    x_std / y_std factor for one parent.
    """
    if distribution == "Normal":
        return {"mu": params.get("mu", 0.0) * scale, "sigma": params.get("sigma", 1.0) * scale}
    if distribution == "HalfNormal":
        return {"sigma": params.get("sigma", 1.0) * scale}
    if distribution == "Exponential":
        # Scaling an Exponential(lam) variable by s divides the rate by s.
        return {"lam": params.get("lam", 1.0) / scale}
    if distribution == "LogNormal":
        # Scaling a LogNormal by s shifts mu by log(s); sigma is unchanged.
        return {"mu": params.get("mu", 0.0) + np.log(scale), "sigma": params.get("sigma", 1.0)}
    raise ValueError(
        f"Unsupported prior distribution: '{distribution}'. "
        "Must be one of: Normal, HalfNormal, Exponential, LogNormal"
    )


# Prior distributions supported on an edge. Names, not the PyMC classes
# themselves, so this module imports without pymc (see the import note above);
# resolved with `getattr(pm, ...)` at the one call site. `Prior` in parser.py
# validates against the same set at parse time, so an unknown name cannot
# reach that lookup from a tree — the guard there is for direct callers.
_PRIOR_DISTRIBUTIONS = frozenset({"Normal", "HalfNormal", "Exponential", "LogNormal"})


#: Most parents a formula node may have and still be attributed exactly.
#:
#: The enumeration below is O(2^n) and RCA runs it six times per formula node
#: (three exact games, three over the bootstrap replicates), all while holding
#: the caller's per-tree lock — so the cost doubles per parent and serializes
#: every other request behind it. End to end through `run_rca` on a developer
#: laptop: 10 parents ~3.5s, 12 ~20s, 14 ~80s.
#:
#: 10 matches `_MAX_SOURCES` in `breakdown.engine.simulate`, which caps the
#: identical enumeration over scenario sources. Refusing is deliberate: a
#: sampled or truncated Shapley value is a *different number*, and this project
#: does not substitute one quietly for the one the author asked for.
_MAX_SHAPLEY_PARENTS = 10


def compute_shapley(
    formula: str,
    parent_names: List[str],
    baselines: Dict[str, Any],
    actuals: Dict[str, Any],
    node: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Distribute the gap between formula(actuals) and formula(baselines) across
    each parent using exact Shapley values (full coalition enumeration, O(2^n)).

    Values may be scalars or equal-length 1-d arrays. Arrays run one Shapley
    game per position (the per-day path: position = analysis-window day), with
    each coalition evaluated in a single vectorized formula call. The return
    type mirrors the input: Dict[str, float] for all-scalar inputs,
    Dict[str, np.ndarray] otherwise. Length-1 arrays/scalars broadcast.

    By Shapley efficiency the values sum (per position) to
    formula(actuals) - formula(baselines).

    Refuses more than `_MAX_SHAPLEY_PARENTS` parents — see that constant.
    `node` is the metric being attributed; it only names the node in that
    refusal, so callers that have it should pass it.
    """
    n = len(parent_names)
    if n == 0:
        return {}
    if n > _MAX_SHAPLEY_PARENTS:
        who = f"'{node}'" if node else f"with formula '{formula}'"
        raise ValueError(
            f"Formula node {who} has too many parents for exact Shapley "
            f"attribution: {n} parents, at most {_MAX_SHAPLEY_PARENTS} are "
            "supported (the coalition enumeration is O(2^n), so each extra "
            "parent doubles the work). Split the node into intermediate sums — "
            "group some parents under their own formula node and make that node "
            "the parent here — which preserves the identity and keeps every "
            "attribution exact."
        )

    array_input = any(
        isinstance(v, np.ndarray) and v.ndim > 0 for v in [*baselines.values(), *actuals.values()]
    )
    b_arr = {p: np.atleast_1d(np.asarray(baselines[p], dtype=float)) for p in parent_names}
    a_arr = {p: np.atleast_1d(np.asarray(actuals[p], dtype=float)) for p in parent_names}
    length = max(arr.shape[0] for arr in [*b_arr.values(), *a_arr.values()])
    for label, vals in (("baselines", b_arr), ("actuals", a_arr)):
        for p, arr in vals.items():
            if arr.shape[0] == 1 and length > 1:
                vals[p] = np.full(length, arr[0])
            elif arr.shape[0] != length:
                raise ValueError(
                    f"compute_shapley: {label}['{p}'] has length {arr.shape[0]}, expected {length}."
                )

    shapley: Dict[str, Any] = {}
    for player in parent_names:
        others = [p for p in parent_names if p != player]
        phi = np.zeros(length)
        for r in range(n):
            for coalition in combinations(others, r):
                coalition_set = set(coalition)
                weight = math.factorial(r) * math.factorial(n - r - 1) / math.factorial(n)

                vals_with = {
                    p: a_arr[p] if (p in coalition_set or p == player) else b_arr[p]
                    for p in parent_names
                }
                vals_without = {
                    p: a_arr[p] if p in coalition_set else b_arr[p] for p in parent_names
                }
                # `eval_formula` already silences numpy's warnings internally
                # (they are fatal under its restricted globals), but a formula
                # that produced an `inf` there makes this subtraction `inf-inf`
                # and warns here instead. Silence it: a non-finite result is
                # caught and reported by name in `shapley_attribution`, so the
                # warning adds nothing to the error and only lands in an
                # operator's log beside a 422 that already explains itself.
                with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                    phi = phi + weight * (
                        eval_formula(formula, vals_with) - eval_formula(formula, vals_without)
                    )

        shapley[player] = phi if array_input else float(phi[0])

    return shapley


def warm_inference_imports() -> None:
    """Import the inference stack now, so the first fit doesn't pay for it.

    Deferring `pymc`/`arviz`/`pytensor` out of module scope moves ~27s (on a
    shared-CPU VM) off startup — but onto whoever clicks *Run analysis* first,
    which in a live demo is strictly worse than a slow boot. The server calls
    this on a background thread once it is already listening, so the cost lands
    in the gap between the page rendering and the first analysis.

    Safe to call repeatedly and from any thread: after the first call these are
    satisfied from `sys.modules`. Never raises — a failure here only means the
    first fit pays full price, and the fit itself will report the real error.
    """
    try:
        import arviz  # noqa: F401
        import pymc  # noqa: F401
        import pytensor.tensor  # noqa: F401
    except Exception:  # pragma: no cover - diagnostics only
        logger.debug("inference import warm-up failed; first fit will pay for it", exc_info=True)


def summarize_trace(trace: Any) -> pd.DataFrame:
    """ArviZ posterior summary (mean, sd, 95% HDI, diagnostics) for a trace."""
    import arviz as az

    return az.summary(trace, hdi_prob=0.95)


def _nuts_diagnostics(trace: Any, draws: int, chains: int) -> Dict[str, Any]:
    """Convergence diagnostics for a NUTS trace.

    `fit_quality` is "suspect" when divergences exceed 1% of total draws, any
    r_hat exceeds 1.05, or any bulk ESS falls below 100 — coarse thresholds
    that flag only fits whose credible intervals should not be trusted as-is.
    Nothing blocks on "suspect"; it is information, not an error.
    """
    import arviz as az

    summary = az.summary(trace)
    divergences = int(trace.sample_stats.diverging.sum())
    rhat_vals = summary["r_hat"].to_numpy(dtype=float)
    ess_vals = summary["ess_bulk"].to_numpy(dtype=float)
    # r_hat / ess are NaN on single-chain traces; missing values don't flag.
    max_rhat = float(np.nanmax(rhat_vals)) if np.isfinite(rhat_vals).any() else None
    min_ess_bulk = float(np.nanmin(ess_vals)) if np.isfinite(ess_vals).any() else None
    suspect = (
        divergences > 0.01 * (draws * chains)
        or (max_rhat is not None and max_rhat > 1.05)
        or (min_ess_bulk is not None and min_ess_bulk < 100)
    )
    return {
        "fit_quality": "suspect" if suspect else "ok",
        "method": "nuts",
        "divergences": divergences,
        "max_rhat": max_rhat,
        "min_ess_bulk": min_ess_bulk,
    }


# Share of exactly-zero observations in the fit window above which the
# Gaussian likelihood's misspecification is disclosed. A trigger for a
# disclosure, not a statistic: at a quarter of the window the fitted sigma is
# already substantially a compromise between the zero regime and the live one,
# posterior mass sits on negative values of a non-negative series, and the
# intervals mis-state everywhere — the real fix is a zero-inflated or count
# likelihood (roadmap S20); until it lands, the reader gets told.
_ZERO_INFLATION_SHARE = 0.25


def _zero_inflation_warnings(values: np.ndarray, target: str, grain: str) -> list:
    """Disclose a zero-inflated fit window (S20's cheap half).

    Keys on *exact* zeros: a seasonal business's off-season is written as
    literal 0.0 by the flow zero-fill contract, and a tiny-but-nonzero value
    is a different regime, not this one. NaN periods never reach a fit
    (`fit_metric` refuses them, roadmap 1.11), so they cannot dilute the share.
    """
    n = int(values.size)
    if n == 0:
        return []
    zeros = values == 0.0
    share = float(zeros.mean())
    if share < _ZERO_INFLATION_SHARE:
        return []
    longest = run = 0
    for z in zeros:
        run = run + 1 if z else 0
        longest = max(longest, run)
    msg = (
        f"'{target}' is exactly zero in {int(zeros.sum())} of {n} fitted {grain} "
        f"periods ({share:.0%}; longest run {longest}). The observation model is "
        "Gaussian, so this fit puts posterior mass on negative values and "
        "mis-states the variance in the periods that are real — read its "
        "intervals and components as approximate. A zero-inflated/count "
        "likelihood is roadmap S20; until it lands this warning is the "
        "disclosure (see docs/model.md)."
    )
    logger.warning(msg)
    return [msg]


# PSIS k-hat bands for a variational approximation (Yao et al., 2018, "Yes,
# but Did It Work?: Evaluating Variational Inference"; smoothing per Vehtari
# et al., 2024). k-hat is the shape parameter of a generalized Pareto fitted
# to the tail of the importance ratios p(theta, y) / q(theta): it says how
# heavy that tail is, i.e. how far the proposal q sits from the target p.
#
#   k <= 0.5   the ratios have finite variance; importance sampling — and so
#              the approximation it is diagnosing — is reliable.
#   0.5 < k    finite mean, infinite variance. Usable with care; convergence
#     <= 0.7   of any reweighting is slow and the approximation is visibly off.
#   k >  0.7   neither the variance nor (past k=1) the mean is finite. The
#              approximation cannot be trusted and cannot be *rescued* by
#              reweighting either; the only fix is a better posterior.
#
# The 0.7 bar is the conventional PSIS threshold. Three bands rather than one
# because 0.5-0.7 and >0.7 call for different things — "read the interval as
# approximate" versus "this interval is not evidence" — and collapsing them
# would either over- or under-warn.
#
# Nothing in the engine *acts* on these bands: k-hat is a disclosure on a
# sampler the caller chose on purpose, not a trigger. See `fit_metric`'s
# `inference_method` docstring for why.
_KHAT_GOOD = 0.5
_KHAT_UNUSABLE = 0.7

# Draws used for the k-hat estimate. ArviZ fits the Pareto to the largest
# `min(n/5, 3*sqrt(n))` ratios, so 1000 draws fit the tail on 95 points — the
# same order as the ArviZ/loo default — and cost ~0.2s after the (one-off,
# ~1.5s) graph compile, against a 1.4-6s ADVI fit on the demo trees. Nothing
# about the diagnostic scales with the tree, only with the model's latent count.
#
# 95 tail points is also what sets k-hat's own standard error (`_khat_se`), and
# that error is large: ~0.15 at k-hat = 0.5, which is most of the width of the
# 0.5-0.7 band. Buying it down is expensive — the tail grows as sqrt(n), so the
# error falls as n^(-1/4) and halving it costs 16x the draws — so S22 reports
# the error rather than spending against it. If that trade is ever revisited,
# it is this constant that moves.
_KHAT_DRAWS = 1000

# The weight of the prior ArviZ's `_gpdfit` puts on k = 0.5 (its `prior_k`,
# from Vehtari et al., 2024): the estimator returns `(M*k + 5) / (M + 10)` for a
# tail of M points. It shrinks the estimate toward 0.5 and, with it, the
# estimator's sampling variance — by the factor `(M / (M + 10))^2`. Read from
# the same place the estimate comes from, because a standard error computed for
# a different estimator is not this number's standard error.
_GPD_PRIOR_K = 10.0


def _khat_tail_len(log_ratios: np.ndarray) -> int:
    """How many of `log_ratios` ArviZ's `psislw` actually fits the Pareto to.

    Mirrors `arviz.stats.stats.psislw` / `_psislw`: the cut is the largest
    `ceil(min(n/5, 3*sqrt(n)))` ratios, floored at `log(tiny)` so a run of
    underflowed ratios cannot be counted as tail. It is recomputed here rather
    than assumed from `n` because that floor can bite, and a standard error
    quoted against the wrong sample size is a made-up number of exactly the
    kind rule 3 exists to keep out of a payload.
    """
    n = int(log_ratios.size)
    if n < 5:
        return 0
    cut = int(np.ceil(min(n / 5.0, 3.0 * n**0.5)))
    if cut >= n:
        return 0
    ordered = np.sort(log_ratios)
    cutoff = max(float(ordered[-cut - 1]), float(np.log(np.finfo(float).tiny)))
    return int((log_ratios > cutoff).sum())


def _khat_se(khat: float, log_ratios: np.ndarray) -> Optional[float]:
    """The Monte-Carlo standard error of one k-hat estimate (roadmap S22).

    k-hat is fitted to a finite tail of a finite sample, so it is an estimate
    with sampling error, and the whole argument for reporting it is that a
    number without a stated uncertainty is not evidence. The generalized
    Pareto shape parameter's MLE is asymptotically normal with variance
    `(1 + k)^2 / M` over a tail of M points (Smith, 1985; Hosking & Wallis,
    1987), and ArviZ's empirical-Bayes estimator shrinks toward 0.5 with prior
    weight `_GPD_PRIOR_K`, which scales that variance by `(M / (M + 10))^2`.
    So:

        se(k-hat) = (M / (M + 10)) * (1 + k-hat) / sqrt(M)

    Returns None — never a NaN, never a zero — when the asymptotics do not
    apply: the normal limit needs `k > -0.5`, and a tail of four points or
    fewer is what makes ArviZ give up on the shape parameter itself. A missing
    standard error is reported as missing; see `_khat_warning`, which says so
    rather than quoting the estimate as if it were exact.
    """
    if not np.isfinite(khat) or khat <= -0.5:
        return None
    m = _khat_tail_len(log_ratios)
    if m <= 4:
        return None
    se = (m / (m + _GPD_PRIOR_K)) * (1.0 + khat) / math.sqrt(m)
    return float(se) if np.isfinite(se) and se > 0 else None


def _khat_borderline(khat: Optional[float], se: Optional[float]) -> bool:
    """Is this k-hat closer to a band edge than to its own error?

    The bands are a verdict about whether the intervals are evidence, and an
    estimate that sits within one standard error of `_KHAT_GOOD` or
    `_KHAT_UNUSABLE` cannot support the one it landed in — the next draw of the
    same 1000 would plausibly land on the other side. The band still reported
    is the measured one (`khat_status` keeps meaning "which band the estimate
    is in", so no consumer's reading of it changes); this says separately that
    the estimate does not resolve it.
    """
    if khat is None or se is None:
        return False
    return min(abs(khat - _KHAT_GOOD), abs(khat - _KHAT_UNUSABLE)) < se


def _psis_khat(
    approx: Any, n_draws: int = _KHAT_DRAWS, random_seed: Optional[int] = None
) -> Tuple[Optional[float], Optional[float], str, Optional[str]]:
    """PSIS k-hat for a variational approximation (Yao et al., 2018).

    Treats the fitted `approx` as an importance-sampling proposal for the true
    posterior: draw theta from q, form the log ratios
    `log p(theta, y) - log q(theta)`, smooth their tail with a generalized
    Pareto, and return its shape parameter k-hat. Both terms are taken in the
    model's **unconstrained** space, where q actually lives — `sized_symbolic_logp`
    carries the change-of-variables Jacobian and `symbolic_logq` is the density
    of the same draws, so the ratio is a ratio of comparable densities. Working
    through PyMC's own symbolic machinery (rather than reconstructing the
    variational density by hand) is what makes this work unchanged for
    mean-field and full-rank: each family supplies its own `symbolic_logq`.

    Returns `(khat, khat_se, status, reason)`. `status` is one of `"ok"`,
    `"suspect"`, `"unusable"` (the three bands above) or `"unavailable"` — the
    last means the number could not be computed, and then `khat` is None and
    `reason` says why. `khat_se` is k-hat's own Monte-Carlo standard error
    (roadmap S22; see `_khat_se`), and is None when that error is not
    computable even though k-hat is — the two are withheld independently,
    because "checked, with an error we cannot state" and "not checked" are
    different facts. Never returns a non-finite k-hat or standard error: a NaN
    rendered as a number is worse than no number, and it would reach an
    encoder (rule 3).
    """
    import arviz as az
    import pytensor.tensor as pt
    from pymc.pytensorf import compile, find_rng_nodes, reseed_rngs

    size = pt.iscalar("khat_draws")
    logp, logq = approx.set_size_and_deterministic(
        [approx.sized_symbolic_logp, approx.symbolic_logq], size, 0
    )
    ratio_fn = compile([size], [logp, logq])
    if random_seed is not None:
        reseed_rngs(find_rng_nodes([logp, logq]), random_seed)
    log_p, log_q = ratio_fn(int(n_draws))

    log_ratios = np.asarray(log_p, dtype=float) - np.asarray(log_q, dtype=float)
    finite = np.isfinite(log_ratios)
    n_finite = int(finite.sum())
    # A ratio is non-finite when a draw lands where the model's logp is -inf
    # (outside a constrained support the transform did not cover) or where q
    # underflows. A handful is noise; a large share means the Pareto tail
    # would be fitted to whatever survived, which is a different quantity.
    if n_finite < 100 or n_finite < 0.9 * log_ratios.size:
        return (
            None,
            None,
            "unavailable",
            f"only {n_finite} of {log_ratios.size} log importance ratios were finite",
        )
    centered = log_ratios[finite]
    centered = centered - centered.max()
    _, khat = az.psislw(centered.reshape(1, -1))
    k = float(np.asarray(khat).ravel()[0])
    if not np.isfinite(k):
        return None, None, "unavailable", "the generalized Pareto fit returned a non-finite shape"
    se = _khat_se(k, centered)
    if k <= _KHAT_GOOD:
        return k, se, "ok", None
    if k <= _KHAT_UNUSABLE:
        return k, se, "suspect", None
    return k, se, "unusable", None


def cached_fit_is_usable(fit: Any, inference_method: str) -> bool:
    """May a cached fit stand in for one the caller asked to run with `inference_method`?

    Only upward. A cached NUTS fit answers a request for `"advi"` — it is the
    exact posterior the approximation is approximating, and reusing it costs
    nothing. A cached approximation does **not** answer a request for NUTS:
    `traces` is shared by every viewer of a process, so one colleague's
    deliberate `?inference_method=advi` triage run would otherwise silently
    decide the sampler behind everybody else's default analysis of the same
    window, with the payload reporting a method nobody chose.

    Imported by both `run_rca` and `run_scenario` so the policy cannot drift
    between them. It is the read-side twin of `_fit_rank` in
    `breakdown/api/main.py`, which orders the same two methods for the same
    reason on the write side.
    """
    if inference_method == "nuts":
        return getattr(fit, "inference_method", None) == "nuts"
    return True


def _khat_figure(khat: float, se: Optional[float]) -> str:
    """`k-hat = 0.68 +/- 0.15`, or the same number with its error named absent.

    Every sentence that quotes a k-hat quotes it through here, so the estimate
    and its uncertainty cannot come apart in one message and not another
    (roadmap S22). A missing standard error is stated, never elided: "0.68"
    with nothing after it reads as an exact number, which is the reading S22
    exists to stop.
    """
    if se is None:
        return f"PSIS k-hat = {khat:.2f} (its own Monte-Carlo error could not be estimated)"
    return f"PSIS k-hat = {khat:.2f} +/- {se:.2f} (one Monte-Carlo standard error)"


def _khat_borderline_clause(khat: float, se: float) -> str:
    """The sentence a k-hat that cannot resolve its own band travels with.

    Names every band the estimate reaches, not just the nearer one. The
    `suspect` band is 0.2 wide and the standard error is often ~0.15, so a
    k-hat inside it can be within one error of *both* edges — and reporting
    only the closer one would understate exactly the uncertainty this sentence
    exists to state.
    """
    near_good = abs(khat - _KHAT_GOOD) < se
    near_unusable = abs(khat - _KHAT_UNUSABLE) < se
    if near_good and near_unusable:
        reach = (
            f"That is within one such error of both band edges ({_KHAT_GOOD} and "
            f"{_KHAT_UNUSABLE}), so this estimate does not separate 'ok', 'suspect' and "
            "'unusable' from each other at all"
        )
    elif near_good:
        reach = (
            f"That is closer to the {_KHAT_GOOD} band edge than to its own error, so this "
            "estimate does not separate 'ok' from 'suspect'"
        )
    else:
        reach = (
            f"That is closer to the {_KHAT_UNUSABLE} band edge than to its own error, so this "
            "estimate does not separate 'suspect' from 'unusable'"
        )
    return (
        f"{reach}: k-hat is fitted to the tail of {_KHAT_DRAWS} sampled importance ratios, and "
        f"another {_KHAT_DRAWS} would plausibly land on the other side of the edge. Read the "
        "band as unresolved rather than as the side it happened to fall on, and use a NUTS fit "
        "for anything that turns on which side it is."
    )


def _khat_warning(
    target: str,
    method: str,
    khat: Optional[float],
    se: Optional[float],
    status: str,
    borderline: bool,
    reason: Optional[str],
) -> str:
    """The one self-contained sentence a k-hat verdict travels with.

    One sentence rather than a list, still: every consumer (the UI's warning
    block, the MCP payload, the exported report) renders `khat_warnings`
    verbatim, so a k-hat's uncertainty belongs *inside* the sentence that
    quotes the k-hat rather than in a second entry that some surface might
    show without the first.
    """
    # A None k-hat cannot reach the other branches by construction — every
    # other status carries a number — but the check is a branch rather than an
    # assert, because `python -O` drops asserts and this function's output is a
    # sentence a reader acts on.
    if status == "unavailable" or khat is None:
        return (
            f"The approximation quality of the {method} fit for '{target}' could not be "
            f"checked: {reason}. That is an unchecked fit, not a clean one — its intervals "
            "carry no evidence either way (see docs/model.md)."
        )
    figure = _khat_figure(khat, se)
    tail = f" {_khat_borderline_clause(khat, se)}" if borderline and se is not None else ""
    if status == "ok":
        # Reached only when the estimate is borderline: an `ok` k-hat clear of
        # the 0.5 edge says nothing, and silence is the right render there.
        return (
            f"The {method} approximation for '{target}' has {figure}, inside the good band "
            f"(<= {_KHAT_GOOD}).{tail}"
        )
    if status == "suspect":
        return (
            f"The {method} approximation for '{target}' has {figure}, above {_KHAT_GOOD}: "
            "the importance ratios against the true posterior have no "
            "finite variance, so this fit sits measurably away from the posterior it "
            "approximates. Read its intervals as approximate, and confirm anything "
            f"load-bearing with a NUTS fit (see docs/model.md).{tail}"
        )
    return (
        f"The {method} approximation for '{target}' has {figure}, above {_KHAT_UNUSABLE}: "
        "the importance ratios against the true posterior have "
        "neither finite variance nor a usable mean, so the approximation is not close to "
        "the posterior and cannot be corrected by reweighting. Its credible intervals are "
        "not evidence about the width of the real ones. This fit ran a variational "
        "approximation because one was asked for; drop `inference_method=advi` to get "
        "the NUTS default, or re-fit this node alone with "
        f"POST /analyze/{target}?inference_method=nuts.{tail}"
    )


def _advi_diagnostics(
    approx: Any,
    method: str = "advi",
    target: str = "",
    random_seed: Optional[int] = None,
    khat_draws: int = _KHAT_DRAWS,
) -> Dict[str, Any]:
    """Diagnostics for a variational (ADVI-family) fit: the optimizer, and the
    approximation.

    Two independent checks, because they answer different questions and this
    used to report only the first:

    - **`elbo_drop` / the ELBO check** — did the *optimizer* stop? Compares the
      mean loss (−ELBO) of the last 10% of iterations against the preceding
      10%: if the loss is still moving by more than half its recent noise
      level, the optimization had not converged. `elbo_drop` (mean(prev) −
      mean(last)) is positive while the loss is still falling. This says
      nothing about how close the converged approximation is to the posterior
      — a well-converged bad approximation passes it, which is exactly what
      S1's benchmark measured on real nodes.
    - **`khat` / the PSIS check** — how far is the *approximation* from the
      posterior? See `_psis_khat`. This is roadmap S2, and it is the check
      that decides whether the intervals mean anything.

    `fit_quality` is "suspect" when either check fails: the field is a gate
    ("do not trust this fit as-is"), and every consumer already branches on
    its two values, so the k-hat verdict rides that channel rather than
    opening a parallel one. `khat` and `khat_status` are the evidence beside
    it, the way `max_rhat` / `divergences` / `min_ess_bulk` are for NUTS. A
    k-hat that could not be computed (`khat_status: "unavailable"`) does not
    flip `fit_quality`: an unchecked fit is not a failed one, and saying so is
    the honest reading — but it does carry the warning that says it is
    unchecked, because silence there reads as "no problems found".

    Roadmap **S22** adds k-hat's own error beside it: `khat_se` is the
    Monte-Carlo standard error of the estimate, and `khat_borderline` says the
    estimate sits within one of those standard errors of a band edge.
    `khat_status` keeps meaning exactly what it meant — which band the point
    estimate is in — so nothing downstream has to reinterpret it; the new flag
    is what says the band is not resolved. It **does** flip `fit_quality`,
    including from an `ok` k-hat: the gate's question is whether this fit can
    be trusted as-is, and an approximation the check cannot distinguish from
    the `suspect` band has not answered it. That is the conservative direction
    and the only one available — the alternative is publishing a clean bill
    the measurement does not support.
    """
    hist = np.asarray(approx.hist, dtype=float)
    w = len(hist) // 10
    if w < 1:
        elbo_drop, elbo_suspect = None, True
    else:
        last, prev = hist[-w:], hist[-2 * w : -w]
        elbo_drop = float(prev.mean() - last.mean())
        elbo_suspect = bool(abs(last.mean() - prev.mean()) > 0.5 * last.std())

    try:
        khat, khat_se, khat_status, khat_reason = _psis_khat(
            approx, n_draws=khat_draws, random_seed=random_seed
        )
    except Exception as e:  # pragma: no cover - defensive: a PyMC API change
        khat, khat_se, khat_status, khat_reason = (
            None,
            None,
            "unavailable",
            f"{type(e).__name__}: {e}",
        )
        logger.warning("PSIS k-hat could not be computed for '%s': %s", target, e)

    borderline = _khat_borderline(khat, khat_se)
    diagnostics: Dict[str, Any] = {
        "fit_quality": "suspect"
        if (elbo_suspect or borderline or khat_status in ("suspect", "unusable"))
        else "ok",
        "method": method,
        "elbo_drop": elbo_drop,
        "khat": khat,
        "khat_se": khat_se,
        "khat_status": khat_status,
        "khat_borderline": borderline,
    }
    if khat_status != "ok" or borderline:
        msg = _khat_warning(target, method, khat, khat_se, khat_status, borderline, khat_reason)
        logger.warning(msg)
        diagnostics["khat_warnings"] = [msg]
    return diagnostics


def _validate_columns(data: pd.DataFrame, cols: List[str]) -> None:
    """Refuse a fit frame the model cannot train on, naming what is wrong.

    The NaN branch is the fit's stated policy for an **undefined period**
    (roadmap 1.11c): a rate whose denominator was legitimately zero has no
    value there, and there is no imputation of it that is not a fabricated
    observation — a zero, a forward-fill and an interpolation each assert
    something the data does not say, and the model would then report posterior
    uncertainty that does not include the invention. Dropping the row is worse
    still: `t = arange(len(y))`, the lag shifts and the seasonal design are all
    positional, so deleting a period silently re-dates every period after it.

    So the node is **unfittable over a window containing one**, and says so.
    `run_rca` turns this `ValueError` into that node's `fit_failed` status with
    the message attached, so one undefined period costs one node's attribution
    and nothing else; `POST /analyze/{name}` turns it into a 422 naming the
    periods and the remedy (a later `--start-date`, or a `fit_end` before them).
    """
    missing = [c for c in cols if c not in data.columns]
    if missing:
        raise ValueError(f"Columns missing from data: {missing}")
    with_nan = [c for c in cols if data[c].isna().any()]
    if not with_nan:
        return
    detail = []
    for c in with_nan:
        bad = pd.DatetimeIndex(data.loc[data[c].isna(), "date"]) if "date" in data else None
        when = (
            ""
            if bad is None
            else " ("
            + ", ".join(str(d.date()) for d in bad[:5])
            + (", …" if len(bad) > 5 else "")
            + ")"
        )
        n = int(data[c].isna().sum())
        detail.append(f"'{c}' on {n} period(s){when}")
    raise ValueError(
        "Cannot fit over undefined periods: "
        + "; ".join(detail)
        + ". A period with no value cannot be trained on and cannot be imputed "
        "without inventing an observation the source never made — and dropping "
        "it would re-date every later period, since model time, lags and the "
        "seasonal design are all positional. Narrow the fit window to defined "
        "periods (a later --start-date, or an earlier fit_end), or give the "
        "metric a source that covers them."
    )


def _normalize(series: pd.Series) -> Tuple[np.ndarray, float, float]:
    mean = series.mean()
    std = series.std()
    if std == 0:
        raise ValueError(f"Column '{series.name}' has zero variance — cannot normalize.")
    return (series.values - mean) / std, float(mean), float(std)


# Roadmap S4. Triggers for a *disclosure*, not statistics — the same status as
# `_ZERO_INFLATION_SHARE` and the k-hat bands, and read the same way.
#
# Two bands rather than one trip-wire, for the reason k-hat has three: "the
# split is softer than the sum" and "the split is not a measured quantity at
# all" ask different things of a reader, and a single threshold either cries
# wolf at the first or stays silent through the second. The demo tree is the
# case that decided it — its deliberately collinear pair measures |r| = 0.86
# over the window an RCA fits it on, which a 0.9 bar would have passed in
# silence while the split of that node's gap moved 1.6 points across numeric
# stacks from the same seed.
#
#   |r| >= 0.7   moderate. The applied convention for where collinearity
#                begins to distort coefficient estimation (Dormann et al.,
#                2013). Their combined effect is still the sound number; the
#                division of it between them is the soft one.
#   |r| >= 0.9   high. >= 81% shared variance — the split is not measured,
#                and reading either parent on its own is the mistake.
#
#   VIF >= 5     moderate, VIF >= 10 high: the conventional pair of bars
#                (Kutner et al., 2004; Sheather, 2009), i.e. a parent 80% /
#                90% explained by its siblings *jointly*. Deliberately looser
#                than the pairwise bands convert to (0.7 and 0.9 are VIF 1.96
#                and 5.26) because VIF is a joint quantity over k-1
#                regressors: more parents raise it for honest reasons, and
#                the pairwise bar applied to it would flag every wide node.
#                Computed only for three or more parents — with exactly two,
#                VIF is identically 1 / (1 - r^2), so it would restate the
#                pairwise finding as a second, more obscure number.
#
# The two channels catch different shapes. A pairwise r cannot see `x3 ~ x1 +
# x2` (every pair modest, the triple degenerate); a VIF cannot say *which*
# parents to look at. Report both, and name names either way.
_COLLINEAR_R_MODERATE = 0.7
_COLLINEAR_R_HIGH = 0.9
_COLLINEAR_VIF_MODERATE = 5.0
_COLLINEAR_VIF_HIGH = 10.0


def _collinearity_diagnostic(
    X: Optional[np.ndarray],
    parents: List[str],
    target: str,
    grain: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Roadmap S4: name the parents whose *split* of credit is unstable.

    Correlated regressors leave the likelihood almost flat along a ridge: the
    data pins their **sum** and says little about the **split** — and the split
    is exactly what RCA reports per parent. Since S2 made NUTS the default the
    posterior is honest about that (a wide interval on each parent, a narrow
    one on their total), but nothing said *which two parents* the width came
    from, or that reading them separately is the mistake. This does.

    A note on principle, because it looks like an exception and is not. This
    project reports relationships as posteriors, never as Pearson r — but this
    is not a relationship. It is a property of the **design matrix** the fit
    was handed: a description of the regressors' co-movement over the fitted
    periods, with no inferential claim, no null hypothesis and no p-value
    attached. Nothing here is an estimate of anything in the world; it is a
    statement about what the data can and cannot separate.

    Runs on `X` as the model sees it — the z-scored, lag-shifted, trimmed
    columns in `list(dag.predecessors(target))` order, so a lagged parent is
    correlated at its lag rather than contemporaneously.

    Returns `(block, warnings)`:

    - `block["status"]` is `"ok"` (checked, nothing crossed a band),
      `"moderate"`, `"high"` (the worst band any pair or parent reached), or
      `"unavailable"` (the check could not run, which is not the same as
      clean — rule 3). `None` for a node with nothing to check: a formula
      node, or fewer than two regressors, where there is no split to be
      unstable.
    - `block["pairs"]` / `block["vif"]` carry only what was flagged, worst
      first, each with its own band; `block["max_abs_correlation"]` is always
      present on a computed check so `"ok"` is evidence rather than an
      assertion.
    - `warnings` is the self-contained prose that rides the payload, one
      string per finding, the way `sign_warnings` does.
    """
    if X is None or len(parents) < 2 or X.ndim != 2 or X.shape[1] != len(parents):
        return None, []

    n, k = X.shape
    # Rule 3, at the top: a design matrix with a non-finite or constant column
    # gives a 0/0 correlation. Withhold the whole check under a named status
    # with the reason rather than emitting NaN or, worse, a zero that reads as
    # "these parents are unrelated".
    if not np.isfinite(X).all():
        bad = [p for i, p in enumerate(parents) if not np.isfinite(X[:, i]).all()]
        reason = f"non-finite values in the fitted regressor(s) {bad}"
    elif not np.isfinite(stds := X.std(axis=0)).all() or float(stds.min()) <= 0.0:
        bad = [p for i, p in enumerate(parents) if not np.isfinite(stds[i]) or stds[i] <= 0]
        reason = f"zero or non-finite variance in the fitted regressor(s) {bad}"
    else:
        reason = ""
    if reason:
        msg = (
            f"The parents of '{target}' could not be checked for collinearity: {reason}. "
            "That is an unchecked design, not a clean one — if two of these parents "
            "restate each other, the per-parent split of this node's gap is arbitrary "
            "and nothing here will say so (see docs/model.md)."
        )
        logger.warning(msg)
        return (
            {
                "status": "unavailable",
                "max_abs_correlation": None,
                "pairs": [],
                "vif": [],
                "reason": reason,
            },
            [msg],
        )

    corr = np.corrcoef(X, rowvar=False)
    if not np.isfinite(corr).all():
        reason = "the correlation matrix of the fitted regressors is not finite"
        msg = (
            f"The parents of '{target}' could not be checked for collinearity: {reason}. "
            "That is an unchecked design, not a clean one (see docs/model.md)."
        )
        logger.warning(msg)
        return (
            {
                "status": "unavailable",
                "max_abs_correlation": None,
                "pairs": [],
                "vif": [],
                "reason": reason,
            },
            [msg],
        )

    warnings: List[str] = []
    pairs: List[Dict[str, Any]] = []
    max_abs = 0.0
    for i, j in combinations(range(k), 2):
        r = float(corr[i, j])
        max_abs = max(max_abs, abs(r))
        if abs(r) < _COLLINEAR_R_MODERATE:
            continue
        band = "high" if abs(r) >= _COLLINEAR_R_HIGH else "moderate"
        pairs.append({"parents": [parents[i], parents[j]], "correlation": r, "status": band})
        head = (
            f"Parents '{parents[i]}' and '{parents[j]}' on '{target}' "
            f"{'are near-collinear' if band == 'high' else 'move largely together'} over the "
            f"{n} fitted {grain} periods (correlation {r:+.3f}). "
        )
        if band == "high":
            msg = head + (
                "The data determines their combined effect much better than the division "
                "of it between them, so each one's coefficient, contribution and share of "
                "the gap is the least stable number in this node's result — the pair's "
                "total is the quantity to read, and the split can move by points on a "
                "different numerical stack without the total moving at all. The fix is in "
                "the tree, not the fit: merge the two, drop one, or redefine one so it is "
                "not a restatement of the other (see docs/model.md)."
            )
        else:
            msg = head + (
                "The data determines their combined effect better than the division of it "
                "between them, so the pair's total is the sound number here and the split "
                "between them is the soft one. Read the two as one cause and do not rank "
                "them against each other on a small difference in share — that ordering "
                "is the part this fit does not pin down (see docs/model.md)."
            )
        warnings.append(msg)
        logger.warning(msg)
    pairs.sort(key=lambda p: -abs(p["correlation"]))

    # VIF needs one regression per parent on the other k-1, so it needs more
    # rows than columns to mean anything; below that every R^2 is 1 by
    # construction. `MIN_FIT_PERIODS` makes this unreachable on any real fit,
    # but the check is cheap and the alternative is publishing VIF = inf as a
    # finding about the parents rather than about the row count.
    vif: List[Dict[str, Any]] = []
    if k >= 3 and n >= k + 2:
        for i, p in enumerate(parents):
            others = np.column_stack([np.ones(n), np.delete(X, i, axis=1)])
            coef, *_ = np.linalg.lstsq(others, X[:, i], rcond=None)
            resid = X[:, i] - others @ coef
            ss_res = float(np.sum(resid**2))
            ss_tot = float(np.sum((X[:, i] - X[:, i].mean()) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
            if not math.isfinite(r2) or r2 >= 1.0 - 1e-12:
                # Exactly (or numerically exactly) a linear combination of the
                # others: VIF is unbounded. Rule 3 — withhold the number under
                # its own status rather than emit `inf`, and keep the finding,
                # which is the strongest one this function can make.
                vif.append({"parent": p, "vif": None, "status": "unbounded"})
                msg = (
                    f"Parent '{p}' on '{target}' is an exact linear combination of the "
                    f"other parents over the {n} fitted {grain} periods, so its own "
                    "coefficient is not identified at all and the split of this node's "
                    "gap among those parents is arbitrary — only their combined effect "
                    "is a real quantity. Remove the redundant parent or redefine it "
                    "(see docs/model.md)."
                )
                warnings.append(msg)
                logger.warning(msg)
                continue
            v = 1.0 / (1.0 - r2)
            if v < _COLLINEAR_VIF_MODERATE:
                continue
            band = "high" if v >= _COLLINEAR_VIF_HIGH else "moderate"
            vif.append({"parent": p, "vif": float(v), "status": band})
            msg = (
                f"Parent '{p}' on '{target}' is {r2:.0%} explained by its sibling parents "
                f"over the {n} fitted {grain} periods (VIF {v:.1f}). "
                + (
                    "Little of its movement is its own, so the share of the gap this node "
                    "hands it is largely a choice among the correlated group rather than a "
                    "measured split. Read the group's combined contribution and treat the "
                    "per-parent numbers inside it as interchangeable"
                    if band == "high"
                    else "Much of its movement is shared with them, so its own share of the "
                    "gap is softer than the group's combined contribution — read the group "
                    "together before acting on any one member's number"
                )
                + " (see docs/model.md)."
            )
            warnings.append(msg)
            logger.warning(msg)
        vif.sort(key=lambda e: (e["vif"] is not None, -(e["vif"] or 0.0)))

    worst = "ok"
    for band in [e["status"] for e in pairs] + [e["status"] for e in vif]:
        # `unbounded` is the strongest VIF finding there is, so it ranks as
        # `high` at the node level rather than as a fourth node status nothing
        # downstream would know how to render.
        if band in ("high", "unbounded"):
            worst = "high"
            break
        worst = "moderate"

    block = {
        "status": worst,
        "max_abs_correlation": float(max_abs),
        "pairs": pairs,
        "vif": vif,
        "reason": None,
    }
    return block, warnings


# Roadmap S3. Triggers for a *disclosure*, not statistics — the same status as
# the k-hat bands and `_COLLINEAR_R_*`, and read the same way.
#
# A posterior predictive p-value is the probability, under the fitted model,
# of replicating data at least as extreme as what was observed on some test
# statistic (Gelman et al., 2020; Gabry et al., 2019). It is deliberately
# *not* a frequentist tail probability against a null: there is no null here
# and nothing is being rejected. It asks one question — could this model have
# produced this series? — and the bands below are where the answer stops being
# "yes".
#
# Reported two-sided, as `2 * min(p, 1 - p)`, because both tails are findings:
# a residual spread the model never replicates and one it always overshoots
# are both misspecification, and a one-sided bar would report the first and
# miss the second.
#
#   p_2s < 0.10  moderate. The observed series sits outside the central 90% of
#                what the fitted model generates on this statistic. Worth
#                saying; not on its own a reason to disbelieve the node.
#   p_2s < 0.02  severe. Outside the central 98%. On a check the model is
#                being graded against its own replicates, this is the model
#                failing to generate its own data.
#
# The bands are wider than the conventional 0.05/0.01 for a reason this
# engine's shape forces: four statistics are reported per node, so a node with
# four independent honest checks crosses a 0.05 bar 19% of the time by chance
# alone. These are disclosure triggers on a payload a reader scans, not
# hypothesis tests, and the cost of spraying is that the ones that matter stop
# being read.
_PPC_P_MODERATE = 0.10
_PPC_P_SEVERE = 0.02

#: Which PPC verdicts move `fit_quality`, written as a predicate rather than
#: an inline comparison so the policy is greppable and directly testable.
#:
#: Only `severe`. The roadmap row asked S3 to surface through the
#: `fit_quality` channel, and for the strong band that is right: a model that
#: cannot generate its own data is not a fit whose intervals mean what they
#: say, which is exactly what the two-valued gate is for. `moderate` stays out
#: of it — four statistics per node cross that band often enough on honest
#: fits that wiring it to the gate would make `suspect` the common verdict and
#: drain it of meaning, the same spray argument that set the bands wide in the
#: first place. `unavailable` stays out for the reason S22 kept k-hat's
#: borderline band out: an unchecked model is not a failed one.
_PPC_GATING_STATUSES = ("severe",)


def _ppc_moves_fit_quality(status: Optional[str]) -> bool:
    """Whether a PPC verdict flips `fit_quality` to "suspect"."""
    return status in _PPC_GATING_STATUSES


#: Posterior draws used for the replicated series. The p-value's own Monte
#: Carlo error is ~sqrt(p(1-p)/n): at n = 500 and p = 0.02 that is 0.006, so a
#: `severe` verdict is separated from its band edge by three of its own
#: standard errors. More draws buy accuracy this diagnostic cannot use — the
#: bands are conventions, not measurements (cf. S22, which had to publish
#: k-hat's error precisely because 0.5 and 0.7 *are* the finding there).
_PPC_DRAWS = 500


def _ppc_test_statistics(y: np.ndarray, mu: np.ndarray) -> Dict[str, float]:
    """The four test statistics S3 scores, for one series against one `mu`.

    Chosen by measurement, and the omission is the load-bearing part. The
    obvious candidate — the standard deviation of the series — is **vacuous
    here** and is deliberately absent: `_prepare_series` z-scores `y` and
    `sigma_obs` is free, so the replicated spread matches the observed spread
    by construction. Measured across a well-specified world, a t(3) world and
    a Poisson-count world it returned p = 0.479 / 0.499 / 0.521 — a number
    that cannot fail is worse than no number, because it reads as a check that
    passed. The same argument retires `resid_sd` (0.511 / 0.526 / 0.482).

    What survives measures something the local-level trend cannot absorb:

    - `min` / `max`: the extremes of the series. A non-negative quantity fitted
      Gaussian is caught here and nowhere else (the Poisson world returns
      p = 0.000 on `min`), which makes this check a real version of what S20's
      zero-share heuristic only approximates.
    - `resid_max`: the largest single residual. Heavy tails live here
      (p = 0.020 on the t(3) world, against 0.094 well-specified).
    - `resid_acf1`: lag-1 autocorrelation of the residuals — structure the
      mean function left behind, which is the check a local-level model most
      needs and the one that will speak when S8's momentum case is real.
    """
    resid = y - mu
    denom = float(np.sum(resid * resid))
    return {
        "min": float(np.min(y)),
        "max": float(np.max(y)),
        "resid_max": float(np.max(np.abs(resid))),
        # Undefined for an all-zero residual vector; 0.0 is the right reading
        # (no leftover structure) and cannot be reached by a real fit anyway.
        "resid_acf1": (float(np.sum(resid[1:] * resid[:-1]) / denom) if denom > 0 else 0.0),
    }


def _ppc_diagnostic(
    y: np.ndarray,
    y_rep: Optional[np.ndarray],
    mu: Optional[np.ndarray],
    target: str,
    grain: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Roadmap S3: could the fitted model have generated the series it was fitted on?

    Convergence diagnostics (r-hat, ESS, divergences) say the sampler explored
    the posterior; k-hat (S2) says a variational approximation is close to it.
    Neither asks whether the *model* is right, and a badly misspecified node
    passes both in silence as long as it converges — §3.2 #5 of the white
    paper. This asks, by simulating replicated series from the posterior and
    comparing four test statistics against the observed ones.

    `y_rep` is `(n_draws, n_periods)` replicated observations; `mu` the
    matching `(n_draws, n_periods)` mean function, so the residual statistics
    are computed per draw against that draw's own mean — the observed residual
    is itself a posterior quantity here, not a single fitted-value residual.

    Returns `(block, warnings)` in the shape S4 established:

    - `block["status"]` is `"ok"` (checked, every statistic inside its band),
      `"moderate"`, `"severe"` (the worst band any statistic reached), or
      `"unavailable"` (the check could not run — unchecked, not clean, rule 3).
      `None` when there was nothing to check.
    - `block["statistics"]` carries **every** statistic with its p-value, not
      only the flagged ones, so an `ok` is evidence rather than an assertion
      and S10 has the series-level material it needs.
    - `warnings` is self-contained prose, one string per flagged statistic.
    """
    if y_rep is None or mu is None:
        return None, []

    y = np.asarray(y, dtype=float)
    n = len(y)
    # Rule 3: a non-finite replicate or mean draw makes every statistic below
    # NaN, and a NaN p-value rounds to `null` on the agent payload — a check
    # that reads as absent rather than as failed. Withhold the whole block
    # under a named status with its reason instead.
    reason = ""
    if y_rep.ndim != 2 or mu.ndim != 2 or y_rep.shape != mu.shape or y_rep.shape[1] != n:
        reason = (
            f"replicated series of shape {getattr(y_rep, 'shape', None)} and mean function of "
            f"shape {getattr(mu, 'shape', None)} do not match the {n} fitted periods"
        )
    elif not np.isfinite(y).all():
        reason = "the fitted series contains non-finite values"
    elif not np.isfinite(y_rep).all() or not np.isfinite(mu).all():
        reason = "the posterior predictive draws contain non-finite values"
    if reason:
        msg = (
            f"The fit for '{target}' could not be checked against its own posterior "
            f"predictive distribution: {reason}. That is an unchecked model, not a "
            "validated one — if this node's likelihood is wrong for its data, nothing "
            "here will say so (see docs/model.md)."
        )
        logger.warning(msg)
        return (
            {"status": "unavailable", "statistics": [], "n_draws": 0, "reason": reason},
            [msg],
        )

    obs = _ppc_test_statistics(y, mu.mean(axis=0))
    per_draw_obs = {k: [] for k in obs}
    per_draw_rep = {k: [] for k in obs}
    for rep_i, mu_i in zip(y_rep, mu):
        o = _ppc_test_statistics(y, mu_i)
        r = _ppc_test_statistics(rep_i, mu_i)
        for k in obs:
            per_draw_obs[k].append(o[k])
            per_draw_rep[k].append(r[k])

    n_draws = int(y_rep.shape[0])
    statistics: List[Dict[str, Any]] = []
    warnings: List[str] = []
    worst = "ok"
    for name in ("min", "max", "resid_max", "resid_acf1"):
        o = np.asarray(per_draw_obs[name], dtype=float)
        r = np.asarray(per_draw_rep[name], dtype=float)
        # Mid-p. `>=` alone makes a statistic with ties (a count series whose
        # minimum is 0 in most replicates) return p = 1.000 on a *correctly*
        # specified node — measured, and it put the only false alarm of the
        # probe on the good world. Splitting the tied mass removes it.
        p_one = float(np.mean(r > o) + 0.5 * np.mean(r == o))
        p_two = float(2.0 * min(p_one, 1.0 - p_one))
        band = "ok"
        if p_two < _PPC_P_SEVERE:
            band = "severe"
        elif p_two < _PPC_P_MODERATE:
            band = "moderate"
        statistics.append(
            {
                "statistic": name,
                "p_value": p_two,
                "observed": float(o.mean()),
                "replicated_mean": float(r.mean()),
                "status": band,
            }
        )
        if band == "ok":
            continue
        if worst != "severe":
            worst = "severe" if band == "severe" else "moderate"
        msg = _ppc_warning(target, grain, n, name, p_two, float(o.mean()), float(r.mean()), band)
        warnings.append(msg)
        logger.warning(msg)

    statistics.sort(key=lambda s: s["p_value"])
    return (
        {"status": worst, "statistics": statistics, "n_draws": n_draws, "reason": None},
        warnings,
    )


#: Roadmap S10. The quantiles of the replicated series kept per fitted period,
#: and the whole of what S10 persists.
#:
#: **Why a band and not the replicates.** S3's `_posterior_predictive_draws`
#: builds a `(n_draws, n_periods)` replicate array inside the model context and
#: discards it, for the reason its docstring gives: on White Cube's
#: `trials_started` at the full window that array is 500 x 790 float64 =
#: **3.16 MB**, and the matching `mu` another 3.16 MB, on every fit, in a cache
#: whose budget is measured in gigabytes (rule 2 — bound the thing that grows
#: with the loaded window). Storing them so the UI could draw them would put
#: 6.3 MB on every trace to render a chart 400 pixels wide. Measured on that
#: same fit, five quantiles of the same array are **31.6 kB** as numpy,
#: **160 kB** as the Python lists that ride on the `FitResult` (which is what
#: `_trace_nbytes` meters), and **88 kB** as the JSON the browser gets — 0.6%
#: of the 24.6 MB trace they ride with. They scale on the same axis the trace
#: does, so the existing byte budget bounds them in proportion, and
#: `_trace_nbytes` counts them explicitly rather than relying on that ratio
#: continuing to hold.
#:
#: Recomputing on demand behind a route was the other candidate and is worse
#: than either: `sample_posterior_predictive` needs the model *graph*, not the
#: trace, so a route would have to refit the node — a minute of NUTS to redraw
#: a chart, and a second posterior that is not the one the reported p-values
#: were computed from.
#:
#: Five and not three: the outer pair is the 95% interval the verdict is about,
#: the inner pair keeps a wide band from reading as "anything would fit", and
#: the median is what the eye tracks against the observed line. Estimated from
#: 500 thinned draws, so the outer pair rests on ~12 order statistics each —
#: enough for a band, which is why S3's p-values do not need more draws either.
_PPC_BAND_QUANTILES = {
    "lo95": 0.025,
    "lo50": 0.25,
    "median": 0.5,
    "hi50": 0.75,
    "hi95": 0.975,
}


def _ppc_band(
    y: np.ndarray,
    y_rep: Optional[np.ndarray],
    dates: pd.DatetimeIndex,
    y_mean: float,
    y_std: float,
    is_residual: bool,
    target: str,
) -> Optional[Dict[str, Any]]:
    """Roadmap S10: the per-period material for an observed-vs-replicated plot.

    S3 asks whether the model could have generated its own data and answers in
    four p-values. This is the same question with the answer left in its
    original shape — the series, and the spread of series the fitted model
    generates around it — because a reader who is told `min` failed at p = 0.016
    still cannot see *where*, and a reader who is shown the band can.

    Returns a block whose arrays are all length `n_periods` and all JSON-safe,
    or `None` when there was nothing to build it from. Rule 3 twice over:

    - Every value is checked finite before it is emitted. One NaN quantile is
      an unhandled 500 through Starlette's `allow_nan=False` encoder, and the
      whole band is withheld under a named `reason` rather than emitted with a
      hole in it — a chart with a gap in the band reads as "the model is
      certain here", which is the opposite of not knowing.
    - `fitted_quantity` says **what** the series is. A formula node fits the
      residual `observed - formula(parents)`, not the metric, so plotting its
      band against the metric's own history would be a chart of two different
      quantities sharing an axis. The caller renders the label; the payload
      makes it impossible to render without one.

    Values are returned in the raw units of the fitted quantity (the fit runs
    z-scored; `y_mean`/`y_std` invert that), and the observed series is carried
    here rather than joined client-side against `/metrics/{name}`'s
    `time_series`. That series covers the loaded window, while this one covers
    the *fitted* window — shorter by the lag trim and by `fit_end`, which for
    an RCA fit deliberately stops before the analysis window. Two arrays of
    different length, silently zipped, is a chart that is wrong everywhere
    after the first missing period.
    """
    if y_rep is None:
        return None

    y = np.asarray(y, dtype=float)
    n = len(y)
    reason = ""
    if y_rep.ndim != 2 or y_rep.shape[1] != n:
        reason = (
            f"replicated series of shape {getattr(y_rep, 'shape', None)} do not match the "
            f"{n} fitted periods"
        )
    elif len(dates) != n:
        reason = f"{len(dates)} fitted dates do not match the {n} fitted periods"
    if reason:
        msg = (
            f"The posterior predictive band for '{target}' was not stored: {reason}. The "
            "metric card will say the band is unavailable rather than draw one."
        )
        logger.warning(msg)
        return {"reason": reason}

    # Raw units, so the chart shares an axis with the metric's own history.
    observed = y * y_std + y_mean
    qs = np.quantile(y_rep, list(_PPC_BAND_QUANTILES.values()), axis=0) * y_std + y_mean

    if not np.isfinite(observed).all() or not np.isfinite(qs).all():
        reason = "the observed series or its replicated quantiles contain non-finite values"
        msg = (
            f"The posterior predictive band for '{target}' was not stored: {reason}. The "
            "metric card will say the band is unavailable rather than draw one."
        )
        logger.warning(msg)
        return {"reason": reason}

    replicated = {name: qs[i].tolist() for i, name in enumerate(_PPC_BAND_QUANTILES)}
    lo, hi = np.asarray(replicated["lo95"]), np.asarray(replicated["hi95"])
    # The periods the model's own 95% predictive interval misses. Published as
    # indices and deliberately without a threshold, band or verdict of its own:
    # S3's p-values are the verdict on this fit, and a second thresholded
    # number computed from the same draws would be the same diagnostic
    # answering twice (the rule S22 was written for). It is here so the chart
    # can mark the periods and caption how many there are, which is a
    # description of the picture rather than a claim about the model.
    outside = [int(i) for i in np.flatnonzero((observed < lo) | (observed > hi))]

    return {
        "dates": [d.strftime("%Y-%m-%d") for d in pd.DatetimeIndex(dates)],
        "observed": observed.tolist(),
        "quantiles": dict(_PPC_BAND_QUANTILES),
        "replicated": replicated,
        "outside": outside,
        "n_draws": int(y_rep.shape[0]),
        "n_periods": n,
        # "metric" | "formula_residual" — see the docstring. Not a boolean:
        # a third fitted quantity later gets a name here rather than a second
        # flag beside this one.
        "fitted_quantity": "formula_residual" if is_residual else "metric",
        "reason": None,
    }


def _ppc_warning(
    target: str,
    grain: str,
    n: int,
    name: str,
    p: float,
    observed: float,
    replicated: float,
    band: str,
) -> str:
    """The prose for one flagged statistic. Each names what the model failed to
    reproduce and what that specifically threatens, because "the PPC failed" is
    not actionable and the reader of a `share_of_gap` is who this has to reach."""
    direction = "above" if observed > replicated else "below"
    head = (
        f"The fitted model for '{target}' does not reproduce its own data on "
        f"'{name}' over the {n} fitted {grain} periods: observed {observed:+.3f} "
        f"against {replicated:+.3f} replicated ({direction}), posterior predictive "
        f"p = {p:.3f}. "
    )
    tail = {
        "min": (
            "The model generates low values the series never reaches — the usual cause is "
            "a Gaussian likelihood on a quantity that cannot go below a floor (a count, a "
            "rate, anything non-negative), which puts posterior mass on impossible values "
            "and mis-states the intervals everywhere, not only at the floor"
        ),
        "max": (
            "The model does not generate peaks the series actually reaches, so the spikes "
            "are being absorbed as noise rather than modeled — anything this node's "
            "intervals say about a period containing one is understated"
        ),
        "resid_max": (
            "One or more periods sit far outside what this model calls noise. A "
            "heavy-tailed or contaminated series fitted Gaussian inflates `sigma_obs` to "
            "cover the outliers, which widens every interval on the node and drags the "
            "trend toward the outlying periods"
        ),
        "resid_acf1": (
            "The residuals are still autocorrelated, so structure the mean function should "
            "carry — momentum, an unmodeled cycle, a lag this tree does not declare — is "
            "being left in the noise term. That understates the uncertainty on every "
            "coefficient, because the fit is treating correlated periods as independent "
            "evidence"
        ),
    }[name]
    close = (
        ". The node's coefficients and its share of any gap are computed from this model, "
        "so read them as conditional on a likelihood the data argues against"
        if band == "severe"
        else ". The fit is usable and this is a caveat on it, not a verdict against it"
    )
    return head + tail + close + " (see docs/model.md)."


def _ppc_unavailable(target: str, reason: str) -> Tuple[Dict[str, Any], List[str]]:
    """Rule 3 for the sampling half of S3: the draws could not be produced, so
    say the model is *unchecked* rather than let the absence of a warning read
    as a model that passed."""
    msg = (
        f"The fit for '{target}' could not be checked against its own posterior "
        f"predictive distribution: {reason}. That is an unchecked model, not a validated "
        "one — if this node's likelihood is wrong for its data, nothing here will say so "
        "(see docs/model.md)."
    )
    logger.warning(msg)
    return (
        {"status": "unavailable", "statistics": [], "n_draws": 0, "reason": reason},
        [msg],
    )


def _seasonal_draws(posterior: Any, seasonality: List[Any], t: np.ndarray) -> np.ndarray:
    """`(n_draws, n_periods)` Fourier seasonal component from a trace.

    Mirrors `_seasonal_component`'s `identifiable_harmonics` filter exactly, for
    the same reason `seasonal_window_delta` does: a harmonic the model skipped
    has no posterior variable to read.
    """
    n_draws = posterior.sizes["chain"] * posterior.sizes["draw"]
    out = np.zeros((n_draws, len(t)))
    for s in seasonality:
        for k in identifiable_harmonics(s.period):
            a = posterior[f"sin_{s.name}_h{k}"].values.reshape(-1)
            b = posterior[f"cos_{s.name}_h{k}"].values.reshape(-1)
            out += np.outer(a, np.sin(2 * np.pi * k * t / s.period))
            out += np.outer(b, np.cos(2 * np.pi * k * t / s.period))
    return out


def _posterior_predictive_draws(
    pm: Any,
    trace: Any,
    X: Optional[np.ndarray],
    defn: MetricDefinition,
    t: np.ndarray,
    random_seed: Optional[int] = None,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[str]]:
    """Replicated series and the matching mean function, for S3.

    Must be called **inside** the model context: `sample_posterior_predictive`
    needs the graph, not just the trace.

    Two things are load-bearing here. The posterior is thinned to
    `_PPC_DRAWS` *before* the replicates are drawn — a 4-chain, 1000-draw
    trace over an 830-period window is 3.3M floats of replicates and as many
    again of mean function, allocated on every fit, and the p-value cannot use
    that precision (rule 2's reasoning applied to a transient: bound the thing
    that grows with the window).

    And `mu` is reconstructed in numpy from `alpha`/`trend`/`beta` plus the
    seasonal coefficients rather than added to the graph as a
    `pm.Deterministic`. A stored `mu` would be a second trend-sized array on
    every trace *forever*, in a cache the trace cap is measured in gigabytes
    against, to hold values that are an exact function of variables already
    there.

    Returns `(y_rep, mu, None)`, or `(None, None, reason)` if either could not
    be produced — never a partial or invented result.
    """
    try:
        posterior = trace.posterior
        n_draws = int(posterior.sizes["chain"] * posterior.sizes["draw"])
        if n_draws < 1:
            return None, None, "the posterior carries no draws"
        step = max(1, n_draws // _PPC_DRAWS)
        thinned = trace.sel(draw=slice(None, None, step)) if step > 1 else trace

        pp = pm.sample_posterior_predictive(
            thinned,
            var_names=["obs"],
            random_seed=random_seed,
            progressbar=False,
        )
        y_rep = pp.posterior_predictive["obs"].values.reshape(-1, len(t))

        post = thinned.posterior
        mu = np.zeros_like(y_rep, dtype=float)
        mu += post["alpha"].values.reshape(-1, 1)
        mu += post["trend"].values.reshape(-1, len(t))
        mu += _seasonal_draws(post, defn.seasonality, t)
        if X is not None:
            # `beta` is the Deterministic stack in `list(dag.predecessors)`
            # order — the same axis order every other component reads.
            mu += post["beta"].values.reshape(-1, X.shape[1]) @ X.T
        if mu.shape != y_rep.shape:
            return (
                None,
                None,
                (
                    f"reconstructed mean function of shape {mu.shape} does not match the "
                    f"replicated series of shape {y_rep.shape}"
                ),
            )
        return y_rep, mu, None
    except Exception as exc:  # pragma: no cover - defensive; rule 3
        return None, None, f"{type(exc).__name__}: {exc}"


def _prepare_series(
    defn: MetricDefinition,
    parents: List[str],
    data: pd.DataFrame,
    target: str,
) -> Tuple[
    np.ndarray,
    Optional[np.ndarray],
    Optional[np.ndarray],
    float,
    float,
    Optional[np.ndarray],
    pd.DatetimeIndex,
]:
    """
    Build the normalized observation vector y and regressor matrix X.

    Formula nodes: y is the z-scored residual (observed - formula(parents));
    there are no regressors — the structural relationship IS the formula, and
    the BSTS models only what the formula doesn't explain.

    Probabilistic nodes: each parent becomes a z-scored regressor column,
    shifted back by its lag (`lags` in the YAML); the first max-lag rows are
    trimmed so every series aligns with no NaNs.

    Returns (y, X, scale, y_mean, y_std, x_stds, dates):
    - `scale[i] = x_std_i / y_std` converts raw-unit coefficients into
      normalized space; X and scale are None when there is nothing to regress on.
    - `y_mean`, `y_std` are the normalization constants of the fitted y series
      (the residual, for formula nodes).
    - `x_stds` are the raw per-parent stds of the (lag-shifted) regressors, or
      None when there is no X.
    - `dates` is the date index actually used in the fit, after lag-trimming.
    """
    all_dates = pd.DatetimeIndex(pd.to_datetime(data["date"]))

    lags = defn.lags
    max_lag = max(lags.values(), default=0)
    # No length guard here: `fit_metric` enforces `MIN_FIT_PERIODS` once, on
    # the same count this function trims to (see `_enforce_fit_length`). A
    # second check here is what let the two paths drift apart in the first place.

    if defn.formula and parents:
        # With lags, the identity is cohort-aligned: A[t] = f(parents with
        # each parent shifted back by its lag). Shift, trim the leading
        # max-lag rows, and fit the residual of the lagged identity.
        parent_arrays = {
            p: data[p].shift(lags.get(p, 0)).values.astype(float)[max_lag:] for p in parents
        }
        target_vals = data[target].values.astype(float)[max_lag:]
        residual = target_vals - eval_formula(defn.formula, parent_arrays)
        y, y_mean, y_std = _normalize(pd.Series(residual, name=f"{target}_residual"))
        return y, None, None, y_mean, y_std, None, all_dates[max_lag:]

    y_series = data[target].iloc[max_lag:] if max_lag > 0 else data[target]
    y, y_mean, y_std = _normalize(y_series)
    dates = all_dates[max_lag:] if max_lag > 0 else all_dates

    if not parents:
        return y, None, None, y_mean, y_std, None, dates

    X_cols, x_stds = [], []
    for p in parents:
        shifted = data[p].shift(lags.get(p, 0))
        if max_lag > 0:
            shifted = shifted.iloc[max_lag:]
        col, _, p_std = _normalize(shifted)
        X_cols.append(col)
        x_stds.append(p_std)

    x_stds_arr = np.array(x_stds)
    return y, np.column_stack(X_cols), x_stds_arr / y_std, y_mean, y_std, x_stds_arr, dates


#: Fourier harmonics attempted per seasonality entry, before the Nyquist filter.
_HARMONICS = (1, 2)


def identifiable_harmonics(period: int) -> tuple:
    """The harmonics of `period` a series sampled at its grain can actually
    resolve: harmonic `k` carries `k / period` cycles per step, so Nyquist
    requires `k / period < 1/2`.

    Below that bound the design matrix is rank-deficient and the extra
    parameters are pure prior — sampled but never informed by data:

    - `period 2`: both sin terms are identically zero on integer `t` and the
      second cosine is constant, i.e. collinear with the intercept. Nothing is
      identifiable, which is why the parser rejects it.
    - `period 3`: the second harmonic is an exact linear image of the first
      (`sin_2 = −sin_1`, `cos_2 = cos_1`).
    - `period 4`: the second sine is identically zero.

    Dropping them is not a loss of expressiveness — those columns carry no
    information about the data — but it removes parameters NUTS would otherwise
    have to explore, and stops the model reporting seasonal structure it
    invented from the prior.
    """
    return tuple(k for k in _HARMONICS if 2 * k < period)


def _seasonal_component(seasonality: List[Any], t: np.ndarray):
    """Fourier seasonality: up to 2 sin/cos harmonic pairs per YAML entry,
    Nyquist-filtered by `identifiable_harmonics`.
    Must be called inside a pm.Model context."""
    import pymc as pm

    terms = []
    for s in seasonality:
        for k in identifiable_harmonics(s.period):
            sin_term = np.sin(2 * np.pi * k * t / s.period)
            cos_term = np.cos(2 * np.pi * k * t / s.period)
            a = pm.Normal(f"sin_{s.name}_h{k}", mu=0, sigma=1.0)
            b = pm.Normal(f"cos_{s.name}_h{k}", mu=0, sigma=1.0)
            terms.append(a * sin_term + b * cos_term)
    return sum(terms) if terms else 0.0


def seasonal_window_delta(
    trace: Any,
    seasonality: List[Any],
    t_ref: np.ndarray,
    t_an: np.ndarray,
) -> np.ndarray:
    """Per-posterior-sample (analysis − reference) window mean of the Fourier
    seasonal component, in normalized units. Returns shape (n_samples,).
    Zero-length seasonality returns zeros.

    The seasonal component is parametric in integer time t (days since the
    first fitted date), so unlike the trend it evaluates anywhere — including
    an analysis window outside the fitted period. Because it is linear in the
    coefficients, the per-sample window-mean difference is the coefficients
    dotted with the window-mean difference of the sin/cos design.
    """
    posterior = trace.posterior
    n_samples = posterior.sizes["chain"] * posterior.sizes["draw"]
    delta = np.zeros(n_samples)
    for s in seasonality:
        # Must mirror `_seasonal_component`'s filter exactly: a harmonic it
        # skipped has no posterior variable to read.
        for k in identifiable_harmonics(s.period):
            a = posterior[f"sin_{s.name}_h{k}"].values.reshape(-1)
            b = posterior[f"cos_{s.name}_h{k}"].values.reshape(-1)
            sin_delta = (
                np.sin(2 * np.pi * k * t_an / s.period).mean()
                - np.sin(2 * np.pi * k * t_ref / s.period).mean()
            )
            cos_delta = (
                np.cos(2 * np.pi * k * t_an / s.period).mean()
                - np.cos(2 * np.pi * k * t_ref / s.period).mean()
            )
            delta = delta + a * sin_delta + b * cos_delta
    return delta


def _regression_component(defn: MetricDefinition, parents: List[str], X, scale):
    """One beta per parent: the parent's own prior if present, else the shared
    `coefficient` prior, else weakly informative Normal(0, 1) in normalized
    space. Priors from YAML are stated in business units and rescaled here.
    Must be called inside a pm.Model context."""
    import pymc as pm

    if X is None:
        return 0.0
    betas = []
    for i, p in enumerate(parents):
        prior = defn.priors.get(p) or defn.priors.get("coefficient")
        if prior:
            if prior.distribution not in _PRIOR_DISTRIBUTIONS:
                raise ValueError(
                    f"Unsupported prior distribution: '{prior.distribution}'. "
                    f"Must be one of {sorted(_PRIOR_DISTRIBUTIONS)}"
                )
            scaled = scale_prior_params(prior.distribution, prior.params, scale[i])
            betas.append(getattr(pm, prior.distribution)(f"beta_{p}", **scaled))
        else:
            betas.append(pm.Normal(f"beta_{p}", mu=0.0, sigma=1.0))
    beta = pm.Deterministic("beta", pm.math.stack(betas))
    pm.Deterministic("beta_raw", beta / scale)
    return pm.math.dot(X, beta)


#: The sampler budget, written once (roadmap C27).
#:
#: These four numbers used to be spelled three times — `fit_metric`'s own
#: defaults, `run_rca`/`run_scenario`'s pass-through defaults, and
#: `POST /analyze/{name}`'s `Query(default=...)` — and the spellings disagreed.
#: `/analyze` warmed up for 500 steps while every analysis route warmed up for
#: 1000, so one node fitted over one window came back from a different
#: adaptation depending on which URL asked. That was harmless while `/analyze`
#: was the only NUTS path and the analyses ran ADVI (which never reads `tune`);
#: roadmap S2's Option C put every route on NUTS and made it two defensible-
#: but-unequal posteriors for one question, with nothing in either payload
#: saying which one the reader got. The engine is supposed to be a pure
#: function of (DAG, data, target).
#:
#: Every call site — orchestrator default, route default, and the numbers the
#: UI prints at the reader — reads these, and
#: `tests/test_project_invariants.py` fails on a literal written anywhere else.
#:
#: The values, and why each:
#:
#: * ``NUTS_TUNE = 1000`` is PyMC's own default and the more conservative of
#:   the two budgets that were in the tree. Measured across 5 seeds on four
#:   probabilistic nodes of `knowledge/b2b_mrr_tree.yml` (draws=500, 4 chains),
#:   500 and 1000 warm-up steps are not distinguishable on adaptation quality:
#:   two nodes improved at 1000, two got worse, and the seed-to-seed spread on
#:   one node at fixed settings (3 to 196 divergences) is far larger than the
#:   gap between the budgets. So the measurement does not pick a winner, and
#:   the tie is broken on cost and honesty: standardizing *up* moves no RCA or
#:   what-if figure (they already warm up 1000), costs ~15-30% wall clock on
#:   `/analyze` alone, and makes the UI's own "after 1,000 discarded tuning
#:   steps" true — it has been telling readers that while the route ran 500.
#: * ``NUTS_DRAWS = 500`` per chain — 2,000 posterior draws over 4 chains, and
#:   what every route already ran. `fit_metric`'s old `draws=1000` default was
#:   reachable only by direct callers, all of which pass the parameter, so it
#:   was a fourth number nobody sampled at. Measured bulk ESS at 500x4 on the
#:   nodes above is 213-921, well clear of the 100 that flags a fit "suspect",
#:   and the doubling is not free: one 830-day NUTS fit is 27.0 MB of posterior,
#:   so 1000 draws would halve `MAX_CACHED_TRACE_BYTES`'s capacity from ~19
#:   cached fits to ~9 (see `breakdown/api/trees.py`).
#: * ``NUTS_CHAINS = 4`` — already agreed everywhere; hoisted so it cannot
#:   start disagreeing.
#: * ``ADVI_ITERATIONS = 20_000`` is the ADVI analogue and had *not* split: no
#:   caller overrides it and no route exposes it. It is hoisted anyway, because
#:   "one spelling today" is exactly what `tune` had before `/analyze` grew its
#:   own, and the UI prints this number too.
NUTS_DRAWS = 500
NUTS_TUNE = 1000
NUTS_CHAINS = 4
ADVI_ITERATIONS = 20_000

#: The seed every path that fits on the caller's behalf passes, for the same
#: reason as the four budgets above and enforced by the same invariant test
#: (roadmap **S22**, half (a)).
#:
#: `run_rca` and `run_scenario` each wrote `random_seed=0` at their own fit call
#: site; `POST /analyze/{name}` passed nothing at all. So the manual-fit route —
#: the one the UI's Analyze button and the "confirm this with NUTS" workflow
#: both use — returned a *different* fit from the same request each time, and
#: with `?inference_method=advi` a different PSIS k-hat with it: measured
#: 2026-08-27 on the White Cube tree at full window, two consecutive calls gave
#: `customer_churn_rate` 1.23 then 1.91 and `trials_started` 0.94 then 1.19.
#: The published verdict held (`unusable` both times), but a diagnostic that
#: answers differently about the same fit is not a diagnostic, and a k-hat near
#: a band edge would have straddled it.
#:
#: Zero, because that is what the two orchestrators already used, so no cached
#: or published RCA figure moves. A fixed seed does not make the fit *correct*
#: — it makes it reproducible, which is the property `fit_metric`'s docstring
#: has always claimed and one route quietly did not have.
FIT_RANDOM_SEED = 0


def fit_metric(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    target: str,
    draws: int = NUTS_DRAWS,
    tune: int = NUTS_TUNE,
    inference_method: str = "nuts",
    fit_end: Optional[str] = None,
    chains: int = NUTS_CHAINS,
    random_seed: Optional[int] = None,
    vi_iterations: int = ADVI_ITERATIONS,
) -> FitResult:
    """
    Fit the Bayesian structural time series for one metric.

    In normalized (z-scored) space the model is:

        y[t] = alpha + trend[t] + seasonal[t] + (X @ beta)[t] + eps[t]

        trend[t]   =  cumsum(sigma_trend * z[t])         local level (non-centered)
        z[t]       ~  Normal(0, 1)
        sigma_trend~  HalfNormal(0.05)  (or the YAML `trend.sigma`); the level
                      drifts slowly so parents and seasonality carry the movement
        seasonal   =  Fourier sin/cos pairs (2 harmonics per `seasonality` entry)
        beta[i]    ~  prior from YAML, stated in business units and rescaled
        alpha      ~  Normal(0, 1)   (data is z-scored; the mean is exactly 0)
        eps[t]     ~  Normal(0, sigma_obs)

    The trend is a non-centered random walk: sampling unit normals and scaling
    by `sigma_trend` avoids the Neal's-funnel geometry of a centered walk, which
    NUTS handles poorly and mean-field ADVI (the opt-in fast path) fails on outright.
    `pm.Deterministic("trend", ...)` preserves the `trend` posterior variable so
    downstream decomposition reads it unchanged.

    Node types:
    - Formula nodes fit the residual (observed - formula(parents)); no beta.
    - Probabilistic nodes regress on their parents, each shifted back by its
      lag from `lags` (in grain steps) and z-scored.
    - Source nodes (no parents) fit trend + seasonality only.

    The fit runs at the node's own declared grain: the target is used natively
    and finer flow/stock parents are resampled up to it, so `t`, lags, and
    seasonality periods are all in grain steps.

    `fit_end` (exclusive): when set, only whole periods that end on/before
    `fit_end` are used, so the model learns the normal-regime relationship
    without the anomalous window it is being asked to explain (for day grain
    this is exactly `date < fit_end`). Normalization and prior scaling then
    follow the filtered rows too. RCA passes `fit_end = analysis_start`.

    Every path refuses a series shorter than `MIN_FIT_PERIODS` whole periods at
    the node's grain, counted after the parent join, the `fit_end` cut and the
    lag trim — the same floor `breakdown doctor` reports readiness against (see
    `_enforce_fit_length`). `run_rca` turns that `ValueError` into a per-node
    `fit_failed` status, so a thin node costs its own attribution and nothing
    else.

    Inference: "nuts" (exact MCMC — the default here and on every orchestrator
    that fits on the caller's behalf), "advi" (mean-field variational
    approximation, an explicit opt-in for triage speed on a wide or day-grain
    tree) or "fullrank_advi" (variational with a full covariance matrix — can represent
    the beta/trend posterior ridge mean-field collapses, and reproduces the
    NUTS interval on small synthetic fits; benchmarked under roadmap S1 and
    NOT adopted as a default, because on real-sized windows the O(d^2)
    covariance over per-period trend latents is slower than NUTS and lands
    far from the posterior with a clean ELBO — see
    knowledge/s1_fullrank_advi_benchmark.md before reaching for it). `tune`
    and `chains` apply to NUTS only; `vi_iterations` (optimizer steps) to the
    two ADVI variants only — full-rank needs roughly 2x mean-field's steps to
    converge even on small fits. The four budgets default to `NUTS_DRAWS` /
    `NUTS_TUNE` / `NUTS_CHAINS` / `ADVI_ITERATIONS` above, which is where
    every orchestrator and route default reads them from too (roadmap C27):
    a fit is a pure function of (DAG, data, target), so the route the caller
    arrived through must not change the answer. `random_seed` makes the fit
    reproducible on
    a given platform/dependency set (RCA and simulate pass a fixed seed so
    their on-demand fits — and hence API responses — are deterministic even
    across empty trace caches; tests seed to kill stochastic flakes).

    Every variational fit is scored with **PSIS k-hat** (Yao et al., 2018;
    roadmap S2) and reports it as `diagnostics["khat"]` / `["khat_status"]`.
    It is a *disclosure*, never a trigger: `inference_method` is a promise
    about which sampler runs, and a function that silently runs a different one
    at 2-20x the cost breaks every caller who asked for the fast path on
    purpose. Measurement (roadmap S2) says mean-field fails the check on
    essentially every real node in this engine — one correlated local-level
    latent per period is not representable by a factorized Gaussian — so the
    honest response was to make NUTS the default everywhere rather than to
    re-fit behind the caller's back. What k-hat buys now is that choosing
    `"advi"` anyway is an informed choice rather than a trap.

    Every fit with two or more regressors is also checked for **parent
    collinearity** (roadmap S4) and reports
    `diagnostics["collinearity_status"]` / `["collinearity"]` /
    `["collinearity_warnings"]`. See `_collinearity_diagnostic`: it says which
    parents the model cannot tell apart, which is the one thing a wide
    posterior on a ridge does not say for itself.

    Returns a `FitResult`. Its trace's posterior includes `beta_raw`
    (= beta / scale): the coefficient on each parent in business units,
    i.e. d(target) per unit change of that parent.
    """
    import pymc as pm
    import pytensor.tensor as pt

    if inference_method not in ("nuts", "advi", "fullrank_advi"):
        raise ValueError(
            f"inference_method must be 'nuts', 'advi' or 'fullrank_advi', got '{inference_method}'"
        )

    parents = list(dag.predecessors(target))
    # The fit runs at the node's own grain: the target native, finer
    # flow/stock parents resampled up, aligned on whole periods.
    grain = fit_grain(dag, target)
    data = ensure_grained(data).fit_frame(target, parents, grain)
    n_joined = len(data)

    if fit_end is not None:
        dates = pd.to_datetime(data["date"])
        cutoff = pd.to_datetime(fit_end)
        # Whole-period-strict: keep only periods that END on/before fit_end,
        # so a coarse period straddling the anomaly can't train the model.
        # For day grain `next_start(d) <= fit_end` is exactly `d < fit_end` —
        # identical to the pre-grain behavior.
        ends = pd.DatetimeIndex([next_start(d, grain) for d in dates])
        data = data.loc[ends <= cutoff].reset_index(drop=True)

    _validate_columns(data, [target] + parents)
    defn: MetricDefinition = dag.nodes[target]["definition"]

    # The floor, enforced on every path — default, `fit_end`, and lagged alike
    # — against the periods the fit will really train on.
    _enforce_fit_length(
        target,
        grain,
        n_joined=n_joined,
        n_windowed=len(data),
        max_lag=max(defn.lags.values(), default=0),
        fit_end=fit_end,
    )

    y, X, scale, y_mean, y_std, x_stds, dates = _prepare_series(defn, parents, data, target)
    t = np.arange(len(y))

    # S20's disclosure half: the observation model is Gaussian, and a series
    # that is exactly zero for a large share of its fit window (a seasonal
    # business's off-season, a spiky count) is the one misspecification that
    # used to pass with `fit_quality: "ok"` and nothing anywhere saying
    # otherwise — the ELBO check only says the optimizer stopped. Detected on
    # the *raw* series (normalization moves the zeros), warned at fit time,
    # and carried on the payload as `likelihood_warnings` so the reader of the
    # number sees it, not only the reader of the log.
    likelihood_warnings = _zero_inflation_warnings(
        data[target].to_numpy(dtype=float), target, grain
    )

    # Seasonality periods are in grain steps; a period the data can't cover
    # twice is unidentifiable (composes with the 1.1 hardening work).
    seasonality_warnings = []
    for s in defn.seasonality:
        if len(y) < 2 * s.period:
            msg = (
                f"Seasonality '{s.name}' (period {s.period} {grain}s) on '{target}' "
                f"is unidentifiable: only {len(y)} fitted {grain} periods "
                f"(need >= {2 * s.period})."
            )
            seasonality_warnings.append(msg)
            logger.warning(msg)
        # Distinct from the shortage above: this one is a property of the
        # period itself, so more data will never fix it. Say so, because the
        # fitted component is narrower than the YAML implies.
        dropped = [k for k in _HARMONICS if k not in identifiable_harmonics(s.period)]
        if dropped:
            msg = (
                f"Seasonality '{s.name}' (period {s.period} {grain}s) on '{target}': "
                f"harmonic(s) {dropped} dropped as unidentifiable at this period "
                f"(Nyquist requires 2k < period); fitted with harmonic(s) "
                f"{list(identifiable_harmonics(s.period))}."
            )
            seasonality_warnings.append(msg)
            logger.warning(msg)

    # Roadmap S4, computed before the sampler runs rather than after: it is a
    # property of the design matrix, not of the trace, and a reader watching
    # the log deserves to know the split is unstable *before* waiting out the
    # fit that will report it.
    collinearity, collinearity_warnings = _collinearity_diagnostic(X, parents, target, grain)

    with pm.Model():
        trend_sigma_prior = defn.trend.sigma if defn.trend else 0.05
        sigma_trend = pm.HalfNormal("sigma_trend", trend_sigma_prior)
        trend_z = pm.Normal("trend_z", 0.0, 1.0, shape=len(y))
        trend = pm.Deterministic("trend", pt.cumsum(sigma_trend * trend_z))
        seasonal = _seasonal_component(defn.seasonality, t)
        regression = _regression_component(defn, parents, X, scale)

        alpha = pm.Normal("alpha", mu=0, sigma=1.0)
        sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
        pm.Normal("obs", mu=alpha + trend + seasonal + regression, sigma=sigma_obs, observed=y)

        logger.info(
            "Sampling metric '%s' method=%s draws=%d tune=%d fit_end=%s",
            target,
            inference_method,
            draws,
            tune,
            fit_end,
        )
        if inference_method in ("advi", "fullrank_advi"):
            approx = pm.fit(
                n=vi_iterations,
                method=inference_method,
                progressbar=False,
                random_seed=random_seed,
            )
            trace = approx.sample(draws=draws, random_seed=random_seed)
            diagnostics = _advi_diagnostics(
                approx, method=inference_method, target=target, random_seed=random_seed
            )
        else:
            trace = pm.sample(
                draws=draws,
                tune=tune,
                target_accept=0.9,
                chains=chains,
                random_seed=random_seed,
            )
            diagnostics = _nuts_diagnostics(trace, draws, chains)

        # Roadmap S3, inside the model context because that is the only place
        # `sample_posterior_predictive` can run — and computed here for every
        # method, because misspecification is a property of the model, not of
        # how its posterior was approximated.
        y_rep, mu_draws, ppc_reason = _posterior_predictive_draws(
            pm, trace, X, defn, t, random_seed=random_seed
        )

    ppc, ppc_warnings = (
        _ppc_diagnostic(y, y_rep, mu_draws, target, grain)
        if ppc_reason is None
        else _ppc_unavailable(target, ppc_reason)
    )

    # Roadmap S10, from the same draws and before they go out of scope. The
    # verdict above is a summary of this; keeping both means the reader who is
    # told `min` failed at p = 0.016 can also see which periods it failed on.
    # When the draws themselves could not be produced there is no band either,
    # and the absent band is `None` with `ppc_status: "unavailable"` beside it
    # saying why — an unchecked model, never an empty chart (rule 3).
    ppc_band = (
        _ppc_band(y, y_rep, dates, y_mean, y_std, bool(defn.formula and parents), target)
        if ppc_reason is None
        else {"reason": ppc_reason}
    )

    if diagnostics["fit_quality"] == "suspect":
        logger.warning("Fit for '%s' is suspect: %s", target, diagnostics)
    if seasonality_warnings:
        diagnostics["seasonality_warnings"] = seasonality_warnings
    if likelihood_warnings:
        diagnostics["likelihood_warnings"] = likelihood_warnings

    # Roadmap S4. `collinearity_status` rides along whenever there was
    # something to check — including `"ok"`, which is the point: on a node with
    # two or more parents the absence of a warning has to be distinguishable
    # from the absence of a check. `fit_quality` deliberately does *not* move
    # on this. A collinear design does not make the fit wrong; the fit is
    # correct and correspondingly unsure, and telling the reader not to trust
    # it would be the opposite of the truth. What is unsafe is reading one
    # parent's number on its own, which is what the warnings say.
    if collinearity is not None:
        diagnostics["collinearity_status"] = collinearity["status"]
        diagnostics["collinearity"] = collinearity
        if collinearity_warnings:
            diagnostics["collinearity_warnings"] = collinearity_warnings

    # Roadmap S3. Like S4's block, this rides along whenever the check ran at
    # all — `"ok"` included, because on a fitted node the absence of a warning
    # has to be distinguishable from the absence of a check.
    #
    # Whether it moves `fit_quality` is the one design question this item had
    # to answer with a measurement rather than a preference, and the answer is
    # `severe` does and `moderate` does not. The roadmap row asked for the
    # `fit_quality` channel, and for the strong band that is right: a model
    # that cannot generate its own data is not a fit whose intervals mean what
    # they say, which is exactly the two-valued gate consumers branch on. But
    # four statistics per node cross a `moderate` band often enough on honest
    # fits that wiring that band to the gate would make `suspect` the common
    # case and drain it of meaning — the same spray argument that set the
    # bands wide. So `moderate` speaks in its own channel and leaves the gate
    # alone, and `unavailable` never moves it: an unchecked model is not a
    # failed one (the rule S22 applied to k-hat's borderline band).
    if ppc is not None:
        diagnostics["ppc_status"] = ppc["status"]
        diagnostics["ppc"] = ppc
        if ppc_warnings:
            diagnostics["ppc_warnings"] = ppc_warnings
        if _ppc_moves_fit_quality(ppc["status"]):
            diagnostics["fit_quality"] = "suspect"
            logger.warning(
                "Fit for '%s' is suspect: the posterior predictive check failed on %s",
                target,
                [s["statistic"] for s in ppc["statistics"] if s["status"] == "severe"],
            )

    # Declared-direction check: expected_signs is not a prior, so the fit is
    # free to contradict it — but when it does, say so loudly. The classic
    # cause is a scale-confounded level-on-level edge (both series grow with
    # the business), where the learned sign answers a different question than
    # the author meant.
    if defn.expected_signs and X is not None:
        arr = trace.posterior["beta_raw"].values.reshape(-1, len(parents))
        sign_warnings = []
        for i, p in enumerate(parents):
            expected = defn.expected_signs.get(p)
            if expected is None:
                continue
            samples = arr[:, i]
            p_expected = float(
                (samples > 0).mean() if expected == "positive" else (samples < 0).mean()
            )
            if p_expected < 0.10:
                msg = (
                    f"Parent '{p}' on '{target}': declared {expected} effect, but "
                    f"P(beta_raw {'>' if expected == 'positive' else '<'} 0) = "
                    f"{p_expected:.2f} (posterior mean {float(samples.mean()):.4g}) — "
                    "the learned direction contradicts the declaration. Check for "
                    "scale confounding (e.g. regress rates on rates instead of "
                    "levels on levels; see docs/model.md)."
                )
                sign_warnings.append(msg)
                logger.warning(msg)
        if sign_warnings:
            diagnostics["sign_warnings"] = sign_warnings

    return FitResult(
        trace=trace,
        target=target,
        parents=parents if X is not None else [],
        y_mean=y_mean,
        y_std=y_std,
        x_stds=x_stds,
        dates=dates,
        inference_method=inference_method,
        fit_end=fit_end,
        grain=grain,
        diagnostics=diagnostics,
        ppc_band=ppc_band,
    )

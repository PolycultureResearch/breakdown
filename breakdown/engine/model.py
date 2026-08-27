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

# Draws used for the k-hat estimate. The Pareto fit uses the largest 20% of
# the ratios, so 1000 draws fit the tail on ~200 points — the same order as
# the ArviZ/loo default — and costs ~0.2s after the (one-off, ~1.5s) graph
# compile, against a 1.4-6s ADVI fit on the demo trees. Nothing about the
# diagnostic scales with the tree, only with the model's latent count.
_KHAT_DRAWS = 1000


def _psis_khat(
    approx: Any, n_draws: int = _KHAT_DRAWS, random_seed: Optional[int] = None
) -> Tuple[Optional[float], str, Optional[str]]:
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

    Returns `(khat, status, reason)`. `status` is one of `"ok"`, `"suspect"`,
    `"unusable"` (the three bands above) or `"unavailable"` — the last means
    the number could not be computed, and then `khat` is None and `reason`
    says why. Never returns a non-finite k-hat: a NaN k-hat rendered as a
    number is worse than no k-hat, and it would reach an encoder (rule 3).
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
            "unavailable",
            f"only {n_finite} of {log_ratios.size} log importance ratios were finite",
        )
    centered = log_ratios[finite]
    centered = centered - centered.max()
    _, khat = az.psislw(centered.reshape(1, -1))
    k = float(np.asarray(khat).ravel()[0])
    if not np.isfinite(k):
        return None, "unavailable", "the generalized Pareto fit returned a non-finite shape"
    if k <= _KHAT_GOOD:
        return k, "ok", None
    if k <= _KHAT_UNUSABLE:
        return k, "suspect", None
    return k, "unusable", None


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


def _khat_warning(target: str, method: str, khat: Optional[float], status: str, reason) -> str:
    """The one self-contained sentence a k-hat verdict travels with."""
    if status == "unavailable":
        return (
            f"The approximation quality of the {method} fit for '{target}' could not be "
            f"checked: {reason}. That is an unchecked fit, not a clean one — its intervals "
            "carry no evidence either way (see docs/model.md)."
        )
    if status == "suspect":
        return (
            f"The {method} approximation for '{target}' has PSIS k-hat = {khat:.2f} "
            f"(> {_KHAT_GOOD}): the importance ratios against the true posterior have no "
            "finite variance, so this fit sits measurably away from the posterior it "
            "approximates. Read its intervals as approximate, and confirm anything "
            "load-bearing with a NUTS fit (see docs/model.md)."
        )
    return (
        f"The {method} approximation for '{target}' has PSIS k-hat = {khat:.2f} "
        f"(> {_KHAT_UNUSABLE}): the importance ratios against the true posterior have "
        "neither finite variance nor a usable mean, so the approximation is not close to "
        "the posterior and cannot be corrected by reweighting. Its credible intervals are "
        "not evidence about the width of the real ones. This fit ran a variational "
        "approximation because one was asked for; drop `inference_method=advi` to get "
        "the NUTS default, or re-fit this node alone with "
        f"POST /analyze/{target}?inference_method=nuts."
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
        khat, khat_status, khat_reason = _psis_khat(
            approx, n_draws=khat_draws, random_seed=random_seed
        )
    except Exception as e:  # pragma: no cover - defensive: a PyMC API change
        khat, khat_status, khat_reason = None, "unavailable", f"{type(e).__name__}: {e}"
        logger.warning("PSIS k-hat could not be computed for '%s': %s", target, e)

    diagnostics: Dict[str, Any] = {
        "fit_quality": "suspect"
        if (elbo_suspect or khat_status in ("suspect", "unusable"))
        else "ok",
        "method": method,
        "elbo_drop": elbo_drop,
        "khat": khat,
        "khat_status": khat_status,
    }
    if khat_status != "ok":
        msg = _khat_warning(target, method, khat, khat_status, khat_reason)
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

    if diagnostics["fit_quality"] == "suspect":
        logger.warning("Fit for '%s' is suspect: %s", target, diagnostics)
    if seasonality_warnings:
        diagnostics["seasonality_warnings"] = seasonality_warnings
    if likelihood_warnings:
        diagnostics["likelihood_warnings"] = likelihood_warnings

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
    )

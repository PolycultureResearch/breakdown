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

import arviz as az
import networkx as nx
import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt

from breakdown.formula import eval_formula
from breakdown.grains import ensure_grained, fit_grain, next_start
from breakdown.parser import MetricDefinition

logger = logging.getLogger(__name__)

# Minimum whole periods (at the node's grain, after lag trimming) a fit needs.
# `breakdown doctor` reports per-metric readiness against the same number.
MIN_FIT_PERIODS = 10


@dataclass
class FitResult:
    """Everything a caller needs from one node's fit, not just the trace.

    `fit_metric` used to return a bare `arviz.InferenceData`, discarding the
    normalization constants, the effective date index, and the fit metadata.
    Downstream code (RCA's trend/seasonal decomposition, window-keyed caching,
    business-unit rendering) all need those pieces, so they are carried here.
    """

    trace: Any                       # arviz.InferenceData
    target: str
    parents: List[str]               # regressor parents ([] for roots/formula nodes)
    y_mean: float                    # of the fitted y series (residual for formula nodes)
    y_std: float
    x_stds: Optional[np.ndarray]     # per-parent stds of the (lag-shifted) regressors, None if no X
    dates: pd.DatetimeIndex          # period starts actually used in the fit (after lag trim and fit_end cut)
    inference_method: str            # "nuts" | "advi"
    fit_end: Optional[str] = None    # exclusive upper date bound of the fit; None = full window
    grain: str = "day"               # the grain the fit ran at (the node's own)
    diagnostics: Dict[str, Any] = field(default_factory=dict)  # populated by T8


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


_PRIOR_DISTRIBUTIONS = {
    "Normal": pm.Normal,
    "HalfNormal": pm.HalfNormal,
    "Exponential": pm.Exponential,
    "LogNormal": pm.LogNormal,
}


def compute_shapley(
    formula: str,
    parent_names: List[str],
    baselines: Dict[str, Any],
    actuals: Dict[str, Any],
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
    """
    n = len(parent_names)
    if n == 0:
        return {}

    array_input = any(
        isinstance(v, np.ndarray) and v.ndim > 0
        for v in [*baselines.values(), *actuals.values()]
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
                    p: a_arr[p] if p in coalition_set else b_arr[p]
                    for p in parent_names
                }
                phi = phi + weight * (
                    eval_formula(formula, vals_with) - eval_formula(formula, vals_without)
                )

        shapley[player] = phi if array_input else float(phi[0])

    return shapley


def summarize_trace(trace: Any) -> pd.DataFrame:
    """ArviZ posterior summary (mean, sd, 95% HDI, diagnostics) for a trace."""
    return az.summary(trace, hdi_prob=0.95)


def _nuts_diagnostics(trace: Any, draws: int, chains: int) -> Dict[str, Any]:
    """Convergence diagnostics for a NUTS trace.

    `fit_quality` is "suspect" when divergences exceed 1% of total draws, any
    r_hat exceeds 1.05, or any bulk ESS falls below 100 — coarse thresholds
    that flag only fits whose credible intervals should not be trusted as-is.
    Nothing blocks on "suspect"; it is information, not an error.
    """
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


def _advi_diagnostics(approx: Any) -> Dict[str, Any]:
    """Convergence diagnostics for a mean-field ADVI fit.

    Compares the mean loss (−ELBO) of the last 10% of iterations against the
    preceding 10%: if the loss is still moving by more than half its recent
    noise level, the optimization had not converged and the approximation is
    suspect. `elbo_drop` (mean(prev) − mean(last)) is positive while the loss
    is still falling.
    """
    hist = np.asarray(approx.hist, dtype=float)
    w = len(hist) // 10
    if w < 1:
        return {"fit_quality": "suspect", "method": "advi", "elbo_drop": None}
    last, prev = hist[-w:], hist[-2 * w:-w]
    elbo_drop = float(prev.mean() - last.mean())
    suspect = abs(last.mean() - prev.mean()) > 0.5 * last.std()
    return {
        "fit_quality": "suspect" if suspect else "ok",
        "method": "advi",
        "elbo_drop": elbo_drop,
    }


def _validate_columns(data: pd.DataFrame, cols: List[str]) -> None:
    missing = [c for c in cols if c not in data.columns]
    if missing:
        raise ValueError(f"Columns missing from data: {missing}")
    with_nan = [c for c in cols if data[c].isna().any()]
    if with_nan:
        raise ValueError(f"NaN values found in columns: {with_nan}")


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
    if max_lag > 0 and len(data) - max_lag < MIN_FIT_PERIODS:
        raise ValueError(
            f"Not enough rows after applying lags to '{target}': "
            f"{len(data)} rows minus max lag {max_lag} leaves "
            f"{len(data) - max_lag} (need >= {MIN_FIT_PERIODS})."
        )

    if defn.formula and parents:
        # With lags, the identity is cohort-aligned: A[t] = f(parents with
        # each parent shifted back by its lag). Shift, trim the leading
        # max-lag rows, and fit the residual of the lagged identity.
        parent_arrays = {
            p: data[p].shift(lags.get(p, 0)).values.astype(float)[max_lag:]
            for p in parents
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


def _seasonal_component(seasonality: List[Any], t: np.ndarray):
    """Fourier seasonality: 2 sin/cos harmonic pairs per YAML entry.
    Must be called inside a pm.Model context."""
    terms = []
    for s in seasonality:
        for k in (1, 2):
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
        for k in (1, 2):
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
    if X is None:
        return 0.0
    betas = []
    for i, p in enumerate(parents):
        prior = defn.priors.get(p) or defn.priors.get("coefficient")
        if prior:
            scaled = scale_prior_params(prior.distribution, prior.params, scale[i])
            betas.append(_PRIOR_DISTRIBUTIONS[prior.distribution](f"beta_{p}", **scaled))
        else:
            betas.append(pm.Normal(f"beta_{p}", mu=0.0, sigma=1.0))
    beta = pm.Deterministic("beta", pm.math.stack(betas))
    pm.Deterministic("beta_raw", beta / scale)
    return pm.math.dot(X, beta)


def fit_metric(
    dag: nx.DiGraph,
    data: pd.DataFrame,
    target: str,
    draws: int = 1000,
    tune: int = 1000,
    inference_method: str = "nuts",
    fit_end: Optional[str] = None,
    chains: int = 4,
    random_seed: Optional[int] = None,
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
    NUTS handles poorly and mean-field ADVI (the RCA default) fails on outright.
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

    Inference: "nuts" (exact MCMC, use when accuracy matters) or "advi"
    (variational approximation, ~5-10x faster, use for triage). `tune` and
    `chains` apply to NUTS only. `random_seed` makes the fit reproducible on
    a given platform/dependency set (RCA and simulate pass a fixed seed so
    their on-demand fits — and hence API responses — are deterministic even
    across empty trace caches; tests seed to kill stochastic flakes).

    Returns a `FitResult`. Its trace's posterior includes `beta_raw`
    (= beta / scale): the coefficient on each parent in business units,
    i.e. d(target) per unit change of that parent.
    """
    if inference_method not in ("nuts", "advi"):
        raise ValueError(f"inference_method must be 'nuts' or 'advi', got '{inference_method}'")

    parents = list(dag.predecessors(target))
    # The fit runs at the node's own grain: the target native, finer
    # flow/stock parents resampled up, aligned on whole periods.
    grain = fit_grain(dag, target)
    data = ensure_grained(data).fit_frame(target, parents, grain)

    if fit_end is not None:
        dates = pd.to_datetime(data["date"])
        cutoff = pd.to_datetime(fit_end)
        # Whole-period-strict: keep only periods that END on/before fit_end,
        # so a coarse period straddling the anomaly can't train the model.
        # For day grain `next_start(d) <= fit_end` is exactly `d < fit_end` —
        # identical to the pre-grain behavior.
        ends = pd.DatetimeIndex([next_start(d, grain) for d in dates])
        data = data.loc[ends <= cutoff].reset_index(drop=True)
        if len(data) < MIN_FIT_PERIODS:
            raise ValueError(
                f"Only {len(data)} whole {grain} periods before fit_end={fit_end} "
                f"for '{target}' (need >= {MIN_FIT_PERIODS})."
            )

    _validate_columns(data, [target] + parents)
    defn: MetricDefinition = dag.nodes[target]["definition"]

    y, X, scale, y_mean, y_std, x_stds, dates = _prepare_series(defn, parents, data, target)
    t = np.arange(len(y))

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
            target, inference_method, draws, tune, fit_end,
        )
        if inference_method == "advi":
            approx = pm.fit(
                n=20_000, method="advi", progressbar=False, random_seed=random_seed
            )
            trace = approx.sample(draws=draws, random_seed=random_seed)
            diagnostics = _advi_diagnostics(approx)
        else:
            trace = pm.sample(
                draws=draws, tune=tune, target_accept=0.9, chains=chains,
                random_seed=random_seed,
            )
            diagnostics = _nuts_diagnostics(trace, draws, chains)

    if diagnostics["fit_quality"] == "suspect":
        logger.warning("Fit for '%s' is suspect: %s", target, diagnostics)
    if seasonality_warnings:
        diagnostics["seasonality_warnings"] = seasonality_warnings

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
            p_expected = float((samples > 0).mean() if expected == "positive" else (samples < 0).mean())
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

import logging
import math
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import networkx as nx
import pymc as pm

from breakdown.formula import eval_formula

logger = logging.getLogger(__name__)


def scale_prior_params(distribution: str, params: Dict[str, Any], scale: np.ndarray) -> Dict[str, Any]:
    """
    Translate raw-scale (business-unit) prior parameters into normalized space.

    The model regresses z-scored y on z-scored x, so a raw-scale coefficient
    beta_raw maps to beta_norm = beta_raw * (x_std / y_std). `scale` is that
    x_std / y_std factor, one entry per parent.
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
    baselines: Dict[str, float],
    actuals: Dict[str, float],
) -> Dict[str, float]:
    """
    Distribute the gap between formula(actuals) and formula(baselines) across
    each parent using exact Shapley values.
    """
    n = len(parent_names)
    if n == 0:
        return {}

    shapley: Dict[str, float] = {}
    for player in parent_names:
        others = [p for p in parent_names if p != player]
        phi = 0.0
        for r in range(n):
            for coalition in combinations(others, r):
                coalition_set = set(coalition)
                weight = math.factorial(r) * math.factorial(n - r - 1) / math.factorial(n)

                vals_with = {
                    p: actuals[p] if (p in coalition_set or p == player) else baselines[p]
                    for p in parent_names
                }
                vals_without = {
                    p: actuals[p] if p in coalition_set else baselines[p]
                    for p in parent_names
                }

                v_with = float(eval_formula(formula, {k: np.array([v]) for k, v in vals_with.items()})[0])
                v_without = float(eval_formula(formula, {k: np.array([v]) for k, v in vals_without.items()})[0])
                phi += weight * (v_with - v_without)

        shapley[player] = phi

    return shapley


class ModelBuilder:
    def __init__(self, dag: nx.DiGraph, data: pd.DataFrame):
        self.dag = dag
        self.data = data
        self.models: Dict[str, pm.Model] = {}
        self.traces: Dict[str, Any] = {}
        self.scale_params: Dict[str, Tuple[float, float]] = {}
        self.formula_nodes: Dict[str, str] = {}
        self.lags: Dict[str, Dict[str, int]] = {}

    def _validate_data(self, target: str, parents: List[str]) -> None:
        missing_cols = [c for c in [target] + parents if c not in self.data.columns]
        if missing_cols:
            raise ValueError(f"Columns missing from data: {missing_cols}")

        cols_with_nan = [c for c in [target] + parents if self.data[c].isna().any()]
        if cols_with_nan:
            raise ValueError(f"NaN values found in columns: {cols_with_nan}")

    def _normalize(self, series: pd.Series) -> Tuple[np.ndarray, float, float]:
        mean = series.mean()
        std = series.std()
        if std == 0:
            raise ValueError(f"Column '{series.name}' has zero variance — cannot normalize.")
        return (series.values - mean) / std, float(mean), float(std)

    def build_and_sample(
        self,
        target_metric_name: str,
        draws: int = 1000,
        tune: int = 1000,
        inference_method: str = "nuts",
    ) -> Any:
        if inference_method not in ("nuts", "advi"):
            raise ValueError(f"inference_method must be 'nuts' or 'advi', got '{inference_method}'")

        parents = list(self.dag.predecessors(target_metric_name))
        self._validate_data(target_metric_name, parents)

        metric_node = self.dag.nodes[target_metric_name]
        formula: Optional[str] = metric_node.get("formula")

        if formula and parents:
            self.formula_nodes[target_metric_name] = formula
            parent_arrays = {p: self.data[p].values.astype(float) for p in parents}
            y_formula = eval_formula(formula, parent_arrays)
            residual = self.data[target_metric_name].values.astype(float) - y_formula
            residual_series = pd.Series(residual, name=f"{target_metric_name}_residual")
            y, y_mean, y_std = self._normalize(residual_series)
            X = None
        else:
            lags = metric_node.get("lags") or {}
            max_lag = max(lags.values(), default=0)
            if max_lag > 0 and len(self.data) - max_lag < 10:
                raise ValueError(
                    f"Not enough rows after applying lags to '{target_metric_name}': "
                    f"{len(self.data)} rows minus max lag {max_lag} leaves "
                    f"{len(self.data) - max_lag} (need >= 10)."
                )
            self.lags[target_metric_name] = lags

            # Shift each parent back by its lag, then trim the leading `max_lag`
            # rows so every series aligns with no NaNs; normalize afterward.
            y_series = self.data[target_metric_name]
            if max_lag > 0:
                y_series = y_series.iloc[max_lag:]
            y, y_mean, y_std = self._normalize(y_series)
            X = None
            if parents:
                X_cols = []
                for p in parents:
                    shifted = self.data[p].shift(lags.get(p, 0))
                    if max_lag > 0:
                        shifted = shifted.iloc[max_lag:]
                    col, p_mean, p_std = self._normalize(shifted)
                    self.scale_params[p] = (p_mean, p_std)
                    X_cols.append(col)
                X = np.column_stack(X_cols)

        self.scale_params[target_metric_name] = (y_mean, y_std)

        seasonality_defs = metric_node.get("seasonality", [])
        t = np.arange(len(y))

        with pm.Model() as model:
            sigma_trend = pm.HalfNormal("sigma_trend", 1.0)
            trend = pm.GaussianRandomWalk("trend", sigma=sigma_trend, shape=len(y))

            seasonal_terms = []
            for s in seasonality_defs:
                period = s["period"] if isinstance(s, dict) else s.period
                name = s["name"] if isinstance(s, dict) else s.name
                for k in range(1, 3):
                    sin_term = np.sin(2 * np.pi * k * t / period)
                    cos_term = np.cos(2 * np.pi * k * t / period)
                    a = pm.Normal(f"sin_{name}_h{k}", mu=0, sigma=1.0)
                    b = pm.Normal(f"cos_{name}_h{k}", mu=0, sigma=1.0)
                    seasonal_terms.append(a * sin_term + b * cos_term)

            seasonal = sum(seasonal_terms) if seasonal_terms else 0.0

            if X is not None:
                # Priors are stated in business units; the fit happens in
                # z-scored space, so translate them per parent. Each parent gets
                # its own beta: a per-parent prior if present, else the shared
                # `coefficient` prior, else a weakly informative Normal(0, 1).
                x_stds = np.array([self.scale_params[p][1] for p in parents])
                scale = x_stds / y_std
                priors_cfg = metric_node.get("priors") or {}
                betas = []
                for i, p in enumerate(parents):
                    prior = priors_cfg.get(p) or priors_cfg.get("coefficient")
                    if prior:
                        scaled = scale_prior_params(
                            prior["distribution"], prior.get("params", {}), scale[i]
                        )
                        betas.append(
                            _PRIOR_DISTRIBUTIONS[prior["distribution"]](f"beta_{p}", **scaled)
                        )
                    else:
                        betas.append(pm.Normal(f"beta_{p}", mu=0.0, sigma=1.0))
                beta = pm.Deterministic("beta", pm.math.stack(betas))
                pm.Deterministic("beta_raw", beta / scale)
                regression = pm.math.dot(X, beta)
            else:
                regression = 0.0

            alpha = pm.Normal("alpha", mu=0, sigma=10.0)
            mu = alpha + trend + seasonal + regression
            sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
            pm.Normal("obs", mu=mu, sigma=sigma_obs, observed=y)

            logger.info(
                "Sampling metric '%s' method=%s draws=%d tune=%d",
                target_metric_name, inference_method, draws, tune,
            )

            if inference_method == "advi":
                approx = pm.fit(n=20_000, method="advi", progressbar=False)
                trace = approx.sample(draws=draws)
            else:
                trace = pm.sample(draws=draws, tune=tune, target_accept=0.9, chains=2)

        self.models[target_metric_name] = model
        self.traces[target_metric_name] = trace
        return trace

    def compute_shapley(
        self,
        target_metric_name: str,
        reference_start: str,
        reference_end: str,
        analysis_start: str,
        analysis_end: str,
    ) -> Dict[str, Any]:
        formula = self.formula_nodes.get(target_metric_name)
        if formula is None:
            formula = self.dag.nodes[target_metric_name].get("formula")
        if not formula:
            raise ValueError(
                f"Metric '{target_metric_name}' has no formula — "
                "Shapley attribution requires a formula definition."
            )

        parents = list(self.dag.predecessors(target_metric_name))
        all_cols = [target_metric_name] + parents
        missing = [c for c in all_cols if c not in self.data.columns]
        if missing:
            raise ValueError(f"Columns missing from data: {missing}")

        data = self.data.copy()
        data["date"] = pd.to_datetime(data["date"])

        ref_mask = (data["date"] >= reference_start) & (data["date"] <= reference_end)
        act_mask = (data["date"] >= analysis_start) & (data["date"] <= analysis_end)

        if not ref_mask.any():
            raise ValueError(f"No data in reference window [{reference_start}, {reference_end}]")
        if not act_mask.any():
            raise ValueError(f"No data in analysis window [{analysis_start}, {analysis_end}]")

        baselines = {col: float(data.loc[ref_mask, col].mean()) for col in all_cols}
        actuals = {col: float(data.loc[act_mask, col].mean()) for col in all_cols}

        parent_baselines = {p: baselines[p] for p in parents}
        parent_actuals = {p: actuals[p] for p in parents}

        shapley_values = compute_shapley(formula, parents, parent_baselines, parent_actuals)

        baseline_target = float(eval_formula(formula, {p: np.array([parent_baselines[p]]) for p in parents})[0])
        actual_target = float(eval_formula(formula, {p: np.array([parent_actuals[p]]) for p in parents})[0])

        return {
            "target": target_metric_name,
            "formula": formula,
            "baseline": baseline_target,
            "actual": actual_target,
            "gap": actual_target - baseline_target,
            "attribution": shapley_values,
        }

    def get_summary(self, target_metric_name: str) -> pd.DataFrame:
        import arviz as az
        if target_metric_name not in self.traces:
            raise ValueError(f"No trace found for metric '{target_metric_name}'")
        return az.summary(self.traces[target_metric_name], hdi_prob=0.95)

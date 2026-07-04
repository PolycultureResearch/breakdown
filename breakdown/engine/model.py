import ast
import logging
import math
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import networkx as nx
import pymc as pm

logger = logging.getLogger(__name__)

_FORMULA_SAFE_NODES = (
    ast.Expression, ast.BinOp, ast.Name, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
    ast.UnaryOp, ast.USub, ast.UAdd,
    ast.Load,  # context node on Name nodes
)


def _eval_formula(formula: str, values: Dict[str, np.ndarray]) -> np.ndarray:
    tree = ast.parse(formula, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _FORMULA_SAFE_NODES):
            raise ValueError(f"Unsafe formula node: {type(node).__name__}")
    return eval(formula, {"__builtins__": {}}, values)  # noqa: S307


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

                v_with = float(_eval_formula(formula, {k: np.array([v]) for k, v in vals_with.items()})[0])
                v_without = float(_eval_formula(formula, {k: np.array([v]) for k, v in vals_without.items()})[0])
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
            y_formula = _eval_formula(formula, parent_arrays)
            residual = self.data[target_metric_name].values.astype(float) - y_formula
            residual_series = pd.Series(residual, name=f"{target_metric_name}_residual")
            y, y_mean, y_std = self._normalize(residual_series)
            X = None
        else:
            y, y_mean, y_std = self._normalize(self.data[target_metric_name])
            X = None
            if parents:
                X_cols = []
                for p in parents:
                    col, p_mean, p_std = self._normalize(self.data[p])
                    self.scale_params[p] = (p_mean, p_std)
                    X_cols.append(col)
                X = np.column_stack(X_cols)

        self.scale_params[target_metric_name] = (y_mean, y_std)

        raw_priors = metric_node.get("priors", {})
        coef_prior = raw_priors.get("coefficient")
        if coef_prior and coef_prior.get("distribution") == "Normal":
            beta_mu = float(coef_prior["params"].get("mu", 0.0))
            beta_sigma = float(coef_prior["params"].get("sigma", 1.0))
        else:
            beta_mu, beta_sigma = 0.0, 1.0

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
                beta = pm.Normal("beta", mu=beta_mu, sigma=beta_sigma, shape=X.shape[1])
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

        baseline_target = float(_eval_formula(formula, {p: np.array([parent_baselines[p]]) for p in parents})[0])
        actual_target = float(_eval_formula(formula, {p: np.array([parent_actuals[p]]) for p in parents})[0])

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

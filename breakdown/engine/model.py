import logging
import pymc as pm
import pandas as pd
import numpy as np
import networkx as nx
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ModelBuilder:
    def __init__(self, dag: nx.DiGraph, data: pd.DataFrame):
        self.dag = dag
        self.data = data
        self.models: Dict[str, pm.Model] = {}
        self.traces: Dict[str, Any] = {}
        # Stores (mean, std) per column used for normalization, so posteriors
        # can later be interpreted in original units.
        self.scale_params: Dict[str, Tuple[float, float]] = {}

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

    def build_and_sample(self, target_metric_name: str, draws: int = 1000, tune: int = 1000) -> Any:
        parents = list(self.dag.predecessors(target_metric_name))
        self._validate_data(target_metric_name, parents)

        metric_node = self.dag.nodes[target_metric_name]

        # Normalize target
        y, y_mean, y_std = self._normalize(self.data[target_metric_name])
        self.scale_params[target_metric_name] = (y_mean, y_std)

        # Normalize parents
        X = None
        if parents:
            X_cols = []
            for p in parents:
                col, p_mean, p_std = self._normalize(self.data[p])
                self.scale_params[p] = (p_mean, p_std)
                X_cols.append(col)
            X = np.column_stack(X_cols)

        # Read priors from YAML config (stored in DAG node dict)
        raw_priors = metric_node.get("priors", {})
        coef_prior = raw_priors.get("coefficient")
        if coef_prior and coef_prior.get("distribution") == "Normal":
            beta_mu = float(coef_prior["params"].get("mu", 0.0))
            beta_sigma = float(coef_prior["params"].get("sigma", 1.0))
        else:
            beta_mu, beta_sigma = 0.0, 1.0

        # Build Fourier seasonality components from YAML config
        seasonality_defs = metric_node.get("seasonality", [])
        t = np.arange(len(y))

        with pm.Model() as model:
            # 1. Trend (local level / Gaussian random walk)
            sigma_trend = pm.HalfNormal("sigma_trend", 1.0)
            trend = pm.GaussianRandomWalk("trend", sigma=sigma_trend, shape=len(y))

            # 2. Seasonality (Fourier components, 2 harmonics per period)
            seasonal_terms = []
            for s in seasonality_defs:
                period = s["period"] if isinstance(s, dict) else s.period
                name = s["name"] if isinstance(s, dict) else s.name
                n_harmonics = 2
                for k in range(1, n_harmonics + 1):
                    sin_term = np.sin(2 * np.pi * k * t / period)
                    cos_term = np.cos(2 * np.pi * k * t / period)
                    a = pm.Normal(f"sin_{name}_h{k}", mu=0, sigma=1.0)
                    b = pm.Normal(f"cos_{name}_h{k}", mu=0, sigma=1.0)
                    seasonal_terms.append(a * sin_term + b * cos_term)

            seasonal = sum(seasonal_terms) if seasonal_terms else 0.0

            # 3. Regression (causal impact of parent metrics)
            if X is not None:
                beta = pm.Normal("beta", mu=beta_mu, sigma=beta_sigma, shape=X.shape[1])
                regression = pm.math.dot(X, beta)
            else:
                regression = 0.0

            # 4. Intercept
            alpha = pm.Normal("alpha", mu=0, sigma=10.0)

            # 5. Likelihood
            mu = alpha + trend + seasonal + regression
            sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
            pm.Normal("obs", mu=mu, sigma=sigma_obs, observed=y)

            logger.info("Sampling metric '%s' (draws=%d, tune=%d)", target_metric_name, draws, tune)
            trace = pm.sample(draws=draws, tune=tune, target_accept=0.9, chains=2)

        self.models[target_metric_name] = model
        self.traces[target_metric_name] = trace
        return trace

    def get_summary(self, target_metric_name: str) -> pd.DataFrame:
        import arviz as az
        if target_metric_name not in self.traces:
            raise ValueError(f"No trace found for metric '{target_metric_name}'")
        return az.summary(self.traces[target_metric_name], hdi_prob=0.95)

import pymc as pm
import pandas as pd
import numpy as np
import networkx as nx
from typing import Dict, Any, List
from breakdown.parser import MetricDefinition

class ModelBuilder:
    def __init__(self, dag: nx.DiGraph, data: pd.DataFrame):
        self.dag = dag
        self.data = data
        self.models: Dict[str, pm.Model] = {}
        self.traces: Dict[str, Any] = {}

    def build_and_sample(self, target_metric_name: str, draws: int = 1000, tune: int = 1000) -> Any:
        metric_node = self.dag.nodes[target_metric_name]
        parents = list(self.dag.predecessors(target_metric_name))
        
        y = self.data[target_metric_name].values
        X = self.data[parents].values if parents else None
        
        with pm.Model() as model:
            # 1. Trend (Local Level Model)
            # sigma_trend ~ HalfNormal(1.0)
            # trend_t = trend_{t-1} + normal(0, sigma_trend)
            sigma_trend = pm.HalfNormal("sigma_trend", 1.0)
            trend = pm.GaussianRandomWalk("trend", sigma=sigma_trend, shape=len(y))
            
            # 2. Regression (Causal Impact of Parents)
            if X is not None:
                # coefficients ~ Normal(0, 1.0)
                # Note: In real scenarios, use priors from YAML
                beta = pm.Normal("beta", mu=0, sigma=1.0, shape=X.shape[1])
                regression = pm.math.dot(X, beta)
            else:
                regression = 0.0
            
            # 3. Intercept
            alpha = pm.Normal("alpha", mu=0, sigma=10.0)
            
            # 4. Likelihood
            mu = alpha + trend + regression
            sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
            obs = pm.Normal("obs", mu=mu, sigma=sigma_obs, observed=y)
            
            # Sampling
            trace = pm.sample(draws=draws, tune=tune, target_accept=0.9, chains=2)
            
        self.models[target_metric_name] = model
        self.traces[target_metric_name] = trace
        return trace

    def get_summary(self, target_metric_name: str) -> pd.DataFrame:
        import arviz as az
        if target_metric_name not in self.traces:
            raise ValueError(f"No trace found for metric '{target_metric_name}'")
        return az.summary(self.traces[target_metric_name], hdi_prob=0.94)

import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.formula import eval_formula

logger = logging.getLogger(__name__)


class BaseDataFetcher(ABC):
    """
    Abstract Base Class for metric data fetching.
    Ensures that regardless of the data source (Cloud, Local, Mock),
    the rest of the app receives a consistent DataFrame.
    """
    @abstractmethod
    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        pass


class CloudDataFetcher(BaseDataFetcher):
    """Fetches data from the dbt Semantic Layer (Cloud) using the official SDK."""
    def __init__(self, environment_id: str, host: str, token: str):
        from dbtsl import SemanticLayerClient
        self.client = SemanticLayerClient(
            environment_id=int(environment_id),
            host=host,
            auth_token=token,
        )

    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        grain_dim = f"metric_time__{grain}"
        with self.client.session():
            table = self.client.query(
                metrics=[metric_name],
                group_by=[grain_dim],
                where=[
                    f"metric_time >= '{start_date}'",
                    f"metric_time <= '{end_date}'",
                ],
            )
        df = table.to_pandas()
        date_col = next(c for c in df.columns if "metric_time" in c.lower())
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", metric_name]].sort_values("date").reset_index(drop=True)


class LocalDataFetcher(BaseDataFetcher):
    """Fetches data from a local dbt project by invoking the MetricFlow CLI."""
    def __init__(self, project_path: str):
        self.project_path = project_path
        logger.info("Initialized LocalDataFetcher for project at %s", project_path)

    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        import os
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            tmp_path = f.name

        try:
            try:
                result = subprocess.run(
                    [
                        "mf", "query",
                        "--metrics", metric_name,
                        "--group-by", f"metric_time__{grain}",
                        "--start-time", start_date,
                        "--end-time", end_date,
                        "--csv", tmp_path,
                    ],
                    cwd=self.project_path,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (FileNotFoundError, NotADirectoryError) as e:
                raise RuntimeError(f"mf query failed for metric '{metric_name}': {e}") from e
            if result.returncode != 0:
                raise RuntimeError(
                    f"mf query failed for metric '{metric_name}':\n{result.stderr.strip()}"
                )
            df = pd.read_csv(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        date_col = next((c for c in df.columns if "metric_time" in c.lower()), None)
        if date_col is None:
            raise RuntimeError(f"No metric_time column found in mf output. Columns: {list(df.columns)}")
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", metric_name]].sort_values("date").reset_index(drop=True)


class MockDataFetcher(BaseDataFetcher):
    """
    Generates synthetic data for development and testing.

    When constructed with a metric DAG, generated series respect the tree
    structure: formula nodes satisfy their formula (plus noise), probabilistic
    nodes co-move with their parents, and root nodes are random walks with
    weekly seasonality. Without a DAG, each metric is an independent random
    walk. Only daily grain is supported.
    """
    def __init__(self, dag: Optional[nx.DiGraph] = None):
        self.dag = dag
        self._cache: Dict[Tuple[str, str], pd.DataFrame] = {}

    @staticmethod
    def _rng_for(metric_name: str) -> np.random.Generator:
        # Seeded deterministically per metric — no global state mutation
        return np.random.default_rng(seed=sum(ord(c) for c in metric_name) % (2**32))

    def _tree_data(self, start_date: str, end_date: str) -> pd.DataFrame:
        key = (start_date, end_date)
        if key in self._cache:
            return self._cache[key]

        dates = pd.date_range(start=start_date, end=end_date)
        n_days = len(dates)
        t = np.arange(n_days)
        values: Dict[str, np.ndarray] = {}

        for name in nx.topological_sort(self.dag):
            defn = self.dag.nodes[name]["definition"]
            rng = self._rng_for(name)
            parents = list(self.dag.predecessors(name))

            if defn.formula and parents:
                base = eval_formula(defn.formula, {p: values[p] for p in parents})
                noise_scale = 0.02 * float(np.abs(base).mean()) or 1.0
                values[name] = base + rng.normal(0, noise_scale, n_days)
            elif parents:
                coef_prior = defn.priors.get("coefficient")
                default_coef = coef_prior.params.get("mu") if coef_prior else None
                signal = np.zeros(n_days)
                for p in parents:
                    parent_vals = values[p]
                    lag = defn.lags.get(p, 0)
                    if lag > 0:
                        # Edge-pad with the first value so the child co-moves
                        # with the parent's value `lag` days earlier.
                        parent_vals = np.concatenate(
                            [np.full(lag, parent_vals[0]), parent_vals[:-lag]]
                        )
                    coef = default_coef if default_coef is not None else float(rng.uniform(0.1, 0.5))
                    signal += coef * parent_vals
                noise_scale = 0.05 * float(np.abs(signal).mean()) or 1.0
                values[name] = signal + rng.normal(0, noise_scale, n_days)
            else:
                level = float(rng.uniform(50, 5000))
                values[name] = (
                    level
                    + np.cumsum(rng.normal(0, 0.02 * level, n_days))
                    + 0.1 * level * np.sin(2 * np.pi * t / 7)
                )

        df = pd.DataFrame({"date": dates, **values})
        self._cache[key] = df
        return df

    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        if end < start:
            raise ValueError(f"end_date '{end_date}' must be >= start_date '{start_date}'")

        if self.dag is not None and metric_name in self.dag:
            return self._tree_data(start_date, end_date)[["date", metric_name]].copy()

        n_days = (end - start).days + 1
        dates = pd.date_range(start=start_date, periods=n_days)
        rng = self._rng_for(metric_name)
        values = 1000 + np.cumsum(rng.normal(0, 10, n_days))

        return pd.DataFrame({"date": dates, metric_name: values})

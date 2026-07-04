import logging
from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
from typing import Dict, List, Optional

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
        import subprocess
        import tempfile
        import os

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
    """Generates synthetic data for development and testing."""
    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        if end < start:
            raise ValueError(f"end_date '{end_date}' must be >= start_date '{start_date}'")
        n_days = (end - start).days + 1
        dates = pd.date_range(start=start_date, periods=n_days)

        # Use a local RNG seeded deterministically per metric — no global state mutation
        seed = sum(ord(c) for c in metric_name) % (2**32)
        rng = np.random.default_rng(seed=seed)
        values = 1000 + np.cumsum(rng.normal(0, 10, n_days))

        return pd.DataFrame({"date": dates, metric_name: values})


def generate_mock_data(n_days: int = 100, seed: int = 42) -> pd.DataFrame:
    """Generate a correlated mock dataset for the jaffle-shop metric tree."""
    rng = np.random.default_rng(seed=seed)
    dates = pd.date_range(start="2024-01-01", periods=n_days)

    # sessions (root)
    daily_sessions = (
        5000
        + np.cumsum(rng.normal(0, 100, n_days))
        + 500 * np.sin(2 * np.pi * np.arange(n_days) / 7)
    )

    # order_count = ~10% of sessions + noise
    order_count = 0.1 * daily_sessions + rng.normal(0, 10, n_days)

    # average_order_value = 50 + noise
    average_order_value = 50 + rng.normal(0, 2, n_days)

    # revenue = order_count * average_order_value + noise
    revenue = order_count * average_order_value + rng.normal(0, 100, n_days)

    return pd.DataFrame({
        "date": dates,
        "daily_sessions": daily_sessions,
        "order_count": order_count,
        "average_order_value": average_order_value,
        "revenue": revenue,
    })

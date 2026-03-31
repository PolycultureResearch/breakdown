from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import os

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
            environment_id=environment_id,
            host=host,
            auth_token=token
        )

    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        print(f"DEBUG: Fetching {metric_name} from dbt Cloud...")
        # Placeholder: Implement actual SDK query logic here
        return pd.DataFrame()

class LocalDataFetcher(BaseDataFetcher):
    """Fetches data from a local dbt project using dbt-metricflow."""
    def __init__(self, project_path: str):
        self.project_path = project_path
        # In a real implementation, we would initialize MetricFlow with the local project
        print(f"DEBUG: Initialized LocalFetcher for {project_path}")

    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        print(f"DEBUG: Fetching {metric_name} from local dbt project via MetricFlow...")
        # Placeholder: Implement MetricFlow query logic here
        return pd.DataFrame()

class MockDataFetcher(BaseDataFetcher):
    """Generates synthetic data for development and testing."""
    def fetch_metric(self, metric_name: str, start_date: str, end_date: str, grain: str = "day") -> pd.DataFrame:
        n_days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 1
        dates = pd.date_range(start=start_date, periods=n_days)
        
        # Consistent-ish mock data generation using seeds based on metric_name
        np.random.seed(sum(ord(c) for c in metric_name))
        values = 1000 + np.cumsum(np.random.normal(0, 10, n_days))
        
        return pd.DataFrame({"date": dates, metric_name: values})

def generate_mock_data(n_days: int = 100) -> pd.DataFrame:
    """Retained for backward compatibility in the MVP/PoC phase."""
    dates = pd.date_range(start="2024-01-01", periods=n_days)
    
    # sessions (Root)
    daily_sessions = 5000 + np.cumsum(np.random.normal(0, 100, n_days)) + 500 * np.sin(2 * np.pi * np.arange(n_days) / 7)
    
    # order_count = 0.1 * daily_sessions + noise
    order_count = 0.1 * daily_sessions + np.random.normal(0, 10, n_days)
    
    # average_order_value = 50 + noise
    average_order_value = 50 + np.random.normal(0, 2, n_days)
    
    # revenue = order_count * average_order_value + noise
    revenue = order_count * average_order_value + np.random.normal(0, 100, n_days)

    df = pd.DataFrame({
        "date": dates,
        "daily_sessions": daily_sessions,
        "order_count": order_count,
        "average_order_value": average_order_value,
        "revenue": revenue
    })
    
    return df

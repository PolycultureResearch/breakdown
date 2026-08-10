"""Synthetic datasets shared across test modules."""

import numpy as np
import pandas as pd


def win(ref, an):
    """Window kwargs for run_rca/shapley_attribution, whose window arguments
    are keyword-only (the reference pair is optional and defaults)."""
    return {
        "reference_start": ref[0],
        "reference_end": ref[1],
        "analysis_start": an[0],
        "analysis_end": an[1],
    }


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

    return pd.DataFrame(
        {
            "date": dates,
            "daily_sessions": daily_sessions,
            "order_count": order_count,
            "average_order_value": average_order_value,
            "revenue": revenue,
        }
    )

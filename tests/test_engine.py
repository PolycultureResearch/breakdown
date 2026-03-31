import pytest
import pandas as pd
import numpy as np
from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder
from breakdown.data_fetch import generate_mock_data

def test_model_builder_sampling():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: conversions
    source: dbt.metric.conversions
    parents: [dau]
"""
    parser = Parser(yaml_content)
    data = generate_mock_data(n_days=50)
    
    # Scale data to make sampling easier for the test
    data["dau"] = (data["dau"] - data["dau"].mean()) / data["dau"].std()
    data["conversions"] = (data["conversions"] - data["conversions"].mean()) / data["conversions"].std()

    builder = ModelBuilder(parser.dag, data)
    
    # Small draws for fast testing
    trace = builder.build_and_sample("conversions", draws=200, tune=200)
    
    assert "beta" in trace.posterior
    assert "trend" in trace.posterior
    
    summary = builder.get_summary("conversions")
    assert "beta[0]" in summary.index
    assert summary.loc["beta[0]", "mean"] is not None

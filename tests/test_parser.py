import pytest
from breakdown.parser import Parser

def test_valid_yaml_parsing():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
  - name: conversions
    source: dbt.metric.conversions
    parents:
      - dau
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.05 }
"""
    parser = Parser(yaml_content)
    assert "dau" in parser.dag.nodes
    assert "conversions" in parser.dag.nodes
    assert parser.dag.has_edge("dau", "conversions")

def test_invalid_distribution():
    yaml_content = """
metrics:
  - name: dau
    source: dbt.metric.dau
    priors:
      coefficient:
        distribution: "InvalidDist"
"""
    with pytest.raises(ValueError, match="Invalid distribution: InvalidDist"):
        Parser(yaml_content)

def test_cycle_detection():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [B]
  - name: B
    source: dbt.metric.B
    parents: [A]
"""
    with pytest.raises(ValueError, match="Metric tree contains cycles"):
        Parser(yaml_content)

def test_missing_parent():
    yaml_content = """
metrics:
  - name: A
    source: dbt.metric.A
    parents: [Missing]
"""
    with pytest.raises(ValueError, match="Parent metric 'Missing' not found for metric 'A'"):
        Parser(yaml_content)

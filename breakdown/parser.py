import ast
import yaml
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator
import networkx as nx

class Prior(BaseModel):
    distribution: str
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("distribution")
    @classmethod
    def validate_distribution(cls, v: str) -> str:
        valid_dists = ["Normal", "HalfNormal", "Exponential", "LogNormal"]
        if v not in valid_dists:
            raise ValueError(f"Invalid distribution: {v}. Must be one of {valid_dists}")
        return v

class DataProviderConfig(BaseModel):
    type: str = "mock" # "mock", "local", "cloud"
    project_path: Optional[str] = None
    environment_id: Optional[str] = None
    host: Optional[str] = None
    token: Optional[str] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ["mock", "local", "cloud"]:
            raise ValueError("type must be one of: mock, local, cloud")
        return v

class Seasonality(BaseModel):
    period: int
    name: str

_FORMULA_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.Name, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
    ast.UnaryOp, ast.USub, ast.UAdd,
    ast.Load,  # context node on Name nodes
)

class MetricDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    source: str
    formula: Optional[str] = None
    parents: List[str] = Field(default_factory=list)
    priors: Dict[str, Prior] = Field(default_factory=dict)
    seasonality: List[Seasonality] = Field(default_factory=list)
    trend: Optional[str] = None

    @model_validator(mode="after")
    def validate_formula(self) -> "MetricDefinition":
        if self.formula is None:
            return self
        try:
            tree = ast.parse(self.formula, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Invalid formula syntax: {e}") from e
        for node in ast.walk(tree):
            if not isinstance(node, _FORMULA_ALLOWED_NODES):
                raise ValueError(f"Formula contains unsupported operation: {type(node).__name__}")
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        missing = referenced - set(self.parents)
        if missing:
            raise ValueError(f"Formula references metrics not listed in parents: {missing}")
        return self

class MetricTreeConfig(BaseModel):
    provider: DataProviderConfig = Field(default_factory=DataProviderConfig)
    metrics: List[MetricDefinition]

class Parser:
    def __init__(self, yaml_content: str):
        self.config = self._parse_yaml(yaml_content)
        self.dag = self._build_dag()

    def _parse_yaml(self, content: str) -> MetricTreeConfig:
        data = yaml.safe_load(content)
        return MetricTreeConfig(**data)

    def _build_dag(self) -> nx.DiGraph:
        G = nx.DiGraph()
        
        # Add all nodes first
        for metric in self.config.metrics:
            G.add_node(metric.name, **metric.model_dump())

        # Add edges
        for metric in self.config.metrics:
            for parent in metric.parents:
                if parent not in G:
                    raise ValueError(f"Parent metric '{parent}' not found for metric '{metric.name}'")
                G.add_edge(parent, metric.name)

        # Check for cycles
        if not nx.is_directed_acyclic_graph(G):
            cycles = list(nx.simple_cycles(G))
            raise ValueError(f"Metric tree contains cycles: {cycles}")

        return G

    def get_metric(self, name: str) -> Optional[MetricDefinition]:
        for metric in self.config.metrics:
            if metric.name == name:
                return metric
        return None

    def get_topological_order(self) -> List[str]:
        return list(nx.topological_sort(self.dag))

import os
import re
from typing import Any, Dict, List, Optional

import networkx as nx
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from breakdown.formula import referenced_names, validate_formula


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

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Optional[str]) -> Optional[str]:
    """Expand ${VAR} references against the environment. Keeps secrets out of
    committed tree YAML — the config carries ${DATABRICKS_TOKEN}, not the value."""
    if value is None:
        return None

    def repl(m: "re.Match[str]") -> str:
        var = m.group(1)
        if var not in os.environ:
            raise ValueError(
                f"Provider config references environment variable '${{{var}}}' which is not set."
            )
        return os.environ[var]

    return _ENV_REF.sub(repl, value)


class DataProviderConfig(BaseModel):
    type: str = "mock" # "mock", "local", "cloud", "warehouse"
    project_path: Optional[str] = None
    environment_id: Optional[str] = None
    host: Optional[str] = None
    token: Optional[str] = None
    # warehouse (direct SQL) provider
    http_path: Optional[str] = None
    catalog: Optional[str] = None
    # `schema` in YAML; renamed to avoid shadowing BaseModel.schema
    db_schema: Optional[str] = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ["mock", "local", "cloud", "warehouse"]:
            raise ValueError("type must be one of: mock, local, cloud, warehouse")
        return v

    @field_validator(
        "project_path", "environment_id", "host", "token",
        "http_path", "catalog", "db_schema", mode="after",
    )
    @classmethod
    def expand_env_vars(cls, v: Optional[str]) -> Optional[str]:
        return _expand_env(v)

class Seasonality(BaseModel):
    period: int
    name: str

class TrendConfig(BaseModel):
    """Local-level (random-walk) trend configuration. `sigma` is the prior scale
    on the per-step drift in z-scored space — the knob that controls how much
    movement the trend is allowed to absorb before parents/seasonality must."""
    type: str = "linear"
    sigma: float = 0.05

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v != "linear":
            raise ValueError(f"Unsupported trend type: {v}. Must be 'linear'.")
        return v

    @field_validator("sigma")
    @classmethod
    def validate_sigma(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("trend sigma must be > 0")
        return v

class MetricDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    source: str
    sql: Optional[str] = None
    formula: Optional[str] = None
    parents: List[str] = Field(default_factory=list)
    priors: Dict[str, Prior] = Field(default_factory=dict)
    lags: Dict[str, int] = Field(default_factory=dict)
    seasonality: List[Seasonality] = Field(default_factory=list)
    trend: Optional[TrendConfig] = None
    # UI display hint for the node card's big number; does not affect modeling.
    format: Optional[str] = None

    @field_validator("format")
    @classmethod
    def check_format(cls, v: Any) -> Any:
        if v is not None and v not in ("currency", "percent", "number"):
            raise ValueError(
                f"format must be one of 'currency', 'percent', 'number', got '{v}'"
            )
        return v

    @field_validator("trend", mode="before")
    @classmethod
    def coerce_trend(cls, v: Any) -> Any:
        # Back-compat: `trend: linear` is shorthand for `{type: linear}`.
        if isinstance(v, str):
            if v != "linear":
                raise ValueError(f"Unsupported trend type: {v}. Must be 'linear'.")
            return {"type": "linear"}
        return v

    @model_validator(mode="after")
    def check_formula(self) -> "MetricDefinition":
        if self.formula is None:
            return self
        validate_formula(self.formula)
        missing = referenced_names(self.formula) - set(self.parents)
        if missing:
            raise ValueError(f"Formula references metrics not listed in parents: {missing}")
        return self

    @model_validator(mode="after")
    def check_priors(self) -> "MetricDefinition":
        allowed = {"coefficient", *self.parents}
        for key in self.priors:
            if key not in allowed:
                raise ValueError(
                    f"Prior key '{key}' on metric '{self.name}' must be 'coefficient' "
                    f"or one of the metric's parents {self.parents}."
                )
        return self

    @model_validator(mode="after")
    def check_lags(self) -> "MetricDefinition":
        if not self.lags:
            return self
        if self.formula is not None:
            raise ValueError(
                f"Metric '{self.name}' declares both `formula` and `lags`; a formula "
                "is a contemporaneous identity and cannot use time-lagged parents."
            )
        parent_set = set(self.parents)
        for key, value in self.lags.items():
            if key not in parent_set:
                raise ValueError(
                    f"Lag key '{key}' on metric '{self.name}' must be one of the "
                    f"metric's parents {self.parents}."
                )
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"Lag for '{key}' on metric '{self.name}' must be an integer >= 1, "
                    f"got {value!r}."
                )
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
        # Each node carries its validated MetricDefinition under the
        # "definition" key: dag.nodes[name]["definition"].formula etc.
        G = nx.DiGraph()

        for metric in self.config.metrics:
            G.add_node(metric.name, definition=metric)

        for metric in self.config.metrics:
            for parent in metric.parents:
                if parent not in G:
                    raise ValueError(f"Parent metric '{parent}' not found for metric '{metric.name}'")
                G.add_edge(parent, metric.name)

        if not nx.is_directed_acyclic_graph(G):
            cycles = list(nx.simple_cycles(G))
            raise ValueError(f"Metric tree contains cycles: {cycles}")

        return G

    def get_metric(self, name: str) -> Optional[MetricDefinition]:
        if name not in self.dag:
            return None
        return self.dag.nodes[name]["definition"]

    def get_topological_order(self) -> List[str]:
        return list(nx.topological_sort(self.dag))

import logging
import os
import re
from typing import Any, Dict, List, Optional

import networkx as nx
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from breakdown.formula import referenced_names, validate_formula
from breakdown.grains import GRAINS, is_finer, nests_in

logger = logging.getLogger(__name__)


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

class AssertedBaseline(BaseModel):
    """Declared operating point for cold-start mode (a tree with no data).

    `[low, high]` is read as the central 90% interval of a Normal — the same
    elicitation convention as what-if assumption effects — with `low == high`
    degenerating to a point. Units are mean per native grain period, exactly
    what a fitted baseline (`window_mean`) would be. Written in YAML either as
    the shorthand `baseline: 1200` or as `baseline: {low: 800, high: 1600}`.
    """

    low: float
    high: float

    @model_validator(mode="after")
    def check_bounds(self) -> "AssertedBaseline":
        if self.low > self.high:
            raise ValueError(f"baseline low ({self.low}) must be <= high ({self.high})")
        return self

    @property
    def mu(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def is_point(self) -> bool:
        return self.low == self.high


class PlausibleRange(BaseModel):
    """Declared honesty band for cold-start mode: the substitute for historical
    min/max when there is no history. A simulated value outside the bounds
    flags the node; `min: 0` recovers the non-physical (negative) check.
    Either bound may be omitted; at least one must be present."""

    min: Optional[float] = None
    max: Optional[float] = None

    @model_validator(mode="after")
    def check_bounds(self) -> "PlausibleRange":
        if self.min is None and self.max is None:
            raise ValueError("plausible must declare at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"plausible min ({self.min}) must be <= max ({self.max})")
        return self


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
    type: str = "mock" # "mock", "local", "cloud", "warehouse", "none" (alias "assumed")
    project_path: Optional[str] = None
    environment_id: Optional[str] = None
    host: Optional[str] = None
    token: Optional[str] = None
    # warehouse (direct SQL) provider
    http_path: Optional[str] = None
    # Databricks CLI auth profile (from `databricks auth login --profile ...`).
    # When set, the warehouse provider authenticates via the Databricks SDK's
    # unified OAuth instead of a PAT `token`; `host` is then read from the
    # profile if not given explicitly.
    profile: Optional[str] = None
    catalog: Optional[str] = None
    # `schema` in YAML; renamed to avoid shadowing BaseModel.schema
    db_schema: Optional[str] = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        # `none` declares a cold-start tree (no data is ever fetched);
        # `assumed` is the same thing said from the tree author's seat.
        if v == "assumed":
            return "none"
        if v not in ["mock", "local", "cloud", "warehouse", "none"]:
            raise ValueError("type must be one of: mock, local, cloud, warehouse, none")
        return v

    @field_validator(
        "project_path", "environment_id", "host", "token",
        "http_path", "profile", "catalog", "db_schema", mode="after",
    )
    @classmethod
    def expand_env_vars(cls, v: Optional[str]) -> Optional[str]:
        return _expand_env(v)

class Seasonality(BaseModel):
    period: int
    name: str

    @field_validator("period")
    @classmethod
    def check_period(cls, v: int) -> int:
        # A period of 2 is at the Nyquist limit of the grain it is declared in:
        # every Fourier term the model would fit is either identically zero or
        # collinear with the intercept, so the component is unidentifiable no
        # matter how much data arrives. That is a config error, not a data
        # shortage — reject it here rather than fitting pure-prior parameters.
        if v < 3:
            raise ValueError(
                f"seasonality period must be an integer >= 3 (in grain steps), got {v}. "
                "A period of 2 is unidentifiable at any sample size; a cycle needs "
                "more than two steps per period to be distinguishable from the level."
            )
        return v

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

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIMPLE_RATIO = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*/\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


class DimensionSpec(BaseModel):
    """A declared slicing dimension for a metric: the axis along which its
    window-over-window gap can be attributed to slices (geo, plan, channel…).

    `source` is the provider's dimension identifier — for the semantic-layer
    providers a MetricFlow dimension like `customer__region`; the mock
    provider synthesizes slices for any source. Written in YAML either as the
    shorthand `region: customer__region` or as a mapping.

    Slicing is analysis-time only: sliced series never enter the startup data
    or the fit path, so a dimension declaration never changes modeling.
    """

    source: str
    # Slices kept individually in responses; the rest fold into `__other__`.
    top_k: int = 8
    # Optional pin-list: exactly these slices are kept (rest -> __other__),
    # overriding the volume-ranked top_k selection.
    values: Optional[List[str]] = None
    # Blend-weight metric for rate metrics: the shares of this metric's sliced
    # series weight each slice's rate when recomposing the blended rate.
    # Defaults to the denominator when the rate's formula is `num / den`.
    weight: Optional[str] = None
    # Reserved for the warehouse provider's sliced-SQL contract (a SELECT
    # returning `date, slice, value` with :start_date/:end_date). Not yet
    # consumed — declared here so the shape is stable.
    sql: Optional[str] = None

    @field_validator("top_k")
    @classmethod
    def check_top_k(cls, v: int) -> int:
        if not (2 <= v <= 20):
            raise ValueError(f"dimension top_k must be between 2 and 20, got {v}")
        return v


class MetricFormat(BaseModel):
    """How a metric's big number is displayed on its node card. Presentation
    only — never affects modeling. Written in YAML either as the shorthand
    `format: currency` or as a mapping with any of these keys."""
    style: str = "number"           # "currency" | "percent" | "number"
    unit: Optional[str] = None      # small caption under the value, e.g. "sessions", "ms"
    decimals: Optional[int] = None  # fixed fraction digits; None = automatic
    compact: Optional[bool] = None  # k/M/B notation; None = auto (currency compacts large values)
    symbol: str = "$"               # currency symbol, when style == "currency"

    @field_validator("style")
    @classmethod
    def check_style(cls, v: str) -> str:
        if v not in ("currency", "percent", "number"):
            raise ValueError(
                f"format.style must be 'currency', 'percent', or 'number', got '{v}'"
            )
        return v

    @field_validator("decimals")
    @classmethod
    def check_decimals(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (0 <= v <= 10):
            raise ValueError(f"format.decimals must be between 0 and 10, got {v}")
        return v


class MetricDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    source: str
    # Natural grain of the series: it is fetched, fitted, and attributed at
    # this grain, never below it. Finer flow/stock parents resample up to it.
    grain: str = "day"
    # Temporal aggregation kind: flows sum over time, stocks take the last
    # value, rates can never be auto-aggregated (recompute from components).
    kind: str = "flow"
    sql: Optional[str] = None
    formula: Optional[str] = None
    parents: List[str] = Field(default_factory=list)
    priors: Dict[str, Prior] = Field(default_factory=dict)
    lags: Dict[str, int] = Field(default_factory=dict)
    # Declared direction of each parent's learned coefficient
    # (positive|negative). NOT a prior: the fit is unconstrained, but a
    # posterior that contradicts the declaration raises a diagnostic warning —
    # the classic failure being a scale-confounded level-on-level edge.
    expected_signs: Dict[str, str] = Field(default_factory=dict)
    seasonality: List[Seasonality] = Field(default_factory=list)
    trend: Optional[TrendConfig] = None
    # Cold-start declarations (trees with no data provider). `baseline` is the
    # asserted operating point of a source/probabilistic node (formula nodes
    # derive theirs from parents — declaring one is rejected); `plausible` is
    # the declared honesty band standing in for historical min/max.
    baseline: Optional[AssertedBaseline] = None
    plausible: Optional[PlausibleRange] = None
    # Declared slicing dimensions: name -> DimensionSpec (or the string
    # shorthand `region: customer__region`). Analysis-time only — never
    # affects fetching at startup, fitting, or tree attribution.
    dimensions: Dict[str, DimensionSpec] = Field(default_factory=dict)
    # UI display hint for the node card's big number; does not affect modeling.
    format: Optional[MetricFormat] = None
    # Which way is good news, for UI coloring only (never affects modeling or
    # attribution): "up_is_good" (default — growth metrics), "down_is_good"
    # (costs, tickets, time-to-X), or "neutral" (no judgment, gray). Note a
    # stored-negative flow like churn_mrr is up_is_good: moving toward zero
    # means less churn.
    direction: str = "up_is_good"

    @field_validator("grain")
    @classmethod
    def check_grain(cls, v: str) -> str:
        if v not in GRAINS:
            raise ValueError(f"grain must be one of {list(GRAINS)}, got '{v}'")
        return v

    @field_validator("kind")
    @classmethod
    def check_kind(cls, v: str) -> str:
        if v not in ("flow", "stock", "rate"):
            raise ValueError(f"kind must be one of ['flow', 'stock', 'rate'], got '{v}'")
        return v

    @field_validator("direction")
    @classmethod
    def check_direction(cls, v: str) -> str:
        if v not in ("up_is_good", "down_is_good", "neutral"):
            raise ValueError(
                f"direction must be one of ['up_is_good', 'down_is_good', 'neutral'], got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def check_expected_signs(self) -> "MetricDefinition":
        if not self.expected_signs:
            return self
        if self.formula is not None:
            raise ValueError(
                f"Metric '{self.name}' declares `expected_signs` on a formula "
                "node; a formula is an exact identity with no learned "
                "coefficients to check."
            )
        parent_set = set(self.parents)
        for key, value in self.expected_signs.items():
            if key not in parent_set:
                raise ValueError(
                    f"expected_signs key '{key}' on metric '{self.name}' must be "
                    f"one of the metric's parents {self.parents}."
                )
            if value not in ("positive", "negative"):
                raise ValueError(
                    f"expected_signs['{key}'] on metric '{self.name}' must be "
                    f"'positive' or 'negative', got '{value}'."
                )
        return self

    @model_validator(mode="after")
    def warn_grain_relative_seasonality(self) -> "MetricDefinition":
        # Seasonality periods are in grain steps: `period: 7` means weekly on
        # a daily node but 7 months on a monthly one. Warn when a non-day node
        # declares a classic day-grain period.
        if self.grain != "day":
            for s in self.seasonality:
                if s.period in (7, 30, 365):
                    logger.warning(
                        "Metric '%s': seasonality period %d at grain '%s' spans "
                        "%d %ss — periods are in grain steps; check this is intended.",
                        self.name, s.period, self.grain, s.period, self.grain,
                    )
        return self

    @field_validator("format", mode="before")
    @classmethod
    def coerce_format(cls, v: Any) -> Any:
        # shorthand: `format: currency` is `format: {style: currency}`
        if isinstance(v, str):
            return {"style": v}
        return v

    @field_validator("baseline", mode="before")
    @classmethod
    def coerce_baseline(cls, v: Any) -> Any:
        # shorthand: `baseline: 1200` is `baseline: {low: 1200, high: 1200}`
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return {"low": float(v), "high": float(v)}
        return v

    @model_validator(mode="after")
    def check_baseline(self) -> "MetricDefinition":
        # A formula node's baseline is derived per-draw from its parents so
        # the identity holds by construction; an asserted one could contradict
        # it, so it is rejected rather than silently ignored.
        if self.baseline is not None and self.formula is not None:
            raise ValueError(
                f"Metric '{self.name}' declares `baseline` on a formula node; "
                "formula baselines are derived from parents (the identity must "
                "hold), so an asserted baseline is not allowed."
            )
        return self

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

    @field_validator("dimensions", mode="before")
    @classmethod
    def coerce_dimensions(cls, v: Any) -> Any:
        # shorthand: `region: customer__region` is `region: {source: ...}`
        if isinstance(v, dict):
            return {
                key: ({"source": val} if isinstance(val, str) else val)
                for key, val in v.items()
            }
        return v

    @model_validator(mode="after")
    def check_dimensions(self) -> "MetricDefinition":
        for key, spec in self.dimensions.items():
            if not _IDENTIFIER.match(key):
                raise ValueError(
                    f"Dimension name '{key}' on metric '{self.name}' must be an "
                    "identifier (letters, digits, underscores; not starting with "
                    "a digit)."
                )
            if self.kind != "rate" and spec.weight is not None:
                raise ValueError(
                    f"Dimension '{key}' on metric '{self.name}' declares `weight`, "
                    "which is only meaningful for rate metrics (it supplies the "
                    "blend shares of the sliced rate)."
                )
            if self.kind == "rate" and spec.weight is None:
                # A blended rate needs weights to recompose from slices. Default
                # to the denominator of a simple `num / den` formula; otherwise
                # the author must say what the rate is weighted by.
                m = _SIMPLE_RATIO.match(self.formula or "")
                if m and m.group(2) in self.parents:
                    spec.weight = m.group(2)
                else:
                    raise ValueError(
                        f"Dimension '{key}' on rate metric '{self.name}' needs a "
                        "`weight` (the tree metric whose sliced shares weight each "
                        "slice's rate). It defaults from a `num / den` formula's "
                        "denominator, which this metric does not have."
                    )
        return self

    @model_validator(mode="after")
    def check_lags(self) -> "MetricDefinition":
        # With `formula`, lags declare a cohort-aligned lagged identity:
        # A[t] = f(each parent shifted back by its lag, in grain steps) —
        # e.g. conversions[t] = trial_starts[t-14] * cohort_rate[t].
        if not self.lags:
            return self
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

    @model_validator(mode="after")
    def check_unique_names(self) -> "MetricTreeConfig":
        """Metric names must be unique (roadmap C6).

        Nothing downstream can survive a duplicate, because the DAG is built in
        two passes: the second definition wins `nodes[name]["definition"]` while
        **both** definitions' edges are added. So `list(dag.predecessors(name))`
        returns the union of two parent lists — and that call is the axis order
        of `beta_raw`, the invariant AGENTS.md names as load-bearing. Meanwhile
        `priors`, `lags` and `expected_signs` are validated against the winning
        `defn.parents`, so every declared prior lands on the wrong axis or
        silently falls back to `Normal(0, 1)`, and `_fetch_all_metrics`
        overwrites the data too. None of it raises; it just produces a wrong
        number with a plausible shape.

        Caught here rather than in `_build_dag` so a duplicate is a config
        error at parse time — the same place every other schema violation is
        reported, and before anything has been fetched.
        """
        seen: Dict[str, int] = {}
        for metric in self.metrics:
            seen[metric.name] = seen.get(metric.name, 0) + 1
        dupes = sorted(name for name, count in seen.items() if count > 1)
        if dupes:
            listed = ", ".join(f"'{name}' ({seen[name]}x)" for name in dupes)
            raise ValueError(
                f"Duplicate metric names in the tree: {listed}. Each metric must "
                "be defined exactly once — a repeated name silently merges the "
                "definitions' edges while only one definition survives."
            )
        return self

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

        self._validate_grains(G)
        self._validate_dimension_weights(G)
        return G

    @staticmethod
    def _validate_dimension_weights(G: nx.DiGraph) -> None:
        """A rate dimension's `weight` must name another tree metric (needs the
        full node set, so it can't live on MetricDefinition)."""
        for name in G.nodes:
            defn = G.nodes[name]["definition"]
            for dim_name, spec in defn.dimensions.items():
                if spec.weight is not None and spec.weight not in G:
                    raise ValueError(
                        f"Dimension '{dim_name}' on metric '{name}' declares "
                        f"weight '{spec.weight}', which is not a metric in the "
                        "tree."
                    )

    @staticmethod
    def _validate_grains(G: nx.DiGraph) -> None:
        """Cross-node grain rules (need both edge endpoints, so they can't
        live on MetricDefinition): a parent may never be coarser than its
        child, and a finer parent must be auto-aggregatable up to the child's
        grain — flow/stock kinds whose grain nests in the child's."""
        for parent, child in G.edges:
            pdefn = G.nodes[parent]["definition"]
            cdefn = G.nodes[child]["definition"]
            pg, cg = pdefn.grain, cdefn.grain
            if is_finer(cg, pg):
                raise ValueError(
                    f"Metric '{child}' (grain '{cg}') has parent '{parent}' at "
                    f"coarser grain '{pg}'. A parent may not be coarser than its "
                    f"child — downward disaggregation is undefined. Declare "
                    f"'{child}' at '{pg}' or provide '{parent}' at '{cg}'."
                )
            if is_finer(pg, cg):
                if pdefn.kind == "rate":
                    raise ValueError(
                        f"Metric '{child}' (grain '{cg}') has rate parent "
                        f"'{parent}' at finer grain '{pg}'. Rates cannot be "
                        f"aggregated automatically; declare '{parent}' at grain "
                        f"'{cg}', recomputed from its components at that grain."
                    )
                if not nests_in(pg, cg):
                    raise ValueError(
                        f"Metric '{child}' (grain '{cg}') has parent '{parent}' "
                        f"at grain '{pg}', which does not nest in '{cg}' (weeks "
                        f"straddle month boundaries). Declare '{parent}' at "
                        f"'{cg}' instead."
                    )

    def get_metric(self, name: str) -> Optional[MetricDefinition]:
        if name not in self.dag:
            return None
        return self.dag.nodes[name]["definition"]

    def get_topological_order(self) -> List[str]:
        return list(nx.topological_sort(self.dag))

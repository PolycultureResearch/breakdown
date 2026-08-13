import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

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
    type: str = "mock"  # mock | local | cloud | dbt | warehouse | none (alias "assumed")
    project_path: Optional[str] = None
    # `dbt` provider: which profiles.yml target to use, and where to find it.
    # Both default to what dbt itself would pick for the project.
    target: Optional[str] = None
    profiles_dir: Optional[str] = None
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
        if v not in ["mock", "local", "cloud", "dbt", "warehouse", "none"]:
            raise ValueError("type must be one of: mock, local, cloud, dbt, warehouse, none")
        return v

    @field_validator(
        "project_path",
        "target",
        "profiles_dir",
        "environment_id",
        "host",
        "token",
        "http_path",
        "profile",
        "catalog",
        "db_schema",
        mode="after",
    )
    @classmethod
    def expand_env_vars(cls, v: Optional[str]) -> Optional[str]:
        return _expand_env(v)


class GoalSpec(BaseModel):
    """A target a tree is being held to. Optional, like everything in `tree:`.

    Any tree may declare one — a five-year platform tree as readily as a
    quarterly push — and most trees won't. `metric` names a metric in this tree
    and is the one field here that can be wrong in a way the author cannot see,
    so `Parser._validate_goal` rejects a dangling name rather than leaving a
    silently dead card on the index. `direction` says which way is winning and
    defaults from the named metric's own `direction` (the tree already has the
    concept); `deadline` is optional too — a target with no date is a perfectly
    ordinary thing to be held to.
    """

    metric: str
    target: Optional[float] = None
    direction: Optional[str] = None
    deadline: Optional[str] = None

    @field_validator("direction")
    @classmethod
    def check_direction(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("up", "down"):
            raise ValueError(f"goal direction must be 'up' or 'down', got '{v}'")
        return v

    @field_validator("deadline")
    @classmethod
    def check_deadline(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            datetime.date.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError(f"goal deadline must be a YYYY-MM-DD date, got '{v}'")
        return v


class TreeMeta(BaseModel):
    """The optional `tree:` block — a tree's identity as a document.

    **Every field is optional, including the block itself.** A process serves
    several trees and they are peers: a wide tree with revenue at the top, a
    marketing tree detailing channels and campaigns, a product tree about
    feature adoption, a tree standing behind a target. Nothing here says how
    long a tree should live or that it owes anyone a goal — a tree can declare
    only `title`, or nothing at all and take its identity from its filename.
    `id` is never declared here: it is always the filename stem, so two files
    can't claim one id.

    `period` is a free-form label the card shows ("2026-Q3", "FY27",
    "2026-2031"), deliberately unparsed; `goal.deadline` is the parsed date.
    """

    title: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    period: Optional[str] = None
    goal: Optional[GoalSpec] = None


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

# Names that read like a ratio: conversion rates, CTRs, shares, averages,
# durations, and per-unit intensities. Used only for the `kind` lint below —
# it is a naming heuristic, so it warns and never rejects.
_RATE_SHAPED_NAME = re.compile(
    r"(^|_)(rate|ratio|pct|percent|share|ctr)(_|$)"
    r"|^(average|avg|mean|median)_"
    r"|^time_to_"
    r"|_per_"
)


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


class BindingDimension(BaseModel):
    """One sliceable dimension reachable from a binding's relation.

    Either a column on the relation itself (`{column: region}`) or a column on
    a conformed dimension table reached by a **many-to-one** join
    (`{join: dim_customers, key: customer_id, column: region}`). Many-to-one is
    the only join shape the contract admits: fan-out is definitionally
    impossible on it, so symmetric aggregates are never required. Anything
    else — chasm traps, bridge tables, multi-hop — belongs in an inline `sql:`
    relation, where the author owns the grain and the warehouse owns the
    testing.
    """

    column: str
    # The dimension relation to join, and the foreign key on the fact side.
    # `key` names the column on *this* binding's relation; the dimension table
    # is matched on its own column of the same name unless `to` says otherwise.
    #
    # `key`, not `on`: PyYAML is YAML 1.1, where a bare `on:` parses as the
    # boolean True, so an `on` field would silently drop the join key from
    # every unquoted declaration and leave the join unprovable. Pinned by
    # test_join_key_is_not_named_on_because_yaml_would_eat_it.
    join: Optional[str] = None
    key: Optional[str] = None
    to: Optional[str] = None

    @model_validator(mode="after")
    def check_join(self) -> "BindingDimension":
        if self.join is None and (self.key is not None or self.to is not None):
            raise ValueError(
                "Binding dimension declares `key`/`to` without `join`; drop them "
                "if the column is on the relation itself."
            )
        if self.join is not None and self.key is None:
            raise ValueError(
                f"Binding dimension joins '{self.join}' but declares no `key` "
                "(the foreign-key column on this binding's relation). A join "
                "with no key cannot be proven many-to-one."
            )
        return self


class EntityGrainSpec(BaseModel):
    """How to resolve a non-additive metric to one row per (entity, period),
    where it becomes an exact sum over slices (roadmap 3.8 §4).

    A `count_distinct` metric's slices overstate the total whenever an entity
    holds several values of a dimension inside a period — a subscription
    `active` in the morning and `cancelled` by evening is counted once in the
    metric and once in each status. Picking one value per entity per period
    makes `Σ_g slices == metric` hold exactly, and the existing sum attribution
    then runs unchanged with a zero residual for a real reason.

    `resolve` has **no default on purpose**. `first` and `last` answer different
    business questions — *what state did they arrive in* versus *what state did
    they end in* — and which is right is not ours to guess. `error` is for an
    author who believes their data is already single-valued and wants that
    enforced rather than silently corrected.
    """

    # Defaults to the binding's own relation; declared separately only when the
    # entity-grain table is a different one.
    relation: Optional[str] = None
    resolve: str

    @field_validator("resolve")
    @classmethod
    def check_resolve(cls, v: str) -> str:
        if v not in ENTITY_RESOLUTIONS:
            raise ValueError(
                f"entity_grain.resolve must be one of {list(ENTITY_RESOLUTIONS)}, got '{v}'"
            )
        return v


# How a multi-valued entity collapses to one row per period. `error` resolves
# nothing — it asserts the data is already single-valued, which `doctor` checks.
ENTITY_RESOLUTIONS = ("first", "last", "error")

# Aggregations the binding contract admits. Deliberately small: each one has a
# defined decomposition (see semantic_layer_connectivity_design.md §7), and a
# metric whose aggregation is not on this list must supply an inline `sql:`
# relation that reduces it to one that is.
BINDING_AGGS = ("sum", "count", "count_distinct", "ratio", "average", "last")

# Aggregations whose slices do not sum, so the engine may never auto-aggregate
# them across time (`resample_up` sums flows — summing daily distinct users is
# not monthly distinct users).
NON_ADDITIVE_AGGS = ("count_distinct",)


class BindingSpec(BaseModel):
    """How one node gets one series: a **fetch descriptor**, not a semantic
    layer (semantic_layer_connectivity_design.md §4).

    The boundary test for every field here is whether it describes fetching a
    single series for a single node, rather than shared org-wide semantics.
    Reusable dimension groups, metric-on-metric references (that is `formula` —
    the DAG, which is already ours), joins beyond fact -> conformed dimension,
    governance and caching are all permanently out of scope, and the escape
    hatch absorbs the pressure: **`sql:` answers every feature request first.**

    Note `bind.sql` and the legacy top-level `MetricDefinition.sql` are
    different contracts and are mutually exclusive. `MetricDefinition.sql` is
    the `warehouse` provider's finished series (`SELECT ... -> date, value`);
    `bind.sql` is a *relation* standing in for a table — one row per
    `grain_key` — which the binding then aggregates. Naming them alike is
    deliberate (both are "write SQL instead"), but confusing them silently
    produces a wrong shape, so declaring both is rejected.
    """

    # Exactly one of these: a table reference, or an inline relation.
    relation: Optional[str] = None
    sql: Optional[str] = None

    # The column making the relation one row per grain. Load-bearing: `doctor`
    # asserts count(*) == count(distinct grain_key) at bind time, which turns
    # silent fan-out into a startup error. MetricFlow and Cube cannot do this —
    # they accept declared relationships on trust.
    grain_key: str
    time_column: str

    agg: str
    # The aggregated column/expression, for every agg except `ratio`.
    measure: Optional[str] = None
    # Required for `ratio`, as separate measures — this is what makes a ratio
    # decomposable into within-slice movement, denominator mix shift, and the
    # cross term. Without them a ratio can only be approximated, and applying
    # additive attribution to a ratio produces a confidently wrong root cause.
    numerator: Optional[str] = None
    denominator: Optional[str] = None
    # The entity a non-additive metric counts. Decomposition happens at the
    # grain where the metric becomes a sum (§8), which requires knowing what
    # the distinct thing *is*.
    entity_key: Optional[str] = None
    # Resolution to one row per (entity, period). Present means slices are
    # expected to sum exactly; absent means they may overlap.
    entity_grain: Optional[EntityGrainSpec] = None

    dimensions: Dict[str, BindingDimension] = Field(default_factory=dict)

    # Row-level predicates, ANDed, over columns of **this binding's own
    # relation**, unqualified (roadmap 2.17). Empty and absent mean the same
    # thing: no filter.
    #
    # **Import-only.** This is the one field on `BindingSpec` a tree author may
    # not write, and `MetricDefinition.check_where_is_import_only` is what
    # enforces it. `bind.sql` already expresses every hand-written filter, and
    # §4.1's stop rule admits a new field only when `sql:` genuinely cannot
    # express the thing — a shorter spelling is convenience, which is what the
    # rule forbids. The importer has no `sql:` escape hatch (composing a SELECT
    # around a manifest predicate means rendering its Jinja anyway, and then
    # hiding the predicate inside a subquery where `doctor` cannot see it), so
    # the boundary moved from *which fields exist* to *which fields an author
    # may write*. An import-only field must be fully derivable from the source
    # artifact with no information from the author; the moment one needs a hint
    # or an override it is an authoring field again.
    #
    # A list rather than a joined string, because dbt's `WhereFilterIntersection`
    # is an AND of separate predicates and keeping the structure lets a skip
    # reason, a `doctor` line or a *show query* panel quote the one predicate
    # that matters. Unqualified, because the fact alias is a `dbt_sql` concept
    # and five builders alias differently — qualification happens at build time.
    where: List[str] = Field(default_factory=list)

    @field_validator("agg")
    @classmethod
    def check_agg(cls, v: str) -> str:
        if v not in BINDING_AGGS:
            raise ValueError(f"binding agg must be one of {list(BINDING_AGGS)}, got '{v}'")
        return v

    @field_validator("relation")
    @classmethod
    def check_relation_is_a_reference(cls, v: Optional[str]) -> Optional[str]:
        # A relation is a table reference, not a query. Catching this here is
        # worth a validator because the failure is otherwise a warehouse syntax
        # error thrown from inside a generated query at startup.
        if v is not None and (";" in v or any(c.isspace() for c in v)):
            raise ValueError(
                f"binding relation '{v}' looks like SQL rather than a table "
                "reference; use `sql:` for an inline relation."
            )
        return v

    @model_validator(mode="after")
    def check_source(self) -> "BindingSpec":
        if (self.relation is None) == (self.sql is None):
            raise ValueError(
                "A binding needs exactly one of `relation` (a table reference) "
                "or `sql` (an inline relation producing one row per grain_key)."
            )
        return self

    @model_validator(mode="after")
    def check_measures(self) -> "BindingSpec":
        if self.agg == "ratio":
            missing = [f for f in ("numerator", "denominator") if getattr(self, f) is None]
            if missing:
                raise ValueError(
                    f"A `ratio` binding needs {' and '.join(missing)}. A ratio "
                    "must be fetched as two separate measures so its change can "
                    "be split into within-slice movement and mix shift; "
                    "attributing a pre-divided ratio additively reports a "
                    "confidently wrong cause."
                )
            if self.measure is not None:
                raise ValueError(
                    "A `ratio` binding declares `numerator` and `denominator`, not `measure`."
                )
        else:
            if self.measure is None:
                raise ValueError(
                    f"A `{self.agg}` binding needs a `measure` (the column or "
                    "expression it aggregates)."
                )
            offenders = [f for f in ("numerator", "denominator") if getattr(self, f) is not None]
            if offenders:
                raise ValueError(
                    f"`{'`/`'.join(offenders)}` only apply to `agg: ratio`, not '{self.agg}'."
                )
        return self

    @model_validator(mode="after")
    def check_entity_key(self) -> "BindingSpec":
        if self.agg == "count_distinct" and self.entity_key is None:
            raise ValueError(
                "A `count_distinct` binding needs an `entity_key`. Its slices "
                "genuinely do not sum — one entity appears in several — so "
                "decomposition has to happen at the grain where the metric "
                "becomes a sum, and that needs to know what the entity is."
            )
        if self.agg == "last" and self.dimensions and self.entity_key is None:
            raise ValueError(
                "A sliced `last` binding needs an `entity_key`. A stock is "
                "additive across dimensions but not across time, so attributing "
                "its change across slices requires stock-and-flow at the entity "
                "grain. Drop the dimensions to serve trend only."
            )
        return self

    @model_validator(mode="after")
    def check_entity_grain(self) -> "BindingSpec":
        if self.entity_grain is None:
            return self
        if self.entity_key is None:
            raise ValueError(
                "`entity_grain` needs an `entity_key`: resolving to one row per "
                "entity per period requires knowing what the entity is."
            )
        if self.agg == "count_distinct" and self.measure != self.entity_key:
            # Resolving to entity grain makes `Σ_g slices` the distinct entity
            # count. If the metric counts something else, that sum is not the
            # metric and the reconciliation it promises would be false.
            raise ValueError(
                f"`entity_grain` on a `count_distinct` binding requires "
                f"`entity_key` to be the counted column: this counts "
                f"'{self.measure}' but resolves entity '{self.entity_key}'. "
                "Resolving a different entity would make the slices sum to a "
                "number that is not this metric."
            )
        return self

    @property
    def resolves_to_entity_grain(self) -> bool:
        """A resolution actually runs, collapsing each entity to one slice per
        period — so the slices are made to sum.

        `resolve: error` is deliberately excluded. It *asserts* the data is
        already single-valued rather than correcting it, and an assertion is
        not a resolution: nothing in the query makes it true. Treating the two
        alike claimed `additivity: exact` while serving the naive, possibly
        overlapping query — a false label on exactly the number this feature
        exists to make trustworthy.
        """
        return self.entity_grain is not None and self.entity_grain.resolve != "error"

    @property
    def asserts_entity_grain(self) -> bool:
        """The author declared `resolve: error` — single-valuedness is claimed
        but unverified at query time. `doctor` is what checks it."""
        return self.entity_grain is not None and self.entity_grain.resolve == "error"

    @model_validator(mode="after")
    def check_dimension_names(self) -> "BindingSpec":
        for key in self.dimensions:
            if not _IDENTIFIER.match(key):
                raise ValueError(
                    f"Binding dimension name '{key}' must be an identifier "
                    "(letters, digits, underscores; not starting with a digit)."
                )
        return self

    @property
    def is_non_additive(self) -> bool:
        """Slices do not sum, so the series may never be auto-aggregated across
        time. Consulted by the grain rules in `Parser._validate_grains`."""
        return self.agg in NON_ADDITIVE_AGGS


class MetricFormat(BaseModel):
    """How a metric's big number is displayed on its node card. Presentation
    only — never affects modeling. Written in YAML either as the shorthand
    `format: currency` or as a mapping with any of these keys."""

    style: str = "number"  # "currency" | "percent" | "number"
    unit: Optional[str] = None  # small caption under the value, e.g. "sessions", "ms"
    decimals: Optional[int] = None  # fixed fraction digits; None = automatic
    compact: Optional[bool] = None  # k/M/B notation; None = auto (currency compacts large values)
    symbol: str = "$"  # currency symbol, when style == "currency"

    @field_validator("style")
    @classmethod
    def check_style(cls, v: str) -> str:
        if v not in ("currency", "percent", "number"):
            raise ValueError(f"format.style must be 'currency', 'percent', or 'number', got '{v}'")
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
    # How this node gets its series, when it does not inherit the tree-level
    # `provider`. A node is *bound* (this is set, or the tree provider serves
    # it) or *unbound* (priors only). Mutually exclusive with the legacy
    # top-level `sql` — see BindingSpec's docstring for why they differ.
    bind: Optional[BindingSpec] = None
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
    # attribution): "up_is_good" (growth metrics), "down_is_good" (costs,
    # tickets, time-to-X), or "neutral" (no judgment, gray). Note a
    # stored-negative flow like churn_mrr is up_is_good: moving toward zero
    # means less churn.
    #
    # `None` — the default — means the author did not say, and that is a
    # *different* statement from "up is good". It used to default to
    # `up_is_good`, and since `/dag` serializes with `model_dump()` the default
    # reached the browser indistinguishable from a declaration: an undeclared
    # metric rendered green for "improved" on a rise, which on `churn_arpu`
    # (rising cost per churned subscription, contributing 27% of the damage)
    # is a confident wrong claim. The UI's own `|| "up_is_good"` fallback could
    # never fire, because the payload was never missing the field. Undeclared
    # has to survive serialization for any renderer to be able to decline to
    # judge, so it does: the field is null, and the UI paints such a metric
    # like `neutral` — arrow and sign, no good/bad colour.
    direction: Optional[str] = None

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
    def check_direction(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("up_is_good", "down_is_good", "neutral"):
            raise ValueError(
                f"direction must be one of ['up_is_good', 'down_is_good', 'neutral'], got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def check_unique_parents(self) -> "MetricDefinition":
        # Parent order is the axis order of `beta`/`beta_raw`, and the DAG holds
        # one edge per (parent, child) — so a name listed twice makes
        # `defn.parents` one entry longer than `list(dag.predecessors(name))`.
        # Everything that reads a coefficient positionally off the definition
        # (the node card's per-parent `beta_raw[i]`, for one) then reads the
        # wrong axis for every parent after the repeat.
        seen: Dict[str, int] = {}
        for parent in self.parents:
            seen[parent] = seen.get(parent, 0) + 1
        repeated = ", ".join(f"'{p}' {n} times" for p, n in seen.items() if n > 1)
        if repeated:
            raise ValueError(
                f"Metric '{self.name}' lists parent {repeated} in {self.parents}. "
                "Parent order is the axis order of the learned coefficients, and "
                "the DAG keeps one edge per parent, so a repeat shifts every "
                "later parent's coefficient by one place. List each parent once."
            )
        return self

    @model_validator(mode="after")
    def check_unique_seasonality_names(self) -> "MetricDefinition":
        # Each entry's name becomes the name of its Fourier coefficients in the
        # model (`sin_<name>_h1`, `cos_<name>_h1`, …). Two entries sharing one
        # collide there, and the sampler's complaint arrives at fit time naming
        # a variable the author never wrote.
        seen: Dict[str, int] = {}
        for s in self.seasonality:
            seen[s.name] = seen.get(s.name, 0) + 1
        repeated = sorted(name for name, n in seen.items() if n > 1)
        if repeated:
            raise ValueError(
                f"Metric '{self.name}' declares the seasonality name "
                f"{', '.join(repr(n) for n in repeated)} more than once. Each "
                "entry's name names its own Fourier coefficients "
                "(`sin_<name>_h1`, `cos_<name>_h1`), so two entries sharing a "
                "name collide in the model and the fit fails with a variable-name "
                "error that never mentions the tree. Name each component "
                "distinctly (e.g. 'weekly' and 'yearly')."
            )
        return self

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
                        self.name,
                        s.period,
                        self.grain,
                        s.period,
                        self.grain,
                    )
        return self

    @model_validator(mode="after")
    def warn_rate_shaped_name_inheriting_flow(self) -> "MetricDefinition":
        # `kind` defaults to `flow`, which is silently wrong for a ratio: flows
        # sum when resampled, and the sum of a month of daily conversion rates
        # is meaningless. The default is also invisible — a tree that declares
        # nothing looks deliberate — so the only signal that a rate was never
        # classified is its name.
        #
        # Deliberately a warning, not an error. This is a naming heuristic and
        # a legitimate flow can match it (`orders_per_day` really does sum), so
        # refusing to parse would be worse than the bug. Writing `kind: flow`
        # explicitly is the escape hatch: the check only fires when `kind` was
        # left at its default, which is exactly the "never thought about it"
        # case it exists to catch.
        if "kind" not in self.model_fields_set and _RATE_SHAPED_NAME.search(self.name):
            logger.warning(
                "Metric '%s' has a ratio-shaped name but no `kind`, so it "
                "inherits `kind: flow` — it will be SUMMED when resampled to a "
                "coarser grain, and the sum of a month of daily ratios is not "
                "that month's ratio. Declare `kind: rate` (or `kind: flow` "
                "explicitly if it really is additive).",
                self.name,
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
                key: ({"source": val} if isinstance(val, str) else val) for key, val in v.items()
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
    def check_bind(self) -> "MetricDefinition":
        if self.bind is None:
            return self
        if self.bind.where:
            # `where` is import-only (roadmap 2.17). The discriminator is
            # structural and needs no cleverness: manifest bindings live in
            # `BridgeResult.bindings` and never pass through YAML, so a `where`
            # arriving on a `MetricDefinition` was written by hand.
            raise ValueError(
                f"Metric '{self.name}' declares `bind.where`, which is populated "
                "by the dbt importer and cannot be written by hand. `bind.sql` "
                "already expresses every hand-written filter — and unlike a "
                "`where:` key it is dialect-explicit, owned by you, and still "
                "checked by `breakdown doctor`'s grain claim:\n"
                "    bind:\n"
                "      sql: SELECT * FROM analytics.fct_orders WHERE is_food_order\n"
                "      grain_key: order_id\n"
                "      ..."
            )
        if self.sql is not None:
            raise ValueError(
                f"Metric '{self.name}' declares both `sql` and `bind`. They are "
                "two different contracts: top-level `sql` returns the finished "
                "series (`date, value`) for the warehouse provider, while "
                "`bind.sql` is a relation the binding then aggregates. Keep one."
            )
        # A ratio is never a flow: it cannot be summed over time, which is
        # exactly what `kind: flow` licenses `resample_up` to do.
        if self.bind.agg == "ratio" and self.kind != "rate":
            raise ValueError(
                f"Metric '{self.name}' binds `agg: ratio` but declares "
                f"`kind: {self.kind}`. A ratio is a rate — declare `kind: rate` "
                "so it is never summed or gap-filled as though it were a flow."
            )
        if self.kind == "rate" and self.bind.agg == "sum":
            raise ValueError(
                f"Rate metric '{self.name}' binds `agg: sum`. Summing a rate is "
                "definitionally wrong; use `agg: ratio` with a `numerator` and "
                "`denominator`, or recompute the rate in a `bind.sql` relation."
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
    # The tree's identity as a document (title/owner/period/goal). Optional in
    # both directions: a tree with no `tree:` block is exactly as valid as it
    # was before the block existed, and because this model ignores extra keys,
    # a tree carrying one already loads on builds that predate it.
    tree: Optional[TreeMeta] = None
    provider: DataProviderConfig = Field(default_factory=DataProviderConfig)
    metrics: List[MetricDefinition]

    @model_validator(mode="after")
    def check_unique_metric_names(self) -> "MetricTreeConfig":
        """A metric name is a node's identity in the DAG, so it must be unique
        (roadmap C6).

        Without this the two passes in `_build_dag` merge the definitions
        instead of refusing them: the last one wins `nodes[name]["definition"]`
        while **both** definitions' edges are added, so
        `list(dag.predecessors(name))` — the axis order of `beta`/`beta_raw` —
        returns the union while `priors`, `lags` and `expected_signs` were
        validated against only the survivor's `parents`. Every declared prior
        then lands on the wrong axis or falls back to `Normal(0, 1)`, and the
        fetch loop overwrites one metric's series with the other's. Nothing
        raises and nothing warns.

        There is no reading under which the author meant that, so this refuses
        rather than picking a merge strategy. It runs before any DAG is built.
        """
        positions: Dict[str, List[int]] = {}
        for i, metric in enumerate(self.metrics, start=1):
            positions.setdefault(metric.name, []).append(i)
        duplicated = {name: at for name, at in positions.items() if len(at) > 1}
        if not duplicated:
            return self
        detail = "; ".join(
            f"'{name}' is defined {len(at)} times, at positions "
            f"{', '.join(str(i) for i in at)} of `metrics:` "
            f"(sources: {', '.join(repr(self.metrics[i - 1].source) for i in at)})"
            for name, at in duplicated.items()
        )
        raise ValueError(
            f"Duplicate metric name in this tree: {detail}. A metric's name is "
            "its identity in the DAG: two definitions sharing one would collapse "
            "into a single node carrying the union of both definitions' parents, "
            "while only the last definition's priors, lags and expected_signs "
            "are validated — so declared priors land on the wrong coefficient "
            "and one definition's data overwrites the other's. Rename one, or "
            "merge them into a single definition."
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    """`SafeLoader` that refuses duplicate keys in a mapping.

    PyYAML keeps the last of two identical keys and says nothing, which is the
    same defect as a duplicate metric name one level down: `priors:` declaring
    `signups:` twice silently drops one prior, and a repeated `dimensions:`
    entry silently drops a dimension. Both read as declared in the file.

    The check compares **constructed** keys, so it also catches YAML 1.1's
    trap: a bare `on:` resolves to the boolean `True` and therefore collides
    with a `true:` in the same mapping (see `BindingDimension.key`). When the
    two keys were written differently, the message says so.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> Dict[Any, Any]:
        seen: Dict[Any, Tuple[str, int]] = {}
        for key_node, _ in node.value:
            # Only scalars can be tree keys; `<<` is YAML's merge key, which is
            # legitimately repeatable.
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            line = key_node.start_mark.line + 1
            if key in seen:
                first_text, first_line = seen[key]
                aka = ""
                if first_text != key_node.value:
                    aka = (
                        f" ('{first_text}' and '{key_node.value}' are one key: "
                        f"YAML 1.1 resolves both to {key!r})"
                    )
                raise ValueError(
                    f"Duplicate key '{key_node.value}' at line {line}, already "
                    f"set at line {first_line}{aka}. YAML keeps only the last of "
                    "two identical keys, so the earlier one — a prior, a lag, a "
                    "dimension — would be dropped with no error at all. Delete "
                    "or rename one."
                )
            seen[key] = (key_node.value, line)
        return super().construct_mapping(node, deep=deep)


class Parser:
    def __init__(self, yaml_content: str):
        self.config = self._parse_yaml(yaml_content)
        self.dag = self._build_dag()

    def _parse_yaml(self, content: str) -> MetricTreeConfig:
        data = yaml.load(content, Loader=_UniqueKeyLoader)
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
                    raise ValueError(
                        f"Parent metric '{parent}' not found for metric '{metric.name}'"
                    )
                G.add_edge(parent, metric.name)

        if not nx.is_directed_acyclic_graph(G):
            cycles = list(nx.simple_cycles(G))
            raise ValueError(f"Metric tree contains cycles: {cycles}")

        self._validate_grains(G)
        self._validate_dimension_weights(G)
        self._validate_shapley_parents(G)
        self._validate_goal(G)
        return G

    @staticmethod
    def _validate_shapley_parents(G: nx.DiGraph) -> None:
        """A formula node's parents are enumerated in coalitions by
        `compute_shapley`, which refuses more than `_MAX_SHAPLEY_PARENTS` of
        them. That refusal is the chokepoint no direct library caller can walk
        around, but by the time it fires the author is minutes into an RCA — so
        the same limit is checked here, where a too-wide node surfaces at tree
        load and in `breakdown doctor`.

        The parents counted are `list(G.predecessors(name))`, the same call the
        engine makes, so the two can't disagree about the number.

        The constant is imported here rather than at module scope because
        `breakdown.engine.model` imports `MetricDefinition` from this module: a
        top-level import would be circular (verified — it raises ImportError on
        `import breakdown.parser`). A function-scope import costs nothing on the
        boot path either way — `engine.model` is already imported at module
        scope by `breakdown.api.main`, and it keeps `pymc`/`arviz` inside its
        own functions (roadmap C14), so reaching it from here loads no part of
        the inference stack.
        """
        from breakdown.engine.model import _MAX_SHAPLEY_PARENTS

        for name in G.nodes:
            defn = G.nodes[name]["definition"]
            if defn.formula is None:
                continue
            n = len(list(G.predecessors(name)))
            if n > _MAX_SHAPLEY_PARENTS:
                raise ValueError(
                    f"Formula node '{name}' has too many parents for exact "
                    f"Shapley attribution: {n} parents, at most "
                    f"{_MAX_SHAPLEY_PARENTS} are supported (the coalition "
                    "enumeration is O(2^n), so each extra parent doubles the "
                    "work). Split the node into intermediate sums — group some "
                    "parents under their own formula node and make that node the "
                    "parent here — which preserves the identity and keeps every "
                    "attribution exact."
                )

    def _validate_goal(self, G: nx.DiGraph) -> None:
        """Resolve `tree.goal` against the tree (needs the full node set).

        Two rules, both about a claim the author cannot check by eye:

        - `goal.metric` must name a metric here. A goal pointing at a metric
          that doesn't exist would show as a permanently blank card on the
          index, which reads as "no progress yet" rather than "typo".
        - `goal.direction` defaults from the named metric's own `direction`
          when the metric *declares* one — `direction` is null when the author
          did not say, so undeclared cannot masquerade as a declaration here or
          anywhere downstream. Declaring both and disagreeing is an error: one
          of the two is telling the reader the wrong thing about which way is
          winning, and guessing which would be worse than stopping.
        """
        meta = self.config.tree
        goal = meta.goal if meta else None
        if goal is None:
            return
        if goal.metric not in G:
            raise ValueError(
                f"tree.goal.metric '{goal.metric}' is not a metric in this tree "
                f"(known: {', '.join(sorted(G.nodes))})."
            )
        defn = G.nodes[goal.metric]["definition"]
        declared = defn.direction is not None
        implied = {"up_is_good": "up", "down_is_good": "down"}.get(defn.direction)
        if goal.direction is None:
            # `neutral` implies nothing, so a goal on a neutral metric must say
            # which way it is trying to move — there is no sane default.
            if declared and implied is None:
                raise ValueError(
                    f"tree.goal names metric '{goal.metric}', which declares "
                    "`direction: neutral`, so no goal direction can be inferred. "
                    "Declare `tree.goal.direction: up` or `down`."
                )
            goal.direction = implied if declared else "up"
        elif declared and implied is not None and goal.direction != implied:
            raise ValueError(
                f"tree.goal.direction is '{goal.direction}' but metric "
                f"'{goal.metric}' declares `direction: {defn.direction}`. "
                "They disagree about which way is winning; declare one or make "
                "them agree."
            )

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
        grain — flow/stock kinds whose grain nests in the child's, and never a
        binding whose aggregation is non-additive."""
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
                if pdefn.bind is not None and pdefn.bind.is_non_additive:
                    raise ValueError(
                        f"Metric '{child}' (grain '{cg}') has parent '{parent}' "
                        f"at finer grain '{pg}' bound with "
                        f"`agg: {pdefn.bind.agg}`, which is not re-aggregable: "
                        f"summing {pg}-grain distinct counts does not give the "
                        f"{cg}-grain distinct count. Declare '{parent}' at grain "
                        f"'{cg}' so it is counted once over the whole period."
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

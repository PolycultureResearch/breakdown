"""A world of a world set, as breakdown's aligned data for a tree.

How a world reaches the engine: a fetcher at the provider boundary
(`WorldFetcher`, a `BaseDataFetcher`) that answers each tree node from the
world's `series.parquet`, handed to the same `loading.fetch_all_metrics` the
server calls. So the data goes through the provider contract every other
source goes through (`_align_to_spine`: whole periods only, trailing gaps
trimmed, a rate's missing period left undefined), derived nodes are derived
by the engine's own code, and the identity checks run. No dbt, no warehouse,
no fake_companies checkout.

It lives here and not in `breakdown/` because it is not a general provider:
it reads one generator's file format, and its job is to serve an evaluation,
where "this node cannot be answered" is a result to report and not a
connection to fix.

**Rule 1 applies** (`AGENTS.md`): what the file cannot answer is refused by
name (`WorldCannotAnswer`), never substituted. A metric the file does not
hold, a dimension it was not exported by, a slice member with no rows, and a
grain whose value cannot be rebuilt from days (a mean over rows, a distinct
count) are each an error that says which.

The series file is daily. A node at a coarser grain is rebuilt from the days
by the rule the manifest gives for its metric (`metrics[].time_aggregation`):

- `sum`: add the days of each whole period;
- `end_of_period`: take the period's last day (a stock);
- a ratio: aggregate its numerator and its denominator, each by its own
  metric's rule, then divide. The manifest's `ratio_of_sums` is this with
  both sides summed; a ratio over a stock (churned / active subscriptions)
  takes the stock's last day, as the semantic layer does;
- `derived`: aggregate each input metric by its own rule, then evaluate the
  metric's expression;
- anything else (`average`, `count_distinct`, `rolling_window`, min, max):
  refused at a coarser grain, served as written at day grain.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from breakdown.data_fetch import BaseDataFetcher, _align_to_spine
from breakdown.formula import eval_formula
from breakdown.grains import GrainedData, resample_up
from breakdown.loading import fetch_all_metrics
from breakdown.parser import Parser

from .worldset import MetricSpec, World, WorldSet

logger = logging.getLogger(__name__)

SERIES_COLUMNS = (
    "date",
    "metric",
    "dimension",
    "value",
    "metric_value",
    "numerator",
    "denominator",
)

# `fetch_all_metrics` asks a provider type how to name a node. The world
# fetcher is addressed by node name, like every provider with no manifest of
# its own to reconcile a `source` against.
_NAME_KEYED = "duckdb"


class WorldCannotAnswer(RuntimeError):
    """The world's series file cannot serve what was asked. Never a fallback."""


class UnservedNodes(RuntimeError):
    """A tree has fetched nodes this world set cannot serve."""

    def __init__(self, unserved: dict[str, str]):
        self.unserved = unserved
        lines = "\n".join(f"  - {n}: {why}" for n, why in unserved.items())
        super().__init__(
            f"{len(unserved)} tree node(s) cannot be served from the world set:\n{lines}\n"
            "Add a binding for each, or pass --drop-unserved to run on the tree "
            "without them (recorded on the scoreboard as a change to the tree)."
        )


@dataclass(frozen=True)
class SeriesBinding:
    """How one tree node is read from the series file.

    The default is the metric the node's `source` names, total. A bindings
    file can instead name a metric and members of one of its dimensions (the
    node is their sum, or for a ratio the ratio of their summed sides), or a
    running total of a summed metric (a stock the generator does not export).
    """

    metric: str
    dimension: str | None = None
    values: tuple[str, ...] = ()
    cumulative: bool = False

    def describe(self) -> str:
        out = self.metric
        if self.dimension:
            out += f" [{self.dimension} in {', '.join(self.values)}]"
        if self.cumulative:
            out += " (running total)"
        return out


def load_bindings(path: str | Path | None) -> dict[str, SeriesBinding]:
    """Node name -> binding, from a YAML file of overrides (or none)."""
    if path is None:
        return {}
    raw = yaml.safe_load(Path(path).read_text()) or {}
    out = {}
    for node, spec in raw.items():
        unknown = set(spec) - {"metric", "dimension", "values", "cumulative"}
        if unknown or "metric" not in spec:
            raise ValueError(
                f"{path}: binding for '{node}' needs `metric` and may give `dimension`, "
                f"`values`, `cumulative`; got {sorted(spec)}"
            )
        if bool(spec.get("dimension")) != bool(spec.get("values")):
            raise ValueError(f"{path}: binding for '{node}' gives one of `dimension`/`values`")
        out[node] = SeriesBinding(
            metric=spec["metric"],
            dimension=spec.get("dimension"),
            values=tuple(str(v) for v in spec.get("values") or ()),
            cumulative=bool(spec.get("cumulative", False)),
        )
    return out


class WorldSeries:
    """One world's `series.parquet`, indexed for lookup."""

    def __init__(self, world: World, specs: dict[str, MetricSpec]):
        if world.series is None:
            raise WorldCannotAnswer(
                f"world '{world.id}' has no `series` in the manifest: the set was built "
                "without metric series (rebuild it with a generator that writes them)"
            )
        frame = pd.read_parquet(world.series)
        missing = [c for c in SERIES_COLUMNS if c not in frame.columns]
        if missing:
            raise WorldCannotAnswer(
                f"{world.series} lacks column(s) {missing}; it has {list(frame.columns)}"
            )
        frame["date"] = pd.to_datetime(frame["date"])
        self.world_id = world.id
        self.specs = specs
        self.start = frame["date"].min()
        self.end = frame["date"].max()
        self._totals = {
            m: g.set_index("date").sort_index()
            for m, g in frame[frame["dimension"].isna()].groupby("metric")
        }
        self._slices = {
            key: g.set_index("date").sort_index()
            for key, g in frame[frame["dimension"].notna()].groupby(
                ["metric", "dimension", "value"]
            )
        }
        self._dimensions: dict[str, set[str]] = {}
        for metric, dimension, _ in self._slices:
            self._dimensions.setdefault(metric, set()).add(dimension)

    def spec(self, metric: str) -> MetricSpec:
        if metric not in self._totals:
            raise WorldCannotAnswer(
                f"metric '{metric}' is not in the series file of world '{self.world_id}'"
            )
        if metric not in self.specs:
            raise WorldCannotAnswer(
                f"metric '{metric}' is in the series file but not in the manifest's "
                "`metrics`, so how it aggregates over time is not stated"
            )
        return self.specs[metric]

    def daily(self, binding: SeriesBinding) -> pd.DataFrame:
        """`metric_value`, `numerator`, `denominator` by day for a binding."""
        spec = self.spec(binding.metric)
        cols = ["metric_value", "numerator", "denominator"]
        if binding.dimension is None:
            return self._totals[binding.metric][cols]
        if binding.dimension not in self._dimensions.get(binding.metric, ()):
            have = sorted(self._dimensions.get(binding.metric, ()))
            raise WorldCannotAnswer(
                f"metric '{binding.metric}' was not exported by dimension "
                f"'{binding.dimension}' (exported by: {have or 'none'})"
            )
        parts = []
        for value in binding.values:
            key = (binding.metric, binding.dimension, value)
            if key not in self._slices:
                raise WorldCannotAnswer(
                    f"metric '{binding.metric}' has no rows for {binding.dimension}='{value}'"
                )
            parts.append(self._slices[key][cols])
        if len(parts) == 1:
            return parts[0]
        if spec.type == "ratio":
            total = sum(p[["numerator", "denominator"]].fillna(0.0) for p in parts)
            with np.errstate(divide="ignore", invalid="ignore"):
                total["metric_value"] = total["numerator"] / total["denominator"]
            return total[cols]
        if spec.time_aggregation != "sum":
            raise WorldCannotAnswer(
                f"metric '{binding.metric}' aggregates as '{spec.time_aggregation}', so "
                f"{len(parts)} members of '{binding.dimension}' cannot be added together"
            )
        total = sum(p[["metric_value"]].fillna(0.0) for p in parts)
        return total.assign(numerator=np.nan, denominator=np.nan)[cols]

    # -- day -> the node's grain ---------------------------------------------
    def _combine(self, days: pd.Series, how: str, grain: str, label: str) -> pd.Series:
        if how == "sum":
            return resample_up(days.fillna(0.0), "day", grain, "flow", label=label)
        if how == "end_of_period":
            return resample_up(days, "day", grain, "stock", label=label)
        raise WorldCannotAnswer(
            f"{label} aggregates over time as '{how}', which cannot be rebuilt from daily "
            f"values at grain '{grain}'"
        )

    def _rule(self, metric: str | None, default: str = "sum") -> str:
        return self.specs[metric].time_aggregation if metric in self.specs else default

    def at_grain(self, binding: SeriesBinding, grain: str) -> pd.Series:
        """The binding's series at `grain`, indexed by period start. Undefined
        periods are absent, as a source that returned no row for them."""
        spec = self.spec(binding.metric)
        label = f"'{binding.describe()}'"
        days = self.daily(binding)
        if binding.cumulative:
            if spec.time_aggregation != "sum":
                raise WorldCannotAnswer(
                    f"{label}: a running total needs a metric that sums, and "
                    f"'{binding.metric}' aggregates as '{spec.time_aggregation}'"
                )
            out = days["metric_value"].fillna(0.0).cumsum()
            if grain != "day":
                out = resample_up(out, "day", grain, "stock", label=label)
        elif grain == "day":
            out = days["metric_value"]
        elif spec.type == "ratio":
            num = self._combine(
                days["numerator"], self._rule(spec.inputs.get("numerator")), grain, label
            )
            den = self._combine(
                days["denominator"], self._rule(spec.inputs.get("denominator")), grain, label
            )
            with np.errstate(divide="ignore", invalid="ignore"):
                out = num / den
        elif spec.time_aggregation == "derived":
            if binding.dimension is not None:
                raise WorldCannotAnswer(
                    f"{label} is a derived metric; a slice of it cannot be rebuilt at grain "
                    f"'{grain}' because its inputs are not exported by the same dimension"
                )
            out = self._derived(spec, grain)
        else:
            out = self._combine(days["metric_value"], spec.time_aggregation, grain, label)
        out = out.astype(float)
        return out[np.isfinite(out)]

    def _derived(self, spec: MetricSpec, grain: str) -> pd.Series:
        if not spec.expr or not spec.inputs:
            raise WorldCannotAnswer(
                f"derived metric '{spec.name}' has no `expr`/`inputs` in the manifest, so it "
                f"cannot be rebuilt at grain '{grain}'"
            )
        inputs = pd.DataFrame(
            {
                alias: self.at_grain(SeriesBinding(metric), grain)
                for alias, metric in spec.inputs.items()
            }
        )
        # `x / nullif(y, 0)` is the semantic layer's way of leaving a zero
        # denominator undefined; dividing and dropping the non-finite result
        # (in `at_grain`) says the same thing.
        expr = re.sub(r"nullif\(\s*(\w+)\s*,\s*0\s*\)", r"\1", spec.expr, flags=re.IGNORECASE)
        try:
            with np.errstate(divide="ignore", invalid="ignore"):
                values = eval_formula(expr, {a: inputs[a].to_numpy(float) for a in inputs})
        except ValueError as exc:
            raise WorldCannotAnswer(
                f"derived metric '{spec.name}' has an expression this loader cannot "
                f"evaluate ({spec.expr!r}): {exc}"
            ) from exc
        return pd.Series(np.asarray(values, dtype=float), index=inputs.index)


def effective_rule(spec: MetricSpec, binding: SeriesBinding) -> str:
    if binding.cumulative:
        return "end_of_period"
    return "ratio" if spec.type == "ratio" else spec.time_aggregation


def kind_refusal(kind: str, spec: MetricSpec, binding: SeriesBinding) -> str | None:
    """Why a tree node's `kind` contradicts how the generator says its metric
    aggregates, or None. The engine resamples a parent by its kind, so a stock
    the tree calls a flow would be summed across a week."""
    rule = effective_rule(spec, binding)
    if (kind == "stock") != (rule == "end_of_period"):
        return (
            f"the tree declares kind '{kind}' but the generator aggregates "
            f"'{binding.describe()}' as '{rule}'"
        )
    if kind == "rate" and rule == "sum":
        return f"the tree declares a rate but '{binding.describe()}' is a sum"
    return None


class WorldFetcher(BaseDataFetcher):
    """Serves tree nodes from one world's series file."""

    def __init__(self, series: WorldSeries, bindings: dict[str, SeriesBinding]):
        self.series = series
        self.bindings = bindings

    def fetch_metric(self, metric_name, start_date, end_date, grain="day", kind="flow", **kw):
        if metric_name not in self.bindings:
            raise WorldCannotAnswer(f"node '{metric_name}' has no binding to the series file")
        binding = self.bindings[metric_name]
        refusal = kind_refusal(kind, self.series.spec(binding.metric), binding)
        if refusal:
            raise WorldCannotAnswer(f"node '{metric_name}': {refusal}")
        values = self.series.at_grain(binding, grain)
        frame = pd.DataFrame({"date": values.index, metric_name: values.to_numpy()})
        return _align_to_spine(
            frame, metric_name, grain, kind, start_date, end_date, metric_name, **kw
        )


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------
@dataclass
class TreeSpec:
    """A tree file as an evaluation input: fixed, hashed, never edited on disk."""

    path: str
    text: str
    bindings: dict[str, SeriesBinding] = field(default_factory=dict)
    bindings_path: str | None = None

    @classmethod
    def read(cls, path: str | Path, bindings_path: str | Path | None = None) -> "TreeSpec":
        return cls(
            path=str(path),
            text=Path(path).read_text(),
            bindings=load_bindings(bindings_path),
            bindings_path=None if bindings_path is None else str(bindings_path),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def bindings_sha256(self) -> str | None:
        if self.bindings_path is None:
            return None
        return hashlib.sha256(Path(self.bindings_path).read_bytes()).hexdigest()

    def raw(self) -> dict[str, Any]:
        """The tree as a dict, with the provider block replaced: the series
        file is the source, so the tree's own provider is never constructed
        (and its `bind:` blocks, which describe that provider's SQL, go too)."""
        raw = yaml.safe_load(self.text)
        raw["provider"] = {"type": "mock"}
        for m in raw["metrics"]:
            m.pop("bind", None)
        return raw

    def binding_for(self, metric: dict[str, Any]) -> SeriesBinding | None:
        if metric["name"] in self.bindings:
            return self.bindings[metric["name"]]
        if metric.get("source"):
            return SeriesBinding(metric["source"].split(".")[-1])
        return None


@dataclass
class TreePlan:
    """What a tree becomes on one world set: who serves each node, and what
    had to change for the rest. Recorded on the scoreboard."""

    served: dict[str, SeriesBinding]
    unserved: dict[str, str]  # node -> why
    dropped: list[str] = field(default_factory=list)  # removed from the tree
    derived_instead: list[str] = field(default_factory=list)  # formula nodes no longer fetched
    raw: dict[str, Any] = field(default_factory=dict)  # the tree actually parsed

    def summary(self) -> dict[str, Any]:
        return {
            "served": {n: b.describe() for n, b in self.served.items()},
            "unserved": dict(self.unserved),
            "dropped": list(self.dropped),
            "derived_instead_of_fetched": list(self.derived_instead),
        }


def plan_tree(tree: TreeSpec, series: WorldSeries, *, drop_unserved: bool = False) -> TreePlan:
    """Bind every fetched node of `tree` to the series file, by trying it.

    A node that cannot be served is reported with the file's own reason. With
    `drop_unserved`, the tree is run without it: a formula node keeps its
    place and is derived from its parents (it loses only the comparison
    against a fetched series); any other node is removed, along with its
    edges. A node some remaining formula needs cannot be removed, and that
    still refuses.
    """
    raw = tree.raw()
    served, unserved = {}, {}
    for m in raw["metrics"]:
        binding = tree.binding_for(m)
        if binding is None:
            continue  # derived: never fetched
        try:
            refusal = kind_refusal(m.get("kind", "flow"), series.spec(binding.metric), binding)
            if refusal:
                raise WorldCannotAnswer(refusal)
            if series.at_grain(binding, m.get("grain", "day")).empty:
                raise WorldCannotAnswer(f"'{binding.describe()}' has no defined period")
            served[m["name"]] = binding
        except (WorldCannotAnswer, ValueError) as exc:
            unserved[m["name"]] = str(exc)
    plan = TreePlan(served=served, unserved=unserved, raw=raw)
    if not unserved:
        return plan
    if not drop_unserved:
        raise UnservedNodes(unserved)
    for m in raw["metrics"]:
        if m["name"] in unserved and m.get("formula"):
            m.pop("source", None)
            plan.derived_instead.append(m["name"])
    gone = [n for n in unserved if n not in plan.derived_instead]
    for m in raw["metrics"]:
        used = [p for p in m.get("parents", []) if p in gone]
        if used and m.get("formula") and m["name"] not in gone:
            raise UnservedNodes(
                {p: f"{unserved[p]} (and the formula of '{m['name']}' needs it)" for p in used}
            )
        if used:
            m["parents"] = [p for p in m["parents"] if p not in gone]
            for key in ("expected_signs", "lags", "priors"):
                if isinstance(m.get(key), dict):
                    m[key] = {p: v for p, v in m[key].items() if p not in gone}
                    if not m[key]:
                        m.pop(key)
            if not m["parents"]:
                m.pop("parents")
    raw["metrics"] = [m for m in raw["metrics"] if m["name"] not in gone]
    plan.dropped = gone
    return plan


@dataclass
class LoadedWorld:
    world: World
    parser: Parser
    data: GrainedData
    plan: TreePlan
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def dag(self):
        return self.parser.dag


def load_world(
    worldset: WorldSet, world: World, tree: TreeSpec, *, drop_unserved: bool = False
) -> LoadedWorld:
    """Tree + world -> the DAG and aligned data the engine fits on."""
    series = WorldSeries(world, worldset.metrics)
    plan = plan_tree(tree, series, drop_unserved=drop_unserved)
    parser = Parser(yaml.safe_dump(plan.raw, sort_keys=False))
    fetcher = WorldFetcher(series, plan.served)
    start, end = str(series.start.date()), str(series.end.date())
    data = fetch_all_metrics(parser, fetcher, _NAME_KEYED, start, end)
    return LoadedWorld(world, parser, data, plan, series.start, series.end)

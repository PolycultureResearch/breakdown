"""Read a fake_companies world set through its manifest.

The contract is `worlds.json`, written by the generator
(`fake_companies.worlds.WorldSetManifest`, documented in that repo's
`docs/eval_worlds_spec.md` and `docs/using.md`). breakdown reads it and
nothing else, and does not import fake_companies: this module is the whole of
what the runner knows about the format. Keys it does not need are ignored, so
the manifest can grow; a key it does need and cannot find is refused by name,
and so is a `schema_version` it was not written against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "worlds.json"
# What each version guarantees is the generator's spec §10.4. Version 2 is
# what this runner is written against; a version 1 set (every set built
# before 2026-11-20, including sets that carry part of version 2 and cannot
# say which `moved` rule built them) is refused up front unless the caller
# asks for it by name, and then the contradiction checks in the loader are
# the only guard.
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSIONS = (1,)
STRICT_VERDICT = "verdict"
WORLD_KINDS = ("null", "single", "story")


class WorldSetError(ValueError):
    """The manifest cannot be read as the contract this runner was written for."""


@dataclass(frozen=True)
class MetricSpec:
    """One metric of the series files, and how its days combine into a period."""

    name: str
    type: str  # simple | ratio | derived | cumulative
    # sum | end_of_period | ratio_of_sums | ratio_of_aggregates | average |
    # derived | count_distinct | rolling_window
    time_aggregation: str
    # ratio: {"numerator": m, "denominator": m}; derived: alias -> metric.
    inputs: dict[str, str] = field(default_factory=dict)
    # Each key of `inputs` -> that input's own time_aggregation. Absent from
    # sets built before the generator stated it (then a ratio or a derived
    # metric is refused at a coarser grain, never guessed).
    input_time_aggregation: dict[str, str] = field(default_factory=dict)
    expr: str | None = None  # derived: SQL text, kept for messages only
    formula: dict | None = None  # derived: the expression as a tree
    measure_agg: str | None = None  # the measure's own aggregation
    has_sides: bool = False  # the series carry numerator/denominator
    has_weekly_series: bool = False  # the world's weekly file holds its week values
    # What MetricFlow answers at week grain, which is what breakdown's own
    # providers return for a week node (they ask the source at the node's
    # grain): sum_of_days | average_of_rows | distinct_over_week | first_day |
    # ratio_of_week_inputs | derived_of_week_inputs. None on sets built before
    # the generator stated it (§10.9).
    metricflow_week: str | None = None
    matches_metricflow_week: bool | None = None
    input_metricflow_week: dict[str, str] = field(default_factory=dict)
    dimensions: tuple[str, ...] = ()
    # Dimension name as the series file spells it -> its MetricFlow group-by
    # id (`channel` -> `session__channel`), which is what a tree's
    # `dimensions:` names as `source`. None where no single group-by
    # addresses it.
    dimension_sources: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    id: str
    seed: int
    regime: str
    kind: str
    anomalies: tuple[str, ...]
    twin_of: str | None
    config_hash: str
    ground_truth: Path
    series: Path | None
    effects: Path | None
    # Single worlds: the measured stats of every slice in the series file
    # (the ground truth's `measured_effects` holds totals only).
    effects_summary: Path | None = None
    # Week values of the distinct counts, as MetricFlow answers them (v2).
    weekly_series: Path | None = None


@dataclass(frozen=True)
class WorldSet:
    root: Path
    schema_version: int
    generator_version: str
    config: str
    config_sha256: str
    vertical: str
    company_slug: str
    seeds: tuple[int, ...]
    regimes: tuple[str, ...]
    metrics: dict[str, MetricSpec]
    worlds: tuple[World, ...]
    # The set's cross-seed summary, with one `verdict` per (regime, anomaly,
    # metric, slice). None for a set built without measured effects.
    effects_summary: Path | None = None
    # v2 `verdicts`: column -> {level, t_critical, min_seeds, ...}, strict first.
    verdict_columns: dict[str, dict[str, Any]] = field(default_factory=dict)

    def world(self, world_id: str) -> World:
        for w in self.worlds:
            if w.id == world_id:
                return w
        raise WorldSetError(f"world {world_id!r} is not in {self.root / MANIFEST_NAME}")

    def ground_truth(self, world: World) -> list[dict[str, Any]]:
        """The world's planted-anomaly records (empty for a null world)."""
        return json.loads(world.ground_truth.read_text())

    def check_verdict_column(self, column: str) -> None:
        """Refuse a verdict column the manifest does not declare."""
        declared = list(self.verdict_columns) or [STRICT_VERDICT]
        if column not in declared:
            raise WorldSetError(
                f"verdict column {column!r} is not in this set's `verdicts` "
                f"(it declares {declared})"
            )

    def verdicts(self, regime: str, anomaly: str, column: str = STRICT_VERDICT) -> dict[tuple, str]:
        """`(metric, dimension, value) -> verdict` for one anomaly, from the
        set's `effects_summary.parquet` column `column` (`dimension`/`value`
        None for a total). Empty when the set has no summary or the summary no
        such column: the caller then says it fell back."""
        table = self._verdict_table()
        if table is None or column not in table.columns:
            return {}
        rows = table[(table["regime"] == regime) & (table["anomaly"] == anomaly)]
        return {
            (r["metric"], _none(r["dimension"]), _none(r["value"])): r[column]
            for r in rows.to_dict("records")
        }

    def set_effects(self, regime: str, anomaly: str) -> dict[tuple, dict[str, Any]]:
        """`(metric, dimension, value) -> {rel_mean, rel_sd, n_seeds}`: the
        cross-seed mean relative change over the anomaly's declared window
        and its spread across the set's seeds. Empty when the summary lacks
        those columns; attribution then refuses sizes by name."""
        table = self._verdict_table()
        if table is None or not set(_SET_STATS.values()) <= set(table.columns):
            return {}
        rows = table[(table["regime"] == regime) & (table["anomaly"] == anomaly)]
        return {
            (r["metric"], _none(r["dimension"]), _none(r["value"])): {
                name: _none(r.get(col))
                for name, col in {**_SET_STATS, **_OPTIONAL_SET_STATS}.items()
            }
            for r in rows.to_dict("records")
        }

    def _verdict_table(self):
        if self.effects_summary is None or not self.effects_summary.exists():
            return None
        cached = _VERDICT_TABLES.get(self.effects_summary)
        if cached is None:
            import pandas as pd

            frame = pd.read_parquet(self.effects_summary)
            need = ["regime", "anomaly", "metric", "dimension", "value", "verdict"]
            if any(c not in frame.columns for c in need):
                return None
            extra = [
                *_SET_STATS.values(),
                "verdict_10pct",
                "seeds_to_establish",
                "seeds_to_establish_10pct",
                *_OPTIONAL_SET_STATS.values(),
            ]
            keep = need + [c for c in extra if c in frame.columns]
            cached = _VERDICT_TABLES[self.effects_summary] = frame[keep]
        return cached


_VERDICT_TABLES: dict[Path, Any] = {}
_SET_STATS = {"rel_mean": "rel_change_mean", "rel_sd": "rel_change_sd", "n_seeds": "n_seeds"}
# Read when present: the null twins' mean level, below which a relative
# change means nothing (a stock that can go negative, a flow that is zero
# outside a promotion).
_OPTIONAL_SET_STATS = {
    "null_mean": "null_mean",
    # The effect by the metric's own time_aggregation (Σnum/Σden for a rate),
    # beside `rel_change`, the window mean of daily values (generator §10.6).
    "rel_agg_mean": "rel_change_aggregated_mean",
    "rel_agg_sd": "rel_change_aggregated_sd",
    # Twins identical on every day before the start, in every seed; NULL when
    # some seed had no day to judge by (generator §10.5).
    "aligned_before_start": "aligned_before_start",
    "aligned_before_start_share": "aligned_before_start_share",
    # Median days from the declared start to the measured onset, across seeds.
    "onset_lag_days": "onset_median_lag_days",
}


def _none(v):
    """A parquet null (None or NaN) as None."""
    return None if v is None or v != v else v


def _need(obj: dict, key: str, where: str):
    if key not in obj:
        raise WorldSetError(f"{where} has no `{key}`, which the evaluation runner reads")
    return obj[key]


def load_world_set(path: str | Path, *, allow_v1: bool = False) -> WorldSet:
    """Read and check a world set's manifest (its directory, or the file).

    Version 2 is required. `allow_v1` reads a version 1 set, for comparing
    against a scoreboard built on one; the caller stamps that it did."""
    path = Path(path)
    if path.is_dir():
        path = path / MANIFEST_NAME
    if not path.exists():
        raise WorldSetError(f"no world-set manifest at {path}")
    raw = json.loads(path.read_text())
    where = str(path)
    version = _need(raw, "schema_version", where)
    if version in LEGACY_SCHEMA_VERSIONS and not allow_v1:
        raise WorldSetError(
            f"{where} is schema_version {version!r}; this runner needs schema_version "
            f"{SCHEMA_VERSION}. A version 1 set cannot say which `moved` rule built it and "
            "lacks the weekly distinct counts and verdict levels (generator spec §10.4): "
            "rebuild it, or pass --allow-schema-v1 to read it anyway, stamped on the "
            "scoreboard."
        )
    if version != SCHEMA_VERSION and version not in LEGACY_SCHEMA_VERSIONS:
        raise WorldSetError(
            f"{where} is schema_version {version!r}; this runner reads {SCHEMA_VERSION}. "
            "Update the runner against the generator's docs/eval_worlds_spec.md before "
            "reading it."
        )
    root = path.parent

    metrics = {}
    for m in raw.get("metrics", []):
        name = _need(m, "name", f"a metric in {where}")
        metrics[name] = MetricSpec(
            name=name,
            type=_need(m, "type", f"metric {name!r} in {where}"),
            time_aggregation=_need(m, "time_aggregation", f"metric {name!r} in {where}"),
            inputs=dict(m.get("inputs") or {}),
            input_time_aggregation=dict(m.get("input_time_aggregation") or {}),
            expr=m.get("expr"),
            formula=m.get("formula"),
            measure_agg=m.get("measure_agg"),
            # Sets that predate `has_sides` carried sides for ratios only.
            has_sides=bool(m.get("has_sides", m["type"] == "ratio")),
            has_weekly_series=bool(m.get("has_weekly_series", False)),
            metricflow_week=m.get("metricflow_week"),
            matches_metricflow_week=m.get("matches_metricflow_week"),
            input_metricflow_week=dict(m.get("input_metricflow_week") or {}),
            dimensions=tuple(m.get("dimensions") or ()),
            dimension_sources=(
                dict(m["dimensions"]) if isinstance(m.get("dimensions"), dict) else {}
            ),
        )

    worlds = []
    for w in _need(raw, "worlds", where):
        wid = _need(w, "id", f"a world in {where}")
        here = f"world {wid!r} in {where}"
        kind = _need(w, "kind", here)
        if kind not in WORLD_KINDS:
            raise WorldSetError(f"{here} is of kind {kind!r}; known kinds: {list(WORLD_KINDS)}")
        worlds.append(
            World(
                id=wid,
                seed=int(_need(w, "seed", here)),
                regime=_need(w, "regime", here),
                kind=kind,
                anomalies=tuple(w.get("anomalies") or ()),
                twin_of=w.get("twin_of"),
                config_hash=_need(w, "config_hash", here),
                ground_truth=root / _need(w, "ground_truth", here),
                series=root / w["series"] if w.get("series") else None,
                effects=root / w["effects"] if w.get("effects") else None,
                effects_summary=(root / w["effects_summary"] if w.get("effects_summary") else None),
                weekly_series=root / w["weekly_series"] if w.get("weekly_series") else None,
            )
        )
    ids = [w.id for w in worlds]
    if len(set(ids)) != len(ids):
        raise WorldSetError(f"{where} lists a world id twice")
    for w in worlds:
        if w.twin_of is not None and w.twin_of not in ids:
            raise WorldSetError(
                f"world {w.id!r} names its twin {w.twin_of!r}, which is not in {where}"
            )
    return WorldSet(
        root=root,
        schema_version=version,
        generator_version=_need(raw, "generator_version", where),
        config=_need(raw, "config", where),
        config_sha256=_need(raw, "config_sha256", where),
        vertical=_need(raw, "vertical", where),
        company_slug=raw.get("company_slug", ""),
        seeds=tuple(raw.get("seeds") or sorted({w.seed for w in worlds})),
        regimes=tuple(raw.get("regimes") or sorted({w.regime for w in worlds})),
        metrics=metrics,
        worlds=tuple(worlds),
        effects_summary=root / raw["effects_summary"] if raw.get("effects_summary") else None,
        verdict_columns={v["column"]: v for v in raw.get("verdicts") or [] if v.get("column")},
    )

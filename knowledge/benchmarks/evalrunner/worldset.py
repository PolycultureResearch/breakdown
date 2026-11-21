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
SUPPORTED_SCHEMA_VERSIONS = (1,)
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
    dimensions: tuple[str, ...] = ()


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

    def world(self, world_id: str) -> World:
        for w in self.worlds:
            if w.id == world_id:
                return w
        raise WorldSetError(f"world {world_id!r} is not in {self.root / MANIFEST_NAME}")

    def ground_truth(self, world: World) -> list[dict[str, Any]]:
        """The world's planted-anomaly records (empty for a null world)."""
        return json.loads(world.ground_truth.read_text())

    def verdicts(self, regime: str, anomaly: str) -> dict[tuple, str]:
        """`(metric, dimension, value) -> verdict` for one anomaly, from the
        set's `effects_summary.parquet` (`dimension`/`value` None for a
        total). Empty when the set has no summary or the summary no
        `verdict` column: the caller then says it fell back."""
        table = self._verdict_table()
        if table is None:
            return {}
        rows = table[(table["regime"] == regime) & (table["anomaly"] == anomaly)]
        return {
            (r.metric, _none(r.dimension), _none(r.value)): r.verdict
            for r in rows.itertuples(index=False)
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
                name: _none(r[col]) for name, col in _SET_STATS.items()
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
            keep = need + [c for c in _SET_STATS.values() if c in frame.columns]
            cached = _VERDICT_TABLES[self.effects_summary] = frame[keep]
        return cached


_VERDICT_TABLES: dict[Path, Any] = {}
_SET_STATS = {"rel_mean": "rel_change_mean", "rel_sd": "rel_change_sd", "n_seeds": "n_seeds"}


def _none(v):
    """A parquet null (None or NaN) as None."""
    return None if v is None or v != v else v


def _need(obj: dict, key: str, where: str):
    if key not in obj:
        raise WorldSetError(f"{where} has no `{key}`, which the evaluation runner reads")
    return obj[key]


def load_world_set(path: str | Path) -> WorldSet:
    """Read and check a world set's manifest (its directory, or the file)."""
    path = Path(path)
    if path.is_dir():
        path = path / MANIFEST_NAME
    if not path.exists():
        raise WorldSetError(f"no world-set manifest at {path}")
    raw = json.loads(path.read_text())
    where = str(path)
    version = _need(raw, "schema_version", where)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise WorldSetError(
            f"{where} is schema_version {version!r}; this runner reads "
            f"{list(SUPPORTED_SCHEMA_VERSIONS)}. Update the runner against the "
            "generator's docs/eval_worlds_spec.md before reading it."
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
            dimensions=tuple(m.get("dimensions") or ()),
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
    )

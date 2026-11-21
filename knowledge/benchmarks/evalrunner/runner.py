"""Plan an evaluation run, and execute its jobs.

A run is a world set, a tree and a tier. Planning (`plan_run`) turns those
into `run.json`: a stamp that says exactly what was run, and a flat, ordered
list of jobs. Shard `k/n` executes jobs `k, k + n, ...` (`run_shard`), each in
its own process with its own PyTensor compile directory, and appends typed
rows to `rows/<type>.<shard>.jsonl`.

Two kinds of job:

- **calibration**: one null world, one non-formula node, one window start.
  One fit, scored at every horizon due at that start (spec §5.1).
- **single**: one single world. The planted root is mapped to the tree, its
  own score at onset is taken (§5.2), and the root is ranked against the
  other nodes four ways (§5.3). All of a world's fits share one `FitStore`.

A job's rows are written together with a `done` marker when it finishes, and
each carries the job's key: a hash of everything that decides its result. A
rerun skips jobs whose key is already done, in whatever shard layout they ran;
a change to the engine, the tree or the world changes the key, and the stale
rows are simply never read again.

Row types beyond the ones scored today are part of the format on purpose:
`nodes` (every in-scope node's innovation, z and contribution, per single
world) is what attribution (§5.4) will be scored from, and the `off_tree` and
`skipped` statuses are what abstention (§5.5) will count.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .attribution import attribution_items, attribution_window
from .fitcache import DEFAULT_TRACE_CACHE_BYTES, FitStore, fit_key
from .loader import TreeSpec, WorldSeries, load_world, plan_tree
from .truth import Measured, analysis_window, map_root, onset_of, pick_target, read_twin_summary
from .windows import horizons_for, window_starts
from .worldset import World, WorldSet, load_world_set

REPO = Path(__file__).resolve().parents[3]
ROW_FORMAT_VERSION = 1
# A single world's rows: 2 added `attribution`, 3 split its root item into
# `root_unexplained` and `root_gap`. Calibration jobs keep their keys.
SINGLE_ROW_FORMAT_VERSION = 3

DEV_SEEDS = tuple(range(0, 5))
HELD_OUT_SEEDS = tuple(range(5, 10))

# Evaluation spec §7. `seeds` is how many development seeds; `regimes` None
# means every regime the set holds; `singles` is how many single worlds per
# seed (None: all); the window grid is `spacing_days`, or `max_windows` per
# null world at whatever whole-week spacing fits.
TIERS: dict[str, dict[str, Any]] = {
    "smoke": dict(
        seeds=3, regimes=["baseline"], horizons=(7,), max_windows=8, spacing_days=None, singles=3
    ),
    "standard": dict(
        seeds=5,
        regimes=["baseline"],
        horizons=(1, 7, 28),
        max_windows=None,
        spacing_days=28,
        singles=None,
    ),
    # Attribution only (§5.4): single worlds, no calibration windows. Meant
    # for a ten-seed set with `--singles` naming the anomalies whose sizes the
    # set can ground; the scoreboard says which items it could.
    "attribution": dict(
        seeds=5, regimes=["baseline"], horizons=(7,), max_windows=0, spacing_days=None, singles=None
    ),
    "full": dict(
        seeds=5,
        regimes=None,
        horizons=(1, 7, 28),
        max_windows=None,
        spacing_days=28,
        singles=None,
    ),
}


class RunRefused(RuntimeError):
    """The run as asked for cannot be planned; the message says why."""


# ---------------------------------------------------------------------------
# Stamps
# ---------------------------------------------------------------------------
def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def engine_stamp() -> dict[str, str]:
    """The engine commit, and the key fits are cached under: the tree hash of
    `breakdown/` (so a docs-only commit keeps the cache), plus a hash of any
    uncommitted change there, plus the sampler settings."""
    from breakdown.engine.model import FIT_RANDOM_SEED, NUTS_CHAINS, NUTS_DRAWS

    commit = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD:breakdown")
    dirty = _git("status", "--porcelain", "--", "breakdown")
    if dirty:
        diff = _git("diff", "HEAD", "--", "breakdown")
        tree += "+dirty." + hashlib.sha256((dirty + diff).encode()).hexdigest()[:12]
    return {
        "commit": commit + ("+dirty" if _git("status", "--porcelain") else ""),
        "key": f"{tree}|draws={NUTS_DRAWS}|chains={NUTS_CHAINS}|seed={FIT_RANDOM_SEED}",
    }


def world_key(worldset: WorldSet, world: World) -> str:
    return f"{world.id}|{world.config_hash}|{worldset.generator_version}"


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def role_of(dag, node: str) -> str:
    """Spec §5's five roles. An exact identity is a formula whatever its kind."""
    defn = dag.nodes[node]["definition"]
    if defn.formula and dag.in_degree(node) > 0:
        return "formula"
    if defn.kind in ("rate", "stock"):
        return defn.kind
    return f"flow, {'no parents' if dag.in_degree(node) == 0 else 'learned parent'}"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def select_seeds(tier: str, available, *, seeds=None, held_out: bool = False) -> list[int]:
    """Development seeds 0-4 by default. Anything from 5 up is held out and
    needs the explicit flag, which the scoreboard then records."""
    available = sorted(available)
    if seeds is None:
        wanted = list(DEV_SEEDS[: TIERS[tier]["seeds"]])
        if held_out:
            wanted += list(HELD_OUT_SEEDS)
    else:
        wanted = sorted(set(seeds))
        beyond = [s for s in wanted if s not in DEV_SEEDS]
        if beyond and not held_out:
            raise RunRefused(
                f"seeds {beyond} are held out (development seeds are {list(DEV_SEEDS)}); "
                "pass --held-out to score them, and the scoreboard will say so"
            )
    chosen = [s for s in wanted if s in available]
    if not chosen:
        raise RunRefused(f"the world set holds seeds {available}; none of {wanted} is among them")
    return chosen


def _depth(dag) -> dict[str, int]:
    import networkx as nx

    depth: dict[str, int] = {}
    for n in nx.topological_sort(dag):
        depth[n] = 1 + max((depth[p] for p in dag.predecessors(n)), default=-1)
    return depth


def select_singles(worldset, worlds, dag, plan, limit, names=None) -> tuple[list[str], dict]:
    """Which anomalies' single worlds to run, and what each one is to this
    tree. With a `limit` (the smoke tier), the scored ones are spread over the
    depths of the tree the roots sit at."""
    by_anomaly: dict[str, World] = {}
    for w in worlds:
        if w.kind == "single" and w.anomalies:
            by_anomaly.setdefault(w.anomalies[0], w)
    if names is not None:
        unknown = sorted(set(names) - set(by_anomaly))
        if unknown:
            raise RunRefused(f"no single world for anomalies {unknown}; have {sorted(by_anomaly)}")
        by_anomaly = {n: by_anomaly[n] for n in names}
    depth = _depth(dag)
    described = {}
    for name, w in by_anomaly.items():
        record = worldset.ground_truth(w)[0]
        if record.get("kind") != "rate":
            described[name] = {"class": "data_quality"}
            continue
        mapping = map_root(dag, plan, record, measured_for(worldset, w, record))
        described[name] = {
            "class": mapping.status,
            "root_metric": record.get("root_metric"),
            "root_metric_rate": record.get("root_metric_rate"),
            "root_slice": record.get("root_slice") or {},
            "truth": mapping.truth,
            "depth": min((depth[t] for t in mapping.truth), default=None),
            "note": mapping.note,
            "set_verdicts": bool(worldset.verdicts(w.regime, name)),
        }
    if limit is None or names is not None:
        return list(by_anomaly), described
    scorable = sorted(
        (n for n, d in described.items() if d.get("truth")),
        key=lambda n: (described[n]["depth"], n),
    )
    if len(scorable) > limit:
        picks = [round(i * (len(scorable) - 1) / (limit - 1)) for i in range(limit)]
        scorable = [scorable[i] for i in sorted(set(picks))]
    return scorable, described


def measured_for(worldset: WorldSet, world: World, record: dict[str, Any]) -> Measured:
    """Everything the set measured about a single world's anomaly."""
    return Measured(
        record,
        verdicts=worldset.verdicts(world.regime, record["id"]),
        twin=read_twin_summary(world.effects_summary),
    )


def plan_run(
    worlds_dir: str | Path,
    tree_path: str | Path,
    *,
    tier: str = "smoke",
    bindings_path: str | Path | None = None,
    seeds: list[int] | None = None,
    held_out: bool = False,
    regimes: list[str] | None = None,
    horizons: tuple[int, ...] | None = None,
    spacing_days: int | None = None,
    max_windows: int | None = None,
    singles: list[str] | None = None,
    drop_unserved: bool = False,
    trace_cache_bytes: int = DEFAULT_TRACE_CACHE_BYTES,
    engine: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Everything a run is, as the dict written to `run.json`."""
    if tier not in TIERS:
        raise RunRefused(f"unknown tier {tier!r}; tiers: {sorted(TIERS)}")
    preset = TIERS[tier]
    worldset = load_world_set(worlds_dir)
    tree = TreeSpec.read(tree_path, bindings_path)
    engine = engine or engine_stamp()

    available = {w.seed for w in worldset.worlds}
    chosen_seeds = select_seeds(tier, available, seeds=seeds, held_out=held_out)
    chosen_regimes = regimes or preset["regimes"] or list(worldset.regimes)
    missing = [r for r in chosen_regimes if r not in worldset.regimes]
    if missing:
        raise RunRefused(f"the world set has regimes {list(worldset.regimes)}, not {missing}")
    horizons = tuple(horizons or preset["horizons"])
    if spacing_days is None and max_windows is None:
        spacing_days, max_windows = preset["spacing_days"], preset["max_windows"]

    in_scope = [w for w in worldset.worlds if w.seed in chosen_seeds and w.regime in chosen_regimes]
    nulls = [w for w in in_scope if w.kind == "null"]
    if not nulls:
        raise RunRefused("no null world for the chosen seeds and regimes")

    # The tree is bound once, on the first null world, and every worker checks
    # its own world gives the same plan: a set's worlds share their metrics.
    from breakdown.parser import Parser

    first = WorldSeries(nulls[0], worldset.metrics)
    plan = plan_tree(tree, first, drop_unserved=drop_unserved)
    effective_tree = yaml.safe_dump(plan.raw, sort_keys=False)
    dag = Parser(effective_tree).dag
    tree_sha = hashlib.sha256(effective_tree.encode()).hexdigest()

    single_names, described = select_singles(
        worldset, in_scope, dag, plan, preset["singles"], names=singles
    )
    single_worlds = [w for w in in_scope if w.kind == "single" and w.anomalies[0] in single_names]

    jobs: list[dict[str, Any]] = []
    for w in single_worlds:
        # The set's verdicts depend on which seeds the set was built with, so
        # they are part of what decides a single world's result.
        verdicts = sorted(
            (json.dumps(k), v) for k, v in worldset.verdicts(w.regime, w.anomalies[0]).items()
        )
        key = _sha(
            [
                "single",
                SINGLE_ROW_FORMAT_VERSION,
                world_key(worldset, w),
                tree_sha,
                engine["key"],
                horizons,
                verdicts,
            ]
        )
        jobs.append({"job": key[:32], "kind": "single", "world": w.id})

    scored_nodes = [n for n in dag.nodes if not dag.nodes[n]["definition"].formula]
    grid = {}
    # `max_windows=0` (the attribution tier) scores no null windows at all.
    for w in nulls if max_windows != 0 else []:
        span = WorldSeries(w, worldset.metrics) if w is not nulls[0] else first
        starts, spacing = window_starts(
            span.start,
            span.end,
            horizons=horizons,
            spacing_days=spacing_days,
            max_windows=max_windows,
        )
        grid[w.id] = {"spacing_days": spacing, "starts": [str(s.date()) for s in starts]}
        for i, start in enumerate(starts):
            for node in scored_nodes:
                grain = dag.nodes[node]["definition"].grain
                due = horizons_for(i, start, grain, horizons, spacing, span.end)
                if not due:
                    continue
                key = _sha(
                    [
                        "calibration",
                        ROW_FORMAT_VERSION,
                        due,
                        fit_key(
                            world_key(worldset, w), dag, node, str(start.date()), engine["key"]
                        ),
                    ]
                )
                jobs.append(
                    {
                        "job": key[:32],
                        "kind": "calibration",
                        "world": w.id,
                        "node": node,
                        "start": str(start.date()),
                        "index": i,
                        "horizons": due,
                        "spacing_days": spacing,
                    }
                )
    # Shard k takes every n-th job. In tree order a shard would draw the same
    # nodes at every start (always the daily ones, or never), so calibration
    # jobs are dealt in the order of their keys: fixed, and unrelated to cost.
    # The single worlds, the heaviest jobs, stay first and are dealt evenly.
    jobs.sort(key=lambda j: (j["kind"] != "single", j["job"] if j["kind"] != "single" else ""))

    return {
        "row_format_version": ROW_FORMAT_VERSION,
        "single_row_format_version": SINGLE_ROW_FORMAT_VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tier": tier,
        "engine_commit": engine["commit"],
        "engine_key": engine["key"],
        "generator_version": worldset.generator_version,
        "manifest": {
            "path": str(Path(worlds_dir).resolve()),
            "schema_version": worldset.schema_version,
            "config": worldset.config,
            "config_sha256": worldset.config_sha256,
            "effects_summary": None
            if worldset.effects_summary is None
            else str(worldset.effects_summary.relative_to(worldset.root)),
        },
        "vertical": worldset.vertical,
        "company": worldset.company_slug,
        "tree": {
            "path": _relative(tree_path),
            "sha256": tree.sha256,
            "canonical": False,
            "bindings_path": None if bindings_path is None else _relative(bindings_path),
            "bindings_sha256": tree.bindings_sha256,
            "effective_sha256": tree_sha,
            "drop_unserved": drop_unserved,
            **plan.summary(),
        },
        "seeds": chosen_seeds,
        "held_out": bool(held_out),
        "held_out_seeds": [s for s in chosen_seeds if s not in DEV_SEEDS],
        "regimes": chosen_regimes,
        "horizons": list(horizons),
        "windows": grid,
        "singles": {n: described[n] for n in single_names},
        "singles_not_run": {n: d for n, d in described.items() if n not in single_names},
        "worlds": {
            w.id: {"seed": w.seed, "regime": w.regime, "kind": w.kind, "anomalies": w.anomalies}
            for w in [*nulls, *single_worlds]
        },
        "trace_cache_bytes": trace_cache_bytes,
        "host": {"machine": platform.machine(), "cpus": os.cpu_count(), "load_at_plan": _load()},
        "jobs": jobs,
    }


def _relative(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def _load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
ROW_TYPES = (
    "calibration",
    "detection",
    "localization",
    "attribution",
    "nodes",
    "fits",
    "analysis",
    "skipped",
)


def read_rows(run_dir: str | Path, row_type: str) -> list[dict[str, Any]]:
    out = []
    for path in sorted(Path(run_dir, "rows").glob(f"{row_type}.*.jsonl")):
        with open(path) as f:
            out.extend(json.loads(line) for line in f if line.strip())
    return out


def done_jobs(run_dir: str | Path) -> set[str]:
    return {r["job"] for r in read_rows(run_dir, "done")}


class RowWriter:
    """Buffers one job's rows and writes them, then its `done` marker, when
    the job ends: a job killed midway leaves nothing behind."""

    def __init__(self, run_dir: Path, shard: str):
        self.dir = run_dir / "rows"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shard = shard
        self._buffer: list[tuple[str, dict]] = []

    def emit(self, row_type: str, row: dict[str, Any]) -> None:
        self._buffer.append((row_type, row))

    def commit(self, job: dict[str, Any], seconds: float) -> None:
        by_type: dict[str, list[dict]] = {}
        for row_type, row in self._buffer:
            by_type.setdefault(row_type, []).append({"job": job["job"], **row})
        by_type["done"] = [
            {
                "job": job["job"],
                "kind": job["kind"],
                "seconds": round(seconds, 1),
                "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        ]
        for row_type, rows in by_type.items():  # `done` last
            with open(self.dir / f"{row_type}.{self.shard}.jsonl", "a") as f:
                for row in rows:
                    f.write(json.dumps(row, default=_jsonable) + "\n")
        self._buffer = []


def _jsonable(v):
    import numpy as np

    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, (pd.Timestamp, datetime)):
        return str(v)
    return str(v)


def _finite(v):
    """A float fit for a row, or None: no NaN reaches the scoreboard (rule 3)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


SCORE_FIELDS = (
    "n z pit tail innovation pred_sd actual status ppc_status fit_quality likelihood trend "
    "min_ess divergences max_rhat"
).split()


def _score_row(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in SCORE_FIELDS}
    for k in ("z", "pit", "tail", "innovation", "pred_sd", "actual", "min_ess", "max_rhat"):
        out[k] = _finite(out[k])
    return out


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def run_calibration(lw, job, run, writer, common) -> None:
    from .scoring import innovation

    dag, data = lw.dag, lw.data
    node, start = job["node"], pd.Timestamp(job["start"])
    fit_rows: list[dict] = []
    fits = FitStore(common["world_key"], dag, run["engine_key"], on_fit=fit_rows.append)
    base = {
        **common["ids"],
        "node": node,
        "role": role_of(dag, node),
        "grain": data.grain_of[node],
        "start": job["start"],
        "index": job["index"],
        "spacing_days": job["spacing_days"],
    }
    for h in job["horizons"]:
        try:
            r = innovation(dag, data, fits, node, (start, start + pd.Timedelta(days=h - 1)))
        except Exception as exc:  # one node's failure is a row, not the run's
            r = {"status": f"error: {type(exc).__name__}: {exc}"[:200]}
        writer.emit("calibration", {**base, "horizon_days": h, **_score_row(r)})
    for f in fit_rows:
        writer.emit("fits", {**base, "purpose": "calibration", **f})


def run_single(lw, job, run, writer, common) -> None:
    import networkx as nx

    from breakdown.grains import floor_period

    from .scoring import innovation, node_windows, propagate, rank_of

    dag, data, plan = lw.dag, lw.data, lw.plan
    record = common["truth"][0]
    base = {
        **common["ids"],
        "anomaly": record["id"],
        "anomaly_type": record.get("type"),
        "root_metric": record.get("root_metric"),
        "root_slice": record.get("root_slice") or {},
    }
    if record.get("kind") != "rate":
        writer.emit("skipped", {**base, "reason": "data_quality", "detail": record.get("kind")})
        return
    measured = common["measured"]
    mapping = map_root(dag, plan, record, measured)
    base.update(
        root_node=mapping.root_node,
        truth=mapping.truth,
        mapping=mapping.status,
        mapping_note=mapping.note,
    )
    if not mapping.on_tree:
        writer.emit("localization", {**base, "status": "off_tree"})
        return

    fit_rows: list[dict] = []
    fits = FitStore(
        common["world_key"],
        dag,
        run["engine_key"],
        trace_dir=common["trace_dir"],
        max_bytes=run["trace_cache_bytes"],
        on_fit=fit_rows.append,
    )

    # §5.2: the root's own score at the date its own series first moves.
    onsets = {}
    for node in mapping.truth:
        onset, source = onset_of(
            node, plan, record, is_root=node == mapping.root_node, measured=measured
        )
        onsets[node] = onset
        grain = data.grain_of[node]
        start = floor_period(onset, grain)
        for h in horizons_for(0, start, grain, tuple(run["horizons"]), 10**6, lw.end):
            try:
                r = innovation(dag, data, fits, node, (start, start + pd.Timedelta(days=h - 1)))
            except Exception as exc:
                r = {"status": f"error: {type(exc).__name__}: {exc}"[:200]}
            writer.emit(
                "detection",
                {
                    **base,
                    "node": node,
                    "role": role_of(dag, node),
                    "grain": grain,
                    "onset": str(onset.date()),
                    "onset_source": source,
                    "declared_start": record["start"],
                    "start": str(start.date()),
                    "horizon_days": h,
                    **_score_row(r),
                },
            )

    # §5.3: rank the root among the nodes upstream of the top metric reached.
    target, basis = pick_target(dag, plan, record, mapping.truth, measured)
    if target is None:
        writer.emit("localization", {**base, "status": "no_target", "detail": basis})
    else:
        ana = analysis_window(min(onsets.values()), record, data.grain_of[target])
        t0 = time.perf_counter()
        scope, windows = node_windows(dag, data, target, ana)
        inn = {}
        for n in scope:
            try:
                inn[n] = innovation(dag, data, fits, n, windows[n])
            except Exception as exc:
                inn[n] = {
                    "status": f"error: {type(exc).__name__}: {exc}"[:200],
                    "node": n,
                    "grain": data.grain_of[n],
                    "dates": pd.DatetimeIndex([]),
                }
        usable = {n for n in scope if inn[n].get("e") is not None}
        departure = propagate(dag, data, scope, windows, inn, usable)[target]
        contrib = {n: propagate(dag, data, scope, windows, inn, {n})[target] for n in usable}
        innovation_seconds = time.perf_counter() - t0

        t0 = time.perf_counter()
        rca, rca_error = _rca(dag, data, fits, target, ana)
        rca_seconds = time.perf_counter() - t0

        z = {n: _finite(inn[n].get("z")) for n in scope}
        rel = {n: _finite(v.get("relative_change")) for n, v in rca["nodes"].items() if n != target}
        rankings = {
            "innovation_contribution": sorted(
                (n for n in usable if n != target), key=lambda n: -abs(contrib[n])
            ),
            "innovation_evidence": sorted(
                (n for n in usable if n != target and z[n] is not None), key=lambda n: -abs(z[n])
            ),
            "engine_ranked_causes": [c["metric"] for c in rca["ranked_causes"]],
            "naive_relative_change": sorted(
                (n for n, v in rel.items() if v is not None), key=lambda n: -abs(rel[n])
            ),
        }
        truth = set(mapping.truth)
        truth_z = max((abs(z[t]) for t in truth if z.get(t) is not None), default=None)
        shared = {
            **base,
            "target": target,
            "target_basis": basis,
            "analysis_start": ana[0],
            "analysis_end": ana[1],
            "role": role_of(dag, mapping.truth[0]),
            "n_scope": len(scope),
            "truth_abs_z": truth_z,
        }
        for method, ranking in rankings.items():
            first = ranking[0] if ranking else None
            first_z = None if first is None or z.get(first) is None else abs(z[first])
            status = "ok"
            if method == "engine_ranked_causes" and rca_error:
                status = f"error: {rca_error}"[:200]
            writer.emit(
                "localization",
                {
                    **shared,
                    "status": status,
                    "method": method,
                    "rank": rank_of(ranking, truth),
                    "n_ranked": len(ranking),
                    "first": first,
                    "first_abs_z": first_z,
                    "top3": ranking[:3],
                },
            )
        topo = [n for n in nx.topological_sort(dag) if n in scope]
        for n in topo:
            i = inn[n]
            writer.emit(
                "nodes",
                {
                    **base,
                    "target": target,
                    "node": n,
                    "role": role_of(dag, n),
                    "window_start": str(windows[n][0].date()),
                    "window_end": str(windows[n][1].date()),
                    "is_truth": n in truth,
                    "contribution": _finite(contrib.get(n)),
                    "departure": _finite(departure),
                    **_score_row(i),
                },
            )
        # §5.4: the engine's attributed sizes over the anomaly's own window.
        t0 = time.perf_counter()
        attr_window = attribution_window(record, data.grain_of[target])
        if tuple(attr_window) == tuple(ana):
            attr_rca, attr_error = rca, rca_error
        else:
            attr_rca, attr_error = _rca(dag, data, fits, target, attr_window)
        if attr_error:
            writer.emit(
                "attribution",
                {**base, "target": target, "status": f"error: {attr_error}"[:200]},
            )
        else:
            twin = common["load_twin"]()
            for item in attribution_items(lw, twin, attr_rca, mapping.truth, target, measured):
                writer.emit(
                    "attribution",
                    {
                        **base,
                        "target": target,
                        "status": "ok",
                        "attribution_start": attr_window[0],
                        "attribution_end": attr_window[1],
                        **item,
                    },
                )
        attribution_seconds = time.perf_counter() - t0

        writer.emit(
            "analysis",
            {
                **base,
                "target": target,
                "n_scope": len(scope),
                "innovation_seconds": round(innovation_seconds, 1),
                "rca_seconds": round(rca_seconds, 1),
                "attribution_seconds": round(attribution_seconds, 1),
                "fits_made": len(fit_rows),
                "trace_cache_hits": fits.hits,
            },
        )
    for f in fit_rows:
        writer.emit(
            "fits",
            {
                **common["ids"],
                "role": role_of(dag, f["node"]),
                "grain": data.grain_of[f["node"]],
                "purpose": "single",
                **f,
            },
        )


def _rca(dag, data, fits, target, window):
    from breakdown.engine.rca import run_rca

    try:
        result = run_rca(
            dag,
            data,
            fits,
            target,
            analysis_start=window[0],
            analysis_end=window[1],
            reference_sensitivity=False,
        )
        return result, None
    except Exception as exc:
        return {"ranked_causes": [], "nodes": {}}, f"{type(exc).__name__}: {exc}"


def run_shard(run_dir: str | Path, shard: tuple[int, int] = (0, 1), log=print) -> int:
    """Execute this shard's jobs that are not already done. Returns how many ran."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    k, n = shard
    mine = [j for i, j in enumerate(run["jobs"]) if i % n == k]
    finished = done_jobs(run_dir)
    todo = [j for j in mine if j["job"] not in finished]
    log(f"shard {k}/{n}: {len(todo)} of {len(mine)} jobs to run", flush=True)
    if not todo:
        return 0
    worldset = load_world_set(run["manifest"]["path"])
    tree = TreeSpec.read(REPO / run["tree"]["path"], _maybe(run["tree"]["bindings_path"]))
    writer = RowWriter(run_dir, f"{k}of{n}")
    order = {wid: i for i, wid in enumerate(run["worlds"])}
    todo.sort(key=lambda j: order[j["world"]])  # stable: one world loaded at a time
    loaded = None
    for count, job in enumerate(todo, 1):
        if loaded is None or loaded.world.id != job["world"]:
            world = worldset.world(job["world"])
            loaded = load_world(worldset, world, tree, drop_unserved=run["tree"]["drop_unserved"])
            here, planned = loaded.plan.summary(), run["tree"]
            # The reasons name the world, so nodes are compared, not sentences.
            if (
                here["served"] != planned["served"]
                or set(here["unserved"]) != set(planned["unserved"])
                or here["dropped"] != planned["dropped"]
                or here["derived_instead_of_fetched"] != planned["derived_instead_of_fetched"]
            ):
                raise RunRefused(
                    f"world '{world.id}' binds the tree differently from the world the run "
                    "was planned on; a set's worlds must share their metrics"
                )
            truth = worldset.ground_truth(world)
            common = {
                "world_key": world_key(worldset, world),
                "ids": {
                    "world": world.id,
                    "seed": world.seed,
                    "regime": world.regime,
                    "vertical": worldset.vertical,
                },
                "truth": truth,
                "measured": measured_for(worldset, world, truth[0]) if truth else None,
                "load_twin": _twin_loader(worldset, world, tree, run["tree"]["drop_unserved"]),
                "trace_dir": run_dir / "traces",
            }
        t0 = time.perf_counter()
        if job["kind"] == "calibration":
            run_calibration(loaded, job, run, writer, common)
        else:
            run_single(loaded, job, run, writer, common)
        seconds = time.perf_counter() - t0
        writer.commit(job, seconds)
        what = job.get("node") or ""
        log(
            f"[{count}/{len(todo)}] {job['kind']} {job['world']} {what} {job.get('start', '')} "
            f"{seconds:.0f}s",
            flush=True,
        )
    return len(todo)


def _twin_loader(worldset, world, tree, drop_unserved):
    """Loads a single world's null twin when first asked, once."""
    cache = {}

    def load():
        if "twin" not in cache:
            if world.twin_of is None:
                raise RunRefused(f"single world '{world.id}' names no null twin")
            twin = worldset.world(world.twin_of)
            cache["twin"] = load_world(worldset, twin, tree, drop_unserved=drop_unserved)
        return cache["twin"]

    return load


def _maybe(path):
    return None if path is None else REPO / path

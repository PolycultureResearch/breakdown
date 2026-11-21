"""Raw rows -> a scoreboard, and the difference between two scoreboards.

A scoreboard is a list of rows, one per **question x metric x role x
vertical** (plus the horizon, the ranking method and, for Q9, the diagnostic
flag it is split by). Each row holds:

- `units`: the metric's value in each world it was computed on. A null world
  is one unit of a calibration metric (its statistic over that world's
  windows); a single world is one unit of a detection or localization metric.
- `value`: the mean over seeds of each seed's mean over its worlds.
- `spread`: the standard deviation of those per-seed means. Worlds of
  different seeds are independent; windows inside one world are not (spec
  §5.1), so the spread is never taken across windows.
- `n_seeds`, and `n`: how many windows, worlds or fits stand behind it.

Keeping the per-world units is what makes `diff` honest: two scoreboards are
compared on the worlds they share, seed by seed, and the difference carries
the spread of the paired per-seed differences.

Questions scored today: `Q1 calibration`, `Q9 diagnostics` (Q1 split by the
engine's own flags), `Q2 detection`, `Q3 localization`, `Q5 counterfactual attribution`
(sizes against the twin difference, only where the world set can ground
them; `attribution.py`), `Q8 cost`.
`Q6 abstention` is a reserved name: its rows will have this same shape,
computed from the `skipped` and off-tree rows the runner already writes.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .attribution import MAX_REL_PRECISION, eligibility, score_items, target_changes
from .runner import DEV_SEEDS, read_rows

SCOREBOARD_VERSION = 1
EVIDENCE_Z = 2.0
NOT_BUILT = {
    "Q4 slices": "whether the true slice is the top slice of the true node (spec §5.3)",
    "Q6 abstention": "off-tree, data-quality and null-world false alarms (spec §5.5)",
    "Q7 power": "the magnitude ladder (spec §5.2); needs generator phase 2",
    "Q9 catch rate": "share of badly calibrated nodes a flag catches; needs a per-node verdict",
}
ROW_KEY = ("question", "metric", "horizon_days", "method", "split", "role", "vertical")


# ---------------------------------------------------------------------------
# Units -> a row
# ---------------------------------------------------------------------------
def summarize(units: dict[str, float | None], seed_of: dict[str, int]) -> dict[str, Any]:
    """Mean over seeds of per-seed means, and the spread across seeds."""
    by_seed: dict[int, list[float]] = defaultdict(list)
    for world, v in units.items():
        if v is not None and math.isfinite(v):
            by_seed[seed_of[world]].append(v)
    means = [float(np.mean(v)) for v in by_seed.values()]
    return {
        "value": float(np.mean(means)) if means else None,
        "spread": float(np.std(means, ddof=1)) if len(means) > 1 else None,
        "n_seeds": len(means),
    }


def _row(seed_of, units, n, **key) -> dict[str, Any]:
    clean = {w: (None if v is None or not math.isfinite(v) else float(v)) for w, v in units.items()}
    full = {k: key.get(k) for k in ROW_KEY}
    return {**full, **summarize(clean, seed_of), "n": int(n), "units": clean}


# ---------------------------------------------------------------------------
# The statistics (spec §5)
# ---------------------------------------------------------------------------
def lag1_correlation(frame: pd.DataFrame) -> tuple[float | None, int]:
    """Correlation of z between consecutive windows of the same node, with
    each node's own mean removed, pooled over nodes. `(value, pairs)`."""
    a, b = [], []
    for _, g in frame.groupby("node"):
        g = g.sort_values("index")
        centred = (g["z"] - g["z"].mean()).to_numpy()
        index = g["index"].to_numpy()
        step = np.diff(index)
        if not len(step):
            continue
        neighbours = step == step.min()
        a.extend(centred[:-1][neighbours])
        b.extend(centred[1:][neighbours])
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None, len(a)
    return float(np.corrcoef(a, b)[0, 1]), len(a)


def calibration_stats(frame: pd.DataFrame) -> dict[str, float | None]:
    """One world's windows for a group of nodes at one horizon. `sd_z` is
    about each node's own mean, so a biased forecast and a too-narrow one read
    as different numbers (as the prototype's `null-compare`)."""
    from .scoring import _pit_ks  # imported here: it brings the sampler with it

    z = frame["z"]
    centred = z - frame.groupby("node")["z"].transform("mean")
    dof = len(frame) - frame["node"].nunique()
    lag1, _ = lag1_correlation(frame)
    return {
        "mean_z": float(z.mean()),
        "sd_z": float(np.sqrt((centred**2).sum() / dof)) if dof > 0 else None,
        "abs_z_over_2": float((z.abs() > 2).mean()),
        "abs_z_over_2_centred": float((centred.abs() > 2).mean()) if dof > 0 else None,
        "pit_ks": float(_pit_ks(frame["pit"])),
        "lag1_z": lag1,
    }


def _usable(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df[df["z"].notna() & (df["status"] != "exact_identity")].copy()


def _with_all(df: pd.DataFrame) -> pd.DataFrame:
    """Every row once under its own role and once under `all`."""
    return pd.concat([df, df.assign(role="all")], ignore_index=True)


def calibration_rows(rows, seed_of, vertical) -> list[dict]:
    df = _usable(rows)
    out: list[dict] = []
    if df.empty:
        return out
    for (h, role), g in _with_all(df).groupby(["horizon_days", "role"]):
        per_world = {w: calibration_stats(wg) for w, wg in g.groupby("world")}
        for metric in next(iter(per_world.values())):
            out.append(
                _row(
                    seed_of,
                    {w: s[metric] for w, s in per_world.items()},
                    len(g),
                    question="Q1 calibration",
                    metric=metric,
                    horizon_days=int(h),
                    role=role,
                    vertical=vertical,
                )
            )
    # Q9: the same numbers split by what the engine itself said about the fit.
    for flag in ("ppc_status", "fit_quality"):
        for (h, value), g in df.assign(role="all").groupby(["horizon_days", flag], dropna=False):
            split = f"{flag}={value if isinstance(value, str) else 'none'}"
            per_world = {w: calibration_stats(wg) for w, wg in g.groupby("world")}
            totals = df[df["horizon_days"] == h].groupby("world").size()
            shares = {w: len(wg) / totals[w] for w, wg in g.groupby("world")}
            stats = {"share_of_windows": shares}
            for metric in ("mean_z", "sd_z", "abs_z_over_2"):
                stats[metric] = {w: s[metric] for w, s in per_world.items()}
            for metric, units in stats.items():
                out.append(
                    _row(
                        seed_of,
                        units,
                        len(g),
                        question="Q9 diagnostics",
                        metric=metric,
                        horizon_days=int(h),
                        split=split,
                        role="all",
                        vertical=vertical,
                    )
                )
    return out


def empirical_tail(z: float, null_z: np.ndarray) -> float | None:
    """Share of the node's quiet windows at least as extreme as `z`."""
    if not len(null_z):
        return None
    return float((np.abs(null_z) >= abs(z)).mean())


def detection_rows(rows, calibration, worlds, seed_of, vertical) -> list[dict]:
    det, null = _usable(rows), _usable(calibration)
    out: list[dict] = []
    if det.empty:
        return out
    null_world = {(w["seed"], w["regime"]): wid for wid, w in worlds.items() if w["kind"] == "null"}
    tails, n_null = [], []
    for r in det.itertuples():
        ref = null_world.get((r.seed, r.regime))
        quiet = (
            null[
                (null.world == ref) & (null.node == r.node) & (null.horizon_days == r.horizon_days)
            ]
            if ref is not None and not null.empty
            else pd.DataFrame({"z": []})
        )
        tails.append(empirical_tail(r.z, quiet["z"].to_numpy(float)))
        n_null.append(len(quiet))
    det = det.assign(empirical_tail=tails, n_null=n_null, abs_z=det["z"].abs())
    # One unit per world: where an anomaly has several truth nodes, the one
    # with the strongest score.
    det = det.sort_values("abs_z", ascending=False).drop_duplicates(["world", "horizon_days"])
    for (h, role), g in _with_all(det).groupby(["horizon_days", "role"]):
        units = {
            "abs_z_at_onset": dict(zip(g.world, g.abs_z)),
            "empirical_tail": dict(zip(g.world, g.empirical_tail)),
            "empirical_tail_under_5pct": {
                w: None if t is None or pd.isna(t) else float(t < 0.05)
                for w, t in zip(g.world, g.empirical_tail)
            },
            "null_windows_per_node": dict(zip(g.world, g.n_null.astype(float))),
        }
        for metric, u in units.items():
            out.append(
                _row(
                    seed_of,
                    u,
                    len(g),
                    question="Q2 detection",
                    metric=metric,
                    horizon_days=int(h),
                    role=role,
                    vertical=vertical,
                )
            )
    return out


def localization_rows(rows, seed_of, vertical) -> list[dict]:
    df = pd.DataFrame(rows)
    out: list[dict] = []
    if df.empty or "method" not in df:
        return out
    df = df[df["method"].notna()].copy()
    ranked = df["rank"].notna()
    df["top1"] = (df["rank"] == 1).astype(float)
    df["top3"] = (df["rank"] <= 3).astype(float)
    df["reciprocal_rank"] = np.where(ranked, 1.0 / df["rank"].where(ranked, 1.0), 0.0)
    first_z = pd.to_numeric(df["first_abs_z"], errors="coerce")
    # A first place the reader has no reason to trust: its own |z| under 2.
    df["first_place_lacks_evidence"] = (~(first_z >= EVIDENCE_Z)).astype(float)
    df["top1_with_evidence"] = ((df["rank"] == 1) & (first_z >= EVIDENCE_Z)).astype(float)
    # How many nodes the root was ranked among: first of one is not a finding.
    df["candidates"] = df["n_ranked"].astype(float)
    metrics = (
        "candidates",
        "top1",
        "top3",
        "reciprocal_rank",
        "top1_with_evidence",
        "first_place_lacks_evidence",
    )
    for (method, role), g in _with_all(df).groupby(["method", "role"]):
        for metric in metrics:
            out.append(
                _row(
                    seed_of,
                    dict(zip(g.world, g[metric])),
                    len(g),
                    question="Q3 localization",
                    metric=metric,
                    method=method,
                    role=role,
                    vertical=vertical,
                )
            )
    return out


def cost_rows(fits, analyses, seed_of, vertical) -> list[dict]:
    out: list[dict] = []
    df = pd.DataFrame(fits)
    if not df.empty:
        # Per-fit cost is read off the null worlds' fits: every non-formula
        # node on the same windows in every run, so one scoreboard's numbers
        # are comparable with another's. A single world's fits are whichever
        # nodes its anomaly's scope happened to hold; their cost is reported
        # per analysis below.
        df = df[df["ok"].astype(bool) & (df["purpose"] == "calibration")].copy()
    if not df.empty:
        div = pd.to_numeric(df["divergences"], errors="coerce")
        df["ess_under_400"] = (pd.to_numeric(df["min_ess"], errors="coerce") < 400).astype(float)
        df["any_divergence"] = (div > 0).astype(float)
        df["suspect"] = (df["fit_quality"] == "suspect").astype(float)
        for role, g in _with_all(df).groupby("role"):
            by_world = g.groupby("world")
            units = {
                "fit_seconds_median": by_world["fit_seconds"].median().to_dict(),
                "ess_under_400": by_world["ess_under_400"].mean().to_dict(),
                "any_divergence": by_world["any_divergence"].mean().to_dict(),
                "suspect": by_world["suspect"].mean().to_dict(),
            }
            for metric, u in units.items():
                out.append(
                    _row(
                        seed_of,
                        u,
                        len(g),
                        question="Q8 cost",
                        metric=metric,
                        role=role,
                        vertical=vertical,
                    )
                )
    an = pd.DataFrame(analyses)
    if not an.empty:
        for metric in ("innovation_seconds", "rca_seconds", "fits_made"):
            out.append(
                _row(
                    seed_of,
                    dict(zip(an.world, an[metric].astype(float))),
                    len(an),
                    question="Q8 cost",
                    metric=f"analysis_{metric}",
                    role="all",
                    vertical=vertical,
                )
            )
    return out


ATTRIBUTION_METRICS = (
    "abs_log_error",
    "log_error",
    "ratio",
    "coverage",
    "withheld",
    "wrong_sign",
    "error_share",
    "abs_error_share",
)
Q5 = "Q5 counterfactual attribution"


def attribution_rows(rows, seed_of, vertical) -> tuple[list[dict], list[dict]]:
    """`(scoreboard rows, eligibility table)` for Q5.

    Items are grouped across the run's seeds by anomaly and item (an edge on
    the path, or the root); `attribution.eligibility` decides whether the set
    can ground the item's size. Per world, each metric is the mean over that
    world's size-eligible items; `items_*` count them, so a world with nothing
    eligible is a 0 and not a gap."""
    ok = [r for r in rows if r.get("status") == "ok" and r.get("item")]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in ok:
        groups[(r["anomaly"], r["item"])].append(r)
    table, scored = [], []
    verdicts = {key: eligibility(g) for key, g in groups.items()}
    changes = target_changes(groups, verdicts)
    for (anomaly, item), g in sorted(groups.items()):
        verdict = verdicts[(anomaly, item)]
        table.append(
            {
                "anomaly": anomaly,
                "item": item,
                "kind": g[0]["kind"],
                **{
                    k: verdict[k]
                    for k in (
                        "verdict",
                        "truth_basis",
                        "n_seeds",
                        "rel_mean",
                        "fraction",
                        "precision",
                    )
                },
                "aligned_worlds": verdict["aligned_worlds"],
                "eligible": verdict["eligible"],
                "why": verdict["why"],
            }
        )
        for s in score_items(g, verdict, changes):
            scored.append({**s, "kind": g[0]["kind"], "eligible": verdict["eligible"]})
    out: list[dict] = []
    worlds = sorted({r["world"] for r in rows})
    if not worlds:
        return out, table
    df = pd.DataFrame(scored) if scored else pd.DataFrame(columns=["world", "kind", "eligible"])
    for col in (
        "ratio",
        "log_error",
        "covered",
        "wrong_sign",
        "direction_agrees",
        "withheld",
        "error_share",
    ):
        if col not in df:
            df[col] = None
    df["abs_log_error"] = pd.to_numeric(df["log_error"], errors="coerce").abs()
    df["abs_error_share"] = pd.to_numeric(df["error_share"], errors="coerce").abs()
    df["coverage"] = df["covered"].map(lambda v: None if v is None or v != v else float(v))
    df["withheld"] = df["withheld"].map(lambda v: float(bool(v) and v == v))
    df["wrong_sign"] = df["wrong_sign"].map(lambda v: None if v is None or v != v else float(v))
    df["direction_agrees"] = df["direction_agrees"].map(
        lambda v: None if v is None or v != v else float(v)
    )

    def per_world(frame, col, how="mean"):
        units = {}
        for w in worlds:
            vals = pd.to_numeric(frame.loc[frame["world"] == w, col], errors="coerce").dropna()
            units[w] = None if vals.empty else float(getattr(vals, how)())
        return units

    eligible = df[df["eligible"].astype(bool)] if len(df) else df
    counts = {
        "items_size_eligible": eligible,
        "items_direction_only": df[df["direction_agrees"].notna()] if len(df) else df,
        "items_total": df,
    }
    for metric, frame in counts.items():
        units = {w: float((frame["world"] == w).sum()) if len(frame) else 0.0 for w in worlds}
        out.append(
            _row(
                seed_of,
                units,
                len(frame),
                question=Q5,
                metric=metric,
                role="all",
                vertical=vertical,
            )
        )
    kinds = ["all", *sorted(set(eligible["kind"]))] if len(eligible) else []
    for kind in kinds:
        frame = eligible if kind == "all" else eligible[eligible["kind"] == kind]
        for metric in ATTRIBUTION_METRICS:
            how = "median" if metric == "ratio" else "mean"
            n = int(pd.to_numeric(frame[metric], errors="coerce").notna().sum())
            out.append(
                _row(
                    seed_of,
                    per_world(frame, metric, how),
                    n,
                    question=Q5,
                    metric=metric,
                    role=kind,
                    vertical=vertical,
                )
            )
    direction = df[df["direction_agrees"].notna()] if len(df) else df
    if len(direction):
        out.append(
            _row(
                seed_of,
                per_world(direction, "direction_agrees"),
                len(direction),
                question=Q5,
                metric="direction_agrees",
                role="all",
                vertical=vertical,
            )
        )
    return out, table


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
STAMP_KEYS = (
    "tier engine_commit engine_key generator_version manifest vertical company tree seeds "
    "held_out held_out_seeds set_seeds regimes horizons created host verdict_column verdict_level "
    "allow_schema_v1"
).split()


def build_scoreboard(run_dir: str | Path) -> dict[str, Any]:
    """One run directory -> its scoreboard. Only rows of the run's own jobs
    are read: rows left by an earlier configuration stay on disk, unused."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    wanted = {j["job"] for j in run["jobs"]}
    done = {r["job"]: r for r in read_rows(run_dir, "done") if r["job"] in wanted}

    def rows(row_type):
        seen, out = set(), []
        for r in read_rows(run_dir, row_type):
            ident = json.dumps(r, sort_keys=True, default=str)
            if r["job"] in done and ident not in seen:
                seen.add(ident)
                out.append(r)
        return out

    worlds = run["worlds"]
    seed_of = {wid: w["seed"] for wid, w in worlds.items()}
    vertical = run["vertical"]
    calibration, localization = rows("calibration"), rows("localization")
    skipped = rows("skipped")
    attribution = rows("attribution")
    q5_rows, eligibility_table = attribution_rows(attribution, seed_of, vertical)
    board_rows = [
        *calibration_rows(calibration, seed_of, vertical),
        *detection_rows(rows("detection"), calibration, worlds, seed_of, vertical),
        *localization_rows(localization, seed_of, vertical),
        *q5_rows,
        *cost_rows(rows("fits"), rows("analysis"), seed_of, vertical),
    ]
    statuses = pd.Series([r.get("status") for r in calibration], dtype=object).value_counts()
    single_ids = [wid for wid, w in worlds.items() if w["kind"] == "single"]
    scored = {r["world"] for r in localization if r.get("method")}
    return {
        "scoreboard_version": SCOREBOARD_VERSION,
        **{k: run.get(k) for k in STAMP_KEYS},
        "windows": {
            w: {"spacing_days": g["spacing_days"], "n": len(g["starts"])}
            for w, g in run["windows"].items()
        },
        "singles": run["singles"],
        "singles_not_run": run["singles_not_run"],
        "worlds": worlds,
        "coverage": {
            "jobs": len(run["jobs"]),
            "jobs_done": len(done),
            "job_seconds": round(sum(d["seconds"] for d in done.values()), 1),
            "calibration_status": {str(k): int(v) for k, v in statuses.items()},
            "single_worlds": len(single_ids),
            "single_worlds_ranked": len(scored),
            "single_worlds_off_tree": sorted(
                r["world"] for r in localization if r.get("status") == "off_tree"
            ),
            "single_worlds_no_target": sorted(
                r["world"] for r in localization if r.get("status") == "no_target"
            ),
            "single_worlds_skipped": {r["world"]: r["reason"] for r in skipped},
            # Each ranked world's target and what said it moved.
            "targets": {
                r["world"]: {
                    "anomaly": r.get("anomaly"),
                    "truth": r.get("truth"),
                    "mapping": r.get("mapping"),
                    "target": r.get("target"),
                    "basis": r.get("target_basis"),
                }
                for r in localization
                if r.get("method") == "innovation_contribution"
            },
        },
        "counterfactual_attribution": {
            "truth": "the twin difference (single world against its null twin); the engine "
            "answers window over window, so a formula edge carrying its parents' drift since "
            "the reference window is expected to exceed it",
            # Devon's decision (2026-11-21): the truth averages every seed of
            # the set, held-out seeds included. Ground truth, not engine output.
            "truth_seeds": run.get("set_seeds"),
            "truth_includes_held_out_seeds": any(
                s not in DEV_SEEDS for s in (run.get("set_seeds") or [])
            ),
            "max_rel_precision": MAX_REL_PRECISION,
            "eligibility": eligibility_table,
            "errors": sorted(
                {r["world"]: r["status"] for r in attribution if r.get("status") != "ok"}.items()
            ),
        },
        "not_built": NOT_BUILT,
        "rows": board_rows,
    }


def merge_scoreboards(boards: list[dict[str, Any]]) -> dict[str, Any]:
    """Several verticals' scoreboards as one: rows are concatenated (each keeps
    its vertical) and a `vertical: all` row is added per metric, over every
    seed of every vertical. Stamps are kept per vertical under `runs`."""
    if len(boards) == 1:
        return boards[0]
    merged: dict[tuple, dict] = {}
    seed_of: dict[str, Any] = {}
    for b in boards:
        for wid, w in b["worlds"].items():
            seed_of[wid] = (b["vertical"], w["seed"])
        for r in b["rows"]:
            key = tuple(r[k] for k in ROW_KEY[:-1])
            slot = merged.setdefault(key, {"units": {}, "n": 0})
            slot["units"].update(r["units"])
            slot["n"] += r["n"]
    rows = [r for b in boards for r in b["rows"]]
    for key, slot in merged.items():
        rows.append(
            _row(seed_of, slot["units"], slot["n"], **dict(zip(ROW_KEY[:-1], key)), vertical="all")
        )
    return {
        "scoreboard_version": SCOREBOARD_VERSION,
        "tier": boards[0]["tier"],
        "runs": [{k: v for k, v in b.items() if k != "rows"} for b in boards],
        "worlds": {wid: w for b in boards for wid, w in b["worlds"].items()},
        "not_built": NOT_BUILT,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
def diff_scoreboards(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """`b - a` on the worlds both scored, paired by seed.

    Per row present in both: each seed's mean over its shared worlds in `a`
    and in `b`, the mean of the per-seed differences, and their spread."""
    # A seed is a seed of one company and regime: world ids start with the slug.
    seed_of = {
        wid: (wid.split(".")[0], w.get("regime"), w["seed"]) for wid, w in a["worlds"].items()
    }
    index_b = {tuple(r[k] for k in ROW_KEY): r for r in b["rows"]}
    out = []
    for ra in a["rows"]:
        key = tuple(ra[k] for k in ROW_KEY)
        rb = index_b.get(key)
        if rb is None:
            continue
        shared = [
            w
            for w in ra["units"]
            if w in rb["units"] and ra["units"][w] is not None and rb["units"][w] is not None
        ]
        by_seed: dict[Any, list[tuple[float, float]]] = defaultdict(list)
        for w in shared:
            by_seed[seed_of[w]].append((ra["units"][w], rb["units"][w]))
        pairs = [(np.mean([x for x, _ in v]), np.mean([y for _, y in v])) for v in by_seed.values()]
        deltas = [y - x for x, y in pairs]
        out.append(
            {
                **{k: ra[k] for k in ROW_KEY},
                "a": float(np.mean([x for x, _ in pairs])) if pairs else None,
                "b": float(np.mean([y for _, y in pairs])) if pairs else None,
                "difference": float(np.mean(deltas)) if deltas else None,
                "spread": float(np.std(deltas, ddof=1)) if len(deltas) > 1 else None,
                "n_seeds": len(deltas),
                "n_worlds": len(shared),
            }
        )
    keys_a = {tuple(r[k] for k in ROW_KEY) for r in a["rows"]}
    return {
        "a": _stamp(a),
        "b": _stamp(b),
        "shared_worlds": sorted(set(a["worlds"]) & set(b["worlds"])),
        "only_in_a": sorted(set(a["worlds"]) - set(b["worlds"])),
        "only_in_b": sorted(set(b["worlds"]) - set(a["worlds"])),
        "rows_only_in_a": len(keys_a - set(index_b)),
        "rows_only_in_b": len(set(index_b) - keys_a),
        "rows": out,
    }


def _stamp(board: dict[str, Any]) -> dict[str, Any]:
    keys = ("tier", "engine_commit", "generator_version", "seeds", "held_out", "created")
    stamp = {k: board.get(k) for k in keys}
    stamp["tree"] = (board.get("tree") or {}).get("path")
    stamp["tree_sha256"] = (board.get("tree") or {}).get("effective_sha256")
    return stamp


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _fmt(v, digits=2) -> str:
    if v is None:
        return ""
    return f"{v:.{digits}f}"


def _cell(row: dict | None, digits=2, pct=False) -> str:
    if row is None or row["value"] is None:
        return ""
    scale = 100 if pct else 1
    unit = "%" if pct else ""
    d = 0 if pct else digits
    text = f"{row['value'] * scale:.{d}f}{unit}"
    if row["spread"] is not None:
        text += f" ± {row['spread'] * scale:.{d}f}"
    return text


ROLE_ORDER = ["all", "rate", "flow, no parents", "flow, learned parent", "stock", "formula"]


def _table(header: list[str], lines: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(line) + " |" for line in lines]
    return out + [""]


def _pick(rows, **want) -> dict[tuple, dict]:
    return {
        (r["role"], r["metric"]): r for r in rows if all(r.get(k) == v for k, v in want.items())
    }


def _roles(index) -> list[str]:
    roles = {role for role, _ in index}
    return [r for r in ROLE_ORDER if r in roles] + sorted(roles - set(ROLE_ORDER))


def _wall(run: dict[str, Any]) -> str:
    calls = run.get("invocations") or []
    if not calls:
        return ""
    total = sum(c["wall_seconds"] for c in calls) / 60
    shards = sorted({c["shards"] for c in calls})
    return f"; wall time {total:.0f} min over {len(calls)} invocation(s) on {shards} shard(s)"


def _unscored(run: dict[str, Any]) -> str:
    statuses = run["coverage"]["calibration_status"]
    scored = sum(n for s, n in statuses.items() if s == "ok" or s.startswith("ok "))
    # One line per reason: the same failure reads differently in each world
    # after the node's name (how many periods, which dates).
    rest: dict[str, int] = defaultdict(int)
    for status, n in statuses.items():
        if not (status == "ok" or status.startswith("ok ")):
            rest[status.split("' on ")[0][:110]] += n
    text = f"- Calibration windows: {scored} scored"
    for status, n in sorted(rest.items(), key=lambda kv: -kv[1]):
        text += f"; {n} not scored ({status}')"
    return text


def _targets(run: dict[str, Any]) -> list[str]:
    """One line per anomaly: where its root lands and which target each seed got."""
    by_anomaly: dict[str, list] = {}
    for world, t in sorted(run["coverage"].get("targets", {}).items()):
        by_anomaly.setdefault(t["anomaly"], []).append((run["worlds"][world]["seed"], t))
    out = []
    for anomaly, seeds in sorted(by_anomaly.items()):
        first = seeds[0][1]
        picks = ", ".join(f"s{seed}: `{t['target']}` ({t['basis']})" for seed, t in seeds)
        out.append(f"- `{anomaly}`: root {first['truth']} ({first['mapping']}); target {picks}")
    return out


def render_markdown(board: dict[str, Any], title: str | None = None) -> str:
    runs = board.get("runs") or [board]
    lines = [f"# {title or 'Evaluation scoreboard'}", ""]
    if board.get("note"):
        lines += [f"> {board['note']}", ""]
    for run in runs:
        tree = run["tree"]
        lines += [
            f"**Tier:** {run['tier']} · **Vertical:** {run['vertical']} ({run['company']}) · "
            f"**Seeds:** {run['seeds']}"
            + (f" · **HELD-OUT seeds scored:** {run['held_out_seeds']}" if run["held_out"] else "")
            + f" · **Regimes:** {run['regimes']} · **Horizons (days):** {run['horizons']}",
            "",
            f"- Verdict column `{run.get('verdict_column') or 'verdict'}`"
            + (
                f" (two-sided {run['verdict_level']:.0%})"
                if run.get("verdict_level") is not None
                else ""
            )
            + " decides targets and attribution eligibility"
            + (
                "; **a loose verdict: about one in ten pairs with no effect is established "
                "by chance**"
                if (run.get("verdict_column") or "verdict") != "verdict"
                else ""
            )
            + f"; world-set schema_version {run['manifest'].get('schema_version')}"
            + (
                " (**version 1, read with --allow-schema-v1**)"
                if run.get("allow_schema_v1")
                else ""
            ),
            f"- Engine commit `{run['engine_commit']}`; generator version "
            f"`{run['generator_version']}`; manifest `config_sha256` "
            f"`{run['manifest']['config_sha256']}` ({run['manifest']['config']})",
            f"- Tree `{tree['path']}` (sha256 `{tree['sha256'][:16]}`, "
            + ("canonical" if tree.get("canonical") else "**not a canonical evaluation tree**")
            + (f", bindings `{tree['bindings_path']}`" if tree.get("bindings_path") else "")
            + f"); {len(tree['served'])} nodes served"
            + (f", dropped: {tree['dropped']}" if tree["dropped"] else "")
            + (
                f", derived instead of fetched: {tree['derived_instead_of_fetched']}"
                if tree["derived_instead_of_fetched"]
                else ""
            ),
            f"- Run {run['created']}; {run['coverage']['jobs_done']} of {run['coverage']['jobs']} "
            f"jobs done, {run['coverage']['job_seconds'] / 60:.0f} process-minutes; load "
            f"average at plan {run['host']['load_at_plan']} on {run['host']['cpus']} cores"
            + _wall(run),
            "- Null windows per world: "
            + (
                ", ".join(
                    sorted(
                        {
                            f"{g['n']} every {g['spacing_days']} days"
                            for g in run["windows"].values()
                        }
                    )
                )
                or "none"
            ),
            _unscored(run),
            f"- Single worlds: {run['coverage']['single_worlds_ranked']} ranked of "
            f"{run['coverage']['single_worlds']}; off-tree "
            f"{len(run['coverage']['single_worlds_off_tree'])}; skipped "
            f"{len(run['coverage']['single_worlds_skipped'])}",
            *_targets(run),
            *[
                f"- Served with a caveat: `{n}`: {why}"
                for n, why in tree.get("caveats", {}).items()
            ],
            "",
        ]
    lines += [
        "A cell is the mean over seeds ± the standard deviation across seeds. "
        "`n` counts windows, worlds or fits.",
        "",
    ]
    verticals = sorted({r["vertical"] for r in board["rows"]}, key=lambda v: (v != "all", v))
    for vertical in verticals:
        rows = [r for r in board["rows"] if r["vertical"] == vertical]
        if len(verticals) > 1:
            lines += [f"## Vertical: {vertical}", ""]
        lines += _render_vertical(rows)
    for run in runs:
        lines += _render_eligibility(run)
    lines += ["## Not scored yet", ""]
    lines += [f"- **{q}:** {why}" for q, why in board["not_built"].items()]
    lines += [
        "",
        "---",
        "",
        "*Generated by `knowledge/benchmarks/evaluate.py`. "
        "This document is written and maintained by an AI agent (Claude), "
        "with human oversight.*",
        "",
    ]
    return "\n".join(lines)


def _render_vertical(rows: list[dict]) -> list[str]:
    out: list[str] = []
    horizons = sorted({r["horizon_days"] for r in rows if r["horizon_days"] is not None})

    calibrated = any(r["question"] == "Q1 calibration" for r in rows)
    out += [
        "### Q1 Calibration on null worlds",
        "",
        "Calibrated: mean z 0, sd z 1, 4.6% of windows beyond 2, PIT distance 0, "
        "lag-1 correlation 0."
        if calibrated
        else "No null windows were scored on this run (its tier scores none).",
        "",
    ]
    for h in horizons:
        index = _pick(rows, question="Q1 calibration", horizon_days=h, split=None)
        if not index:
            continue
        body = []
        for role in _roles(index):
            g = lambda m: index.get((role, m))  # noqa: E731
            body.append(
                [
                    role,
                    str(g("mean_z")["n"]),
                    _cell(g("mean_z")),
                    _cell(g("sd_z")),
                    _cell(g("abs_z_over_2"), pct=True),
                    _cell(g("abs_z_over_2_centred"), pct=True),
                    _cell(g("pit_ks")),
                    _cell(g("lag1_z")),
                ]
            )
        out += [f"**{h}-day horizon**", ""]
        out += _table(
            [
                "Role",
                "n",
                "mean z",
                "sd z",
                "\\|z\\|>2",
                "\\|z\\|>2 centred",
                "PIT KS",
                "lag-1 corr of z",
            ],
            body,
        )

    q9 = [r for r in rows if r["question"] == "Q9 diagnostics"]
    if q9:
        out += ["### Q9 Calibration split by the engine's own flags", ""]
        body = []
        for h in horizons:
            for split in sorted({r["split"] for r in q9 if r["horizon_days"] == h}):
                index = _pick(q9, horizon_days=h, split=split)
                g = lambda m: index.get(("all", m))  # noqa: E731
                body.append(
                    [
                        str(h),
                        split,
                        str(g("mean_z")["n"]),
                        _cell(g("share_of_windows"), pct=True),
                        _cell(g("mean_z")),
                        _cell(g("sd_z")),
                        _cell(g("abs_z_over_2"), pct=True),
                    ]
                )
        out += _table(
            ["Horizon", "Flag", "n", "share of windows", "mean z", "sd z", "\\|z\\|>2"], body
        )

    det = [r for r in rows if r["question"] == "Q2 detection"]
    if det:
        out += [
            "### Q2 Detection: the root's own score at onset",
            "",
            "Empirical tail: share of the same node's null-world windows (same seed, same "
            "horizon) at least as extreme. Its resolution is one over the null windows "
            "per node.",
            "",
        ]
        body = []
        for h in horizons:
            index = _pick(det, horizon_days=h)
            for role in _roles(index):
                g = lambda m: index.get((role, m))  # noqa: E731
                body.append(
                    [
                        str(h),
                        role,
                        str(g("abs_z_at_onset")["n"]),
                        _cell(g("abs_z_at_onset")),
                        _cell(g("empirical_tail")),
                        _cell(g("empirical_tail_under_5pct"), pct=True),
                        _cell(g("null_windows_per_node"), digits=0),
                    ]
                )
        out += _table(
            [
                "Horizon",
                "Root role",
                "worlds",
                "\\|z\\| at onset",
                "empirical tail",
                "tail < 5%",
                "null windows per node",
            ],
            body,
        )

    loc = [r for r in rows if r["question"] == "Q3 localization"]
    if loc:
        out += [
            "### Q3 Localization: rank of the true root",
            "",
            f"A first place lacks evidence when its own \\|z\\| is under {EVIDENCE_Z:g}. "
            "Nodes ranked is how many candidates the root was ranked among: where the "
            "anomaly reaches only one node below its root, first of one is not a finding.",
            "",
        ]
        body = []
        methods = [
            "innovation_contribution",
            "innovation_evidence",
            "engine_ranked_causes",
            "naive_relative_change",
        ]
        for method in methods:
            index = _pick(loc, method=method)
            for role in _roles(index):
                g = lambda m: index.get((role, m))  # noqa: E731
                body.append(
                    [
                        method,
                        role,
                        str(g("top1")["n"]),
                        _cell(g("candidates"), digits=1),
                        _cell(g("top1"), pct=True),
                        _cell(g("top3"), pct=True),
                        _cell(g("reciprocal_rank")),
                        _cell(g("top1_with_evidence"), pct=True),
                        _cell(g("first_place_lacks_evidence"), pct=True),
                    ]
                )
        out += _table(
            [
                "Method",
                "Root role",
                "worlds",
                "nodes ranked",
                "top-1",
                "top-3",
                "mean reciprocal rank",
                "top-1 with evidence",
                "first place lacks evidence",
            ],
            body,
        )

    out += _render_attribution(rows)

    cost = [r for r in rows if r["question"] == "Q8 cost"]
    if cost:
        out += [
            "### Q8 Cost",
            "",
            "Per fit, over the null worlds' fits. Wall seconds were taken on a shared "
            "machine: read them as ratios between roles and between scoreboards, not as "
            "absolutes.",
            "",
        ]
        index = _pick(cost)
        body = []
        for role in _roles({k for k in index if k[1] == "fit_seconds_median"}):
            g = lambda m: index.get((role, m))  # noqa: E731
            body.append(
                [
                    role,
                    str(g("fit_seconds_median")["n"]),
                    _cell(g("fit_seconds_median"), digits=1),
                    _cell(g("ess_under_400"), pct=True),
                    _cell(g("any_divergence"), pct=True),
                    _cell(g("suspect"), pct=True),
                ]
            )
        if body:
            out += _table(
                ["Role", "fits", "median fit seconds", "ESS < 400", "any divergence", "suspect"],
                body,
            )
        g = lambda m: index.get(("all", m))  # noqa: E731
        if g("analysis_innovation_seconds"):
            out += [
                f"Per single-world analysis: innovation ranking "
                f"{_cell(g('analysis_innovation_seconds'), digits=0)} s, engine RCA "
                f"{_cell(g('analysis_rca_seconds'), digits=0)} s (after the fits the innovation "
                f"pass already made), {_cell(g('analysis_fits_made'), digits=0)} fits.",
                "",
            ]
    return out


def _render_attribution(rows: list[dict]) -> list[str]:
    q5 = [r for r in rows if r["question"] == Q5]
    if not q5:
        return []
    index = _pick(q5)
    g = lambda role, m: index.get((role, m))  # noqa: E731
    out = [
        "### Q5 Counterfactual attribution: attributed sizes against the twin difference",
        "",
        "Over the anomaly's declared window, each edge on the path from the root to the "
        "target (the engine's contribution of the parent to the child's change, with its 95% "
        "interval) and the root (the part of its change the engine leaves unexplained, or "
        "for a root with no parents its change itself; no interval), against the "
        "counterfactual: what the anomaly changed, single world against its null twin. The "
        "engine answers a different question, window over window, so a formula edge that "
        "carries its parents' drift since the reference window is expected to exceed the "
        "counterfactual; that excess is the gap innovation-based RCA (branch "
        "design/innovation-rca) aims to close. Only items whose size the world set can "
        "ground are scored: set verdict established and the set's cross-seed mean precise "
        f"to ±{MAX_REL_PRECISION:.0%} at its own seed count (the eligibility table says why "
        "each other item is not). Log error is ln(engine / truth); error share is "
        "(engine − truth) / |the target's measured counterfactual change|; a withheld "
        "estimate counts as withheld, never as zero.",
        "",
    ]
    counts = [
        _cell(g("all", "items_size_eligible"), digits=1),
        _cell(g("all", "items_direction_only"), digits=1),
        _cell(g("all", "items_total"), digits=1),
    ]
    out += _table(
        ["size-eligible items per world", "direction-only items per world", "items per world"],
        [counts],
    )
    body = []
    for role in _roles({k for k in index if k[1] == "abs_log_error"}):
        body.append(
            [
                role,
                str(g(role, "abs_log_error")["n"]),
                _cell(g(role, "ratio")),
                _cell(g(role, "log_error")),
                _cell(g(role, "abs_log_error")),
                _cell(g(role, "coverage"), pct=True),
                _cell(g(role, "withheld"), pct=True),
                _cell(g(role, "wrong_sign"), pct=True),
                _cell(g(role, "error_share")),
                _cell(g(role, "abs_error_share")),
            ]
        )
    if body:
        out += _table(
            [
                "Item",
                "scored",
                "median ratio",
                "mean log error",
                "mean \\|log error\\|",
                "interval covers truth",
                "withheld",
                "wrong sign",
                "mean error share",
                "mean \\|error share\\|",
            ],
            body,
        )
    else:
        out += ["No item's size could be scored on this run: see the eligibility table.", ""]
    if g("all", "direction_agrees"):
        out += [
            "Direction only (verdict established, size not scorable: see the table): the engine's "
            f"sign agrees with the measured one in {_cell(g('all', 'direction_agrees'), pct=True)} "
            f"of {g('all', 'direction_agrees')['n']} items.",
            "",
        ]
    return out


def _render_eligibility(run: dict[str, Any]) -> list[str]:
    section = run.get("counterfactual_attribution") or run.get("attribution") or {}
    table = section.get("eligibility") or []
    if not table:
        return []
    out = [
        f"### Q5 Which counterfactual attribution sizes the world set can ground "
        f"({run['vertical']})",
        "",
        "Truth averages every seed the set holds"
        + (
            f" ({section.get('truth_seeds')}), **held-out seeds included**, by decision: "
            "it is ground truth, not engine output."
            if section.get("truth_includes_held_out_seeds")
            else f" ({section.get('truth_seeds')})."
        )
        + " Basis is which set effect matches the engine's window value: `rel_change` "
        "(window mean of daily values, for a stock) or `rel_change_aggregated` (the metric's "
        "own rule: Σnum/Σden for a rate).",
        "",
        "Mean relative effect, seeds and precision are the set's, for the item's child, over "
        "the anomaly's declared window: precision is the 95% half-width of the cross-seed "
        "mean as a share of it (`t · sd / √n / |mean|`, t on n − 1 degrees of freedom). "
        "Share is the item's mean fraction of its child's twin difference over the run's "
        "worlds (a formula parent's Shapley share; 1 otherwise). Aligned worlds "
        "are those whose twins were identical until the anomaly; there one twin's difference "
        "is the truth.",
        "",
    ]
    body = [
        [
            f"`{t['anomaly']}`",
            f"`{t['item']}`",
            t["kind"],
            str(t["verdict"]),
            str(t.get("truth_basis") or "—"),
            str(t["n_seeds"]),
            _fmt(t["rel_mean"], 3),
            _fmt(t.get("fraction"), 2),
            "—" if t["precision"] is None else f"±{t['precision']:.0%}",
            str(t["aligned_worlds"]),
            "size"
            if t["eligible"]
            else ("direction" if t["verdict"] in ("established_up", "established_down") else "no"),
            t["why"],
        ]
        for t in table
    ]
    out += _table(
        [
            "Anomaly",
            "Item",
            "Kind",
            "Set verdict",
            "basis",
            "seeds",
            "mean relative effect",
            "share",
            "precision",
            "aligned worlds",
            "scored on",
            "why",
        ],
        body,
    )
    errors = section.get("errors") or []
    out += [f"- Attribution not run on `{w}`: {why}" for w, why in errors]
    return out + ([""] if errors else [])


def render_diff(diff: dict[str, Any]) -> str:
    a, b = diff["a"], diff["b"]
    lines = [
        "# Scoreboard difference (B − A)",
        "",
        f"- **A:** engine `{a['engine_commit']}`, tree `{a['tree']}`, tier {a['tier']}, "
        f"seeds {a['seeds']}, {a['created']}",
        f"- **B:** engine `{b['engine_commit']}`, tree `{b['tree']}`, tier {b['tier']}, "
        f"seeds {b['seeds']}, {b['created']}",
        f"- Shared worlds: {len(diff['shared_worlds'])}; only in A: {len(diff['only_in_a'])}; "
        f"only in B: {len(diff['only_in_b'])}",
    ]
    if a["tree_sha256"] != b["tree_sha256"]:
        lines.append("- **The trees differ.** Every difference below includes that change.")
    lines += [
        "",
        "Differences are paired by seed on the shared worlds; ± is the standard "
        "deviation of the per-seed differences.",
        "",
    ]
    body = []
    for r in diff["rows"]:
        if r["difference"] is None:
            continue
        spread = "" if r["spread"] is None else f" ± {r['spread']:.3f}"
        body.append(
            [
                r["question"],
                r["metric"],
                _fmt(r["horizon_days"], 0),
                r["method"] or "",
                r["split"] or "",
                r["role"],
                r["vertical"],
                _fmt(r["a"], 3),
                _fmt(r["b"], 3),
                f"{r['difference']:+.3f}{spread}",
                str(r["n_seeds"]),
            ]
        )
    lines += _table(
        [
            "Question",
            "Metric",
            "Horizon",
            "Method",
            "Split",
            "Role",
            "Vertical",
            "A",
            "B",
            "B − A",
            "seeds",
        ],
        body,
    )
    return "\n".join(lines)

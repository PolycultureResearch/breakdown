"""Hand-written world sets in the fake_companies manifest format, for the
evaluation runner's tests (`knowledge/benchmarks/evalrunner`).

Written by hand on purpose: the runner must not import the generator, and the
tests must not need it either. The format is the one documented in the
generator's `docs/using.md` ("World sets"): `worlds.json`, one
`<world>.series.parquet` per world (daily; `date`, `metric`, `dimension`,
`value`, `metric_value`, `numerator`, `denominator`), one
`<world>.ground_truth.json`, and for single worlds optionally a
`<world>.effects_summary.parquet` (that twin's stats per slice). A set may
carry `effects_summary.parquet` at its root with a cross-seed `verdict`.

The metric entries follow the contract of the generator's commit 83640ca:
`input_time_aggregation` per side of a ratio and per input of a derived
metric, `formula` as a tree, `measure_agg`, and `has_sides`.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["date", "metric", "dimension", "value", "metric_value", "numerator", "denominator"]


def series_rows(dates, metric, values, *, dimension=None, value=None, num=None, den=None):
    n = len(dates)
    return pd.DataFrame(
        {
            "date": dates,
            "metric": metric,
            "dimension": dimension,
            "value": value,
            "metric_value": np.asarray(values, dtype=float),
            "numerator": np.full(n, np.nan) if num is None else np.asarray(num, dtype=float),
            "denominator": np.full(n, np.nan) if den is None else np.asarray(den, dtype=float),
        }
    )[COLUMNS]


def metric_info(
    name,
    type="simple",
    time_aggregation="sum",
    inputs=None,
    expr=None,
    dims=None,
    *,
    sides=None,
    formula=None,
    measure_agg=None,
    has_sides=None,
):
    """One `metrics[]` entry. `sides` is `input_time_aggregation`; for a
    ratio_of_sums it defaults to both sides summed."""
    if sides is None and time_aggregation == "ratio_of_sums":
        sides = {"numerator": "sum", "denominator": "sum"}
    return {
        "name": name,
        "type": type,
        "fill_nulls_with": 0.0 if time_aggregation == "sum" else None,
        "time_aggregation": time_aggregation,
        "measure_agg": measure_agg,
        "inputs": inputs or {},
        "input_time_aggregation": sides or {},
        "expr": expr,
        "formula": formula,
        "has_sides": (
            (type == "ratio" or time_aggregation == "average") if has_sides is None else has_sides
        ),
        "dimensions": dims or {},
    }


def write_world_set(root: Path, metrics: list[dict], worlds: list[dict], **top) -> Path:
    """`worlds`: dicts with `id`, `seed`, `kind`, `frames` (a list of
    `series_rows`), and optionally `truth` (ground-truth records) and
    `twin_of`. Returns the set's directory."""
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for w in worlds:
        stem = w["id"].split(".", 3)[-1]
        rel = Path("baseline") / f"s{w['seed']}"
        (root / rel).mkdir(parents=True, exist_ok=True)
        pd.concat(w["frames"], ignore_index=True).to_parquet(root / rel / f"{stem}.series.parquet")
        (root / rel / f"{stem}.ground_truth.json").write_text(json.dumps(w.get("truth", [])))
        summary = None
        if w.get("twin_summary") is not None:
            summary = (rel / f"{stem}.effects_summary.parquet").as_posix()
            pd.DataFrame(w["twin_summary"]).to_parquet(root / summary)
        entries.append(
            {
                "id": w["id"],
                "seed": w["seed"],
                "regime": "baseline",
                "overrides": {},
                "kind": w["kind"],
                "anomalies": [t["id"] for t in w.get("truth", [])],
                "twin_of": w.get("twin_of"),
                "path": None,
                "ground_truth": (rel / f"{stem}.ground_truth.json").as_posix(),
                "run_manifest": (rel / f"{stem}.run_manifest.json").as_posix(),
                "config_hash": f"hash-of-{w['id']}",
                "effects": None,
                "effects_summary": summary,
                "series": (rel / f"{stem}.series.parquet").as_posix(),
            }
        )
    if top.get("effects_summary") is not None and not isinstance(top["effects_summary"], str):
        pd.DataFrame(top["effects_summary"]).to_parquet(root / "effects_summary.parquet")
        top = {**top, "effects_summary": "effects_summary.parquet"}
    manifest = {
        "schema_version": 1,
        "generator_version": "fixture",
        "config": "configs/fixture.yaml",
        "config_sha256": "0" * 64,
        "vertical": "fixture_vertical",
        "company_slug": "fixture",
        "format": "parquet",
        "seeds": sorted({w["seed"] for w in worlds}),
        "regimes": ["baseline"],
        "metrics": metrics,
        "worlds": entries,
        **top,
    }
    (root / "worlds.json").write_text(json.dumps(manifest, indent=1))
    return root


# ---------------------------------------------------------------------------
# The arithmetic fixture: four whole weeks whose aggregates are known by hand.
# ---------------------------------------------------------------------------
ARITHMETIC_TREE = """
provider:
  type: local
  project_path: "${NOT_SET_ANYWHERE}"
metrics:
  - {name: orders, source: fx.metrics.orders, grain: week}
  - {name: visits_ab, source: fx.metrics.visits_ab}
  - {name: conversion, source: fx.metrics.conversion, grain: week, kind: rate,
     no_denominator: "fixture"}
  - {name: base, source: fx.metrics.base, grain: week, kind: stock}
  - {name: churn, source: fx.metrics.churn, grain: week, kind: rate, no_denominator: "fixture"}
  - {name: net, source: fx.metrics.net, grain: week}
"""


TICKET_COUNT = 1.0 + np.arange(28) % 7
TICKET_SUM = TICKET_COUNT * (40.0 + 10.0 * (np.arange(28) % 7 >= 4))


def arithmetic_world_set(root: Path) -> Path:
    """28 days from Monday 2024-01-01. Day `i` (0-based):

    - `orders` = i + 1 (a sum);
    - `visits` by `channel`: a = 10, b = 20, c = 30 every day (total 60);
    - `conversion` = signups / visits, a ratio of two sums: 6 / 60 daily;
    - `base` = 100 + i (a stock, a distinct count per day);
    - `churn` = churned / base, a ratio over a stock: churned = 2 daily;
    - `net` = orders - refunds, derived; `refunds` = 1 daily;
    - `ticket`, a mean over rows: `TICKET_COUNT` rows a day summing to
      `TICKET_SUM`, so the weekly mean is not the mean of the daily means;
    - `users`, a distinct count (cannot be rebuilt weekly).
    """
    dates = pd.date_range("2024-01-01", periods=28)
    i = np.arange(28)
    ones = np.ones(28)
    frames = [
        series_rows(dates, "orders", i + 1),
        series_rows(dates, "refunds", ones),
        series_rows(dates, "net", i + 1 - ones),
        series_rows(dates, "visits", 60 * ones),
        series_rows(dates, "visits", 10 * ones, dimension="channel", value="a"),
        series_rows(dates, "visits", 20 * ones, dimension="channel", value="b"),
        series_rows(dates, "visits", 30 * ones, dimension="channel", value="c"),
        series_rows(dates, "signups", 6 * ones),
        series_rows(dates, "conversion", 0.1 * ones, num=6 * ones, den=60 * ones),
        series_rows(dates, "base", 100 + i),
        series_rows(dates, "churned", 2 * ones),
        series_rows(dates, "churn", 2 / (100 + i), num=2 * ones, den=100 + i),
        series_rows(dates, "ticket", TICKET_SUM / TICKET_COUNT, num=TICKET_SUM, den=TICKET_COUNT),
        series_rows(dates, "users", 40 * ones),
    ]
    metrics = [
        metric_info("orders"),
        metric_info("refunds"),
        metric_info(
            "net",
            "derived",
            "derived",
            {"o": "orders", "r": "refunds"},
            "o - r",
            sides={"o": "sum", "r": "sum"},
            formula={"op": "-", "args": [{"metric": "orders"}, {"metric": "refunds"}]},
        ),
        metric_info("visits", dims={"channel": "session__channel"}),
        metric_info("signups"),
        metric_info(
            "conversion",
            "ratio",
            "ratio_of_sums",
            {"numerator": "signups", "denominator": "visits"},
        ),
        metric_info("base", time_aggregation="end_of_period", measure_agg="count_distinct"),
        metric_info("churned"),
        metric_info(
            "churn",
            "ratio",
            "ratio_of_aggregates",
            {"numerator": "churned", "denominator": "base"},
            sides={"numerator": "sum", "denominator": "end_of_period"},
        ),
        metric_info("ticket", time_aggregation="average", measure_agg="average"),
        metric_info("users", time_aggregation="count_distinct", measure_agg="count_distinct"),
    ]
    world = {"id": "fixture.baseline.s0.null", "seed": 0, "kind": "null", "frames": frames}
    return write_world_set(root, metrics, [world])


# ---------------------------------------------------------------------------
# The end-to-end fixture: a small business with one planted level shift.
# ---------------------------------------------------------------------------
E2E_TREE = """
provider: {type: mock}
metrics:
  - name: spend
    source: fx.metrics.spend
  - name: visits
    source: fx.metrics.visits
    parents: [spend]
  - name: signup_rate
    source: fx.metrics.signup_rate
    kind: rate
    denominator: visits
  - name: signups
    source: fx.metrics.signups
    formula: "visits * signup_rate"
    parents: [visits, signup_rate]
"""
E2E_ONSET = "2024-09-02"  # a Monday, 35 weeks in
E2E_END = "2024-09-29"


def _e2e_frames(seed: int, shift: float):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=280)
    t = np.arange(280)
    weekday = 1 + 0.1 * np.sin(2 * np.pi * t / 7)
    spend = 1000 * weekday + rng.normal(0, 30, 280)
    rate = np.full(280, 0.05) + rng.normal(0, 0.002, 280)
    after = (dates >= E2E_ONSET) & (dates <= E2E_END)
    rate = np.where(after, rate * shift, rate)
    visits = np.round(2.0 * spend + rng.normal(0, 40, 280))
    signups = np.round(visits * rate)
    return [
        series_rows(dates, "spend", spend),
        series_rows(dates, "visits", visits),
        series_rows(dates, "signups", signups),
        series_rows(dates, "signup_rate", signups / visits, num=signups, den=visits),
    ]


def e2e_world_set(root: Path) -> Path:
    """One seed: a null world, and its twin with `signup_rate` cut by 40% for
    four weeks. The single world's record carries what the generator's would."""
    metrics = [
        metric_info("spend"),
        metric_info("visits"),
        metric_info("signups"),
        metric_info(
            "signup_rate",
            "ratio",
            "ratio_of_sums",
            {"numerator": "signups", "denominator": "visits"},
        ),
    ]
    effect = {
        "onset": E2E_ONSET,
        "offset": E2E_END,
        "first_diff": E2E_ONSET,
        "moved": True,
        "rel_change": -0.4,
    }
    quiet = {"onset": None, "offset": None, "first_diff": None, "moved": False, "rel_change": 0.0}
    truth = {
        "id": "rate_drop",
        "kind": "rate",
        "type": "level_shift",
        "start": E2E_ONSET,
        "end": E2E_END,
        "root_metric": "signup_rate",
        "root_dimension": None,
        "root_value": None,
        "root_slice": {},
        "measured_effects": {
            "spend": quiet,
            "visits": quiet,
            "signup_rate": effect,
            "signups": effect,
        },
        "root_effect": effect,
        "declared_affected_metrics_confirmed": {
            "moved": ["signups"],
            "moved_in_slice_only": [],
            "not_detected": [],
        },
    }
    worlds = [
        {
            "id": "fixture.baseline.s0.null",
            "seed": 0,
            "kind": "null",
            "frames": _e2e_frames(0, 1.0),
        },
        {
            "id": "fixture.baseline.s0.only.rate_drop",
            "seed": 0,
            "kind": "single",
            "frames": _e2e_frames(0, 0.6),
            "truth": [truth],
            "twin_of": "fixture.baseline.s0.null",
            "twin_summary": [
                {"metric": m, "dimension": None, "value": None, **e}
                for m, e in truth["measured_effects"].items()
            ],
        },
    ]
    verdict = {"spend": "exactly_zero", "visits": "exactly_zero"}
    summary = [
        {
            "regime": "baseline",
            "anomaly": "rate_drop",
            "metric": m,
            "dimension": None,
            "value": None,
            "n_seeds": 1,
            "verdict": verdict.get(m, "established_down"),
        }
        for m in truth["measured_effects"]
    ]
    return write_world_set(root, metrics, worlds, effects_summary=summary)

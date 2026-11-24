"""Slice localization (evaluation spec §5.3, Q4): inside the truth node,
does the engine's slice attribution put the root's slice first?

The generator labels a root with a slice (`root_slice`, e.g.
`{"category": "tees"}`). Where the truth node declares that dimension, the
engine's `slice_attribution` (breakdown/engine/slices.py) is run on it over
the anomaly's declared window, against the default reference window, with
every member served from the world's series file by `WorldFetcher` (the same
week rules as the node's total). Scored per (world, dimension):

- the rank of the root's member among the slices, as the engine orders them
  (by excess in the direction of the gap), with how many were ranked;
- the root member's |z|: its excess over the half-width of its 95% interval
  over 1.96, and whether the first-ranked slice carries evidence (its
  `noise_level` affirmatively False);
- for a sliced rate, whether the engine called the root member's change
  `within` (its own rate moved) or `mix` (its weight moved) the way the
  set's verdicts say: within when the rate's own slice is established,
  mix when only its weight's slice is, `both` when both are (not scored as
  right or wrong), `none` when neither (not scored).

A dimension in the root slice that the node does not declare, or that the
series file cannot serve, is a row with the reason, never a guess.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .truth import ESTABLISHED, Measured, RootMapping


def _z(row: dict[str, Any]) -> float | None:
    ci, excess = row.get("ci_95"), row.get("excess")
    if not ci or excess is None or ci[1] <= ci[0]:
        return None
    return abs(excess) / ((ci[1] - ci[0]) / (2 * 1.96))


def _truth_call(measured: Measured, metric: str, weight_metric: str | None, dim, value):
    rate = measured.verdicts.get((metric, dim, value))
    weight = measured.verdicts.get((weight_metric, dim, value)) if weight_metric else None
    within, mix = rate in ESTABLISHED, weight in ESTABLISHED
    if within and mix:
        return "both", rate, weight
    if within:
        return "within", rate, weight
    if mix:
        return "mix", rate, weight
    return "none", rate, weight


def slice_items(
    lw, record: dict[str, Any], mapping: RootMapping, measured: Measured
) -> list[dict[str, Any]]:
    """One row per (truth node, root-slice dimension) of one world."""
    from breakdown.engine.rca import resolve_reference_window
    from breakdown.engine.slices import slice_attribution
    from breakdown.grains import floor_period

    root_slice = record.get("root_slice") or {}
    out: list[dict[str, Any]] = []
    for node in mapping.truth:
        defn = lw.dag.nodes[node]["definition"]
        binding = lw.plan.served.get(node)
        served_dims = lw.plan.slices.get(node, {})
        by_series = {series_dim: tree_dim for tree_dim, series_dim in served_dims.items()}
        for series_dim, value in root_slice.items():
            row: dict[str, Any] = {
                "node": node,
                "node_kind": defn.kind,
                "dimension": series_dim,
                "root_value": value,
            }
            if series_dim not in by_series:
                declared = [d for d, s in served_dims.items() if s == series_dim] or sorted(
                    defn.dimensions
                )
                row.update(
                    status="not_sliceable",
                    detail=lw.plan.slice_refusals.get(f"{node}.{series_dim}")
                    or f"'{node}' declares no dimension served as '{series_dim}' "
                    f"(declares {declared})",
                )
                out.append(row)
                continue
            tree_dim = by_series[series_dim]
            spec = defn.dimensions[tree_dim]
            grain = defn.grain
            a_start = str(floor_period(pd.Timestamp(record["start"]), grain).date())
            a_end = record.get("end") or str(lw.end.date())
            try:
                r_start, r_end, _ = resolve_reference_window(
                    lw.dag, lw.data, node, a_start, a_end, None, None
                )
                span = (min(r_start, a_start), max(r_end, a_end))
                sliced = lw.fetcher.fetch_metric_sliced(
                    node, spec.source, span[0], span[1], grain, defn.kind
                )
                weight_sliced, weight_metric = None, None
                if defn.kind == "rate":
                    weight_binding = lw.plan.served.get(spec.weight)
                    weight_metric = None if weight_binding is None else weight_binding.metric
                    weight_sliced = lw.fetcher.fetch_metric_sliced(
                        spec.weight, spec.source, span[0], span[1], grain, "flow"
                    )
                result = slice_attribution(
                    defn,
                    tree_dim,
                    sliced,
                    lw.data.series(node),
                    r_start,
                    r_end,
                    a_start,
                    a_end,
                    weight_sliced=weight_sliced,
                )
            except Exception as exc:  # one node's refusal is a row, not the job's
                row.update(status=f"error: {type(exc).__name__}: {exc}"[:240])
                out.append(row)
                continue
            ranked = [s["value"] for s in result["slices"]]
            # The engine orders by excess in the direction of the node's
            # window-over-window gap, so a member that fell while the node
            # grew ranks last. Its rank by |excess| alone is kept beside it,
            # as a contrast, not as the engine's answer.
            by_size = [
                s["value"]
                for s in sorted(
                    result["slices"],
                    key=lambda s: -abs(s["excess"]) if s.get("excess") is not None else 0.0,
                )
            ]
            hit = next((s for s in result["slices"] if s["value"] == value), None)
            first = result["slices"][0] if result["slices"] else None
            row.update(
                status="ok",
                analysis_start=a_start,
                analysis_end=a_end,
                reference_start=r_start,
                reference_end=r_end,
                n_ranked=len(ranked),
                rank=ranked.index(value) + 1 if value in ranked else None,
                rank_by_abs_excess=by_size.index(value) + 1 if value in by_size else None,
                gap_sign=None if result.get("gap") is None else (1 if result["gap"] > 0 else -1),
                first=None if first is None else first["value"],
                first_noise_level=None if first is None else first.get("noise_level"),
                root_abs_z=None if hit is None else _z(hit),
                root_excess=None if hit is None else hit.get("excess"),
                localization=result.get("localization"),
                gap=result.get("gap"),
            )
            if defn.kind == "rate" and hit is not None:
                within, mix = hit.get("within"), hit.get("mix")
                call = None
                if within is not None and mix is not None and (within or mix):
                    call = "within" if abs(within) >= abs(mix) else "mix"
                truth, rate_verdict, weight_verdict = _truth_call(
                    measured, binding.metric, weight_metric, series_dim, value
                )
                row.update(
                    within=within,
                    mix=mix,
                    engine_call=call,
                    truth_call=truth,
                    rate_slice_verdict=rate_verdict,
                    weight_slice_verdict=weight_verdict,
                    verdict_column=measured.verdict_column,
                    call_correct=(
                        None if truth not in ("within", "mix") or call is None else call == truth
                    ),
                )
            out.append(row)
    return out


def finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None

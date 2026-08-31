"""The loading pipeline: tree YAML + provider -> aligned `GrainedData`.

Extracted from `api/main.py` (roadmap grill 2026-08-29 M9), which had grown
into the composition root as well as the route layer — the visible symptom
being `doctor.py` importing `_build_fetcher` from the FastAPI module with a
comment apologising for it (`# lazy: pulls FastAPI`): a CLI connectivity
check depended on the web app. Nothing here knows about HTTP, `TreeState`,
or FastAPI; `api/main.py`'s `load_tree` composes these, and `doctor.py`
calls the same functions to probe the same path the server serves from.

Public names: `doctor.py` and `api/main.py` both depend on them, and an
underscore that two modules import across a boundary is a lie (the same
argument as `engine/stats.py`).
"""

import logging
import os
from typing import Dict

import numpy as np
import pandas as pd

from breakdown.data_fetch import (
    CloudDataFetcher,
    LocalDataFetcher,
    MockDataFetcher,
    WarehouseDataFetcher,
    provider_query_name,
)
from breakdown.formula import eval_formula
from breakdown.grains import GrainedData, build_grained, resample_up
from breakdown.snapshots import SnapshotFetcher, SnapshotStore, resolve_snapshot_dir

logger = logging.getLogger(__name__)


def build_fetcher(provider_cfg, dag, metrics=None):
    if provider_cfg.type == "local":
        return LocalDataFetcher(project_path=provider_cfg.project_path)
    if provider_cfg.type == "cloud":
        return CloudDataFetcher(
            environment_id=provider_cfg.environment_id,
            host=provider_cfg.host,
            token=provider_cfg.token,
        )
    if provider_cfg.type == "dbt":
        # Bindings come from the project's semantic manifest; a node's own
        # `bind:` block overrides it, so a tree can correct what dbt declares
        # without editing the dbt project.
        from breakdown.dbt_provider import fetcher_from_project

        overrides = {
            m.source.split(".")[-1]: m.bind for m in (metrics or []) if m.bind and m.source
        }
        return fetcher_from_project(
            provider_cfg.project_path,
            target=provider_cfg.target,
            profiles_dir=provider_cfg.profiles_dir,
            overrides=overrides,
        )
    if provider_cfg.type == "warehouse":
        metric_sql = {m.name: m.sql for m in (metrics or []) if m.sql}
        # A derived node is never fetched, so it owes no `sql` — the same
        # exemption `fetch_all_metrics` applies, said at build time so the
        # error cannot name a metric nobody will ask for.
        missing = [m.name for m in (metrics or []) if not m.sql and not m.derived]
        if missing:
            raise RuntimeError(
                f"warehouse provider requires `sql` on every metric; missing for: {missing}"
            )
        return WarehouseDataFetcher(
            host=provider_cfg.host,
            http_path=provider_cfg.http_path,
            token=provider_cfg.token,
            metric_sql=metric_sql,
            catalog=provider_cfg.catalog,
            schema=provider_cfg.db_schema,
            profile=provider_cfg.profile,
        )
    return MockDataFetcher(dag=dag)


def wrap_snapshots(fetcher, provider_type: str, tree_path: str, slice_span=None):
    """Wrap the fetcher in the snapshot read-through cache (roadmap 2.4).

    Mock data is already deterministic and free, so only real providers are
    cached. Default directory is tree-adjacent (`.breakdown/snapshots`) so a
    partner repo can commit its snapshots and re-run RCAs from a fresh clone;
    BREAKDOWN_SNAPSHOT_DIR overrides, "off" disables, BREAKDOWN_REFRESH=1
    forces one refetch pass.

    `slice_span` is the loaded data window. Sliced fetches are widened to it
    before being stored, so one snapshot per (metric, dimension) serves every
    analysis window rather than only the ones already run."""
    if provider_type == "mock":
        return fetcher
    # Directory resolution lives in snapshots.py so `doctor` resolves the same
    # one — the two disagreeing about where snapshots live was half of 2.20.
    snapshot_dir = resolve_snapshot_dir(tree_path)
    if snapshot_dir is None:
        return fetcher
    return SnapshotFetcher(
        fetcher,
        SnapshotStore(snapshot_dir),
        refresh=os.environ.get("BREAKDOWN_REFRESH") == "1",
        slice_span=slice_span,
    )


def fetch_all_metrics(parser, fetcher, provider_type, start_date, end_date) -> GrainedData:
    """Fetch every *sourced* metric at its native grain, derive the rest, and
    assemble per-grain frames (metrics inner-join on date only against series
    at the same grain).

    **`source` is the switch** (roadmap 1.11a). A formula node with a source is
    fetched exactly as before, and `check_identities` then compares the
    identity against what came back — cheap, and it catches drift no analysis
    window ever looks at. A formula node *without* one is derived here from its
    parents, in topological order, and is never asked of the provider: that is
    what makes the documented remedy for a rate over true-zero periods actually
    work, since the derived node is precisely the one the provider would have
    refused to gap-fill.
    """
    grain_of: Dict[str, str] = {m.name: m.grain for m in parser.config.metrics}
    kind_of: Dict[str, str] = {m.name: m.kind for m in parser.config.metrics}
    denominator_of: Dict[str, str] = {
        m.name: m.denominator for m in parser.config.metrics if m.denominator
    }
    # The other two states of the same question travel together: a name absent
    # from `denominator_of` and present here has been asked and answered, and
    # one absent from both has not been asked (roadmap 1.11).
    no_denominator_of: Dict[str, str] = {
        m.name: m.no_denominator for m in parser.config.metrics if m.no_denominator
    }
    series: Dict[str, pd.DataFrame] = {}
    for metric in parser.config.metrics:
        if metric.derived:
            continue
        query_name = provider_query_name(provider_type, metric)
        df = fetcher.fetch_metric(
            query_name, start_date, end_date, grain=metric.grain, kind=metric.kind
        )
        df = df.rename(columns={query_name: metric.name})
        series[metric.name] = df[["date", metric.name]]

    # Derived nodes second, in topological order so a derived node whose parent
    # is itself derived still finds its inputs.
    for name in parser.get_topological_order():
        if parser.dag.nodes[name]["definition"].derived:
            series[name] = derive_series(parser.dag, name, series, grain_of, kind_of)

    # Declaration order, which is the frame's column order and therefore what
    # every caller reading `frame.columns` has always seen.
    per_metric = {m.name: series[m.name] for m in parser.config.metrics}
    data = build_grained(per_metric, grain_of, kind_of, denominator_of, no_denominator_of)
    report_undefined_periods(parser, data)
    check_identities(parser, data)
    check_declared_shares(parser, data)
    return data


def derive_series(dag, name: str, per_metric, grain_of, kind_of) -> pd.DataFrame:
    """One derived node's series: `formula(parents)`, period by period, at the
    node's own grain.

    Parents are resampled up by their own kind, exactly as a fit would see
    them, and the periods are the ones every parent covers — an inner join, so
    a period one parent is missing is a period the identity cannot speak about
    rather than one it guesses at. Where the formula is undefined (a zero
    denominator) the result is `NaN`, which is the honest value and travels
    through the rest of the pipeline as an undefined period.
    """
    parents = list(dag.predecessors(name))
    grain = grain_of[name]
    frames = None
    for p in parents:
        s = pd.Series(
            per_metric[p][p].to_numpy(dtype=float),
            index=pd.DatetimeIndex(per_metric[p]["date"]),
            name=p,
        )
        if grain_of[p] != grain:
            s = resample_up(s, grain_of[p], grain, kind_of[p], label=f"'{p}'")
        frames = s.to_frame() if frames is None else frames.join(s, how="inner")
    if frames is None or frames.empty:
        raise RuntimeError(
            f"Derived metric '{name}' has no periods its parents "
            f"{parents} all cover at grain '{grain}', so its series cannot be "
            "computed. Check each parent's date coverage."
        )
    defn = dag.nodes[name]["definition"]
    with np.errstate(divide="ignore", invalid="ignore"):
        values = eval_formula(defn.formula, {p: frames[p].to_numpy(dtype=float) for p in parents})
    values = np.asarray(values, dtype=float)
    # An infinity is not a value either (`x / 0` with a non-zero numerator).
    # Reported as undefined so exactly one representation reaches the pipeline.
    values = np.where(np.isfinite(values), values, np.nan)
    return pd.DataFrame({"date": frames.index, name: values})


def report_undefined_periods(parser, data: GrainedData) -> None:
    """Say which periods have no value, and — where the tree knows enough —
    whether that is a fact or a gap.

    The provider boundary cannot tell the two apart: an undefined rate and an
    unloaded one both arrive as an absent row. Here the denominator's series is
    in hand, so a period whose denominator is **zero** is a genuinely undefined
    rate (`0/0` — nobody churned that week), while one whose denominator is
    non-zero is a *missing* value, which is an ETL question and gets its own,
    louder line. Neither is invented; both are named.
    """
    for name, grain in data.grain_of.items():
        series = data.series(name)
        undefined = pd.DatetimeIndex(series.loc[series[name].isna(), "date"])
        if not len(undefined):
            continue
        weights = data.weights_for(name)
        if weights is None:
            # Same missing classification, two different things to say about
            # it. A rate that declares `no_denominator` has already answered
            # this question, and telling its author to "declare it to find out"
            # is advice that cannot be followed — the reason they wrote is the
            # answer, so quote it back instead of asking again.
            answered = data.no_denominator_of.get(name)
            logger.warning(
                "Metric '%s': %d of %d %s period(s) have no value (%s%s). %s",
                name,
                len(undefined),
                len(series),
                grain,
                ", ".join(str(d.date()) for d in undefined[:5]),
                ", …" if len(undefined) > 5 else "",
                (
                    "It declares `no_denominator` (%s), so no series can "
                    "classify these: an undefined value and a missing one are "
                    "indistinguishable here by construction, not by omission." % answered
                    if answered
                    else "It declares no `denominator`, so breakdown cannot tell "
                    "an undefined rate from a missing one — declare it to find "
                    'out, or `no_denominator: "<why>"` if there is none.'
                ),
            )
            continue
        den = weights.reindex(undefined)
        genuinely = pd.DatetimeIndex(den.index[den.fillna(1.0) == 0.0])
        missing = undefined.difference(genuinely)
        logger.info(
            "Metric '%s': %d of %d %s period(s) are genuinely undefined — its "
            "denominator '%s' is zero there, so there is no rate to report. "
            "They are excluded from window aggregates (which recompute from "
            "components) and make the metric unfittable over any window "
            "containing them.",
            name,
            len(genuinely),
            len(series),
            grain,
            data.denominator_of[name],
        )
        if len(missing):
            logger.warning(
                "Metric '%s': %d %s period(s) have no value even though its "
                "denominator '%s' is non-zero there (%s%s) — that is a missing "
                "value, not an undefined one. Check the source.",
                name,
                len(missing),
                grain,
                data.denominator_of[name],
                ", ".join(str(d.date()) for d in missing[:5]),
                ", …" if len(missing) > 5 else "",
            )


# A fetched formula node whose identity misses the fetched series by more than
# this share of the node's own level, on average, is worth saying so about.
# Generous on purpose: it is a drift alarm, not a tolerance — rounding in a
# warehouse, a late-arriving row, a rate stored to two decimals all move an
# identity by fractions of a percent, and an alarm that fires on those is one
# nobody reads.
IDENTITY_DRIFT = 0.01


def check_identities(parser, data: GrainedData) -> None:
    """Check every fetched formula node against its own identity, at **load**.

    `unexplained` already reports this, but only for the windows somebody
    happens to analyse — an identity that has been drifting since March is
    invisible until an RCA lands on March. This runs once over the whole loaded
    window and costs one vectorized formula evaluation per node.

    Derived nodes are skipped, and the skip is the point: there is nothing to
    check them against. That asymmetry is exactly what `unexplained_status`
    reports downstream.
    """
    for name in parser.get_topological_order():
        defn = parser.dag.nodes[name]["definition"]
        if not defn.formula or defn.derived or defn.lags:
            continue
        parents = list(parser.dag.predecessors(name))
        try:
            frame = data.fit_frame(name, parents, data.grain_of[name])
        except (ValueError, RuntimeError, KeyError) as e:
            logger.info("identity check skipped for '%s': %s", name, e)
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            implied = np.asarray(
                eval_formula(defn.formula, {p: frame[p].to_numpy(dtype=float) for p in parents}),
                dtype=float,
            )
        observed = frame[name].to_numpy(dtype=float)
        usable = np.isfinite(implied) & np.isfinite(observed)
        if not usable.any():
            continue
        scale = float(np.abs(observed[usable]).mean())
        residual = np.abs(observed[usable] - implied[usable])
        drift = float(residual.mean()) / scale if scale else float(residual.mean())
        if drift <= IDENTITY_DRIFT:
            continue
        worst = np.argsort(residual)[::-1][:3]
        dates = pd.DatetimeIndex(frame.loc[usable, "date"].to_numpy())
        logger.warning(
            "Metric '%s': the fetched series departs from its own identity "
            "'%s' by %.1f%% of its level on average over the loaded window "
            "(worst periods: %s). The identity and the warehouse disagree — "
            "every RCA on this node will report that difference as "
            "`unexplained`.",
            name,
            defn.formula,
            100 * drift,
            ", ".join(f"{dates[i].date()} (Δ{residual[i]:.4g})" for i in worst),
        )


# A share stored as `1.0000000002` is a rounding artefact, not a claim that
# 100.00000002% of anything happened. Tiny and absolute rather than relative:
# the quantity is a proportion, so its scale is known.
SHARE_EPS = 1e-9


def check_declared_shares(parser, data: GrainedData) -> None:
    """Check every `share: true` node against its own data, at **load**.

    `share: true` is what makes a simulated value *impossible* rather than
    unusual (roadmap C26), and it is the author's assertion — so the one thing
    that must not happen is a mis-declaration turning into a confident refusal
    of a scenario that was fine. Nothing else in the tree can catch it: the
    parser sees no data, and the what-if engine sees one window.

    This is the check running the other way. If the loaded history itself
    leaves [0, 1], then either the declaration is wrong or the source is, and
    the run is going to print "impossible" over values the warehouse has
    already recorded. Say so once, with the range it actually runs, and keep
    going — a
    log line, not a refusal, because the honest reading of the disagreement
    depends on which side is wrong and the parser cannot know.
    """
    for name in parser.get_topological_order():
        if parser.dag.nodes[name]["definition"].share is not True:
            continue
        try:
            series = data.series(name)[name].to_numpy(dtype=float)
        except (ValueError, RuntimeError, KeyError) as e:  # pragma: no cover - defensive
            logger.info("share check skipped for '%s': %s", name, e)
            continue
        observed = series[np.isfinite(series)]
        if not observed.size:
            continue
        lo, hi = float(np.min(observed)), float(np.max(observed))
        if lo >= -SHARE_EPS and hi <= 1 + SHARE_EPS:
            continue
        logger.warning(
            "Metric '%s' declares `share: true` — a proportion, bounded by "
            "[0, 1] — but its loaded series runs [%.4g, %.4g]. One of the two "
            "is wrong, and until it is settled the what-if engine will call a "
            "simulated value outside [0, 1] impossible for a metric whose own "
            "history is already outside it. Drop the `share` if this rate can "
            "genuinely exceed its whole (a per-unit intensity, a retention "
            "rate above 100%%), or fix the source.",
            name,
            lo,
            hi,
        )

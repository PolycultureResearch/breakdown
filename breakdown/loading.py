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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from breakdown.data_fetch import (
    SOURCE_ROWS,
    CloudDataFetcher,
    LocalDataFetcher,
    MockDataFetcher,
    WarehouseDataFetcher,
    provider_query_name,
    sparse_kw,
)
from breakdown.formula import eval_formula
from breakdown.grains import GrainedData, build_grained, resample_up, stock_period_refusal
from breakdown.snapshots import SnapshotFetcher, SnapshotStore, resolve_snapshot_dir

logger = logging.getLogger(__name__)


class NoDataInWindow(RuntimeError):
    """Every fetched metric returned no rows for the loaded window."""


def refuse_empty_window(source_rows: Dict[str, Optional[int]], start_date, end_date) -> None:
    """Refuse a load in which **no** fetched metric returned a row.

    `source_rows` is `{metric: rows the source returned}`, read off each
    frame's `attrs[SOURCE_ROWS]`; None means the provider did not say (the
    mock synthesizes and never aligns), and one None is enough to stand down,
    because this refuses only what it can prove.

    One empty series is an ordinary fact (a product stream not on sale yet)
    and is warned about where it is fetched. *Every* series empty is a window
    that misses the data: `breakdown serve --tree t.yml` with no dates reads
    the default window, a folder of 2025 exports answers it with nothing, and
    until grill 2026-10-05 H3 the tree then loaded as a hundred periods of
    zeros with `/health: ok` and a `data_through` invented from the requested
    window. There is no analysis to run on that tree, so it is a load error
    with the remedy rather than a flat line (rule 1).

    Here rather than in `load_tree` so `serve` and anything else that loads a
    tree (`doctor` can call it with the counts from its own fetches) refuse on
    the same terms.
    """
    if not source_rows or any(n is None or n > 0 for n in source_rows.values()):
        return
    names = sorted(source_rows)
    raise NoDataInWindow(
        f"None of the {len(names)} fetched metric(s) returned a row for "
        f"[{start_date}, {end_date}] ({', '.join(names[:5])}"
        f"{', …' if len(names) > 5 else ''}). The window misses the data, so "
        "every series would be zeros or undefined. Pass --start-date/--end-date "
        "(or BREAKDOWN_START_DATE/BREAKDOWN_END_DATE) matching the dates the "
        "source holds."
    )


class StockFoldedOverPeriod(RuntimeError):
    """A `kind: stock` node whose provider would add up (or average, or count)
    its snapshots over each period instead of reporting the period's level."""


# How each provider folds a `kind: stock` node's source rows into one period
# when the node is coarser than its source, and so how breakdown can know what
# it will get (roadmap C66). Every provider a tree may name has an entry — the
# invariant test enumerates `parser.PROVIDER_TYPES` against it — so a new
# provider has to say which of these it is before it can ship.
#
# * `binding`: breakdown compiles the query itself from a `BindingSpec`, whose
#   `agg` and `time_grain` say exactly what is folded. Checked exactly.
# * `manifest`: MetricFlow folds by the measure's aggregation, which the dbt
#   project's `semantic_manifest.json` states. Checked exactly when the file
#   is readable; otherwise the node is named as unverified.
# * `unverifiable`: the definitions are not on disk (dbt Cloud's Semantic
#   Layer). Every coarse stock is named as unverified, with the remedy.
# * the rest fold nothing: the tree's own `sql` returns the finished
#   per-period series (`warehouse` — the docs say a stock's SQL returns the
#   period-end level), the mock generates on the node's own spine, and a
#   cold-start tree fetches nothing.
#
# Unverified is a warning and not a refusal, deliberately. Refusing what
# breakdown cannot see would refuse every coarse stock on dbt Cloud and on a
# snapshot-served deployment (the hosted demo ships no dbt project), including
# the ones that are right — a period `count_distinct` or a MetricFlow
# `non_additive_dimension` — and the only remedy on offer, moving the node to
# day grain, changes what those metrics mean. A warning that names the node and
# says what breakdown could not check is what rule 1 asks for.
STOCK_FOLD_SOURCES: Dict[str, str] = {
    "duckdb": "binding",
    "dbt": "binding",
    "local": "manifest",
    "cloud": "unverifiable",
    "warehouse": "author_sql",
    "mock": "generated",
    "none": "no_data",
}


@dataclass
class StockFoldCheck:
    """What `stock_period_problems` found: refusals (a fold that is provably
    not the period's level) and nodes it could not check, each a sentence."""

    refusals: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)


def _coarse_stocks(config) -> List[Any]:
    """Fetched stock nodes coarser than a day: the only ones a provider can
    fold several snapshots into one period for."""
    return [m for m in config.metrics if m.kind == "stock" and not m.derived and m.grain != "day"]


def _binding_folds(config, stocks) -> Dict[str, Any]:
    """`{node: BindingSpec}` for the binding-compiled providers, read the way
    `build_fetcher` reads them: the node's own `bind:` first, then (for `dbt`)
    the manifest's binding for its source. A node with neither is left out —
    its fetch refuses by name."""
    bindings: Dict[str, Any] = {m.name: m.bind for m in stocks if m.bind is not None}
    unbound = [m for m in stocks if m.bind is None]
    if unbound and config.provider.type == "dbt":
        from breakdown.dbt_bridge import bridge_project

        bridged = bridge_project(config.provider.project_path).bindings
        for m in unbound:
            bind = bridged.get(provider_query_name("dbt", m))
            if bind is not None:
                bindings[m.name] = bind
    return bindings


def stock_period_problems(config) -> StockFoldCheck:
    """Every `kind: stock` node whose provider would fold its finer snapshots
    into one period by something other than a period-end snapshot (roadmap
    C66), without fetching anything.

    A provider asked for a weekly series aggregates the source's daily rows by
    the measure's own aggregation, not by the node's `kind`: a weekly `SUM` of
    a daily MRR snapshot is seven days of MRR. Nothing downstream can tell —
    the number is on the right scale for a flow and the fit is happy — so it
    is refused here, by name, before the first fetch. Called from
    `fetch_all_metrics` (serve, doctor) and `breakdown check`, so the three
    say the same sentence.
    """
    out = StockFoldCheck()
    stocks = _coarse_stocks(config)
    provider = config.provider.type
    how = STOCK_FOLD_SOURCES.get(provider)
    if not stocks or how in ("author_sql", "generated", "no_data"):
        return out
    if how == "binding":
        try:
            bindings = _binding_folds(config, stocks)
        except Exception as e:  # the fetcher will say this again, louder
            out.unverified += [
                f"Metric '{m.name}' is a `kind: stock` at grain '{m.grain}', and "
                f"breakdown could not read its binding to check how the {provider} "
                f"provider folds it into a period ({type(e).__name__}: {e})."
                for m in stocks
            ]
            return out
        for m in stocks:
            bind = bindings.get(m.name)
            if bind is None:
                continue
            refusal = stock_period_refusal(
                m.name,
                m.grain,
                bind.agg,
                bind.time_grain or "day",
                measure=bind.measure or bind.numerator or m.name,
                provider=provider,
                alternative=(
                    f"bind a `bind.sql` relation that already reduces to one row per "
                    f"{m.grain} (its period-end snapshot) and declare `time_grain: "
                    f"{m.grain}` on the binding. (`agg: last`, which would pick that "
                    "snapshot for you, is not built yet.)"
                ),
            )
            if refusal:
                out.refusals.append(refusal)
        return out
    if how == "manifest":
        from breakdown.dbt_bridge import load_manifest, manifest_path, period_fold

        try:
            manifest = load_manifest(config.provider.project_path)
        except FileNotFoundError:
            manifest = None
            why = f"no semantic manifest at {manifest_path(config.provider.project_path)}"
        except Exception as e:
            manifest, why = None, f"{type(e).__name__}: {e}"
        for m in stocks:
            fold = period_fold(manifest, provider_query_name(provider, m)) if manifest else None
            if fold is None or fold.agg is None:
                reason = fold.note if fold is not None else why
                out.unverified.append(_unverified(m, provider, reason))
                continue
            refusal = stock_period_refusal(
                m.name,
                m.grain,
                fold.agg,
                fold.source_grain,
                measure=fold.measure,
                provider=provider,
                alternative=(
                    "give the measure a `non_additive_dimension` over its time "
                    "dimension with `window_choice: max`, so MetricFlow itself "
                    f"reports each {m.grain}'s closing snapshot."
                ),
            )
            if refusal:
                out.refusals.append(refusal)
        return out
    out.unverified += [
        _unverified(m, provider, "its metric definitions are not on disk") for m in stocks
    ]
    return out


def _unverified(m, provider: str, reason: str) -> str:
    return (
        f"Metric '{m.name}' is a `kind: stock` at grain '{m.grain}', and breakdown "
        f"cannot check how the {provider} provider folds its source into a "
        f"{m.grain} ({reason}). If the source holds one snapshot per day and the "
        f"measure sums, every {m.grain} is several days of the level added "
        f"together. Declare it at its source's grain to have breakdown take the "
        f"last value per {m.grain}, or make sure the metric reports a period-end "
        "snapshot (MetricFlow: `non_additive_dimension` with `window_choice: max`)."
    )


def refuse_stock_period_folds(config) -> StockFoldCheck:
    """`stock_period_problems`, acted on: raise on a refusal, warn once per
    unverified node. Returns the check so a caller can report it too."""
    found = stock_period_problems(config)
    if found.refusals:
        raise StockFoldedOverPeriod(" ".join(found.refusals))
    for line in found.unverified:
        logger.warning("%s", line)
    return found


def resolve_data_dir(data_dir: str, tree_path: Optional[str]) -> str:
    """The folder the `duckdb` provider reads, as an absolute path.

    A relative `data_dir` is anchored to the **tree file's directory**, not the
    process's working directory, so a tree and its exports move together and
    `breakdown serve --tree some/where/tree.yml` starts from anywhere. This is
    the one place that resolution happens; the server (`load_tree`) and
    `doctor` both reach it through `build_fetcher`, which is what keeps the two
    reading the same folder — the same argument as `resolve_snapshot_dir`.

    Without a tree path (a library caller building a fetcher from a parsed
    config) a relative path means what it means everywhere else, relative to
    the working directory, and the resolved absolute path is what every error
    message names, so nothing is guessed silently.
    """
    if os.path.isabs(data_dir):
        return os.path.normpath(data_dir)
    base = os.path.dirname(os.path.abspath(tree_path)) if tree_path else os.getcwd()
    return os.path.normpath(os.path.join(base, data_dir))


def build_fetcher(provider_cfg, dag, metrics=None, *, tree_path: Optional[str] = None):
    """The provider's fetcher for one tree, before the snapshot wrapper.

    `tree_path` is the tree file being served; the `duckdb` provider anchors a
    relative `data_dir` to it (`resolve_data_dir`). Every other provider
    ignores it.
    """
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
    if provider_cfg.type == "duckdb":
        # The CSV/Parquet on-ramp (roadmap 2.2): the tree's own `bind:` blocks
        # are the whole binding set — the parser has already refused a node
        # without one — served by the `dbt` provider's fetcher over an
        # in-memory DuckDB with one view per export. Keyed by the node's name
        # (`provider_query_name`), since there is no manifest to reconcile
        # `source` against.
        from breakdown.dbt_provider import fetcher_from_data_dir

        bindings = {
            provider_query_name("duckdb", m): m.bind for m in (metrics or []) if not m.derived
        }
        return fetcher_from_data_dir(
            resolve_data_dir(provider_cfg.data_dir, tree_path),
            bindings,
            allow_external_access=provider_cfg.allow_external_access,
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
    analysis window rather than only the ones already run.

    The `duckdb` provider is not wrapped either, and for the opposite reason:
    its files *are* the committed artifact. A snapshot is keyed on
    `(metric, grain, kind, window)` with no content hash, so it would freeze
    an edited or re-exported CSV at whatever it said the first time and serve
    that silently — the C18 shape at a new boundary (rule 1). Reading the
    files again is free; a stale copy of them is not.

    That argument holds for the fetcher and not for `tree.data`, which is the
    boot-time fetch of those same files (grill 2026-10-05 M9): a file rewritten
    under a running server left the totals at what they were and the slices at
    what the file says now, and the reconciliation blamed the dimension. The
    provider therefore records each file's size and mtime when it opens the
    folder and refuses a sliced fetch once one has changed
    (`dbt_provider.DataFilesChanged`), naming the file — a restart re-reads
    both sides; nothing is reloaded behind the reader's back.
    """
    if provider_type in ("mock", "duckdb"):
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
    assemble per-grain frames (metrics outer-join on date against series at
    the same grain, each keeping its own range — GitHub #112).

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
    # Before the first fetch: a stock the provider would sum over each period
    # is a wrong number at every step after this one (roadmap C66).
    refuse_stock_period_folds(parser.config)
    series: Dict[str, pd.DataFrame] = {}
    # Per sparse metric, what the alignment contract filled by declaration
    # (GitHub #112): kept only where something was actually filled, so the
    # payload never carries "declared, nothing to do" — the YAML says that.
    sparse_fills: Dict[str, Dict[str, Any]] = {}
    source_rows: Dict[str, Optional[int]] = {}
    for metric in parser.config.metrics:
        if metric.derived:
            continue
        query_name = provider_query_name(provider_type, metric)
        df = fetcher.fetch_metric(
            query_name,
            start_date,
            end_date,
            grain=metric.grain,
            kind=metric.kind,
            **sparse_kw(metric.sparse),
        )
        record = df.attrs.get("sparse_fill")
        if record and record.get("filled"):
            sparse_fills[metric.name] = dict(record)
        source_rows[metric.name] = df.attrs.get(SOURCE_ROWS)
        df = df.rename(columns={query_name: metric.name})
        series[metric.name] = df[["date", metric.name]]
    # Before anything is derived or checked: every later step would be
    # reporting on zeros nobody observed.
    refuse_empty_window(source_rows, start_date, end_date)

    # Derived nodes second, in topological order so a derived node whose parent
    # is itself derived still finds its inputs.
    for name in parser.get_topological_order():
        if parser.dag.nodes[name]["definition"].derived:
            series[name] = derive_series(parser.dag, name, series, grain_of, kind_of)

    # Declaration order, which is the frame's column order and therefore what
    # every caller reading `frame.columns` has always seen.
    per_metric = {m.name: series[m.name] for m in parser.config.metrics}
    data = build_grained(per_metric, grain_of, kind_of, denominator_of, no_denominator_of)
    data.sparse_fills = sparse_fills
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

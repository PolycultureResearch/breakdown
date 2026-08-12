import importlib
import importlib.util
import logging
import re
import shutil
from abc import ABC, abstractmethod
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.formula import eval_formula
from breakdown.grains import floor_period, period_spine, resample_up

logger = logging.getLogger(__name__)

# Which extra ships each provider's SDK. The base install deliberately carries
# none of them, so every provider dependency is imported at the point of use
# and a missing one is reported as a fixable install, not a traceback.
PROVIDER_EXTRAS = {
    "cloud": "dbt",
    "local": "dbt",
    "dbt": "dbt-bridge",
    "warehouse": "databricks",
}


# Providers keyed by the tree's own metric name rather than by `source`. The
# semantic-layer providers (`local`, `cloud`, `dbt`) address their source system
# by the last segment of `source`; `mock` and `warehouse` resolve the tree name
# directly, because the tree *is* their addressing scheme.
_NAME_KEYED_PROVIDERS = ("mock", "warehouse")


def provider_query_name(provider_type: str, metric) -> str:
    """The name to ask `provider_type` for `metric` by.

    One function rather than the ternary this replaces, which lived at three
    call sites (startup fetch, sliced fetch, doctor's fit-readiness check) and
    had to be found and edited in all three to add a provider — the kind of
    duplication that ships a provider working at startup and broken on slice.
    """
    if provider_type in _NAME_KEYED_PROVIDERS:
        return metric.name
    return metric.source.split(".")[-1]


class MissingProviderExtra(RuntimeError):
    """A provider was selected whose optional dependencies aren't installed.

    Distinct from ImportError so callers (the API startup path, `breakdown
    doctor`) can tell "you need to install something" apart from "the provider
    is installed and broken" — the remediations are completely different.
    """


def _extra_hint(provider: str, extra: str, detail: str) -> str:
    return (
        f"provider type '{provider}' needs the {extra} extra: "
        f"pip install 'metric-breakdown[{extra}]'   ({detail})"
    )


def _require_module(module: str, provider: str, extra: str) -> ModuleType:
    """Import a provider SDK, or say which extra installs it."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise MissingProviderExtra(
            _extra_hint(provider, extra, f"missing module '{module}'")
        ) from e


def provider_extra_missing(provider: str) -> Optional[str]:
    """The error message for `provider`'s extra being absent, or None if it is
    installed. Lets callers report the problem before building a fetcher."""
    extra = PROVIDER_EXTRAS.get(provider)
    if extra is None:
        return None
    if provider == "local":
        # The local provider shells out to MetricFlow's `mf`; the binary on
        # PATH is the dependency, not an importable module.
        if shutil.which("mf") is None:
            return _extra_hint(provider, extra, "`mf` not found on PATH")
        return None
    modules = {
        "cloud": ("dbtsl",),
        # The manifest models are in-tree (breakdown/dbt_manifest.py), so the
        # only third-party piece left is the SQL generator.
        "dbt": ("sqlglot",),
        "warehouse": ("databricks.sql", "databricks.sdk"),
    }[provider]
    for module in modules:
        # `databricks` is a namespace package split across two distributions,
        # so only the dotted submodule proves the right one is installed.
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return _extra_hint(provider, extra, f"missing module '{module}'")
    return None


def _to_naive_dates(df: pd.DataFrame, metric_name: str) -> pd.DataFrame:
    """Parse the `date` column and drop any timezone, keeping the wall-clock
    label the source attached to the row.

    This has to happen before anything else touches the dates. `floor_period`
    normalizes but *preserves* tzinfo, so a tz-aware midnight satisfies every
    period-start check and then matches nothing against the tz-naive spine —
    which used to leave the metric identically zero (roadmap C1). Any SQL
    returning `TIMESTAMP` rather than `DATE` hits this (Databricks attaches
    UTC), as does a session timezone setting.

    The zone is dropped rather than converted: a row labelled midnight
    `+09:00` means that calendar date to whoever wrote the query, and
    converting through UTC would move it to the day before.
    """
    df = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    if idx.tz is not None:
        logger.warning(
            "Metric '%s': date column is timezone-aware (%s); dropping the zone "
            "and keeping the labelled date. Return DATE rather than TIMESTAMP to "
            "make this explicit.",
            metric_name,
            idx.tz,
        )
        idx = idx.tz_localize(None)
    df["date"] = idx
    return df


def _floor_labels(df: pd.DataFrame, metric_name: str, grain: str) -> pd.DataFrame:
    """Snap returned date labels to period starts (day midnight, week Monday,
    month 1st), warning if any label moved — e.g. a dbt project configured
    with non-Monday weeks."""
    idx = pd.DatetimeIndex(df["date"])
    floored = floor_period(idx, grain)
    if (idx != floored).any():
        logger.warning(
            "Metric '%s': %s-grain labels were not on period starts; flooring "
            "(weeks assume ISO Monday weeks).",
            metric_name,
            grain,
        )
        df = df.copy()
        df["date"] = floored
    return df


def _align_to_spine(
    df: pd.DataFrame,
    metric_name: str,
    grain: str,
    kind: str,
    start_date: str,
    end_date: str,
    value_col: str,
) -> pd.DataFrame:
    """Reindex a `[date, <value_col>]` frame onto the spine of whole `grain`
    periods inside the window, and fill what is missing by `kind`.

    Every provider goes through here, which is the point: this contract used to
    live inside the warehouse fetcher, so `cloud` and `local` floored their
    labels and returned raw rows (roadmap C2). A window ending Tuesday on a
    `grain: week` metric handed back a two-day partial week as a full row at
    ~2/7 normal volume, and `snap_window` treated it as whole — a manufactured
    −71% gap. The mock provider snapped correctly, which is why the suite never
    saw it.

    The rules, unchanged from the warehouse path that proved them:

    - Rows for periods only *partially* inside the window are dropped, never
      zero-filled into fake periods.
    - **Trailing** gaps are trimmed, not filled: periods after the last row the
      source returned mean "not loaded yet" far more often than "genuinely
      zero", and filling them bakes a lying tail into every headline number.
    - **Interior** gaps are filled by kind — flow → 0, stock → forward-fill
      (a leading gap is an error), rate → an error, because a rate cannot be
      invented. Filling one is a judgement call rather than a fact, so it is
      logged: a three-day ETL outage becomes three zero days otherwise, which
      is indistinguishable from a real collapse and which RCA will happily name
      as the root cause.
    - A source returning *no rows at all* keeps the full fill for flows — an
      all-quiet window is a legitimate flow series. Rows that all miss the
      spine is a different thing entirely (a query ignoring its bound window),
      and raises.
    """
    spine = period_spine(start_date, end_date, grain)
    s = df.set_index("date")[value_col].astype(float)

    kept = s.index.isin(spine)
    if len(s) and len(spine) and not kept.any():
        raise RuntimeError(
            f"Metric '{metric_name}' returned {len(s)} rows, none of which fall "
            f"on a '{grain}' period inside [{start_date}, {end_date}] "
            f"(returned {s.index.min().date()} to {s.index.max().date()}). "
            "Zero-filling would report the metric as identically zero, so this "
            "is an error: check that the query honours its window parameters."
        )
    s = s[kept].reindex(spine)

    if s.notna().any():
        s = s.loc[: s.last_valid_index()]

    first = s.first_valid_index()
    interior = s.index[s.isna() & (s.index > first)] if first is not None else s.index[:0]
    if len(interior) and kind in ("flow", "stock"):
        shown = [str(d.date()) for d in interior[:5]]
        logger.warning(
            "Metric '%s': %d interior %s period(s) had no row and were filled "
            "(%s → %s); %s. A gap in the source is not the same as a zero.",
            metric_name,
            len(interior),
            grain,
            ", ".join(shown),
            "0.0" if kind == "flow" else "the previous value",
            "…" if len(interior) > 5 else "that is all of them",
        )

    if kind == "flow":
        s = s.fillna(0.0)
    elif kind == "stock":
        s = s.ffill()
        if s.isna().any():
            raise RuntimeError(
                f"Stock metric '{metric_name}' has no value at or before the "
                f"first {grain} period ({spine[0].date()}) of the window."
            )
    else:  # rate
        if s.isna().any():
            missing = [str(d.date()) for d in s.index[s.isna()][:5]]
            raise RuntimeError(
                f"Rate metric '{metric_name}' is missing {grain} periods "
                f"{missing}; a rate cannot be gap-filled — fix the source or "
                "narrow the window."
            )
    return s.rename(metric_name).rename_axis("date").reset_index()


class SliceNotSupported(RuntimeError):
    """The provider cannot fetch a metric grouped by a business dimension."""


class BaseDataFetcher(ABC):
    """
    Abstract Base Class for metric data fetching.
    Ensures that regardless of the data source (Cloud, Local, Mock),
    the rest of the app receives a consistent DataFrame: a `date` column of
    period-start timestamps at the requested grain plus one value column.
    `kind` (flow/stock/rate) determines gap-filling semantics where the
    provider reindexes onto a period spine.
    """

    @abstractmethod
    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        pass

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        """The metric grouped by one business dimension, long format
        `[date, slice, value]` — one row per (period, dimension value).

        Analysis-time only: sliced frames never enter the startup data or the
        fit path. Providers that can't slice raise `SliceNotSupported`.
        """
        raise SliceNotSupported(
            f"The {type(self).__name__} provider does not support dimensional "
            f"slicing (requested '{metric_name}' by '{dimension_source}')."
        )

    def fetch_entity_flows(
        self,
        metric_name: str,
        dimension_source: str,
        reference_start: str,
        reference_end: str,
        analysis_start: str,
        analysis_end: str,
    ) -> "pd.DataFrame":
        """Two-window entity transitions, for the flow diagnostic (3.8 §6).

        Raises `SliceNotSupported` by default: classifying entities needs a
        binding that declares how to resolve one to a single slice per window,
        which most providers have no way to express.
        """
        raise SliceNotSupported(
            f"The {type(self).__name__} provider cannot classify entity flows "
            f"for '{metric_name}' by '{dimension_source}'."
        )

    def slice_additivity(self, metric_name: str, dimension_source: str) -> str:
        """Whether this metric's slices along `dimension_source` are expected
        to sum back to it: `exact`, `overlapping`, or `unknown`.

        Answered from the binding, never from the observed residual — a
        residual cannot distinguish deduplication overlap from a sliced query
        that diverged from the governed definition or a stale snapshot, and
        treating the two alike is what made the reconciliation flag unreadable.

        `unknown` is the honest default for a provider with no binding to ask.
        """
        return "unknown"

    def query_provenance(
        self,
        metric_name: str,
        dimension_source: Optional[str] = None,
        *,
        grain: str = "day",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Optional[str]:
        """The query behind this metric's series, or None.

        Principle 3 says never ship a number the engine can't defend, and for
        most of this project's life `warehouse` was the only provider where a
        user could see what was asked — because they wrote it themselves. A
        provider that knows its query should say so.

        The window is passed because provenance must not depend on whether a
        fetch happened to run in *this* process. A snapshot hit serves the
        number without executing anything, and "no query ran just now" is not
        the same fact as "this provider has no query" — reporting the second
        when the first is true tells the reader the number is less defensible
        than it is.

        None is a legitimate answer, not a failure: `mock` synthesizes, and the
        semantic-layer providers hand a metric name to someone else's planner
        and never see SQL. Callers report the absence rather than hiding it.
        """
        return None

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        """Earliest period-start date the source has for this metric
        (ISO ``YYYY-MM-DD``), or None when the provider cannot answer cheaply.

        A capability, not a contract: implementations must never raise —
        return None and log instead. Used only to tell users that history
        exists before ``--start-date``; nothing downstream depends on it.
        """
        return None


def _sliced_long(df: pd.DataFrame, metric_name: str, grain: str) -> pd.DataFrame:
    """Reshape a semantic-layer result (metric_time, dimension, value) into the
    long `[date, slice, value]` contract. The dimension column is identified as
    the one that is neither the time column nor the metric value column."""
    date_col = next((c for c in df.columns if "metric_time" in c.lower()), None)
    if date_col is None:
        raise RuntimeError(
            f"No metric_time column in sliced result for '{metric_name}'. "
            f"Columns: {list(df.columns)}"
        )
    value_col = next((c for c in df.columns if c.lower() == metric_name.lower()), None)
    if value_col is None:
        raise RuntimeError(
            f"No '{metric_name}' value column in sliced result. Columns: {list(df.columns)}"
        )
    dim_cols = [c for c in df.columns if c not in (date_col, value_col)]
    if len(dim_cols) != 1:
        raise RuntimeError(
            f"Expected exactly one dimension column in sliced result for "
            f"'{metric_name}'; got {dim_cols or 'none'}."
        )
    out = df.rename(columns={date_col: "date", dim_cols[0]: "slice", value_col: "value"})[
        ["date", "slice", "value"]
    ].copy()
    out["date"] = pd.to_datetime(out["date"])
    out = _floor_labels(out, metric_name, grain)
    out["slice"] = out["slice"].map(lambda v: "__null__" if pd.isna(v) else str(v))
    out["value"] = out["value"].astype(float)
    return out.sort_values(["date", "slice"]).reset_index(drop=True)


class CloudDataFetcher(BaseDataFetcher):
    """Fetches data from the dbt Semantic Layer (Cloud) using the official SDK."""

    def __init__(self, environment_id: str, host: str, token: str):
        SemanticLayerClient = _require_module("dbtsl", "cloud", "dbt").SemanticLayerClient
        self.client = SemanticLayerClient(
            environment_id=int(environment_id),
            host=host,
            auth_token=token,
        )

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        grain_dim = f"metric_time__{grain}"
        with self.client.session():
            table = self.client.query(
                metrics=[metric_name],
                group_by=[grain_dim],
                where=[
                    f"metric_time >= '{start_date}'",
                    f"metric_time <= '{end_date}'",
                ],
            )
        df = table.to_pandas()
        date_col = next(c for c in df.columns if "metric_time" in c.lower())
        df = df.rename(columns={date_col: "date"})
        df = _to_naive_dates(df, metric_name)
        df = _floor_labels(df, metric_name, grain)
        df = df.sort_values("date")
        return _align_to_spine(
            df, metric_name, grain, kind, start_date, end_date, value_col=metric_name
        )

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        grain_dim = f"metric_time__{grain}"
        try:
            with self.client.session():
                table = self.client.query(
                    metrics=[metric_name],
                    group_by=[grain_dim],
                    order_by=[grain_dim],
                    limit=1,
                )
            df = table.to_pandas()
            date_col = next(c for c in df.columns if "metric_time" in c.lower())
            if df.empty:
                return None
            return str(pd.Timestamp(df[date_col].iloc[0]).date())
        except Exception as e:
            logger.info("earliest_date unavailable for '%s': %s", metric_name, e)
            return None

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        grain_dim = f"metric_time__{grain}"
        with self.client.session():
            table = self.client.query(
                metrics=[metric_name],
                group_by=[grain_dim, dimension_source],
                where=[
                    f"metric_time >= '{start_date}'",
                    f"metric_time <= '{end_date}'",
                ],
            )
        return _sliced_long(table.to_pandas(), metric_name, grain)


class LocalDataFetcher(BaseDataFetcher):
    """Fetches data from a local dbt project by invoking the MetricFlow CLI."""

    def __init__(self, project_path: str):
        self.project_path = project_path
        logger.info("Initialized LocalDataFetcher for project at %s", project_path)

    def _run_mf_query(
        self, metric_name: str, group_by: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        import os
        import subprocess
        import tempfile

        # Checked here rather than in __init__ because a tree can name the
        # local provider and still never query it — the snapshot cache serves
        # committed parquet without a dbt project present (see the white-cube
        # demo). The dependency is the `mf` binary, so this is a PATH check:
        # `uv tool install dbt-metricflow` satisfies it just as well as the
        # extra, which is what `breakdown doctor` recommends.
        problem = provider_extra_missing("local")
        if problem:
            raise MissingProviderExtra(problem)

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            tmp_path = f.name

        try:
            try:
                result = subprocess.run(
                    [
                        "mf",
                        "query",
                        "--metrics",
                        metric_name,
                        "--group-by",
                        group_by,
                        "--start-time",
                        start_date,
                        "--end-time",
                        end_date,
                        "--csv",
                        tmp_path,
                    ],
                    cwd=self.project_path,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (FileNotFoundError, NotADirectoryError) as e:
                raise RuntimeError(f"mf query failed for metric '{metric_name}': {e}") from e
            if result.returncode != 0:
                raise RuntimeError(
                    f"mf query failed for metric '{metric_name}':\n{result.stderr.strip()}"
                )
            try:
                return pd.read_csv(tmp_path)
            except pd.errors.EmptyDataError:
                # `mf` writes a zero-byte file — not even a header — when the
                # metric's filter matches no rows in the window. That is an
                # ordinary state for a seasonal business (a product stream that
                # has not gone on sale yet), and `_align_to_spine` already knows
                # what to do with it: "a source returning no rows at all keeps
                # the full fill for flows". Hand it the empty frame with the
                # columns it expects so it can, instead of dying on the way.
                # Warned rather than silent, on the same reasoning as the
                # interior gap-fill: an all-quiet window and a filter that
                # matches nothing look identical from here.
                logger.warning(
                    "Metric '%s' returned no rows for [%s, %s]; treating the "
                    "window as empty (a flow fills to zero, a rate still errors).",
                    metric_name,
                    start_date,
                    end_date,
                )
                return pd.DataFrame({c: [] for c in [*group_by.split(","), metric_name]})
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        df = self._run_mf_query(metric_name, f"metric_time__{grain}", start_date, end_date)
        date_col = next((c for c in df.columns if "metric_time" in c.lower()), None)
        if date_col is None:
            raise RuntimeError(
                f"No metric_time column found in mf output. Columns: {list(df.columns)}"
            )
        df = df.rename(columns={date_col: "date"})
        df = _to_naive_dates(df, metric_name)
        df = _floor_labels(df, metric_name, grain)
        df = df.sort_values("date")
        return _align_to_spine(
            df, metric_name, grain, kind, start_date, end_date, value_col=metric_name
        )

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        df = self._run_mf_query(
            metric_name,
            f"metric_time__{grain},{dimension_source}",
            start_date,
            end_date,
        )
        return _sliced_long(df, metric_name, grain)

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        import os
        import subprocess
        import tempfile

        # Failure-soft everywhere: a snapshot-only deployment has no `mf`
        # binary and must simply not know, not crash.
        if provider_extra_missing("local"):
            return None
        group_by = f"metric_time__{grain}"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            tmp_path = f.name
        try:
            result = subprocess.run(
                [
                    "mf", "query",
                    "--metrics", metric_name,
                    "--group-by", group_by,
                    "--order", group_by,
                    "--limit", "1",
                    "--csv", tmp_path,
                ],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.info(
                    "earliest_date unavailable for '%s': %s",
                    metric_name, result.stderr.strip(),
                )
                return None
            df = pd.read_csv(tmp_path)
            date_col = next(
                (c for c in df.columns if "metric_time" in c.lower()), None
            )
            if date_col is None or df.empty:
                return None
            return str(pd.Timestamp(df[date_col].iloc[0]).date())
        except Exception as e:
            logger.info("earliest_date unavailable for '%s': %s", metric_name, e)
            return None
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class WarehouseDataFetcher(BaseDataFetcher):
    """Fetches data by running per-metric SQL directly against a warehouse.

    Unlike the semantic-layer providers, each metric in the tree carries its own
    `sql` — a SELECT that returns two columns, ``date`` and ``value``, with
    ``:start_date`` / ``:end_date`` named parameters for the window. This is the
    "warehouse-direct" path: the analyst mirrors governed metric definitions in
    SQL, so it works even when the semantic layer isn't reachable (e.g. a dbt
    Fusion project whose SL can't be queried offline).

    Currently targets Databricks SQL warehouses. The SQL must return one row
    per period at the metric's declared grain, with period-start dates (weeks
    start Monday, months on the 1st). Returned series are reindexed onto the
    spine of whole periods inside the window; **interior** gaps are filled by
    `kind` — flow → 0, stock → forward-fill (a leading gap is an error),
    rate → any missing period is an error (a rate cannot be invented) — while
    **trailing** gaps are trimmed, not filled: periods after the last row the
    SQL returned are treated as not-yet-loaded, so a lagging mart ends the
    series early instead of manufacturing fake zeros at the tail. (A query
    returning no rows at all keeps the old full-window fill for flows — an
    all-quiet window is a legitimate flow series.)

    Authentication is either a personal access token (`token`) or a Databricks
    CLI OAuth `profile` created by ``databricks auth login --profile <name>``.
    With a profile, credentials come from the Databricks SDK's unified auth and
    `host` defaults to the profile's host — no long-lived secret in the config.
    """

    def __init__(
        self,
        host: Optional[str],
        http_path: str,
        token: Optional[str],
        metric_sql: Dict[str, str],
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        profile: Optional[str] = None,
    ):
        # No extra check here: constructing the fetcher is pure config, and the
        # driver is only needed to connect. `_connect` raises MissingProviderExtra
        # on the first fetch, which is the startup path anyway.
        if not token and not profile:
            raise ValueError(
                "warehouse provider needs either a `token` (PAT) or a `profile` "
                "(from `databricks auth login`) for authentication."
            )
        self.host = host
        self.http_path = http_path
        self.token = token
        self.metric_sql = metric_sql
        self.catalog = catalog
        self.schema = schema
        self.profile = profile
        self._con = None

    def _connect(self):
        dbsql = _require_module("databricks.sql", "warehouse", "databricks")

        if self.profile:
            # Reuse the Databricks SDK's unified OAuth for this CLI profile. The
            # connector's `credentials_provider` wants a zero-arg callable that
            # returns a HeaderFactory; `Config.authenticate` is exactly that.
            Config = _require_module("databricks.sdk.core", "warehouse", "databricks").Config

            cfg = Config(profile=self.profile)
            host = self.host or cfg.host
            if not host:
                raise ValueError(
                    f"Could not resolve a host for profile '{self.profile}'. Set "
                    "`host` in the provider config or check the profile."
                )
            return dbsql.connect(
                server_hostname=host.replace("https://", "").rstrip("/"),
                http_path=self.http_path,
                credentials_provider=lambda: cfg.authenticate,
            )
        return dbsql.connect(
            server_hostname=self.host,
            http_path=self.http_path,
            access_token=self.token,
        )

    def _cursor(self):
        if self._con is None:
            self._con = self._connect()
            if self.catalog and self.schema:
                c = self._con.cursor()
                c.execute(f"USE {self.catalog}.{self.schema}")
                c.close()
        return self._con.cursor()

    def query_provenance(
        self, metric_name: str, dimension_source: Optional[str] = None, **_: Any
    ) -> Optional[str]:
        """The metric's own `sql` — this provider has always been transparent,
        because the author wrote the query. Slicing is not supported here yet
        (roadmap 2.8), so a dimension has no query to show."""
        if dimension_source is not None:
            return None
        return self.metric_sql.get(metric_name)

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        sql = self.metric_sql.get(metric_name)
        if sql is None:
            raise RuntimeError(
                f"No `sql` defined for metric '{metric_name}'. The warehouse provider "
                "requires each metric to carry a SQL query returning (date, value)."
            )

        cur = self._cursor()
        try:
            cur.execute(sql, parameters={"start_date": start_date, "end_date": end_date})
            cols = [d[0].lower() for d in cur.description]
            rows = cur.fetchall()
        finally:
            cur.close()

        df = pd.DataFrame(rows, columns=cols)
        if "date" not in df.columns or "value" not in df.columns:
            raise RuntimeError(
                f"SQL for metric '{metric_name}' must return columns named 'date' and "
                f"'value'; got {list(df.columns)}."
            )
        df = _to_naive_dates(df, metric_name)
        df["value"] = df["value"].astype(float)

        # The SQL author owns the aggregation to the declared grain, so a
        # misaligned label here is a bug in the query rather than a dbt
        # convention to accommodate — unlike the semantic-layer providers,
        # which floor with a warning. Runs on tz-naive dates: comparing two
        # tz-aware values is what let C1 through this guard.
        idx = pd.DatetimeIndex(df["date"])
        aligned = floor_period(idx, grain)
        if (idx != aligned).any():
            example = df["date"][(idx != aligned).argmax()]
            raise RuntimeError(
                f"SQL for metric '{metric_name}' at grain '{grain}' returned dates "
                f"not aligned to period starts (e.g. {pd.Timestamp(example).date()}); "
                "weekly periods start Monday, monthly on the 1st."
            )

        return _align_to_spine(
            df, metric_name, grain, kind, start_date, end_date, value_col="value"
        )

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        sql = self.metric_sql.get(metric_name)
        if sql is None:
            return None
        try:
            cur = self._cursor()
            try:
                # The metric's own SELECT, probed over an effectively unbounded
                # window: MIN over the subquery is cheap for a warehouse, and
                # the named parameters live inside it and must still be bound.
                cur.execute(
                    f"SELECT MIN(date) AS date FROM ({sql}) AS __breakdown_probe",
                    parameters={"start_date": "1900-01-01", "end_date": "9999-12-31"},
                )
                row = cur.fetchone()
            finally:
                cur.close()
            if row is None or row[0] is None:
                return None
            return str(pd.Timestamp(row[0]).date())
        except Exception as e:
            logger.info("earliest_date unavailable for '%s': %s", metric_name, e)
            return None


# --- Mock-only scale heuristics ---------------------------------------------
# `kind: rate` says a metric is a ratio, but not what it is a ratio *of*: a
# conversion rate lives in (0,1), a mean page-load time in seconds, an ARPU in
# dollars. The generator has to invent a scale for every leaf, and inventing an
# impression-count scale for a conversion rate is what made a funnel of
# `count × rate` identities compound once per level — 10²⁵ MRR on the reference
# tree, and five-digit percentages on every `format: percent` node (C11).
#
# A name is the only signal available, so it splits rates into two bands. These
# are mock-only: nothing in the engine reads them, and a miss costs realism in
# a fixture, never correctness. Flows and stocks keep their original scale so
# all-flow trees stay byte-identical (pinned by `test_mock_all_day_tree_pinned_values`).
_MOCK_RATE_MAGNITUDE_NAME = re.compile(
    r"^(average|avg|mean|median)_|_per_|^time_to_|^time_on_|_speed$|_cycle$|_latency$"
)


def _mock_rate_scale(name: str, rng) -> Tuple[float, float, float]:
    """Base level and (lo, hi) clip band for a `kind: rate` mock series."""
    if _MOCK_RATE_MAGNITUDE_NAME.search(name):
        # A magnitude per unit — a mean duration, an ARPU, sends per subscriber.
        # Bounded below at ~0 because none of them can go negative.
        return float(rng.uniform(1.0, 200.0)), 1e-3, np.inf
    # A share of something, so it belongs strictly inside (0, 1).
    return float(rng.uniform(0.01, 0.6)), 1e-3, 0.999


def _mock_coef(prior, rng) -> float:
    """A representative coefficient for one learned edge.

    Reads the *per-parent* prior when there is one. The previous version used
    `priors["coefficient"].params["mu"]` for every parent and fell back to
    `uniform(0.1, 0.5)`, so per-parent priors were never read and no
    coefficient was ever negative — a correctly declared
    `expected_signs: negative` edge then produced a guaranteed spurious
    `sign_warnings`, training authors to ignore the engine's own honesty
    diagnostic (C11).
    """
    if prior is None:
        return float(rng.uniform(0.1, 0.5))
    params = prior.params
    if prior.distribution == "Normal":
        return float(params.get("mu", 0.0))
    if prior.distribution == "HalfNormal":
        # E[HalfNormal(sigma)] = sigma * sqrt(2/pi); positive by construction,
        # which is the whole point of declaring one.
        return float(params.get("sigma", 1.0)) * 0.7979
    if prior.distribution == "LogNormal":
        return float(np.exp(params.get("mu", 0.0)))
    if prior.distribution == "Exponential":
        lam = float(params.get("lam", params.get("lambda", 1.0))) or 1.0
        return 1.0 / lam
    return float(rng.uniform(0.1, 0.5))


class MockDataFetcher(BaseDataFetcher):
    """
    Generates synthetic data for development and testing.

    When constructed with a metric DAG, generated series respect the tree
    structure *at each node's declared grain*: formula nodes satisfy their
    formula against their parents aggregated to the node's grain (plus
    noise), probabilistic nodes co-move with their parents, and root nodes
    are random walks with weekly seasonality on their native period spine.
    Rate-kind parents at a finer grain are resampled by per-period mean — a
    mock-only convenience (real rates are recomputed from components; the
    mock generates the rate series directly). Without a DAG, each metric is
    an independent random walk at the requested grain.

    Generation is **kind-aware**: a `kind: rate` node is generated on a rate's
    scale (a share inside (0,1), or a per-unit magnitude for a duration/ARPU
    name — see `_mock_rate_scale`) rather than a volume metric's, so a funnel
    of `count × rate` identities composes instead of compounding once per
    level. Learned edges read their **per-parent** prior, so a declared
    negative effect really is generated negative. Flows and stocks are
    unchanged, which keeps all-flow trees byte-identical.
    """

    def __init__(self, dag: Optional[nx.DiGraph] = None):
        self.dag = dag
        self._cache: Dict[Tuple[str, str], Dict[str, pd.Series]] = {}

    @staticmethod
    def _rng_for(metric_name: str) -> np.random.Generator:
        # Seeded deterministically per metric — no global state mutation
        return np.random.default_rng(seed=sum(ord(c) for c in metric_name) % (2**32))

    def _parent_at_grain(self, parent: str, series: pd.Series, grain: str) -> pd.Series:
        """A parent's series aggregated up to `grain` by the parent's kind."""
        pdefn = self.dag.nodes[parent]["definition"]
        pgrain = getattr(pdefn, "grain", "day")
        pkind = getattr(pdefn, "kind", "flow")
        if pgrain == grain:
            return series
        if pkind == "rate":
            # Mock-only: average the finer rate per coarse period.
            coarse = floor_period(pd.DatetimeIndex(series.index), grain)
            out = series.groupby(coarse).mean()
            out.index.name = series.index.name
            return out
        return resample_up(series, pgrain, grain, pkind, label=f"'{parent}'")

    def _tree_data(self, start_date: str, end_date: str) -> Dict[str, pd.Series]:
        key = (start_date, end_date)
        if key in self._cache:
            return self._cache[key]

        series: Dict[str, pd.Series] = {}
        for name in nx.topological_sort(self.dag):
            defn = self.dag.nodes[name]["definition"]
            grain = getattr(defn, "grain", "day")
            rng = self._rng_for(name)
            parents = list(self.dag.predecessors(name))
            spine = period_spine(start_date, end_date, grain)

            if parents:
                aligned = {p: self._parent_at_grain(p, series[p], grain) for p in parents}
                idx = spine
                for p_series in aligned.values():
                    idx = idx.intersection(p_series.index)
                n = len(idx)
                arrs = {p: aligned[p].loc[idx].to_numpy() for p in parents}

                if defn.formula:
                    # Cohort-aligned lagged identities: shift each lagged
                    # parent back, edge-padding with its first value.
                    shifted = {}
                    for p in parents:
                        vals_p = arrs[p]
                        lag = defn.lags.get(p, 0)
                        if lag > 0:
                            vals_p = np.concatenate([np.full(lag, vals_p[0]), vals_p[:-lag]])
                        shifted[p] = vals_p
                    base = eval_formula(defn.formula, shifted)
                    noise_scale = 0.02 * float(np.abs(base).mean()) or 1.0
                    vals = base + rng.normal(0, noise_scale, n)
                else:
                    rate_child = getattr(defn, "kind", "flow") == "rate"
                    signal = np.zeros(n)
                    for p in parents:
                        parent_vals = arrs[p]
                        lag = defn.lags.get(p, 0)
                        if lag > 0:
                            # Edge-pad with the first value so the child
                            # co-moves with the parent `lag` periods earlier.
                            parent_vals = np.concatenate(
                                [np.full(lag, parent_vals[0]), parent_vals[:-lag]]
                            )
                        coef = _mock_coef(defn.priors.get(p) or defn.priors.get("coefficient"), rng)
                        # On a rate child the coefficient is a per-unit effect
                        # *on the rate*, not a share of the parent's level, so
                        # `sum(coef * parent)` would put a conversion rate on
                        # its regressors' scale (seconds of page load). Take
                        # deviations and let the rate carry its own level.
                        signal += coef * (
                            parent_vals - parent_vals.mean() if rate_child else parent_vals
                        )

                    if rate_child:
                        base, lo, hi = _mock_rate_scale(name, rng)
                        # Rescale the composite to a modest swing around the
                        # level. A positive scalar, so every parent keeps its
                        # sign and its share of the signal — which is what the
                        # fit is meant to recover — while the series stays a
                        # plausible rate whatever units the priors were in.
                        sd = float(signal.std())
                        if sd > 0:
                            signal = signal * (0.12 * base / sd)
                        vals = np.clip(base + signal + rng.normal(0, 0.03 * base, n), lo, hi)
                    else:
                        noise_scale = 0.05 * float(np.abs(signal).mean()) or 1.0
                        vals = signal + rng.normal(0, noise_scale, n)
                series[name] = pd.Series(vals, index=idx)
            else:
                n = len(spine)
                t = np.arange(n)
                if getattr(defn, "kind", "flow") == "rate":
                    # A rate wanders far less than a volume metric and cannot
                    # leave its band, so the walk is damped and clipped — an
                    # undamped one drifts a share negative over a long window.
                    base, lo, hi = _mock_rate_scale(name, rng)
                    vals = np.clip(
                        base
                        + np.cumsum(rng.normal(0, 0.005 * base, n))
                        + 0.03 * base * np.sin(2 * np.pi * t / 7),
                        lo,
                        hi,
                    )
                else:
                    level = float(rng.uniform(50, 5000))
                    vals = (
                        level
                        + np.cumsum(rng.normal(0, 0.02 * level, n))
                        + 0.1 * level * np.sin(2 * np.pi * t / 7)
                    )
                series[name] = pd.Series(vals, index=spine)

        self._cache[key] = series
        return series

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        if end < start:
            raise ValueError(f"end_date '{end_date}' must be >= start_date '{start_date}'")

        if self.dag is not None and metric_name in self.dag:
            s = self._tree_data(start_date, end_date)[metric_name]
            return pd.DataFrame({"date": s.index, metric_name: s.to_numpy()})

        spine = period_spine(start_date, end_date, grain)
        rng = self._rng_for(metric_name)
        values = 1000 + np.cumsum(rng.normal(0, 10, len(spine)))
        return pd.DataFrame({"date": spine, metric_name: values})

    def earliest_date(self, metric_name: str, grain: str = "day") -> Optional[str]:
        # Mock history is effectively unbounded; the epoch (also the anchor of
        # the slice-share curves) is a deterministic answer that keeps the
        # history-discovery path exercisable in dev and demo.
        return str(self._EPOCH.date())

    _SLICE_SUFFIXES = ("a", "b", "c", "d")
    _EPOCH = pd.Timestamp("2020-01-01")

    def _covering_series(
        self, metric_name: str, start_date: str, end_date: str
    ) -> Optional[pd.Series]:
        """A cached tree series covering [start, end], subset to it — so slice
        fetches split the very numbers the startup fetch produced and slices
        reconcile exactly against the served data."""
        s_ts, e_ts = pd.to_datetime(start_date), pd.to_datetime(end_date)
        for (cs, ce), series in self._cache.items():
            if pd.to_datetime(cs) <= s_ts and pd.to_datetime(ce) >= e_ts and metric_name in series:
                s = series[metric_name]
                return s[(s.index >= s_ts) & (s.index <= e_ts)]
        return None

    def _share_curves(
        self, dimension_source: str, dates: pd.DatetimeIndex
    ) -> Tuple[list, np.ndarray]:
        """Deterministic smooth slice-share curves, softmax across slices per
        date. Purely date-anchored (fixed epoch), so a date's shares are
        identical across metrics and fetch windows — which is what makes a
        mock rate blend reconcile exactly against its weight metric's slices.
        The drift term gives demos genuine mix shifts."""
        label = dimension_source.split("__")[-1]
        names = [f"{label}_{sfx}" for sfx in self._SLICE_SUFFIXES]
        t = (dates - self._EPOCH).days.to_numpy(dtype=float)
        logits = []
        for name in names:
            rng = self._rng_for(f"dim:{dimension_source}:{name}")
            base = rng.uniform(-0.5, 1.5)
            amp = rng.uniform(0.1, 0.4)
            period = rng.uniform(60.0, 240.0)
            phase = rng.uniform(0, 2 * np.pi)
            drift = rng.uniform(-0.0008, 0.0008)
            logits.append(base + amp * np.sin(2 * np.pi * t / period + phase) + drift * t)
        L = np.stack(logits)
        L -= L.max(axis=0, keepdims=True)
        shares = np.exp(L) / np.exp(L).sum(axis=0, keepdims=True)
        return names, shares

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        s = None
        if self.dag is not None and metric_name in self.dag:
            s = self._covering_series(metric_name, start_date, end_date)
        if s is None:
            df = self.fetch_metric(metric_name, start_date, end_date, grain, kind)
            s = pd.Series(df[metric_name].to_numpy(), index=pd.DatetimeIndex(df["date"]))
        dates = pd.DatetimeIndex(s.index)
        names, shares = self._share_curves(dimension_source, dates)
        total = s.to_numpy(dtype=float)

        if kind == "rate":
            # Slice rates deviate smoothly around the blended rate,
            # orthogonalized against the shares so Σ s_g·r_g reproduces the
            # metric exactly (Σ s_g = 1 makes the correction term vanish).
            t = (dates - self._EPOCH).days.to_numpy(dtype=float)
            devs = []
            for name in names:
                rng = self._rng_for(f"ratedev:{metric_name}:{dimension_source}:{name}")
                amp = rng.uniform(0.02, 0.12)
                period = rng.uniform(45.0, 180.0)
                phase = rng.uniform(0, 2 * np.pi)
                devs.append(amp * np.sin(2 * np.pi * t / period + phase))
            D = np.stack(devs)
            D = D - (shares * D).sum(axis=0, keepdims=True)
            values = total[None, :] * (1.0 + D)
        else:
            values = total[None, :] * shares

        out = pd.concat(
            [
                pd.DataFrame({"date": dates, "slice": name, "value": values[i]})
                for i, name in enumerate(names)
            ],
            ignore_index=True,
        )
        return out.sort_values(["date", "slice"]).reset_index(drop=True)

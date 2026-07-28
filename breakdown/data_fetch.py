import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from breakdown.formula import eval_formula
from breakdown.grains import floor_period, period_spine, resample_up

logger = logging.getLogger(__name__)


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
            metric_name, grain,
        )
        df = df.copy()
        df["date"] = floored
    return df


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
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
    ) -> pd.DataFrame:
        pass


class CloudDataFetcher(BaseDataFetcher):
    """Fetches data from the dbt Semantic Layer (Cloud) using the official SDK."""
    def __init__(self, environment_id: str, host: str, token: str):
        from dbtsl import SemanticLayerClient
        self.client = SemanticLayerClient(
            environment_id=int(environment_id),
            host=host,
            auth_token=token,
        )

    def fetch_metric(
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
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
        df["date"] = pd.to_datetime(df["date"])
        df = _floor_labels(df, metric_name, grain)
        return df[["date", metric_name]].sort_values("date").reset_index(drop=True)


class LocalDataFetcher(BaseDataFetcher):
    """Fetches data from a local dbt project by invoking the MetricFlow CLI."""
    def __init__(self, project_path: str):
        self.project_path = project_path
        logger.info("Initialized LocalDataFetcher for project at %s", project_path)

    def fetch_metric(
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
    ) -> pd.DataFrame:
        import os
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            tmp_path = f.name

        try:
            try:
                result = subprocess.run(
                    [
                        "mf", "query",
                        "--metrics", metric_name,
                        "--group-by", f"metric_time__{grain}",
                        "--start-time", start_date,
                        "--end-time", end_date,
                        "--csv", tmp_path,
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
            df = pd.read_csv(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        date_col = next((c for c in df.columns if "metric_time" in c.lower()), None)
        if date_col is None:
            raise RuntimeError(f"No metric_time column found in mf output. Columns: {list(df.columns)}")
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"])
        df = _floor_labels(df, metric_name, grain)
        return df[["date", metric_name]].sort_values("date").reset_index(drop=True)


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
    spine of whole periods inside the window and gap-filled by `kind`:
    flow → 0, stock → forward-fill (a leading gap is an error), rate → any
    missing period is an error (a rate cannot be invented).

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
        from databricks import sql as dbsql

        if self.profile:
            # Reuse the Databricks SDK's unified OAuth for this CLI profile. The
            # connector's `credentials_provider` wants a zero-arg callable that
            # returns a HeaderFactory; `Config.authenticate` is exactly that.
            from databricks.sdk.core import Config

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

    def fetch_metric(
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
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
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = df["value"].astype(float)

        # The SQL author owns the aggregation to the declared grain; the
        # engine only validates the labels and fills gaps by kind.
        idx = pd.DatetimeIndex(df["date"])
        aligned = floor_period(idx, grain)
        if (idx != aligned).any():
            example = df["date"][(idx != aligned).argmax()]
            raise RuntimeError(
                f"SQL for metric '{metric_name}' at grain '{grain}' returned dates "
                f"not aligned to period starts (e.g. {pd.Timestamp(example).date()}); "
                "weekly periods start Monday, monthly on the 1st."
            )

        # Reindex onto the spine of whole periods inside the window (rows for
        # partial edge periods are dropped) so missing periods become explicit
        # rather than dropping rows from the tree-wide join.
        spine = period_spine(start_date, end_date, grain)
        s = df.set_index("date")["value"]
        s = s[s.index.isin(spine)].reindex(spine)
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
                    f"{missing}; a rate cannot be gap-filled — fix the SQL or "
                    "narrow the window."
                )
        return s.rename(metric_name).rename_axis("date").reset_index()


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
                            vals_p = np.concatenate(
                                [np.full(lag, vals_p[0]), vals_p[:-lag]]
                            )
                        shifted[p] = vals_p
                    base = eval_formula(defn.formula, shifted)
                    noise_scale = 0.02 * float(np.abs(base).mean()) or 1.0
                    vals = base + rng.normal(0, noise_scale, n)
                else:
                    coef_prior = defn.priors.get("coefficient")
                    default_coef = coef_prior.params.get("mu") if coef_prior else None
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
                        coef = default_coef if default_coef is not None else float(rng.uniform(0.1, 0.5))
                        signal += coef * parent_vals
                    noise_scale = 0.05 * float(np.abs(signal).mean()) or 1.0
                    vals = signal + rng.normal(0, noise_scale, n)
                series[name] = pd.Series(vals, index=idx)
            else:
                n = len(spine)
                t = np.arange(n)
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
        self, metric_name: str, start_date: str, end_date: str,
        grain: str = "day", kind: str = "flow",
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

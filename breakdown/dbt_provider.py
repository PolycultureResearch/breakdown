"""The `dbt` provider: fetch a bound node's series from the user's own dbt
project and warehouse.

Three pieces already exist and this joins them up — `dbt_bridge` reads the
semantic manifest into bindings, `dbt_sql` compiles a binding into dialect SQL,
and `BaseDataFetcher`'s shared helpers align whatever comes back onto the period
spine. What is added here is the connection, and it comes from the project's own
`profiles.yml`: **the practitioner supplies no new credentials.**

It lives outside `data_fetch.py` because `dbt_sql` and `dbt_bridge` both import
from it, so a fetcher there would close an import cycle. `SnapshotFetcher` sets
the same precedent.

Design: `knowledge/semantic_layer_connectivity_design.md` §5.
"""

import logging
import os
import re
from typing import Any, Callable, Dict, Optional

import pandas as pd
import yaml

from breakdown.data_fetch import (
    BaseDataFetcher,
    MissingProviderExtra,
    SliceNotSupported,
    _align_to_spine,
    _floor_labels,
    _to_naive_dates,
)
from breakdown.dbt_bridge import bridge_project
from breakdown.dbt_sql import (
    build_entity_flow_query,
    build_grain_assertion,
    build_query,
    build_resolved_slice_query,
    dialect_for_adapter,
)
from breakdown.parser import BindingSpec

logger = logging.getLogger(__name__)

# `{{ env_var('NAME') }}` / `{{ env_var("NAME", "default") }}` — by far the most
# common Jinja in a profiles.yml, and the only construct resolved here. Anything
# richer needs dbt's own renderer, which this provider deliberately does not
# depend on; an unresolved template is reported rather than passed to a driver
# as a literal, since `{{ env_var('DBT_TOKEN') }}` as a password fails in a way
# nobody can read.
_ENV_VAR = re.compile(
    r"\{\{\s*env_var\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"(?:\s*,\s*['\"]([^'\"]*)['\"])?\s*\)\s*\}\}"
)
_JINJA = re.compile(r"\{\{.*?\}\}")


class DbtProfileError(RuntimeError):
    """The dbt profile could not be resolved into a usable connection."""


def _render(value: Any, where: str) -> Any:
    if not isinstance(value, str):
        return value

    def repl(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise DbtProfileError(
            f"{where} references environment variable '{name}', which is not set."
        )

    rendered = _ENV_VAR.sub(repl, value)
    if _JINJA.search(rendered):
        raise DbtProfileError(
            f"{where} contains Jinja this provider cannot render ({rendered!r}). "
            "Only env_var() is supported; resolve it in the environment, or set "
            "the value literally."
        )
    return rendered


def _profiles_path(project_path: str, profiles_dir: Optional[str]) -> str:
    for candidate in (
        profiles_dir,
        os.environ.get("DBT_PROFILES_DIR"),
        project_path,
        os.path.expanduser("~/.dbt"),
    ):
        if not candidate:
            continue
        path = os.path.join(candidate, "profiles.yml")
        if os.path.exists(path):
            return path
    raise DbtProfileError(
        "No profiles.yml found. Looked in the `profiles_dir` setting, "
        "$DBT_PROFILES_DIR, the project directory, and ~/.dbt."
    )


def resolve_profile(
    project_path: str,
    *,
    target: Optional[str] = None,
    profiles_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """The connection settings dbt itself would use for this project.

    Reads the project's `profile:` from `dbt_project.yml`, then that profile's
    chosen output from `profiles.yml`. Returns the output dict with `env_var()`
    resolved, plus the resolved `target` under `_target`.
    """
    project_file = os.path.join(project_path, "dbt_project.yml")
    if not os.path.exists(project_file):
        raise DbtProfileError(f"No dbt_project.yml at {project_file}.")
    with open(project_file) as fh:
        project = yaml.safe_load(fh) or {}
    profile_name = project.get("profile")
    if not profile_name:
        raise DbtProfileError(f"{project_file} declares no `profile:`.")

    path = _profiles_path(project_path, profiles_dir)
    with open(path) as fh:
        profiles = yaml.safe_load(fh) or {}
    profile = profiles.get(profile_name)
    if profile is None:
        raise DbtProfileError(
            f"Profile '{profile_name}' (from {project_file}) is not in {path}. "
            f"Available: {sorted(k for k in profiles if k != 'config')}."
        )

    outputs = profile.get("outputs") or {}
    chosen = target or profile.get("target")
    if chosen not in outputs:
        raise DbtProfileError(
            f"Target '{chosen}' is not an output of profile '{profile_name}' in "
            f"{path}. Available: {sorted(outputs)}."
        )
    out = {
        k: _render(v, f"profiles.yml [{profile_name}.outputs.{chosen}.{k}]")
        for k, v in (outputs[chosen] or {}).items()
    }
    out["_target"] = chosen
    return out


# --- connections ------------------------------------------------------------
#
# One connector per dbt adapter. Each imports its driver lazily and names the
# package to install, because the driver a user needs is the one their own dbt
# adapter already depends on — breakdown ships no warehouse driver unconditionally,
# only the optional `databricks` and `bigquery` extras.
#
# Note that this map and `ADAPTER_DIALECTS` in `dbt_sql.py` are independent: a
# dialect entry means the generator emits correct SQL for that warehouse, not
# that anything here can run it. BigQuery sat in that gap from 2.10 until a
# connector was added, which is worth remembering before mapping a dialect and
# calling a warehouse supported.


def _connect_bigquery(out: Dict[str, Any]) -> Any:
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import dbapi
    except ImportError as e:
        raise MissingProviderExtra(
            "provider type 'dbt' with a bigquery target needs the bigquery "
            "extra: pip install 'metric-breakdown[bigquery]'"
        ) from e

    # BigQuery is the one adapter here whose credential is chosen by a `method`
    # rather than carried in a fixed field, so the profile is read as dbt reads
    # it: `oauth` means Application Default Credentials (the driver finds them
    # itself), and the two service-account methods differ only in whether the
    # key is a path or already inline.
    method = str(out.get("method") or "oauth").lower()
    credentials = None
    if method in ("service-account", "service_account"):
        from google.oauth2 import service_account

        keyfile = out.get("keyfile")
        if not keyfile:
            raise DbtProfileError(
                "bigquery method 'service-account' requires `keyfile` in the profile."
            )
        credentials = service_account.Credentials.from_service_account_file(str(keyfile))
    elif method in ("service-account-json", "service_account_json"):
        from google.oauth2 import service_account

        info = out.get("keyfile_json")
        if not isinstance(info, dict):
            raise DbtProfileError(
                "bigquery method 'service-account-json' requires `keyfile_json` "
                "in the profile, as an inline mapping."
            )
        credentials = service_account.Credentials.from_service_account_info(info)
    elif method != "oauth":
        # Named rather than silently fallen back to ADC: an unsupported method
        # that quietly authenticates as somebody else is worse than a stop.
        raise DbtProfileError(
            f"bigquery method '{method}' is not supported. Supported: oauth "
            "(Application Default Credentials), service-account, "
            "service-account-json."
        )

    client = bigquery.Client(
        # dbt writes `project`; `database` is its accepted alias and appears in
        # real profiles, so both are read. No default dataset is set — the
        # manifest gives every relation fully qualified and already quoted, so
        # an unqualified name never reaches the warehouse.
        project=out.get("project") or out.get("database"),
        credentials=credentials,
        location=out.get("location"),
    )
    # The DBAPI wrapper rather than the native `Client.query()` API, so the
    # cursor satisfies `_frame` unchanged like every other connector.
    return dbapi.connect(client)


def _connect_databricks(out: Dict[str, Any]) -> Any:
    try:
        from databricks import sql as dbsql
    except ImportError as e:
        raise MissingProviderExtra(
            "provider type 'dbt' with a databricks target needs the databricks "
            "extra: pip install 'metric-breakdown[databricks]'"
        ) from e
    host = str(out["host"]).replace("https://", "").rstrip("/")
    return dbsql.connect(
        server_hostname=host,
        http_path=out["http_path"],
        access_token=out["token"],
    )


def _connect_duckdb(out: Dict[str, Any]) -> Any:
    try:
        import duckdb
    except ImportError as e:
        raise MissingProviderExtra(
            "provider type 'dbt' with a duckdb target needs duckdb: pip install duckdb"
        ) from e
    return duckdb.connect(out.get("path") or ":memory:", read_only=bool(out.get("path")))


def _connect_postgres(out: Dict[str, Any]) -> Any:
    try:
        import psycopg2
    except ImportError as e:
        raise MissingProviderExtra(
            "provider type 'dbt' with a postgres target needs psycopg2: pip install psycopg2-binary"
        ) from e
    return psycopg2.connect(
        host=out.get("host"),
        port=out.get("port", 5432),
        dbname=out.get("dbname") or out.get("database"),
        user=out.get("user"),
        password=out.get("password"),
    )


def _connect_snowflake(out: Dict[str, Any]) -> Any:
    try:
        import snowflake.connector as sf
    except ImportError as e:
        raise MissingProviderExtra(
            "provider type 'dbt' with a snowflake target needs "
            "snowflake-connector-python: pip install snowflake-connector-python"
        ) from e
    return sf.connect(
        account=out.get("account"),
        user=out.get("user"),
        password=out.get("password"),
        role=out.get("role"),
        warehouse=out.get("warehouse"),
        database=out.get("database"),
        schema=out.get("schema"),
    )


CONNECTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "bigquery": _connect_bigquery,
    "databricks": _connect_databricks,
    "duckdb": _connect_duckdb,
    "postgres": _connect_postgres,
    "snowflake": _connect_snowflake,
}


def connect_from_profile(out: Dict[str, Any]) -> Any:
    adapter = str(out.get("type", "")).lower()
    connector = CONNECTORS.get(adapter)
    if connector is None:
        raise DbtProfileError(
            f"No connection support for dbt adapter '{adapter}'. Supported: "
            f"{sorted(CONNECTORS)}. The binding contract still works — bind the "
            "nodes by hand and use a provider that can reach this warehouse."
        )
    return connector(out)


# --- the fetcher ------------------------------------------------------------


def _frame(cursor: Any) -> pd.DataFrame:
    """A DataFrame from a DBAPI cursor, with lowercased column names.

    Snowflake upper-cases unquoted identifiers and several drivers differ on
    case, so the aliases the generator quotes come back inconsistently. The
    contract downstream is `date`/`slice`/`value`, so normalize once here.
    """
    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    return pd.DataFrame([tuple(r) for r in rows], columns=cols)


class DbtDataFetcher(BaseDataFetcher):
    """Fetches bound nodes by generating SQL and running it on the project's
    own warehouse connection.

    `connect` is a zero-argument callable rather than a live connection so the
    fetcher can be constructed without touching the warehouse — a tree whose
    metrics all have snapshots must boot with the warehouse down, which is the
    same rule `LocalDataFetcher` follows for `mf`.
    """

    def __init__(
        self,
        bindings: Dict[str, BindingSpec],
        connect: Callable[[], Any],
        *,
        dialect: str = "",
    ):
        self.bindings = dict(bindings)
        self._connect = connect
        self.dialect = dialect
        self._conn: Any = None
        # Last statement per (metric, kind of query). The hook 2.11 reads to
        # show a user what produced a number; principle 3 in one dict.
        self.last_sql: Dict[str, str] = {}

    # -- connection --

    def _cursor(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn.cursor()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _query(self, sql: str) -> pd.DataFrame:
        cursor = self._cursor()
        try:
            cursor.execute(sql)
            return _frame(cursor)
        finally:
            cursor.close()

    # -- bindings --

    def binding(self, metric_name: str) -> BindingSpec:
        try:
            return self.bindings[metric_name]
        except KeyError:
            raise RuntimeError(
                f"No binding for '{metric_name}'. It is neither a metric in the "
                f"dbt semantic manifest nor a node with its own `bind:` block. "
                f"Known: {sorted(self.bindings)[:8]}"
                f"{' …' if len(self.bindings) > 8 else ''}"
            ) from None

    # -- the BaseDataFetcher contract --

    def fetch_metric(
        self,
        metric_name: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
    ) -> pd.DataFrame:
        bind = self.binding(metric_name)
        sql = build_query(
            bind,
            grain=grain,
            start_date=start_date,
            end_date=end_date,
            dialect=self.dialect,
        )
        self.last_sql[metric_name] = sql
        df = self._query(sql)
        if "date" not in df.columns or "value" not in df.columns:
            raise RuntimeError(
                f"Query for '{metric_name}' returned columns {list(df.columns)}; "
                "expected 'date' and 'value'."
            )
        df = _to_naive_dates(df, metric_name)
        # Floored with a warning rather than rejected, like the other
        # semantic-layer providers: the generator buckets to period starts
        # itself, so a moved label means the warehouse disagreed with us about a
        # boundary and that is worth surfacing, not fatal.
        df = _floor_labels(df, metric_name, grain)
        df = df.sort_values("date")
        return _align_to_spine(
            df, metric_name, grain, kind, start_date, end_date, value_col="value"
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
        bind = self.binding(metric_name)
        if dimension_source not in bind.dimensions:
            raise SliceNotSupported(
                f"The binding for '{metric_name}' declares no dimension "
                f"'{dimension_source}' (has {sorted(bind.dimensions) or 'none'})."
            )
        if bind.is_non_additive and not bind.resolves_to_entity_grain:
            # Confirmed against a real warehouse: slicing `active_subscription_count`
            # by status over two weeks gave 2,106 against an unsliced 2,069. That
            # is not a defect — one subscription changing status inside a day is
            # counted once in the total and once in each status it held — but it
            # means the slices cannot be read as a decomposition. The slice path
            # already reports a residual rather than rescaling; this says why the
            # residual exists, so it reads as dedup overlap rather than an
            # unexplained cause. Resolving it properly is roadmap 3.8: decompose
            # at the grain where the metric becomes a sum.
            logger.warning(
                "Metric '%s' is bound with `agg: %s`, whose slices do not sum: "
                "an entity appearing in several slices is counted once in the "
                "total and once per slice, so the difference is deduplication "
                "overlap, not an unexplained cause.",
                metric_name,
                bind.agg,
            )
        if bind.resolves_to_entity_grain:
            sql = build_resolved_slice_query(
                bind,
                dimension=dimension_source,
                grain=grain,
                start_date=start_date,
                end_date=end_date,
                dialect=self.dialect,
            )
        else:
            sql = build_query(
                bind,
                grain=grain,
                start_date=start_date,
                end_date=end_date,
                dialect=self.dialect,
                dimension=dimension_source,
            )
        self.last_sql[f"{metric_name}::{dimension_source}"] = sql
        df = self._query(sql)
        missing = {"date", "slice", "value"} - set(df.columns)
        if missing:
            raise RuntimeError(
                f"Sliced query for '{metric_name}' is missing {sorted(missing)}; "
                f"got {list(df.columns)}."
            )
        # Built directly rather than through `_sliced_long`, which finds its
        # date column by looking for `metric_time` — a MetricFlow name this
        # provider never produces, because it names the column itself.
        df = df[["date", "slice", "value"]].copy()
        df["date"] = pd.to_datetime(df["date"])
        df = _to_naive_dates(df, metric_name)
        df = _floor_labels(df, metric_name, grain)
        df["slice"] = df["slice"].map(lambda v: "__null__" if pd.isna(v) else str(v))
        df["value"] = df["value"].astype(float)
        return df.sort_values(["date", "slice"]).reset_index(drop=True)

    def slice_additivity(self, metric_name: str, dimension_source: str) -> str:
        """`overlapping` for a non-additive aggregation, else `exact`.

        This is the conservative reading of what the binding can prove today:
        `count_distinct` *may* double-count an entity that holds several values
        of the dimension inside a period. Whether it actually does is a
        property of the data, and settling it needs the entity-grain resolution
        of roadmap 3.8 — until then, claiming `exact` for a distinct count
        would be a guess in the direction that hides a real overstatement.
        """
        bind = self.bindings.get(metric_name)
        if bind is None or dimension_source not in bind.dimensions:
            return "unknown"
        if not bind.is_non_additive:
            return "exact"
        if bind.resolves_to_entity_grain:
            # Resolution collapses the relation to one row per (entity, period),
            # so every entity lands in exactly one slice and the sum is the
            # distinct entity count — which is the metric. Verified against both
            # DuckDB and a real Databricks warehouse.
            return "exact"
        if bind.asserts_entity_grain:
            # `resolve: error` claims single-valuedness without making it true.
            # Reporting `exact` here would put a false label on the number, and
            # withhold nothing when the claim is wrong; reporting `overlapping`
            # would contradict an author who is right. `unknown` is the honest
            # answer at query time — reconciliation still flags a real residual
            # as discrepant, and `doctor` is what settles the assertion.
            return "unknown"
        return "overlapping"

    def query_provenance(
        self,
        metric_name: str,
        dimension_source: Optional[str] = None,
        *,
        grain: str = "day",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Optional[str]:
        """The statement behind this series: what ran if anything did, else
        what would run for the given window.

        The executed statement is preferred — it is the one that produced the
        number. But a snapshot hit serves the series without executing
        anything, and answering "no query" there would understate how
        defensible the number is: the binding still determines it exactly.
        Generating for the loaded window closes that gap, and the caller
        labels which of the two it got.
        """
        key = metric_name if dimension_source is None else f"{metric_name}::{dimension_source}"
        ran = self.last_sql.get(key)
        if ran is not None:
            return ran
        if start_date is None or end_date is None:
            return None
        bind = self.bindings.get(metric_name)
        if bind is None or (dimension_source and dimension_source not in bind.dimensions):
            return None
        try:
            return build_query(
                bind,
                grain=grain,
                start_date=start_date,
                end_date=end_date,
                dialect=self.dialect,
                dimension=dimension_source,
            )
        except Exception:
            return None

    def executed(self, metric_name: str, dimension_source: Optional[str] = None) -> bool:
        """Whether the statement provenance reports actually ran this process."""
        key = metric_name if dimension_source is None else f"{metric_name}::{dimension_source}"
        return key in self.last_sql

    def fetch_entity_flows(
        self,
        metric_name: str,
        dimension_source: str,
        reference_start: str,
        reference_end: str,
        analysis_start: str,
        analysis_end: str,
    ) -> pd.DataFrame:
        """`[reference_slice, analysis_slice, entities]` between two windows.

        Only available for a binding that declares `entity_grain`: classifying
        an entity as new, churned or migrated needs one slice per entity per
        window, and which one is the author's `resolve` choice, not ours.
        """
        bind = self.binding(metric_name)
        sql = build_entity_flow_query(
            bind,
            dimension=dimension_source,
            reference_start=reference_start,
            reference_end=reference_end,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            dialect=self.dialect,
        )
        self.last_sql[f"{metric_name}::{dimension_source}::flows"] = sql
        df = self._query(sql)
        df["entities"] = df["entities"].astype(int)
        return df

    # -- the grain claim --

    def check_grain(
        self,
        metric_name: str,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> tuple[int, int]:
        """`(rows, distinct grain_keys)` for a binding's relation.

        Equal means the relation really is one row per grain, so a many-to-one
        join cannot fan out. Unequal means every aggregate over it is silently
        multiplied. MetricFlow and Cube cannot make this check — they take
        declared relationships on trust — which is the argument for owning the
        contract.
        """
        bind = self.binding(metric_name)
        sql = build_grain_assertion(
            bind, dialect=self.dialect, start_date=start_date, end_date=end_date
        )
        self.last_sql[f"{metric_name}::grain"] = sql
        row = self._query(sql).iloc[0]
        return int(row["rows"]), int(row["distinct_keys"])


def fetcher_from_project(
    project_path: str,
    *,
    target: Optional[str] = None,
    profiles_dir: Optional[str] = None,
    overrides: Optional[Dict[str, BindingSpec]] = None,
) -> DbtDataFetcher:
    """Build a fetcher from a dbt project on disk.

    Bindings come from the semantic manifest; `overrides` (a node's own `bind:`
    block) win over it, so a tree can correct or extend what dbt declares
    without editing the dbt project.
    """
    out = resolve_profile(project_path, target=target, profiles_dir=profiles_dir)
    bindings = dict(bridge_project(project_path).bindings)
    if overrides:
        bindings.update(overrides)
    return DbtDataFetcher(
        bindings,
        connect=lambda: connect_from_profile(out),
        dialect=dialect_for_adapter(out.get("type")),
    )

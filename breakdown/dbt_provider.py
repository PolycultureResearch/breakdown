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
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import yaml

from breakdown.data_fetch import (
    OTHER_SLICE,
    SLICE_ROLLUP,
    BaseDataFetcher,
    MissingProviderExtra,
    ReservedSliceValue,
    SliceNotSupported,
    SliceSelection,
    _align_to_spine,
    _floor_labels,
    _require_module,
    _to_naive_dates,
    label_slices,
    reserved_slice_refusal,
)
from breakdown.dbt_bridge import bridge_project
from breakdown.dbt_sql import (
    ROLLUP_N_DISTINCT_COL,
    ROLLUP_N_FOLDED_COL,
    ROLLUP_OTHER_COL,
    ROLLUP_RESERVED_COL,
    _require_sqlglot,
    build_entity_flow_query,
    build_filter_probe,
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
    con = duckdb.connect(out.get("path") or ":memory:", read_only=bool(out.get("path")))
    _pin_utc(con)
    return con


def _pin_utc(con: Any) -> None:
    """Pin a DuckDB session to UTC (grill 2026-10-05 H4).

    DuckDB's session zone defaults to the process's, so a TIMESTAMPTZ column —
    which is what `read_csv_auto` makes of `2025-06-02T00:30:00Z` — was
    truncated and window-bounded in whatever `TZ` the server happened to start
    under: the same file, tree and window gave 11 orders in UTC, 1 in Los
    Angeles and 10 in Tokyo. An instant has one calendar date only relative to
    a zone, and the zone the answer depends on has to be one the tree's author
    can know, so it is UTC on every connection this module opens. A column
    that means local time should be stored as a DATE or a naive timestamp,
    which no zone touches.

    `SET GLOBAL`, not `SET`: `DbtDataFetcher._cursor` calls `con.cursor()`,
    which in DuckDB's Python client is a *new session* on the same database
    and inherits only the global value. A plain `SET` pinned the connection
    nothing ever queries through.
    """
    con.execute("SET GLOBAL TimeZone = 'UTC'")


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


def _weight_is_denominator(rate: "BindingSpec", weight: "BindingSpec", dimension: str) -> bool:
    """Whether `weight`'s sliced series is, row for row, the rate's Σden.

    Structural, not empirical: the same relation (or inline SQL), the same
    time column, the same filter list, the same dimension column and join,
    no entity-grain resolution on either side, and the weight summing exactly
    the rate's denominator column. Anything looser and Σnum / Σden in SQL
    could be a different number from the engine's Σ(r·w) / Σw.
    """
    if rate.agg != "ratio" or weight.agg != "sum":
        return False
    if weight.measure != rate.denominator:
        return False
    if (rate.relation, rate.sql) != (weight.relation, weight.sql):
        return False
    if rate.time_column != weight.time_column or list(rate.where) != list(weight.where):
        return False
    if rate.entity_grain is not None or weight.entity_grain is not None:
        return False
    rd, wd = rate.dimensions.get(dimension), weight.dimensions.get(dimension)
    if rd is None or wd is None:
        return False
    return rd.model_dump() == wd.model_dump()


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
        changed_files: Optional[Callable[[], List[str]]] = None,
        explain_error: Optional[Callable[[Exception], Optional[Exception]]] = None,
    ):
        self.bindings = dict(bindings)
        self._connect = connect
        self.dialect = dialect
        self._conn: Any = None
        # Two things only a file-backed source has, handed in rather than
        # subclassed (see `fetcher_from_data_dir`): which of its files changed
        # since they were first read, and a better sentence for an error the
        # engine underneath words for somebody else.
        self._changed_files = changed_files
        self._explain_error = explain_error
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
        except Exception as e:
            better = self._explain_error(e) if self._explain_error else None
            if better is None:
                raise
            raise better from e
        finally:
            cursor.close()

    # -- staleness --

    def changed_files(self) -> List[str]:
        """Names of the data files rewritten or removed since they were
        loaded; always empty for a warehouse. Two `stat` calls per file."""
        return list(self._changed_files()) if self._changed_files else []

    def _refuse_stale_files(self, what: str) -> None:
        """Refuse an analysis-time read once a data file has changed under
        the running server (grill 2026-10-05 M9).

        The totals an analysis is reconciled against were fetched at load and
        held in `tree.data`; the views re-read the files on every query. After
        a rewrite the two describe different files, the slices stop summing to
        the total, and the reconciliation said "the dimension does not cleanly
        partition it" — the wrong cause, stated confidently. Refused rather
        than reloaded: a reload here would change the totals under every
        cached fit and analysis without anybody having asked for it.
        """
        changed = self.changed_files()
        if changed:
            raise DataFilesChanged(
                f"Data file(s) {', '.join(changed)} changed after this tree was "
                f"loaded (size or modification time differs), so {what} read now "
                "would not add up to the totals loaded at startup. Restart "
                "`breakdown serve` to load the new file; nothing was reloaded."
            )

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
        sparse: bool = False,
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
            df, metric_name, grain, kind, start_date, end_date, value_col="value", sparse=sparse
        )

    def slice_rollup_refusal(
        self,
        metric_name: str,
        dimension_source: str,
        kind: str,
        weight_metric: Optional[str] = None,
    ) -> Optional[str]:
        """Whether the `top_k`/`values` roll-up can happen in the generated SQL.

        The generated fold must be *the* fold — the number the engine would
        have produced from the whole frame — or it must not happen (roadmap
        C32, and the four rules' first). Three cases the SQL cannot reproduce:

        * a **stock**: the engine forward-fills an absent period before it
          ranks, and a sum in SQL cannot see the fill;
        * a **rate whose weight is not its denominator**: the engine folds
          rates weighted by the declared `weight` metric, and Σnum / Σden is
          that number only when the weight *is* the denominator, binding for
          binding (same relation, filter, time column and dimension column);
        * a rate bound with anything but `agg: ratio`, or a weight bound with
          anything but `agg: sum` over the denominator column.

        The sentence returned is what the payload carries as the reason the
        roll-up ran client-side; None means the SQL may fold.
        """
        bind = self.bindings.get(metric_name)
        if bind is None or dimension_source not in bind.dimensions:
            return None  # the fetch itself will refuse, by name
        if kind == "stock":
            return (
                "stocks are forward-filled across absent periods before ranking, "
                "which a warehouse sum cannot reproduce; the roll-up ran after the fetch."
            )
        if kind == "rate":
            if bind.agg != "ratio":
                return (
                    f"'{metric_name}' is a rate bound with `agg: {bind.agg}`, so its "
                    "slices cannot be folded as Σnumerator / Σdenominator in SQL; "
                    "the roll-up ran after the fetch."
                )
            weight = self.bindings.get(weight_metric) if weight_metric else None
            if weight is None:
                return (
                    f"the weight metric '{weight_metric}' for rate '{metric_name}' has "
                    "no binding of its own to compare with the rate's denominator; "
                    "the roll-up ran after the fetch."
                )
            if not _weight_is_denominator(bind, weight, dimension_source):
                return (
                    f"the weight '{weight_metric}' is not provably '{metric_name}''s "
                    "denominator (same relation, filter, time column and dimension "
                    "column, `agg: sum` over the denominator), so a SQL fold could "
                    "differ from the engine's weighted merge; the roll-up ran after "
                    "the fetch."
                )
        return None

    def fetch_metric_sliced(
        self,
        metric_name: str,
        dimension_source: str,
        start_date: str,
        end_date: str,
        grain: str = "day",
        kind: str = "flow",
        selection: Optional[SliceSelection] = None,
    ) -> pd.DataFrame:
        bind = self.binding(metric_name)
        self._refuse_stale_files(f"the slices of '{metric_name}'")
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
                selection=selection,
            )
        else:
            sql = build_query(
                bind,
                grain=grain,
                start_date=start_date,
                end_date=end_date,
                dialect=self.dialect,
                dimension=dimension_source,
                selection=selection,
            )
        self.last_sql[f"{metric_name}::{dimension_source}"] = sql
        df = self._query(sql)
        missing = {"date", "slice", "value"} - set(df.columns)
        if selection is not None:
            missing |= {
                ROLLUP_OTHER_COL,
                ROLLUP_N_DISTINCT_COL,
                ROLLUP_N_FOLDED_COL,
                ROLLUP_RESERVED_COL,
            } - set(df.columns)
        if missing:
            raise RuntimeError(
                f"Sliced query for '{metric_name}' is missing {sorted(missing)}; "
                f"got {list(df.columns)}."
            )
        rollup = None
        if selection is not None:
            # The fold rows come back with a NULL slice and `bd_other = 1`,
            # distinct from a kept NULL dimension value (`bd_other = 0`), so
            # `__null__` stays a real slice and `__other__` the roll-up.
            other = df[ROLLUP_OTHER_COL].fillna(0).astype(int) == 1
            n_distinct = int(df[ROLLUP_N_DISTINCT_COL].iloc[0]) if len(df) else 0
            n_folded = int(df[ROLLUP_N_FOLDED_COL].iloc[0]) if len(df) else 0
            rollup = {"where": "sql", "n_distinct": n_distinct, "n_folded": n_folded}
            # A real value spelled like a reserved label, anywhere among the
            # distinct values — kept or folded — is refused before the fold
            # rows are given that label (grill 2026-10-05 L1). It used to
            # collide with them and surface as "more than one row per (date,
            # slice)", which sent the reader looking for a fan-out.
            reserved = df[ROLLUP_RESERVED_COL].iloc[0] if len(df) else None
            if reserved is not None and not pd.isna(reserved):
                raise ReservedSliceValue(reserved_slice_refusal(str(reserved), metric_name))
            df = df[["date", "slice", "value"]].copy()
            labels = label_slices(df["slice"], metric_name)
            labels[other.to_numpy()] = OTHER_SLICE
            df["slice"] = labels
        else:
            # Built directly rather than through `_sliced_long`, which finds its
            # date column by looking for `metric_time` — a MetricFlow name this
            # provider never produces, because it names the column itself.
            df = df[["date", "slice", "value"]].copy()
            df["slice"] = label_slices(df["slice"], metric_name)
        df["date"] = pd.to_datetime(df["date"])
        df = _to_naive_dates(df, metric_name)
        df = _floor_labels(df, metric_name, grain)
        df["value"] = df["value"].astype(float)
        df = df.sort_values(["date", "slice"]).reset_index(drop=True)
        if rollup is not None:
            logger.info(
                "Sliced '%s' by '%s' rolled up in SQL: %d distinct value(s), %d folded "
                "into __other__.",
                metric_name,
                dimension_source,
                rollup["n_distinct"],
                rollup["n_folded"],
            )
            df.attrs[SLICE_ROLLUP] = rollup
        return df

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
        self._refuse_stale_files(f"the entity flows of '{metric_name}'")
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

    # -- the filter claim --

    def check_filter(
        self,
        metric_name: str,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> tuple[int, int]:
        """`(rows, kept)` for a filtered binding over the probe window.

        `0 < kept < rows` is the predicate doing something on this warehouse in
        this dialect against these columns — which is a claim about the *data*,
        not about the metadata, and neither MetricFlow nor Cube makes it. The
        two degenerate answers are the ones worth catching: `kept == 0` is a
        dialect-hostile predicate that would serve an empty series, and
        `kept == rows` is a predicate that excluded nothing, which is C15's
        original defect arriving through a new door.
        """
        bind = self.binding(metric_name)
        sql = build_filter_probe(
            bind, dialect=self.dialect, start_date=start_date, end_date=end_date
        )
        self.last_sql[f"{metric_name}::filter"] = sql
        row = self._query(sql).iloc[0]
        # A relation with no rows in the window makes `SUM(...)` NULL rather
        # than 0; that is `rows == 0`, which the caller reports as "nothing to
        # check over this window" rather than as an excluded-everything failure.
        kept = row["kept"]
        return int(row["rows"]), int(kept) if pd.notna(kept) else 0


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
    # The profile is resolved first so the bridge knows which dialect a filter
    # predicate has to parse in. A predicate that parses generically may mean
    # something else where it will actually run, and this module's rule is
    # generate in the target dialect, never translate into it.
    dialect = dialect_for_adapter(out.get("type"))
    bindings = dict(bridge_project(project_path, dialect).bindings)
    if overrides:
        bindings.update(overrides)
    return DbtDataFetcher(
        bindings,
        connect=lambda: connect_from_profile(out),
        dialect=dialect,
    )


# --- the `duckdb` provider: bindings over a folder of exports (roadmap 2.2) ---

# File extension -> the DuckDB table function that reads it. Only files
# directly inside `data_dir` count; a subfolder is not a relation.
DATA_FILE_READERS = {".csv": "read_csv_auto", ".parquet": "read_parquet"}


def list_data_files(data_dir: str) -> Dict[str, str]:
    """`{relation name: file name}` for every export in `data_dir`.

    The relation name is the file stem (`orders.csv` -> `orders`). Three things
    are refused by name rather than smoothed over, since each would otherwise
    surface as an empty series or a wrong one: a folder that is not there, a
    folder with nothing readable in it, and two files claiming one stem
    (`orders.csv` beside `orders.parquet` — which one the view read would be
    an accident of listing order).
    """
    if not os.path.isdir(data_dir):
        raise RuntimeError(
            f"duckdb `data_dir` not found: {data_dir}. Point it at the folder holding "
            "the .csv / .parquet exports (a relative path resolves against the tree file)."
        )
    tables: Dict[str, str] = {}
    for name in sorted(os.listdir(data_dir)):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in DATA_FILE_READERS or not os.path.isfile(os.path.join(data_dir, name)):
            continue
        if stem in tables:
            raise RuntimeError(
                f"Two files in {data_dir} map to the relation '{stem}' "
                f"({tables[stem]}, {name}); rename one."
            )
        tables[stem] = name
    if not tables:
        raise RuntimeError(
            f"No .csv or .parquet files directly inside duckdb `data_dir` {data_dir}."
        )
    return tables


class DataFilesChanged(ValueError):
    """A data file was rewritten or removed after the tree loaded from it.

    A `ValueError` so the slice route's existing 422 mapping carries the
    sentence — which names the file and the remedy — to the reader."""


class AmbiguousDateFormat(RuntimeError):
    """A CSV time column whose dates read as day-first or month-first alike,
    with no `date_format` declared to settle it."""


class DateFormatMismatch(RuntimeError):
    """A declared `date_format` that some value of the time column, or the
    column's own type, does not match."""


class OutsideDataDir(RuntimeError):
    """A query reached for a file or URL outside the confined `data_dir`."""


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(name: str) -> str:
    """A DuckDB identifier, quoted. A stem is whatever the file was called, so
    `a"b.csv` reaches here, and unescaped it ended the identifier early and
    took `CREATE VIEW` — and with it every metric in the tree — down."""
    return '"' + name.replace('"', '""') + '"'


_BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_QUOTED_IDENTIFIER = re.compile(r'"((?:[^"]|"")+)"\Z')


def _identifier_name(expr: Optional[str]) -> Optional[str]:
    """The name `expr` refers to when it is a lone identifier, bare or
    double-quoted; None when it is an expression (or absent)."""
    token = (expr or "").strip()
    if _BARE_IDENTIFIER.match(token):
        return token
    quoted = _QUOTED_IDENTIFIER.match(token)
    return quoted.group(1).replace('""', '"') if quoted else None


def _fingerprint(path: str) -> Optional[Tuple[int, int]]:
    """`(size, mtime_ns)`, or None when the file is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


# How a slashed, dotted or dashed numeric date begins: two short fields, then
# the year. A year-first value (`2025-01-02`) does not match — its first field
# is four digits — which is the point: ISO order is the one nobody reverses.
_NUMERIC_DATE = r"^\s*(\d{1,2})([/.\-])(\d{1,2})([/.\-])(\d{2,4})(?:\D|$)"
_NUMERIC_DATE_RE = re.compile(_NUMERIC_DATE)
_TIME_SUFFIX = re.compile(r"([ T])(\d{1,2}):(\d{2})(:\d{2})?")


def _suggested_formats(sample: str) -> Tuple[str, str]:
    """`(month-first, day-first)` strptime formats shaped like `sample`."""
    m = _NUMERIC_DATE_RE.match(sample)
    if m is None:  # pragma: no cover - the caller matched the same pattern in SQL
        return "%m/%d/%Y", "%d/%m/%Y"
    sep1, sep2, year = m.group(2), m.group(4), m.group(5)
    y = "%Y" if len(year) == 4 else "%y"
    rest = _TIME_SUFFIX.match(sample[m.end(5) :])
    clock = f"{rest.group(1)}%H:%M{':%S' if rest.group(4) else ''}" if rest else ""
    return f"%m{sep1}%d{sep2}{y}{clock}", f"%d{sep1}%m{sep2}{y}{clock}"


class DataDir:
    """The folder behind the `duckdb` provider: its connection, and what the
    files looked like when that connection was first opened.

    Four things happen at `connect`, in an order that matters:

    1. **Fingerprint** every file (size, mtime) before reading any, so a file
       rewritten *during* the load is as stale as one rewritten after it.
    2. **Pin UTC** (`_pin_utc`), then **confine** the connection to `data_dir`
       unless `allow_external_access` says otherwise: `allowed_directories`
       first — it cannot be widened once external access is off —, then
       `enable_external_access = false`.
    3. **Settle every time column's date format** (`_time_column_plan`) and
       create one view per file, reading a governed column as text and parsing
       it with the declared format.
    4. **Lock the configuration**, last, so nothing a tree's SQL can say
       reopens what step 2 closed.

    Not a fetcher: `fetcher_from_data_dir` hands its three methods to the one
    `DbtDataFetcher` every bound provider uses.
    """

    def __init__(
        self,
        data_dir: str,
        bindings: Optional[Dict[str, BindingSpec]] = None,
        *,
        allow_external_access: bool = False,
    ):
        self.data_dir = data_dir
        self.bindings = dict(bindings or {})
        self.allow_external_access = allow_external_access
        # {file name: (size, mtime_ns)} as of the first `connect`. Bounded by
        # the number of files in one folder; never grows after load.
        self._loaded: Optional[Dict[str, Optional[Tuple[int, int]]]] = None

    # -- staleness (M9) --

    def changed_files(self) -> List[str]:
        if self._loaded is None:
            return []
        return [
            name
            for name, was in self._loaded.items()
            if _fingerprint(os.path.join(self.data_dir, name)) != was
        ]

    # -- confinement (M10) --

    def explain_error(self, exc: Exception) -> Optional[Exception]:
        """DuckDB's refusal of a path outside `data_dir`, in the tree author's
        terms. Its own wording ("file system operations are disabled by
        configuration") names a configuration the author never wrote."""
        text = str(exc)
        if self.allow_external_access or "disabled by configuration" not in text:
            return None
        return OutsideDataDir(
            f"The duckdb provider reads only the files inside its `data_dir` "
            f"({self.data_dir}), and this query reached outside it: "
            f"{text.splitlines()[0]}. Move the file into `data_dir` and bind it "
            "by its stem, or set `allow_external_access: true` under `provider:` "
            "if this tree reads another folder, https:// or s3:// on purpose."
        )

    # -- the connection --

    def connect(self) -> Any:
        duckdb = _require_module("duckdb", "duckdb", "duckdb")
        tables = list_data_files(self.data_dir)
        if self._loaded is None:
            # First connection only: a reconnect must not quietly re-baseline
            # against a file the loaded totals never saw.
            self._loaded = {
                name: _fingerprint(os.path.join(self.data_dir, name)) for name in tables.values()
            }
        con = duckdb.connect()
        _pin_utc(con)
        if not self.allow_external_access:
            con.execute(f"SET allowed_directories = [{_sql_string(self.data_dir)}]")
            con.execute("SET enable_external_access = false")
        try:
            plan = self._time_column_plan(con, tables)
            for stem, name in tables.items():
                con.execute(
                    f"CREATE VIEW {_sql_identifier(stem)} AS "
                    + self._view_select(name, plan.get(stem, {}))
                )
            if not self.allow_external_access:
                con.execute("SET lock_configuration = true")
        except Exception:
            con.close()
            raise
        return con

    def _reader(self, name: str, *, options: str = "") -> str:
        function = DATA_FILE_READERS[os.path.splitext(name)[1].lower()]
        return f"{function}({_sql_string(os.path.join(self.data_dir, name))}{options})"

    def _raw_text(self, name: str) -> str:
        """The file with every column as the text that is in it. For a CSV
        that is the bytes between the delimiters, before any type was sniffed;
        Parquet is typed at rest, so its own reader is already the answer."""
        if name.lower().endswith(".csv"):
            return self._reader(name, options=", all_varchar = true")
        return self._reader(name)

    def _view_select(self, name: str, formats: Dict[str, str]) -> str:
        """`SELECT *`, with each column that has a declared format read as text
        and parsed by `strptime` — which raises on a value that does not match,
        so a row that arrives later in the wrong shape is an error at the query
        and never a NULL that drops out of the window."""
        if not formats:
            return f"SELECT * FROM {self._reader(name)}"
        replaced = ", ".join(
            f"strptime({_sql_identifier(col)}, {_sql_string(fmt)}) AS {_sql_identifier(col)}"
            for col, fmt in formats.items()
        )
        options = ""
        if name.lower().endswith(".csv"):
            types = ", ".join(f"{_sql_string(col)}: 'VARCHAR'" for col in formats)
            options = f", types = {{{types}}}"
        return f"SELECT * REPLACE ({replaced}) FROM {self._reader(name, options=options)}"

    # -- date formats (H2) --

    def _stems_read(self, bind: BindingSpec, tables: Dict[str, str]) -> List[str]:
        """The data files a binding reads, by stem.

        A `relation` is one name. A `bind.sql` is parsed for the tables it
        names; a statement that does not parse reads nothing as far as this is
        concerned, and fails on its own terms when it runs.
        """
        by_lower = {stem.lower(): stem for stem in tables}
        if bind.relation is not None:
            name = _identifier_name(bind.relation)
            stem = by_lower.get(name.lower()) if name else None
            return [stem] if stem else []
        sqlglot = _require_sqlglot()
        try:
            parsed = sqlglot.parse_one(bind.sql, read="duckdb")
        except Exception:
            return []
        named = {t.name.lower() for t in parsed.find_all(sqlglot.exp.Table) if not t.db}
        return [stem for low, stem in by_lower.items() if low in named]

    def _time_column_plan(self, con: Any, tables: Dict[str, str]) -> Dict[str, Dict[str, str]]:
        """`{stem: {column: date_format}}` for the views to apply — after
        refusing any time column whose dates nothing settles.

        **How ambiguity is decided** (grill 2026-10-05 H2). `read_csv_auto`
        picks a date format by trying candidates against a sample, and for a
        monthly export `01/01/2025 … 12/01/2025` both `%d/%m/%Y` and
        `%m/%d/%Y` parse every row; it took day-first, and twelve months
        became the first twelve days of January with no word said. The type
        DuckDB reports afterwards (`DATE`) carries none of that, so the type is
        not what is inspected. The **raw text** is: the column is re-read with
        every value as a string (`all_varchar`), and each value that begins
        like a numeric date with the year last (`_NUMERIC_DATE`) gives up its
        first two fields. The column is ambiguous when

        * neither field ever exceeds 12 — nothing in the file distinguishes a
          day from a month — or
        * the year has fewer than four digits, where `01/02/03` has three
          readings and a 13 in any field rules out only one of them.

        Year-first text (`2025-01-31`, `2025/01/31`, with or without a time) is
        ISO order and never ambiguous; so is anything with a month name. A
        column that *is* settled by its own values (a `13/01/2025` somewhere)
        is left to DuckDB, which either reads it that way or fails to type it
        and says so. Parquet stores a typed DATE/TIMESTAMP and is not text to
        misread.

        A **declared** `date_format` skips the question: the column is read as
        text and every non-empty value must parse with it, checked here — once,
        over the whole file — so the refusal names the file and a value rather
        than surfacing as a conversion error in the middle of a fit.

        Which column: the binding's `time_column` when it is a plain column
        name, in every file the binding reads that has a column by that name.
        A time *expression*, or a `bind.sql` that renames or derives its time
        column, is not something this can trace; every other date-typed column
        of a file some binding reads is therefore scanned too and, when
        ambiguous, **warned** about by name. Warned, not refused, because no
        binding is known to use it and `date_format` could not fix it.
        """
        # (stem, lowercased column) -> {"format": ..., "metrics": [...]}
        governed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        columns_of: Dict[str, Dict[str, Tuple[str, str]]] = {}
        read_by_a_binding: List[str] = []

        def columns(stem: str) -> Dict[str, Tuple[str, str]]:
            if stem not in columns_of:
                rows = con.execute(
                    f"DESCRIBE SELECT * FROM {self._reader(tables[stem])}"
                ).fetchall()
                columns_of[stem] = {r[0].lower(): (r[0], str(r[1]).upper()) for r in rows}
            return columns_of[stem]

        for metric, bind in self.bindings.items():
            stems = self._stems_read(bind, tables)
            read_by_a_binding.extend(s for s in stems if s not in read_by_a_binding)
            column = _identifier_name(bind.time_column)
            applied = False
            for stem in stems if column else []:
                if column.lower() not in columns(stem):
                    continue
                applied = True
                entry = governed.setdefault((stem, column.lower()), {"format": None, "metrics": []})
                entry["metrics"].append(metric)
                if bind.date_format is None:
                    continue
                if entry["format"] not in (None, bind.date_format):
                    raise DateFormatMismatch(
                        f"Metrics {entry['metrics']} read time column '{column}' of "
                        f"data file '{tables[stem]}' with two different `date_format`s "
                        f"({entry['format']!r} and {bind.date_format!r}). One column "
                        "has one format; declare the same one on each binding."
                    )
                entry["format"] = bind.date_format
            if bind.date_format is not None and not applied:
                raise DateFormatMismatch(
                    f"Metric '{metric}' declares `date_format: {bind.date_format!r}`, "
                    f"but its time column ({bind.time_column!r}) is not a plain column "
                    f"of a data file the binding reads (files: {sorted(tables.values())}). "
                    "`date_format` parses a file's own column; for a derived or renamed "
                    "time column, parse it in `bind.sql` with strptime(<column>, '<format>') "
                    "and drop `date_format`."
                )

        plan: Dict[str, Dict[str, str]] = {}
        for (stem, low), entry in governed.items():
            name = tables[stem]
            column, sql_type = columns(stem)[low]
            if entry["format"] is not None:
                self._check_declared_format(con, name, column, sql_type, entry)
                plan.setdefault(stem, {})[column] = entry["format"]
            elif name.lower().endswith(".csv"):
                found = self._ambiguous_dates(con, name, column)
                if found is not None:
                    sample, why = found
                    month_first, day_first = _suggested_formats(sample)
                    raise AmbiguousDateFormat(
                        f"Data file '{name}' in {self.data_dir}: time column '{column}' "
                        f"holds dates such as '{sample}' that read as month-first or "
                        f"day-first alike, and {why}. DuckDB would pick one without "
                        "saying which, and the wrong pick moves every row to another "
                        "month. Declare the format on the binding of "
                        f"{', '.join(repr(m) for m in entry['metrics'])}:\n"
                        "    bind:\n"
                        f"      date_format: '{month_first}'    # month first; "
                        f"day first is '{day_first}'"
                    )

        for stem in read_by_a_binding:
            name = tables[stem]
            if not name.lower().endswith(".csv"):
                continue
            for low, (column, sql_type) in columns(stem).items():
                if (stem, low) in governed or not sql_type.startswith(("DATE", "TIME")):
                    continue
                found = self._ambiguous_dates(con, name, column)
                if found is not None:
                    logger.warning(
                        "Data file '%s': column '%s' holds dates such as '%s' that read "
                        "as month-first or day-first alike (%s), and DuckDB read it as "
                        "%s without saying which. It is not the plain `time_column` of "
                        "any binding, so it is not refused; if a `bind.sql` or a time "
                        "expression uses it, read it as text and parse it there with "
                        "strptime().",
                        name,
                        column,
                        found[0],
                        found[1],
                        sql_type,
                    )
        return plan

    def _ambiguous_dates(self, con: Any, name: str, column: str) -> Optional[Tuple[str, str]]:
        """`(sample value, why)` when the raw text of a CSV column is
        ambiguous between day-first and month-first; None when it is not."""
        col = _sql_identifier(column)
        row = con.execute(
            "SELECT COUNT(*) FILTER (WHERE p.a <> ''), "
            "MAX(TRY_CAST(NULLIF(p.a, '') AS INTEGER)), "
            "MAX(TRY_CAST(NULLIF(p.b, '') AS INTEGER)), "
            "MIN(LENGTH(NULLIF(p.y, ''))), "
            "MIN(raw) FILTER (WHERE p.a <> '') "
            f"FROM (SELECT {col} AS raw, regexp_extract({col}, {_sql_string(_NUMERIC_DATE)}, "
            f"['a', 's1', 'b', 's2', 'y']) AS p FROM {self._raw_text(name)})"
        ).fetchone()
        n, max_a, max_b, min_year, sample = row
        if not n:
            return None
        if min_year is not None and min_year < 4:
            return str(sample), "the year has fewer than four digits, so even its place is a guess"
        if (max_a or 0) <= 12 and (max_b or 0) <= 12:
            return str(sample), "no value in the file has a day above 12 to settle it"
        return None

    def _check_declared_format(
        self, con: Any, name: str, column: str, sql_type: str, entry: Dict[str, Any]
    ) -> None:
        fmt = entry["format"]
        whom = ", ".join(repr(m) for m in entry["metrics"])
        if not name.lower().endswith(".csv") and sql_type != "VARCHAR":
            raise DateFormatMismatch(
                f"Metric(s) {whom} declare `date_format: {fmt!r}` for column "
                f"'{column}' of '{name}', which is already stored as {sql_type}. "
                "A typed column has no text to parse; drop `date_format`."
            )
        col = _sql_identifier(column)
        bad = f"{col} IS NOT NULL AND try_strptime({col}, {_sql_string(fmt)}) IS NULL"
        try:
            n_bad, sample, n = con.execute(
                f"SELECT COUNT(*) FILTER (WHERE {bad}), MIN({col}) FILTER (WHERE {bad}), "
                f"COUNT({col}) FROM {self._raw_text(name)}"
            ).fetchone()
        except Exception as e:
            raise DateFormatMismatch(
                f"`date_format: {fmt!r}` (metric(s) {whom}) could not be applied to "
                f"column '{column}' of '{name}': {str(e).splitlines()[0]}"
            ) from e
        if n_bad:
            raise DateFormatMismatch(
                f"Data file '{name}' in {self.data_dir}: {n_bad} of {n} value(s) in time "
                f"column '{column}' do not match the declared `date_format: {fmt!r}` "
                f"(metric(s) {whom}), for example '{sample}'. A row that does not parse "
                "is refused rather than dropped; fix the format or the file."
            )


def open_data_dir(
    data_dir: str,
    bindings: Optional[Dict[str, BindingSpec]] = None,
    *,
    allow_external_access: bool = False,
) -> Any:
    """An in-memory DuckDB connection with one view per export in `data_dir`.

    In memory on purpose: the files are the source of truth and nothing is
    written beside them. The `duckdb` module is imported here, at the point of
    use, so a base install can parse a `duckdb` tree and fail with the extra
    to install rather than an ImportError. See `DataDir.connect` for what the
    connection is pinned and confined to.
    """
    return DataDir(data_dir, bindings, allow_external_access=allow_external_access).connect()


def _quoted_file_relations(
    bindings: Dict[str, BindingSpec], tables: Dict[str, str]
) -> Dict[str, BindingSpec]:
    """Bindings with every relation that *is* a data-file stem quoted as an
    identifier (grill 2026-10-05 L2).

    `list_data_files` advertises the stem as the relation name, and a stem is
    whatever the export was called: `orders-2025`, `2025_orders` and
    `Orders Export` are all ordinary, and none is a SQL identifier unquoted —
    the first two died in the SQL parser and the third never got past the tree
    parser. The author wrote the name the folder shows; quoting it is this
    provider's job. An exact, case-sensitive match only: anything else is the
    author's own SQL and is left as written.
    """

    def quote(relation: Optional[str], where: str) -> Optional[str]:
        if relation is None:
            return None
        if relation in tables:
            return _sql_identifier(relation)
        if any(c.isspace() for c in relation) and _identifier_name(relation) is None:
            raise RuntimeError(
                f"{where} names relation '{relation}', which is not a data file in "
                f"`data_dir` (have: {sorted(tables)}) and is not one SQL name either. "
                "Use a file's stem exactly as listed, or `bind.sql` for a query."
            )
        return relation

    out: Dict[str, BindingSpec] = {}
    for metric, bind in bindings.items():
        update: Dict[str, Any] = {"relation": quote(bind.relation, f"Metric '{metric}'")}
        if bind.entity_grain is not None and bind.entity_grain.relation is not None:
            update["entity_grain"] = bind.entity_grain.model_copy(
                update={
                    "relation": quote(
                        bind.entity_grain.relation, f"Metric '{metric}' (`entity_grain`)"
                    )
                }
            )
        if any(d.join is not None for d in bind.dimensions.values()):
            update["dimensions"] = {
                key: d.model_copy(
                    update={"join": quote(d.join, f"Metric '{metric}' (dimension '{key}')")}
                )
                for key, d in bind.dimensions.items()
            }
        out[metric] = bind.model_copy(update=update)
    return out


def fetcher_from_data_dir(
    data_dir: str,
    bindings: Dict[str, BindingSpec],
    *,
    allow_external_access: bool = False,
) -> DbtDataFetcher:
    """Build the `duckdb` provider's fetcher: the same `DbtDataFetcher` that
    serves the `dbt` provider, with the tree's own `bind:` blocks as the whole
    binding set and a connection over the exports in `data_dir`.

    No new fetcher class, deliberately. Everything a binding buys — the grain
    claim `doctor` asserts, declared dimensions, `agg: ratio` decomposition,
    the SQL provenance surface — is `DbtDataFetcher`'s already, and a second
    class would be the "same policy, different file" defect the four rules
    exist to prevent. What a folder has and a warehouse does not (files that
    change under the server, a boundary to stay inside) lives on `DataDir`
    and is handed in as callables. `connect` stays a zero-argument callable so
    the fetcher constructs without opening a file.

    The folder is *listed* here, failure-soft, so relations can be quoted
    before the first statement is built; a folder that is not there is still
    reported by `connect`, by name, at the first fetch.
    """
    if allow_external_access:
        logger.warning(
            "duckdb provider: `allow_external_access: true` — the connection over %s "
            "is NOT confined to that folder. A `relation` or `bind.sql` in this tree "
            "can read any local file this process can, and any https:// or s3:// "
            "path. Leave it off unless the tree file is as trusted as the server.",
            data_dir,
        )
    try:
        tables = list_data_files(data_dir)
    except RuntimeError:
        tables = {}
    if tables:
        bindings = _quoted_file_relations(bindings, tables)
    source = DataDir(data_dir, bindings, allow_external_access=allow_external_access)
    return DbtDataFetcher(
        bindings,
        connect=source.connect,
        dialect="duckdb",
        changed_files=source.changed_files,
        explain_error=source.explain_error,
    )

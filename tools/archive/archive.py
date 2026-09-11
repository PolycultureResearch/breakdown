#!/usr/bin/env python
"""Warehouse archive: extract a de-identified, static copy of a client's marts.

Why this exists
---------------
An engagement ends and the warehouse goes away with it. What made that dataset
worth testing against was never the numbers; it was the exceptions -- an
event-grained relation where `new` does not mean new, a governed metric that
disagrees with its hand-written twin, a metric whose meaning changed under it
when the business model did. Those are only reachable while you can still ask
questions you have not thought of yet, which means keeping the relations at row
grain, not a frozen set of answers.

So: pull the marts, de-identify them on the way past without breaking anything
Breakdown relies on, land them as parquet, and load them into DuckDB where
`dbt parse --target duckdb` re-resolves `node_relation` against the local copy
and the `dbt` provider runs with nothing changed in the tree.

The four verbs
--------------
    plan     what is there and what would happen to every column. No data moves.
    extract  parquet + manifest. Raw rows never touch disk.
    load     build the DuckDB file the dbt project will point at.
    verify   prove the copy is faithful: row counts, keys, arithmetic identities.

Run them in that order. `plan` is not optional in spirit: it is where you read
the per-column disposition and catch the id column nobody thought to hash.

The invariant everything else serves
------------------------------------
Every transformation must preserve the identities in the tree. If
`net_new = new + expansion + contraction + churn + reactivation` stops holding
to the cent, every formula node fails to reconcile and every test result is a
false positive. That is why money scaling is a single global constant and never
per-row jitter, why the date offset is global and applied to every date column
in every table, and why `verify` refuses to pass on a broken identity.

The other invariant is join integrity. Hashing is keyed on the *value alone*,
never on the column name, so `user_id` in `dim_users` and `user_id` in
`fct_signups` land on the same hash and every join survives -- including joins
between columns that are named differently in different marts. `hash_domains`
exists for the rare case you want two id spaces separated, and using it is a
decision to break those joins.

The manifest is also the disclosure document
--------------------------------------------
`manifest.json` and the `DISCLOSURE.md` rendered beside it record exactly which
columns were taken, hashed, pseudonymized and dropped, per table, with row
counts. That is the artifact to put in front of the client when asking for
written permission, and the thing to re-read in a year when you have forgotten
what the archive contains.

Usage
-----
    export BREAKDOWN_ARCHIVE_SALT="$(openssl rand -hex 32)"   # keep this
    python tools/archive/archive.py plan    -c configs/<client>.yml
    python tools/archive/archive.py extract -c configs/<client>.yml -o ./archive
    python tools/archive/archive.py load    -c configs/<client>.yml -o ./archive
    python tools/archive/archive.py verify  -c configs/<client>.yml -o ./archive

Keep the salt. Re-extracting with a different one produces an archive whose ids
do not join to the first one, and `verify` will tell you so from the salt
fingerprint rather than letting you discover it in a broken RCA six months on.

Requires: pyyaml, pyarrow, duckdb, and for a Databricks source
`databricks-sql-connector` + `databricks-sdk` (the repo's `databricks` extra).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("archive.py needs pyyaml: pip install pyyaml")

try:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover
    sys.exit("archive.py needs pyarrow: pip install 'pyarrow>=15'")

log = logging.getLogger("archive")

SALT_ENV_DEFAULT = "BREAKDOWN_ARCHIVE_SALT"
BATCH_ROWS = 200_000

# Above this many distinct values we stop memoizing hashes and just compute
# them. A primary key is 1:1 so the cache never pays off there and would grow
# without bound; a foreign key repeats heavily and the cache is most of the
# speed. Bounding it keeps the pathological case from eating the machine.
HASH_CACHE_MAX = 2_000_000


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass
class Deident:
    salt_env: str = SALT_ENV_DEFAULT
    hash_columns: list[str] = field(default_factory=list)
    hash_patterns: list[str] = field(default_factory=list)
    hash_domains: dict[str, str] = field(default_factory=dict)
    pseudonymize_columns: dict[str, str] = field(default_factory=dict)
    drop_columns: list[str] = field(default_factory=list)
    drop_patterns: list[str] = field(default_factory=list)
    keep_columns: list[str] = field(default_factory=list)
    date_offset_days: int = 0
    money_multiplier: float = 1.0
    money_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._hash_re = [re.compile(p) for p in self.hash_patterns]
        self._drop_re = [re.compile(p) for p in self.drop_patterns]
        self._money_re = [re.compile(p) for p in self.money_patterns]
        self._hash_set = {c.lower() for c in self.hash_columns}
        self._drop_set = {c.lower() for c in self.drop_columns}
        self._keep_set = {c.lower() for c in self.keep_columns}
        self._pseudo = {k.lower(): v for k, v in self.pseudonymize_columns.items()}


@dataclass
class Table:
    relation: str
    tier: str = "core"
    time_column: str | None = None
    unique_on: list[str] = field(default_factory=list)
    where: str | None = None
    note: str = ""


@dataclass
class Identity:
    table: str
    expression: str
    tolerance: float = 0.01
    note: str = ""


@dataclass
class Config:
    name: str
    source: dict[str, Any]
    target: dict[str, Any]
    deident: Deident
    tables: list[Table]
    identities: list[Identity]
    raw: dict[str, Any]


def load_config(path: str) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    for key in ("name", "source", "tables"):
        if key not in raw:
            sys.exit(f"config {path} is missing required key `{key}`")
    tables = [Table(**t) for t in raw["tables"]]
    if len({t.relation for t in tables}) != len(tables):
        sys.exit("config lists the same relation twice")
    return Config(
        name=raw["name"],
        source=raw["source"],
        target=raw.get("target") or {},
        deident=Deident(**(raw.get("deident") or {})),
        tables=tables,
        identities=[Identity(**i) for i in raw.get("identities") or []],
        raw=raw,
    )


def get_salt(deid: Deident) -> bytes:
    salt = os.environ.get(deid.salt_env, "")
    if not salt:
        sys.exit(
            f"${deid.salt_env} is not set. The archive is de-identified with an "
            "HMAC keyed on this value, so running without one would either write "
            "raw ids or write hashes nobody can reproduce.\n\n"
            f"    export {deid.salt_env}=\"$(openssl rand -hex 32)\"\n\n"
            "Save it somewhere durable. Every later extract must use the same "
            "salt or its ids will not join to this one."
        )
    if len(salt) < 16:
        sys.exit(f"${deid.salt_env} is too short; use at least 32 hex characters.")
    return salt.encode()


def salt_fingerprint(salt: bytes) -> str:
    """A public identifier for a salt, so two extracts can be checked for
    consistency without either of them carrying the secret."""
    return hashlib.sha256(b"breakdown-archive-salt-fingerprint:" + salt).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# column disposition
# --------------------------------------------------------------------------- #

ACTION_ORDER = ["drop", "hash", "pseudonymize", "date_shift", "money_scale", "keep"]


def dispositions(name: str, arrow_type: Any, deid: Deident) -> list[str]:
    """What will happen to this column, as a list of actions.

    `keep_columns` wins over everything: it is the escape hatch for a column a
    pattern would otherwise mangle, and an escape hatch that can be overridden
    is not one.
    """
    low = name.lower()
    if low in deid._keep_set:
        return ["keep"]

    acts: list[str] = []
    if low in deid._drop_set or any(r.search(low) for r in deid._drop_re):
        return ["drop"]
    if low in deid._pseudo:
        acts.append("pseudonymize")
    elif low in deid._hash_set or any(r.search(low) for r in deid._hash_re):
        acts.append("hash")

    if deid.date_offset_days and _is_temporal(arrow_type):
        acts.append("date_shift")
    if deid.money_multiplier != 1.0 and _is_numeric(arrow_type):
        if any(r.search(low) for r in deid._money_re):
            acts.append("money_scale")

    return acts or ["keep"]


def _is_temporal(t: Any) -> bool:
    return t is not None and (pa.types.is_date(t) or pa.types.is_timestamp(t))


def _is_numeric(t: Any) -> bool:
    return t is not None and (
        pa.types.is_floating(t) or pa.types.is_decimal(t) or pa.types.is_integer(t)
    )


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #

_HASH_CACHE: dict[Any, str] = {}


def _hash_value(value: Any, salt: bytes, domain: str, prefix: str | None) -> Any:
    if value is None:
        return None
    key = (domain, value) if domain else value
    hit = _HASH_CACHE.get(key)
    if hit is not None:
        return hit
    msg = (domain + "\x00" if domain else "") + str(value)
    digest = hmac.new(salt, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    out = f"{prefix}_{digest[:12]}" if prefix else digest
    if len(_HASH_CACHE) < HASH_CACHE_MAX:
        _HASH_CACHE[key] = out
    return out


def _hash_column(col: pa.ChunkedArray, salt: bytes, domain: str, prefix: str | None) -> pa.Array:
    """Hash every value to a stable opaque string.

    Deliberately not vectorized through pyarrow: there is no HMAC kernel, and
    the memo cache is worth more than a kernel would be on the columns that
    matter (a foreign key repeats across millions of rows).
    """
    return pa.array(
        [_hash_value(v, salt, domain, prefix) for v in col.to_pylist()], type=pa.string()
    )


def _shift_temporal(col: pa.ChunkedArray, days: int) -> pa.Array:
    delta = timedelta(days=days)
    original = col.type
    try:
        # Arrow promotes date32 + duration to a timestamp. Casting back keeps
        # the archived column the same type the warehouse had, which matters:
        # a date column that silently became a timestamp changes how dbt and
        # DuckDB resolve it, and the tree's grain floors with it.
        return pc.add(col, pa.scalar(delta)).cast(original)
    except Exception:
        # date32 and some timestamp units do not accept a duration scalar on
        # every pyarrow build. Falling back through Python is slow and correct,
        # and this path is off unless someone opted into a date offset.
        out = []
        for v in col.to_pylist():
            if v is None:
                out.append(None)
            elif isinstance(v, datetime):
                out.append(v + delta)
            elif isinstance(v, date):
                out.append(v + delta)
            else:
                out.append(v)
        return pa.array(out, type=col.type)


def _scale_money(col: pa.ChunkedArray, factor: float) -> pa.Array:
    t = col.type
    if pa.types.is_decimal(t):
        # Decimal times float has no exact arrow kernel. Casting to float64 is
        # the honest trade and it is why money_multiplier should stay at 1.0
        # unless the client requires otherwise: exactness at the cent is what
        # the identity checks depend on.
        log.warning(
            "scaling decimal column through float64; identity tolerances may need widening"
        )
        col = col.cast(pa.float64())
    elif pa.types.is_integer(t):
        col = col.cast(pa.float64())
    return pc.multiply(col, pa.scalar(float(factor)))


def transform(batch: pa.Table, deid: Deident, salt: bytes) -> pa.Table:
    """Apply the de-identification to one batch.

    Order matters: drop first so nothing downstream reads a column that should
    not exist, then hash, then the value-preserving numeric transforms.
    """
    names, cols = [], []
    for name in batch.schema.names:
        col = batch.column(name)
        acts = dispositions(name, col.type, deid)
        if "drop" in acts:
            continue
        low = name.lower()
        if "pseudonymize" in acts:
            col = _hash_column(col, salt, deid.hash_domains.get(low, ""), deid._pseudo[low])
        elif "hash" in acts:
            col = _hash_column(col, salt, deid.hash_domains.get(low, ""), None)
        if "date_shift" in acts:
            col = _shift_temporal(col, deid.date_offset_days)
        if "money_scale" in acts:
            col = _scale_money(col, deid.money_multiplier)
        names.append(name)
        cols.append(col)
    return pa.Table.from_arrays([pa.chunked_array(c) if not isinstance(c, pa.ChunkedArray) else c for c in cols], names=names)


# --------------------------------------------------------------------------- #
# source: databricks
# --------------------------------------------------------------------------- #


class Source:
    """A warehouse connection that can describe and stream relations."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.catalog = cfg.get("catalog")
        self.schema = cfg.get("schema")
        self._con = None

    def qualified(self, relation: str) -> str:
        parts = [p for p in (self.catalog, self.schema, relation) if p]
        return ".".join(f"`{p}`" for p in parts)

    def _connect(self):
        kind = self.cfg.get("type", "databricks")
        if kind != "databricks":
            raise NotImplementedError(
                f"source type '{kind}' is not implemented. Databricks is the only "
                "one wired up; the shape to copy is `_connect` plus `stream`."
            )
        try:
            from databricks import sql as dbsql
        except ImportError:
            sys.exit(
                "a databricks source needs the connector: "
                "pip install 'metric-breakdown[databricks]'"
            )
        http_path = _env(self.cfg.get("http_path"))
        if not http_path:
            sys.exit("source.http_path is required for a databricks source")

        profile = self.cfg.get("profile")
        if profile:
            from databricks.sdk.core import Config as DbxConfig

            dbx = DbxConfig(profile=profile)
            host = _env(self.cfg.get("host")) or dbx.host
            if not host:
                sys.exit(f"could not resolve a host for databricks profile '{profile}'")
            return dbsql.connect(
                server_hostname=host.replace("https://", "").rstrip("/"),
                http_path=http_path,
                credentials_provider=lambda: dbx.authenticate,
            )
        token = _env(self.cfg.get("token"))
        host = _env(self.cfg.get("host"))
        if not (token and host):
            sys.exit(
                "a databricks source needs either `profile` (from `databricks auth "
                "login --profile <name>`) or both `host` and `token`."
            )
        return dbsql.connect(
            server_hostname=host.replace("https://", "").rstrip("/"),
            http_path=http_path,
            access_token=token,
        )

    def cursor(self):
        if self._con is None:
            self._con = self._connect()
        return self._con.cursor()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # -- introspection ----------------------------------------------------- #

    def schema_of(self, relation: str) -> pa.Schema:
        cur = self.cursor()
        try:
            cur.execute(f"SELECT * FROM {self.qualified(relation)} WHERE 1 = 0")
            return cur.fetchall_arrow().schema
        finally:
            cur.close()

    def stats(self, table: Table) -> dict[str, Any]:
        rel = self.qualified(table.relation)
        where = f" WHERE {table.where}" if table.where else ""
        selects = ["COUNT(*) AS n"]
        if table.time_column:
            selects += [
                f"MIN(`{table.time_column}`) AS t_min",
                f"MAX(`{table.time_column}`) AS t_max",
            ]
        cur = self.cursor()
        try:
            cur.execute(f"SELECT {', '.join(selects)} FROM {rel}{where}")
            row = cur.fetchall_arrow().to_pylist()[0]
        finally:
            cur.close()

        out = {
            "rows": int(row["n"]),
            "time_min": _iso(row.get("t_min")),
            "time_max": _iso(row.get("t_max")),
            "bytes": None,
        }
        # Delta only, and a view or a non-Delta relation is a normal miss, not
        # an error worth stopping a sizing pass over.
        cur = self.cursor()
        try:
            cur.execute(f"DESCRIBE DETAIL {rel}")
            detail = cur.fetchall_arrow().to_pylist()
            if detail and detail[0].get("sizeInBytes") is not None:
                out["bytes"] = int(detail[0]["sizeInBytes"])
        except Exception as exc:
            log.debug("DESCRIBE DETAIL %s: %s", rel, exc)
        finally:
            cur.close()
        return out

    def profile_columns(self, table: Table, schema: pa.Schema) -> dict[str, dict[str, Any]]:
        """Approximate distinct count and max string length per column.

        The point is finding free-text columns nobody classified: high
        cardinality plus long strings is a survey response or a URL, and those
        are the ones that leak.
        """
        pieces = []
        for f in schema:
            n = f.name
            pieces.append(f"approx_count_distinct(`{n}`) AS `d__{n}`")
            if pa.types.is_string(f.type) or pa.types.is_large_string(f.type):
                pieces.append(f"MAX(LENGTH(`{n}`)) AS `l__{n}`")
        where = f" WHERE {table.where}" if table.where else ""
        cur = self.cursor()
        try:
            cur.execute(
                f"SELECT {', '.join(pieces)} FROM {self.qualified(table.relation)}{where}"
            )
            row = cur.fetchall_arrow().to_pylist()[0]
        finally:
            cur.close()
        out: dict[str, dict[str, Any]] = {}
        for f in schema:
            out[f.name] = {
                "distinct": row.get(f"d__{f.name}"),
                "max_len": row.get(f"l__{f.name}"),
            }
        return out

    # -- streaming ---------------------------------------------------------- #

    def stream(self, table: Table, limit: int | None = None) -> Iterator[pa.Table]:
        rel = self.qualified(table.relation)
        where = f" WHERE {table.where}" if table.where else ""
        cap = f" LIMIT {int(limit)}" if limit else ""
        cur = self.cursor()
        try:
            cur.execute(f"SELECT * FROM {rel}{where}{cap}")
            while True:
                chunk = cur.fetchmany_arrow(BATCH_ROWS)
                if chunk is None or chunk.num_rows == 0:
                    return
                yield chunk
        finally:
            cur.close()


def _env(value: Any) -> Any:
    """Resolve a `${VAR}` reference so no secret has to live in the config."""
    if isinstance(value, str):
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        if m:
            return os.environ.get(m.group(1), "")
    return value


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    step = 1024.0
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < step:
            return f"{x:,.1f} {unit}"
        x /= step
    return f"{x:,.1f} PB"


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #


def select_tables(cfg: Config, tiers: list[str] | None, only: list[str] | None) -> list[Table]:
    tables = cfg.tables
    if tiers:
        tables = [t for t in tables if t.tier in tiers]
    if only:
        wanted = set(only)
        missing = wanted - {t.relation for t in tables}
        if missing:
            sys.exit(f"--only names relations not in the config (or filtered out): {sorted(missing)}")
        tables = [t for t in tables if t.relation in wanted]
    if not tables:
        sys.exit("no tables selected")
    return tables


def cmd_plan(cfg: Config, args) -> int:
    src = Source(cfg.source)
    tables = select_tables(cfg, args.tier, args.only)
    total_rows = total_bytes = 0
    unknown_bytes = False
    flagged: list[str] = []

    try:
        for t in tables:
            print(f"\n=== {t.relation}  [{t.tier}]")
            if t.note:
                print(f"    {t.note}")
            schema = src.schema_of(t.relation)
            stats = src.stats(t)
            total_rows += stats["rows"]
            if stats["bytes"] is None:
                unknown_bytes = True
            else:
                total_bytes += stats["bytes"]
            span = (
                f"  {stats['time_min']} .. {stats['time_max']}"
                if stats["time_min"]
                else "  (no time_column declared)"
            )
            print(f"    {stats['rows']:,} rows   {_human(stats['bytes'])}{span}")

            prof = src.profile_columns(t, schema) if args.profile else {}
            print(f"    {'column':<40} {'type':<22} {'action':<26} {'notes'}")
            for f in schema:
                acts = dispositions(f.name, f.type, cfg.deident)
                notes = ""
                if prof:
                    p = prof.get(f.name, {})
                    d, ml = p.get("distinct"), p.get("max_len")
                    notes = f"~{d:,} distinct" if d is not None else ""
                    if ml is not None:
                        notes += f", max len {ml}"
                    # The heuristic that earns its keep: lots of distinct values
                    # and long strings is free text, whatever it is called.
                    if (
                        acts == ["keep"]
                        and d is not None
                        and ml is not None
                        and d > 1000
                        and ml > 60
                    ):
                        notes += "   << REVIEW: looks like free text"
                        flagged.append(f"{t.relation}.{f.name}")
                print(f"    {f.name:<40} {str(f.type):<22} {'+'.join(acts):<26} {notes}")

            for key in t.unique_on:
                if key not in schema.names:
                    print(f"    !! unique_on names '{key}', which is not a column")
                elif "drop" in dispositions(key, schema.field(key).type, cfg.deident):
                    print(f"    !! unique_on names '{key}', which would be dropped")
    finally:
        src.close()

    print("\n" + "=" * 72)
    print(f"{len(tables)} relations   {total_rows:,} rows   "
          f"{_human(total_bytes)}{' (+ some unsized)' if unknown_bytes else ''} on the warehouse side")
    print("Parquet on disk is typically a small fraction of that; budget for the")
    print("wire time rather than the disk.")
    if cfg.deident.date_offset_days:
        print(f"\n!! date_offset_days = {cfg.deident.date_offset_days}. Every documented")
        print("   analysis window in the trees and knowledge docs shifts by that much.")
    if cfg.deident.money_multiplier != 1.0:
        print(f"\n!! money_multiplier = {cfg.deident.money_multiplier}. Identity tolerances")
        print("   may need widening, and decimals go through float64.")
    if flagged:
        print(f"\n!! {len(flagged)} columns look like free text and are currently kept:")
        for f in flagged:
            print(f"     {f}")
        print("   Add them to deident.drop_columns, or to keep_columns to silence this.")
    print("\nNothing was extracted. Review the actions above, then run `extract`.")
    return 0


def cmd_extract(cfg: Config, args) -> int:
    salt = get_salt(cfg.deident)
    out = Path(args.out)
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    src = Source(cfg.source)
    tables = select_tables(cfg, args.tier, args.only)
    manifest = _read_manifest(out) or {
        "archive": cfg.name,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "salt_fingerprint": salt_fingerprint(salt),
        "source": {
            "type": cfg.source.get("type", "databricks"),
            "catalog": cfg.source.get("catalog"),
            "schema": cfg.source.get("schema"),
        },
        "deident": {
            "date_offset_days": cfg.deident.date_offset_days,
            "money_multiplier": cfg.deident.money_multiplier,
            "hash_domains_used": bool(cfg.deident.hash_domains),
        },
        "tables": {},
    }
    if manifest["salt_fingerprint"] != salt_fingerprint(salt):
        sys.exit(
            "the salt in this environment does not match the one this archive was "
            "started with. Ids extracted now would not join to the ones already "
            f"here (manifest {manifest['salt_fingerprint']}, current "
            f"{salt_fingerprint(salt)}). Restore the original salt, or extract "
            "into a fresh --out directory."
        )

    try:
        for t in tables:
            path = data_dir / f"{t.relation}.parquet"
            if path.exists() and not args.force:
                log.info("%s already extracted, skipping (use --force to redo)", t.relation)
                continue

            stats = src.stats(t)
            started = time.time()
            log.info("%s: %s rows expected", t.relation, f"{stats['rows']:,}")

            writer: pq.ParquetWriter | None = None
            written = 0
            kept: list[str] = []
            actions: dict[str, list[str]] = {}
            tmp = path.with_suffix(".parquet.partial")
            try:
                for batch in src.stream(t, limit=args.limit):
                    if not actions:
                        for f in batch.schema:
                            actions[f.name] = dispositions(f.name, f.type, cfg.deident)
                    clean = transform(batch, cfg.deident, salt)
                    if writer is None:
                        writer = pq.ParquetWriter(tmp, clean.schema, compression="zstd")
                        kept = list(clean.schema.names)
                    else:
                        clean = clean.cast(writer.schema)
                    writer.write_table(clean)
                    written += clean.num_rows
                    if written % (BATCH_ROWS * 5) == 0:
                        log.info("  %s: %s rows", t.relation, f"{written:,}")
            finally:
                if writer is not None:
                    writer.close()

            if writer is None:
                # An empty relation is a fact worth recording, not a crash: a
                # mart that is empty in the archive because it was empty in the
                # warehouse is exactly the kind of thing to know later.
                schema = src.schema_of(t.relation)
                clean = transform(schema.empty_table(), cfg.deident, salt)
                pq.ParquetWriter(tmp, clean.schema, compression="zstd").close()
                kept = list(clean.schema.names)
                for f in schema:
                    actions[f.name] = dispositions(f.name, f.type, cfg.deident)
            tmp.replace(path)

            if args.limit is None and written != stats["rows"]:
                log.error(
                    "%s: wrote %s rows but the warehouse counted %s. The relation "
                    "changed under the extract, or the read was truncated.",
                    t.relation, f"{written:,}", f"{stats['rows']:,}",
                )

            manifest["tables"][t.relation] = {
                "tier": t.tier,
                "file": f"data/{path.name}",
                "rows_source": stats["rows"],
                "rows_written": written,
                "truncated": args.limit is not None,
                "bytes_parquet": path.stat().st_size,
                "bytes_source": stats["bytes"],
                "time_column": t.time_column,
                "time_min": stats["time_min"],
                "time_max": stats["time_max"],
                "unique_on": t.unique_on,
                "where": t.where,
                "note": t.note,
                "columns_kept": kept,
                "columns_dropped": sorted(k for k, v in actions.items() if "drop" in v),
                "columns_hashed": sorted(k for k, v in actions.items() if "hash" in v),
                "columns_pseudonymized": sorted(
                    k for k, v in actions.items() if "pseudonymize" in v
                ),
                "sha256": _sha256(path),
                "extracted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "seconds": round(time.time() - started, 1),
            }
            _write_manifest(out, manifest)
            log.info(
                "%s: %s rows -> %s in %.0fs",
                t.relation, f"{written:,}", _human(path.stat().st_size), time.time() - started,
            )
    finally:
        src.close()

    _write_disclosure(out, manifest)
    print(f"\nWrote {len(manifest['tables'])} relations to {data_dir}")
    print(f"Manifest: {out / 'manifest.json'}")
    print(f"Disclosure: {out / 'DISCLOSURE.md'}  (this is the document to show the client)")
    print("\nNext: `load`, then `verify`.")
    return 0


def cmd_load(cfg: Config, args) -> int:
    import duckdb

    out = Path(args.out)
    manifest = _read_manifest(out)
    if not manifest:
        sys.exit(f"no manifest in {out}; run `extract` first")

    alias = cfg.target.get("database_alias", cfg.name)
    schema = cfg.target.get("schema", "main")
    db_path = out / cfg.target.get("duckdb_file", f"{alias}.duckdb")

    # dbt-duckdb derives the `database` part of a relation from the file's stem,
    # so the filename is load-bearing: get it wrong and every `node_relation` in
    # the re-parsed manifest points somewhere that does not exist.
    if db_path.stem != alias:
        log.warning(
            "duckdb file stem '%s' != database_alias '%s'; dbt will resolve "
            "relations under '%s'", db_path.stem, alias, db_path.stem,
        )

    con = duckdb.connect(str(db_path))
    try:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for relation, entry in manifest["tables"].items():
            parquet = (out / entry["file"]).resolve()
            if not parquet.exists():
                log.error("%s: %s is missing", relation, parquet)
                continue
            con.execute(
                f'CREATE OR REPLACE TABLE "{schema}"."{relation}" AS '
                f"SELECT * FROM read_parquet('{parquet.as_posix()}')"
            )
            n = con.execute(f'SELECT COUNT(*) FROM "{schema}"."{relation}"').fetchone()[0]
            log.info("loaded %s (%s rows)", relation, f"{n:,}")
    finally:
        con.close()

    print(f"\nBuilt {db_path}")
    print("\nAdd this output to the dbt project's profiles.yml, then re-parse:\n")
    print(f"  {cfg.raw.get('dbt_profile_name', '<profile>')}:")
    print("    outputs:")
    print("      archive:")
    print("        type: duckdb")
    print(f"        path: {db_path.resolve()}")
    print(f"        schema: {schema}")
    print("\n  dbt parse --target archive")
    print("\nThat rewrites target/semantic_manifest.json so every node_relation")
    print("points at this file. The tree needs no edits.")
    return 0


def cmd_verify(cfg: Config, args) -> int:
    import duckdb

    out = Path(args.out)
    manifest = _read_manifest(out)
    if not manifest:
        sys.exit(f"no manifest in {out}; run `extract` first")

    salt = os.environ.get(cfg.deident.salt_env, "")
    alias = cfg.target.get("database_alias", cfg.name)
    schema = cfg.target.get("schema", "main")
    db_path = out / cfg.target.get("duckdb_file", f"{alias}.duckdb")
    if not db_path.exists():
        sys.exit(f"{db_path} does not exist; run `load` first")

    failures: list[str] = []
    checks = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    print("\n-- salt")
    if salt:
        check(
            salt_fingerprint(salt.encode()) == manifest["salt_fingerprint"],
            "environment salt matches the archive",
            f"({manifest['salt_fingerprint']})",
        )
    else:
        print(f"  skip  ${cfg.deident.salt_env} not set, cannot check salt consistency")

    print("\n-- completeness")
    for t in cfg.tables:
        entry = manifest["tables"].get(t.relation)
        check(entry is not None, f"{t.relation} present in the archive")
        if entry and not entry.get("truncated"):
            check(
                entry["rows_written"] == entry["rows_source"],
                f"{t.relation} row count matches the warehouse",
                f"({entry['rows_written']:,})",
            )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        print("\n-- integrity")
        for relation, entry in manifest["tables"].items():
            n = con.execute(f'SELECT COUNT(*) FROM "{schema}"."{relation}"').fetchone()[0]
            check(
                n == entry["rows_written"],
                f"{relation} loaded row count matches the parquet",
                f"({n:,})",
            )
            keys = [k for k in entry.get("unique_on") or [] if k in entry["columns_kept"]]
            if keys:
                cols = ", ".join(f'"{k}"' for k in keys)
                dupes = con.execute(
                    f'SELECT COUNT(*) FROM (SELECT {cols} FROM "{schema}"."{relation}" '
                    f"GROUP BY {cols} HAVING COUNT(*) > 1)"
                ).fetchone()[0]
                check(
                    dupes == 0,
                    f"{relation} unique on ({', '.join(keys)})",
                    "" if dupes == 0 else f"{dupes:,} duplicated keys",
                )

        print("\n-- arithmetic identities")
        if not cfg.identities:
            print("  none declared. This is where an archive quietly rots: without an")
            print("  identity check, a broken extract looks exactly like a working one.")
        for ident in cfg.identities:
            if ident.table not in manifest["tables"]:
                check(False, f"{ident.table}: identity table missing")
                continue
            worst = con.execute(
                f"SELECT COALESCE(MAX(ABS({ident.expression})), 0) "
                f'FROM "{schema}"."{ident.table}"'
            ).fetchone()[0]
            check(
                float(worst) <= ident.tolerance,
                f"{ident.table}: {ident.note or ident.expression}",
                f"worst |residual| = {float(worst):.6g} (tolerance {ident.tolerance})",
            )
    finally:
        con.close()

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("\nFix these while warehouse access still exists. That is the whole")
        print("reason to run verify now rather than in six months.")
        return 1
    print(f"All {checks} checks passed.")
    print("\nRemaining step, and the one that actually proves the archive: re-run the")
    print("RCAs against this copy and diff them against snapshots taken from the live")
    print("warehouse. Identical numbers mean the extraction is faithful; a difference")
    print("is the extraction, not the engine.")
    return 0


# --------------------------------------------------------------------------- #
# manifest and disclosure
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest(out: Path) -> dict[str, Any] | None:
    p = out / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else None


def _write_manifest(out: Path, manifest: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))


def _write_disclosure(out: Path, manifest: dict[str, Any]) -> None:
    """The human-readable half of the manifest.

    Written for two readers: the client deciding whether to grant permission,
    and whoever opens this archive in a year having forgotten what is in it.
    """
    d = manifest["deident"]
    lines = [
        f"# Data archive: {manifest['archive']}",
        "",
        f"Created {manifest['created_at']}.",
        f"Source: {manifest['source'].get('catalog')}.{manifest['source'].get('schema')} "
        f"({manifest['source'].get('type')}).",
        "",
        "## What was done to the data",
        "",
        "- Identifier columns are replaced by a keyed HMAC-SHA256 digest. The key is",
        "  not stored in this archive and the mapping back to the original values is",
        "  not recoverable from it.",
        "- The same original value always produces the same digest, so joins between",
        "  tables still work and the number of distinct values is unchanged.",
        "- Columns listed as dropped below were not extracted at all.",
        f"- Dates shifted by: {d['date_offset_days']} days.",
        f"- Monetary values scaled by: {d['money_multiplier']}.",
        "",
        "Everything else is unmodified, including null values, duplicates and gaps.",
        "That is deliberate: the irregularities are what the archive is for.",
        "",
        "## Tables",
        "",
    ]
    for relation in sorted(manifest["tables"]):
        e = manifest["tables"][relation]
        lines += [
            f"### {relation}",
            "",
            f"- Rows: {e['rows_written']:,}"
            + (f" (of {e['rows_source']:,}; truncated extract)" if e.get("truncated") else ""),
            f"- Time span: {e.get('time_min')} to {e.get('time_max')}"
            if e.get("time_min")
            else "- Time span: n/a",
            f"- Columns kept ({len(e['columns_kept'])}): {', '.join(e['columns_kept'])}",
        ]
        if e["columns_hashed"]:
            lines.append(f"- Columns hashed: {', '.join(e['columns_hashed'])}")
        if e["columns_pseudonymized"]:
            lines.append(f"- Columns pseudonymized: {', '.join(e['columns_pseudonymized'])}")
        if e["columns_dropped"]:
            lines.append(f"- Columns dropped: {', '.join(e['columns_dropped'])}")
        else:
            lines.append("- Columns dropped: none")
        if e.get("note"):
            lines.append(f"- Note: {e['note']}")
        lines.append("")
    (out / "DISCLOSURE.md").write_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="archive.py",
        description="Extract a de-identified static copy of a client's marts.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="verb", required=True)

    def common(sp, need_out=True):
        sp.add_argument("-c", "--config", required=True)
        sp.add_argument("--tier", action="append", help="repeatable; default all tiers")
        sp.add_argument("--only", action="append", help="repeatable; specific relations")
        if need_out:
            sp.add_argument("-o", "--out", default="./archive")
        return sp

    sp = common(sub.add_parser("plan", help="size it and show every column's fate"), need_out=False)
    sp.add_argument(
        "--profile",
        action="store_true",
        help="also compute approximate distinct counts and string lengths, to flag free text",
    )

    sp = common(sub.add_parser("extract", help="write parquet + manifest"))
    sp.add_argument("--limit", type=int, help="rows per table, for a smoke test")
    sp.add_argument("--force", action="store_true", help="re-extract tables already present")

    common(sub.add_parser("load", help="build the duckdb file"))
    common(sub.add_parser("verify", help="prove the copy is faithful"))

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    cfg = load_config(args.config)
    return {
        "plan": cmd_plan,
        "extract": cmd_extract,
        "load": cmd_load,
        "verify": cmd_verify,
    }[args.verb](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

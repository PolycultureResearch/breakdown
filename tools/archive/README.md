# Warehouse archive

Extract a de-identified, static copy of a client's marts, so the dataset
outlives the engagement.

Design and scope rationale: [`knowledge/test_data_strategy.md`](../../knowledge/test_data_strategy.md).
The short version is that snapshots freeze today's answers and the relations
keep answering new questions, and only one of those is worth the trouble of
asking a client for permission.

## Quick start

```bash
export BREAKDOWN_ARCHIVE_SALT="$(openssl rand -hex 32)"   # save this somewhere durable
cd /path/to/breakdown

uv run python tools/archive/archive.py plan    -c tools/archive/configs/<client>.yml --profile
uv run python tools/archive/archive.py extract -c tools/archive/configs/<client>.yml -o ~/archives/<client>
uv run python tools/archive/archive.py load    -c tools/archive/configs/<client>.yml -o ~/archives/<client>
uv run python tools/archive/archive.py verify  -c tools/archive/configs/<client>.yml -o ~/archives/<client>
```

Needs the `databricks` extra for a Databricks source, plus `duckdb`. Both are
already in the dev group.

## The four verbs

| Verb | What it does | Moves data? |
|---|---|---|
| `plan` | Row counts, byte sizes, time spans, and **what will happen to every column**. `--profile` adds approximate distinct counts and string lengths to flag free text nobody classified. | No |
| `extract` | Streams each relation, de-identifies it batch by batch, writes zstd parquet plus `manifest.json` and `DISCLOSURE.md`. Raw rows never touch disk. Resumable: already-extracted tables are skipped unless `--force`. | Yes |
| `load` | Builds the DuckDB file and prints the `profiles.yml` block to add. | Local only |
| `verify` | Salt consistency, row counts, key uniqueness, and the arithmetic identities. Exits non-zero on failure. | Local only |

`--tier` and `--only` narrow any of them. `extract --limit 1000` is a smoke test.

Read `plan` before extracting. It is the only place an unhashed id column or a
free-text field shows up before it is in a parquet file.

## The salt

De-identification is HMAC-SHA256 keyed on `$BREAKDOWN_ARCHIVE_SALT`. Save it.

- Hashing is keyed on the **value alone**, never the column name, so `user_id`
  in `dim_users` and `user_id` in `fct_signups` produce the same digest and
  every join survives, including joins between columns named differently in
  different marts. `hash_domains` can separate two id spaces, and using it is a
  decision to break those joins.
- The manifest stores a **fingerprint** of the salt, not the salt. Re-extracting
  with a different one produces ids that do not join to the first extract, and
  `extract` refuses rather than letting you find out later.
- Because hashing is 1:1, `unique_on` still means something after
  de-identification. The uniqueness check doubles as a hash-collision check.

## What is deliberately not done

- **No cleaning.** Nulls, duplicates, gaps and the weeks the pipeline broke all
  come through. The irregularities are the reason the archive exists.
- **No aggregation.** Row grain, every column, full history. Pre-aggregating to
  week would kill grain testing and `entity_flows` outright.
- **No per-row noise.** Money scaling, if used at all, is one global constant
  and date shifting is one global offset, because every transformation has to
  preserve the identities in the tree. Per-row jitter would break
  `net_new = new + expansion + contraction + churn + reactivation` and turn
  every later test result into a false positive.

Both `date_offset_days` and `money_multiplier` default to off, and should stay
that way unless a client requires otherwise. After id hashing there is nobody
left to re-identify, and both options cost real fidelity: a date offset moves
every documented analysis window in the trees and knowledge docs, and a money
multiplier pushes decimals through float64 and eats the identity tolerances.

## `DISCLOSURE.md` is the point of the manifest

`extract` renders a human-readable record of exactly which columns were taken,
hashed, pseudonymized and dropped, per table, with row counts and time spans.

That is the document to put in front of the client when asking for written
permission, and the one to re-read in a year when you have forgotten what is in
the archive. Ask for permission covering **derived artifacts** too: snapshots
and committed test fixtures are derived works.

## After `verify` passes

Two things remain, and both need warehouse access, so do them before it ends.

1. **Re-parse the dbt project against the archive.** Add the `duckdb` output
   `load` prints, then `dbt parse --target archive`. That rewrites
   `target/semantic_manifest.json` so every `node_relation` points at the local
   file. The tree needs no edits, because the `dbt` provider reads the resolved
   manifest and generates its own SQL.

   The DuckDB **filename is load-bearing**: dbt-duckdb takes the `database` part
   of a relation from the file's stem, so it has to match `database_alias`.

2. **Diff the RCAs.** Snapshot the tree against the live warehouse first
   (`.breakdown/snapshots/`), then re-run the same analyses against the archive
   and compare. Identical numbers mean the extraction is faithful. A difference
   is the extraction, not the engine, and you can still go back and fix it.

The thing neither step captures is what actually happened in the business at
each incident the tree can see. Write that down separately while there are still
people to ask. It is the only genuinely unrecoverable part.

## Where this should live

`archive.py` is client-agnostic and belongs here. A config names a client's
schema, and an extract is client data.

Breakdown is headed for PyPI and open source. Before that: move
every client config in `configs/` and every archive directory into the private archive
repo, and keep the public test suite running on synthetic data only.

## Adding a source

Databricks is the only backend wired up. The shape to copy is `Source._connect`
plus `Source.stream`, `Source.stats` and `Source.schema_of`. Everything above
those four methods is warehouse-agnostic.

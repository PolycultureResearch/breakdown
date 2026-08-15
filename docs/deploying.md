# Deploying

This page covers running breakdown as a shared service: serving more than one
metric tree from a process, authentication, a Dockerized deployment, checking
provider connectivity, the snapshot cache, and every environment variable
`breakdown serve` accepts. For installing and running it on your own laptop,
see the [README](../README.md); for authoring a tree, see the
[YAML reference](yaml-reference.md).

---

## Serving several trees

One breakdown process can serve several metric trees. They are **peers**, not a
hierarchy: a company typically keeps one wide tree with revenue at the top (the
net-MRR tree), and alongside it trees that go deep on one part of the business —

- a **marketing** tree whose leaves are channels and campaigns,
- a **product** tree about feature adoption and what it does to retention,
- a tree standing behind a **specific goal**, whether that is a quarter, a
  year, or five years.

Any tree may be long-lived or short-lived, and any may declare a goal or not.
breakdown takes no position on either: a focused tree can be as durable and as
useful as the revenue tree, and most trees have no target attached at all.

Point `--tree` at a directory and every `*.yml` in it (non-recursively) is one
tree, its **id the filename stem**:

```
acme-dbt-project/
  breakdown/
    net_mrr.yml           -> id "net_mrr"
    marketing.yml         -> id "marketing"
    activation.yml        -> id "activation"
```

```bash
breakdown serve --tree ./breakdown --default-tree net_mrr
```

`/ui` then opens an **index** of the trees — title, owner, and, where one is
declared, period and current-vs-target — and a **Tree** switcher appears in the
header. A single `--tree <file>` behaves exactly as it always has: no index, no
switcher, one tree.

**Trees load lazily.** Boot parses every tree's YAML — cheap, no provider
involved — and fetches none, so the index is instant on a cold process and
nobody pays for the trees they didn't open. A tree's data is fetched on the
first request that needs it (or from the index's **Load** button); until then
its card says *not loaded* rather than showing a zero. A single-file `--tree`
still loads at startup, where lazy buys nothing; `--eager` asks for the same
from a directory, loading the default tree up front.

**Failures are per tree.** One malformed YAML shows as a broken card carrying
its own parse error, and the other trees serve normally.

Every data route is available at `/trees/{tree_id}/…` as well as bare, and the
bare paths mean the default tree — so existing links, scripts and MCP clients
keep working unchanged:

```bash
curl -X POST "localhost:9090/trees/marketing/rca/paid_signups?analysis_start=2026-08-01&analysis_end=2026-08-07"
curl localhost:9090/trees            # the index: every tree and its state
```

| Flag | Default |
|------|---------|
| `--tree` | A tree file **or a directory of them** |
| `--default-tree <id>` | The only tree if there is one, else the alphabetically first |
| `--eager` | Off — a directory of trees loads on demand |

---

## Authentication

**By default there is none, and that is safe only because the default bind is
loopback.** `breakdown serve` listens on `127.0.0.1`, so nothing is reachable
from off the machine. The moment you pass `--host 0.0.0.0` (which every
container deployment does), the entire API — your tree, your series, your
generated SQL — is open to anyone who can reach the port. Decide this before you
expose the port, not after.

Access control is one shared bearer token, configured with two environment
variables:

| Variable | Effect |
|---|---|
| `BREAKDOWN_API_TOKEN` | The secret itself. Set alone, it gates **`/mcp` only** — every JSON data route stays open. This is the default behavior and is unchanged. |
| `BREAKDOWN_REQUIRE_AUTH` | Extends the same bearer check to **every route** except a small allow-list. Requires `BREAKDOWN_API_TOKEN`. |

Callers present it as a standard bearer header:

```bash
export BREAKDOWN_API_TOKEN=$(openssl rand -hex 32)
export BREAKDOWN_REQUIRE_AUTH=1
breakdown serve --host 0.0.0.0

curl -H "Authorization: Bearer $BREAKDOWN_API_TOKEN" http://your-host:9090/meta
```

**`BREAKDOWN_REQUIRE_AUTH` is on unless it is explicitly off.** Anything other
than `""`, `0`, `false`, `no` or `off` (case-insensitive, whitespace stripped)
counts as on — so `BREAKDOWN_REQUIRE_AUTH=ture` closes the door rather than
opening it. A typo in a security switch must fail toward the safe side.

**What stays open with the flag on**, and nothing else:

| Open | Why |
|---|---|
| `/health` | Liveness and readiness. `compose.yaml`'s healthcheck calls it with no credentials, and orchestrators can't present one — gating it makes a correctly configured deployment look dead. |
| `/ui` and everything under it | A JS bundle, not data. |
| `/` | A one-line "the API is running" message that carries nothing. |

Everything else is gated, **including `/openapi.json` and `/docs`** — the
allow-list is an allow-list precisely so a route added tomorrow is closed by
default rather than open until someone remembers it. Matching is on **path
segment boundaries**, so `/healthz` and `/uiconfig` are *not* treated as open;
only `/health` itself, and `/ui` plus genuine children like `/ui/app.js`.

> **With the flag on, the browser UI's own fetches are gated too.** `/ui` loads,
> but every request it makes (`/meta`, `/dag`, `/series`, RCA, …) needs the
> header, and a browser will not add one by itself. This mode therefore assumes
> **a reverse proxy that injects the header** — Cloudflare Access, an
> authenticating ingress, an oauth2-proxy sidecar — or an operator who accepts
> that the UI is unusable without one and is gating a machine-facing API. There
> is deliberately no login page, no cookie, and no token-in-the-URL: that is
> hosted mode ([roadmap 3.5](../knowledge/roadmap.md)),
> and a half-built version of it would be worse than none.

**Setting `BREAKDOWN_REQUIRE_AUTH` without `BREAKDOWN_API_TOKEN` is refused.**
Every request would otherwise be checked against an empty secret and pass — the
one configuration that fails *open*. Instead, non-open routes return **503**,
`GET /health` reports `{"status": "degraded", …}` naming the misconfiguration,
and the process logs it at startup. You get loud, diagnosable 503s rather than a
deployment that looks protected and isn't.

There is one asymmetry to know about before you debug it. `/health` is on the
open allow-list and always answers **200**, which is what lets the container
healthcheck run without credentials — so in this misconfigured state the
container reports **healthy while being unusable**: every data route 503s and
the health probe passes. The `status` field is where the truth is; read the
body, not the code. (This is the same design that keeps a provider outage from
looking like a dead container, and it costs exactly this one confusing case.)

**Query redaction, independent of the flag.** Whenever `BREAKDOWN_API_TOKEN` is
set and a caller does not present it, `GET /dag` returns each node's `sql` and
`bind` as `null` rather than their real contents. `/dag` has to stay reachable
for the unauthenticated UI to draw anything — but those two blocks are the only
parts of a definition that are infrastructure rather than modeling: `sql` is the
metric's whole statement and `bind` carries the fully-qualified table name plus
its WHERE-clause business logic. On a deployment that bothered to configure a
token, "the graph is public" should not also mean "our warehouse layout and
filter logic are public". They are redacted to `null` rather than dropped, so a
client reading `def.sql` sees an absent query instead of a missing key. With no
token set (the laptop default) nothing is redacted. The UI's *show query* panel
reads [`GET /metrics/{name}/query`](api-reference.md#get-metricsnamequery),
which is gated normally, so it loses nothing.

**What this is not.** One shared secret, no per-user identity, no audit trail,
and no revocation short of rotating the value and restarting. It is a down
payment on hosted mode, not a substitute for it. If you need per-user access,
put breakdown behind something that provides it.

---

## A shared instance with Docker

```bash
cp path/to/my_tree.yml tree.yml
export DATABRICKS_TOKEN=...        # whatever ${VARS} your tree references
export BREAKDOWN_API_TOKEN=$(openssl rand -hex 32)
export BREAKDOWN_REQUIRE_AUTH=1    # gate every route, not just /mcp
export BREAKDOWN_PUBLIC_URL=https://breakdown.acme.com
docker compose up --build
```

The [`compose.yaml`](../compose.yaml) mounts `./tree.yml` read-only at `/config/tree.yml`, passes provider credentials through as environment variables, and healthchecks `GET /health`. The image is large (~2.5–3 GB — PyMC and its compiler toolchain); the first build takes a while.

> **Set the access-control variables in your environment — the shipped
> `compose.yaml` passes them through.** `BREAKDOWN_API_TOKEN`,
> `BREAKDOWN_REQUIRE_AUTH` and `BREAKDOWN_PUBLIC_URL` are all listed bare in its
> `environment:` block, so there is nothing to edit; what the file cannot do is
> decide them for you. A container publishes its port, so leaving the two token
> variables unset means the whole API is open to whatever can reach the host —
> that is a choice, and it should be a deliberate one. See
> [Authentication](#authentication) for exactly what each level gates: the token
> alone gates `/mcp` and redacts `sql`/`bind` from `/dag`; `BREAKDOWN_REQUIRE_AUTH`
> extends the bearer check to every route but `/`, `/health` and `/ui`.
>
> `BREAKDOWN_PUBLIC_URL` is what makes the MCP server's `report_url` deep links
> resolve — without it they point at `http://127.0.0.1:9090`, which is correct
> only on the container's own loopback and therefore useless to whoever the link
> was handed to.
>
> **A variable you never export is *absent* in the container, not empty**, which
> is what keeps an unset `BREAKDOWN_REQUIRE_AUTH` from tripping the
> no-token-configured refusal below.

Three things differ from a laptop run:

- **Credentials must be headless.** The Databricks CLI OAuth `profile:` flow opens a browser, which a container can't. Use `token: ${DATABRICKS_TOKEN}` in the tree's provider block instead (see [`provider`](yaml-reference.md#provider) for `${VAR}` interpolation). If you must reuse a profile, mount both `~/.databrickscfg` and `~/.databricks/token-cache.json` read-only into the container.
- **Startup failures degrade, not crash.** If the provider can't be reached (bad token, warehouse down), the server still starts: `GET /health` returns `{"status": "degraded", "error": …}`, data endpoints return 503, and the UI shows the error with a pointer to `breakdown doctor`. Fix the config and restart — no crash-loop to debug through.
- **The port is published, so the API is exposed.** The compose file passes the access-control variables through, but it cannot set them — if you export nothing, nothing is gated. See [Authentication](#authentication) above.

---

## Checking connectivity: `breakdown doctor`

Before the first `serve` against real data — or whenever startup reports `degraded` — run:

```bash
uv run breakdown doctor --tree path/to/my_tree.yml
```

It walks the provider's auth chain step by step (tree parses → env vars set → CLI/profile/token valid → connection opens → every metric's query actually runs) and prints `[PASS]`/`[FAIL]` per step with copy-paste remediation for each failure. Exit code is non-zero if anything failed — a `[WARN]` is a result worth reading, not a failure, and does not change the exit code. Probes run over the last 7 days by default; override with `--start-date`/`--end-date`.

Two mode-specific checks ride along: a cold-start tree (`provider: none`) gets its declarations validated instead of a connection probe, and when you pass an explicit `--start-date`/`--end-date` window the doctor adds a **fit readiness** report — each metric's whole-period count against the 10-period fit minimum, the graduation check for a tree [moving from cold start to fitted mode](yaml-reference.md#cold-start-mode-what-if-with-no-data) — plus a **history headroom** report: whether the provider has history before your `--start-date`. Breakdown trains on everything you load, so an earlier start date strengthens every fit (and the default RCA reference windows) at no cost beyond fetch time.

For the **`dbt` provider**, `doctor` walks manifest → profile → connection →
bindings → dimensions → grain claims → filters, in the order a failure cascades.
The last three are the ones that pay for themselves: a declared dimension that
does not exist becomes a startup failure rather than a 500 on the first *slice
by* click; the **grain claim** (`count(*)` vs `count(distinct grain_key)`)
catches a relation that is not one row per grain — silent fan-out that
multiplies every aggregate over it, which neither MetricFlow nor Cube checks;
and **`filters narrow`** counts kept-vs-total rows for every metric whose dbt
`filter:` was imported.

```
[PASS] grain claims hold  — 12 relation(s) one row per grain, 3 under a filter
[WARN] filters narrow     — 1 filter(s) excluded nothing: everything (6 of 6 rows)
```

Both of the degenerate answers are worth a check of their own. A filter that
keeps **no** rows fails: the node would serve an empty or all-zero series, and
that is the signature of a predicate the warehouse accepts but reads
differently — `= TRUE` against a `VARCHAR`, a boolean stored as `'Y'`. A filter
that excludes **nothing** warns: either it is genuinely vacuous over the probe
window (widen it and re-run) or it is evaluating constant-true, which is the
silently-dropped filter this check exists to prevent. As with the grain claim,
this is a question about your **data**, not your metadata, and no semantic layer
answers it.

---

## Snapshots: fetch once, refit forever

For non-mock providers, every fetched series is cached as a parquet snapshot keyed on `(metric, grain, kind, window)` — by default in `.breakdown/snapshots/` next to the tree. Later startups with the same window read from disk instead of the warehouse, which makes restarts fast, keeps re-runs reproducible (commit the snapshots next to the tree and an RCA re-runs from a fresh clone), and lets the server boot even when the warehouse is unreachable, as long as every metric has a snapshot.

```bash
uv run breakdown serve --tree my_tree.yml --refresh        # refetch everything, overwrite snapshots
uv run breakdown serve --tree my_tree.yml --no-snapshots   # always hit the provider
uv run breakdown serve --tree my_tree.yml --snapshot-dir /somewhere/writable
```

A snapshot freezes what the provider returned at fetch time — if the warehouse backfills late-arriving data, run `--refresh` once to pick it up. `BREAKDOWN_REFRESH=1` is the environment-variable form, for a scheduled refresh that has no command line to edit. In Docker, `compose.yaml` mounts `./snapshots` and sets `BREAKDOWN_SNAPSHOT_DIR` (the default tree-adjacent location is unwritable there because `/config` is read-only); an unwritable snapshot directory is never fatal — the server logs one warning and runs uncached.

**If your metric restates, the snapshot key cannot tell.** The key is
`(metric, grain, kind, window)` with no content hash, so a series whose *past*
values change — payment plans settling backwards, late-arriving conversions,
any bitemporal source — is frozen at whatever it said the first time. Two rules
follow. Refresh unconditionally on a schedule (`BREAKDOWN_REFRESH=1`) rather
than relying on the cache to notice. And prefer a basis that never restates for
the series you actually fit: an order-*created* date including every status
moves forward only, where a settled or completed basis rewrites history behind
you. Keep the restating version in the tree if you report on it, but do not make
it an RCA target mid-period — the model would be training on values that are
still changing.

---

## Environment variables

Every `breakdown serve` flag has an environment-variable form, which is what a
container or a scheduled job uses. The flag wins where both are set.

| Variable | CLI flag | Default | What it does |
|---|---|---|---|
| `BREAKDOWN_TREE` | `--tree` | bundled `jaffle_shop_tree.yml` | Tree file **or a directory** of them ([Serving several trees](#serving-several-trees)) |
| `BREAKDOWN_DEFAULT_TREE` | `--default-tree` | the only tree, else alphabetically first | Which tree the unprefixed routes mean |
| `BREAKDOWN_EAGER` | `--eager` | unset (a directory loads lazily) | Load the default tree at boot instead of on first use |
| `BREAKDOWN_START_DATE` | `--start-date` | `2024-01-01` | Start of the loaded data window |
| `BREAKDOWN_END_DATE` | `--end-date` | `2024-04-09` | End of the loaded data window |
| `BREAKDOWN_HOST` | `--host` | `127.0.0.1` | Bind address. Anything non-loopback exposes the API — see [Authentication](#authentication) |
| `BREAKDOWN_PORT` | `--port` | `9090` | Listen port |
| `BREAKDOWN_SNAPSHOT_DIR` | `--snapshot-dir` / `--no-snapshots` | `.breakdown/snapshots` beside the tree | Parquet snapshot cache; `off` disables it |
| `BREAKDOWN_REFRESH` | `--refresh` | unset | Skip snapshot reads for one pass and refetch, still writing |
| `BREAKDOWN_API_TOKEN` | — | unset | Bearer token. Alone it gates `/mcp` and redacts `sql`/`bind` from `/dag` |
| `BREAKDOWN_REQUIRE_AUTH` | — | unset | Gate every route but `/`, `/health`, `/ui`. Needs `BREAKDOWN_API_TOKEN` |
| `BREAKDOWN_PUBLIC_URL` | — | `http://127.0.0.1:$BREAKDOWN_PORT` | Base URL for MCP `report_url` deep links, when the server is reached at anything else |
| `BREAKDOWN_MAX_TRACE_BYTES` | — | `536870912` (512 MiB) | Byte budget for the fitted-model cache; `0` disables the byte bound |

**`BREAKDOWN_MAX_TRACE_BYTES` is the one worth understanding before you size a
box.** Fitted models are cached so a second RCA is fast, and the cache is bounded
by **total bytes** rather than by a number of entries, with a 256-entry backstop
behind it. That is not a stylistic choice: one entry's size scales with the
loaded data window — a single ADVI fit over an 830-day window measures ~13 MB of
posterior — so no fixed entry count can be safe for every window, and tuning the
count down only moves the cliff to a wider one. With a byte budget, a wider
window simply caches fewer fits instead of OOM-killing the process. The 512 MiB
default assumes the smallest box this is expected to run on (a 2 GB VM, where the
interpreter plus PyMC and one tree's frames sit near 0.5–0.7 GB resident); raise
it on a larger host, lower it on a smaller one. The on-demand slice and
entity-flow caches are bounded too, by entry count — a slice frame is two orders
of magnitude smaller than a trace.

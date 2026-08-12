# Multiple metric trees — design spec

Status: designed, not built. Roadmap item **2.16**. Companion to
[`ui_design_spec.md`](ui_design_spec.md) (UI) and
[`../docs/ai-context/python-backend.md`](../docs/ai-context/python-backend.md)
(the single-tree architecture this amends).

## 1. Product framing

breakdown today serves exactly one tree per process: `BREAKDOWN_TREE` is read
once in `lifespan`, and `app.state.parser` / `.data` / `.traces` are that tree's
state. That is the right shape for "here is our business, why did revenue
move" — one durable model of the company, edited over time.

It is the wrong shape for the use case that keeps surfacing: **a metric tree per
company goal, per quarter.** "Get 200 new paying Pro members this quarter" is a
tree — a small, focused, *disposable* one. It has an owner, a deadline, a
target, and a life expectancy of thirteen weeks. A company running that way has
five or eight of them live at once, plus the durable business tree, and next
quarter it has a different five.

That reframes what breakdown is for. The durable tree answers *why did the
business move*. A goal tree answers *are we going to make it, and if not,
which driver is behind it* — the same engine, a much sharper question, and a
natural weekly cadence (goal review) instead of an incident-driven one.

Two consequences shape everything below:

- **Trees are cheap and numerous.** Onboarding cost per tree matters more than
  it did; so does not paying for trees nobody is looking at.
- **A tree is a document with an owner**, not just a config file. It wants a
  title, a period, and a target — and a place that lists them all.

### 1.1 Personas

Unchanged from `ui_design_spec.md`, plus one:

- **The operator** builds and runs analyses inside one tree.
- **The reader** receives a deep link to a specific analysis.
- **The goal owner** (new) wants the *index*: which goals exist, who owns
  them, which are off track. They may never open a tree at all.

## 2. Decisions

Three questions were settled with the author (2026-08-12) before design. They
are recorded here with their alternatives, because each one has a plausible
opposite and re-litigating them silently would be worse than changing them
deliberately.

### 2.1 The YAML carries a full goal block, every field optional

**Decision.** Define the whole `tree:` block now — `title`, `description`,
`owner`, `period`, `goal` — and make **every field optional**, so a tree that
is not a goal declares only `title` (or nothing at all, and takes its identity
from the filename).

**Why not "title only".** The goal framing *is* the use case driving this work.
Shipping `title:` alone means a second, breaking YAML change the moment the
index wants to show a target — against trees that by then live in customers'
dbt repos. The cost of defining the block now is one Pydantic model.

**Why not "goal required".** A tree that isn't a goal is a first-class citizen:
the durable business tree, an exploratory "how does the funnel actually work"
tree, the bundled examples. Requiring `tree.goal` would force them to invent a
goal they don't have, or fail to load.

### 2.2 Navigation is an index page *and* a header dropdown

**Decision.** A landing view listing trees as cards (title, goal, owner,
period, status), plus a switcher in the header for fast movement once inside a
tree.

**Why both.** The index is where the quarterly-goals framing pays off — it is
the goal owner's whole surface, and the only screen that answers "how are we
doing" across trees. The dropdown is what the operator wants on their third
switch, when the index has become a toll booth. Neither substitutes for the
other.

### 2.3 The index shows goal progress lazily, and says when it doesn't know

**Decision.** Show current-vs-target for trees already loaded in this process;
for the rest show an explicit *not loaded* state (a dash and a "load" affordance),
never a blank that reads as zero and never a stale number presented as live.

**Why not eager.** Loading eight trees at boot is eight provider fetches before
the port is useful — precisely what lazy loading exists to avoid, and on a
warehouse-backed tree it is minutes, not seconds.

**Why not "list only".** The index without progress is a table of contents. The
question the goal owner came to ask is "are we on track", and refusing to answer
it for trees that are *already loaded and already know* would be artificial.

**Why not snapshots.** Reading `.breakdown/snapshots` for the index is
attractive (fast, offline) and is the natural **follow-on**, but it introduces
a number whose freshness differs from everything else on screen. If it lands, it
must be labelled with the snapshot date — an unlabelled stale number is the one
outcome worse than a dash. Out of scope for v1; noted in §9.

## 3. The `tree:` block

```yaml
tree:
  title: "Q3 Pro member growth"
  description: "200 net-new paying Pro members by Sep 30"
  owner: "growth@acme.com"
  period: "2026-Q3"
  goal:
    metric: pro_members_net_new   # must name a metric in this tree
    target: 200
    direction: up                 # up | down — which way is winning
    deadline: "2026-09-30"

provider:
  type: dbt

metrics:
  - name: pro_members_net_new
    ...
```

Contract:

- **Every field is optional, including the block itself.** A tree with no
  `tree:` block is exactly as valid as it is today and takes its title from its
  filename stem.
- **`goal.metric` must resolve to a metric in this tree.** A goal naming a
  metric that doesn't exist is a parse error, not a silently dead card — it is
  the one field here that can be wrong in a way the author can't see.
- **`goal.direction` defaults from the named metric's own `direction`** when
  that metric declares one, since the tree already has the concept
  (`goodDir`/`goodClass` in the UI). Declaring both and disagreeing is an error.
- **`period` is a free-form label**, not a parsed date range. It appears on the
  card and groups the index. `deadline` is the machine-readable date.
- **`title` is display-only. `id` is always the filename stem** — see §4.

### 3.1 Backward and forward compatibility

`MetricTreeConfig` uses Pydantic's default `extra='ignore'`, so a tree carrying
`tree:` **already loads without error on today's build** — it is ignored. That
is worth stating plainly, because it means authors can start annotating trees
before the feature ships, and a customer on an older breakdown does not break
when someone adds a goal block.

The reverse also holds: a tree with no `tree:` block loads on the new build.
There is no migration.

## 4. Discovery and identity

`--tree` accepts **a file or a directory**. A directory is globbed for `*.yml` /
`*.yaml`, non-recursively.

```
acme-dbt-project/
  breakdown/
    business.yml          -> id "business"
    q3_pro_growth.yml     -> id "q3_pro_growth"
    q3_activation.yml     -> id "q3_activation"
```

- **`id` is the filename stem.** Stable, greppable, obvious in logs, and it
  makes the deep link (`#tree=q3_pro_growth`) legible. A `tree.id` key was
  considered and rejected: two files could then claim one id, and the failure
  is confusing where a filename collision is impossible.
- **The default tree** is `--default-tree <id>`, else the single tree if there
  is one, else the alphabetically first. It backs the unprefixed route aliases
  (§6.3) and is what a bare `/ui` opens if the index is skipped.
- **A file argument keeps today's behavior exactly** — one tree, its id its
  stem, and it is the default.
- **Parse errors are per-tree.** One malformed YAML in the directory must not
  take down the other seven; that tree gets a `load_error` and shows as broken
  on the index. This mirrors the existing degraded-startup discipline
  (`app.state.startup_error` → `/health` → banner) rather than inventing a
  second one.

## 5. Backend: `TreeState`

Today's per-tree state is spread across `app.state` and read at ~40 call sites:

```
parser · fetcher · data · startup_error · traces · slice_cache
flow_cache · lock · earliest · earliest_task · progress
```

**All of it except `progress` is per-tree.** The refactor is mechanical and is
the bulk of the work:

```python
@dataclass
class TreeState:
    id: str
    path: str
    title: str                  # tree.title, else the id
    meta: TreeMeta | None       # the parsed `tree:` block
    parser: Parser | None
    fetcher: Any | None
    data: Any | None
    load_error: str | None
    loaded: bool
    traces: dict
    slice_cache: dict
    flow_cache: dict
    lock: asyncio.Lock          # per-tree: two trees may run concurrently
    earliest: dict
    earliest_task: asyncio.Task | None
```

with `app.state.trees: dict[str, TreeState]` and `app.state.default_tree: str`.

Notes that are not obvious from the shape:

- **The lock becomes per-tree.** Today one global lock serializes every
  analysis, which is right when there is one tree and one trace cache. With
  eight trees it would make an RCA on the durable tree wait behind a
  simulation on an unrelated goal tree, for no reason — the caches they mutate
  are disjoint. (The `waiting` progress stage stays meaningful: it now means
  "queued behind another run *on this tree*".)
- **`progress` stays global**, keyed by the client's `run_id`. It is not tree
  state; the id is already unique per run.
- **Trace caps must be global, not per-tree.** `MAX_CACHED_TRACES = 256` becomes
  256 × N if each tree gets its own cap, and an InferenceData holds every
  posterior draw. Either key one shared LRU on `(tree_id, metric, fit_end)`, or
  keep per-tree dicts under a shared accounting of total entries. The first is
  simpler and preferred.
- **Snapshots are already per-tree.** `_wrap_snapshots(..., tree_path, ...)`
  resolves `.breakdown/snapshots` relative to the tree file, so a directory of
  trees gets one snapshot store per tree with no change.
- **`_require_ready` / `_require_data` take a `TreeState`** instead of reading
  `request.app.state`. Their 503/422 semantics are unchanged, but the message
  should name the tree.

### 5.1 Lazy loading

Boot parses **every** tree's YAML (cheap, no I/O beyond the file) and fetches
**none**. That gives `GET /trees` a complete, instant index — titles, goals,
owners, metric counts, load errors — without touching a warehouse.

A tree's data loads on first request that needs it, under that tree's lock, with
a `loading` state visible to the index. The default tree may load eagerly (a
flag) so the common single-tree case boots exactly as it does today.

This is the one place the design earns its complexity: eight goal trees in a dbt
repo is eight warehouse round-trip sets, and paying for the seven nobody opened
is the difference between a tool that starts in three seconds and one that
starts in three minutes.

## 6. API surface

### 6.1 `GET /trees`

The index's data source. Answers from parsed YAML alone — never triggers a load.

```json
{
  "default": "business",
  "trees": [
    {
      "id": "q3_pro_growth",
      "title": "Q3 Pro member growth",
      "description": "200 net-new paying Pro members by Sep 30",
      "owner": "growth@acme.com",
      "period": "2026-Q3",
      "goal": {"metric": "pro_members_net_new", "target": 200,
               "direction": "up", "deadline": "2026-09-30"},
      "provider": "dbt",
      "metric_count": 18,
      "state": "loaded",
      "progress": {"current": 143.0, "target": 200, "as_of": "2026-08-11"}
    },
    {"id": "q3_activation", "title": "Q3 activation", "state": "not_loaded",
     "goal": {"metric": "activation_rate", "target": 0.42, "direction": "up"},
     "progress": null, "...": "..."}
  ]
}
```

- **`state`** is `loaded` | `not_loaded` | `loading` | `error` (with
  `load_error`). This is the field that keeps §2.3 honest: `progress: null` with
  `state: "not_loaded"` is *"we haven't looked"*, which the UI must render
  differently from a real zero.
- **`progress`** is present only for loaded trees. `current` is the goal
  metric's value at the tree's own data edge (the `as_of` anchor the cards
  already use), so it agrees with what the tree itself shows.

### 6.2 `POST /trees/{id}/load`

Explicit load, for the index's "load" affordance. Returns when the fetch
completes (or immediately with `state: "loading"` if another request got there
first). Everything else can also load implicitly.

### 6.3 Tree-scoped routes, with the current paths as aliases

Every existing data route gains a `/trees/{tree_id}` prefix:

```
GET  /trees/{tree}/meta          GET  /trees/{tree}/dag
GET  /trees/{tree}/series        GET  /trees/{tree}/metrics/{name}
POST /trees/{tree}/analyze/{name}
POST /trees/{tree}/rca/{name}    POST /trees/{tree}/rca/{name}/slices
POST /trees/{tree}/simulate
```

**The unprefixed routes stay, bound to the default tree.** That is not
politeness — it is what keeps every existing deep link, the README's curl
examples, the MCP tools, the demo, and the test suite working unchanged. A
path prefix is preferred over `?tree=` or a header because it is cache-friendly,
unambiguous in logs, and makes a shared URL self-describing.

`GET /progress/{run_id}` stays global and unprefixed — run ids are unique
already, and the poller shouldn't need to know which tree it is watching.

### 6.4 MCP

`breakdown/mcp/server.py`'s `_state()` gains an optional `tree` argument, and
every tool gains an optional `tree` parameter defaulting to the default tree.
An AI analyst asking "why did Q3 Pro signups stall" needs to be able to *find*
the goal tree first, so `get_tree` should either list trees when called with no
argument or gain a sibling `list_trees`. The `how_to_read` / `report_url`
pattern is unchanged; `report_url` must carry `#tree=`.

## 7. Frontend

### 7.1 The index

A new landing view at `/ui` (the tree view moves to `/ui#tree=<id>`), rendering
`GET /trees` as cards grouped by `period`:

- **Goal trees** show title, description, owner, and a progress bar —
  `143 / 200 · 72%` — plus a pace read against `deadline` where one exists
  ("on track" / "behind" is a *derived, uncertain* claim; it must be visibly
  softer than the engine's numbers, or it becomes a forecast the engine never
  made).
- **Non-goal trees** (no `tree.goal`) show title, description, and metric count.
  They are not failed goal cards.
- **Not-loaded trees** show the declared goal and a **Load** button, with the
  progress area explicitly reading "not loaded" rather than empty.
- **Errored trees** show the parse error and the `breakdown doctor` hint,
  matching the existing degraded banner's language.

### 7.2 The switcher

A select in the header, **leftmost, next to the brand** — tree is the outermost
scope, outside `Target`, and the header already reads left-to-right by
narrowing scope. Switching resets everything keyed on metric names: `state.dag`,
`series`, `metricCache`, `rca`, `whatif`, card overrides, and the RCA window
inputs. The safest implementation is to re-run `init()` against the new tree
rather than to patch state incrementally.

### 7.3 Deep links

`#tree=<id>` is parsed **first** and gates everything else, since
`#metric=`/`#rca=`/`#whatif=` are meaningless without knowing which tree's
metric names they refer to. A link with no `#tree=` means the default tree, so
every URL shared before this feature still resolves.

`updateShareMenu` / the copy-link path must include `#tree=` once there is more
than one tree; the saved-views feature (`localStorage`) must key on tree id, or
a view saved against one tree will replay against another.

## 8. Implementation order

Each step should land green; the first three are invisible to users.

1. **Parser**: the `tree:` block, `goal.metric` resolution, `direction`
   defaulting. Tests only.
2. **`TreeState` refactor** against a single tree — introduce the dataclass and
   an accessor, move the ~40 `request.app.state.X` reads onto it, change
   nothing about behavior. The whole existing suite must pass untouched; this
   is the step where that is the entire point.
3. **Directory discovery + lazy loading + `GET /trees`**, still serving the
   default tree at the current routes.
4. **Tree-scoped routes + aliases**, MCP `tree` argument.
5. **UI switcher + `#tree=`**.
6. **UI index page + progress + Load.**

## 9. Out of scope for v1

- **Cross-tree analysis.** No shared metrics between trees, no "this driver
  appears in three goals". Two trees naming the same metric are two independent
  nodes; that is a property of the fetch contract, not a limitation to fix
  later without thought.
- **Editing trees in the UI.** Trees are files in a repo; that is the point.
- **Snapshot-backed index progress** (§2.3) — the natural follow-on, gated on
  labelling freshness.
- **Per-tree auth.** Everything visible to one visitor is visible to all, as
  today.
- **Hot reload on file change.** Restart, as today.

## 10. Open questions

- **Does `period` group the index, or is `deadline` enough?** Free-form periods
  won't sort ("Q3" vs "2026-Q3" vs "Summer"). Deriving groups from `deadline`
  is more robust but loses the author's label.
- **What happens to a goal tree after its deadline?** Archiving is a real need
  by quarter two, and a directory that only grows is a bad index. A `tree.status`
  or an `archive/` convention are both plausible; neither is designed.
- **Should the durable business tree be visually distinct** from disposable goal
  trees on the index, beyond "has no goal block"?

import asyncio
import datetime
import hmac
import logging
import math
import os
import threading
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated, Any, Dict, MutableMapping, Optional, Tuple

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator
from starlette.datastructures import State

from breakdown.api.trees import (
    MAX_CACHED_TRACES,
    TraceStore,
    TreeState,
    discover_trees,
    parse_tree,
    resolve_default,
)
from breakdown.data_fetch import (
    SliceNotSupported,
    provider_query_name,
)
from breakdown.engine.model import (
    FIT_RANDOM_SEED,
    NUTS_CHAINS,
    NUTS_DRAWS,
    NUTS_TUNE,
    fit_metric,
    summarize_trace,
    warm_inference_imports,
)
from breakdown.engine.rca import resolve_reference_window, run_rca, shapley_attribution
from breakdown.engine.simulate import ScenarioRequest, run_scenario, validate_cold_start
from breakdown.engine.slices import entity_flows, slice_attribution
from breakdown.grains import next_start
from breakdown.loading import (
    build_fetcher,
    fetch_all_metrics,
    wrap_snapshots,
)
from breakdown.mcp.server import mcp

logger = logging.getLogger(__name__)

# The data routes. Mounted twice at the bottom of this module — bare and under
# `/trees/{tree_id}` — so a tree is addressed by path and the old paths keep
# working as aliases for the default tree.
router = APIRouter()

# Package-relative so the wheel install works the same as a repo checkout;
# static/ and examples/ ship inside the `breakdown` package.
DEFAULT_TREE_PATH = str(files("breakdown").joinpath("examples/jaffle_shop_tree.yml"))
DEFAULT_START_DATE = "2024-01-01"
DEFAULT_END_DATE = "2024-04-09"


# Why a provider has no query to show. Stated per provider rather than as one
# vague message, because "there is no SQL" and "we never see the SQL" are
# different facts about how much the user can verify.
_NO_PROVENANCE = {
    "mock": "The mock provider synthesizes data from the tree; no query is run.",
    "none": "Cold-start trees declare beliefs and fetch nothing.",
    "local": (
        "The local provider asks MetricFlow for a metric by name; MetricFlow "
        "plans and runs the SQL, and never returns it."
    ),
    "cloud": (
        "The dbt Cloud Semantic Layer plans and runs the query server-side; the "
        "API returns results, not SQL."
    ),
}


def _validate_date(value: str, label: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise RuntimeError(f"{label} must be a valid YYYY-MM-DD date, got '{value}'")
    return value


def _iso_date(value: Optional[str]) -> Optional[str]:
    """Reject anything that is not a real YYYY-MM-DD date, at the boundary.

    `str` is not a date type, and the engine's `pd.Timestamp(value)` is not a
    validator: `pd.Timestamp("")` is `NaT`, which satisfies the annotation,
    survives every `if value is None` guard and reaches `snap_window`, where
    `NaT.normalize()` is an `AttributeError` — a 500 for any client that
    submits a cleared date field. `"banana"` raised `ValueError` and became a
    correct 422; the empty string took the other path, which is the whole
    defect. Two routes already ran exactly this check inline (`/analyze`'s
    `fit_end`, `/rca/{name}/slices`' four dates) and their neighbours did not,
    so it is one annotated type now and every date parameter carries it.

    Raising `ValueError` here is what makes it a 422: FastAPI validates query
    parameters through pydantic, so this lands in the request-validation error
    response like a type mismatch would, rather than as an exception from the
    handler body.
    """
    if value is None:  # an omitted optional date, not a bad one
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"must be a valid YYYY-MM-DD date, got '{value}'")
    return value


# The two date parameter shapes. Every date-taking route uses one of them —
# `tests/test_project_invariants.py` enumerates the routes and checks it.
#
# Each site writes `Annotated[IsoDate, Query(description=...)]` rather than
# `IsoDate = Query(...)`: FastAPI rebuilds the field from a `Query` passed as
# the *default value* and drops the `Annotated` metadata with it, so the
# validator would silently never run. Nested `Annotated` flattens, so putting
# the `Query` inside keeps both.
IsoDate = Annotated[str, AfterValidator(_iso_date)]
OptionalIsoDate = Annotated[Optional[str], AfterValidator(_iso_date)]


# Live progress for the two long calls, keyed by a client-supplied run id.
# Bounded like `traces` for the same reason: a client that navigates away
# mid-run never sends the request that would clean its entry up, and on the
# public demo that is every visitor. Entries are tiny, so the cap is generous.
MAX_PROGRESS_ENTRIES = 64


class EngineBusy(Exception):
    """A cancelled request's engine thread is still finishing on this tree.

    The asyncio `tree.lock` released when its coroutine was cancelled (a
    middleware timeout, a shutdown, a client disconnect on Starlette versions
    that cancel), but threads are not cancellable and the orphan runs on. A
    second engine run beside it is the OOM rule 2 exists to prevent — two
    NUTS working sets on a 2 GB box — so the guard refuses with a 409 and a
    retry hint instead (roadmap C41).
    """


def _guarded(tree, fn, *args, **kwargs):
    """Run one engine call under the tree's thread-side guard (roadmap C41).

    Non-blocking on purpose: the per-tree asyncio lock already serializes the
    ordinary flow, so any contention here means an orphaned run. Blocking
    would quietly queue engine work the caller believes it owns the lock for.
    """
    if not tree.engine_guard.acquire(blocking=False):
        raise EngineBusy(
            f"Tree '{tree.id}' is still finishing an analysis whose caller "
            "went away (engine threads cannot be cancelled). Retry in a "
            "moment, once it completes."
        )
    try:
        return fn(*args, **kwargs)
    finally:
        tree.engine_guard.release()


def _progress_reporter(state, run_id: Optional[str], stage: str):
    """Register `run_id` and return a callback the engine can report through.

    Returns None when the client didn't ask for progress, which is also what
    every non-UI caller (curl, MCP, the tests) does — so the engine runs on its
    no-callback path and behaves exactly as it did before.

    The callback runs on the worker thread while `GET /progress` reads the same
    dict from the event loop. It **replaces** the entry rather than mutating it,
    so a reader sees either the old update or the new one, never a half-written
    dict — which is all the consistency a progress display needs.
    """
    if not run_id:
        return None
    # Entries are popped when their run completes, so everything present is a
    # *live* run — and the old eviction (`pop(next(iter(...)))`) therefore
    # always killed one mid-flight, whose poller then read "finished or never
    # started" while it kept executing (grill L5). Full means refuse: the
    # run proceeds without a progress channel, which is what every non-UI
    # caller gets anyway, and the log says why.
    if len(state.progress) >= MAX_PROGRESS_ENTRIES:
        logger.warning(
            "progress registry full (%d live runs): run %s proceeds without a progress channel",
            MAX_PROGRESS_ENTRIES,
            run_id,
        )
        return None
    state.progress[run_id] = {"stage": stage}

    def report(update: Dict[str, Any]) -> None:
        # Membership-guarded: after a cancelled request's `finally` pops this
        # run, its orphaned thread (roadmap C41) keeps reporting — an
        # unguarded write would resurrect the entry as a zombie that nothing
        # ever pops again.
        if run_id in state.progress:
            state.progress[run_id] = update

    return report


def _tree(request: Request) -> TreeState:
    """The `TreeState` a request addresses: the named tree, else the default.

    The id is read from the request's own path params rather than a handler
    argument, so one router serves both mount points: every data route is
    registered twice, once under `/trees/{tree_id}` and once bare. The bare
    path is what keeps existing deep links, the README's curl examples, the MCP
    tools and the test suite working unchanged."""
    state = request.app.state
    trees = state.trees
    tree_id = request.path_params.get("tree_id")
    if not trees:
        # Nothing was discovered at all — a 503 with the reason, not a 404
        # that reads as "you asked for the wrong tree".
        raise HTTPException(
            status_code=503,
            detail=f"breakdown started without a metric tree: {state.startup_error}. "
            "Check the --tree path and restart.",
        )
    tid = tree_id or state.default_tree
    tree = trees.get(tid)
    if tree is None:
        raise HTTPException(
            status_code=404,
            detail=f"No tree '{tid}'. Known trees: {', '.join(sorted(trees))}.",
        )
    return tree


async def _loaded_tree(request: Request) -> TreeState:
    """The addressed tree, with its data fetched if this is the first request
    that needs it."""
    tree = _tree(request)
    await _ensure_loaded(tree)
    return tree


async def _ensure_loaded(tree: TreeState) -> None:
    """Fetch a tree's data on first use, at most once.

    Under the tree's own lock and off the event loop, with the double check
    inside it: two viewers opening the same cold tree at the same moment must
    not both fetch. `loading` is what the index shows meanwhile — and a tree
    that failed to load has a `load_error` rather than being retried on every
    request, which would hammer a down warehouse once per click."""
    if tree.loaded or tree.load_error is not None:
        return
    async with tree.lock:
        if tree.loaded or tree.load_error is not None:
            return
        tree.loading = True
        try:
            await asyncio.to_thread(load_tree, tree)
        finally:
            tree.loading = False
    _start_earliest_discovery(tree)


def _require_ready(tree: TreeState) -> None:
    """503 on data endpoints while a tree is serving degraded (its parse or
    its data load failed); the detail carries the original error."""
    if tree.load_error is not None:
        raise HTTPException(
            status_code=503,
            detail=f"Tree '{tree.id}' started without data: {tree.load_error}. "
            f"Run `breakdown doctor --tree {tree.path}` to diagnose.",
        )


def _require_data(tree: TreeState) -> None:
    """422 on time-series endpoints for a cold-start tree (`provider: none`).
    A stated mode, not an error: the tree deliberately has no data, so
    analyses that consume history cannot exist — only /simulate can."""
    _require_ready(tree)
    if tree.data is None:
        raise HTTPException(
            status_code=422,
            detail=f"Tree '{tree.id}' declares no data provider (cold start mode); "
            "this endpoint needs time-series data. What-if simulation over "
            "the declared beliefs is available at POST /simulate.",
        )


def _remember_fit(traces: MutableMapping, key, fit) -> None:
    """Publish a fit into the shared cache, bounded and never downgrading.

    Two viewers share one process and one cache, so a fit that one of them
    requests is a fit the other may be shown. `/analyze` exposes
    `inference_method` and `draws` while the cache key carries neither, so a
    cheap 50-draw ADVI run would otherwise silently replace a NUTS fit that a
    previous RCA had already paid for. Ordering fits by quality — NUTS over
    ADVI, then by draw count — keeps the deliberate "confirm this with NUTS"
    upgrade working while blocking the accidental downgrade (C8).

    The trailing eviction is a backstop for a plain dict: a tree's `traces` is
    a `TraceView` onto the process-wide `TraceStore`, which caps *across* trees
    on every write (256 per tree would be 256 x N InferenceData objects) and
    caps by total bytes rather than entry count, because one entry's size
    scales with the loaded window.
    """
    existing = traces.get(key)
    if existing is not None and _fit_rank(existing) > _fit_rank(fit):
        return
    traces[key] = fit
    while len(traces) > MAX_CACHED_TRACES:
        traces.pop(next(iter(traces)))


def _fit_rank(fit) -> Tuple[int, int]:
    """(method quality, draws) — NUTS is exact MCMC, ADVI an approximation."""
    method = getattr(fit, "inference_method", "advi")
    draws = 0
    try:
        draws = int(fit.trace.posterior.sizes.get("draw", 0))
    except Exception:  # pragma: no cover - a trace without a posterior dim
        pass
    return (1 if method == "nuts" else 0, draws)


def _fit_summary(fit) -> Dict[str, Dict[str, Optional[float]]]:
    """The JSON-safe posterior summary for one fit, computed at most once.

    `az.summary` is the one heavy engine call `/metrics/{name}` makes, and it
    scales with `draws`: 1.1s on an 830-day ADVI trace with 1000 draws, and the
    UI's box goes to 5000. Nothing memoized it, so it was paid on *every* GET —
    and `clearRCA` in app.js re-fetches every fitted metric after wiping its
    own cache, which issues N of these back to back.

    The trace is immutable once fitted, so the answer is too: cache it on the
    `FitResult` itself, where it is collected with the fit it describes rather
    than outliving it in a side table. Callers run this via `asyncio.to_thread`
    — even memoized, it must not be the thing that decides whether /health
    answers.
    """
    cached = getattr(fit, "summary_json", None)
    if cached is not None:
        return cached
    # NaN/inf (e.g. r_hat on single-chain ADVI traces) are not valid JSON
    summary = {
        col: {k: (float(v) if math.isfinite(v) else None) for k, v in vals.items()}
        for col, vals in summarize_trace(fit.trace).to_dict().items()
    }
    try:
        fit.summary_json = summary
    except AttributeError:  # pragma: no cover - a slotted/frozen fit object
        pass
    return summary


def _pick_fit(traces: Dict[Tuple[str, Optional[str]], Any], name: str):
    """Best cached fit to summarize for a metric: prefer the full-window fit,
    else the one with the latest fit_end, else None."""
    if (name, None) in traces:
        return traces[(name, None)]
    # Snapshot before filtering: `run_rca` inserts into this dict from a worker
    # thread, and `.items()` is a live view (C8). Same race as /meta's.
    dated = [(fit_end, fit) for (n, fit_end), fit in list(traces.items()) if n == name]
    if not dated:
        return None
    return max(dated, key=lambda item: item[0])[1]


# Attributes that live on a `TreeState` and are aliased onto `app.state` for
# the default tree. `progress`, `trees`, `default_tree` and `trace_store` are
# genuinely app-wide and are not in here.
_TREE_ATTRS = frozenset(
    {
        "parser",
        "fetcher",
        "data",
        "traces",
        "slice_cache",
        "flow_cache",
        "lock",
        "earliest",
        "earliest_task",
        "loaded",
    }
)


class BreakdownState(State):
    """`app.state`, with the default tree's own state aliased onto it.

    The unprefixed routes are aliases for the default tree (§6.3), and so is
    `app.state`: `app.state.data` *is* `app.state.trees[default].data`, for
    reads and writes alike. That is not politeness — it is what keeps the MCP
    server, the README's examples and the whole test suite addressing the same
    attributes they always have while the routes underneath them work against
    one `TreeState` among many.

    `startup_error` is the one composite. It reads "can the default tree
    serve?", which is three different failures: `auth_error` when the auth
    variables are configured in a combination that would fail open,
    `discovery_error` when `--tree` named nothing loadable (no tree exists to
    hang it on), and the default tree's own `load_error` otherwise. The auth
    one comes first because it is the one the operator must fix before
    anything else the process says about itself matters.
    """

    def _default_tree(self):
        state = self._state
        trees = state.get("trees")
        if not trees:
            return None
        return trees.get(state.get("default_tree"))

    def __getattr__(self, key):
        if key in _TREE_ATTRS:
            tree = self._default_tree()
            if tree is not None:
                return getattr(tree, key)
        if key == "startup_error":
            tree = self._default_tree()
            return (
                self._state.get("auth_error")
                or self._state.get("discovery_error")
                or (tree.load_error if tree is not None else None)
            )
        if key == "startup_error_kind":
            tree = self._default_tree()
            if self._state.get("auth_error"):
                return "auth_config_error"
            if self._state.get("discovery_error"):
                return "discovery_error"
            if tree is not None and tree.load_error is not None:
                return tree.load_error_kind or "data_load_error"
            return None
        return super().__getattr__(key)

    def __setattr__(self, key, value):
        tree = self._default_tree() if key in _TREE_ATTRS or key == "startup_error" else None
        if tree is not None:
            setattr(tree, "load_error" if key == "startup_error" else key, value)
            return
        if key == "startup_error":
            key = "discovery_error"
        super().__setattr__(key, value)


class _McpMount:
    """ASGI shim for the MCP transport. The SDK's session manager is
    single-use per instance, but a process can run the lifespan more than
    once (tests open several TestClients), so each startup builds a fresh
    transport app and points the mounted shim at it."""

    def __init__(self):
        self.asgi = None

    def rebuild(self):
        # Stateless + plain-JSON: tools carry no per-session state, clients
        # survive --reload restarts, and the transport stays curlable. The
        # default transport security admits localhost hosts only — correct
        # for a loopback-bound server, but a non-loopback bind (`serve
        # --host 0.0.0.0`, containers) is reached under arbitrary Host
        # headers, so DNS-rebinding protection must come off there.
        security = None
        if os.environ.get("BREAKDOWN_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost"):
            from mcp.server.transport_security import TransportSecuritySettings

            security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.asgi = mcp.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )

    async def __call__(self, scope, receive, send):
        await self.asgi(scope, receive, send)


_mcp_mount = _McpMount()


def _window() -> Tuple[str, str]:
    """The loaded data window, validated. One `--start-date`/`--end-date` pair
    for the process, N trees over it."""
    start_date = _validate_date(
        os.environ.get("BREAKDOWN_START_DATE", DEFAULT_START_DATE), "start date"
    )
    end_date = _validate_date(os.environ.get("BREAKDOWN_END_DATE", DEFAULT_END_DATE), "end date")
    if end_date < start_date:
        raise RuntimeError(f"end date '{end_date}' is before start date '{start_date}'")
    return start_date, end_date


def load_tree(tree: TreeState) -> None:
    """Build one tree's fetcher and fetch every metric. Blocking — callers run
    it off the event loop, holding that tree's lock.

    Failure-soft, per tree: a bad token or an unreachable warehouse sets
    `load_error` and that tree serves 503s while the process — and every other
    tree — keeps running. A container never crash-loops on a bad credential,
    and per-metric diagnosis stays `doctor.py`'s job.
    """
    if tree.parser is None:  # parse failed at boot; there is nothing to load
        return
    provider_cfg = tree.parser.config.provider
    try:
        if provider_cfg.type == "none":
            # Cold-start tree: nothing is fetched and `data` stays None — a
            # stated mode, not a degraded load. Missing declarations would
            # otherwise surface one 422 at a time on /simulate, so check
            # readiness here and fail loudly with the full list.
            problems = validate_cold_start(tree.parser.dag)
            if problems:
                raise RuntimeError(
                    "tree declares no data provider but is not cold-start "
                    "ready: " + "; ".join(problems)
                )
            logger.info(
                "tree '%s' ready (cold start): %s metrics=%d — no data provider, "
                "serving what-if over declared beliefs",
                tree.id,
                tree.path,
                len(tree.parser.config.metrics),
            )
        else:
            start_date, end_date = _window()
            fetcher = build_fetcher(provider_cfg, tree.parser.dag, tree.parser.config.metrics)
            fetcher = wrap_snapshots(
                fetcher, provider_cfg.type, tree.path, slice_span=(start_date, end_date)
            )
            data = fetch_all_metrics(tree.parser, fetcher, provider_cfg.type, start_date, end_date)
            tree.fetcher = fetcher
            tree.data = data
            logger.info(
                "tree '%s' loaded: %s provider=%s window=[%s, %s] rows=%s",
                tree.id,
                tree.path,
                provider_cfg.type,
                start_date,
                end_date,
                ", ".join(f"{g}:{len(f)}" for g, f in data.frames.items()),
            )
        tree.loaded = True
    except Exception as e:
        tree.load_error = f"{type(e).__name__}: {e}"
        tree.load_error_kind = "data_load_error"
        logger.error(
            "Data load failed for tree '%s' (%s); serving degraded. "
            "Run `breakdown doctor --tree %s` to diagnose. %s",
            tree.id,
            tree.path,
            tree.path,
            e,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    tree_path = os.environ.get("BREAKDOWN_TREE", DEFAULT_TREE_PATH)
    # Per-tree state (parser, fetcher, data, caches, lock) lives on TreeState;
    # `app.state.<attr>` still reads and writes the **default** tree's, which
    # is what keeps every existing caller working (see `BreakdownState`).
    app.state.trees: Dict[str, TreeState] = {}
    app.state.default_tree = ""
    # Fitted models for every tree, capped process-wide: 256 per tree would be
    # 256 x N InferenceData objects, each holding every posterior draw. The cap
    # is a byte budget (BREAKDOWN_MAX_TRACE_BYTES) with the entry count as a
    # backstop — an entry's size scales with the loaded window, so a count
    # alone bounds nothing.
    app.state.trace_store = TraceStore()
    # A *discovery* failure (`--tree` pointing at nothing) has no tree to hang
    # itself on, so it stays global; a per-tree parse or load failure lands on
    # that tree's `load_error`, and `app.state.startup_error` reads the default
    # tree's. Either way the app still serves — /health carries the error, data
    # endpoints return 503, and the UI shows a banner pointing at `breakdown
    # doctor` — rather than a container crash-looping with no way to see why.
    app.state.discovery_error = None
    # An auth configuration that would fail open (see `_auth_config_error`).
    # The middleware refuses every non-open route with a 503 while this is set,
    # so the process does not serve data; it is recorded and logged here, and
    # read by `startup_error`, so `/health` and the UI banner say *why* rather
    # than leaving an operator staring at 503s. Same degraded-startup
    # discipline as a bad provider credential: loud, diagnosable, not a
    # crash-loop with the reason only in a log that scrolled past.
    app.state.auth_error = _auth_config_error()
    if app.state.auth_error:
        logger.error("Refusing to serve data routes: %s", app.state.auth_error)
    # The *other* fail-open combination was silent (roadmap C42, grill M5):
    # `_auth_config_error` refuses REQUIRE_AUTH-without-a-token with a 503 and
    # the loud line above, while a non-loopback bind with no token at all —
    # the Dockerfile's default CMD — served /mcp (which runs run_rca and
    # run_whatif and returns the whole tree) to anything that could reach the
    # port, with Host validation off, and logged nothing. Same file, opposite
    # policy, no stated reason. It stays permitted — `docker run` works out of
    # the box by decision (2026-08-30) — but never silently: this is the one
    # unmissable line an operator gets before exposing the port.
    _host = os.environ.get("BREAKDOWN_HOST", "127.0.0.1")
    if _host not in ("127.0.0.1", "localhost") and not os.environ.get("BREAKDOWN_API_TOKEN"):
        logger.error(
            "Serving on %s with no BREAKDOWN_API_TOKEN: every route, including "
            "/mcp (which runs analyses and returns the whole tree, with "
            "DNS-rebinding protection off for non-loopback binds), is open to "
            "anything that can reach this port. Set BREAKDOWN_API_TOKEN before "
            "exposing it beyond a trusted network; see docs/deploying.md.",
            _host,
        )
    # run_id -> the latest progress update from an in-flight RCA or simulation.
    # Written from the worker thread, read by GET /progress. **Not** tree
    # state: run ids are already unique, and a poller shouldn't need to know
    # which tree it is watching. See `_progress_reporter`.
    app.state.progress: Dict[str, Dict[str, Any]] = {}

    try:
        trees = discover_trees(tree_path)
        for tree in trees.values():
            tree.traces = app.state.trace_store.view(tree.id)
            parse_tree(tree)
        app.state.trees = trees
        app.state.default_tree = resolve_default(
            trees, os.environ.get("BREAKDOWN_DEFAULT_TREE") or None
        )
    except Exception as e:
        app.state.discovery_error = f"{type(e).__name__}: {e}"
        logger.error(
            "No metric tree could be discovered at %s; serving degraded. %s",
            tree_path,
            e,
        )

    # Boot parses every tree's YAML (cheap, no I/O beyond the file) and fetches
    # none, so `GET /trees` is a complete, instant index without touching a
    # warehouse. Eight trees in a dbt repo is eight sets of warehouse
    # round-trips, and paying for the seven nobody opened is the difference
    # between a tool that starts in three seconds and one that starts in three
    # minutes. `_eager_trees` names the exceptions.
    for tree in _eager_trees(app):
        await asyncio.to_thread(load_tree, tree)
        _start_earliest_discovery(tree)
    # PyMC/ArviZ/PyTensor are deferred out of `engine.model`'s module scope so
    # the port binds without paying for them (~27s on a shared-CPU VM, which is
    # what made Fly's proxy 503 the first visitor after an idle period). That
    # only moves the cost unless someone absorbs it, so absorb it here: a
    # daemon thread, started after the data load, importing while the operator
    # is still looking at the page. `fit_metric` re-imports from `sys.modules`
    # either way, so a slow or failed warm-up costs correctness nothing.
    threading.Thread(target=warm_inference_imports, name="warm-inference", daemon=True).start()

    # The MCP sub-app's own lifespan never runs under a Starlette mount, so
    # its session manager must be driven from here.
    _mcp_mount.rebuild()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        for tree in app.state.trees.values():
            task = tree.earliest_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def _eager_trees(app: FastAPI):
    """Which trees to load at startup rather than on first use.

    A **file** argument is one tree and nothing is saved by deferring it, so it
    loads eagerly and the single-tree case boots exactly as it always did. A
    **directory** defers everything, including the default — that is the whole
    point of §5.1 — unless `--eager` (`BREAKDOWN_EAGER=1`) asks for the default
    tree back, for a deployment that knows which tree its visitors open.
    """
    trees = app.state.trees
    if not trees:
        return []
    default = trees.get(app.state.default_tree)
    if default is None:
        return []
    if len(trees) == 1 or os.environ.get("BREAKDOWN_EAGER") == "1":
        return [default]
    return []


def _start_earliest_discovery(tree: TreeState) -> None:
    """History discovery runs in the background: one provider round-trip per
    metric would roughly double a cold load, and /meta must stay instant — it
    reports whatever has arrived."""
    if tree.fetcher is None or tree.parser is None:
        return
    tree.earliest_task = asyncio.create_task(_discover_earliest(tree))


async def _discover_earliest(tree: TreeState) -> None:
    """Fill `tree.earliest` metric by metric. Failure-soft per metric:
    earliest_date never raises by contract, and a surprise here must not
    take the app down with it."""
    provider_type = tree.parser.config.provider.type
    for metric in tree.parser.config.metrics:
        if metric.derived:
            # Nothing to ask a provider about: the series is computed from
            # parents, whose own history is what bounds it.
            continue
        query_name = provider_query_name(provider_type, metric)
        try:
            earliest = await asyncio.to_thread(tree.fetcher.earliest_date, query_name, metric.grain)
        except Exception as e:  # pragma: no cover - belt over the contract
            logger.info("earliest_date failed for '%s': %s", metric.name, e)
            earliest = None
        tree.earliest[metric.name] = earliest


app = FastAPI(title="breakdown API", lifespan=lifespan)


@app.exception_handler(EngineBusy)
async def _engine_busy_handler(request: Request, exc: EngineBusy):
    # 409, not 503: the server is healthy and the request is well-formed —
    # the resource (this tree's one engine slot) is momentarily held by an
    # orphaned run (roadmap C41). Retryable, and the body says when.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# `app.state` aliases the default tree's own state — see `BreakdownState`.
app.state = BreakdownState(app.state._state)


def _presents_token(request: Request, token: str) -> bool:
    """Whether this request carries `Authorization: Bearer <token>`.

    Compared as **bytes**, not str: `hmac.compare_digest` raises TypeError on a
    str containing non-ASCII, so a header of `Bearer sécret` used to be a 500
    from inside the middleware rather than the 401 every other wrong token
    gets — a trivially reachable error-page-vs-401 oracle (L3). Starlette
    decodes header values latin-1 (HTTP's byte-to-str mapping), so latin-1 is
    the exact inverse and round-trips the bytes the client actually sent; the
    token comes from the environment, which Python decoded utf-8. A value that
    cannot round-trip is not a token we issued, so it compares as empty rather
    than raising — still one constant-time comparison, on the same path.
    """
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    try:
        presented_bytes = presented.encode("latin-1")
    except UnicodeEncodeError:
        presented_bytes = b""
    token_bytes = token.encode("utf-8", "surrogateescape")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented_bytes, token_bytes)


def _under(path: str, prefix: str) -> bool:
    """Prefix match on **path-segment boundaries**.

    `path.startswith("/mcp")` also matches `/mcphony`, which is the wrong shape
    of test for a security decision even when no such route exists today: the
    day someone adds `/metadata`, a `startswith("/meta")` open-list would hand
    it out. Only `/mcp` itself and things genuinely under it match here.
    """
    return path == prefix or path.startswith(prefix + "/")


# What stays reachable without a token even in BREAKDOWN_REQUIRE_AUTH mode.
# Deliberately an *allow*-list, so the gate fails closed: a route added
# tomorrow is gated by default rather than open until someone remembers it.
#
# - `/health` is liveness/readiness. `compose.yaml`'s healthcheck calls it with
#   no credentials, and orchestrators can't present one, so gating it makes a
#   correctly-configured deployment look dead.
# - `/ui` is a JS bundle, not data. Its *fetches* are gated, which is the
#   intended consequence: this mode assumes a reverse proxy (Cloudflare Access
#   and the like) injecting the header, or an operator who accepts that the
#   browser needs one. A login, a cookie or a token-in-the-URL would be hosted
#   mode (roadmap 3.5) and is deliberately not built here.
# - `/` is a one-line "the API is running" message and carries nothing.
_OPEN_PATHS = frozenset({"/", "/health"})
_OPEN_PREFIXES = ("/ui",)

# Anything but an explicit off switch counts as on, so that a typo
# (`BREAKDOWN_REQUIRE_AUTH=ture`) closes the door rather than opening it.
_AUTH_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})

_AUTH_MISCONFIGURED = (
    "BREAKDOWN_REQUIRE_AUTH is set but BREAKDOWN_API_TOKEN is empty, so every "
    "request would be checked against an empty secret and pass. Set "
    "BREAKDOWN_API_TOKEN, or unset BREAKDOWN_REQUIRE_AUTH."
)


def _open_path(path: str) -> bool:
    return path in _OPEN_PATHS or any(_under(path, prefix) for prefix in _OPEN_PREFIXES)


def _require_auth() -> bool:
    """Whether BREAKDOWN_REQUIRE_AUTH asks for the whole API to be gated."""
    value = os.environ.get("BREAKDOWN_REQUIRE_AUTH")
    return value is not None and value.strip().lower() not in _AUTH_OFF_VALUES


def _auth_config_error() -> Optional[str]:
    """The one auth configuration that must not be served: gate everything,
    with nothing to gate it against."""
    if _require_auth() and not os.environ.get("BREAKDOWN_API_TOKEN"):
        return _AUTH_MISCONFIGURED
    return None


@app.middleware("http")
async def bearer_token(request: Request, call_next):
    """The bearer-token gate. Two levels, both opt-in through the environment.

    **BREAKDOWN_API_TOKEN alone gates `/mcp`.** The MCP endpoint runs whole
    analyses, so exposing it off loopback without a gate hands anyone who finds
    the URL the tree and its data. Unset (the laptop default) keeps the
    loopback workflow friction-free; set closes that one surface and nothing
    else, which is what existing deployments already depend on.

    **BREAKDOWN_REQUIRE_AUTH=1 extends the same check to every data route** —
    /meta, /dag, /series, /metrics/*, /analyze/*, /shapley/*, /rca/*,
    /simulate, /progress/*, /trees, /trees/{id}/load and their
    `/trees/{tree_id}/…` aliases. Gating here rather than per route is what
    keeps the two mounts from drifting: the router is included twice, but the
    middleware sees one resolved path, so an alias cannot be gated differently
    from the route it aliases. `_OPEN_PATHS` names the exceptions.

    Set without a token it would gate everything against an empty secret and
    pass everything, so that combination is refused (503) rather than served —
    and `lifespan` says so loudly at startup.

    A down payment on hosted mode (roadmap 3.5), not a substitute for it: one
    shared secret, no per-user identity, no revocation short of a redeploy."""
    path = request.url.path
    if _open_path(path):
        return await call_next(request)

    config_error = _auth_config_error()
    if config_error:
        return JSONResponse({"detail": config_error}, status_code=503)

    token = os.environ.get("BREAKDOWN_API_TOKEN")
    if token and (_require_auth() or _under(path, "/mcp")):
        if not _presents_token(request, token):
            detail = (
                "Missing or invalid bearer token for /mcp."
                if _under(path, "/mcp")
                else "Missing or invalid bearer token. This deployment sets "
                "BREAKDOWN_REQUIRE_AUTH, so every data route needs "
                "`Authorization: Bearer <BREAKDOWN_API_TOKEN>`."
            )
            return JSONResponse(
                {"detail": detail},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


@app.middleware("http")
async def ui_no_cache(request: Request, call_next):
    """Make browsers revalidate the no-build-step UI on every load.

    StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers
    heuristically cache /ui assets and keep showing a stale app.js/index.html
    after an upgrade. `no-cache` forces a conditional request each time —
    unchanged files still come back as cheap 304s."""
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-cache"
    return response


static_dir = str(files("breakdown").joinpath("static"))
app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")

# MCP for AI assistants, at /mcp. The transport app is rebuilt by each
# lifespan run (see _McpMount), so the mount is a shim that delegates to
# the current one.
app.mount("/mcp", _mcp_mount)


@app.get("/")
async def root():
    return {"message": "breakdown API is running. Visit /ui for the visualization."}


_HEALTH_ERROR_TEXT = {
    "parse_error": (
        "The default tree failed to parse. The full diagnostic is in the "
        "server log and on GET /trees."
    ),
    "data_load_error": (
        "The default tree parsed but its data failed to load (a provider "
        "credential, an unreachable source, or a bad window). The full "
        "diagnostic is in the server log and on GET /trees; run "
        "`breakdown doctor --tree <path>` to diagnose the provider."
    ),
    "auth_config_error": (
        "Authentication is misconfigured: BREAKDOWN_REQUIRE_AUTH is set "
        "without BREAKDOWN_API_TOKEN, so data routes refuse to serve."
    ),
    "discovery_error": ("Tree discovery failed. The full diagnostic is in the server log."),
    None: "Startup failed. The full diagnostic is in the server log.",
}


@app.get("/health")
async def health(request: Request):
    """Liveness + readiness in one: always 200 (the process is up), with
    `status` distinguishing ok from degraded so orchestrators and the UI can
    react without treating a bad data source as a dead container.

    Reports on the **default** tree, like every other unprefixed route; the
    per-tree view is `GET /trees`, which carries each tree's own state."""
    state = request.app.state
    if state.startup_error is not None:
        # A stable classification, never the exception text (roadmap C43,
        # grill M6): this route is deliberately open — it must be safe for an
        # orchestrator to poll unauthenticated — and the raw text was leaking
        # the tree's SQL on a parse failure and provider hostnames and
        # usernames on a connection failure, through the one route auth
        # leaves open. The full diagnostic stays in the server log and on
        # the auth-gated `GET /trees` card.
        kind = state.startup_error_kind
        return {
            "status": "degraded",
            "error_kind": kind,
            "error": _HEALTH_ERROR_TEXT.get(kind, _HEALTH_ERROR_TEXT[None]),
        }
    # A parse failure sets `load_error`, which `startup_error` reads, so
    # reaching here means the default tree parsed — its provider and metric
    # count are known whether or not its data has been fetched yet.
    parser = state.parser
    return {
        "status": "ok",
        "provider": parser.config.provider.type,
        "metrics": len(parser.config.metrics),
    }


def _goal_progress(tree: TreeState) -> Optional[Dict[str, Any]]:
    """Current-vs-target for a loaded tree that declares a goal, else None.

    `current` is the goal metric's value at the tree's own data edge — the same
    anchor the node cards use (the oldest `data_through` across the tree, and
    only periods *fully completed* by it) — so the index agrees with what the
    tree itself shows rather than quoting a fresher half-period nothing else
    displays. None for a tree that isn't loaded: §2.3's whole point is that the
    index says when it doesn't know, and a dash is the one honest answer there.
    A tree with no `tree.goal` simply has no progress to report — most trees
    don't, and that is not a gap.
    """
    meta = tree.meta
    goal = meta.goal if meta else None
    data = tree.data
    if goal is None or data is None or goal.metric not in data.grain_of:
        return None
    edges = [e for e in (data.data_through(n) for n in data.grain_of) if e is not None]
    anchor = min(edges) if edges else data.date_end
    grain = data.grain_of[goal.metric]
    series = data.series(goal.metric)
    day = pd.Timedelta(days=1)
    complete = [
        (next_start(d, grain) - day, v)
        for d, v in zip(series["date"], series[goal.metric])
        if next_start(d, grain) - day <= anchor
    ]
    if not complete:
        return None
    period_end, value = complete[-1]
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return {
        "current": float(value),
        "target": goal.target,
        "as_of": str(period_end.date()),
    }


def _tree_card(tree: TreeState) -> Dict[str, Any]:
    """One row of the index: what `GET /trees` can say from parsed YAML alone,
    plus progress when this tree happens to be loaded already."""
    meta = tree.meta
    goal = meta.goal if meta else None
    return {
        "id": tree.id,
        "title": tree.title,
        "description": meta.description if meta else None,
        "owner": meta.owner if meta else None,
        "period": meta.period if meta else None,
        "goal": goal.model_dump() if goal else None,
        "provider": tree.provider_type,
        "metric_count": len(tree.parser.config.metrics) if tree.parser else 0,
        # `loaded` | `loading` | `not_loaded` | `error`. This is the field that
        # keeps the lazy index honest: `progress: null` with `not_loaded` is
        # "we haven't looked", which must not render as a zero.
        "state": tree.state,
        "load_error": tree.load_error,
        "progress": _goal_progress(tree),
    }


@app.get("/trees")
async def list_trees(request: Request):
    """The index's data source. Answers from parsed YAML alone and **never
    triggers a load** — eight trees in a dbt repo is eight sets of warehouse
    round-trips, and this endpoint has to be instant on a cold process for the
    lazy loading below it to be worth anything."""
    state = request.app.state
    return {
        "default": state.default_tree,
        "trees": [_tree_card(t) for t in state.trees.values()],
        # The discovery failure's full text lives here, not on /health
        # (roadmap C43): with zero trees there is no card to carry it, this
        # route is auth-gated whenever auth is configured, and the UI's
        # degraded banner reads the index anyway.
        "discovery_error": state.discovery_error,
    }


@app.post("/trees/{tree_id}/load")
async def load_tree_endpoint(tree_id: str, request: Request):
    """Explicit load, behind the index's **Load** affordance. Returns when the
    fetch completes; a second caller that arrives mid-fetch waits on the same
    lock rather than starting a second one."""
    tree = _tree(request)
    await _ensure_loaded(tree)
    return _tree_card(tree)


@router.get("/meta")
async def get_meta(request: Request):
    """Bootstrap info for the UI: metrics, data window, provider, fit status.
    `mode` tells the UI which surface to boot: "fitted" (data-backed) or
    "cold_start" (no data provider — what-if over declared beliefs only)."""
    tree = await _loaded_tree(request)
    _require_ready(tree)
    parser = tree.parser
    data = tree.data
    if data is None:
        return {
            "mode": "cold_start",
            "tree": tree.id,
            "provider": parser.config.provider.type,
            "metrics": [m.name for m in parser.config.metrics],
            "date_start": None,
            "date_end": None,
            "grains": {m.name: m.grain for m in parser.config.metrics},
            "kinds": {m.name: m.kind for m in parser.config.metrics},
            "data_through": {},
            "earliest_available": {},
            "fitted": [],
        }
    # A metric whose data edge is unknown is reported as `null`, not omitted.
    # Omitting it made "we don't know when this metric's data ends" and "this
    # metric doesn't exist" the same absence, so the UI's tree-wide as-of anchor
    # (a min over the present keys) silently skipped it and its card showed no
    # freshness row at all — a metric with no data quietly excluded from the
    # freshness calculation it should have been the worst case in.
    data_through = {}
    for name in data.grain_of:
        through = data.data_through(name)
        data_through[name] = str(through.date()) if through is not None else None
    return {
        "mode": "fitted",
        "tree": tree.id,
        "provider": parser.config.provider.type,
        "metrics": [m.name for m in parser.config.metrics],
        "date_start": str(data.date_start.date()),
        "date_end": str(data.date_end.date()),
        "grains": dict(data.grain_of),
        "kinds": dict(data.kind_of),
        # Inclusive last covered date per metric (end of its last observed
        # period) — the honest data edge, which may lag the requested window
        # when a source mart is behind.
        "data_through": data_through,
        # Earliest date the provider has per metric (null = can't say), from
        # the background discovery task — dict(...) snapshots it for the same
        # worker-thread reason as `fitted` below. Lets the UI say "history
        # exists before --start-date; widen it to train on more".
        "earliest_available": dict(tree.earliest),
        # `list(...)` snapshots the keys in one bytecode op rather than
        # iterating lazily: `run_rca` mutates this dict from a worker thread
        # (it is handed the cache directly and fits on demand), so a lazy
        # comprehension here raced it into "dictionary changed size during
        # iteration" — an intermittent 500 for one viewer precisely while
        # another's analysis ran, which is the single most likely way a
        # multi-viewer demo breaks (C8).
        "fitted": sorted({name for (name, _) in list(tree.traces)}),
    }


# What a node definition exposes that is infrastructure rather than modelling:
# `sql` is the metric's whole statement, `bind` the table plus its WHERE-clause
# business logic. Everything else in the definition (parents, priors, grain,
# dimensions) is the tree the UI draws.
_SENSITIVE_DEFINITION_FIELDS = ("sql", "bind")


@router.get("/dag")
async def get_dag(request: Request):
    """The tree's shape and every node's definition.

    When `BREAKDOWN_API_TOKEN` is set and the caller doesn't present it, the
    `sql` and `bind` blocks are redacted to null. `/dag` is open by design —
    the UI is unauthenticated and needs the shape to draw anything — but on a
    deployment that bothered to configure a token, "the graph is public" should
    not also mean "our fully-qualified table names and filter logic are
    public". Redacted to `null` rather than dropped, so a client reading
    `def.sql` sees an absent query rather than a KeyError. Unset (the laptop
    default) behaves exactly as before. `GET /metrics/{name}/query` refuses
    under the same condition (roadmap C31) — an earlier edition of this
    docstring pointed callers there while that route had no gate at all,
    which was grill H2.
    """
    tree = _tree(request)
    _require_ready(tree)
    parser = tree.parser
    token = os.environ.get("BREAKDOWN_API_TOKEN")
    redact = bool(token) and not _presents_token(request, token)
    nodes = []
    for name, attrs in parser.dag.nodes(data=True):
        definition = attrs["definition"].model_dump()
        if redact:
            for key in _SENSITIVE_DEFINITION_FIELDS:
                if key in definition:
                    definition[key] = None
        nodes.append([name, definition])
    return {"nodes": nodes, "edges": [list(e) for e in parser.dag.edges()]}


@router.get("/series")
async def get_series(request: Request):
    """Every metric's series at its native grain, for the node cards. Mixed
    grains mean there is no single shared date axis: each metric carries its
    own period-start dates. NaN -> null for valid JSON."""
    tree = await _loaded_tree(request)
    _require_data(tree)
    parser = tree.parser
    data = tree.data
    metrics = {}
    for m in parser.config.metrics:
        if m.name not in data.grain_of:
            continue
        s = data.series(m.name)
        metrics[m.name] = {
            "grain": data.grain_of[m.name],
            "dates": [str(d.date()) for d in s["date"]],
            "values": [
                None if (v is None or (isinstance(v, float) and not math.isfinite(v))) else float(v)
                for v in s[m.name].tolist()
            ],
        }
    return {"metrics": metrics}


@router.get("/metrics/{name}/query")
async def get_metric_query(name: str, request: Request, dimension: Optional[str] = None):
    """The query behind a metric's numbers, when the provider knows it.

    Principle 3 — never ship a number the engine can't defend — has had a hole
    in it: for most providers a user cannot see what was asked, so the number is
    unfalsifiable by exactly the person being asked to trust it. `warehouse` was
    the only exception, and only because the author wrote the SQL themselves.

    `sql: null` is a real answer rather than an error. `mock` synthesizes, and
    the semantic-layer providers hand a metric name to someone else's planner
    and never see SQL — so the response says which case it is instead of
    implying the query is missing.

    **When `/dag` redacts, this route refuses** (roadmap C31, grill H2). The
    redaction there nulled `sql`/`bind` behind `BREAKDOWN_API_TOKEN` while
    this route — which `get_dag`'s own docstring pointed callers at — handed
    the full statement to anyone, fully-qualified table names and filter
    logic included. It refuses with a 403 rather than nulling, because a
    redacted `sql: null` here would be indistinguishable from `mock`'s
    legitimate `sql: null` — a substitute wearing a real answer's shape,
    which is rule 1's defect.
    """
    # Degraded startup leaves no parser, so this needs the same 503 the data
    # endpoints give rather than an AttributeError. Provenance is *more* useful
    # when things are broken, but it still needs a tree that loaded.
    tree = await _loaded_tree(request)
    _require_ready(tree)
    token = os.environ.get("BREAKDOWN_API_TOKEN")
    if token and not _presents_token(request, token):
        raise HTTPException(
            status_code=403,
            detail=(
                "Query provenance is redacted because BREAKDOWN_API_TOKEN is set "
                "and this request does not present it. Send the token as a "
                "Bearer authorization header to read the generated SQL."
            ),
        )
    parser = tree.parser
    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    provider = parser.config.provider.type
    fetcher = tree.fetcher
    payload = {
        "metric": name,
        "dimension": dimension,
        "provider": provider,
        "sql": None,
        "dialect": None,
        "executed": None,
        "note": None,
    }
    if dimension is not None:
        spec = metric.dimensions.get(dimension)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"Metric '{name}' declares no dimension '{dimension}'",
            )
        dimension_source = spec.source
    else:
        dimension_source = None

    if fetcher is None:
        payload["note"] = "No provider is attached (the tree has no data)."
        return payload

    query_name = provider_query_name(provider, metric)
    # The loaded window, so a provider that can generate its query does so even
    # when a snapshot served the series and nothing executed this process.
    data = tree.data
    start, end = (
        (str(data.date_start.date()), str(data.date_end.date()))
        if data is not None
        else (None, None)
    )
    sql = fetcher.query_provenance(
        query_name,
        dimension_source,
        grain=getattr(metric, "grain", "day"),
        start_date=start,
        end_date=end,
    )
    if sql is None:
        payload["note"] = _NO_PROVENANCE.get(
            provider, f"The '{provider}' provider does not expose a query."
        )
        if dimension is not None and provider == "warehouse":
            payload["note"] = (
                "The warehouse provider does not support slicing yet "
                "(roadmap 2.8), so there is no sliced query to show."
            )
        return payload

    # Read through the snapshot wrapper: it delegates the query but carries
    # none of the provider's own attributes.
    inner = getattr(fetcher, "inner", fetcher)
    payload["sql"] = sql
    payload["dialect"] = getattr(inner, "dialect", None) or None
    # Whether this statement ran, or is what *would* run for the loaded window.
    # A snapshot hit serves the number without executing anything; the binding
    # still determines it exactly, so the query is real provenance either way —
    # but the reader is told which, rather than left to assume.
    if hasattr(inner, "executed"):
        payload["executed"] = bool(inner.executed(query_name, dimension_source))
        if not payload["executed"]:
            payload["note"] = (
                "This series was served from a snapshot, so no query ran. "
                "Shown is the statement the binding produces for this window."
            )
    return payload


@router.get("/metrics/{name}")
async def get_metric(name: str, request: Request):
    tree = await _loaded_tree(request)
    _require_ready(tree)
    parser = tree.parser
    data = tree.data
    traces = tree.traces

    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    if data is None:
        # Cold-start tree: the definition (with its asserted baseline and
        # plausible band) is the whole story — there is no series to show.
        time_series = []
    else:
        try:
            series = data.series(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No data found for metric '{name}'")
        # An undefined period is `null`, never a NaN and never a 0 (rule 3:
        # no engine result reaches an encoder unsanitized). `/series` has
        # always done this; this route did not, so one undefined rate period
        # was an unhandled 500 on the whole metric — which is exactly how the
        # White Cube demo's `churn_arpu` behaved before roadmap 1.11.
        time_series = [
            {
                "date": row["date"],
                name: None
                if (
                    row[name] is None
                    or (isinstance(row[name], float) and not math.isfinite(row[name]))
                )
                else float(row[name]),
            }
            for row in series.to_dict(orient="records")
        ]

    summary = None
    diagnostics = None
    inference_method = None
    fit_end = None
    fit = _pick_fit(traces, name)
    if fit is not None:
        summary = await asyncio.to_thread(_fit_summary, fit)
        diagnostics = fit.diagnostics
        # Top-level, not only `diagnostics.method` (roadmap C35): the sampler
        # and the window the fit ends at are the two facts that make one fit
        # comparable with another, and the Metric tab was left *inferring*
        # ADVI from the presence of a k̂. Same names the RCA node payload uses.
        inference_method = fit.inference_method
        fit_end = fit.fit_end

    return {
        "definition": metric.model_dump(),
        "inference_method": inference_method,
        "fit_end": fit_end,
        "time_series": time_series,
        "summary": summary,
        "diagnostics": diagnostics,
    }


@router.get("/metrics/{name}/ppc")
async def get_metric_ppc(name: str, request: Request):
    """Roadmap S10: the observed-vs-replicated series behind this node's
    posterior predictive verdict.

    A route of its own rather than a field on `GET /metrics/{name}`, and the
    reason is that route's own history: `_fit_summary` had to be memoized
    because `clearRCA` re-fetches *every* fitted metric back to back, and on a
    106-metric tree that is 106 requests. The band is ~88 kB of JSON per fitted
    node — measured on White Cube's `trials_started` over 790 days, and
    window-scaled, unlike everything else on that response — so folding it in
    would have made a bookkeeping loop move megabytes to repaint some edge
    labels. Only the metric card wants it, and only when it is open.

    Three answers, all 200, because "this node has no band" is information and
    not an error:

    - `ppc_status: null` — no fit is cached for this metric. Nothing was
      checked; the card says so and offers the Analyze button.
    - `band: null` with a `reason` — a fit exists and the check or the band
      could not be produced. Unchecked, named, never an empty chart (rule 3).
    - `band` — the arrays, all of length `band.n_periods`.
    """
    tree = await _loaded_tree(request)
    _require_ready(tree)
    metric = tree.parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    # The same `_pick_fit` the metric route summarizes, so the band and the
    # `ppc_status` rendered beside it always describe one fit. Two routes
    # choosing independently is how a chart comes to illustrate a verdict that
    # was reached about a different window.
    fit = _pick_fit(tree.traces, name)
    if fit is None:
        return {"metric": name, "ppc_status": None, "band": None, "reason": None}

    band = getattr(fit, "ppc_band", None)
    status = fit.diagnostics.get("ppc_status")
    if not band or band.get("reason") is not None:
        return {
            "metric": name,
            "ppc_status": status,
            "band": None,
            # A band withheld for its own reason says that reason; one absent
            # because the check never ran says the check's. Either way the
            # card prints a sentence, not a blank panel.
            "reason": (band or {}).get("reason")
            or (fit.diagnostics.get("ppc") or {}).get("reason")
            or "this fit stored no posterior predictive band",
        }
    return {"metric": name, "ppc_status": status, "band": band, "reason": None}


@router.post("/analyze/{name}")
async def analyze_metric(
    name: str,
    request: Request,
    inference_method: str = Query(default="nuts", pattern="^(nuts|advi)$"),
    # The budget is the engine's, not this route's (roadmap C27). This route
    # used to declare `tune=500` while `run_rca`/`run_scenario` inherited
    # `fit_metric`'s 1000, so the same node over the same window came back
    # from a different warm-up depending on which URL the reader called —
    # invisible in the payload, and a broken promise that a fit is a pure
    # function of (DAG, data, target). The knobs stay overridable per request;
    # only the *defaults* are now read from one place.
    draws: int = Query(default=NUTS_DRAWS, ge=50, le=5000),
    tune: int = Query(default=NUTS_TUNE, ge=50, le=5000),
    chains: int = Query(default=NUTS_CHAINS, ge=1, le=8),
    fit_end: Annotated[OptionalIsoDate, Query()] = None,
    # Grill L4: the one route that holds the tree lock for a whole NUTS run
    # was also the only analysis route with no progress channel — a second
    # viewer's /rca queued behind `/analyze?chains=8&draws=5000` polled
    # `{"stage": "waiting"}` with no way to learn what it was waiting for.
    run_id: Optional[str] = Query(
        default=None, description="Opaque id to poll on GET /progress/{run_id}"
    ),
):
    tree = await _loaded_tree(request)
    _require_data(tree)
    parser = tree.parser
    data = tree.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    state = request.app.state
    report = _progress_reporter(state, run_id, "waiting")

    # `fit_end` lets the "confirm with NUTS" workflow reproduce exactly what
    # RCA fitted; `OptionalIsoDate` is what checks it is a date.
    #
    # And `random_seed` is the other half of that promise (roadmap S22). This
    # route passed no seed while `run_rca` and `run_scenario` both passed
    # `FIT_RANDOM_SEED`, so "reproduce exactly what RCA fitted" was reachable
    # in the window but not in the sampler — and with `?inference_method=advi`
    # the PSIS k-hat it reported changed between two identical requests
    # (1.23 then 1.91 on the demo's `customer_churn_rate`). A diagnostic that
    # answers differently about the same fit is not one.
    try:
        async with tree.lock:
            if report:
                report({"stage": "fitting", "metric": name, "current": 1, "total": 1})
            fit = await asyncio.to_thread(
                _guarded,
                tree,
                fit_metric,
                parser.dag,
                data,
                name,
                draws=draws,
                tune=tune,
                inference_method=inference_method,
                chains=chains,
                fit_end=fit_end,
                random_seed=FIT_RANDOM_SEED,
            )
            _remember_fit(tree.traces, (name, fit_end), fit)
    finally:
        if run_id:
            state.progress.pop(run_id, None)

    return {
        "status": "success",
        "message": f"Analysis complete for '{name}'",
        "inference_method": inference_method,
        "diagnostics": fit.diagnostics,
    }


@router.get("/shapley/{name}")
async def get_shapley(
    name: str,
    request: Request,
    analysis_start: Annotated[IsoDate, Query(description="Start of analysis window (YYYY-MM-DD)")],
    analysis_end: Annotated[IsoDate, Query(description="End of analysis window (YYYY-MM-DD)")],
    reference_start: Annotated[
        OptionalIsoDate,
        Query(
            description="Start of baseline window (YYYY-MM-DD); omit both reference "
            "dates to default to the matched adjacent block before the analysis window"
        ),
    ] = None,
    reference_end: Annotated[
        OptionalIsoDate, Query(description="End of baseline window (YYYY-MM-DD)")
    ] = None,
):
    tree = await _loaded_tree(request)
    _require_data(tree)
    parser = tree.parser
    data = tree.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    # Off the event loop and under the tree lock, like every other engine call
    # in this file (roadmap C33, grill H4): the enumeration is O(2ⁿ) and a
    # 10-parent node measured 1.09s inline — one unauthenticated request
    # freezing /health (the container healthcheck), every /progress poll and
    # every /ui asset for the duration. `MAX_SHAPLEY_PARENTS` caps the work;
    # it does not stop it landing on the loop.
    try:
        async with tree.lock:
            result = await asyncio.to_thread(
                _guarded,
                tree,
                shapley_attribution,
                parser.dag,
                data,
                name,
                reference_start=reference_start,
                reference_end=reference_end,
                analysis_start=analysis_start,
                analysis_end=analysis_end,
            )
    # RuntimeError too (roadmap C38): `grains.fit_frame` raises it for an
    # empty grain join — a caller-fixable window/grain mismatch whose message
    # names the remedy — and it surfaced as an unhandled 500 with the
    # diagnostic thrown away, while a neighbouring loader wraps the identical
    # call in `except (ValueError, RuntimeError, KeyError)`.
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    return result


@router.post("/rca/{name}")
async def root_cause_analysis(
    name: str,
    request: Request,
    analysis_start: Annotated[IsoDate, Query(description="Start of analysis window (YYYY-MM-DD)")],
    analysis_end: Annotated[IsoDate, Query(description="End of analysis window (YYYY-MM-DD)")],
    reference_start: Annotated[
        OptionalIsoDate,
        Query(
            description="Start of baseline window (YYYY-MM-DD); omit both reference "
            "dates to default to the matched adjacent block before the analysis window"
        ),
    ] = None,
    reference_end: Annotated[
        OptionalIsoDate, Query(description="End of baseline window (YYYY-MM-DD)")
    ] = None,
    inference_method: str = Query(
        default="nuts",
        pattern="^(nuts|advi)$",
        description="Sampler for any node this analysis has to fit. `nuts` (default) is "
        "exact MCMC; `advi` is the mean-field approximation — faster, and reported with "
        "its PSIS k-hat so you can see how far off it is. Same values and same default as "
        "POST /analyze/{name} and POST /simulate.",
    ),
    run_id: Optional[str] = Query(
        default=None,
        description="Opaque client-generated id. Poll GET /progress/{run_id} for "
        "live stages while this request is in flight. Omit it and no progress is "
        "tracked at all.",
    ),
):
    tree = await _loaded_tree(request)
    _require_data(tree)
    parser = tree.parser
    data = tree.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    state = request.app.state
    # Registered before the lock, so a run queued behind another one on **this
    # tree** can say that rather than looking wedged. The lock is per-tree:
    # another tree's simulation is not something this run waits for.
    report = _progress_reporter(state, run_id, "waiting")
    try:
        async with tree.lock:
            if report:
                report({"stage": "resolving"})
            # run_rca adds any traces it fits on demand to this tree's cache.
            try:
                result = await asyncio.to_thread(
                    _guarded,
                    tree,
                    run_rca,
                    parser.dag,
                    data,
                    tree.traces,
                    name,
                    analysis_start=analysis_start,
                    analysis_end=analysis_end,
                    reference_start=reference_start,
                    reference_end=reference_end,
                    inference_method=inference_method,
                    progress=report,
                )
            # RuntimeError too (roadmap C38): grain-join and PyMC sampling
            # failures subclass it, and both are named diagnoses the caller
            # can act on, not server faults.
            except (ValueError, RuntimeError) as e:
                raise HTTPException(status_code=422, detail=str(e))
    finally:
        if run_id:
            state.progress.pop(run_id, None)

    return result


def _fetch_sliced_cached(tree, parser, metric, dimension_source, start, end) -> pd.DataFrame:
    """Read-through slice cache: one provider query per
    (metric, dimension, grain, window), reused across requests. The cache is
    the tree's own — two trees naming the same metric are two independent
    nodes, with independent fetches."""
    key = (metric.name, dimension_source, metric.grain, start, end)
    cached = tree.slice_cache.get(key)
    if cached is not None:
        return cached
    provider_type = parser.config.provider.type
    query_name = provider_query_name(provider_type, metric)
    df = tree.fetcher.fetch_metric_sliced(
        query_name,
        dimension_source,
        start,
        end,
        grain=metric.grain,
        kind=metric.kind,
    )
    tree.slice_cache[key] = df
    return df


def _fetch_flows_cached(
    tree, parser, metric, dimension_source, ref_start, ref_end, an_start, an_end
):
    """Read-through cache for the entity-flow transition matrix.

    Mirrors `_fetch_sliced_cached`, but keyed on both windows because a flow is
    a comparison between them rather than a series over one. Uncached, this was
    an extra FULL OUTER JOIN on every slice request — the same query, for the
    same windows, on every click.
    """
    key = (metric.name, dimension_source, ref_start, ref_end, an_start, an_end)
    cached = tree.flow_cache.get(key)
    if cached is not None:
        return cached
    df = tree.fetcher.fetch_entity_flows(
        provider_query_name(parser.config.provider.type, metric),
        dimension_source,
        ref_start,
        ref_end,
        an_start,
        an_end,
    )
    tree.flow_cache[key] = df
    return df


def _loaded_window(data) -> Tuple[str, str]:
    """The inclusive span the loaded data actually covers, as ISO dates.

    `date_start`/`date_end` are period *starts* (what /meta reports), so a
    month-grain tree's `date_end` is the 1st of its last month. The honest
    upper edge is the last date fully covered — `data_through`, the same anchor
    the node cards and the goal progress use — so a legitimate request for the
    end of the last month is not mistaken for one past the end of the data.
    """
    start = str(data.date_start.date())
    edges = [e for e in (data.data_through(n) for n in data.grain_of) if e is not None]
    end = max(edges) if edges else data.date_end
    return start, str(end.date())


def _require_window_loaded(data, span_start: str, span_end: str) -> None:
    """Reject a slice window that reaches outside the loaded data. Raises
    ValueError, which the endpoint turns into a 422.

    Called **before any provider call**: `_run_slice` fetches
    `min(reference_start, analysis_start) … max(reference_end, analysis_end)`,
    and nothing checked those dates beyond "they parse". A caller could ask for
    1900-01-01…2100-12-31 and get a 73,000-day warehouse scan, held under the
    tree's lock, whose frame then sat in the slice cache forever — even though
    the request went on to 422 for having no data in it. Checked here rather
    than in the endpoint because the reference window may be defaulted inside
    `_run_slice`: what matters is the span about to be fetched, not the span
    that was passed.
    """
    loaded_start, loaded_end = _loaded_window(data)
    if span_start < loaded_start or span_end > loaded_end:
        raise ValueError(
            f"Requested window {span_start}..{span_end} reaches outside the loaded "
            f"data window {loaded_start}..{loaded_end}. Slicing reads from the "
            "provider for the window you ask for, so it is restricted to the data "
            "this process loaded; restart with --start-date/--end-date covering "
            "the window you want."
        )


def _run_slice(
    tree,
    parser,
    data,
    defn,
    dimension,
    reference_start,
    reference_end,
    analysis_start,
    analysis_end,
):
    """Fetch the sliced frames (and the weight's, for a rate) and attribute.
    Sync — called via asyncio.to_thread under the tree's lock. The engine stays
    pure: all I/O happens here (slice_attribution keeps concrete dates, so
    omitted references resolve here too)."""
    reference_start, reference_end, reference_defaulted = resolve_reference_window(
        parser.dag,
        data,
        defn.name,
        analysis_start,
        analysis_end,
        reference_start,
        reference_end,
    )
    spec = defn.dimensions[dimension]
    span_start = min(reference_start, analysis_start)
    span_end = max(reference_end, analysis_end)
    _require_window_loaded(data, span_start, span_end)
    sliced = _fetch_sliced_cached(tree, parser, defn, spec.source, span_start, span_end)
    weight_sliced = None
    if defn.kind == "rate":
        weight_defn = parser.get_metric(spec.weight)
        # Backstop only: `Parser._validate_dimension_weights` refuses this at
        # parse time (C12), so a served tree cannot reach it. Kept for callers
        # who assemble a DAG without the parser.
        if weight_defn.grain != defn.grain:
            raise ValueError(
                f"Rate '{defn.name}' (grain '{defn.grain}') has weight "
                f"'{spec.weight}' at grain '{weight_defn.grain}'; sliced weights "
                "must share the rate's grain."
            )
        weight_sliced = _fetch_sliced_cached(
            tree, parser, weight_defn, spec.source, span_start, span_end
        )
    # Whether these slices are expected to sum comes from the binding, not from
    # the residual they produce — see `BaseDataFetcher.slice_additivity`.
    additivity = tree.fetcher.slice_additivity(
        provider_query_name(parser.config.provider.type, defn), spec.source
    )
    result = slice_attribution(
        defn,
        dimension,
        sliced,
        data.series(defn.name),
        reference_start,
        reference_end,
        analysis_start,
        analysis_end,
        weight_sliced=weight_sliced,
        additivity=additivity,
    )
    result["reference_defaulted"] = reference_defaulted

    # Entity flows are a *diagnostic alongside* the attribution, never a second
    # decomposition of the same gap: they compare window-level sets, which do
    # not reconcile to a window-mean gap. Best-effort — a provider that cannot
    # classify entities simply has none to add, and that is not an error.
    try:
        transitions = _fetch_flows_cached(
            tree,
            parser,
            defn,
            spec.source,
            reference_start,
            reference_end,
            analysis_start,
            analysis_end,
        )
        # Fold to the same slices the attribution shows. Two panels side by side
        # that disagree on which slices exist is a worse read than either alone.
        result["entity_flows"] = entity_flows(transitions, top_k=spec.top_k, pinned=spec.values)
    except SliceNotSupported:
        result["entity_flows"] = None
    except Exception as e:  # a flow query failing must not lose the attribution
        logger.warning("Entity flows unavailable for '%s' by '%s': %s", defn.name, dimension, e)
        result["entity_flows"] = None
    return result


@router.post("/rca/{name}/slices")
async def slice_metric_gap(
    name: str,
    request: Request,
    dimension: Annotated[str, Query(description="Declared dimension name on the metric")],
    analysis_start: Annotated[IsoDate, Query(description="Start of analysis window (YYYY-MM-DD)")],
    analysis_end: Annotated[IsoDate, Query(description="End of analysis window (YYYY-MM-DD)")],
    reference_start: Annotated[
        OptionalIsoDate,
        Query(
            description="Start of baseline window (YYYY-MM-DD); omit both reference "
            "dates to default to the matched adjacent block before the analysis window"
        ),
    ] = None,
    reference_end: Annotated[
        OptionalIsoDate, Query(description="End of baseline window (YYYY-MM-DD)")
    ] = None,
):
    """Attribute `name`'s window-over-window gap across one dimension's slices.

    The traverse-then-slice follow-up to POST /rca/{name}: tree RCA says which
    upstream metric moved; this says where inside it. When slicing a lagged
    parent, pass the parent's own lag-shifted windows (a defaulted reference
    matches the metric's own timeline, not a lag-shifted one).
    """
    tree = await _loaded_tree(request)
    _require_data(tree)
    parser = tree.parser
    data = tree.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    defn = parser.get_metric(name)
    if dimension not in defn.dimensions:
        raise HTTPException(
            status_code=422,
            detail=f"Metric '{name}' declares no dimension '{dimension}' "
            f"(declared: {sorted(defn.dimensions) or 'none'}).",
        )
    async with tree.lock:
        try:
            result = await asyncio.to_thread(
                _guarded,
                tree,
                _run_slice,
                tree,
                parser,
                data,
                defn,
                dimension,
                reference_start,
                reference_end,
                analysis_start,
                analysis_end,
            )
        except SliceNotSupported as e:
            raise HTTPException(status_code=422, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return result


@router.post("/simulate")
async def simulate(
    scenario: ScenarioRequest,
    request: Request,
    inference_method: str = Query(
        default="nuts",
        pattern="^(nuts|advi)$",
        description="Sampler for any node this scenario has to fit. `nuts` (default) is "
        "exact MCMC; `advi` is the mean-field approximation — faster, and reported with "
        "its PSIS k-hat so you can see how far off it is. Same values and same default as "
        "POST /analyze/{name} and POST /rca/{name}.",
    ),
    run_id: Optional[str] = Query(
        default=None,
        description="Opaque client-generated id. Poll GET /progress/{run_id} for "
        "live stages while this request is in flight.",
    ),
):
    tree = await _loaded_tree(request)
    _require_ready(tree)
    parser = tree.parser
    data = tree.data

    state = request.app.state
    report = _progress_reporter(state, run_id, "waiting")
    try:
        async with tree.lock:
            if report:
                report({"stage": "resolving"})
            # run_scenario adds any traces it fits on demand to this tree's cache.
            try:
                result = await asyncio.to_thread(
                    _guarded,
                    tree,
                    run_scenario,
                    parser.dag,
                    data,
                    tree.traces,
                    scenario,
                    inference_method=inference_method,
                    progress=report,
                )
            # RuntimeError too (roadmap C38): grain-join and PyMC sampling
            # failures subclass it, and both are named diagnoses the caller
            # can act on, not server faults.
            except (ValueError, RuntimeError) as e:
                raise HTTPException(status_code=422, detail=str(e))
    finally:
        if run_id:
            state.progress.pop(run_id, None)

    return result


@app.get("/progress/{run_id}")
async def get_progress(run_id: str, request: Request):
    """Live stage of an in-flight RCA or simulation started with this `run_id`.

    Deliberately cheap: no lock (the analysis holds it for its whole duration,
    so taking it here would deadlock the very thing being reported on) and no
    readiness check. It is a data route like any other for the bearer-token
    gate, so under BREAKDOWN_REQUIRE_AUTH a poller carries the same header the
    request it is polling for did. An unknown id is `{"stage": null}` with a 200 rather
    than a 404 — a finished run and a never-started one are the same answer to a
    poller, and neither is an error worth handling on the client.
    """
    return request.app.state.progress.get(run_id) or {"stage": None}


# Every data route is registered twice from this one router: bare (the default
# tree) and under `/trees/{tree_id}`. Handlers never see the id — `_tree()`
# reads it off `request.path_params` — so there is exactly one implementation
# of each endpoint and the aliases cannot drift from the routes they alias.
# Registered here, at the bottom, because `include_router` copies whatever the
# router holds at call time.
app.include_router(router)
app.include_router(
    router,
    prefix="/trees/{tree_id}",
    generate_unique_id_function=lambda route: f"tree_{route.name}",
)

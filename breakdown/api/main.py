import asyncio
import datetime
import hmac
import logging
import math
import os
import threading
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from breakdown.data_fetch import (
    CloudDataFetcher,
    LocalDataFetcher,
    MockDataFetcher,
    SliceNotSupported,
    WarehouseDataFetcher,
    provider_query_name,
)
from breakdown.engine.model import fit_metric, summarize_trace, warm_inference_imports
from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.engine.simulate import ScenarioRequest, run_scenario, validate_cold_start
from breakdown.engine.slices import entity_flows, slice_attribution
from breakdown.grains import GrainedData, build_grained
from breakdown.mcp.server import mcp
from breakdown.parser import Parser
from breakdown.snapshots import SnapshotFetcher, SnapshotStore

logger = logging.getLogger(__name__)

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


def _build_fetcher(provider_cfg, dag, metrics=None):
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

        overrides = {m.source.split(".")[-1]: m.bind for m in (metrics or []) if m.bind}
        return fetcher_from_project(
            provider_cfg.project_path,
            target=provider_cfg.target,
            profiles_dir=provider_cfg.profiles_dir,
            overrides=overrides,
        )
    if provider_cfg.type == "warehouse":
        metric_sql = {m.name: m.sql for m in (metrics or []) if m.sql}
        missing = [m.name for m in (metrics or []) if not m.sql]
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


def _wrap_snapshots(fetcher, provider_type: str, tree_path: str, slice_span=None):
    """Wrap the fetcher in the snapshot read-through cache (roadmap 2.4).

    Mock data is already deterministic and free, so only real providers are
    cached. Default directory is tree-adjacent (`.breakdown/snapshots`) so a
    partner repo can commit its snapshots and re-run RCAs from a fresh clone;
    BREAKDOWN_SNAPSHOT_DIR overrides, "off" disables, BREAKDOWN_REFRESH=1
    forces one refetch pass.

    `slice_span` is the loaded data window. Sliced fetches are widened to it
    before being stored, so one snapshot per (metric, dimension) serves every
    analysis window rather than only the ones already run."""
    if provider_type == "mock":
        return fetcher
    snapshot_dir = os.environ.get("BREAKDOWN_SNAPSHOT_DIR", "")
    if snapshot_dir == "off":
        return fetcher
    if not snapshot_dir:
        snapshot_dir = os.path.join(os.path.dirname(tree_path), ".breakdown", "snapshots")
    return SnapshotFetcher(
        fetcher,
        SnapshotStore(snapshot_dir),
        refresh=os.environ.get("BREAKDOWN_REFRESH") == "1",
        slice_span=slice_span,
    )


def _fetch_all_metrics(parser, fetcher, provider_type, start_date, end_date) -> GrainedData:
    """Fetch every metric at its native grain and assemble per-grain frames
    (metrics inner-join on date only against series at the same grain)."""
    per_metric: Dict[str, pd.DataFrame] = {}
    grain_of: Dict[str, str] = {}
    kind_of: Dict[str, str] = {}
    for metric in parser.config.metrics:
        query_name = provider_query_name(provider_type, metric)
        df = fetcher.fetch_metric(
            query_name, start_date, end_date, grain=metric.grain, kind=metric.kind
        )
        df = df.rename(columns={query_name: metric.name})
        per_metric[metric.name] = df[["date", metric.name]]
        grain_of[metric.name] = metric.grain
        kind_of[metric.name] = metric.kind

    return build_grained(per_metric, grain_of, kind_of)


def _validate_date(value: str, label: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise RuntimeError(f"{label} must be a valid YYYY-MM-DD date, got '{value}'")
    return value


def _require_ready(request: Request) -> None:
    """503 on data endpoints while the app is serving degraded (startup
    data load failed); the detail carries the original error."""
    error = request.app.state.startup_error
    if error is not None:
        raise HTTPException(
            status_code=503,
            detail=f"breakdown started without data: {error}. "
            "Run `breakdown doctor --tree <tree.yml>` to diagnose.",
        )


def _require_data(request: Request) -> None:
    """422 on time-series endpoints for a cold-start tree (`provider: none`).
    A stated mode, not an error: the tree deliberately has no data, so
    analyses that consume history cannot exist — only /simulate can."""
    _require_ready(request)
    if request.app.state.data is None:
        raise HTTPException(
            status_code=422,
            detail="This tree declares no data provider (cold start mode); "
            "this endpoint needs time-series data. What-if simulation over "
            "the declared beliefs is available at POST /simulate.",
        )


# Cap on `app.state.traces`. Each entry is an InferenceData object holding
# every posterior draw, so an unbounded cache grows with distinct
# (metric, analysis_start) pairs until the process is OOM-killed — reachable
# without malice on the public demo, where each visitor picks their own windows
# (C8). Insertion-ordered eviction: dicts preserve order, so the oldest key is
# the first, and a refit re-inserts at the end. Generous enough that a normal
# session never evicts; a fit that is dropped is simply recomputed.
MAX_CACHED_TRACES = 256


def _remember_fit(traces: Dict[Tuple[str, Optional[str]], Any], key, fit) -> None:
    """Publish a fit into the shared cache, bounded and never downgrading.

    Two viewers share one process and one cache, so a fit that one of them
    requests is a fit the other may be shown. `/analyze` exposes
    `inference_method` and `draws` while the cache key carries neither, so a
    cheap 50-draw ADVI run would otherwise silently replace a NUTS fit that a
    previous RCA had already paid for. Ordering fits by quality — NUTS over
    ADVI, then by draw count — keeps the deliberate "confirm this with NUTS"
    upgrade working while blocking the accidental downgrade (C8).
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    tree_path = os.environ.get("BREAKDOWN_TREE", DEFAULT_TREE_PATH)
    app.state.parser = None
    app.state.fetcher = None
    app.state.data = None
    # A startup failure (bad tree, unset ${VAR}, unreachable warehouse) must
    # not kill the process — a container would crash-loop with no way to see
    # why. Serve degraded instead: /health carries the error, data endpoints
    # return 503, and the UI shows a banner pointing at `breakdown doctor`.
    app.state.startup_error = None
    # Keyed by (metric name, fit_end): a full-window fit (fit_end=None) and a
    # pre-anomaly RCA fit are different objects and must not shadow each other.
    app.state.traces: Dict[Tuple[str, Optional[str]], Any] = {}
    # Sliced frames fetched on demand for slice attribution, keyed by
    # (metric, dimension source, grain, start, end). Deliberately separate
    # from the startup GrainedData — slices never enter the fit path.
    app.state.slice_cache: Dict[Tuple[str, str, str, str, str], pd.DataFrame] = {}
    # Flows are keyed by a *pair* of windows, so they cannot share the slice
    # cache's key. Without one, every click on a ranked cause re-ran a FULL
    # OUTER JOIN over two windows on the warehouse.
    app.state.flow_cache: Dict[Tuple[str, str, str, str, str, str], Any] = {}
    app.state.lock = asyncio.Lock()

    try:
        with open(tree_path, "r") as f:
            yaml_config = f.read()

        parser = Parser(yaml_config)
        provider_cfg = parser.config.provider
        if provider_cfg.type == "none":
            # Cold-start tree: nothing is fetched, app.state.data stays None
            # — a stated mode, not a degraded startup. Missing declarations
            # would otherwise surface one 422 at a time on /simulate, so
            # check readiness here and fail loudly with the full list.
            problems = validate_cold_start(parser.dag)
            if problems:
                raise RuntimeError(
                    "tree declares no data provider but is not cold-start "
                    "ready: " + "; ".join(problems)
                )
            app.state.parser = parser
            logger.info(
                "breakdown API started (cold start): tree=%s metrics=%d — "
                "no data provider, serving what-if over declared beliefs",
                tree_path,
                len(parser.config.metrics),
            )
        else:
            start_date = _validate_date(
                os.environ.get("BREAKDOWN_START_DATE", DEFAULT_START_DATE), "start date"
            )
            end_date = _validate_date(
                os.environ.get("BREAKDOWN_END_DATE", DEFAULT_END_DATE), "end date"
            )
            if end_date < start_date:
                raise RuntimeError(f"end date '{end_date}' is before start date '{start_date}'")

            fetcher = _build_fetcher(provider_cfg, parser.dag, parser.config.metrics)
            fetcher = _wrap_snapshots(
                fetcher, provider_cfg.type, tree_path, slice_span=(start_date, end_date)
            )
            data = _fetch_all_metrics(parser, fetcher, provider_cfg.type, start_date, end_date)

            app.state.parser = parser
            app.state.fetcher = fetcher
            app.state.data = data

            logger.info(
                "breakdown API started: tree=%s provider=%s window=[%s, %s] rows=%s",
                tree_path,
                provider_cfg.type,
                start_date,
                end_date,
                ", ".join(f"{g}:{len(f)}" for g, f in data.frames.items()),
            )
    except Exception as e:
        app.state.startup_error = f"{type(e).__name__}: {e}"
        logger.error(
            "Startup data load failed for tree=%s; serving degraded. "
            "Run `breakdown doctor --tree %s` to diagnose. %s",
            tree_path,
            tree_path,
            e,
        )
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
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="breakdown API", lifespan=lifespan)


@app.middleware("http")
async def mcp_bearer_token(request: Request, call_next):
    """Require a bearer token on /mcp when BREAKDOWN_API_TOKEN is set.

    The MCP endpoint runs whole analyses, so exposing it off loopback without
    a gate hands anyone who finds the URL the tree and its data. Opt-in rather
    than mandatory: unset (the laptop default) keeps the loopback workflow
    friction-free, set (a public deployment) closes it. The UI and /health stay
    open — the token is for the machine-facing surface, not a login.

    A down payment on hosted mode (roadmap 3.5), not a substitute for it: one
    shared secret, no per-user identity, no revocation short of a redeploy."""
    token = os.environ.get("BREAKDOWN_API_TOKEN")
    if token and request.url.path.startswith("/mcp"):
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        # compare_digest over the raw strings: constant-time, and it also
        # keeps a missing header from short-circuiting differently.
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented, token):
            return JSONResponse(
                {"detail": "Missing or invalid bearer token for /mcp."},
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


@app.get("/health")
async def health(request: Request):
    """Liveness + readiness in one: always 200 (the process is up), with
    `status` distinguishing ok from degraded so orchestrators and the UI can
    react without treating a bad data source as a dead container."""
    state = request.app.state
    if state.startup_error is not None:
        return {"status": "degraded", "error": state.startup_error}
    return {
        "status": "ok",
        "provider": state.parser.config.provider.type,
        "metrics": len(state.parser.config.metrics),
    }


@app.get("/meta")
async def get_meta(request: Request):
    """Bootstrap info for the UI: metrics, data window, provider, fit status.
    `mode` tells the UI which surface to boot: "fitted" (data-backed) or
    "cold_start" (no data provider — what-if over declared beliefs only)."""
    _require_ready(request)
    parser = request.app.state.parser
    data = request.app.state.data
    if data is None:
        return {
            "mode": "cold_start",
            "provider": parser.config.provider.type,
            "metrics": [m.name for m in parser.config.metrics],
            "date_start": None,
            "date_end": None,
            "grains": {m.name: m.grain for m in parser.config.metrics},
            "kinds": {m.name: m.kind for m in parser.config.metrics},
            "data_through": {},
            "fitted": [],
        }
    data_through = {}
    for name in data.grain_of:
        through = data.data_through(name)
        if through is not None:
            data_through[name] = str(through.date())
    return {
        "mode": "fitted",
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
        # `list(...)` snapshots the keys in one bytecode op rather than
        # iterating lazily: `run_rca` mutates this dict from a worker thread
        # (it is handed the cache directly and fits on demand), so a lazy
        # comprehension here raced it into "dictionary changed size during
        # iteration" — an intermittent 500 for one viewer precisely while
        # another's analysis ran, which is the single most likely way a
        # multi-viewer demo breaks (C8).
        "fitted": sorted({name for (name, _) in list(request.app.state.traces)}),
    }


@app.get("/dag")
async def get_dag(request: Request):
    _require_ready(request)
    parser = request.app.state.parser
    return {
        "nodes": [
            [name, attrs["definition"].model_dump()] for name, attrs in parser.dag.nodes(data=True)
        ],
        "edges": [list(e) for e in parser.dag.edges()],
    }


@app.get("/series")
async def get_series(request: Request):
    """Every metric's series at its native grain, for the node cards. Mixed
    grains mean there is no single shared date axis: each metric carries its
    own period-start dates. NaN -> null for valid JSON."""
    _require_data(request)
    parser = request.app.state.parser
    data = request.app.state.data
    metrics = {}
    for m in parser.config.metrics:
        if m.name not in data.grain_of:
            continue
        s = data.series(m.name)
        metrics[m.name] = {
            "grain": data.grain_of[m.name],
            "dates": [str(d.date()) for d in s["date"]],
            "values": [
                None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
                for v in s[m.name].tolist()
            ],
        }
    return {"metrics": metrics}


@app.get("/metrics/{name}/query")
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
    """
    # Degraded startup leaves no parser, so this needs the same 503 the data
    # endpoints give rather than an AttributeError. Provenance is *more* useful
    # when things are broken, but it still needs a tree that loaded.
    _require_ready(request)
    parser = request.app.state.parser
    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    provider = parser.config.provider.type
    fetcher = getattr(request.app.state, "fetcher", None)
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
    data = request.app.state.data
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


@app.get("/metrics/{name}")
async def get_metric(name: str, request: Request):
    _require_ready(request)
    parser = request.app.state.parser
    data = request.app.state.data
    traces = request.app.state.traces

    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    if data is None:
        # Cold-start tree: the definition (with its asserted baseline and
        # plausible band) is the whole story — there is no series to show.
        time_series = []
    else:
        try:
            time_series = data.series(name).to_dict(orient="records")
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No data found for metric '{name}'")

    summary = None
    diagnostics = None
    fit = _pick_fit(traces, name)
    if fit is not None:
        # NaN/inf (e.g. r_hat on single-chain ADVI traces) are not valid JSON
        summary = {
            col: {k: (float(v) if math.isfinite(v) else None) for k, v in vals.items()}
            for col, vals in summarize_trace(fit.trace).to_dict().items()
        }
        diagnostics = fit.diagnostics

    return {
        "definition": metric.model_dump(),
        "time_series": time_series,
        "summary": summary,
        "diagnostics": diagnostics,
    }


@app.post("/analyze/{name}")
async def analyze_metric(
    name: str,
    request: Request,
    inference_method: str = Query(default="nuts", pattern="^(nuts|advi)$"),
    draws: int = Query(default=500, ge=50, le=5000),
    tune: int = Query(default=500, ge=50, le=5000),
    chains: int = Query(default=4, ge=1, le=8),
    fit_end: Optional[str] = Query(default=None),
):
    _require_data(request)
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    if fit_end is not None:
        # Lets the "confirm with NUTS" workflow reproduce exactly what RCA fitted.
        try:
            datetime.date.fromisoformat(fit_end)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"fit_end must be YYYY-MM-DD, got '{fit_end}'"
            )

    async with request.app.state.lock:
        fit = await asyncio.to_thread(
            fit_metric,
            parser.dag,
            data,
            name,
            draws=draws,
            tune=tune,
            inference_method=inference_method,
            chains=chains,
            fit_end=fit_end,
        )
        _remember_fit(request.app.state.traces, (name, fit_end), fit)

    return {
        "status": "success",
        "message": f"Analysis complete for '{name}'",
        "inference_method": inference_method,
        "diagnostics": fit.diagnostics,
    }


@app.get("/shapley/{name}")
async def get_shapley(
    name: str,
    request: Request,
    reference_start: str = Query(..., description="Start of baseline window (YYYY-MM-DD)"),
    reference_end: str = Query(..., description="End of baseline window (YYYY-MM-DD)"),
    analysis_start: str = Query(..., description="Start of analysis window (YYYY-MM-DD)"),
    analysis_end: str = Query(..., description="End of analysis window (YYYY-MM-DD)"),
):
    _require_data(request)
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    try:
        result = shapley_attribution(
            parser.dag,
            data,
            name,
            reference_start=reference_start,
            reference_end=reference_end,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return result


@app.post("/rca/{name}")
async def root_cause_analysis(
    name: str,
    request: Request,
    reference_start: str = Query(..., description="Start of baseline window (YYYY-MM-DD)"),
    reference_end: str = Query(..., description="End of baseline window (YYYY-MM-DD)"),
    analysis_start: str = Query(..., description="Start of analysis window (YYYY-MM-DD)"),
    analysis_end: str = Query(..., description="End of analysis window (YYYY-MM-DD)"),
):
    _require_data(request)
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    async with request.app.state.lock:
        # run_rca adds any traces it fits on demand to app.state.traces itself.
        try:
            result = await asyncio.to_thread(
                run_rca, parser.dag, data, request.app.state.traces, name,
                analysis_start=analysis_start, analysis_end=analysis_end,
                reference_start=reference_start, reference_end=reference_end,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return result


def _fetch_sliced_cached(state, parser, metric, dimension_source, start, end) -> pd.DataFrame:
    """Read-through slice cache: one provider query per
    (metric, dimension, grain, window), reused across requests."""
    key = (metric.name, dimension_source, metric.grain, start, end)
    cached = state.slice_cache.get(key)
    if cached is not None:
        return cached
    provider_type = parser.config.provider.type
    query_name = provider_query_name(provider_type, metric)
    df = state.fetcher.fetch_metric_sliced(
        query_name,
        dimension_source,
        start,
        end,
        grain=metric.grain,
        kind=metric.kind,
    )
    state.slice_cache[key] = df
    return df


def _fetch_flows_cached(
    state, parser, metric, dimension_source, ref_start, ref_end, an_start, an_end
):
    """Read-through cache for the entity-flow transition matrix.

    Mirrors `_fetch_sliced_cached`, but keyed on both windows because a flow is
    a comparison between them rather than a series over one. Uncached, this was
    an extra FULL OUTER JOIN on every slice request — the same query, for the
    same windows, on every click.
    """
    key = (metric.name, dimension_source, ref_start, ref_end, an_start, an_end)
    cached = state.flow_cache.get(key)
    if cached is not None:
        return cached
    df = state.fetcher.fetch_entity_flows(
        provider_query_name(parser.config.provider.type, metric),
        dimension_source,
        ref_start,
        ref_end,
        an_start,
        an_end,
    )
    state.flow_cache[key] = df
    return df


def _run_slice(
    state,
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
    Sync — called via asyncio.to_thread under the app lock. The engine stays
    pure: all I/O happens here."""
    spec = defn.dimensions[dimension]
    span_start = min(reference_start, analysis_start)
    span_end = max(reference_end, analysis_end)
    sliced = _fetch_sliced_cached(state, parser, defn, spec.source, span_start, span_end)
    weight_sliced = None
    if defn.kind == "rate":
        weight_defn = parser.get_metric(spec.weight)
        if weight_defn.grain != defn.grain:
            raise ValueError(
                f"Rate '{defn.name}' (grain '{defn.grain}') has weight "
                f"'{spec.weight}' at grain '{weight_defn.grain}'; sliced weights "
                "must share the rate's grain."
            )
        weight_sliced = _fetch_sliced_cached(
            state, parser, weight_defn, spec.source, span_start, span_end
        )
    # Whether these slices are expected to sum comes from the binding, not from
    # the residual they produce — see `BaseDataFetcher.slice_additivity`.
    additivity = state.fetcher.slice_additivity(
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

    # Entity flows are a *diagnostic alongside* the attribution, never a second
    # decomposition of the same gap: they compare window-level sets, which do
    # not reconcile to a window-mean gap. Best-effort — a provider that cannot
    # classify entities simply has none to add, and that is not an error.
    try:
        transitions = _fetch_flows_cached(
            state,
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
        result["entity_flows"] = entity_flows(
            transitions, top_k=spec.top_k, pinned=spec.values
        )
    except SliceNotSupported:
        result["entity_flows"] = None
    except Exception as e:  # a flow query failing must not lose the attribution
        logger.warning("Entity flows unavailable for '%s' by '%s': %s", defn.name, dimension, e)
        result["entity_flows"] = None
    return result


@app.post("/rca/{name}/slices")
async def slice_metric_gap(
    name: str,
    request: Request,
    dimension: str = Query(..., description="Declared dimension name on the metric"),
    reference_start: str = Query(..., description="Start of baseline window (YYYY-MM-DD)"),
    reference_end: str = Query(..., description="End of baseline window (YYYY-MM-DD)"),
    analysis_start: str = Query(..., description="Start of analysis window (YYYY-MM-DD)"),
    analysis_end: str = Query(..., description="End of analysis window (YYYY-MM-DD)"),
):
    """Attribute `name`'s window-over-window gap across one dimension's slices.

    The traverse-then-slice follow-up to POST /rca/{name}: tree RCA says which
    upstream metric moved; this says where inside it. When slicing a lagged
    parent, pass the parent's own lag-shifted windows.
    """
    _require_data(request)
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    defn = parser.get_metric(name)
    if dimension not in defn.dimensions:
        raise HTTPException(
            status_code=422,
            detail=f"Metric '{name}' declares no dimension '{dimension}' "
            f"(declared: {sorted(defn.dimensions) or 'none'}).",
        )
    for label, value in [
        ("reference_start", reference_start),
        ("reference_end", reference_end),
        ("analysis_start", analysis_start),
        ("analysis_end", analysis_end),
    ]:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"{label} must be YYYY-MM-DD, got '{value}'"
            )

    async with request.app.state.lock:
        try:
            result = await asyncio.to_thread(
                _run_slice,
                request.app.state,
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


@app.post("/simulate")
async def simulate(scenario: ScenarioRequest, request: Request):
    """Steady-state what-if simulation. Stateless: the client owns the
    scenario; on-demand fits land in the shared trace cache."""
    _require_ready(request)
    parser = request.app.state.parser
    data = request.app.state.data

    async with request.app.state.lock:
        # run_scenario adds any traces it fits on demand to app.state.traces.
        try:
            result = await asyncio.to_thread(
                run_scenario,
                parser.dag,
                data,
                request.app.state.traces,
                scenario,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return result

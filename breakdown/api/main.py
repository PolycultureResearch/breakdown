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
)
from breakdown.engine.model import fit_metric, summarize_trace, warm_inference_imports
from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.engine.simulate import ScenarioRequest, run_scenario, validate_cold_start
from breakdown.engine.slices import slice_attribution
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


def _build_fetcher(provider_cfg, dag, metrics=None):
    if provider_cfg.type == "local":
        return LocalDataFetcher(project_path=provider_cfg.project_path)
    if provider_cfg.type == "cloud":
        return CloudDataFetcher(
            environment_id=provider_cfg.environment_id,
            host=provider_cfg.host,
            token=provider_cfg.token,
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
        # local/cloud providers query the semantic layer by the last segment
        # of `source`; mock and warehouse providers key off the tree name
        # directly (warehouse resolves it to per-metric SQL).
        if provider_type in ("mock", "warehouse"):
            query_name = metric.name
        else:
            query_name = metric.source.split(".")[-1]
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


def _pick_fit(traces: Dict[Tuple[str, Optional[str]], Any], name: str):
    """Best cached fit to summarize for a metric: prefer the full-window fit,
    else the one with the latest fit_end, else None."""
    if (name, None) in traces:
        return traces[(name, None)]
    dated = [(fit_end, fit) for (n, fit_end), fit in traces.items() if n == name]
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
            streamable_http_path="/", stateless_http=True, json_response=True,
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
                tree_path, len(parser.config.metrics),
            )
        else:
            start_date = _validate_date(
                os.environ.get("BREAKDOWN_START_DATE", DEFAULT_START_DATE), "start date"
            )
            end_date = _validate_date(
                os.environ.get("BREAKDOWN_END_DATE", DEFAULT_END_DATE), "end date"
            )
            if end_date < start_date:
                raise RuntimeError(
                    f"end date '{end_date}' is before start date '{start_date}'"
                )

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
                tree_path, provider_cfg.type, start_date, end_date,
                ", ".join(f"{g}:{len(f)}" for g, f in data.frames.items()),
            )
    except Exception as e:
        app.state.startup_error = f"{type(e).__name__}: {e}"
        logger.error(
            "Startup data load failed for tree=%s; serving degraded. "
            "Run `breakdown doctor --tree %s` to diagnose. %s",
            tree_path, tree_path, e,
        )
    # PyMC/ArviZ/PyTensor are deferred out of `engine.model`'s module scope so
    # the port binds without paying for them (~27s on a shared-CPU VM, which is
    # what made Fly's proxy 503 the first visitor after an idle period). That
    # only moves the cost unless someone absorbs it, so absorb it here: a
    # daemon thread, started after the data load, importing while the operator
    # is still looking at the page. `fit_metric` re-imports from `sys.modules`
    # either way, so a slow or failed warm-up costs correctness nothing.
    threading.Thread(
        target=warm_inference_imports, name="warm-inference", daemon=True
    ).start()

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
        "fitted": sorted({name for (name, _) in request.app.state.traces}),
    }


@app.get("/dag")
async def get_dag(request: Request):
    _require_ready(request)
    parser = request.app.state.parser
    return {
        "nodes": [
            [name, attrs["definition"].model_dump()]
            for name, attrs in parser.dag.nodes(data=True)
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
            raise HTTPException(status_code=422, detail=f"fit_end must be YYYY-MM-DD, got '{fit_end}'")

    async with request.app.state.lock:
        fit = await asyncio.to_thread(
            fit_metric, parser.dag, data, name,
            draws=draws, tune=tune, inference_method=inference_method,
            chains=chains, fit_end=fit_end,
        )
        request.app.state.traces[(name, fit_end)] = fit

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
            parser.dag, data, name,
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
                reference_start, reference_end,
                analysis_start, analysis_end,
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
    query_name = (
        metric.name
        if provider_type in ("mock", "warehouse")
        else metric.source.split(".")[-1]
    )
    df = state.fetcher.fetch_metric_sliced(
        query_name, dimension_source, start, end,
        grain=metric.grain, kind=metric.kind,
    )
    state.slice_cache[key] = df
    return df


def _run_slice(
    state, parser, data, defn, dimension,
    reference_start, reference_end, analysis_start, analysis_end,
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
    return slice_attribution(
        defn, dimension, sliced, data.series(defn.name),
        reference_start, reference_end, analysis_start, analysis_end,
        weight_sliced=weight_sliced,
    )


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
        ("reference_start", reference_start), ("reference_end", reference_end),
        ("analysis_start", analysis_start), ("analysis_end", analysis_end),
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
                _run_slice, request.app.state, parser, data, defn, dimension,
                reference_start, reference_end, analysis_start, analysis_end,
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
                run_scenario, parser.dag, data, request.app.state.traces, scenario,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return result

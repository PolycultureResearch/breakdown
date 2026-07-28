import asyncio
import datetime
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from breakdown.data_fetch import (
    CloudDataFetcher,
    LocalDataFetcher,
    MockDataFetcher,
    WarehouseDataFetcher,
)
from breakdown.engine.model import fit_metric, summarize_trace
from breakdown.engine.rca import run_rca, shapley_attribution
from breakdown.engine.simulate import ScenarioRequest, run_scenario
from breakdown.grains import GrainedData, build_grained
from breakdown.parser import Parser

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TREE_PATH = os.path.join(BASE_DIR, "examples", "jaffle_shop_tree.yml")
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


def _pick_fit(traces: Dict[Tuple[str, Optional[str]], Any], name: str):
    """Best cached fit to summarize for a metric: prefer the full-window fit,
    else the one with the latest fit_end, else None."""
    if (name, None) in traces:
        return traces[(name, None)]
    dated = [(fit_end, fit) for (n, fit_end), fit in traces.items() if n == name]
    if not dated:
        return None
    return max(dated, key=lambda item: item[0])[1]


@asynccontextmanager
async def lifespan(app: FastAPI):
    tree_path = os.environ.get("BREAKDOWN_TREE", DEFAULT_TREE_PATH)
    start_date = _validate_date(
        os.environ.get("BREAKDOWN_START_DATE", DEFAULT_START_DATE), "start date"
    )
    end_date = _validate_date(
        os.environ.get("BREAKDOWN_END_DATE", DEFAULT_END_DATE), "end date"
    )
    if end_date < start_date:
        raise RuntimeError(f"end date '{end_date}' is before start date '{start_date}'")

    with open(tree_path, "r") as f:
        yaml_config = f.read()

    parser = Parser(yaml_config)
    provider_cfg = parser.config.provider
    fetcher = _build_fetcher(provider_cfg, parser.dag, parser.config.metrics)
    data = _fetch_all_metrics(parser, fetcher, provider_cfg.type, start_date, end_date)

    app.state.parser = parser
    app.state.fetcher = fetcher
    app.state.data = data
    # Keyed by (metric name, fit_end): a full-window fit (fit_end=None) and a
    # pre-anomaly RCA fit are different objects and must not shadow each other.
    app.state.traces: Dict[Tuple[str, Optional[str]], Any] = {}
    app.state.lock = asyncio.Lock()

    logger.info(
        "breakdown API started: tree=%s provider=%s window=[%s, %s] rows=%s",
        tree_path, provider_cfg.type, start_date, end_date,
        ", ".join(f"{g}:{len(f)}" for g, f in data.frames.items()),
    )
    yield


app = FastAPI(title="breakdown API", lifespan=lifespan)

static_dir = os.path.join(BASE_DIR, "static")
app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")


@app.get("/")
async def root():
    return {"message": "breakdown API is running. Visit /ui for the visualization."}


@app.get("/meta")
async def get_meta(request: Request):
    """Bootstrap info for the UI: metrics, data window, provider, fit status."""
    parser = request.app.state.parser
    data = request.app.state.data
    data_through = {}
    for name in data.grain_of:
        through = data.data_through(name)
        if through is not None:
            data_through[name] = str(through.date())
    return {
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
    parser = request.app.state.parser
    data = request.app.state.data
    traces = request.app.state.traces

    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    try:
        time_series = data.series(name).to_dict(orient="records")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No data found for metric '{name}'")

    summary = None
    fit = _pick_fit(traces, name)
    if fit is not None:
        # NaN/inf (e.g. r_hat on single-chain ADVI traces) are not valid JSON
        summary = {
            col: {k: (float(v) if math.isfinite(v) else None) for k, v in vals.items()}
            for col, vals in summarize_trace(fit.trace).to_dict().items()
        }

    return {
        "definition": metric.model_dump(),
        "time_series": time_series,
        "summary": summary,
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


@app.post("/simulate")
async def simulate(scenario: ScenarioRequest, request: Request):
    """Steady-state what-if simulation. Stateless: the client owns the
    scenario; on-demand fits land in the shared trace cache."""
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

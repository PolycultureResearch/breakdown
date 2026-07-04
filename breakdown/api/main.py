import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder
from breakdown.data_fetch import MockDataFetcher, LocalDataFetcher, CloudDataFetcher, generate_mock_data

logger = logging.getLogger(__name__)

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "examples",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    jaffle_shop_path = os.path.join(EXAMPLES_DIR, "jaffle_shop_tree.yml")
    with open(jaffle_shop_path, "r") as f:
        yaml_config = f.read()

    parser = Parser(yaml_config)
    provider_cfg = parser.config.provider

    if provider_cfg.type == "local":
        fetcher = LocalDataFetcher(project_path=provider_cfg.project_path)
    elif provider_cfg.type == "cloud":
        fetcher = CloudDataFetcher(
            environment_id=provider_cfg.environment_id,
            host=provider_cfg.host,
            token=provider_cfg.token,
        )
    else:
        fetcher = MockDataFetcher()

    data = generate_mock_data(n_days=100)

    app.state.parser = parser
    app.state.fetcher = fetcher
    app.state.data = data
    app.state.traces: Dict[str, Any] = {}
    app.state.lock = asyncio.Lock()

    logger.info("breakdown API started with provider=%s", provider_cfg.type)
    yield


app = FastAPI(title="breakdown API", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
static_dir = os.path.join(BASE_DIR, "static")
app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")


@app.get("/")
async def root():
    return {"message": "breakdown API is running. Visit /ui for the visualization."}


@app.get("/dag")
async def get_dag(request: Request):
    parser = request.app.state.parser
    return {
        "nodes": [n for n in parser.dag.nodes(data=True)],
        "edges": [list(e) for e in parser.dag.edges()],
    }


@app.get("/metrics/{name}")
async def get_metric(name: str, request: Request):
    parser = request.app.state.parser
    data = request.app.state.data
    traces = request.app.state.traces

    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    try:
        time_series = data[["date", name]].to_dict(orient="records")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No data found for metric '{name}'")

    summary = None
    if name in traces:
        builder = ModelBuilder(parser.dag, data)
        builder.traces[name] = traces[name]
        summary = builder.get_summary(name).to_dict()

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
):
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    async with request.app.state.lock:
        builder = ModelBuilder(parser.dag, data)
        builder.build_and_sample(name, draws=draws, tune=tune, inference_method=inference_method)
        request.app.state.traces[name] = builder.traces[name]

    return {
        "status": "success",
        "message": f"Analysis complete for '{name}'",
        "inference_method": inference_method,
    }


@app.get("/shapley/{name}")
async def shapley_attribution(
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

    metric = parser.get_metric(name)
    if not metric or not metric.formula:
        raise HTTPException(
            status_code=422,
            detail=f"Metric '{name}' has no formula — Shapley attribution requires a formula definition.",
        )

    builder = ModelBuilder(parser.dag, data)
    try:
        result = builder.compute_shapley(
            target_metric_name=name,
            reference_start=reference_start,
            reference_end=reference_end,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return result

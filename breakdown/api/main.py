import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder
from breakdown.engine.rca import run_rca
from breakdown.data_fetch import MockDataFetcher, LocalDataFetcher, CloudDataFetcher

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TREE_PATH = os.path.join(BASE_DIR, "examples", "jaffle_shop_tree.yml")
DEFAULT_START_DATE = "2024-01-01"
DEFAULT_END_DATE = "2024-04-09"


def _build_fetcher(provider_cfg, dag):
    if provider_cfg.type == "local":
        return LocalDataFetcher(project_path=provider_cfg.project_path)
    if provider_cfg.type == "cloud":
        return CloudDataFetcher(
            environment_id=provider_cfg.environment_id,
            host=provider_cfg.host,
            token=provider_cfg.token,
        )
    return MockDataFetcher(dag=dag)


def _fetch_all_metrics(parser, fetcher, provider_type, start_date, end_date):
    """Fetch every metric in the tree and align them on date (inner join)."""
    frames = []
    for metric in parser.config.metrics:
        # local/cloud providers query the semantic layer by the last segment
        # of `source`; the mock provider generates by tree name directly.
        if provider_type == "mock":
            query_name = metric.name
        else:
            query_name = metric.source.split(".")[-1]
        df = fetcher.fetch_metric(query_name, start_date, end_date)
        df = df.rename(columns={query_name: metric.name})
        frames.append(df.set_index("date")[[metric.name]])

    data = pd.concat(frames, axis=1, join="inner").reset_index()
    if data.empty:
        raise RuntimeError(
            f"No overlapping dates across metrics in window [{start_date}, {end_date}]"
        )
    return data


@asynccontextmanager
async def lifespan(app: FastAPI):
    tree_path = os.environ.get("BREAKDOWN_TREE", DEFAULT_TREE_PATH)
    start_date = os.environ.get("BREAKDOWN_START_DATE", DEFAULT_START_DATE)
    end_date = os.environ.get("BREAKDOWN_END_DATE", DEFAULT_END_DATE)

    with open(tree_path, "r") as f:
        yaml_config = f.read()

    parser = Parser(yaml_config)
    provider_cfg = parser.config.provider
    fetcher = _build_fetcher(provider_cfg, parser.dag)
    data = _fetch_all_metrics(parser, fetcher, provider_cfg.type, start_date, end_date)

    app.state.parser = parser
    app.state.fetcher = fetcher
    app.state.data = data
    app.state.traces: Dict[str, Any] = {}
    app.state.lock = asyncio.Lock()

    logger.info(
        "breakdown API started: tree=%s provider=%s window=[%s, %s] rows=%d",
        tree_path, provider_cfg.type, start_date, end_date, len(data),
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
    return {
        "provider": parser.config.provider.type,
        "metrics": [m.name for m in parser.config.metrics],
        "date_start": str(data["date"].min().date()),
        "date_end": str(data["date"].max().date()),
        "fitted": sorted(request.app.state.traces.keys()),
    }


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
        # NaN/inf (e.g. r_hat on single-chain ADVI traces) are not valid JSON
        summary = {
            col: {k: (float(v) if math.isfinite(v) else None) for k, v in vals.items()}
            for col, vals in builder.get_summary(name).to_dict().items()
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
):
    parser = request.app.state.parser
    data = request.app.state.data

    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")

    async with request.app.state.lock:
        builder = ModelBuilder(parser.dag, data)
        await asyncio.to_thread(
            builder.build_and_sample, name,
            draws=draws, tune=tune, inference_method=inference_method,
        )
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
        builder = ModelBuilder(parser.dag, data)
        builder.traces.update(request.app.state.traces)
        try:
            result = await asyncio.to_thread(
                run_rca, builder, name,
                reference_start, reference_end,
                analysis_start, analysis_end,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        # Persist any traces fitted on demand during this call.
        request.app.state.traces.update(builder.traces)

    return result

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from typing import List, Dict, Any
from breakdown.parser import Parser
from breakdown.engine.model import ModelBuilder
from breakdown.data_fetch import MockDataFetcher, LocalDataFetcher, CloudDataFetcher, generate_mock_data
import pandas as pd
import json
import os

app = FastAPI(title="breakdown API")

# Load jaffle-shop example
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "examples")
jaffle_shop_path = os.path.join(EXAMPLES_DIR, "jaffle_shop_tree.yml")

with open(jaffle_shop_path, 'r') as f:
    YAML_CONFIG = f.read()

parser = Parser(YAML_CONFIG)

# Initialize the correct fetcher
provider_cfg = parser.config.provider
if provider_cfg.type == "local":
    fetcher = LocalDataFetcher(project_path=provider_cfg.project_path)
elif provider_cfg.type == "cloud":
    fetcher = CloudDataFetcher(
        environment_id=provider_cfg.environment_id,
        host=provider_cfg.host,
        token=provider_cfg.token
    )
else:
    fetcher = MockDataFetcher()

# In this PoC we still use generate_mock_data for global state but endpoints will use fetcher
data = generate_mock_data(n_days=100)
builder = ModelBuilder(parser.dag, data)

# Mount static files with an absolute path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
static_dir = os.path.join(BASE_DIR, "static")
app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")

@app.get("/")
async def root():
    return {"message": "breakdown API is running. Visit /ui for the visualization."}

@app.get("/dag")
async def get_dag():
    return {
        "nodes": [n for n in parser.dag.nodes(data=True)],
        "edges": [list(e) for e in parser.dag.edges()]
    }

@app.get("/metrics/{name}")
async def get_metric(name: str):
    metric = parser.get_metric(name)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    
    time_series = data[["date", name]].to_dict(orient="records")
    
    summary = None
    if name in builder.traces:
        summary = builder.get_summary(name).to_dict()

    return {
        "definition": metric.model_dump(),
        "time_series": time_series,
        "summary": summary
    }

@app.post("/analyze/{name}")
async def analyze_metric(name: str):
    if name not in parser.dag:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    
    # In a real app, this should be a background task
    builder.build_and_sample(name, draws=500, tune=500)
    
    return {"status": "success", "message": f"Analysis complete for {name}"}

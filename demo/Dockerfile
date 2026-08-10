# The White Cube demo image: breakdown + the tree + its parquet snapshots.
#
# Deliberately not the repo's root Dockerfile. That one is the general
# deployment story (mount your own tree, reach your own warehouse); this one
# bakes a specific analysis in and has no data provider at all, so it boots in
# seconds and cannot fail on a warehouse in the middle of a pitch. Everything
# it serves was produced by `make -C demo snapshots` against the live dbt +
# MetricFlow path.
#
# Build from the repo root so the package and the demo are both in context:
#   docker build -f demo/Dockerfile -t white-cube-demo .
FROM ghcr.io/astral-sh/uv:python3.14-bookworm

WORKDIR /app

# Dependency layer first so demo edits don't re-resolve the world. pytensor
# (PyMC's backend) compiles C at runtime, so the toolchain in this base image
# is a runtime requirement, not just a build one — hence no multi-stage build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY breakdown/ ./breakdown/
COPY README.md ./
RUN uv sync --frozen --no-dev

COPY demo/white_cube_tree.yml /demo/white_cube_tree.yml
COPY demo/.breakdown/snapshots/ /demo/snapshots/
COPY demo/prewarm.py /demo/prewarm.py

# The tree names its provider's project_path via ${WHITE_CUBE_DBT_PROJECT}.
# Nothing resolves it here on purpose: every series is a snapshot hit, and a
# path that does not exist is the loudest possible signal if that ever stops
# being true.
ENV WHITE_CUBE_DBT_PROJECT=/nonexistent-by-design \
    BREAKDOWN_SNAPSHOT_DIR=/demo/snapshots \
    BREAKDOWN_START_DATE=2024-06-01 \
    BREAKDOWN_END_DATE=2026-07-30

EXPOSE 9090

# Set BREAKDOWN_API_TOKEN to gate /mcp, and BREAKDOWN_PUBLIC_URL to the public
# hostname so MCP report_url deep links resolve for the client, not for us.
#
# The console script directly, not `uv run --no-sync`. uv's project discovery
# and lockfile read cost ~0.5s on a warm cache and every container start is a
# cold one — time spent before the port is bound, which is exactly the budget
# that made Fly's proxy give up and serve 503s to the first visitor after an
# idle period. `uv sync` above put the environment at /app/.venv; nothing here
# needs uv at runtime.
CMD ["/app/.venv/bin/breakdown", "serve", \
     "--host", "0.0.0.0", "--tree", "/demo/white_cube_tree.yml"]

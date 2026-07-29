# Single-stage on purpose: pytensor (PyMC's backend) compiles C at runtime,
# so the runtime image needs a C++ toolchain anyway and a slim multi-stage
# build saves little against the ~GB-scale scientific stack.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm

WORKDIR /app

# Dependency layer first so code edits don't re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 9090

# The tree is mounted read-only at /config/tree.yml (see compose.yaml).
# Provider credentials arrive as env vars referenced from the tree with
# ${VAR} interpolation — the Databricks CLI OAuth `profile` browser flow
# does not work headless.
CMD ["uv", "run", "--no-sync", "breakdown", "serve", "--host", "0.0.0.0", "--tree", "/config/tree.yml"]

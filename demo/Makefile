# Rebuild the White Cube demo from nothing: synthetic data -> dbt models ->
# MetricFlow -> parquet snapshots that the deployed image serves.
#
# The split matters. Everything through `snapshots` runs the *live* dbt +
# MetricFlow path — that is what proves the semantic-layer story, and it is the
# target to screen-share when a prospect asks "what does it take to point this at
# our warehouse?". What ships is only the tree plus the snapshots it produced, so
# the running demo has no provider that can be down mid-pitch.
#
#   make all        # generate -> build -> snapshots -> verify   (~5 min cold)
#   make serve      # run the demo locally off the snapshots
#   make verify     # re-check that every planted story is still recoverable
#
# Requires the fake_companies repo beside this one. Override with FAKE:
#   make all FAKE=/path/to/fake_companies

FAKE        ?= $(abspath $(CURDIR)/../../fake_companies)
BREAKDOWN   ?= $(abspath $(CURDIR)/..)
CONFIG      ?= $(FAKE)/configs/white_cube_b2c_app.yaml
DB          ?= $(FAKE)/out/white_cube.duckdb
DBT_PROJECT ?= $(FAKE)/dbt
SNAPSHOTS   ?= $(CURDIR)/.breakdown/snapshots
TREE        ?= $(CURDIR)/white_cube_tree.yml
START       ?= 2024-06-01
END         ?= 2026-07-30
PORT        ?= 9090

# dbt-core does not run on Python 3.14, which is what breakdown targets, so the
# semantic-layer toolchain stays in fake_companies' own 3.13 environment.
# LocalDataFetcher shells out to whatever `mf` is on PATH, so putting that venv
# first is all it takes — the two interpreters never have to agree. Note this
# must come before any `uv run`, which would prepend breakdown's own venv.
DATA_BIN := $(FAKE)/.venv/bin
ENV      := PATH="$(DATA_BIN):$$PATH" \
            DBT_PROFILES_DIR="$(DBT_PROJECT)" \
            FAKE_DB="$(DB)" \
            WHITE_CUBE_DBT_PROJECT="$(DBT_PROJECT)"

.PHONY: all generate build snapshots verify serve doctor clean

all: verify

## Synthetic raw data + ground truth, deterministic from the config's seed.
generate:
	cd $(FAKE) && uv sync --extra dbt
	cd $(FAKE) && uv run fake-companies generate --config $(CONFIG) --out $(DB)

## Staging + marts + semantic layer, in DuckDB.
build: generate
	cd $(DBT_PROJECT) && $(ENV) uv run dbt build --project-dir .
	cd $(DBT_PROJECT) && $(ENV) uv run mf validate-configs

## Fetch every metric once through MetricFlow and freeze it as parquet.
## This is the only step that touches the warehouse; the deployed image replays it.
snapshots: build
	rm -rf $(SNAPSHOTS)
	$(ENV) $(BREAKDOWN)/.venv/bin/breakdown serve --tree $(TREE) \
	  --start-date $(START) --end-date $(END) \
	  --snapshot-dir $(SNAPSHOTS) --refresh --port $(PORT) & \
	  echo $$! > /tmp/white_cube_snap.pid
	@echo "waiting for the startup fetch to finish..."
	@until curl -fsS http://127.0.0.1:$(PORT)/health 2>/dev/null | grep -q '"ok"'; do sleep 3; done
	@kill $$(cat /tmp/white_cube_snap.pid) && rm -f /tmp/white_cube_snap.pid
	@echo "snapshots written to $(SNAPSHOTS)"
	@ls $(SNAPSHOTS) | wc -l | xargs echo "  files:"

## Both halves: the planted stories are recoverable from the data, and the tree
## resolves against the semantic layer.
verify: snapshots doctor
	cd $(FAKE) && FAKE_DB=$(DB) uv run python scripts/verify_white_cube_stories.py
	cd $(BREAKDOWN) && uv run pytest tests/test_white_cube_demo.py -v

doctor:
	$(ENV) $(BREAKDOWN)/.venv/bin/breakdown doctor --tree $(TREE) \
	  --start-date $(START) --end-date $(END)

## Serve the demo the way the deployed image does — snapshots only, no provider.
serve:
	$(BREAKDOWN)/.venv/bin/breakdown serve --tree $(TREE) \
	  --start-date $(START) --end-date $(END) \
	  --snapshot-dir $(SNAPSHOTS) --port $(PORT)

clean:
	rm -rf $(SNAPSHOTS) $(DB) $(FAKE)/out/white_cube_drivers.csv

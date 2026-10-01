#!/usr/bin/env bash
# Deploy a commit to the demo box and warm its trace cache.
#
# Runs on the server as `deploy`. The workflow pipes this file over SSH from
# its own checkout, so the version that runs is the one being deployed,
# not whatever an older deploy left on disk:
#
#   ssh deploy@<server> 'bash -s' -- <commit> < demo/hetzner/deploy.sh
set -euo pipefail

sha=${1:?usage: deploy.sh <commit>}
cd /srv/breakdown

git fetch --quiet origin
git checkout --quiet --detach "$sha"
echo "checked out $(git log -1 --format='%h %s')"

cd demo/hetzner
test -f .env || { echo "demo/hetzner/.env is missing; see README.md" >&2; exit 1; }

# Built here, on the server's own architecture, so the same workflow serves an
# ARM box or an x86 one.
docker compose build
docker compose up -d --wait

# A restarted container has an empty trace cache. Warm it from inside the
# container, against loopback, so the prewarm neither depends on DNS nor
# passes through Caddy.
docker compose exec -T white-cube /app/.venv/bin/python /demo/prewarm.py --rcas

# Keep the disk from filling with superseded images and build cache.
docker image prune -f
docker builder prune -f --filter until=168h

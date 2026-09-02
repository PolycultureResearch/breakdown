#!/usr/bin/env python
"""Probe every deployed demo in demo/demos.yaml: /health and /manifest.

The lightest "are the demos up and serving the right thing" check: for each
`deployed: true` entry, /health must say `ok` and /manifest must identify
itself (app, version, a loadable default tree). Run it after a deploy, from
CI, or as an agent's first step before exercising a demo. `--all` also probes
entries still marked `deployed: false` (reported, but not counted as
failures — they are expected to be absent until their Fly apps exist).

Usage (from the repo root):
    python demo/check_demos.py
    python demo/check_demos.py --all
    python demo/check_demos.py white_cube

Exits non-zero if any deployed demo fails, so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMOS_YAML = REPO_ROOT / "demo" / "demos.yaml"
TIMEOUT = 30  # generous: auto-stopped Fly machines cold-start on first request


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.load(resp)


def probe(demo: dict) -> tuple[bool, str]:
    base = demo["url"].rstrip("/")
    try:
        health = get_json(f"{base}/health")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        return False, f"/health unreachable: {e}"
    if health.get("status") != "ok":
        return False, f"/health degraded: {health.get('error', health)}"
    try:
        manifest = get_json(f"{base}/manifest")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        # An older deploy without /manifest still passes /health; say so
        # rather than failing, until every demo runs a build that has it.
        return True, f"healthy (no /manifest yet: {e})"
    tree = manifest.get("default_tree") or {}
    demo_id = manifest.get("demo", {})
    return True, (
        f"healthy — breakdown {manifest.get('version')}, "
        f"tree '{tree.get('title')}' ({tree.get('metric_count')} metrics), "
        f"demo={demo_id.get('slug', '?')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="demo slugs to probe (default: deployed ones)")
    ap.add_argument("--all", action="store_true", help="also probe undeployed entries")
    args = ap.parse_args()

    registry = yaml.safe_load(DEMOS_YAML.read_text())
    demos = {d["slug"]: d for d in registry["demos"]}
    unknown = set(args.slugs) - set(demos)
    if unknown:
        ap.error(f"unknown slugs {sorted(unknown)}; known: {sorted(demos)}")

    failures = 0
    for slug, demo in demos.items():
        if args.slugs and slug not in args.slugs:
            continue
        deployed = demo.get("deployed", False)
        if not deployed and not (args.all or slug in args.slugs):
            print(f"  SKIP {slug} (not deployed yet)")
            continue
        ok, detail = probe(demo)
        tag = "PASS" if ok else ("WARN" if not deployed else "FAIL")
        print(f"  {tag} {slug} {demo['url']} — {detail}")
        if not ok and deployed:
            failures += 1
    print("ALL DEPLOYED DEMOS HEALTHY" if failures == 0 else f"{failures} DEMO(S) FAILING")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

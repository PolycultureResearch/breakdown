#!/usr/bin/env python
"""Fetch the prebuilt demo dataset bundles named in demo/demos.yaml.

Why download instead of regenerate: the bundles are deterministic build
artifacts of fake_companies (raw tables + dbt marts + MetricFlow semantic
already materialized in one duckdb, plus the planted-anomaly ground truth),
published as GitHub release assets. Downloading gives an agent or CI the
exact bytes the hosted demos were built from in seconds, with no
fake_companies checkout, no dbt, and no 3.13-vs-3.14 toolchain split.

Usage (from the repo root):
    python demo/fetch_demo_data.py                 # every demo -> demo/.data/<slug>/
    python demo/fetch_demo_data.py alpenglow       # just one
    python demo/fetch_demo_data.py --dest /tmp/dd  # elsewhere
    python demo/fetch_demo_data.py --force         # re-download existing files

To rebuild from scratch instead (needs a fake_companies checkout with its own
venv): `uv run python scripts/build_demo_datasets.py` over there, which is
also how a new release version is produced.

Exits non-zero if any requested download fails, so it can gate a build.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMOS_YAML = REPO_ROOT / "demo" / "demos.yaml"


def fetch(url: str, dest: Path, force: bool) -> bool:
    if dest.exists() and not force:
        print(f"  have {dest.name} ({dest.stat().st_size / 1e6:.0f} MB) — skipping")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        tmp.rename(dest)
    except OSError as e:
        print(f"  FAIL {url}: {e}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False
    print(f"  got {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="demo slugs to fetch (default: all)")
    ap.add_argument("--dest", type=Path, default=REPO_ROOT / "demo" / ".data")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    registry = yaml.safe_load(DEMOS_YAML.read_text())
    demos = {d["slug"]: d for d in registry["demos"]}
    unknown = set(args.slugs) - set(demos)
    if unknown:
        ap.error(f"unknown slugs {sorted(unknown)}; known: {sorted(demos)}")

    ok = True
    for slug in args.slugs or demos:
        print(f"== {slug}")
        for kind, url in demos[slug]["dataset"].items():
            ok &= fetch(url, args.dest / slug / url.rsplit("/", 1)[1], args.force)
    if ok:
        print(f"done: {args.dest}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

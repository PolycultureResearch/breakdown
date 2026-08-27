#!/usr/bin/env python
"""Warm a running White Cube instance, for two different jobs.

**At build time** (`--slices`, against a live provider): touch every declared
(metric, dimension) pair once. Because the snapshot fetcher widens sliced fetches
to the loaded data window, one touch per pair stores a snapshot that answers
*any* window a prospect later picks — so the deployed image can slice with no
provider at all.

**At boot** (`--rcas`, against the deployed image): run the guided tour's four
analyses so their fits land in the trace cache. Fits are serialized behind a
single lock and a cold multi-node RCA takes ~42s at the tour's widest story
(exact MCMC, the default since roadmap S2 — measured 42.3s / 46.2s / 9.8s /
24.1s across the four), which is a long silence in a live pitch. Everything
after this is a cache hit.

    python demo/prewarm.py --slices --rcas --url http://127.0.0.1:9090

Exits non-zero if anything failed, so it can gate a build.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

# The guided tour's window pairs, kept in sync with knowledge/demo_guided_tour.md
# and fake_companies/scripts/verify_white_cube_stories.py.
TOUR_RCAS = [
    ("A  mobile signup CTA", "new_mrr", "2026-01-05", "2026-02-01", "2026-02-09", "2026-03-08"),
    (
        "B  professional churn",
        "net_new_mrr",
        "2026-03-16",
        "2026-04-12",
        "2026-05-11",
        "2026-06-07",
    ),
    ("C  Brazil campaign", "signups", "2025-02-03", "2025-03-02", "2025-03-10", "2025-04-06"),
    ("D  onboarding revamp", "new_mrr", "2025-07-07", "2025-08-03", "2025-08-11", "2025-09-07"),
]

# Any whole-week window inside the data does; the widening is what matters.
SLICE_PROBE = ("2026-03-16", "2026-04-12", "2026-05-11", "2026-06-07")


def call(url: str, method: str = "GET", timeout: int = 900):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        import json

        return json.loads(r.read())


# How many times a sliced fetch is attempted before it counts as a failure.
#
# Measured 2026-08-13 rebuilding the demo from scratch: `mf query` returned a
# non-zero exit with **empty stderr** on 12 of 32 pairs, and every one of them
# succeeded on the next pass with the same command and the same data. So the
# failure is transient in the MetricFlow/DuckDB path, not in the query — and a
# whole rebuild aborting on it (the target is `snapshots: build`, so a retry is
# minutes of dbt) is a worse answer than asking twice. A pair that fails every
# attempt is still a hard failure: this retries, it does not tolerate.
_ATTEMPTS = 3


def warm_slices(base: str) -> list[str]:
    """One touch per declared (metric, dimension) — the widened fetch does the rest.

    Dimensions come from /dag, which carries each node's full definition; /meta
    reports grains, kinds and freshness but not declared dimensions."""
    dag = call(f"{base}/dag")
    failures = []
    pairs = [
        (name, dim) for name, defn in dag.get("nodes", []) for dim in (defn.get("dimensions") or {})
    ]
    print(f"warming {len(pairs)} sliced snapshots...")
    rs, re, as_, ae = SLICE_PROBE
    for metric, dim in pairs:
        url = (
            f"{base}/rca/{metric}/slices?dimension={dim}"
            f"&reference_start={rs}&reference_end={re}"
            f"&analysis_start={as_}&analysis_end={ae}"
        )
        detail = None
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                call(url, method="POST")
                note = "" if attempt == 1 else f" (attempt {attempt})"
                print(f"  ok   {metric} by {dim}{note}")
                detail = None
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                detail = (
                    e.read().decode()[:200] if isinstance(e, urllib.error.HTTPError) else str(e)
                )
                if attempt < _ATTEMPTS:
                    print(f"  retry {metric} by {dim}: {detail}")
        if detail is not None:
            print(f"  FAIL {metric} by {dim} after {_ATTEMPTS} attempts: {detail}")
            failures.append(f"slice {metric} by {dim}")
    return failures


def warm_rcas(base: str) -> list[str]:
    failures = []
    print(f"warming {len(TOUR_RCAS)} guided-tour analyses...")
    for label, target, rs, re, as_, ae in TOUR_RCAS:
        url = (
            f"{base}/rca/{target}?reference_start={rs}&reference_end={re}"
            f"&analysis_start={as_}&analysis_end={ae}"
        )
        try:
            d = call(url, method="POST")
            top = (d.get("ranked_causes") or [{}])[0].get("metric", "?")
            print(f"  ok   {label}: {target} -> top cause {top}")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            detail = e.read().decode()[:200] if isinstance(e, urllib.error.HTTPError) else str(e)
            print(f"  FAIL {label}: {detail}")
            failures.append(label)
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:9090")
    p.add_argument("--slices", action="store_true", help="warm sliced snapshots (build time)")
    p.add_argument("--rcas", action="store_true", help="warm the trace cache (boot)")
    args = p.parse_args()
    if not (args.slices or args.rcas):
        args.slices = args.rcas = True

    base = args.url.rstrip("/")
    failures = []
    if args.slices:
        failures += warm_slices(base)
    if args.rcas:
        failures += warm_rcas(base)

    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("\nwarm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

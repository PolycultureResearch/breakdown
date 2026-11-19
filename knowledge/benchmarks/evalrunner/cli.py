"""The evaluation runner's command line (`knowledge/benchmarks/evaluate.py`).

    evaluate.py run   --worlds DIR --tree TREE --tier smoke --out DIR
    evaluate.py score RUN_DIR [RUN_DIR ...] [--publish knowledge/eval/NAME]
    evaluate.py diff  A.json B.json [--out FILE.md]

`run` plans the jobs (`run.json`), executes them in `--shards` worker
processes, each with a fresh PyTensor compile directory of its own, and writes
`scoreboard.json` and `scoreboard.md` into the output directory. Running it
again into the same directory resumes: finished jobs are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ENTRY = PACKAGE_DIR.parent / "evaluate.py"
MAX_DEFAULT_SHARDS = 4


def parse_seeds(text: str) -> list[int]:
    """`0-4`, `3`, `0-2,7`."""
    seeds: set[int] = set()
    for part in text.split(","):
        lo, sep, hi = part.strip().partition("-")
        seeds.update(range(int(lo), (int(hi) if sep else int(lo)) + 1))
    return sorted(seeds)


def default_bindings(tree_path: str) -> str | None:
    """A bindings file shipped beside the runner for this tree, if there is one."""
    candidate = PACKAGE_DIR / "bindings" / (Path(tree_path).stem + ".yml")
    return str(candidate) if candidate.exists() else None


def compile_env(compile_dir: Path) -> dict[str, str]:
    """This process's environment with `base_compiledir` pointed at a
    directory no other process uses (roadmap C65), other flags kept."""
    flags = [
        f
        for f in os.environ.get("PYTENSOR_FLAGS", "").split(",")
        if f.strip() and not f.strip().startswith("base_compiledir=")
    ]
    env = dict(os.environ)
    env["PYTENSOR_FLAGS"] = ",".join([*flags, f"base_compiledir={compile_dir}"])
    return env


def cmd_run(opts) -> int:
    from .loader import UnservedNodes, WorldCannotAnswer
    from .runner import RunRefused, done_jobs, plan_run
    from .worldset import WorldSetError

    out = Path(opts.out)
    bindings = opts.bindings or default_bindings(opts.tree)
    try:
        run = plan_run(
            opts.worlds,
            opts.tree,
            tier=opts.tier,
            bindings_path=bindings,
            seeds=parse_seeds(opts.seeds) if opts.seeds else None,
            held_out=opts.held_out,
            regimes=opts.regimes.split(",") if opts.regimes else None,
            horizons=tuple(int(h) for h in opts.horizons.split(",")) if opts.horizons else None,
            spacing_days=opts.spacing_days,
            max_windows=opts.max_windows,
            singles=opts.singles.split(",") if opts.singles else None,
            drop_unserved=opts.drop_unserved,
            trace_cache_bytes=int(opts.trace_cache_gb * 1024**3),
        )
    except (RunRefused, UnservedNodes, WorldCannotAnswer, WorldSetError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    kinds = {k: sum(j["kind"] == k for j in run["jobs"]) for k in ("calibration", "single")}
    tree = run["tree"]
    print(f"tier {run['tier']}: {run['vertical']} seeds {run['seeds']} regimes {run['regimes']}")
    print(f"tree {tree['path']}: {len(tree['served'])} nodes served")
    for node, why in tree["unserved"].items():
        print(f"  not served: {node}: {why}")
    for name, d in {**run["singles"], **run["singles_not_run"]}.items():
        mark = "run " if name in run["singles"] else "skip"
        print(f"  {mark} single {name}: {d['class']} {d.get('truth') or ''} {d.get('note') or ''}")
    print(f"jobs: {kinds['calibration']} calibration fits, {kinds['single']} single worlds")
    if opts.dry_run:
        return 0

    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run, indent=1, default=str) + "\n")
    before = len(done_jobs(out) & {j["job"] for j in run["jobs"]})
    shards = max(1, min(opts.shards, len(run["jobs"])))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    (out / "logs").mkdir(exist_ok=True)
    started = time.time()
    procs = []
    for k in range(shards):
        compile_dir = out / "compiledir" / f"{stamp}-{k}"
        compile_dir.mkdir(parents=True)
        log = open(out / "logs" / f"{stamp}-shard{k}.log", "w")
        procs.append(
            (
                subprocess.Popen(
                    [sys.executable, str(ENTRY), "worker", str(out), "--shard", f"{k}/{shards}"],
                    env=compile_env(compile_dir),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                ),
                compile_dir,
                log,
            )
        )
    wanted = {j["job"] for j in run["jobs"]}
    while any(p.poll() is None for p, _, _ in procs):
        time.sleep(opts.poll_seconds)
        done = len(done_jobs(out) & wanted)
        print(
            f"  {done}/{len(wanted)} jobs done, {(time.time() - started) / 60:.1f} min", flush=True
        )
    failed = [k for k, (p, _, _) in enumerate(procs) if p.returncode != 0]
    for _, compile_dir, log in procs:
        log.close()
        shutil.rmtree(compile_dir, ignore_errors=True)
    wall = time.time() - started
    with open(out / "invocations.jsonl", "a") as f:
        f.write(
            json.dumps(
                {
                    "started": stamp,
                    "wall_seconds": round(wall, 1),
                    "shards": shards,
                    "jobs_already_done": before,
                    "jobs_done_now": len(done_jobs(out) & wanted) - before,
                    "failed_shards": failed,
                }
            )
            + "\n"
        )
    print(f"wall time {wall / 60:.1f} min on {shards} shard(s)")
    if failed:
        print(f"shard(s) {failed} failed; see {out / 'logs'}", file=sys.stderr)
        return 1
    return cmd_score(argparse.Namespace(run_dirs=[str(out)], publish=None, title=None))


def cmd_worker(opts) -> int:
    from .runner import run_shard

    k, n = (int(x) for x in opts.shard.split("/"))
    run_shard(opts.run_dir, (k, n))
    return 0


def cmd_score(opts) -> int:
    from .scoreboard import build_scoreboard, merge_scoreboards, render_markdown

    boards = []
    for run_dir in opts.run_dirs:
        board = build_scoreboard(run_dir)
        log = Path(run_dir) / "invocations.jsonl"
        if log.exists():
            board["invocations"] = [json.loads(line) for line in log.read_text().splitlines()]
        boards.append(board)
    board = merge_scoreboards(boards)
    targets = [Path(opts.run_dirs[0]) / "scoreboard"] if len(boards) == 1 else []
    if opts.publish:
        targets.append(Path(opts.publish))
    if not targets:
        print("several run directories need --publish PATH", file=sys.stderr)
        return 2
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".json").write_text(json.dumps(board, indent=1, default=str) + "\n")
        target.with_suffix(".md").write_text(render_markdown(board, opts.title))
        print(f"wrote {target.with_suffix('.json')} and {target.with_suffix('.md')}")
    return 0


def cmd_diff(opts) -> int:
    from .scoreboard import diff_scoreboards, render_diff

    a, b = (json.loads(Path(p).read_text()) for p in (opts.a, opts.b))
    diff = diff_scoreboards(a, b)
    text = render_diff(diff)
    if opts.out:
        Path(opts.out).write_text(text + "\n")
        Path(opts.out).with_suffix(".json").write_text(json.dumps(diff, indent=1) + "\n")
        print(f"wrote {opts.out}")
    else:
        print(text)
    return 0


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = cli.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="plan, execute and score one world set on one tree")
    run.add_argument("--worlds", required=True, help="a world set directory (holds worlds.json)")
    run.add_argument("--tree", required=True, help="the tree file; recorded on the scoreboard")
    run.add_argument("--bindings", help="node -> series-file binding overrides (YAML)")
    run.add_argument("--out", required=True, help="run directory: rows, fit cache, scoreboard")
    run.add_argument("--tier", choices=("smoke", "standard", "full"), default="smoke")
    run.add_argument("--seeds", help="override the tier's seeds, e.g. 0-2 or 0,3")
    run.add_argument(
        "--held-out",
        action="store_true",
        help="also score held-out seeds (5-9, or any named in --seeds); recorded on the scoreboard",
    )
    run.add_argument("--regimes", help="comma-separated; default per tier")
    run.add_argument("--horizons", help="comma-separated days; default per tier")
    run.add_argument("--spacing-days", type=int, help="days between null-window starts")
    run.add_argument("--max-windows", type=int, help="null windows per world, evenly spaced")
    run.add_argument("--singles", help="comma-separated anomaly names; default per tier")
    run.add_argument(
        "--drop-unserved",
        action="store_true",
        help="run without the nodes the world set cannot serve (recorded as a tree change)",
    )
    run.add_argument("--shards", type=int, default=MAX_DEFAULT_SHARDS, help="worker processes")
    run.add_argument("--trace-cache-gb", type=float, default=4.0, help="byte cap; 0 disables")
    run.add_argument("--poll-seconds", type=float, default=60.0)
    run.add_argument("--dry-run", action="store_true", help="plan and print; run nothing")
    run.set_defaults(func=cmd_run)

    worker = sub.add_parser("worker", help="(internal) run one shard of a planned run")
    worker.add_argument("run_dir")
    worker.add_argument("--shard", default="0/1")
    worker.set_defaults(func=cmd_worker)

    score = sub.add_parser("score", help="rebuild the scoreboard from a run's rows")
    score.add_argument("run_dirs", nargs="+")
    score.add_argument("--publish", help="also write PATH.json and PATH.md")
    score.add_argument("--title")
    score.set_defaults(func=cmd_score)

    diff = sub.add_parser("diff", help="paired differences between two scoreboards")
    diff.add_argument("a")
    diff.add_argument("b")
    diff.add_argument("--out", help="write FILE.md and FILE.json instead of printing")
    diff.set_defaults(func=cmd_diff)

    opts = cli.parse_args(argv)
    return opts.func(opts)

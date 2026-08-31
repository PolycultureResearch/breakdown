"""Console entry point (`breakdown` after install, `python main.py` in the repo)."""

import argparse
import datetime
import os

from breakdown import __version__


def serve(
    port: int = 9090,
    host: str = "127.0.0.1",
    tree: str | None = None,
    default_tree: str | None = None,
    eager: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    reload: bool = False,
    snapshot_dir: str | None = None,
    no_snapshots: bool = False,
    refresh: bool = False,
) -> None:
    import uvicorn

    # Config flows to the app via env vars so it survives uvicorn's
    # reload subprocess (and doubles as the container config surface).
    if tree:
        tree_path = os.path.abspath(tree)
        # A directory serves every *.yml in it as its own tree (roadmap 2.16);
        # a file is the single-tree case, unchanged.
        if not os.path.exists(tree_path):
            raise SystemExit(f"Metric tree file or directory not found: {tree_path}")
        os.environ["BREAKDOWN_TREE"] = tree_path
    if default_tree:
        os.environ["BREAKDOWN_DEFAULT_TREE"] = default_tree
    if eager:
        os.environ["BREAKDOWN_EAGER"] = "1"
    for flag, value, env in (
        ("--start-date", start_date, "BREAKDOWN_START_DATE"),
        ("--end-date", end_date, "BREAKDOWN_END_DATE"),
    ):
        if value:
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise SystemExit(f"{flag} must be a valid YYYY-MM-DD date, got '{value}'")
            os.environ[env] = value
    # Ordering is checked here, not just per date (roadmap grill L11): the
    # engine's own check lives inside `load_tree`'s try, so a reversed pair on
    # a directory of trees gave a clean startup and then a 503 per tree at
    # first click — the operator's mistake reported as the server's.
    if start_date and end_date and end_date < start_date:
        raise SystemExit(f"--end-date ({end_date}) is before --start-date ({start_date}).")
    # MCP deep links (rca_link etc.) default to this port; the bind host
    # tells the MCP transport whether Host-header validation still makes
    # sense (it doesn't off-loopback).
    os.environ["BREAKDOWN_PORT"] = str(port)
    os.environ["BREAKDOWN_HOST"] = host
    if no_snapshots:
        os.environ["BREAKDOWN_SNAPSHOT_DIR"] = "off"
    elif snapshot_dir:
        os.environ["BREAKDOWN_SNAPSHOT_DIR"] = os.path.abspath(snapshot_dir)
    if refresh:
        os.environ["BREAKDOWN_REFRESH"] = "1"

    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"Starting breakdown server on http://{display_host}:{port}")
    print(f"UI available at http://{display_host}:{port}/ui")
    print(f"MCP endpoint at http://{display_host}:{port}/mcp")
    if tree:
        print(f"Metric tree: {os.environ['BREAKDOWN_TREE']}")
    uvicorn.run("breakdown.api.main:app", host=host, port=port, reload=reload)


def doctor(tree: str, start_date: str | None = None, end_date: str | None = None) -> int:
    from breakdown.doctor import print_report, run_doctor

    return print_report(run_doctor(tree, start_date=start_date, end_date=end_date))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="breakdown", description="breakdown: Open-Source Bayesian Metric Trees"
    )
    # First thing anyone filing a bug is asked for. Names the distribution as
    # well as the command, because `pip install metric-breakdown` and
    # `breakdown` read differently and issue reports need both.
    parser.add_argument(
        "--version",
        action="version",
        version=f"breakdown {__version__} (metric-breakdown {__version__})",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the API and UI server")
    serve_parser.add_argument("--port", type=int, default=9090, help="Port to run on")
    serve_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Interface to bind (0.0.0.0 to accept outside connections, e.g. in a container)",
    )
    serve_parser.add_argument(
        "--tree",
        type=str,
        default=None,
        help="Path to a metric tree YAML, or a directory of them — each *.yml "
        "is one tree, its id the filename stem (default: the bundled "
        "jaffle_shop example)",
    )
    serve_parser.add_argument(
        "--default-tree",
        type=str,
        default=None,
        help="Which tree id the unprefixed routes and a bare /ui open "
        "(default: the only tree, else the alphabetically first)",
    )
    serve_parser.add_argument(
        "--eager",
        action="store_true",
        help="Fetch the default tree's data at startup instead of on first "
        "use (a directory of trees loads lazily by default)",
    )
    serve_parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start of the data window, YYYY-MM-DD (default: 2024-01-01)",
    )
    serve_parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End of the data window, YYYY-MM-DD (default: 2024-04-09)",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Restart on code changes (development only)",
    )
    serve_parser.add_argument(
        "--snapshot-dir",
        type=str,
        default=None,
        help="Where to cache fetched series as parquet snapshots "
        "(default: .breakdown/snapshots next to the tree)",
    )
    serve_parser.add_argument(
        "--no-snapshots",
        action="store_true",
        help="Always fetch from the provider; never read or write snapshots",
    )
    serve_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refetch every metric from the provider and overwrite its snapshot",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check a tree's data-provider connectivity and print remediation"
    )
    doctor_parser.add_argument("--tree", type=str, required=True, help="Path to a metric tree YAML")
    doctor_parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start of the probe window, YYYY-MM-DD (default: 7 days ago)",
    )
    doctor_parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End of the probe window, YYYY-MM-DD (default: today)",
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(
            args.port,
            host=args.host,
            tree=args.tree,
            default_tree=args.default_tree,
            eager=args.eager,
            start_date=args.start_date,
            end_date=args.end_date,
            reload=args.reload,
            snapshot_dir=args.snapshot_dir,
            no_snapshots=args.no_snapshots,
            refresh=args.refresh,
        )
    elif args.command == "doctor":
        raise SystemExit(doctor(args.tree, start_date=args.start_date, end_date=args.end_date))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

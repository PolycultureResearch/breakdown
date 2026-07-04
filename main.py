import argparse
import os

import uvicorn


def serve(port: int = 9090, tree: str = None, start_date: str = None, end_date: str = None):
    # Config flows to the app via env vars so it survives uvicorn's
    # reload subprocess.
    if tree:
        tree_path = os.path.abspath(tree)
        if not os.path.isfile(tree_path):
            raise SystemExit(f"Metric tree file not found: {tree_path}")
        os.environ["BREAKDOWN_TREE"] = tree_path
    if start_date:
        os.environ["BREAKDOWN_START_DATE"] = start_date
    if end_date:
        os.environ["BREAKDOWN_END_DATE"] = end_date

    print(f"Starting breakdown server on http://127.0.0.1:{port}")
    print(f"UI available at http://127.0.0.1:{port}/ui")
    if tree:
        print(f"Metric tree: {os.environ['BREAKDOWN_TREE']}")
    uvicorn.run("breakdown.api.main:app", host="127.0.0.1", port=port, reload=True)


def main():
    parser = argparse.ArgumentParser(description="breakdown: Open-Source Bayesian Metric Trees")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the API and UI server")
    serve_parser.add_argument("--port", type=int, default=9090, help="Port to run on")
    serve_parser.add_argument(
        "--tree", type=str, default=None,
        help="Path to a metric tree YAML (default: examples/jaffle_shop_tree.yml)",
    )
    serve_parser.add_argument(
        "--start-date", type=str, default=None,
        help="Start of the data window, YYYY-MM-DD (default: 2024-01-01)",
    )
    serve_parser.add_argument(
        "--end-date", type=str, default=None,
        help="End of the data window, YYYY-MM-DD (default: 2024-04-09)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        serve(args.port, tree=args.tree, start_date=args.start_date, end_date=args.end_date)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

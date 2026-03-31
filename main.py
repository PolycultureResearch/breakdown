import uvicorn
import argparse

def serve(port: int = 9090):
    print(f"Starting breakdown server on http://127.0.0.1:{port}")
    print(f"UI available at http://127.0.0.1:{port}/ui")
    uvicorn.run("breakdown.api.main:app", host="127.0.0.1", port=port, reload=True)

def main():
    parser = argparse.ArgumentParser(description="breakdown: Open-Source Bayesian Metric Trees")
    subparsers = parser.add_subparsers(dest="command")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Start the API and UI server")
    serve_parser.add_argument("--port", type=int, default=9090, help="Port to run on")

    args = parser.parse_args()

    if args.command == "serve":
        serve(args.port)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()

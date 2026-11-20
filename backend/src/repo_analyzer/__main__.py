"""Command-line entry point: ``python -m repo_analyzer`` or ``repo-analyzer``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from repo_analyzer.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Repository Analyzer API server.")
    # Loopback by default: the API has no authentication and can clone repositories.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()

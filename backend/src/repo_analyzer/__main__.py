"""Command-line entry point: ``python -m repo_analyzer`` or ``repo-analyzer``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from repo_analyzer.api.app import create_app

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
# Libraries that are very chatty below WARNING (the Neo4j driver logs every packet at DEBUG).
_QUIET_LOGGERS = ("neo4j", "httpcore", "urllib3", "watchfiles")


def configure_logging(level: str, log_file: Path | None = None) -> None:
    """One format for the app, uvicorn and its access log, on stderr and optionally a file."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, handlers=handlers, force=True)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Repository Analyzer API server.")
    # Loopback by default: the API has no authentication and can clone repositories.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    parser.add_argument(
        "--log-file", type=Path, help="Also append the log to this file (e.g. .data/server.log)"
    )
    args = parser.parse_args()

    configure_logging(args.log_level, args.log_file)
    # log_config=None: uvicorn's own loggers propagate to the handlers configured above,
    # so server, access and application lines share one format and the log file.
    uvicorn.run(
        create_app(), host=args.host, port=args.port, log_level=args.log_level, log_config=None
    )


if __name__ == "__main__":
    main()

"""``python -m repo_analyzer.benchmark``: run the known-answer benchmark.

Needs ANTHROPIC_API_KEY for a real run. Without it the command refuses to run
(``--dry-run`` still checks the setup) so no results are ever made up.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.benchmark.approaches import APPROACHES
from repo_analyzer.benchmark.questions import load_questions
from repo_analyzer.benchmark.report import write_report
from repo_analyzer.benchmark.runner import BenchmarkConfig, BenchmarkRunner
from repo_analyzer.config import Settings, default_benchmarks_dir
from repo_analyzer.retrieval.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingsUnavailableError,
    FastEmbedEmbedder,
    fastembed_available,
)

EXIT_NO_API_KEY = 2


def _git_commit(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    benchmarks = default_benchmarks_dir()
    parser = argparse.ArgumentParser(
        prog="python -m repo_analyzer.benchmark",
        description="Answer the known-answer questions under each approach and grade them.",
    )
    parser.add_argument(
        "--approach", action="append", choices=APPROACHES, dest="approaches",
        help="Approach to run (repeatable). Default: all four.",
    )  # fmt: skip
    parser.add_argument("--questions", type=Path, default=benchmarks / "questions.yaml")
    parser.add_argument("--fixtures", type=Path, default=benchmarks / "fixtures")
    parser.add_argument(
        "--out", type=Path, help="Output directory (default: benchmarks/results/<timestamp>)"
    )
    parser.add_argument(
        "--only", action="append", metavar="QUESTION_ID", help="Run only these questions"
    )
    parser.add_argument("--limit", type=int, help="Run only the first N questions")
    parser.add_argument(
        "--judge", action="store_true", help="Also grade each answer with an LLM judge"
    )
    parser.add_argument("--top-k", type=int, default=8, help="Chunks given to json-embeddings")
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--dry-run", action="store_true", help="Check the setup; call no model")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    question_set = load_questions(args.questions)
    questions = list(question_set.questions)
    if args.only:
        questions = [q for q in questions if q.id in set(args.only)]
    if args.limit:
        questions = questions[: args.limit]
    approaches = args.approaches or list(APPROACHES)

    has_key = bool(settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value())

    plan = {
        "questions": len(questions),
        "approaches": approaches,
        "fixtures": str(args.fixtures),
        "ANTHROPIC_API_KEY": "set" if has_key else "missing",
        "fastembed": "installed"
        if fastembed_available()
        else 'missing (json-embeddings will be skipped; pip install -e ".[embeddings]")',
        "model": settings.llm_model,
    }
    for key, value in plan.items():
        print(f"{key}: {value}")
    if args.dry_run:
        with tempfile.TemporaryDirectory() as tmp:
            config = BenchmarkConfig(
                settings=settings,
                fixtures_dir=args.fixtures,
                repositories=question_set.repositories,
                questions=questions,
                work_dir=Path(tmp),
            )
            runner = BenchmarkRunner(config, lambda: build_chat_model(settings))
            for repository in runner.index_fixtures():
                print(
                    f"indexed {repository.name}: {repository.file_count} files, "
                    f"{repository.symbol_count} functions/classes, "
                    f"{repository.dependency_count} dependencies"
                )
        return 0
    if not has_key:
        print(
            "\nANTHROPIC_API_KEY is not set, so no question can be answered. Set it and run "
            "again; no results were written.",
            file=sys.stderr,
        )
        return EXIT_NO_API_KEY

    started = datetime.now(UTC)
    out_dir = args.out or (default_benchmarks_dir() / "results" / started.strftime("%Y%m%d-%H%M%S"))

    def embedder() -> FastEmbedEmbedder:
        return FastEmbedEmbedder(args.embedding_model)

    config = BenchmarkConfig(
        settings=settings,
        fixtures_dir=args.fixtures,
        repositories=question_set.repositories,
        questions=questions,
        work_dir=out_dir / "work",
        approaches=approaches,
        judge=args.judge,
        top_k=args.top_k,
        embedder_factory=embedder if fastembed_available() else None,
    )
    try:
        result = asyncio.run(BenchmarkRunner(config, lambda: build_chat_model(settings)).run())
    except EmbeddingsUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    run_info: dict[str, Any] = {
        "started_at": started.isoformat(timespec="seconds"),
        "model": settings.llm_model,
        "effort": settings.llm_effort,
        "questions": len(questions),
        "commit": _git_commit(args.fixtures),
    }
    summary = write_report(result, out_dir, run_info)
    print(f"\nWrote {summary}")
    print(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Write benchmark results as CSV, JSON lines and a markdown summary."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any

from repo_analyzer.benchmark.runner import BenchmarkResult, QuestionResult

CSV_COLUMNS = [
    "approach", "question_id", "category", "correct", "facts_matched", "facts_total",
    "judge_correct", "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
    "cache_creation_tokens", "llm_calls", "tool_calls", "latency_ms", "error",
]  # fmt: skip


def csv_text(results: list[QuestionResult]) -> str:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in results:
        writer.writerow(asdict(row))
    return handle.getvalue()


def write_csv(results: list[QuestionResult], path: Path) -> None:
    path.write_text(csv_text(results), encoding="utf-8", newline="")


def write_answers(results: list[QuestionResult], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def _approach_order(result: BenchmarkResult) -> list[str]:
    seen: list[str] = []
    for row in result.results:
        if row.approach not in seen:
            seen.append(row.approach)
    return seen


def summary_rows(result: BenchmarkResult) -> list[dict[str, Any]]:
    rows = []
    setup = {s.approach: s for s in result.setup}
    for approach in _approach_order(result):
        mine = [r for r in result.results if r.approach == approach]
        judged = [r for r in mine if r.judge_correct is not None]
        setup_tokens = setup[approach].usage.get("total_tokens", 0) if approach in setup else 0
        rows.append(
            {
                "approach": approach,
                "questions": len(mine),
                "correct": sum(r.correct for r in mine),
                "fact_score": mean(r.facts_matched / r.facts_total for r in mine if r.facts_total)
                if mine
                else 0.0,
                "judge_correct": sum(bool(r.judge_correct) for r in judged) if judged else None,
                "judged": len(judged),
                "errors": sum(bool(r.error) for r in mine),
                "mean_input_tokens": mean(r.input_tokens for r in mine),
                "mean_output_tokens": mean(r.output_tokens for r in mine),
                "mean_total_tokens": mean(r.total_tokens for r in mine),
                "total_tokens": sum(r.total_tokens for r in mine),
                "setup_tokens": setup_tokens,
                "mean_tool_calls": mean(r.tool_calls for r in mine),
                "mean_latency_s": mean(r.latency_ms for r in mine) / 1000,
            }
        )
    return rows


def render_markdown(result: BenchmarkResult, run_info: dict[str, Any]) -> str:
    lines = ["# Benchmark results", ""]
    for key in ("started_at", "model", "questions", "commit", "github_ref"):
        if run_info.get(key) is not None:
            lines.append(f"- **{key.replace('_', ' ')}**: {run_info[key]}")
    lines += [
        "",
        "## Summary",
        "",
        "Correct = every key fact present. Fact score = mean share of key facts present.",
        "Tokens are per question, summed over every model call in the agent loop.",
        "",
        "| Approach | Correct | Fact score | Judge | Mean tokens (in / out) | Mean tool calls "
        "| Mean latency | Setup tokens | Errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary_rows(result):
        judge = (
            f"{row['judge_correct']}/{row['judged']}" if row["judge_correct"] is not None else "-"
        )
        lines.append(
            f"| {row['approach']} | {row['correct']}/{row['questions']} "
            f"| {row['fact_score']:.0%} | {judge} "
            f"| {row['mean_total_tokens']:,.0f} ({row['mean_input_tokens']:,.0f} / "
            f"{row['mean_output_tokens']:,.0f}) | {row['mean_tool_calls']:.1f} "
            f"| {row['mean_latency_s']:.1f} s | {row['setup_tokens']:,} | {row['errors']} |"
        )
    for name, reason in result.skipped.items():
        lines.append(f"| {name} | skipped: {reason} | | | | | | | |")

    approaches = _approach_order(result)
    if approaches:
        lines += ["", "## Per question (facts matched / total)", ""]
        lines.append("| Question | " + " | ".join(approaches) + " |")
        lines.append("| --- |" + " --- |" * len(approaches))
        by_key = {(r.question_id, r.approach): r for r in result.results}
        for question_id in dict.fromkeys(r.question_id for r in result.results):
            cells = []
            for approach in approaches:
                r = by_key.get((question_id, approach))
                cells.append(
                    "-"
                    if r is None
                    else f"{r.facts_matched}/{r.facts_total}{' ✓' if r.correct else ''}"
                )
            lines.append(f"| {question_id} | " + " | ".join(cells) + " |")

    if result.setup:
        lines += ["", "## Setup cost", ""]
        for s in result.setup:
            lines.append(
                f"- **{s.approach}**: {s.description}; {s.usage.get('total_tokens', 0):,} "
                f"tokens, {s.usage.get('latency_ms', 0) / 1000:.1f} s"
            )
    if result.judge_usage:
        lines.append(
            f"- **LLM judge** (not counted above): {result.judge_usage.get('total_tokens', 0):,}"
            " tokens"
        )
    return "\n".join(lines) + "\n"


def write_report(result: BenchmarkResult, out_dir: Path, run_info: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(result.results, out_dir / "results.csv")
    write_answers(result.results, out_dir / "answers.jsonl")
    summary = out_dir / "summary.md"
    summary.write_text(render_markdown(result, run_info), encoding="utf-8")
    (out_dir / "run.json").write_text(
        json.dumps(
            {
                **run_info,
                "skipped": result.skipped,
                "setup": [asdict(s) for s in result.setup],
                "judge_usage": result.judge_usage,
                "summary": summary_rows(result),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from repo_analyzer.agent.github_tools import GitHubClient, GitHubSource
from repo_analyzer.benchmark import __main__ as cli
from repo_analyzer.benchmark.approaches import APPROACHES
from repo_analyzer.benchmark.grading import grade_key_facts, judge_answer
from repo_analyzer.benchmark.questions import Question, load_questions
from repo_analyzer.benchmark.report import write_report
from repo_analyzer.benchmark.runner import BenchmarkConfig, BenchmarkRunner
from repo_analyzer.config import Settings
from support import FunctionChatModel, github_fixture_transport, tool_call, usage
from test_embeddings import KeywordEmbedder

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
QUESTIONS = load_questions(BENCHMARKS / "questions.yaml")


def test_key_facts_match_case_insensitively_with_alternatives() -> None:
    grade = grade_key_facts(
        "It reads the **`Orders`** table and caches for five minutes.",
        ["orders", "300 | 5 min | five min", "redis"],
    )
    assert grade.matched == ("orders", "300 | 5 min | five min")
    assert grade.missing == ("redis",)
    assert grade.score == pytest.approx(2 / 3)
    assert not grade.correct
    assert grade_key_facts("anything", []).correct is False


def test_every_reference_answer_grades_as_correct() -> None:
    for q in QUESTIONS.questions:
        assert grade_key_facts(q.expected_answer, q.key_facts).correct, q.id


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"correct": true, "reason": "matches"}', True),
        ('Sure. {"correct": false, "reason": "wrong TTL"}', False),
        ("I think it is right", None),
    ],
)
def test_judge_reads_its_verdict(reply: str, expected: bool | None) -> None:
    model = FunctionChatModel(respond=lambda _: AIMessage(content=reply))
    verdict = judge_answer(model, "q?", "reference", "answer")
    assert verdict.correct is expected


def find_question(text: str) -> Question:
    for q in QUESTIONS.questions:
        if text.startswith(q.question):
            return q
    raise AssertionError(f"unknown question {text[:60]!r}")


def fake_model(messages: list[BaseMessage]) -> AIMessage:
    """Stands in for Claude: writes metadata, judges, or answers each question in two steps."""
    system = str(messages[0].content) if isinstance(messages[0], SystemMessage) else ""
    if system.startswith("You document how data flows"):
        entries = [
            {"kind": "endpoint", "name": "POST /orders", "location": "app/routers/orders.py:40"}
        ]
        return AIMessage(content=json.dumps({"entries": entries}), usage_metadata=usage(5000, 300))
    if system.startswith("You grade answers"):
        return AIMessage(content='{"correct": true, "reason": "ok"}', usage_metadata=usage(400, 20))
    if not isinstance(messages[-1], ToolMessage):
        return tool_call("list_repositories", {}, usage=usage(1000, 50, cache_creation=800))
    human = [m for m in messages if isinstance(m, HumanMessage)][-1]
    answer = find_question(str(human.content)).expected_answer
    return AIMessage(content=answer, usage_metadata=usage(1300, 120, cache_read=800))


@pytest.fixture
def github() -> tuple[GitHubClient, dict[str, GitHubSource]]:
    transport, _ = github_fixture_transport(
        BENCHMARKS / "fixtures", owner="o", repo="r", ref="main", prefix="benchmarks/fixtures"
    )
    sources = {
        n: GitHubSource("o", "r", "main", f"benchmarks/fixtures/{n}")
        for n in ("shop-api", "shop-web")
    }
    return GitHubClient(token="t", transport=transport), sources


def config(tmp_path: Path, **overrides: object) -> BenchmarkConfig:
    values: dict[str, object] = {
        "settings": Settings(_env_file=None),
        "fixtures_dir": BENCHMARKS / "fixtures",
        "repositories": QUESTIONS.repositories,
        "questions": QUESTIONS.questions[:3],
        "work_dir": tmp_path / "work",
        "prompt_caching": False,
        "embedder_factory": KeywordEmbedder,
    }
    values.update(overrides)
    return BenchmarkConfig(**values)  # type: ignore[arg-type]


async def test_all_four_approaches_run_grade_and_report(
    tmp_path: Path, github: tuple[GitHubClient, dict[str, GitHubSource]]
) -> None:
    model = FunctionChatModel(respond=fake_model)
    client, sources = github
    runner = BenchmarkRunner(
        config(tmp_path, judge=True, github_client=client, github_sources=sources), lambda: model
    )

    result = await runner.run()

    assert result.skipped == {}
    assert [r.approach for r in result.results] == [a for a in APPROACHES for _ in range(3)]
    for row in result.results:
        assert row.correct and row.judge_correct, (row.approach, row.question_id)
        assert row.facts_matched == row.facts_total
        assert (row.input_tokens, row.output_tokens, row.total_tokens) == (2300, 170, 2470)
        assert (row.cache_read_tokens, row.cache_creation_tokens) == (800, 800)
        assert (row.llm_calls, row.tool_calls, row.tools_used) == (2, 1, ["list_repositories"])
        assert row.error == ""
    assert {s.approach for s in result.setup} == {"json-metadata", "json-embeddings"}
    metadata_cost = next(s for s in result.setup if s.approach == "json-metadata")
    assert metadata_cost.usage["llm_calls"] == 2  # one per repository
    assert result.judge_usage["llm_calls"] == 12

    # What each approach could see.
    first_calls = {}
    for call in model.calls:
        system = str(call[0].content)
        for name in APPROACHES:
            if (
                name not in first_calls
                and isinstance(call[0], SystemMessage)
                and (
                    "`get_endpoint_metadata`" in system
                    if name == "json-metadata"
                    else "GitHub API" in system
                    if name == "json-github"
                    else "There is no index" in system
                    if name == "tools-only"
                    else "`semantic_search`" in system
                )
            ):
                first_calls[name] = call
    embeddings_question = str(first_calls["json-embeddings"][1].content)
    assert "<retrieved_code" in embeddings_question and "shop-" in embeddings_question
    assert "<retrieved_code" not in str(first_calls["tools-only"][1].content)
    metadata_file = tmp_path / "work" / "data" / "repos" / "local__shop-api.metadata.json"
    assert json.loads(metadata_file.read_text())["entries"][0]["name"] == "POST /orders"

    summary = write_report(result, tmp_path / "out", {"model": "fake", "questions": 3})
    text = summary.read_text()
    assert "| json-github | 3/3 | 100% | 3/3 | 2,470 (2,300 / 170) | 1.0 |" in text
    assert "| summary-data-source | 5/5 ✓ | 5/5 ✓ | 5/5 ✓ | 5/5 ✓ |" in text
    rows = list(csv.DictReader((tmp_path / "out" / "results.csv").open()))
    assert len(rows) == 12 and rows[0]["total_tokens"] == "2470"
    assert len((tmp_path / "out" / "answers.jsonl").read_text().splitlines()) == 12
    assert json.loads((tmp_path / "out" / "run.json").read_text())["summary"][0]["correct"] == 3


async def test_approaches_without_prerequisites_are_skipped_not_faked(tmp_path: Path) -> None:
    model = FunctionChatModel(respond=fake_model)
    runner = BenchmarkRunner(
        config(
            tmp_path,
            approaches=["json-github", "json-embeddings", "tools-only"],
            embedder_factory=None,
            questions=QUESTIONS.questions[:1],
        ),
        lambda: model,
    )

    result = await runner.run()

    assert result.skipped == {
        "json-github": "GITHUB_TOKEN is not set",
        "json-embeddings": "no embedder configured",
    }
    assert [r.approach for r in result.results] == ["tools-only"]
    text = write_report(result, tmp_path / "out", {}).read_text()
    assert "| json-github | skipped: GITHUB_TOKEN is not set |" in text


async def test_wrong_answers_are_graded_as_wrong(tmp_path: Path) -> None:
    model = FunctionChatModel(respond=lambda _: AIMessage(content="It uses Redis."))
    runner = BenchmarkRunner(
        config(tmp_path, approaches=["tools-only"], questions=QUESTIONS.questions[:2]),
        lambda: model,
    )
    result = await runner.run()
    assert [r.correct for r in result.results] == [False, False]
    assert all(r.facts_matched == 0 and r.missing_facts for r in result.results)
    assert all(r.input_tokens == 0 for r in result.results)  # the model reported none


def test_cli_refuses_to_run_without_an_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    out = tmp_path / "results"
    assert cli.main(["--limit", "1", "--out", str(out)]) == cli.EXIT_NO_API_KEY
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not out.exists()


def test_cli_dry_run_indexes_the_fixtures_without_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "questions: 19" in printed
    assert "GITHUB_TOKEN: missing (json-github will be skipped)" in printed
    assert "indexed shop-api:" in printed and "indexed shop-web:" in printed

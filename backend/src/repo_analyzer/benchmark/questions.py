"""Load benchmarks/questions.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    category: str
    question: str
    expected_answer: str
    key_facts: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionSet:
    repositories: tuple[str, ...]
    questions: tuple[Question, ...]


def load_questions(path: Path) -> QuestionSet:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml comes with LangChain
        raise SystemExit('The benchmark needs PyYAML: pip install -e ".[benchmark]"') from exc
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{path} is not a version 1 questions file")
    questions = tuple(
        Question(
            id=str(q["id"]),
            category=str(q["category"]),
            question=str(q["question"]).strip(),
            expected_answer=str(q["expected_answer"]).strip(),
            key_facts=tuple(str(f) for f in q["key_facts"]),
            evidence=tuple(str(e) for e in q.get("evidence", [])),
        )
        for q in data["questions"]
    )
    return QuestionSet(tuple(str(r) for r in data["repositories"]), questions)

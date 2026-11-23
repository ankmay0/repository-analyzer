"""Grade an answer: automatic key-fact matching, plus an optional LLM judge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from repo_analyzer.agent.usage import UsageTracker


def _normalize(text: str) -> str:
    """Lowercase, drop markdown emphasis and code ticks, unify quotes and whitespace."""
    text = text.lower().replace("`", "").replace("*", "")
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"')
    text = text.replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text)


@dataclass(frozen=True, slots=True)
class FactGrade:
    matched: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.missing)

    @property
    def score(self) -> float:
        return len(self.matched) / self.total if self.total else 0.0

    @property
    def correct(self) -> bool:
        """Correct means every key fact is present."""
        return self.total > 0 and not self.missing


def grade_key_facts(answer: str, key_facts: tuple[str, ...] | list[str]) -> FactGrade:
    """A fact is present if any of its ``|``-separated alternatives is a substring."""
    haystack = _normalize(answer)
    matched: list[str] = []
    missing: list[str] = []
    for fact in key_facts:
        alternatives = [_normalize(a).strip() for a in fact.split("|")]
        (matched if any(a and a in haystack for a in alternatives) else missing).append(fact)
    return FactGrade(tuple(matched), tuple(missing))


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    correct: bool | None  # None when the judge's reply could not be read
    reason: str


_JUDGE_SYSTEM = """\
You grade answers to questions about a code base against a reference answer written \
from the code. Judge factual agreement only, not style or length. An answer that adds \
correct detail is fine; one that contradicts the reference or misses its central point \
is not. Reply with JSON only: {"correct": true or false, "reason": "<one sentence>"}\
"""


def judge_answer(
    model: BaseChatModel,
    question: str,
    expected_answer: str,
    answer: str,
    usage: UsageTracker | None = None,
) -> JudgeVerdict:
    prompt = (
        f"Question:\n{question}\n\nReference answer:\n{expected_answer}\n\n"
        f"Answer to grade:\n{answer or '(empty)'}"
    )
    response = model.invoke([SystemMessage(_JUDGE_SYSTEM), HumanMessage(prompt)])
    if isinstance(response, AIMessage) and usage is not None:
        usage.add_model_response(response)
    text = str(response.text) if isinstance(response, AIMessage) else ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("correct"), bool):
        return JudgeVerdict(None, f"Unreadable judge reply: {text[:200]!r}")
    return JudgeVerdict(data["correct"], str(data.get("reason", "")))

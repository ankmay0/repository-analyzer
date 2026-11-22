"""System prompt for the repository analysis agent.

The long, task-specific guidance now lives in Agent Skills (see ``skills.py``
and the ``skills/`` directory shipped with the server). ``BASE_PROMPT`` is the
always-on preamble; the catalogue of available skills is appended when the
process starts so the full prompt stays static and prompt-cacheable.
"""

from __future__ import annotations

from repo_analyzer.agent.skills import (  # noqa: F401 - re-exported for callers and tests
    BASE_PROMPT,
    SkillLibrary,
    build_system_prompt,
    default_skills_dir,
)

SYSTEM_PROMPT = build_system_prompt(SkillLibrary.load(default_skills_dir()))

"""Runtime configuration.

Every setting can be overridden with an ``RA_``-prefixed environment variable
or a ``.env`` file in the working directory. The Anthropic credential is the one
exception to the prefix: it uses the conventional ``ANTHROPIC_API_KEY`` name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_skills_dir() -> Path:
    """Packaged skills, or ``backend/skills`` when running from a source checkout."""
    here = Path(__file__).resolve().parent  # repo_analyzer/
    packaged = here / "skills"
    if packaged.is_dir():
        return packaged
    return here.parents[1] / "skills"  # backend/src/repo_analyzer -> backend/skills


def default_benchmarks_dir() -> Path:
    """``benchmarks/`` of a source checkout (questions and fixture apps), else under the cwd."""
    checkout = Path(__file__).resolve().parents[3] / "benchmarks"
    return checkout if checkout.is_dir() else Path.cwd() / "benchmarks"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Field(
        default=Path(".data"),
        description="Root for checkouts, the per-repository JSON indexes and workspace state.",
    )

    # --- Repository ingestion -------------------------------------------------
    allowed_git_hosts: tuple[str, ...] = ("github.com", "gitlab.com", "bitbucket.org")
    allow_local_paths: bool = Field(
        default=False,
        description=(
            "Accept local directories as repository sources. Off by default: with it on, "
            "any API caller can make the server read arbitrary directories."
        ),
    )
    git_clone_timeout_seconds: int = 300
    max_repositories: int = Field(default=10, ge=1)
    max_file_bytes: int = Field(default=200_000, ge=1)

    # --- Symbol extraction -------------------------------------------------
    ctags_path: str = Field(
        default="ctags",
        description=(
            "Path to the Universal Ctags binary used to extract functions, "
            "classes and imports. Indexing fails if this is missing or unusable."
        ),
    )

    # --- Agent skills ------------------------------------------------------------
    skills_dir: Path = Field(
        default_factory=default_skills_dir,
        description=(
            "Directory of Agent Skills (each a subdirectory with SKILL.md). "
            "Defaults to the packaged skills shipped with the server."
        ),
    )

    # --- Endpoint metadata --------------------------------------------------------
    endpoint_metadata: bool = Field(
        default=False,
        description=(
            "After indexing, ask the model once per repository for per-endpoint data-flow "
            "metadata (written next to the JSON index) and give the agent a tool to read it. "
            "Costs one model call per repository per indexing run."
        ),
    )

    # --- Benchmark -------------------------------------------------------------
    benchmarks_dir: Path = Field(
        default_factory=default_benchmarks_dir,
        description="Directory with questions.yaml and fixtures/ for the benchmark API and CLI.",
    )
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_TOKEN",
        description="Lets the json-github benchmark approach read the fixtures via the GitHub API.",
    )
    benchmark_github_repo: str = Field(
        default="ankmay0/repository-analyzer",
        description="owner/name that holds the benchmark fixtures, for json-github.",
    )
    benchmark_github_ref: str = Field(
        default="main", description="Branch, tag or commit that json-github reads."
    )
    benchmark_github_root: str = "benchmarks/fixtures"

    # --- LLM ------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    llm_model: str = "claude-opus-5-5"
    llm_max_tokens: int = Field(default=16_000, ge=1024)
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_timeout_seconds: float = 600.0
    agent_recursion_limit: int = Field(
        default=30, ge=4, description="Upper bound on model/tool steps for a single question."
    )

    # --- HTTP -----------------------------------------------------------------
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)

    @property
    def checkouts_dir(self) -> Path:
        """Where remote repositories are cloned."""
        return self.data_dir / "checkouts"

    @property
    def repos_dir(self) -> Path:
        """Where the per-repository JSON indexes (``<owner>__<name>.json``) are written."""
        return self.data_dir / "repos"

    @property
    def workspace_file(self) -> Path:
        return self.data_dir / "workspace.json"

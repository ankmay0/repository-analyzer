"""Application error hierarchy.

``AnalyzerError`` subclasses carry messages that are safe to show to an API
caller. Anything else that escapes is a bug and is reported as an opaque 500.
"""

from __future__ import annotations


class AnalyzerError(Exception):
    """Base class for expected, user-presentable failures."""


class InvalidRepositorySourceError(AnalyzerError):
    """The repository source was rejected before any network or disk access."""


class RepositoryFetchError(AnalyzerError):
    """The repository could not be cloned or opened."""


class IndexIncompatibleError(AnalyzerError):
    """The on-disk vector index was built with a different embedding model."""


class UnknownRepositoryError(AnalyzerError):
    """A repository name does not exist in the current workspace."""


class IndexingInProgressError(AnalyzerError):
    """An indexing job is already running."""


class LLMNotConfiguredError(AnalyzerError):
    """No Anthropic credential is available to the server process."""


class WorkspaceEmptyError(AnalyzerError):
    """A question was asked before any repository was indexed."""


class ReindexRequiredError(AnalyzerError):
    """The stored index does not match the configured knowledge mode."""

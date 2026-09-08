"""Search backend failures."""

from __future__ import annotations


class SearchError(Exception):
    """Base class for search failures."""


class InvalidSearchModeError(SearchError):
    """An unknown search mode was requested.

    Surfaced by the service; the API layer maps it to 422 later.
    """


class InvalidSearchRequestError(SearchError):
    """A request value is out of range or malformed."""


class SemanticSearchUnavailableError(SearchError):
    """The local embedding model could not be loaded.

    Only the semantic path is affected: lexical search and browsing keep
    working, so a missing model degrades the service rather than stopping it.
    """

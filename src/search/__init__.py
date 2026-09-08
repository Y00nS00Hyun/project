"""ACL-aware search backend.

    authenticated user -> ACL filter -> current READY revisions
        -> vector / pg_trgm retrieval -> document aggregation -> ranked page

ACL is applied before retrieval, never after. FastAPI endpoints, RAG and the
UI are later stages and are deliberately absent.
"""

from __future__ import annotations

from .exceptions import (
    InvalidSearchModeError,
    InvalidSearchRequestError,
    SearchError,
    SemanticSearchUnavailableError,
)
from .models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SNIPPET_MAX_CHARS,
    DocumentResult,
    MatchedChunk,
    SearchMode,
    SearchRequest,
    SearchResult,
    make_snippet,
)
from .query_embedding import LocalQueryEmbedder, QueryEmbedder
from .repository import READ_PERMISSIONS, SearchRepository
from .service import SearchService

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "READ_PERMISSIONS",
    "SNIPPET_MAX_CHARS",
    "DocumentResult",
    "InvalidSearchModeError",
    "InvalidSearchRequestError",
    "LocalQueryEmbedder",
    "MatchedChunk",
    "QueryEmbedder",
    "SearchError",
    "SearchMode",
    "SearchRepository",
    "SearchRequest",
    "SearchResult",
    "SearchService",
    "SemanticSearchUnavailableError",
    "make_snippet",
]

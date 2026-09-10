"""Search backend request and result models.

These are the *internal* service contract. Translation to the API Contract v1
response shape happens in the API layer, which is a later stage -- which is why
the retrieval score lives here but must not survive into a public response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .exceptions import InvalidSearchModeError, InvalidSearchRequestError

#: Pagination bounds, matching API Contract v1 section 4.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: documents.file_type CHECK domain. Lower case, matching the column and the
#: API contract -- translating case here would create two vocabularies.
FILE_TYPES = ("hwp", "hwpx", "docx", "pdf")

#: How much of the matched chunk is returned as a snippet. Enough to show
#: context, short enough that a result list is not a bulk content export.
SNIPPET_MAX_CHARS = 200


class SearchMode(str, Enum):
    """Retrieval route.

    ``DEFAULT`` is resolved by the service rather than being an alias, so the
    default route can change (to RRF, say) without changing this contract or
    any caller.
    """

    DEFAULT = "default"
    SEMANTIC = "semantic"
    LEXICAL = "lexical"

    @classmethod
    def parse(cls, value: str | None) -> "SearchMode":
        if value is None or value == "":
            return cls.DEFAULT
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise InvalidSearchModeError(
                f"unknown search mode {value!r}; allowed: {allowed}"
            ) from exc


@dataclass(frozen=True)
class SearchRequest:
    """One search, already bound to an authenticated user.

    ``user_id`` is supplied by the caller from the authenticated session. The
    service never accepts an identity from request data -- there is no field
    here a client could set to search as somebody else.
    """

    user_id: str
    query: str | None = None
    mode: SearchMode = SearchMode.DEFAULT

    department_id: str | None = None
    year: int | None = None
    tag_ids: tuple[int, ...] = ()
    file_type: str | None = None
    #: Canonical folder path. Restricts results to that folder's whole subtree.
    #: Validated in __post_init__, never interpolated into SQL.
    folder_path: str | None = None

    page: int = 1
    size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        if not self.user_id:
            raise InvalidSearchRequestError("user_id is required")
        if self.page < 1:
            raise InvalidSearchRequestError("page must be >= 1")
        if not (1 <= self.size <= MAX_PAGE_SIZE):
            raise InvalidSearchRequestError(f"size must be between 1 and {MAX_PAGE_SIZE}")
        if self.year is not None and not (1900 <= self.year <= 2100):
            raise InvalidSearchRequestError("year must be between 1900 and 2100")
        if self.file_type is not None and self.file_type not in FILE_TYPES:
            raise InvalidSearchRequestError(
                f"file_type must be one of {', '.join(FILE_TYPES)}"
            )
        if self.folder_path is not None:
            # Rejects absolute paths, '..', empty segments and anything that is
            # not a canonical path. Frozen dataclass, so the normalized value is
            # set through object.__setattr__.
            from .folder_paths import normalize_folder_path

            object.__setattr__(self, "folder_path", normalize_folder_path(self.folder_path))

    @property
    def normalized_query(self) -> str | None:
        """The query with surrounding whitespace removed, or None if blank."""
        if self.query is None:
            return None
        stripped = self.query.strip()
        return stripped or None

    @property
    def is_browse(self) -> bool:
        """No query text: filter browsing, not retrieval."""
        return self.normalized_query is None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def folder_prefix(self) -> str | None:
        """The LIKE pattern for this request's folder, already escaped."""
        if self.folder_path is None:
            return None
        from .folder_paths import subtree_prefix

        return subtree_prefix(self.folder_path)


@dataclass(frozen=True)
class MatchedChunk:
    """The chunk that produced a document's score."""

    chunk_id: str
    chunk_index: int
    paragraph_start: int | None
    paragraph_end: int | None
    section_title: str | None
    page_number: int | None
    snippet: str


@dataclass(frozen=True)
class Tag:
    id: int
    name: str


@dataclass(frozen=True)
class DocumentResult:
    """One document in a result page."""

    document_id: str
    revision_id: str
    title: str
    file_type: str
    department_id: str | None
    department_name: str | None
    year: int | None
    updated_at: datetime

    revision_no: int | None = None
    revision_created_at: datetime | None = None
    #: A newer revision exists but is not READY yet, so search still serves the
    #: current one. Not a staleness warning.
    has_newer_revision: bool = False
    tags: tuple[Tag, ...] = ()

    matched_chunk: MatchedChunk | None = None

    #: Raw retrieval score. Deliberately NOT named `confidence`: cosine
    #: similarity and trigram similarity are different, incomparable scales,
    #: and the search PoC showed cosine stays high even for a query with no
    #: real answer. It exists to order results, nothing more, and the API layer
    #: must not pass it through.
    retrieval_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """A page of documents plus what produced it."""

    items: list[DocumentResult] = field(default_factory=list)
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE
    total: int = 0
    mode: SearchMode = SearchMode.DEFAULT
    #: The route actually taken, after DEFAULT was resolved.
    resolved_mode: SearchMode = SearchMode.SEMANTIC

    query_embedding_ms: float | None = None
    retrieval_ms: float | None = None


def make_snippet(text: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    """Trim chunk text down to a display snippet.

    A plain prefix, not a highlight engine: no term marking, no re-ranking of
    sentences. Returning whole chunks would turn a result list into a bulk
    export of document bodies.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"

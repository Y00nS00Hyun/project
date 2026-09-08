"""Search service.

Routes a request to browse, semantic or lexical retrieval, and assembles the
result page. All three paths go through the same ACL-first repository queries.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import psycopg

from ingestion.config import IngestionConfig

from .models import (
    DocumentResult,
    MatchedChunk,
    SearchMode,
    SearchRequest,
    SearchResult,
    make_snippet,
)
from .query_embedding import LocalQueryEmbedder
from .repository import SearchRepository

logger = logging.getLogger("search")

#: Design Freeze v1: the default route is semantic (vector). Resolved here
#: rather than aliased in the enum, so changing it later (to RRF, say) does not
#: change the request contract or break callers.
DEFAULT_RESOLVED_MODE = SearchMode.SEMANTIC


class SearchService:
    """ACL-aware document search."""

    def __init__(
        self,
        connection_factory,
        config: IngestionConfig,
        query_embedder: Any | None = None,
    ):
        self.connection_factory = connection_factory
        self.config = config
        self._embedder = query_embedder or LocalQueryEmbedder(config)

    def search(self, request: SearchRequest) -> SearchResult:
        started = time.perf_counter()
        resolved = self._resolve_mode(request.mode)

        embedding_ms = None
        query_vector = None
        if not request.is_browse and resolved is SearchMode.SEMANTIC:
            # Outside the DB call: model inference should not hold a connection.
            embed_started = time.perf_counter()
            query_vector = self._embedder.embed_query_literal(request.normalized_query)
            embedding_ms = round((time.perf_counter() - embed_started) * 1000, 3)

        retrieval_started = time.perf_counter()
        with self.connection_factory() as conn:
            conn.autocommit = False
            try:
                rows, total = self._retrieve(conn, request, resolved, query_vector)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 3)

        result = SearchResult(
            items=[self._to_document_result(row) for row in rows],
            page=request.page,
            size=request.size,
            total=total,
            mode=request.mode,
            resolved_mode=SearchMode.DEFAULT if request.is_browse else resolved,
            query_embedding_ms=embedding_ms,
            retrieval_ms=retrieval_ms,
        )

        # The query text itself may be internal information, so it is not
        # logged; a short digest keeps requests correlatable without the words.
        logger.info(
            "search.completed",
            extra={
                "user_id": request.user_id,
                "mode": result.resolved_mode.value,
                "browse": request.is_browse,
                "result_count": len(result.items),
                "total": total,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "query_digest": self._query_digest(request.normalized_query),
            },
        )
        return result

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _resolve_mode(mode: SearchMode) -> SearchMode:
        return DEFAULT_RESOLVED_MODE if mode is SearchMode.DEFAULT else mode

    def _retrieve(self, conn: psycopg.Connection, request, resolved, query_vector):
        repo = SearchRepository(conn)
        common = {
            "user_id": request.user_id,
            "department_id": request.department_id,
            "year": request.year,
            "tag_ids": request.tag_ids,
            "limit": request.size,
            "offset": request.offset,
        }

        if request.is_browse:
            # No query text -> no retrieval. The embedding model is not touched.
            return repo.browse(**common)

        if resolved is SearchMode.LEXICAL:
            return repo.lexical_search(
                query_text=request.normalized_query,
                threshold=self.config.trigram_threshold,
                **common,
            )

        return repo.semantic_search(query_vector=query_vector, **common)

    @staticmethod
    def _to_document_result(row: dict[str, Any]) -> DocumentResult:
        matched = None
        if row.get("chunk_id") is not None:
            matched = MatchedChunk(
                chunk_id=str(row["chunk_id"]),
                chunk_index=row["chunk_index"],
                paragraph_start=row["paragraph_start"],
                paragraph_end=row["paragraph_end"],
                section_title=row["section_title"],
                page_number=row["page_number"],
                snippet=make_snippet(row["chunk_text"] or ""),
            )
        score = row.get("score")
        return DocumentResult(
            document_id=str(row["document_id"]),
            revision_id=str(row["revision_id"]),
            title=row["title"],
            file_type=row["file_type"],
            department_id=str(row["department_id"]) if row["department_id"] else None,
            department_name=row["department_name"],
            year=row["document_year"],
            updated_at=row["updated_at"],
            matched_chunk=matched,
            retrieval_score=float(score) if score is not None else None,
        )

    @staticmethod
    def _query_digest(query: str | None) -> str | None:
        if not query:
            return None
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]

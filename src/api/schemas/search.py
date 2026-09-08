"""Search response schemas (API Contract v1 section 6.5)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from search.models import DocumentResult

from .common import Anchor, DepartmentRef, NoAnchor, ParagraphAnchor, RevisionRef, TagRef


def build_anchor(matched) -> Anchor:
    """Choose the anchor kind from what the parser actually recovered.

    Never invents a position. HWP/HWPX carry paragraph anchors; a page number is
    only emitted when one genuinely exists, and when neither is available the
    contract's explicit `none` form is used rather than a guess.
    """
    if matched.paragraph_start is not None:
        return ParagraphAnchor(
            paragraph_index=matched.paragraph_start,
            paragraph_end=(
                matched.paragraph_end
                if matched.paragraph_end is not None
                and matched.paragraph_end != matched.paragraph_start
                else None
            ),
        )
    if matched.page_number is not None:
        from .common import PageAnchor

        return PageAnchor(page_number=matched.page_number)
    return NoAnchor()


class MatchedChunkOut(BaseModel):
    chunk_id: str
    revision_id: str
    section_title: str | None = None
    anchor: Anchor


class SearchItemOut(BaseModel):
    """One document in a search or browse page.

    Deliberately absent: any retrieval score. Cosine and trigram similarity are
    incomparable scales, and the search PoC measured cosine 0.816 for a query
    with no real answer -- exposing that as a number invites the UI to present
    it as confidence.
    """

    document_id: str
    title: str
    file_type: str
    department: DepartmentRef | None = None
    tags: list[TagRef] = []
    updated_at: datetime
    current_revision: RevisionRef | None = None
    has_newer_revision: bool = False
    snippet: str | None = None
    matched_chunk: MatchedChunkOut | None = None


class SearchResponse(BaseModel):
    items: list[SearchItemOut]
    page: int
    size: int
    total: int


def to_search_item(result: DocumentResult) -> SearchItemOut:
    department = None
    if result.department_id:
        department = DepartmentRef(id=result.department_id, name=result.department_name or "")

    current_revision = None
    if result.revision_no is not None and result.revision_created_at is not None:
        current_revision = RevisionRef(
            revision_id=result.revision_id,
            revision_no=result.revision_no,
            created_at=result.revision_created_at,
        )

    matched = None
    snippet = None
    if result.matched_chunk is not None:
        snippet = result.matched_chunk.snippet
        matched = MatchedChunkOut(
            chunk_id=result.matched_chunk.chunk_id,
            revision_id=result.revision_id,
            section_title=result.matched_chunk.section_title,
            anchor=build_anchor(result.matched_chunk),
        )

    return SearchItemOut(
        document_id=result.document_id,
        title=result.title,
        file_type=result.file_type,
        department=department,
        tags=[TagRef(id=t.id, name=t.name) for t in result.tags],
        updated_at=result.updated_at,
        current_revision=current_revision,
        has_newer_revision=result.has_newer_revision,
        snippet=snippet,
        matched_chunk=matched,
    )

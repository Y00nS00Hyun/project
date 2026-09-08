"""Document detail and revision history schemas (contract section 8)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .common import DepartmentRef, RevisionRef, TagRef, UserRef


class DocumentDetailOut(BaseModel):
    """Contract section 8.1.

    Not present, on purpose: `source_path`, `original_filename`,
    `extracted_text`, `parsed_structure`, `content_hash`, embeddings and any
    parser internals. They expose the shared-folder layout or internal
    processing detail that the user has no use for.
    """

    document_id: str
    title: str
    file_type: str
    department: DepartmentRef | None = None
    owner: UserRef | None = None
    tags: list[TagRef] = []
    created_at: datetime
    updated_at: datetime
    current_revision: RevisionRef | None = None
    latest_revision: RevisionRef | None = None
    is_searchable: bool
    downloadable: bool


class RevisionOut(BaseModel):
    """Contract section 8.2.

    `parse_status` is the worker's execution state; `parse_result_code` is what
    the parse *means*. SUCCESS + OCR_REQUIRED is a normal combination -- the
    parser correctly determined the document is a scan -- so the two are never
    merged into one field.

    `error_message` is not exposed: it can carry internal detail.
    """

    revision_id: str
    revision_no: int
    content_hash: str
    file_size: int | None = None
    source_modified_at: datetime | None = None
    parse_status: str
    parse_result_code: str | None = None
    is_current: bool
    is_ready: bool
    created_at: datetime


class RevisionListResponse(BaseModel):
    items: list[RevisionOut]
    page: int
    size: int
    total: int

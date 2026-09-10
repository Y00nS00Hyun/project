"""Document detail and revision history schemas (contract section 8)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .common import DepartmentRef, RevisionRef, TagRef, UserRef


class SummaryOut(BaseModel):
    """The current revision's precomputed summary (contract v1.2).

    `state` is what happened to this revision's summary; `available` is whether
    this deployment can produce summaries at all. They are separate because a
    reader needs different words for "being written" and "no summaries here",
    and because the second is a property of the installation, not of the
    document -- it is the capability field, so that turning generation off never
    needs a new value in the database's status CHECK.

    Not present, on purpose: which provider or model wrote it, and the prompt
    version. Those are operational facts about our pipeline; exposing them
    invites a reader to weigh an answer by its model name, and tells anyone who
    asks what we send documents to.
    """

    #: PENDING | RUNNING | SUCCESS | FAILED | SKIPPED, mirroring the revision.
    state: str
    #: Only ever set when state is SUCCESS.
    content: str | None = None
    generated_at: datetime | None = None
    #: False when no generation provider is configured in this deployment.
    available: bool
    #: The revision this summary describes; a newer revision gets its own.
    revision_id: str | None = None


class ChatCapabilityOut(BaseModel):
    """Whether this deployment can answer questions about documents at all.

    A capability, like `SummaryOut.available`, and true for exactly the same
    two reasons: a provider is configured, and this corpus is cleared to be
    sent to one. Both features put document text into a provider request, so
    neither may be enabled without the other.

    It exists so the UI can disable the question box *before* somebody types a
    question. Without it the only way to discover the feature is off is to ask
    something and have it fail, which is the worst moment to find out.
    """

    available: bool


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
    summary: SummaryOut
    chat: ChatCapabilityOut


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

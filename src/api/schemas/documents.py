"""Document detail and revision history schemas (contract section 8)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from .common import Anchor, DepartmentRef, RevisionRef, TagRef, UserRef


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
    #: Why, when the state alone is ambiguous. SKIPPED covers three different
    #: situations that need three different sentences on screen, and without
    #: this a document that was too large to summarize is described to the
    #: reader as having no text in it.
    #:
    #: A small closed vocabulary of its own, not the worker's internal
    #: result_code passed through: this is an explanation owed to a reader, so
    #: it must stay stable even when the pipeline's codes change.
    #:
    #: NO_TEXT | TOO_LARGE | PROVIDER_DISABLED | null
    reason: str | None = None
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
    #: When this system first registered the file. Not the file's own age:
    #: a document written in 2019 and discovered today reads 2026 here.
    created_at: datetime
    #: When this system last changed anything about the row -- a new revision,
    #: a promotion, the file being seen again. Not when the file's contents
    #: changed; `source_modified_at` is that.
    updated_at: datetime
    #: Size of the current revision's file, in bytes.
    #:
    #: The revision search is serving, like every other figure here -- not the
    #: newest file on disk. Somebody deciding whether to download wants to know
    #: what they would actually get.
    file_size: int | None = None
    #: The filesystem mtime of the current revision's file, as the shared
    #: folder reports it. What a person means by "수정일".
    source_modified_at: datetime | None = None
    #: The date printed on the document itself, when its front matter states
    #: one outright. NULL far more often than not, and deliberately so -- a
    #: cover reading "2026년도 사업" states a year, not a day, and 2026-01-01
    #: would be this system inventing a fact.
    document_date: date | None = None
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


class TextBlockOut(BaseModel):
    """One piece of the extracted text, in document order.

    A chunk, which is the same unit search matches and citations point at -- so
    a block shown here and a source quoted in an answer describe the same place
    the same way.
    """

    chunk_index: int
    text: str
    section_title: str | None = None
    anchor: Anchor


class TextPreviewResponse(BaseModel):
    """Extracted text, a page at a time.

    NOT a rendering of the original file. Tables, columns and layout are gone;
    what remains is the text the parser could read, which is also exactly what
    search and citations work from. Showing it is how a reader checks whether
    the system understood the document.

    Deliberately paged and absent from the document detail response. A 200,000
    character report would otherwise be sent to every visitor who opened the
    page, most of whom only wanted the title.
    """

    #: The revision this text came from -- always the current READY one. Null
    #: when nothing is ready, in which case `items` is empty.
    revision_id: str | None = None
    items: list[TextBlockOut] = []
    offset: int = 0
    total: int = 0
    has_more: bool = False

"""Public Chat Contract v1 section 9. Identity comes only from authentication."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import ApiError
from .common import Anchor


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str | None = None
    #: Optional. Present -> the session may only draw on this one document;
    #: absent -> the existing whole-corpus session. Checked against the
    #: caller's read permission before the session is created.
    #:
    #: strict is relaxed for this field alone. JSON has no UUID type, so under
    #: the model's strict setting a perfectly ordinary '"document_id": "…"'
    #: would be rejected as "not an instance of UUID". Parsing the string is
    #: still exact -- a malformed one is a 422 here rather than reaching SQL.
    document_id: UUID | None = Field(default=None, strict=False)


class AccessibleScope(BaseModel):
    """A scoped session whose document the caller may still read."""
    document_id: UUID
    title: str
    accessible: Literal[True]


class InaccessibleScope(BaseModel):
    """A scoped session whose document the caller may no longer read.

    Carries no title by construction, the same rule historical citations
    follow: losing access to a document must also hide its name.
    """
    document_id: UUID
    accessible: Literal[False]


DocumentScope = Annotated[AccessibleScope | InaccessibleScope, Field(discriminator='accessible')]


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator('message', mode='before')
    @classmethod
    def check_message(cls, value):
        if isinstance(value, str):
            if len(value) > 4000:
                raise ApiError('CHAT_MESSAGE_TOO_LONG', '질문은 최대 4000자까지 입력할 수 있습니다.')
            if not value.strip():
                raise ValueError('질문을 입력해 주세요.')
        return value


class SessionOut(BaseModel):
    session_id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    #: null for an ordinary whole-corpus session.
    document_scope: DocumentScope | None = None


class SessionListItem(SessionOut):
    message_count: int


class SessionListResponse(BaseModel):
    items: list[SessionListItem]
    page: int
    size: int
    total: int


class AccessibleSource(BaseModel):
    document_id: UUID
    revision_id: UUID
    chunk_id: UUID
    title: str
    file_type: str
    section_title: str | None
    anchor: Anchor
    accessible: Literal[True]


class InaccessibleSource(BaseModel):
    document_id: UUID
    revision_id: UUID
    chunk_id: UUID
    accessible: Literal[False]


Source = Annotated[AccessibleSource | InaccessibleSource, Field(discriminator='accessible')]


class PlainMessage(BaseModel):
    message_id: UUID
    role: Literal['user', 'system']
    content: str
    created_at: datetime


class AssistantMessage(BaseModel):
    message_id: UUID
    role: Literal['assistant']
    content: str | None
    refused: bool
    has_inaccessible_sources: bool
    content_hidden: bool
    sources: list[Source]
    created_at: datetime


Message = Annotated[PlainMessage | AssistantMessage, Field(discriminator='role')]


class MessagePage(BaseModel):
    items: list[Message]
    page: int
    size: int
    total: int


class SessionDetail(SessionOut):
    messages: MessagePage


class SendMessageResponse(BaseModel):
    message_id: UUID
    answer: str
    refused: bool
    sources: list[AccessibleSource]
    created_at: datetime

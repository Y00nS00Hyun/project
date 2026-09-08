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

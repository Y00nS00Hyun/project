"""Shared response schemas (API Contract v1 section 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class DepartmentRef(BaseModel):
    id: str
    name: str


class UserRef(BaseModel):
    id: str
    name: str | None = None


class TagRef(BaseModel):
    #: tags.id is SERIAL in the schema -- the one documented exception to
    #: "every id is a UUID".
    id: int
    name: str


class RevisionRef(BaseModel):
    revision_id: str
    revision_no: int
    created_at: datetime


# ---------------------------------------------------------------------------
# Citation anchors (contract section 5.4): a discriminated union.
#
# A missing page_number is not an error -- it is the normal state for HWP/HWPX,
# whose formats do not store page numbers. Clients branch on `type` and must
# never assume page_number exists.
# ---------------------------------------------------------------------------

class ParagraphAnchor(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    paragraph_index: int
    paragraph_end: int | None = None


class PageAnchor(BaseModel):
    type: Literal["page"] = "page"
    page_number: int = Field(ge=1)


class NoAnchor(BaseModel):
    type: Literal["none"] = "none"


Anchor = Annotated[Union[ParagraphAnchor, PageAnchor, NoAnchor], Field(discriminator="type")]


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[dict] | None = None


class ErrorResponse(BaseModel):
    """The only error shape this API returns."""

    error: ErrorBody

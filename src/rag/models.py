"""Internal evidence and provider contracts, separate from HTTP models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


class ParagraphAnchor(TypedDict):
    type: Literal['paragraph']
    paragraph_index: int
    paragraph_end: int | None


class PageAnchor(TypedDict):
    type: Literal['page']
    page_number: int


class NoAnchor(TypedDict):
    type: Literal['none']


Anchor = ParagraphAnchor | PageAnchor | NoAnchor


def source_anchor(file_type: str, start: int | None, end: int | None, page: int | None) -> Anchor:
    if file_type in ('hwp', 'hwpx', 'docx') and start is not None:
        return {'type': 'paragraph', 'paragraph_index': start, 'paragraph_end': end}
    if file_type == 'pdf' and page is not None and page >= 1:
        return {'type': 'page', 'page_number': page}
    return {'type': 'none'}


@dataclass(frozen=True)
class EvidenceChunk:
    document_id: str
    revision_id: str
    chunk_id: str
    title: str
    file_type: str
    section_title: str | None
    anchor: Anchor
    text: str


class GenerationResult(BaseModel):
    """Strict structured output; never salvage a malformed model answer."""
    model_config = ConfigDict(extra='forbid', strict=True, revalidate_instances='always')
    answerable: StrictBool
    answer: StrictStr = Field(max_length=8000)
    citation_chunk_ids: list[StrictStr] = Field(max_length=100)


@dataclass(frozen=True)
class ValidatedAnswer:
    answer: str
    refused: bool
    citation_chunk_ids: tuple[str, ...] = ()

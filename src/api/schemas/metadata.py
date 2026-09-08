"""Tag and department list schemas (contract sections 10, 11)."""

from __future__ import annotations

from pydantic import BaseModel

from .common import DepartmentRef, TagRef


class TagListResponse(BaseModel):
    items: list[TagRef]
    page: int
    size: int
    total: int


class DepartmentListResponse(BaseModel):
    """The one list endpoint without pagination.

    Contract section 11: departments are few, so the whole set is returned and
    `page`/`size`/`total` are deliberately absent.
    """

    items: list[DepartmentRef]

"""Filter vocabulary: tags and departments."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Query

from ..dependencies import AuthenticatedUser, get_connection, require_user
from ..document_service import list_departments, list_tags
from ..schemas.common import DepartmentRef, TagRef
from ..schemas.metadata import DepartmentListResponse, TagListResponse

router = APIRouter(tags=["metadata"])


@router.get("/tags", response_model=TagListResponse, summary="태그 목록")
def get_tags(
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> TagListResponse:
    items, total = list_tags(conn, limit=size, offset=(page - 1) * size)
    return TagListResponse(
        items=[TagRef(**item) for item in items], page=page, size=size, total=total
    )


@router.get("/departments", response_model=DepartmentListResponse, summary="부서 목록")
def get_departments(
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> DepartmentListResponse:
    """No pagination: contract section 11 returns the whole (small) set."""
    return DepartmentListResponse(
        items=[DepartmentRef(**item) for item in list_departments(conn)]
    )

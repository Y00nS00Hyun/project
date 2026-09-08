"""Document detail, revision history and download.

There is no POST/PUT/PATCH/DELETE here and there never will be in v1: the
shared folder is the source of truth, and the system does not modify it.
"""

from __future__ import annotations

import mimetypes

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from ..dependencies import AuthenticatedUser, get_config, get_connection, require_user
from ..document_service import (
    DocumentNotDownloadable,
    DocumentNotVisible,
    DocumentService,
)
from ..errors import document_not_found, not_downloadable, validation_error
from ..schemas.documents import DocumentDetailOut, RevisionListResponse, RevisionOut

router = APIRouter(prefix="/documents", tags=["documents"])

#: Sensible defaults per file type; mimetypes does not know the Hancom ones.
_CONTENT_TYPES = {
    "hwp": "application/x-hwp",
    "hwpx": "application/hwp+zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def _service(conn: psycopg.Connection) -> DocumentService:
    return DocumentService(conn, get_config().resolved_root)


@router.get("/{document_id}", response_model=DocumentDetailOut, summary="문서 상세")
def get_document(
    document_id: str,
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> DocumentDetailOut:
    try:
        detail = _service(conn).get_detail(user.user_id, document_id)
    except DocumentNotVisible as exc:
        # 404 for both "absent" and "not permitted": 403 would confirm the
        # document exists (contract section 3.3).
        raise document_not_found() from exc
    return DocumentDetailOut(**detail)


@router.get(
    "/{document_id}/revisions",
    response_model=RevisionListResponse,
    summary="revision 이력",
)
def list_revisions(
    request: Request,
    document_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> RevisionListResponse:
    unknown = sorted(set(request.query_params.keys()) - {"page", "size"})
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )
    try:
        items, total = _service(conn).list_revisions(
            user.user_id, document_id, limit=size, offset=(page - 1) * size
        )
    except DocumentNotVisible as exc:
        raise document_not_found() from exc
    return RevisionListResponse(
        items=[RevisionOut(**item) for item in items], page=page, size=size, total=total
    )


@router.get("/{document_id}/download", summary="원본 다운로드")
def download_document(
    document_id: str,
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> FileResponse:
    """Serve the current revision's original file, read-only.

    ACL is checked before any path is resolved or opened, and the stored path
    is re-validated against the shared root. No filesystem path appears in the
    response or its headers -- only the display filename.
    """
    try:
        target = _service(conn).resolve_download(user.user_id, document_id)
    except DocumentNotVisible as exc:
        raise document_not_found() from exc
    except DocumentNotDownloadable as exc:
        raise not_downloadable(str(exc)) from exc

    media_type = _CONTENT_TYPES.get(target.file_type) or (
        mimetypes.guess_type(target.filename)[0] or "application/octet-stream"
    )
    return FileResponse(
        path=target.absolute_path,
        media_type=media_type,
        filename=target.filename,
    )

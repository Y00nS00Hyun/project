"""Document detail, revision history, download, and administrator relocation.

The shared folder is the source of truth. The one route here that changes it is
POST /relocate: an administrator renaming or moving an original file, only with
DOCUMENT_FILE_MANAGEMENT_ENABLED and a separate write mount. There is no
PUT/PATCH/DELETE, no upload and no content editing.
"""

from __future__ import annotations

import mimetypes

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from ..dependencies import (
    AuthenticatedUser, connection_factory, get_config, get_connection,
    require_admin_user, require_user,
)
from ..document_service import (
    DocumentNotDownloadable,
    DocumentNotVisible,
    DocumentService,
)
from ..errors import ApiError, document_not_found, not_downloadable, validation_error
from ..file_management import (
    DestinationExists, DocumentBusy, DocumentNotRelocatable, InvalidRelocation,
    RelocationUnavailable, SourceFileMissing, file_management_available, load_config, relocate,
)
from ..schemas.common import ErrorResponse
from ..schemas.documents import (
    DocumentDetailOut,
    RevisionListResponse,
    RelocateRequest,
    RelocateResponse,
    RevisionDiffResponse,
    RevisionOut,
    TextPreviewResponse,
)

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
    detail["file_management"] = {
        "available": file_management_available(user.user_id, connection_factory()),
    }
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


#: Blocks per preview request. Enough to fill a screen and judge the document,
#: small enough that opening one is not a bulk export of its body.
PREVIEW_PAGE_SIZE = 20
MAX_PREVIEW_PAGE_SIZE = 50


@router.get(
    "/{document_id}/text",
    response_model=TextPreviewResponse,
    summary="추출 텍스트 미리보기",
)
def preview_text(
    request: Request,
    document_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(PREVIEW_PAGE_SIZE, ge=1, le=MAX_PREVIEW_PAGE_SIZE),
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> TextPreviewResponse:
    """The text the parser extracted from the current READY revision.

    Its own endpoint rather than a field on the detail response: the text is
    large, and most visitors to a document page never ask for it.

    Nothing here reaches an LLM. It reads rows the ingestion pipeline already
    wrote and returns them.
    """
    unknown = sorted(set(request.query_params.keys()) - {"offset", "limit"})
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )
    try:
        # ACL is checked inside, on the logical document, before any body text
        # is read.
        payload = _service(conn).text_preview(
            user.user_id, document_id, offset=offset, limit=limit,
        )
    except DocumentNotVisible as exc:
        raise document_not_found() from exc
    # Deliberately not logged. The one thing this endpoint returns is document
    # body text, and a log line is the easiest place for it to escape.
    return TextPreviewResponse(**payload)


#: Per side. Enough for any document in the current corpus to be read in full;
#: a rewrite larger than this is reported as truncated with exact totals.
MAX_DIFF_ITEMS = 200


@router.get(
    "/{document_id}/diff",
    response_model=RevisionDiffResponse,
    summary="이전 버전과 변경사항 비교",
)
def revision_diff(
    request: Request,
    document_id: str,
    user: AuthenticatedUser = Depends(require_user),
    conn: psycopg.Connection = Depends(get_connection),
) -> RevisionDiffResponse:
    """Current revision against the nearest earlier revision that has text.

    Lazy: fetched only when a reader opens the comparison, never part of the
    detail response. Stored extracted text only -- nothing is re-parsed, and
    nothing reaches an LLM.
    """
    unknown = sorted(request.query_params.keys())
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )
    try:
        payload = _service(conn).revision_diff(user.user_id, document_id, MAX_DIFF_ITEMS)
    except DocumentNotVisible as exc:
        raise document_not_found() from exc
    # Not logged: the payload is document body text.
    return RevisionDiffResponse(**payload)


@router.post(
    "/{document_id}/relocate",
    response_model=RelocateResponse,
    summary="원본 파일 이름 변경 / 폴더 이동 (관리자)",
    responses={code: {"model": ErrorResponse} for code in (403, 409, 503)},
)
def relocate_document(
    request: Request,
    document_id: str,
    body: RelocateRequest,
    user: AuthenticatedUser = Depends(require_admin_user),
) -> RelocateResponse:
    """Rename and/or move the original file. Administrators only, feature-gated.

    Not content editing: the bytes are untouched, so no revision is created and
    the document keeps its id, revisions, permissions and every reference.
    """
    if request.query_params:
        raise validation_error("알 수 없는 query parameter가 있습니다.")
    config = load_config()
    if not config.usable:
        raise ApiError("FEATURE_UNAVAILABLE", "원본 파일 관리 기능이 꺼져 있습니다.")
    try:
        result = relocate(
            connection_factory(), config, user.user_id, document_id,
            filename=body.filename, folder_path=body.folder_path,
        )
    except DocumentNotRelocatable:
        raise document_not_found() from None
    except InvalidRelocation as exc:
        raise validation_error(exc.message) from None
    except DestinationExists as exc:
        raise ApiError("FILE_ALREADY_EXISTS", exc.message) from None
    except DocumentBusy as exc:
        raise ApiError("DOCUMENT_PROCESSING", exc.message) from None
    except SourceFileMissing as exc:
        raise ApiError("SOURCE_FILE_MISSING", exc.message) from None
    except RelocationUnavailable as exc:
        raise ApiError("FEATURE_UNAVAILABLE", exc.message) from None
    return RelocateResponse(**result)


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

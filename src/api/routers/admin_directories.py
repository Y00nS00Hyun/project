"""GET / POST /api/v1/admin/directories.

For administrators managing original files: where a folder can be created and
where a document can be moved. This is the filesystem, so empty folders appear.

It is deliberately not GET /folders. That endpoint stays the ACL-filtered
navigation every user sees, listing only folders that hold a document the user
may read; this one would disclose folder names to anyone who could call it, so
it requires a system administrator and the file-management feature.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..dependencies import AuthenticatedUser, connection_factory, require_admin_user
from ..errors import ApiError, validation_error
from ..file_management import (
    FolderExists, InvalidRelocation, RelocationUnavailable, create_directory,
    list_directories, load_config,
)
from ..schemas.admin_directories import CreateDirectoryRequest, DirectoryListResponse, DirectoryOut
from ..schemas.common import ErrorResponse

router = APIRouter(prefix="/admin/directories", tags=["admin"], responses={
    code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 500, 503)
})

_UNAVAILABLE = "원본 파일 관리 기능이 꺼져 있습니다."


@router.get("", response_model=DirectoryListResponse, summary="공유폴더 디렉터리 목록 (관리자)")
def list_admin_directories(
    request: Request,
    user: AuthenticatedUser = Depends(require_admin_user),
) -> DirectoryListResponse:
    if request.query_params:
        raise validation_error("알 수 없는 query parameter가 있습니다.")
    config = load_config()
    if not config.usable:
        raise ApiError("FEATURE_UNAVAILABLE", _UNAVAILABLE)
    return DirectoryListResponse(directories=list_directories(config))


@router.post("", response_model=DirectoryOut, status_code=201, summary="새 폴더 만들기 (관리자)")
def create_admin_directory(
    request: Request,
    body: CreateDirectoryRequest,
    user: AuthenticatedUser = Depends(require_admin_user),
) -> DirectoryOut:
    if request.query_params:
        raise validation_error("알 수 없는 query parameter가 있습니다.")
    config = load_config()
    if not config.usable:
        raise ApiError("FEATURE_UNAVAILABLE", _UNAVAILABLE)
    try:
        created = create_directory(
            connection_factory(), config, user.user_id, body.parent_path, body.name,
        )
    except InvalidRelocation as exc:
        raise validation_error(exc.message) from None
    except FolderExists as exc:
        raise ApiError("FOLDER_ALREADY_EXISTS", exc.message) from None
    except RelocationUnavailable as exc:
        raise ApiError("FEATURE_UNAVAILABLE", exc.message) from None
    return DirectoryOut(**created)

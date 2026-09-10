"""GET /api/v1/search.

The router validates and translates. It does not rank, filter by permission or
touch SQL -- all of that stays in SearchService, which already enforces
ACL-before-retrieval.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from search.exceptions import (
    InvalidSearchModeError,
    InvalidSearchRequestError,
    SemanticSearchUnavailableError,
)
from search.models import SearchMode, SearchRequest
from search.service import SearchService

from ..dependencies import AuthenticatedUser, get_search_service, require_user
from ..errors import ApiError, validation_error
from ..schemas.search import SearchResponse, to_search_item

router = APIRouter(tags=["search"])

#: Contract section 6.1: q is at most 512 characters.
MAX_QUERY_CHARS = 512

#: Every parameter this endpoint understands. Anything else is rejected rather
#: than ignored, so a typo like `yer=2026` cannot silently return unfiltered
#: results that the caller believes were filtered.
ALLOWED_PARAMS = frozenset(
    {"q", "mode", "page", "size", "department_id", "year", "tag_id", "file_type",
     "folder_path"}
)


def reject_unknown_params(request: Request) -> None:
    unknown = sorted(set(request.query_params.keys()) - ALLOWED_PARAMS)
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )


@router.get("/search", response_model=SearchResponse, summary="문서 검색")
def search(
    request: Request,
    q: str | None = Query(None, description="검색어. 없으면 필터 브라우징"),
    mode: str | None = Query(None, description="default | semantic | lexical"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    department_id: str | None = Query(None),
    year: int | None = Query(None, ge=1900, le=2100),
    tag_id: list[int] = Query(default=[], description="반복 지정 시 AND"),
    file_type: str | None = Query(None),
    folder_path: str | None = Query(
        None, description="공유폴더 기준 상대 경로. 해당 폴더의 하위 전체를 대상으로 한다"
    ),
    user: AuthenticatedUser = Depends(require_user),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    reject_unknown_params(request)

    if q is not None and len(q) > MAX_QUERY_CHARS:
        raise ApiError(
            "SEARCH_QUERY_TOO_LONG",
            f"검색어는 최대 {MAX_QUERY_CHARS}자까지 입력할 수 있습니다.",
        )

    try:
        parsed_mode = SearchMode.parse(mode)
        search_request = SearchRequest(
            # Identity comes from the authenticated session, never from the
            # query string.
            user_id=user.user_id,
            query=q,
            mode=parsed_mode,
            department_id=department_id,
            year=year,
            tag_ids=tuple(tag_id),
            file_type=file_type,
            folder_path=folder_path,
            page=page,
            size=size,
        )
    except (InvalidSearchModeError, InvalidSearchRequestError) as exc:
        raise validation_error(str(exc)) from exc

    try:
        result = service.search(search_request)
    except SemanticSearchUnavailableError as exc:
        # The embedding model is missing. Lexical search and browsing still
        # work, so this is a per-route failure, not a dead service.
        raise ApiError(
            "INTERNAL_ERROR",
            "의미 기반 검색을 현재 사용할 수 없습니다. lexical 모드를 사용해 주세요.",
        ) from exc

    return SearchResponse(
        items=[to_search_item(item) for item in result.items],
        page=result.page,
        size=result.size,
        total=result.total,
    )

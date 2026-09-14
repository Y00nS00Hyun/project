"""GET /api/v1/search/years.

The year filter's choices, taken from the documents themselves instead of a
fixed ten-year window. That window listed years with no documents and hid any
document older than it -- a 2015 report could be searched but never filtered to.

Read-only, and computed over the same eligible set search uses, so it changes
nothing about ranking, retrieval or ACL.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from search.repository import SearchRepository

from ..dependencies import AuthenticatedUser, connection_factory, require_user
from ..errors import validation_error
from ..schemas.years import YearListResponse

router = APIRouter(tags=["search"])

ALLOWED_PARAMS: frozenset[str] = frozenset()


@router.get("/search/years", response_model=YearListResponse, summary="검색 가능한 문서 연도 목록")
def list_years(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
) -> YearListResponse:
    unknown = sorted(set(request.query_params.keys()) - ALLOWED_PARAMS)
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )
    with connection_factory()() as conn:
        years = SearchRepository(conn).available_years(user.user_id)
    return YearListResponse(years=years)

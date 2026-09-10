"""GET /api/v1/folders.

Navigation, not a search facet. The tree is built from ACL plus current-READY
alone, so it stays stable while the user changes year, document kind or file
type filters -- a folder holding only manuals does not disappear when "report"
is selected, it simply returns nothing when opened.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from search.folder_tree import folder_tree

from ..dependencies import AuthenticatedUser, connection_factory, require_user
from ..errors import validation_error
from ..schemas.folders import FolderListResponse, FolderOut

router = APIRouter(tags=["folders"])

#: This endpoint takes no parameters. Rejecting the ones a caller might expect
#: to work is better than silently returning an unfiltered tree.
ALLOWED_PARAMS: frozenset[str] = frozenset()


@router.get("/folders", response_model=FolderListResponse, summary="공유폴더 구조")
def list_folders(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
) -> FolderListResponse:
    unknown = sorted(set(request.query_params.keys()) - ALLOWED_PARAMS)
    if unknown:
        raise validation_error(
            "알 수 없는 query parameter가 있습니다.",
            [{"field": name, "reason": "지원하지 않는 parameter입니다."} for name in unknown],
        )

    nodes = folder_tree(connection_factory(), user.user_id)
    return FolderListResponse(
        items=[
            FolderOut(
                path=node.path,
                name=node.name,
                parent_path=node.parent_path,
                depth=node.depth,
                document_count=node.document_count,
            )
            for node in nodes
        ]
    )

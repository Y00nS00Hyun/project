"""Error contract.

Every JSON error leaves the API in the envelope defined by API Contract v1
section 3:

    {"error": {"code": ..., "message": ..., "request_id": ...}}

FastAPI's default ``{"detail": ...}`` shape is never exposed. Nothing internal
-- exception types, SQL, stack traces, shared-folder paths -- reaches the
client; those stay in the logs, tied to the same ``request_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The closed set from API Contract v1 section 3.4. A code outside this set is
#: a bug: the frontend branches on these values.
ERROR_CODES: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "VALIDATION_ERROR": 422,
    "BAD_REQUEST": 400,
    "DOCUMENT_NOT_FOUND": 404,
    "REVISION_NOT_FOUND": 404,
    "DOCUMENT_NOT_DOWNLOADABLE": 409,
    "CHAT_SESSION_NOT_FOUND": 404,
    "CHAT_MESSAGE_TOO_LONG": 422,
    "SEARCH_QUERY_TOO_LONG": 422,
    "RATE_LIMITED": 429,
    # v1.2. A configured-off feature is not a server fault, and 500 tells a
    # client to retry something that will never start working. 503 says the
    # capability is absent, which is what the UI needs in order to disable the
    # control rather than let a question fail after it is typed.
    "FEATURE_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}


@dataclass
class ApiError(Exception):
    """An error that is safe to return to the client verbatim."""

    code: str
    message: str
    details: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"{self.code} is not in the API error code set")

    @property
    def status_code(self) -> int:
        return ERROR_CODES[self.code]

    def body(self, request_id: str) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
        }
        if self.details:
            error["details"] = self.details
        return {"error": error}


def document_not_found() -> ApiError:
    """404 for both "missing" and "not permitted".

    Contract section 3.3: returning 403 for an unreadable document would
    confirm that it exists and that the id is valid, leaking exactly what the
    ACL pre-filter is there to hide.
    """
    return ApiError("DOCUMENT_NOT_FOUND", "문서를 찾을 수 없습니다.")


def not_downloadable(reason: str = "원본 파일을 제공할 수 없습니다.") -> ApiError:
    return ApiError("DOCUMENT_NOT_DOWNLOADABLE", reason)


def validation_error(message: str, details: list[dict[str, Any]] | None = None) -> ApiError:
    return ApiError("VALIDATION_ERROR", message, details)


def unauthenticated(message: str = "인증이 필요합니다.") -> ApiError:
    return ApiError("UNAUTHENTICATED", message)

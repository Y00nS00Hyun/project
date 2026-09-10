"""FastAPI application for the Search / Document API.

Implements Search, Documents and Chat from API Contract v1. Production LLM
integration is an explicit dependency; no external provider is configured.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .dependencies import app_env, is_production
from .errors import ApiError
from .routers import chat, documents, folders, metadata, search

logger = logging.getLogger("api")

API_PREFIX = "/api/v1"

#: Response header carrying the correlation id, matching error.request_id.
REQUEST_ID_HEADER = "X-Request-Id"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error.body(_request_id(request)),
        headers={REQUEST_ID_HEADER: _request_id(request)},
    )


def cors_origins() -> list[str]:
    """Allow-list from the environment.

    Never ``*`` by default: with credentials in play a wildcard origin turns
    every site the user visits into a client of this API.
    """
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def create_app() -> FastAPI:
    docs_url = "/docs" if not is_production() else None

    app = FastAPI(
        title="사내 문서 관리 시스템 — Search / Document / Chat API",
        version="1.0.0",
        description=(
            "API Contract v1의 Search / Document / Chat API. "
            "LLM provider는 회사 승인 후 주입해야 한다."
        ),
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/openapi.json" if not is_production() else None,
    )

    origins = cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a correlation id and log the outcome.

        The query text is never logged: an internal search term can itself be
        sensitive. Route, status and duration are enough to operate on.
        """
        request.state.request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        logger.info(
            "http.request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "route": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Translate Pydantic errors into the contract envelope.

        Only field name and a short reason are passed through; the raw Pydantic
        payload can echo submitted values and internal model structure.
        """
        details = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]) or "request",
                "reason": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ]
        return _error_response(
            request, ApiError("VALIDATION_ERROR", "요청 값이 올바르지 않습니다.", details)
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Keep FastAPI's default {"detail": ...} shape off the wire."""
        mapping = {
            401: ("UNAUTHENTICATED", "인증이 필요합니다."),
            403: ("FORBIDDEN", "권한이 없습니다."),
            404: ("DOCUMENT_NOT_FOUND", "요청한 리소스를 찾을 수 없습니다."),
            405: ("BAD_REQUEST", "허용되지 않은 메서드입니다."),
            429: ("RATE_LIMITED", "요청이 너무 많습니다."),
        }
        code, message = mapping.get(exc.status_code, ("INTERNAL_ERROR", "오류가 발생했습니다."))
        return _error_response(request, ApiError(code, message))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: log the detail, return none of it.

        An unexpected exception message can carry SQL, a shared-folder path or
        a stack frame; the client gets a correlation id instead.
        """
        logger.exception(
            "http.unhandled_error",
            extra={"request_id": _request_id(request), "route": request.url.path},
        )
        return _error_response(
            request, ApiError("INTERNAL_ERROR", "서버 오류가 발생했습니다.")
        )

    app.include_router(search.router, prefix=API_PREFIX)
    app.include_router(documents.router, prefix=API_PREFIX)
    app.include_router(metadata.router, prefix=API_PREFIX)
    app.include_router(folders.router, prefix=API_PREFIX)
    app.include_router(chat.router, prefix=API_PREFIX)

    logger.info("api.started", extra={"env": app_env(), "docs_enabled": docs_url is not None})
    return app


app = create_app()

"""Chat HTTP validation and service mapping. No retrieval, ACL or SQL here."""
import logging
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, Query, Request

from rag.exceptions import GenerationRateLimited, GenerationUnavailable, SessionNotFound
from rag.service import ChatService, RagService

from ..dependencies import AuthenticatedUser, get_chat_service, get_rag_service, require_user
from ..errors import ApiError, validation_error
from ..schemas.chat import (
    CreateSessionRequest, SendMessageRequest, SendMessageResponse,
    SessionDetail, SessionListResponse, SessionOut,
)
from ..schemas.common import ErrorResponse

logger = logging.getLogger('chat')
T = TypeVar('T')
router = APIRouter(prefix='/chat/sessions', tags=['chat'], responses={
    code: {'model': ErrorResponse} for code in (401, 404, 422, 500)
})


def _call(request: Request, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except SessionNotFound:
        raise ApiError('CHAT_SESSION_NOT_FOUND', '채팅 세션을 찾을 수 없습니다.') from None
    except GenerationRateLimited:
        # Upstream provider throttled us. RATE_LIMITED already exists in the
        # contract's closed code set, so no new code is invented for it.
        logger.warning('chat.generation_rate_limited', extra={'request_id': request.state.request_id})
        raise ApiError('RATE_LIMITED', '요청이 많아 잠시 후 다시 시도해 주세요.') from None
    except GenerationUnavailable:
        logger.warning('chat.generation_unavailable', extra={'request_id': request.state.request_id})
        raise ApiError('INTERNAL_ERROR', '답변 생성을 현재 사용할 수 없습니다.') from None
    except Exception:
        # Database/provider errors may contain document text or bind parameters.
        logger.warning('chat.failed', extra={'request_id': request.state.request_id})
        raise ApiError('INTERNAL_ERROR', '채팅 요청을 처리하지 못했습니다.') from None


def _parameters(request: Request, allowed: set[str]) -> None:
    if set(request.query_params) - allowed:
        raise validation_error('알 수 없는 query parameter가 있습니다.')


@router.post('', response_model=SessionOut, status_code=201, summary='채팅 세션 생성')
def create_session(
    request: Request, body: CreateSessionRequest,
    user: AuthenticatedUser = Depends(require_user),
    service: ChatService = Depends(get_chat_service),
):
    _parameters(request, set())
    return _call(request, lambda: service.create_session(user.user_id, body.title))


@router.get('', response_model=SessionListResponse, summary='내 채팅 세션 목록')
def list_sessions(
    request: Request,
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
    service: ChatService = Depends(get_chat_service),
):
    _parameters(request, {'page', 'size'})
    return _call(request, lambda: service.list_sessions(user.user_id, page, size))


@router.get('/{session_id}', response_model=SessionDetail, summary='채팅 세션과 메시지')
def get_session(
    request: Request, session_id: str,
    page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
    service: ChatService = Depends(get_chat_service),
):
    _parameters(request, {'page', 'size'})
    return _call(request, lambda: service.get_session(user.user_id, session_id, page, size))


@router.post('/{session_id}/messages', response_model=SendMessageResponse, status_code=201,
             summary='문서 근거형 질문과 답변',
             # Only this route reaches an LLM provider, so only this route can
             # be throttled by one.
             responses={429: {'model': ErrorResponse}})
def send_message(
    request: Request, session_id: str, body: SendMessageRequest,
    user: AuthenticatedUser = Depends(require_user),
    service: RagService = Depends(get_rag_service),
):
    _parameters(request, set())
    return _call(request, lambda: service.send_message(
        user.user_id, session_id, body.message, request.state.request_id,
    ))

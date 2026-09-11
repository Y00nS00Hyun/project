"""Local authentication and account administration.

This is the operational way into the system -- there is no external identity
provider to defer to -- and it still ships switched off, because an
authentication path should be something a deployment turned on deliberately.

The router is always mounted; the switches are checked per request. Mounting
conditionally would make "the feature is off" and "no such path" the same 404,
and an operator needs to tell those apart.

No route here reads or writes a department. The organisation does not use them;
the column and the department ACL principal stay for the documents that still
carry them, and authentication simply never touches either.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response

from auth.config import SESSION_COOKIE, LocalAuthConfig
from auth.service import (
    AccountDisabled, AccountPendingApproval, AuthService, CannotDisableSelf,
    InvalidCredentials, InvalidPassword, InvalidResetToken, InvalidSignup,
    LastAdministrator, LocalAuthDisabled, RateLimited, SignupDisabled, UserNotFound,
)

from ..dependencies import (
    AuthenticatedUser, get_auth_service, get_local_auth_config, require_admin_user,
    require_user,
)
from ..errors import ApiError, validation_error
from ..schemas.auth import (
    AdminUserOut, AuthCapabilityResponse, ChangePasswordRequest, LoginRequest,
    MeResponse, ResetPasswordRequest, SetAdminRequest, SignupRequest, SignupResponse,
)
from ..schemas.common import ErrorResponse

logger = logging.getLogger('auth.http')

router = APIRouter(prefix='/auth', tags=['auth'], responses={
    code: {'model': ErrorResponse} for code in (401, 422, 500, 503)
})

#: One message for every way a login can fail. Distinguishing "no such account"
#: from "wrong password" would turn this endpoint into a way to test whether
#: somebody has one.
INVALID_CREDENTIALS_MESSAGE = '아이디 또는 비밀번호가 올바르지 않습니다.'

#: Shown once the password has been verified and the account turns out not to
#: be approved yet. Safe to be specific here precisely because it is only
#: reachable by someone who already proved they own the account.
PENDING_APPROVAL_MESSAGE = '관리자 승인 대기 중입니다.'
DISABLED_ACCOUNT_MESSAGE = '비활성화된 계정입니다. 관리자에게 문의해 주세요.'


def _disabled() -> ApiError:
    return ApiError('FEATURE_UNAVAILABLE', '로컬 로그인이 비활성화되어 있습니다.')


def _client(request: Request) -> str | None:
    """The address the rate limiter counts against.

    request.client is the socket peer, which behind the reverse proxy is the
    proxy. X-Forwarded-For is deliberately NOT trusted here: it is a header a
    client can set, so honouring it would let an attacker reset their own
    counter by changing one string.
    """
    return request.client.host if request.client else None


def _set_session_cookie(response: Response, token: str, config: LocalAuthConfig) -> None:
    """Hand the browser the token and nothing else.

    httponly     script cannot read it, so an XSS bug cannot exfiltrate a session
    samesite=lax it does not ride along on cross-site POSTs
    secure       HTTPS only in production; false over plain HTTP in development,
                 where a secure cookie would simply never be stored
    max_age      matches the row's expiry, so the browser and the database
                 agree on when the session ended
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.session_days * 24 * 60 * 60,
        httponly=True,
        samesite='lax',
        secure=config.cookie_secure,
        path='/',
    )


@router.get('/capability', response_model=AuthCapabilityResponse, summary='로컬 로그인 사용 가능 여부')
def capability(config: LocalAuthConfig = Depends(get_local_auth_config)):
    # Unauthenticated on purpose: a login page has to be able to ask this
    # before anybody has logged in.
    return AuthCapabilityResponse(
        local_auth_enabled=config.enabled, signup_enabled=config.signup_enabled,
    )


@router.post('/signup', response_model=SignupResponse, status_code=201, summary='회원가입')
def signup(
    request: Request, body: SignupRequest,
    service: AuthService = Depends(get_auth_service),
):
    try:
        service.signup(
            login_id=body.login_id, name=body.name,
            password=body.password, password_confirm=body.password_confirm,
            client=_client(request),
        )
    except (LocalAuthDisabled, SignupDisabled):
        raise _disabled() from None
    except RateLimited:
        raise ApiError('RATE_LIMITED', '요청이 많습니다. 잠시 후 다시 시도해 주세요.') from None
    except InvalidSignup as exc:
        raise validation_error(exc.message, [{'field': exc.field, 'reason': exc.message}]) from None

    # No session is issued. The account is PENDING, so logging it straight in
    # would hand out a session that every protected route then rejects -- and
    # would blur signing up with being let in, which is the distinction this
    # whole flow exists to make.
    return SignupResponse(status='PENDING', message=PENDING_APPROVAL_MESSAGE)


@router.post('/login', response_model=MeResponse, summary='로그인')
def login(
    request: Request, body: LoginRequest, response: Response,
    service: AuthService = Depends(get_auth_service),
    config: LocalAuthConfig = Depends(get_local_auth_config),
):
    try:
        user_id, token, _ = service.login(
            login_id=body.login_id, password=body.password, client=_client(request),
        )
    except LocalAuthDisabled:
        raise _disabled() from None
    except RateLimited:
        raise ApiError('RATE_LIMITED', '요청이 많습니다. 잠시 후 다시 시도해 주세요.') from None
    except AccountPendingApproval:
        raise ApiError('FORBIDDEN', PENDING_APPROVAL_MESSAGE) from None
    except AccountDisabled:
        raise ApiError('FORBIDDEN', DISABLED_ACCOUNT_MESSAGE) from None
    except InvalidCredentials:
        raise ApiError('UNAUTHENTICATED', INVALID_CREDENTIALS_MESSAGE) from None

    _set_session_cookie(response, token, config)
    profile = service.profile(user_id) or {'user_id': user_id}
    return MeResponse(**profile)


@router.post('/logout', status_code=204, summary='로그아웃')
def logout(request: Request, response: Response, service: AuthService = Depends(get_auth_service)):
    # Revoked server-side, then cleared client-side. Clearing the cookie alone
    # would leave a token that still works for anyone who kept a copy.
    service.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path='/')
    return Response(status_code=204)


@router.get('/me', response_model=MeResponse, summary='현재 로그인 사용자')
def me(
    user: AuthenticatedUser = Depends(require_user),
    service: AuthService = Depends(get_auth_service),
):
    # Goes through require_user like every other authenticated route, so it
    # answers for a debug-header identity too and there is no second definition
    # of "who is calling".
    profile = service.profile(user.user_id)
    if profile is None:
        raise ApiError('UNAUTHENTICATED', '인증이 필요합니다.')
    return MeResponse(**profile)


@router.post('/password', status_code=204, summary='비밀번호 변경')
def change_password(
    request: Request, body: ChangePasswordRequest,
    user: AuthenticatedUser = Depends(require_user),
    service: AuthService = Depends(get_auth_service),
):
    try:
        service.change_password(
            user.user_id,
            current_password=body.current_password,
            new_password=body.new_password,
            new_password_confirm=body.new_password_confirm,
            # The browser doing the changing keeps its session; every other one
            # is ended.
            keep_token=request.cookies.get(SESSION_COOKIE),
        )
    except LocalAuthDisabled:
        raise _disabled() from None
    except InvalidCredentials:
        raise ApiError('UNAUTHENTICATED', '현재 비밀번호가 올바르지 않습니다.') from None
    except InvalidPassword as exc:
        raise validation_error(exc.message) from None
    return Response(status_code=204)


@router.post('/password/reset', status_code=204, summary='재설정 토큰으로 비밀번호 설정')
def reset_password(body: ResetPasswordRequest, service: AuthService = Depends(get_auth_service)):
    # Unauthenticated: somebody who cannot log in is exactly who needs this.
    # The token stands in for the forgotten password.
    try:
        service.complete_reset(body.token, body.new_password, body.new_password_confirm)
    except LocalAuthDisabled:
        raise _disabled() from None
    except InvalidPassword as exc:
        raise validation_error(exc.message) from None
    except InvalidResetToken:
        raise ApiError(
            'UNAUTHENTICATED', '재설정 링크가 유효하지 않거나 만료되었습니다.',
        ) from None
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Account administration
#
# Every route here goes through require_admin_user, which checks
# users.is_system_admin -- a different thing from a document's ADMIN
# permission. Nothing granted in document_permissions leads to this section.
#
# Approving grants no document permission either. What an account may read is
# decided in document_permissions, separately and on purpose.
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix='/admin', tags=['admin'], responses={
    code: {'model': ErrorResponse} for code in (401, 403, 422, 500)
})


@admin_router.get('/users', response_model=list[AdminUserOut], summary='사용자 목록')
def list_users(
    status: str | None = None,
    user: AuthenticatedUser = Depends(require_admin_user),
    service: AuthService = Depends(get_auth_service),
):
    try:
        return [AdminUserOut(**row) for row in service.list_users(user.user_id, status)]
    except InvalidSignup as exc:
        raise validation_error(exc.message) from None


@admin_router.post('/users/{user_id}/approve', response_model=AdminUserOut, summary='가입 승인')
def approve_user(
    user_id: str,
    user: AuthenticatedUser = Depends(require_admin_user),
    service: AuthService = Depends(get_auth_service),
):
    # No request body at all. Approval is one decision -- may this person sign
    # in -- and a body would be somewhere for a second one to creep in.
    try:
        service.approve(user.user_id, user_id)
    except UserNotFound:
        raise ApiError('DOCUMENT_NOT_FOUND', '사용자를 찾을 수 없습니다.') from None
    return _one(service, user.user_id, user_id)


@admin_router.post('/users/{user_id}/disable', response_model=AdminUserOut, summary='계정 비활성화')
def disable_user(
    user_id: str,
    user: AuthenticatedUser = Depends(require_admin_user),
    service: AuthService = Depends(get_auth_service),
):
    try:
        service.disable(user.user_id, user_id)
    except CannotDisableSelf:
        raise validation_error('자기 자신을 비활성화할 수 없습니다.') from None
    except LastAdministrator:
        raise validation_error('마지막 관리자는 비활성화할 수 없습니다.') from None
    except UserNotFound:
        raise ApiError('DOCUMENT_NOT_FOUND', '사용자를 찾을 수 없습니다.') from None
    return _one(service, user.user_id, user_id)


@admin_router.post('/users/{user_id}/admin', response_model=AdminUserOut, summary='관리자 권한 변경')
def set_admin(
    user_id: str, body: SetAdminRequest,
    user: AuthenticatedUser = Depends(require_admin_user),
    service: AuthService = Depends(get_auth_service),
):
    try:
        service.set_admin(user.user_id, user_id, body.granted)
    except CannotDisableSelf:
        raise validation_error('자기 자신의 관리자 권한은 해제할 수 없습니다.') from None
    except LastAdministrator:
        raise validation_error('마지막 관리자의 권한은 해제할 수 없습니다.') from None
    except UserNotFound:
        raise ApiError('DOCUMENT_NOT_FOUND', '사용자를 찾을 수 없습니다.') from None
    return _one(service, user.user_id, user_id)


def _one(service: AuthService, admin_id: str, user_id: str) -> AdminUserOut:
    """Re-read the row so the response is what the database holds, not what we sent."""
    for row in service.list_users(admin_id):
        if str(row['user_id']) == str(user_id):
            return AdminUserOut(**row)
    raise ApiError('DOCUMENT_NOT_FOUND', '사용자를 찾을 수 없습니다.')

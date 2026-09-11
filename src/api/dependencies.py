"""Request-scoped dependencies: identity, database, services.

The identity rule is the important one. The authenticated user is resolved by
the server and never taken from request data -- there is no query parameter or
body field a client could set to search as somebody else. That is what makes
the ACL pre-filter in the search backend meaningful.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator

import psycopg
from fastapi import Depends, Request

from ingestion.config import IngestionConfig, config_from_env, database_url
from search.query_embedding import LocalQueryEmbedder
from search.service import SearchService

from .errors import unauthenticated

#: Header used by the development identity provider. Honoured ONLY outside
#: production -- see require_user().
DEBUG_USER_HEADER = "X-Debug-User-Id"

#: Environments in which the development identity provider may run.
NON_PRODUCTION_ENVS = {"test", "testing", "development", "dev", "local"}


@dataclass(frozen=True)
class AuthenticatedUser:
    """Trusted identity. Populated by the server, never by the client."""

    user_id: str
    department_id: str | None = None


def app_env() -> str:
    return os.environ.get("APP_ENV", "production").strip().lower()


def is_production() -> bool:
    return app_env() not in NON_PRODUCTION_ENVS


@lru_cache(maxsize=1)
def get_config() -> IngestionConfig:
    """Shared configuration. Cached: reading it per request is pointless work."""
    return config_from_env()


@lru_cache(maxsize=1)
def get_dsn() -> str:
    return database_url()


def get_connection() -> Iterator[psycopg.Connection]:
    """One connection per request, closed when the request ends."""
    conn = psycopg.connect(get_dsn())
    try:
        yield conn
    finally:
        conn.close()


def connection_factory():
    """Factory the search backend uses to open its own short transactions."""

    def factory() -> psycopg.Connection:
        return psycopg.connect(get_dsn())

    return factory


@lru_cache(maxsize=1)
def get_query_embedder() -> LocalQueryEmbedder:
    """Process-wide embedder.

    One instance per process, and the model inside it loads lazily on first
    semantic query. Loading per request would make every search pay for a model
    load; loading eagerly at startup would break lexical search and browsing in
    a deployment with no model cache.
    """
    return LocalQueryEmbedder(get_config())


def get_search_service() -> SearchService:
    return SearchService(connection_factory(), get_config(), query_embedder=get_query_embedder())


def get_local_auth_config():
    """Resolved per request rather than cached.

    The switches are read live so that turning local auth off takes effect
    without a restart -- which matters most in the direction that closes it.
    """
    from auth.config import LocalAuthConfig

    return LocalAuthConfig.from_env(production=is_production())


def require_admin_user(request: Request) -> AuthenticatedUser:
    """Authenticate, then require the system-administrator flag.

    A separate dependency rather than a check inside each handler, so an
    administrative route cannot be added without one.
    """
    from auth.service import NotAnAdministrator

    user = require_user(request)
    try:
        get_auth_service().require_admin(user.user_id)
    except NotAnAdministrator:
        from .errors import ApiError

        # 403, not 404: the caller is authenticated and this is a real path
        # they simply may not use. Nothing about it discloses another account.
        raise ApiError("FORBIDDEN", "관리자 권한이 필요합니다.") from None
    return user


def get_auth_service():
    from auth.repository import AuthRepository
    from auth.service import AuthService

    return AuthService(AuthRepository(connection_factory()), get_local_auth_config())


def require_user(request: Request) -> AuthenticatedUser:
    """Resolve the authenticated user, in a fixed order of preference.

    There is no SSO, OIDC, SAML, LDAP or AD to defer to, so the local session
    cookie is the real way in and works in every environment, production
    included. It is still only available where an operator switched it on.

        1. a local-auth session cookie -- how a person logs in
        2. the debug identity header -- automated testing, never in production

    The cookie is tried first because it is the one a person actually holds; a
    stale header left in a script must not override the session somebody just
    logged into.

    Whichever succeeds yields a plain user id, and every access decision after
    this point runs on that id through the existing
    users -> department -> document_permissions path. No permission logic is
    duplicated here and none is bypassed: authenticating establishes who you
    are and says nothing whatsoever about what you may read. That separation is
    also what keeps a future replacement cheap -- another mechanism would only
    have to produce a user id.
    """
    session_user = _user_from_session(request)
    if session_user is not None:
        return session_user

    if is_production():
        # The header is a testing affordance and stays out of production
        # regardless of how local auth is configured. With no valid session
        # this is where a production request ends.
        raise unauthenticated()

    raw = request.headers.get(DEBUG_USER_HEADER)
    if not raw:
        raise unauthenticated()

    with psycopg.connect(get_dsn()) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                # The debug identity obeys the same lifecycle as a real login:
                # a pending or disabled account is not a way in either.
                "SELECT id, department_id FROM users "
                "WHERE id = %s AND is_active = TRUE AND status = 'ACTIVE'",
                (raw,),
            )
        except psycopg.errors.InvalidTextRepresentation:
            # A malformed uuid is an unusable identity, not a 500.
            raise unauthenticated("알 수 없는 사용자입니다.") from None
        row = cur.fetchone()
    if row is None:
        raise unauthenticated("알 수 없는 사용자입니다.")
    return AuthenticatedUser(
        user_id=str(row[0]),
        department_id=str(row[1]) if row[1] else None,
    )


def _user_from_session(request: Request) -> AuthenticatedUser | None:
    """The local-auth cookie, if local auth is on and the session is live."""
    from auth.config import SESSION_COOKIE

    config = get_local_auth_config()
    if not config.enabled:
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    from auth.repository import AuthRepository

    session = AuthRepository(connection_factory()).resolve_session(token)
    if session is None:
        # Expired, revoked, deactivated or simply wrong. Returning None rather
        # than raising lets the header fall through, and an invalid cookie ends
        # as an ordinary 401.
        return None
    return AuthenticatedUser(
        user_id=str(session["user_id"]),
        department_id=str(session["department_id"]) if session["department_id"] else None,
    )


CurrentUser = Depends(require_user)


@lru_cache(maxsize=1)
def get_llm_provider():
    """Select the provider explicitly, and only explicitly.

    A credential sitting in the environment is never enough on its own:
    LLM_PROVIDER has to name the provider before anything leaves the network,
    so no deployment starts sending internal documents to an external service
    by accident. An unset selector keeps the default that cannot call out at
    all. Cached, so the client and its connection pool outlive one request.

    Tests keep using dependency_overrides for a deterministic fake; that path
    is unchanged and never consults the environment.
    """
    from rag.exceptions import ProviderConfigurationError
    from rag.provider import UNCONFIGURED, UnconfiguredProvider, selected_provider_name

    selected = selected_provider_name()
    if selected == UNCONFIGURED:
        return UnconfiguredProvider()
    if selected == "anthropic":
        try:
            from rag.providers.anthropic_claude import provider_from_env
        except ImportError:
            raise ProviderConfigurationError(
                "LLM_PROVIDER=anthropic needs the 'llm' extra: pip install -e '.[llm]'"
            ) from None
        return provider_from_env()
    raise ProviderConfigurationError(f"LLM_PROVIDER={selected} is not a known provider")


def get_chat_repository():
    from rag.repository import ChatRepository
    return ChatRepository(connection_factory())


def get_chat_service(repository=Depends(get_chat_repository)):
    from rag.service import ChatService
    return ChatService(repository)


def get_rag_service(
    search: SearchService = Depends(get_search_service),
    provider=Depends(get_llm_provider),
    repository=Depends(get_chat_repository),
):
    from rag.context import RagConfig
    from rag.service import RagService
    return RagService(repository, search, provider, RagConfig.from_env())

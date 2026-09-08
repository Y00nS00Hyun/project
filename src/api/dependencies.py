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


def require_user(request: Request) -> AuthenticatedUser:
    """Resolve the authenticated user.

    Real SSO is not implemented yet. Until it is, a development-only provider
    accepts an identity header -- but only outside production, and only for a
    user that actually exists. In production the header is ignored entirely and
    the request is rejected, so a misconfigured deployment fails closed rather
    than silently trusting a client-supplied id.
    """
    if is_production():
        # No SSO wired up yet: production has no way to authenticate anyone.
        raise unauthenticated("인증 공급자가 구성되지 않았습니다.")

    raw = request.headers.get(DEBUG_USER_HEADER)
    if not raw:
        raise unauthenticated()

    with psycopg.connect(get_dsn()) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT id, department_id FROM users WHERE id = %s AND is_active = TRUE",
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
    from rag.provider import UnconfiguredProvider

    selected = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not selected or selected == "unconfigured":
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

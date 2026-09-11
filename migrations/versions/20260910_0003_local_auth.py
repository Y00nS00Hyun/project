"""development-only local authentication

Two tables, both additive, neither touched by any existing query.

Why separate tables rather than columns on `users`: `users` describes a person
in the organisation, and it is what the ACL joins against. Credentials describe
one *way* of proving you are that person, and this way is a development
stand-in for SSO that will be deleted rather than migrated. Keeping them apart
means turning local auth off is a configuration change and removing it is a
DROP TABLE -- neither of which can disturb identity or permissions.

It also keeps a plain fact plain: a `users` row with no credential row is a
perfectly normal user. That is what every SSO-provisioned user will look like.

Revision ID: 0003_local_auth
Revises: 0002_chat_session_document_scope
Create Date: 2026-09-10
"""
from __future__ import annotations

from alembic import op

revision = "0003_local_auth"
down_revision = "0002_chat_session_document_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE local_auth_credentials (
            user_id UUID PRIMARY KEY
                REFERENCES users(id)
                ON DELETE CASCADE,

            /*
             * 로그인 식별자. 애플리케이션에서 소문자로 정규화한 뒤 저장하므로
             * 대소문자만 다른 중복 계정이 생기지 않는다. citext 확장을 쓰지
             * 않는 이유는 개발용 기능 하나를 위해 배포에 확장 의존성을
             * 더하지 않기 위해서다.
             */
            login_id TEXT NOT NULL UNIQUE,

            /*
             * Argon2id 인코딩 문자열. 알고리즘과 파라미터가 문자열 안에
             * 들어 있으므로 나중에 파라미터를 올려도 기존 해시를 그대로
             * 검증할 수 있다. 평문은 어떤 컬럼에도 저장하지 않는다.
             */
            password_hash TEXT NOT NULL,

            /*
             * 로그인 실패 backoff. 별도 테이블이나 외부 저장소를 두지 않고
             * credential 행에 둔다 -- 잠글 대상이 바로 이 행이기 때문이다.
             */
            failed_attempts INT NOT NULL DEFAULT 0
                CHECK (failed_attempts >= 0),

            locked_until TIMESTAMPTZ,

            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE auth_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

            user_id UUID NOT NULL
                REFERENCES users(id)
                ON DELETE CASCADE,

            /*
             * 브라우저가 가진 token의 SHA-256. 원문은 저장하지 않는다.
             * DB를 읽을 수 있게 된 사람이 그것만으로 남의 세션을 가장할 수
             * 없어야 한다. token 자체는 발급 시점 한 번만 존재한다.
             */
            token_hash TEXT NOT NULL UNIQUE,

            expires_at TIMESTAMPTZ NOT NULL,

            /*
             * 로그아웃은 행을 지우지 않고 이 값을 채운다. 세션이 만료된
             * 것인지 명시적으로 끊긴 것인지 구분할 수 있어야 하고, 지워버린
             * 행은 아무것도 말해주지 않는다.
             */
            revoked_at TIMESTAMPTZ,

            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Every request looks a session up by token hash; the UNIQUE constraint
    # above already provides that index, so only the revoke-all-for-a-user path
    # needs one of its own.
    op.execute("CREATE INDEX idx_auth_sessions_user ON auth_sessions (user_id)")
    op.execute(
        """
        CREATE INDEX idx_auth_sessions_expiry
            ON auth_sessions (expires_at)
            WHERE revoked_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_sessions")
    op.execute("DROP TABLE IF EXISTS local_auth_credentials")

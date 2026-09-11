"""administrator-initiated password reset

Two columns on the existing credential row rather than a new table: a reset is
a property of one credential, only one can be outstanding at a time, and it is
cleared the moment it is used.

Why a token and not a temporary password: an administrator who sets a password
knows that password, and the account is then only as private as their memory
and whatever channel they sent it over. A token lets somebody back into their
account without anyone else ever learning what they choose.

The token is stored as a hash for the same reason a session token is -- reading
the table must not hand anyone a way in.

Revision ID: 0005_password_reset
Revises: 0004_user_status_and_admin
Create Date: 2026-09-11
"""
from __future__ import annotations

from alembic import op

revision = "0005_password_reset"
down_revision = "0004_user_status_and_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE local_auth_credentials
            ADD COLUMN reset_token_hash TEXT UNIQUE,
            ADD COLUMN reset_expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN local_auth_credentials.reset_token_hash IS
            '관리자가 발급한 비밀번호 재설정 토큰의 SHA-256. 원문은 발급
             시점에 한 번만 존재하며 저장하지 않는다. 사용 즉시 NULL 로
             되돌린다.'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE local_auth_credentials
            DROP COLUMN IF EXISTS reset_expires_at,
            DROP COLUMN IF EXISTS reset_token_hash
        """
    )

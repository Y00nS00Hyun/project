"""account lifecycle and system administrators

Local authentication is the operational way in, so signing up can no longer be
the same act as gaining access. These two columns separate "this person has an
account" from "this person has been let in", and separate "may administer
users" from every existing notion of permission.

Why `status` is not folded into the existing `is_active`: that column answers
"should this account work at all", and it already means that in the ACL join.
The new question is where an account is in its lifecycle, and PENDING is not a
kind of inactive -- it is an account waiting for a person to look at it. Two
questions, two columns, neither one overloaded.

Why `is_system_admin` is not `document_permissions.permission = 'ADMIN'`: that
value grants power over *one document* and is already one of the values
READ_ACL_PREDICATE accepts as read access. Administering user accounts is not a
document permission and must not be reachable by being granted one.

Revision ID: 0004_user_status_and_admin
Revises: 0003_local_auth
Create Date: 2026-09-10
"""
from __future__ import annotations

from alembic import op

revision = "0004_user_status_and_admin"
down_revision = "0003_local_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defaults to ACTIVE so every user that already exists keeps working
    # exactly as before. Only signup writes PENDING, explicitly.
    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('PENDING', 'ACTIVE', 'DISABLED'))
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN users.status IS
            'PENDING = 가입했으나 관리자 승인 대기. ACTIVE = 승인됨. '
            'DISABLED = 관리자가 비활성화. 로그인과 세션 확인 양쪽에서 '
            'ACTIVE만 통과한다.'
        """
    )

    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN is_system_admin BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN users.is_system_admin IS
            '사용자 계정 관리 권한. document_permissions 의 ADMIN 과 다른 '
            '개념이며 서로 대체하지 않는다. 가입으로 획득할 수 없고 오직 '
            '기존 관리자 또는 서버 CLI 만 부여한다.'
        """
    )

    # The approval queue is the only status query, and PENDING is the small
    # minority of rows.
    op.execute(
        """
        CREATE INDEX idx_users_pending
            ON users (created_at)
            WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_pending")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_system_admin")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS status")

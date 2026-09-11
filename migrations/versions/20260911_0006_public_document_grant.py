"""a third permission principal: every signed-in user

The corpus this system ingests is general internal documentation only --
confidential, HR and payroll material is excluded at collection time, not
filtered out at read time. So the operating policy is that any approved account
may read any document that was ingested.

Expressing that as "the ACL returns true for everyone" would delete the
mechanism. Expressing it as a grant per user per document would be
users x documents rows that have to be maintained forever. This is the third
option: one more kind of principal, one row per document, evaluated by the same
predicate in the same place, before retrieval, exactly as before.

What it preserves:

  * default deny. A document with no permission row is still readable by
    nobody, and that is still the state a document starts in.
  * per-document control. Removing a document's public row and granting
    specific users instead needs no schema change and no new code -- that path
    already exists and still works.
  * ACL before retrieval. The predicate gains one OR branch and keeps running
    in the candidate CTE.

What it does not do: grant anything to an account that is not signed in and
ACTIVE. There is no anonymous access; require_user still has to produce a user
id before any of this is consulted.

Revision ID: 0006_public_document_grant
Revises: 0005_password_reset
Create Date: 2026-09-11
"""
from __future__ import annotations

from alembic import op

revision = "0006_public_document_grant"
down_revision = "0005_password_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_permissions
            ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN document_permissions.is_public IS
            '승인된(ACTIVE) 사용자 전원에게 부여. 익명 접근이 아니라 "로그인한
             모든 사용자" 를 뜻하는 principal 이다. user_id / department_id 와
             정확히 배타적이다.'
        """
    )

    # The original CHECK said "exactly one of user_id, department_id". A public
    # row names neither, so the rule becomes "exactly one principal", counting
    # the public flag as one.
    op.execute(
        "ALTER TABLE document_permissions DROP CONSTRAINT document_permissions_check"
    )
    op.execute(
        """
        ALTER TABLE document_permissions
            ADD CONSTRAINT document_permissions_principal_check
            CHECK (
                num_nonnulls(user_id, department_id) + is_public::int = 1
            )
        """
    )

    # One public row per document, matching how the other two principals are
    # constrained. Without it a second identical row could be inserted and the
    # grant would have to be revoked twice.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_permissions_document_public
            ON document_permissions (document_id)
            WHERE is_public
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_permissions_document_public")
    # Public rows name no principal, so they cannot survive the original CHECK.
    op.execute("DELETE FROM document_permissions WHERE is_public")
    op.execute(
        "ALTER TABLE document_permissions "
        "DROP CONSTRAINT IF EXISTS document_permissions_principal_check"
    )
    op.execute("ALTER TABLE document_permissions DROP COLUMN IF EXISTS is_public")
    op.execute(
        """
        ALTER TABLE document_permissions
            ADD CONSTRAINT document_permissions_check
            CHECK (num_nonnulls(user_id, department_id) = 1)
        """
    )

"""document-scoped chat sessions

Adds one nullable column so a chat session can be bound to a single document.
NULL keeps the existing behaviour -- a session over the whole corpus -- so
every existing row and every existing client is unaffected.

Why a column rather than a per-request parameter: the scope has to be something
the server reads, not something the client asserts. A request parameter must be
validated on every message, and one missed check silently widens retrieval to
the entire corpus. A stored scope cannot be forgotten.

Nothing else is needed for this feature. `document_revisions` already carries
summary/summary_status/summary_provider/summary_model/summary_prompt_version,
and `processing_jobs.job_type` already admits 'SUMMARIZE' -- both were provided
for in database-schema-v2.5 and are used as they were intended.

Revision ID: 0002_chat_session_document_scope
Revises: 0001_initial_schema
Create Date: 2026-09-10
"""
from __future__ import annotations

from alembic import op

revision = "0002_chat_session_document_scope"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE chat_sessions
            ADD COLUMN document_id UUID
                REFERENCES documents(id)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN chat_sessions.document_id IS
            'NULL = 전체 corpus 대상 세션. 값이 있으면 그 문서로만 retrieval을 '
            '제한한다. 제한은 prompt가 아니라 검색 후보 SQL에서 강제된다.'
        """
    )
    # Partial: only scoped sessions are ever looked up by document, and the
    # global sessions are the majority.
    op.execute(
        """
        CREATE INDEX idx_chat_sessions_document
            ON chat_sessions (document_id)
            WHERE document_id IS NOT NULL
        """
    )
    # No ON DELETE action on purpose. Documents are soft-deleted, so a real
    # DELETE is not part of normal operation; if one is ever attempted, failing
    # loudly is better than silently detaching a conversation from what it was
    # about.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chat_sessions_document")
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS document_id")

"""the date printed on the document itself

``document_year`` answers "which year is this document about". This answers the
narrower question "what date does its cover state", and only when the cover
states one outright.

On document_revisions rather than documents because a cover date belongs to a
revision: re-issuing a report in January gives a new cover date while the
document is the same document, and the old revision must keep saying what it
said. Every historical citation and every past answer refers to a revision, so
the date has to live where they can still find it.

DATE rather than TIMESTAMPTZ: a cover states a day, with no time and no
timezone. Storing it as an instant would invent both, and the invented timezone
would shift the printed day for some readers.

Nullable, and left NULL far more often than not. A year alone never becomes a
date here -- "2026년도 사업" yields document_year = 2026 and document_date =
NULL, because 2026-01-01 would be a fact the document does not state.

Revision ID: 0007_document_date
Revises: 0006_public_document_grant
Create Date: 2026-09-11
"""
from __future__ import annotations

from alembic import op

revision = "0007_document_date"
down_revision = "0006_public_document_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_revisions ADD COLUMN document_date DATE")
    op.execute(
        """
        COMMENT ON COLUMN document_revisions.document_date IS
            '문서 표지/앞부분에 명시된 작성일. 명시적 full date 가 있을 때만
             채운다. 연도만 알 수 있으면 NULL 이며 1월 1일 같은 날짜를
             만들어내지 않는다. document_year 와 독립적으로 판정한다 --
             같은 해의 서로 다른 날짜가 둘 있으면 year 는 확정되지만 date 는
             NULL 이다.'
        """
    )
    # Deliberately no index. Nothing filters or sorts by this column: it is
    # displayed on one document at a time, reached by primary key.


def downgrade() -> None:
    op.execute("ALTER TABLE document_revisions DROP COLUMN IF EXISTS document_date")

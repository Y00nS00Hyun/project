"""Short PostgreSQL transactions for chat and historical source visibility."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from search.repository import READ_ACL_PREDICATE, READ_PERMISSIONS, SearchRepository

from .exceptions import DocumentScopeNotFound, SessionNotFound
from .models import EvidenceChunk, ValidatedAnswer, source_anchor
from .prompts import PROMPT_VERSION
from .validation import refusal


def session_uuid(session_id: str) -> str:
    try:
        return str(UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise SessionNotFound() from None


def _document_uuid(document_id: str) -> str:
    # A malformed id is treated exactly like an unreadable one, so probing with
    # junk and probing with a real id give the same answer.
    try:
        return str(UUID(document_id))
    except (ValueError, TypeError, AttributeError):
        raise DocumentScopeNotFound() from None


def source_from_row(row: dict[str, Any]) -> dict[str, Any]:
    source = {key: str(row[key]) for key in ('document_id', 'revision_id', 'chunk_id')}
    source['accessible'] = bool(row['accessible'])
    if source['accessible']:
        source.update(
            title=row['title'], file_type=row['file_type'], section_title=row['section_title'],
            anchor=source_anchor(row['file_type'], row['paragraph_start'], row['paragraph_end'], row['page_number']),
        )
    return source


def scope_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Shape a session's document scope, hiding the title when unreadable.

    Returning None for an unscoped session keeps "this session is about one
    document" and "this session is about everything" structurally distinct in
    the response, rather than signalling it with a null document_id.

    The title is convenience data about a document, so it obeys the same rule
    as a historical citation: permission is re-checked now, not at the time the
    session was created. A user who has since lost access sees that the session
    is scoped -- they created it -- but not what it was scoped to.
    """
    if row.get('document_id') is None:
        return None
    scope = {'document_id': str(row['document_id']), 'accessible': bool(row['document_accessible'])}
    if scope['accessible']:
        scope['title'] = row['document_title']
    return scope


def public_session(row: dict[str, Any]) -> dict[str, Any]:
    """Replace the raw scope columns with the shaped, ACL-checked scope.

    The internal columns are removed rather than left alongside: document_title
    holds a value the caller may not be entitled to see, and a field that only
    the serializer is trusted to drop is one refactor away from leaking.
    """
    public = {key: value for key, value in row.items()
              if key not in ('document_id', 'document_accessible', 'document_title')}
    public['document_scope'] = scope_from_row(row)
    return public


#: Session columns plus a re-checked document scope. The ACL runs in SQL so an
#: inaccessible title is never fetched into Python in the first place.
SESSION_COLUMNS = f"""
    s.id AS session_id, s.title, s.created_at, s.updated_at, s.document_id,
    (d.id IS NOT NULL AND NOT d.is_deleted AND {READ_ACL_PREDICATE}) AS document_accessible,
    CASE WHEN d.id IS NOT NULL AND NOT d.is_deleted AND {READ_ACL_PREDICATE}
         THEN d.title END AS document_title
"""


class ChatRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    @staticmethod
    def _session(conn, user_id: str, session_id: str, *, lock: bool = False):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT {SESSION_COLUMNS}
                FROM chat_sessions s
                LEFT JOIN documents d ON d.id = s.document_id
                WHERE s.id = %(session_id)s AND s.user_id = %(user_id)s
                """
                # Only the session row is locked. FOR UPDATE cannot be applied
                # to the outer side of a LEFT JOIN, and the document is read
                # here for display only -- the retrieval ACL is enforced again
                # in the candidate query.
                + (' FOR NO KEY UPDATE OF s' if lock else ''),
                {
                    'session_id': session_uuid(session_id), 'user_id': user_id,
                    'read_permissions': list(READ_PERMISSIONS),
                },
            )
            row = cur.fetchone()
            if row is None:
                raise SessionNotFound()
            return dict(row)

    def require_session(self, user_id: str, session_id: str) -> dict[str, Any]:
        """Assert the session belongs to the caller and return its scope."""
        with self.connection_factory() as conn:
            return self._session(conn, user_id, session_id)

    def create_session(self, user_id: str, title: str | None, document_id: str | None = None):
        """Create a session, optionally bound to a single document.

        The permission check is part of the INSERT rather than a SELECT before
        it. A separate check would leave a window in which access is revoked
        between the check and the write, and the resulting session would then
        be scoped to a document the owner may not read.
        """
        if document_id is not None:
            document_id = _document_uuid(document_id)
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                INSERT INTO chat_sessions (user_id, title, document_id)
                SELECT %(user_id)s, %(title)s, %(document_id)s::uuid
                WHERE %(document_id)s::uuid IS NULL
                   OR EXISTS (
                        SELECT 1 FROM documents d
                        WHERE d.id = %(document_id)s::uuid
                          AND NOT d.is_deleted
                          AND {READ_ACL_PREDICATE}
                   )
                RETURNING id AS session_id, title, created_at, updated_at, document_id
                """,
                {
                    'user_id': user_id, 'title': title, 'document_id': document_id,
                    'read_permissions': list(READ_PERMISSIONS),
                },
            )
            row = cur.fetchone()
            if row is None:
                # The WHERE excluded the row: the document does not exist, is
                # deleted, or the caller cannot read it.
                raise DocumentScopeNotFound()
            row = dict(row)
            # Just verified above, so no second ACL round trip.
            row['document_accessible'] = row['document_id'] is not None
            row['document_title'] = None
            if row['document_id'] is not None:
                cur.execute('SELECT title FROM documents WHERE id = %s', (row['document_id'],))
                row['document_title'] = cur.fetchone()['title']
            return public_session(row)

    def list_sessions(self, user_id: str, page: int, size: int):
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute('SELECT count(*) AS total FROM chat_sessions WHERE user_id = %s', (user_id,))
            total = cur.fetchone()['total']
            cur.execute(f'''
                SELECT {SESSION_COLUMNS},
                       (SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count
                FROM chat_sessions s
                LEFT JOIN documents d ON d.id = s.document_id
                WHERE s.user_id = %(user_id)s
                ORDER BY s.updated_at DESC, s.id ASC
                LIMIT %(limit)s OFFSET %(offset)s
            ''', {'user_id': user_id, 'read_permissions': list(READ_PERMISSIONS),
                  'limit': size, 'offset': (page - 1) * size})
            return {'items': [public_session(dict(row)) for row in cur.fetchall()],
                    'page': page, 'size': size, 'total': total}

    def load_context(self, user_id: str, chunk_ids: list[str], max_chars: int) -> list[EvidenceChunk]:
        with self.connection_factory() as conn:
            rows = SearchRepository(conn).load_context_chunks(user_id, chunk_ids, max_chars)
        return [EvidenceChunk(
            document_id=str(row['document_id']), revision_id=str(row['revision_id']),
            chunk_id=str(row['chunk_id']), title=row['title'], file_type=row['file_type'],
            section_title=row['section_title'], text=row['text'],
            anchor=source_anchor(row['file_type'], row['paragraph_start'], row['paragraph_end'], row['page_number']),
        ) for row in rows]

    @staticmethod
    def _sources(conn, user_id: str, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not chunk_ids:
            return {}
        # Historical provenance MUST NOT join current_ready_chunks: promotion
        # cannot rewrite old citations. Only present-day document ACL/visibility
        # is checked. Inaccessible metadata is suppressed in SQL and serialization.
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f'''
                WITH provenance AS (
                    SELECT d.id AS document_id, r.id AS revision_id, c.id AS chunk_id,
                           d.title, d.file_type, c.section_title,
                           c.paragraph_start, c.paragraph_end, c.page_number,
                           (NOT d.is_deleted AND {READ_ACL_PREDICATE}) AS accessible
                    FROM chunks c
                    JOIN document_revisions r ON r.id = c.document_revision_id
                    JOIN documents d ON d.id = r.document_id
                    WHERE c.id = ANY(%(chunk_ids)s::uuid[])
                )
                SELECT document_id, revision_id, chunk_id, accessible,
                       CASE WHEN accessible THEN title END AS title,
                       CASE WHEN accessible THEN file_type END AS file_type,
                       CASE WHEN accessible THEN section_title END AS section_title,
                       CASE WHEN accessible THEN paragraph_start END AS paragraph_start,
                       CASE WHEN accessible THEN paragraph_end END AS paragraph_end,
                       CASE WHEN accessible THEN page_number END AS page_number
                FROM provenance
            ''', {'user_id': user_id, 'read_permissions': list(READ_PERMISSIONS), 'chunk_ids': chunk_ids})
            return {str(row['chunk_id']): source_from_row(dict(row)) for row in cur.fetchall()}

    def get_session(self, user_id: str, session_id: str, page: int, size: int):
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            session = self._session(conn, user_id, session_id)
            cur.execute('SELECT count(*) AS total FROM chat_messages WHERE session_id = %s', (session_id,))
            total = cur.fetchone()['total']
            cur.execute('''
                SELECT id AS message_id, role, content, refused, created_at
                FROM chat_messages WHERE session_id = %s
                ORDER BY created_at ASC, id ASC LIMIT %s OFFSET %s
            ''', (session_id, size, (page - 1) * size))
            messages = [dict(row) for row in cur.fetchall()]
            cur.execute('''
                SELECT message_id, chunk_id FROM chat_message_sources
                WHERE message_id = ANY(%s::uuid[])
                ORDER BY message_id, retrieval_rank NULLS LAST, chunk_id
            ''', ([str(m['message_id']) for m in messages],))
            links = list(cur.fetchall())
            sources = self._sources(conn, user_id, [str(link['chunk_id']) for link in links])
            for message in messages:
                if message['role'] != 'assistant':
                    del message['refused']
                    continue
                attached = [sources[str(link['chunk_id'])] for link in links
                            if link['message_id'] == message['message_id']]
                hidden = any(not source['accessible'] for source in attached)
                message.update(sources=attached, has_inaccessible_sources=hidden, content_hidden=hidden)
                if hidden:
                    message['content'] = None
            return {**public_session(session),
                    'messages': {'items': messages, 'page': page, 'size': size, 'total': total}}

    def save_turn(
        self, user_id: str, session_id: str, question: str, result: ValidatedAnswer,
        context: tuple[EvidenceChunk, ...], provider_id: str, latency_ms: int,
    ):
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._session(conn, user_id, session_id, lock=True)
            sources = self._sources(conn, user_id, [chunk.chunk_id for chunk in context])
            # Recheck ALL context after generation, not just cited sources: an
            # uncited document may also have influenced the answer.
            if any(not sources.get(chunk.chunk_id, {}).get('accessible') for chunk in context):
                result = refusal()
            cur.execute('''
                INSERT INTO chat_messages (session_id, role, content, refused, created_at)
                VALUES (%s, 'user', %s, NULL, clock_timestamp()) RETURNING created_at
            ''', (session_id, question))
            user_created_at = cur.fetchone()['created_at']
            cur.execute('''
                INSERT INTO chat_messages
                    (session_id, role, content, refused, llm_provider, prompt_version,
                     retrieval_pipeline_version, latency_ms, created_at)
                VALUES (%s, 'assistant', %s, %s, %s, %s, 'semantic-current-ready-v1', %s,
                        GREATEST(clock_timestamp(), %s::timestamptz + interval '1 microsecond'))
                RETURNING id AS message_id, created_at
            ''', (session_id, result.answer, result.refused, provider_id, PROMPT_VERSION, latency_ms, user_created_at))
            message = dict(cur.fetchone())
            ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(context, 1)}
            cited = sorted(result.citation_chunk_ids, key=ranks.__getitem__)
            for number, chunk_id in enumerate(cited, 1):
                cur.execute('''
                    INSERT INTO chat_message_sources (message_id, chunk_id, source_label, retrieval_rank)
                    VALUES (%s, %s, %s, %s)
                ''', (message['message_id'], chunk_id, f'SOURCE {number}', ranks[chunk_id]))
            cur.execute('UPDATE chat_sessions SET updated_at = clock_timestamp() WHERE id = %s', (session_id,))
            return {**message, 'answer': result.answer, 'refused': result.refused,
                    'sources': [sources[chunk_id] for chunk_id in cited]}

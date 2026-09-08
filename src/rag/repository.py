"""Short PostgreSQL transactions for chat and historical source visibility."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from search.repository import READ_ACL_PREDICATE, READ_PERMISSIONS, SearchRepository

from .exceptions import SessionNotFound
from .models import EvidenceChunk, ValidatedAnswer, source_anchor
from .prompts import PROMPT_VERSION
from .validation import refusal


def session_uuid(session_id: str) -> str:
    try:
        return str(UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise SessionNotFound() from None


def source_from_row(row: dict[str, Any]) -> dict[str, Any]:
    source = {key: str(row[key]) for key in ('document_id', 'revision_id', 'chunk_id')}
    source['accessible'] = bool(row['accessible'])
    if source['accessible']:
        source.update(
            title=row['title'], file_type=row['file_type'], section_title=row['section_title'],
            anchor=source_anchor(row['file_type'], row['paragraph_start'], row['paragraph_end'], row['page_number']),
        )
    return source


class ChatRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    @staticmethod
    def _session(conn, user_id: str, session_id: str, *, lock: bool = False):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'SELECT id AS session_id, title, created_at, updated_at FROM chat_sessions '
                'WHERE id = %s AND user_id = %s' + (' FOR UPDATE' if lock else ''),
                (session_uuid(session_id), user_id),
            )
            row = cur.fetchone()
            if row is None:
                raise SessionNotFound()
            return dict(row)

    def require_session(self, user_id: str, session_id: str) -> None:
        with self.connection_factory() as conn:
            self._session(conn, user_id, session_id)

    def create_session(self, user_id: str, title: str | None):
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'INSERT INTO chat_sessions (user_id, title) VALUES (%s, %s) '
                'RETURNING id AS session_id, title, created_at, updated_at',
                (user_id, title),
            )
            return dict(cur.fetchone())

    def list_sessions(self, user_id: str, page: int, size: int):
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute('SELECT count(*) AS total FROM chat_sessions WHERE user_id = %s', (user_id,))
            total = cur.fetchone()['total']
            cur.execute('''
                SELECT s.id AS session_id, s.title, s.created_at, s.updated_at,
                       (SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count
                FROM chat_sessions s WHERE s.user_id = %s
                ORDER BY s.updated_at DESC, s.id ASC LIMIT %s OFFSET %s
            ''', (user_id, size, (page - 1) * size))
            return {'items': list(cur.fetchall()), 'page': page, 'size': size, 'total': total}

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
            return {**session, 'messages': {'items': messages, 'page': page, 'size': size, 'total': total}}

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

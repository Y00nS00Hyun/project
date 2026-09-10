"""Document-scoped chat, on a real migrated PostgreSQL and with no network.

The point of these tests is the security property, not the feature. A chat
session bound to one document must be unable to retrieve any other document,
and it must stop working the moment the caller loses permission -- and both
have to hold because of what the SQL does, not because of how a prompt is
worded. So every assertion here is made against the candidate set that
retrieval actually produced, never against the text of an answer.

The provider is a local fake. Nothing in this file reaches an LLM, and no real
document content exists in it to send.
"""

from __future__ import annotations

import pytest

from rag.context import RagConfig
from rag.exceptions import DocumentScopeNotFound, SessionNotFound
from rag.models import GenerationResult
from rag.repository import ChatRepository
from rag.service import ChatService, RagService

# Reusing the search suite's real-PostgreSQL fixtures and corpus builder: the
# scope is enforced in the search candidate query, so it has to be tested
# against the same schema and the same embedder that query runs on.
from test_search_backend import (  # noqa: F401
    Corpus, DeterministicEmbedder, config, conn, connection_factory, corpus,
    dsn, embedder, search_db, server, service,
)


class RecordingProvider:
    """Answers by citing everything it was given, and remembers what that was.

    Citing every chunk is what makes the tests meaningful: the answer can only
    be built from the context, so `seen` is exactly the evidence that reached
    generation. A provider that cited nothing would make every scope assertion
    pass vacuously.
    """

    identifier = 'fake'

    def __init__(self):
        self.seen: list[tuple[str, ...]] = []

    def generate(self, request):
        import json

        context = json.loads(request.document_context_json)
        self.seen.append(tuple(item['chunk_id'] for item in context))
        return GenerationResult(
            answerable=True, answer='근거에 따른 답변입니다.',
            citation_chunk_ids=[item['chunk_id'] for item in context],
        )


@pytest.fixture(autouse=True)
def generation_allowed(monkeypatch):
    """Both switches on: these tests are about scope, not about the gate.

    The provider is a local fake and the corpus is fixture text, so turning
    them on sends nothing anywhere. The gate itself is covered by
    tests/test_external_llm_gate.py.
    """
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('DOCUMENT_EXTERNAL_LLM_ENABLED', 'true')


@pytest.fixture
def provider():
    return RecordingProvider()


@pytest.fixture
def chat_repository(connection_factory):  # noqa: F811
    return ChatRepository(connection_factory)


@pytest.fixture
def chat(chat_repository):
    return ChatService(chat_repository)


@pytest.fixture
def rag(chat_repository, service, provider):  # noqa: F811
    return RagService(chat_repository, service, provider, RagConfig())


@pytest.fixture
def world(corpus):  # noqa: F811
    """Two documents the user can read, one they cannot.

    All three are about "서버 장애" so the deterministic embedder scores them
    identically. Scope therefore cannot be confused with relevance: if a
    document is missing from a candidate set, it is because it was excluded,
    not because it ranked poorly.
    """
    user = corpus.user('reader')
    stranger = corpus.user('stranger')
    a, _ = corpus.document('서버 장애 대응 A', text='서버 장애 발생 시 대응 절차')
    b, _ = corpus.document('서버 장애 대응 B', text='서버 장애 보고 양식')
    secret, _ = corpus.document('서버 장애 기밀', text='서버 장애 기밀 대응 절차')
    corpus.grant(a, user_id=user)
    corpus.grant(b, user_id=user)
    corpus.grant(secret, user_id=stranger)
    return {'user': user, 'stranger': stranger, 'a': a, 'b': b, 'secret': secret}


def revoke(conn, document_id, user_id):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute(
            'DELETE FROM document_permissions WHERE document_id = %s AND user_id = %s',
            (document_id, user_id),
        )


# ---------------------------------------------------------------------------
# Creating a scoped session
# ---------------------------------------------------------------------------

def test_scoped_session_requires_read_permission(chat, world):
    with pytest.raises(DocumentScopeNotFound):
        chat.create_session(world['user'], None, world['secret'])


def test_scoped_session_on_a_missing_or_malformed_document_is_refused(chat, world):
    for document_id in ('00000000-0000-0000-0000-000000000000', 'not-a-uuid'):
        with pytest.raises(DocumentScopeNotFound):
            chat.create_session(world['user'], None, document_id)


def test_scoped_session_on_a_soft_deleted_document_is_refused(chat, conn, corpus, world):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET is_deleted = TRUE, deleted_at = now() WHERE id = %s",
                    (world['a'],))
    with pytest.raises(DocumentScopeNotFound):
        chat.create_session(world['user'], None, world['a'])


def test_a_refused_scope_creates_no_session(chat, world):
    with pytest.raises(DocumentScopeNotFound):
        chat.create_session(world['user'], None, world['secret'])
    assert chat.list_sessions(world['user'], 1, 20)['total'] == 0


def test_unscoped_session_is_still_created_and_reports_no_scope(chat, world):
    session = chat.create_session(world['user'], '전체 검색')
    assert session['document_scope'] is None
    assert 'document_id' not in session and 'document_title' not in session


def test_scoped_session_reports_its_document(chat, world):
    session = chat.create_session(world['user'], None, world['a'])
    assert session['document_scope'] == {
        'document_id': world['a'], 'accessible': True, 'title': '서버 장애 대응 A',
    }


# ---------------------------------------------------------------------------
# Retrieval is confined to the scope
# ---------------------------------------------------------------------------

def _cited_documents(conn, message_id):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute(
            '''
            SELECT DISTINCT r.document_id
            FROM chat_message_sources s
            JOIN chunks c ON c.id = s.chunk_id
            JOIN document_revisions r ON r.id = c.document_revision_id
            WHERE s.message_id = %s
            ''',
            (message_id,),
        )
        return {str(row[0]) for row in cur.fetchall()}


def test_scoped_session_retrieves_only_its_own_document(rag, chat, provider, conn, world):  # noqa: F811
    session = chat.create_session(world['user'], None, world['a'])
    result = rag.send_message(world['user'], str(session['session_id']), '서버 장애 절차는?', 'req-1')

    # Document B is readable and scores identically, so its absence is the
    # scope doing its job.
    assert _cited_documents(conn, result['message_id']) == {world['a']}
    assert len(provider.seen) == 1 and provider.seen[0]


def test_unscoped_session_still_sees_every_permitted_document(rag, chat, conn, world):  # noqa: F811
    session = chat.create_session(world['user'], None)
    result = rag.send_message(world['user'], str(session['session_id']), '서버 장애 절차는?', 'req-2')
    assert _cited_documents(conn, result['message_id']) == {world['a'], world['b']}


def test_no_scope_ever_reaches_an_unpermitted_document(rag, chat, conn, world):  # noqa: F811
    for document_id in (world['a'], None):
        session = chat.create_session(world['user'], None, document_id)
        result = rag.send_message(world['user'], str(session['session_id']), '서버 장애', 'req-3')
        assert world['secret'] not in _cited_documents(conn, result['message_id'])


def test_scope_is_taken_from_the_session_not_the_caller(rag, chat, world):
    """A caller cannot widen or move the scope by asking a different question.

    send_message has no document parameter at all; this asserts the signature
    stays that way, because adding one would put the scope back under client
    control.
    """
    import inspect

    parameters = set(inspect.signature(RagService.send_message).parameters)
    assert parameters == {'self', 'user_id', 'session_id', 'question', 'request_id'}


# ---------------------------------------------------------------------------
# Permission revoked after the session exists
# ---------------------------------------------------------------------------

def test_new_question_is_blocked_after_scope_permission_is_revoked(rag, chat, conn, world):  # noqa: F811
    session = chat.create_session(world['user'], None, world['a'])
    revoke(conn, world['a'], world['user'])
    with pytest.raises(DocumentScopeNotFound):
        rag.send_message(world['user'], str(session['session_id']), '서버 장애 절차는?', 'req-4')


def test_revoked_scope_hides_the_title_but_keeps_the_session(chat, conn, world):  # noqa: F811
    created = chat.create_session(world['user'], '문서 대화', world['a'])
    revoke(conn, world['a'], world['user'])

    detail = chat.get_session(world['user'], str(created['session_id']), 1, 20)
    listed = chat.list_sessions(world['user'], 1, 20)['items'][0]
    for session in (detail, listed):
        assert session['document_scope'] == {'document_id': world['a'], 'accessible': False}
        assert '서버 장애 대응 A' not in str(session)


def test_another_users_scoped_session_is_not_visible(chat, world):
    created = chat.create_session(world['user'], None, world['a'])
    with pytest.raises(SessionNotFound):
        chat.get_session(world['stranger'], str(created['session_id']), 1, 20)


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

def test_new_questions_use_the_new_revision_and_old_citations_keep_the_old_one(
    rag, chat, corpus, conn, world,  # noqa: F811
):
    session = chat.create_session(world['user'], None, world['a'])
    first = rag.send_message(world['user'], str(session['session_id']), '서버 장애 절차는?', 'req-5')

    with conn.cursor() as cur:
        cur.execute('SELECT current_revision_id FROM documents WHERE id = %s', (world['a'],))
        old_revision = str(cur.fetchone()[0])

    new_revision = corpus.revision(world['a'], 2, text='서버 장애 대응 절차 개정본')
    second = rag.send_message(world['user'], str(session['session_id']), '서버 장애 절차는?', 'req-6')

    def revisions(message_id):
        with conn.cursor() as cur:
            cur.execute(
                'SELECT DISTINCT c.document_revision_id FROM chat_message_sources s '
                'JOIN chunks c ON c.id = s.chunk_id WHERE s.message_id = %s',
                (message_id,),
            )
            return {str(row[0]) for row in cur.fetchall()}

    assert revisions(second['message_id']) == {new_revision}
    # Promotion must not rewrite what an earlier answer was based on.
    assert revisions(first['message_id']) == {old_revision}

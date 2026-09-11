"""The public permission principal: every approved account, one row per document.

The operating policy is that any approved account may read any document this
system ingested, because the corpus is general internal documentation and
confidential material is excluded at collection time rather than filtered at
read time.

These tests are about what that policy did NOT change. The ACL still runs
before retrieval, a document with no permission row is still readable by
nobody, per-document and per-user grants still work, and nothing is readable
without a signed-in account. The policy is a row somebody wrote, not a hole in
the predicate.
"""

from __future__ import annotations

import psycopg
import pytest

from search.models import SearchMode, SearchRequest

from test_search_backend import (  # noqa: F401
    Corpus, DeterministicEmbedder, config, conn, connection_factory, corpus, dsn,
    embedder, search_db, server, service,
)


@pytest.fixture
def world(corpus):  # noqa: F811
    reader = corpus.user('reader')
    stranger = corpus.user('stranger')
    public, _ = corpus.document('공개 서버 장애 지침', text='서버 장애 대응 절차')
    private, _ = corpus.document('개별 서버 장애 문서', text='서버 장애 개별 문서')
    orphan, _ = corpus.document('권한 없는 서버 문서', text='서버 장애 미부여')
    corpus.grant_public(public)
    corpus.grant(private, user_id=stranger)
    return {
        'reader': reader, 'stranger': stranger,
        'public': public, 'private': private, 'orphan': orphan,
    }


def visible(service, user_id):  # noqa: F811
    result = service.search(SearchRequest(
        user_id=user_id, query='서버 장애', mode=SearchMode.SEMANTIC, page=1, size=20,
    ))
    return {item.document_id for item in result.items}


def test_a_public_grant_is_readable_by_any_account(service, world):  # noqa: F811
    # Neither account holds a grant of its own on this document.
    assert world['public'] in visible(service, world['reader'])
    assert world['public'] in visible(service, world['stranger'])


def test_a_document_with_no_permission_row_is_readable_by_nobody(service, world):  # noqa: F811
    """Default deny, unchanged.

    The policy adds rows; it does not make the absence of a row mean yes.
    """
    assert world['orphan'] not in visible(service, world['reader'])
    assert world['orphan'] not in visible(service, world['stranger'])


def test_a_grant_to_one_person_stays_that_way(service, world):  # noqa: F811
    assert world['private'] in visible(service, world['stranger'])
    assert world['private'] not in visible(service, world['reader'])


def test_removing_the_public_row_takes_the_document_back(service, conn, world):  # noqa: F811
    """Per-document control still works, with no code and no schema change."""
    assert world['public'] in visible(service, world['reader'])
    with conn.cursor() as cur:
        cur.execute('DELETE FROM document_permissions WHERE document_id = %s AND is_public',
                    (world['public'],))
    assert world['public'] not in visible(service, world['reader'])


def test_an_id_belonging_to_nobody_sees_nothing(service, world):  # noqa: F811
    """"Everyone" means every approved account, and the SQL says so.

    require_user does resolve a real ACTIVE user before this predicate runs, so
    this cannot happen over HTTP -- which is exactly why it is worth pinning.
    A public branch that ignored the caller would hand the corpus to any id at
    all, and nothing inside the ACL would object.
    """
    assert visible(service, '00000000-0000-0000-0000-000000000000') == set()


@pytest.mark.parametrize('column, value', [
    ('is_active', False),
    ('status', 'PENDING'),
    ('status', 'DISABLED'),
])
def test_an_account_that_is_not_approved_sees_nothing(
    service, conn, world, column, value,  # noqa: F811
):
    """Two independent checks refuse these accounts, and this is the second.

    require_user already turns them away, so this is defence in depth -- the
    ACL agreeing with the session layer rather than trusting it.
    """
    assert world['public'] in visible(service, world['reader'])
    with conn.cursor() as cur:
        cur.execute(f'UPDATE users SET {column} = %s WHERE id = %s', (value, world['reader']))
    assert visible(service, world['reader']) == set()


def test_the_public_principal_is_exclusive_with_the_others(conn, corpus):  # noqa: F811
    """The CHECK admits exactly one principal per row."""
    user = corpus.user('someone')
    document, _ = corpus.document('배타성 문서', text='본문')
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, is_public, permission) "
                "VALUES (%s, %s, TRUE, 'READ')",
                (document, user),
            )


def test_a_row_naming_no_principal_at_all_is_refused(conn, corpus):  # noqa: F811
    document, _ = corpus.document('빈 principal', text='본문')
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, permission) VALUES (%s, 'READ')",
                (document,),
            )


def test_a_document_cannot_be_granted_publicly_twice(conn, corpus):  # noqa: F811
    document, _ = corpus.document('중복 방지', text='본문')
    corpus.grant_public(document)
    with pytest.raises(psycopg.errors.UniqueViolation):
        corpus.grant_public(document)


def test_a_soft_deleted_document_stays_invisible_despite_a_public_grant(
    service, conn, corpus, world,  # noqa: F811
):
    """The policy does not reach past the other conditions in the CTE."""
    with conn.cursor() as cur:
        cur.execute('UPDATE documents SET is_deleted = TRUE, deleted_at = now() WHERE id = %s',
                    (world['public'],))
    assert world['public'] not in visible(service, world['reader'])


def test_ingestion_applies_the_configured_policy(conn, config):  # noqa: F811
    """create_document writes the row in the same transaction as the document."""
    from dataclasses import replace

    from ingestion.repository import IngestionRepository

    repo = IngestionRepository(conn)
    without = repo.create_document(
        title='정책 없음', original_filename='a.hwpx', source_path='a.hwpx',
        file_type='hwpx',
    )
    with_public = repo.create_document(
        title='정책 있음', original_filename='b.hwpx', source_path='b.hwpx',
        file_type='hwpx', grant_public_read=True,
    )
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM document_permissions WHERE document_id = %s',
                    (without,))
        assert cur.fetchone()[0] == 0
        cur.execute('SELECT is_public, permission FROM document_permissions '
                    'WHERE document_id = %s', (with_public,))
        assert cur.fetchall() == [(True, 'READ')]
    # The config default is the safe direction: a deployment that says nothing
    # ingests documents nobody can read, which is noticed and fixed in one
    # command.
    assert replace(config).document_access == 'none'


def test_an_unrecognised_policy_value_is_refused(tmp_path):
    """Guessing here would decide who can read the corpus."""
    from ingestion.config import IngestionConfig
    from ingestion.exceptions import ConfigurationError

    root = tmp_path / 'shared'
    root.mkdir()
    with pytest.raises(ConfigurationError):
        IngestionConfig(shared_root=root, document_access='everyone')

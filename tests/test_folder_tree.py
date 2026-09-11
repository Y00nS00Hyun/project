"""Folder navigation, from the filesystem to the API response.

Real PostgreSQL and real files on disk, including folders whose names are CP949
bytes. A test that only used UTF-8 folders would pass while the actual corpus --
Korean directories created on Windows -- produced paths the database cannot
store and names nobody can read.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from support import folder_fixture, pgtest

from ingestion.config import IngestionConfig
from ingestion.path_encoding import display_name
from ingestion.repository import IngestionRepository
from ingestion.sync_service import SyncService
from search.exceptions import InvalidSearchRequestError
from search.folder_paths import escape_like, normalize_folder_path, subtree_prefix
from search.folder_tree import (
    browsable_document_count,
    folder_tree,
    top_level_document_count,
)

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
NOBODY = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for these DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def folder_db(server) -> str:
    return pgtest.migrated_database(server, "folder_tree_test")


@pytest.fixture
def dsn(folder_db) -> str:
    url = pgtest.psycopg_url(folder_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def factory(dsn):
    def make():
        return psycopg.connect(dsn)

    return make


@pytest.fixture
def shared_root(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    folder_fixture.build(root)
    return root


@pytest.fixture
def world(factory, conn, shared_root):
    """Ingest the fixture, then grant per-user access.

    Alice sees 프로젝트_A and the root file. Bob sees 프로젝트_B. Nobody sees
    비공개_프로젝트, which is what makes "the name must not appear" testable.
    """
    SyncService(factory, IngestionConfig(shared_root=shared_root)).scan_once()

    # Mark every revision READY without running the parser: this suite is about
    # the tree, and parsing eight synthetic files would only slow it down.
    conn.execute(
        """
        UPDATE document_revisions
        SET parse_status = 'SUCCESS', parse_result_code = 'TEXT_EXTRACTED',
            embedding_status = 'SUCCESS'
        """
    )
    conn.execute(
        "UPDATE documents d SET current_revision_id = d.latest_revision_id"
    )
    for user_id, subject in ((ALICE, "alice"), (BOB, "bob"), (NOBODY, "nobody")):
        conn.execute(
            "INSERT INTO users (id, sso_subject, is_active) VALUES (%s, %s, TRUE)",
            (user_id, subject),
        )

    def grant(user_id: str, path_fragment: str) -> None:
        conn.execute(
            """
            INSERT INTO document_permissions (document_id, user_id, permission)
            SELECT id, %s, 'READ' FROM documents WHERE source_path LIKE %s
            """,
            (user_id, f"%{path_fragment}%"),
        )

    # Matched on the canonical prefix each project produced.
    grant(ALICE, "프로젝트_A")
    grant(ALICE, "공용안내")
    grant(BOB, "%C7%C1%B7%CE%C1%A7Ʈ_B")
    return shared_root


def paths(nodes) -> set[str]:
    return {n.path for n in nodes}


def names(nodes) -> set[str]:
    return {n.name for n in nodes}


class TestAclFirst:
    def test_a_user_sees_only_folders_holding_documents_they_may_read(self, world, factory):
        tree = folder_tree(factory, ALICE)
        assert paths(tree) == {"프로젝트_A", "프로젝트_A/요구사항", "프로젝트_A/완료"}

    def test_a_project_the_user_cannot_read_is_absent_by_name(self, world, factory):
        """The name of a confidential project is itself information.

        Not greyed out, not empty -- absent.
        """
        for user_id in (ALICE, BOB):
            tree = folder_tree(factory, user_id)
            rendered = " ".join(n.path + n.name for n in tree)
            assert "비공개" not in rendered
            assert "기밀" not in rendered
            assert "%BA%F1%B0%F8" not in rendered

    def test_a_user_with_no_permissions_sees_nothing(self, world, factory):
        assert folder_tree(factory, NOBODY) == []

    def test_two_users_see_disjoint_trees(self, world, factory):
        assert paths(folder_tree(factory, ALICE)).isdisjoint(paths(folder_tree(factory, BOB)))

    def test_an_unknown_user_sees_nothing(self, world, factory):
        assert folder_tree(factory, "00000000-0000-0000-0000-000000000000") == []
        assert folder_tree(factory, "") == []

    def test_a_document_that_is_not_ready_contributes_no_folder(self, world, factory, conn):
        conn.execute(
            """
            UPDATE document_revisions SET embedding_status = 'PENDING'
            WHERE document_id IN (
                SELECT id FROM documents WHERE source_path LIKE '프로젝트_A/완료%%')
            """
        )
        assert "프로젝트_A/완료" not in paths(folder_tree(factory, ALICE))

    def test_a_soft_deleted_document_contributes_no_folder(self, world, factory, conn):
        conn.execute(
            # deleted_at too: the schema CHECK ties the flag to the timestamp,
            # and soft_delete_expired_missing sets both.
            "UPDATE documents SET is_deleted = TRUE, deleted_at = now() "
            "WHERE source_path LIKE '프로젝트_A/완료%%'"
        )
        assert "프로젝트_A/완료" not in paths(folder_tree(factory, ALICE))


class TestLegacyEncodedFolders:
    def test_a_cp949_folder_is_reachable_and_readable(self, world, factory):
        tree = folder_tree(factory, BOB)
        # The path is canonical -- escapes and all -- and the name is Korean.
        assert names(tree) == {"프로젝트_B", "제안", "운영"}
        assert all("%" in n.path or n.path.isprintable() for n in tree)

    def test_path_and_name_are_not_interchangeable(self, world, factory):
        """A client joining names would build a path that matches nothing."""
        root = next(n for n in folder_tree(factory, BOB) if n.depth == 1)
        assert root.name == "프로젝트_B"
        assert root.path != root.name
        assert display_name(root.path) == root.name

    def test_parent_path_is_canonical_not_display(self, world, factory):
        child = next(n for n in folder_tree(factory, BOB) if n.depth == 2)
        parent = next(n for n in folder_tree(factory, BOB) if n.depth == 1)
        assert child.parent_path == parent.path

    def test_no_stored_path_is_unstorable(self, world, factory):
        for node in folder_tree(factory, ALICE) + folder_tree(factory, BOB):
            node.path.encode("utf-8")


class TestTreeShape:
    def test_depth_and_parent_are_consistent(self, world, factory):
        tree = folder_tree(factory, ALICE)
        by_path = {n.path: n for n in tree}
        for node in tree:
            if node.depth == 1:
                assert node.parent_path is None
            else:
                assert node.parent_path in by_path

    def test_a_root_level_file_creates_no_folder(self, world, factory):
        """공용안내.hwpx sits at the root; it is a document, not a folder."""
        assert all(n.path != "공용안내.hwpx" for n in folder_tree(factory, ALICE))

    def test_parent_counts_its_whole_subtree(self, world, factory):
        tree = {n.path: n for n in folder_tree(factory, ALICE)}
        assert tree["프로젝트_A/요구사항"].document_count == 2
        assert tree["프로젝트_A/완료"].document_count == 1
        assert tree["프로젝트_A"].document_count == 3

    def test_no_absolute_path_leaks(self, world, factory, shared_root):
        blob = " ".join(n.path + n.name for n in folder_tree(factory, ALICE))
        assert str(shared_root) not in blob
        assert "/tmp" not in blob
        assert not blob.startswith("/")


class TestFolderPathValidation:
    @pytest.mark.parametrize(
        "hostile",
        ["/etc/passwd", "../outside", "a/../b", "a/./b", "a//b", "a\\b", "%ZZ", "%G0"],
    )
    def test_hostile_paths_are_rejected(self, hostile):
        with pytest.raises(InvalidSearchRequestError):
            normalize_folder_path(hostile)

    def test_blank_means_no_folder(self):
        assert normalize_folder_path(None) is None
        assert normalize_folder_path("") is None
        assert normalize_folder_path("   ") is None

    def test_a_canonical_path_passes_through(self):
        assert normalize_folder_path("프로젝트_A/요구사항") == "프로젝트_A/요구사항"
        assert normalize_folder_path("%C7%C1_B") == "%C7%C1_B"


class TestLikeEscaping:
    def test_wildcards_in_a_canonical_path_are_escaped(self):
        """A canonical path is full of '%' and file names are full of '_'."""
        assert escape_like("%C7%C1_A") == "\\%C7\\%C1\\_A"

    def test_backslash_is_escaped_first(self):
        assert escape_like("a\\b") == "a\\\\b"

    def test_the_prefix_ends_at_the_separator(self):
        assert subtree_prefix("프로젝트_A") == "프로젝트\\_A/"
        assert subtree_prefix("프로젝트_A/") == "프로젝트\\_A/"


class TestSubtreeSearch:
    def _search(self, factory, user_id, folder_path=None, **kwargs):
        from search.models import SearchRequest
        from search.repository import SearchRepository

        request = SearchRequest(user_id=user_id, folder_path=folder_path, **kwargs)
        with factory() as conn:
            rows, total = SearchRepository(conn).browse(
                user_id=user_id,
                department_id=None,
                year=None,
                tag_ids=(),
                file_type=None,
                folder_prefix=request.folder_prefix,
                limit=50,
                offset=0,
            )
        return {r["title"] for r in rows}, total

    def test_selecting_a_parent_searches_the_whole_subtree(self, world, factory):
        titles, total = self._search(factory, ALICE, "프로젝트_A")
        assert total == 3
        assert titles == {"요구사항정의서", "기능명세", "완료보고서"}

    def test_selecting_a_child_searches_only_that_child(self, world, factory):
        titles, total = self._search(factory, ALICE, "프로젝트_A/요구사항")
        assert total == 2
        assert titles == {"요구사항정의서", "기능명세"}

    def test_no_folder_searches_everything_visible(self, world, factory):
        _, total = self._search(factory, ALICE)
        assert total == 4  # three in 프로젝트_A plus the root-level file

    def test_a_folder_the_user_cannot_read_returns_nothing(self, world, factory):
        _, total = self._search(factory, ALICE, "%C7%C1%B7%CE%C1%A7Ʈ_B")
        assert total == 0

    def test_a_cp949_folder_filters_correctly(self, world, factory):
        titles, total = self._search(factory, BOB, "%C7%C1%B7%CE%C1%A7Ʈ_B/%C1%A6%BE%C8")
        assert total == 1
        assert {display_name(t) for t in titles} == {"제안요청서"}

    def test_the_boundary_is_the_separator(self, world, factory, conn, shared_root, factory2=None):
        """"프로젝트_A" must not match a sibling named "프로젝트_A2"."""
        sibling = shared_root / "프로젝트_A2"
        sibling.mkdir()
        from support import builders

        builders.write_hwpx(sibling / "다른문서.hwpx", text="다른 프로젝트")
        SyncService(factory, IngestionConfig(shared_root=shared_root)).scan_once()
        conn.execute(
            """
            UPDATE document_revisions SET parse_status='SUCCESS',
                parse_result_code='TEXT_EXTRACTED', embedding_status='SUCCESS'
            """
        )
        conn.execute("UPDATE documents d SET current_revision_id = d.latest_revision_id")
        conn.execute(
            """
            INSERT INTO document_permissions (document_id, user_id, permission)
            SELECT id, %s, 'READ' FROM documents WHERE source_path LIKE '프로젝트_A2%%'
            ON CONFLICT DO NOTHING
            """,
            (ALICE,),
        )

        titles, total = self._search(factory, ALICE, "프로젝트_A")
        assert "다른문서" not in titles
        assert total == 3

    def test_a_percent_in_the_path_is_not_a_wildcard(self, world, factory):
        """Without escaping, "%C7%C1..." would match nearly every path."""
        _, total = self._search(factory, ALICE, "%C7%C1%B7%CE%C1%A7Ʈ_B")
        assert total == 0  # Alice may not read 프로젝트_B, and % matched nothing


class TestTreeIsNotAFacet:
    """The tree is navigation and must stay stable while filters change.

    A folder holding only manuals must not disappear when the user filters for
    reports; selecting it should simply return nothing.
    """

    def test_the_tree_ignores_year_kind_and_file_type(self, world, factory, conn):
        before = paths(folder_tree(factory, ALICE))
        conn.execute("UPDATE document_revisions SET document_year = 2026")
        assert paths(folder_tree(factory, ALICE)) == before

    def test_folder_tree_takes_no_filter_arguments(self):
        import inspect

        params = set(inspect.signature(folder_tree).parameters)
        assert params == {"connection_factory", "user_id"}


# ---------------------------------------------------------------------------
# The root count
# ---------------------------------------------------------------------------

class TestBrowsableDocumentCount:
    """What the tree's root row stands for: the whole shared folder.

    Deliberately not derivable from the tree. A document at the top of the
    shared folder contributes no folder row, so summing the depth-1 counts
    would under-report -- and in a corpus with no subdirectories at all it
    would report zero while the user can browse everything. That is exactly the
    corpus this was built for.
    """

    def test_it_counts_a_top_level_document_that_makes_no_folder(self, world, factory):
        # Alice may read 공용안내.hwpx at the top plus three under 프로젝트_A.
        # The tree can only account for the three.
        tree_total = sum(node.document_count for node in folder_tree(factory, ALICE)
                         if node.depth == 1)
        assert tree_total == 3
        assert browsable_document_count(factory, ALICE) == 4

    def test_it_counts_the_same_set_the_tree_is_built_from(self, world, factory):
        # Bob's two documents both live in folders, so here the two agree --
        # which is what makes the mismatch above a property of top-level files
        # rather than of the counting.
        tree_total = sum(node.document_count for node in folder_tree(factory, BOB)
                         if node.depth == 1)
        assert tree_total == browsable_document_count(factory, BOB) == 2

    def test_an_unreadable_document_is_not_counted(self, world, factory):
        # 비공개_프로젝트 is granted to nobody, and NOBODY holds no grant at all.
        assert folder_tree(factory, NOBODY) == []
        assert browsable_document_count(factory, NOBODY) == 0

    def test_a_revision_that_is_not_ready_is_not_counted(self, world, factory, conn):
        before = browsable_document_count(factory, ALICE)
        conn.execute("UPDATE document_revisions SET embedding_status = 'PENDING'")
        # Same condition the tree uses, so the root cannot claim documents that
        # searching would not return.
        assert browsable_document_count(factory, ALICE) == 0
        assert folder_tree(factory, ALICE) == []
        assert before == 4

    def test_an_empty_user_id_counts_nothing(self, factory):
        assert browsable_document_count(factory, "") == 0


class TestTopLevelDocuments:
    """Documents in no folder at all.

    They contribute no folder row, so the tree alone cannot account for them --
    which in a flat shared folder is the whole corpus. Counted separately, over
    the same ACL-filtered set the tree is built from.
    """

    def test_it_counts_documents_that_produce_no_folder_row(self, world, factory):
        # Alice reads 공용안내.hwp at the top and three under 프로젝트_A.
        assert top_level_document_count(factory, ALICE) == 1
        assert browsable_document_count(factory, ALICE) == 4

    def test_the_two_counts_plus_the_folders_account_for_everything(self, world, factory):
        top = top_level_document_count(factory, ALICE)
        in_folders = sum(node.document_count for node in folder_tree(factory, ALICE)
                         if node.depth == 1)
        assert top + in_folders == browsable_document_count(factory, ALICE)

    def test_a_user_whose_documents_are_all_in_folders_has_none(self, world, factory):
        # Bob reads only 프로젝트_B/제안 and 프로젝트_B/운영.
        assert top_level_document_count(factory, BOB) == 0
        assert browsable_document_count(factory, BOB) == 2

    def test_an_unreadable_top_level_document_is_not_counted(self, world, factory):
        assert top_level_document_count(factory, NOBODY) == 0

    def test_an_empty_user_id_counts_nothing(self, factory):
        assert top_level_document_count(factory, "") == 0

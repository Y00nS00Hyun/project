"""EMBED job, READY and current-revision promotion, on a real PostgreSQL.

The promotion tests are the important ones: `current_revision_id` decides what
search shows, and getting it wrong either hides a good document or exposes an
unembedded one.

Most tests use a deterministic fake model so they stay fast; a separate class
runs the *real* multilingual-e5-small so vector correctness is not asserted
from a stub alone.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import psycopg
import pytest

from ingestion.config import IngestionConfig
from ingestion.embedding import PASSAGE_PREFIX
from ingestion.embedding_service import RESULT_NO_CHUNKS, EmbeddingService
from ingestion.ingestion_service import IngestionService
from ingestion.repository import IngestionRepository
from ingestion.sync_service import SyncService
from ingestion.tokenizers import SimpleTokenizer

from support import builders, pgtest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeModel:
    """Deterministic unit-norm vectors. Records what it was asked to embed."""

    name = "fake-model"
    revision = "fake-revision"
    dimension = 384

    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self.seen: list[str] = []
        self.calls = 0

    def embed_passages(self, texts):
        self.calls += 1
        self.seen.extend(texts)
        out = []
        for index, _text in enumerate(texts):
            vector = [0.0] * self.dimension
            vector[index % self.dimension] = 1.0
            out.append(vector)
        return out


class BrokenModel:
    name, revision, dimension = "broken", None, 384

    def embed_passages(self, texts):
        raise RuntimeError("model exploded")


class WrongDimensionModel(FakeModel):
    def __init__(self):
        super().__init__(dimension=128)


class NaNModel(FakeModel):
    def embed_passages(self, texts):
        return [[float("nan")] * 384 for _ in texts]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for embedding DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def embed_db(server) -> str:
    return pgtest.migrated_database(server, "embedding_test")


@pytest.fixture
def dsn(embed_db) -> str:
    url = pgtest.psycopg_url(embed_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def connection_factory(dsn):
    def factory():
        return psycopg.connect(dsn)

    return factory


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def shared_root(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


@pytest.fixture
def config(shared_root) -> IngestionConfig:
    return IngestionConfig(
        shared_root=shared_root, chunk_target_tokens=16, chunk_max_tokens=16,
        embedding_batch_size=2,
    )


@pytest.fixture
def ingest(connection_factory, config):
    """Sync + parse (queues EMBED jobs but does not run the model)."""

    def run():
        SyncService(connection_factory, config).scan_once()
        return IngestionService(
            connection_factory, config, tokenizer=SimpleTokenizer()
        ).process_pending()

    return run


def embed(connection_factory, config, model=None, limit=100):
    return EmbeddingService(connection_factory, config, model=model or FakeModel()).process_pending(limit)


def scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# EMBED job creation
# ---------------------------------------------------------------------------

class TestEmbedJobEnqueue:
    def test_text_extracted_gets_an_embed_job(self, ingest, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="2026년 사업 예산은 3억원이다.")
        ingest()
        assert rows(conn, "SELECT job_type, status FROM processing_jobs ORDER BY job_type") == [
            ("EMBED", "PENDING"), ("PARSE", "SUCCESS")
        ]

    @pytest.mark.parametrize(
        ("name", "builder"),
        [
            ("empty.hwpx", lambda p: builders.write_hwpx(p, text="")),
            ("broken.hwpx", builders.write_not_a_zip),
            ("scan.hwpx", lambda p: builders.write_hwpx(
                p, text="", extra_parts={"BinData/i.png": "x"})),
        ],
    )
    def test_non_text_results_get_no_embed_job(self, ingest, conn, shared_root, name, builder):
        builder(shared_root / name)
        ingest()
        assert scalar(conn, "SELECT count(*) FROM processing_jobs WHERE job_type='EMBED'") == 0
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "SKIPPED"

    def test_no_duplicate_active_embed_job(self, ingest, connection_factory, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        revision_id = scalar(conn, "SELECT id FROM document_revisions")
        with psycopg.connect(conn.info.dsn, autocommit=True) as c:
            repo = IngestionRepository(c)
            assert repo.enqueue_embed_job(str(revision_id)) is None
            assert repo.enqueue_embed_job(str(revision_id)) is None
        assert scalar(conn, "SELECT count(*) FROM processing_jobs WHERE job_type='EMBED'") == 1

    def test_already_embedded_revision_is_not_requeued(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        embed(connection_factory, config)
        revision_id = scalar(conn, "SELECT id FROM document_revisions")
        with psycopg.connect(conn.info.dsn, autocommit=True) as c:
            assert IngestionRepository(c).enqueue_embed_job(str(revision_id)) is None


# ---------------------------------------------------------------------------
# Vector persistence
# ---------------------------------------------------------------------------

class TestVectorPersistence:
    def test_vectors_are_written_for_every_chunk(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(60)))
        ingest()
        chunk_count = scalar(conn, "SELECT count(*) FROM chunks")
        result = embed(connection_factory, config)

        assert result.succeeded == 1
        assert result.vectors_written == chunk_count
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0

    def test_service_passes_raw_chunk_text_to_the_model(
        self, ingest, connection_factory, config, shared_root
    ):
        """The `passage: ` prefix is the model's job, not the service's.

        e5 needs that prefix; another model would need a different one or none.
        Prefixing in the service would bake e5 semantics into generic
        orchestration, so the service hands over the chunk text unchanged and
        LocalE5Model adds the prefix (see test_embedding.py).
        """
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        ingest()
        model = FakeModel()
        EmbeddingService(connection_factory, config, model=model).process_pending()
        assert model.seen, "model should have been called"
        assert not any(text.startswith(PASSAGE_PREFIX) for text in model.seen)
        assert any("본문입니다" in text for text in model.seen)

    def test_batching_is_respected(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(120)))
        ingest()
        chunk_count = scalar(conn, "SELECT count(*) FROM chunks")
        assert chunk_count > 2, "need several chunks to exercise batching"
        model = FakeModel()
        EmbeddingService(connection_factory, config, model=model).process_pending()
        # batch size 2 -> ceil(chunks / 2) calls
        assert model.calls == math.ceil(chunk_count / config.embedding_batch_size)

    def test_stored_vector_has_the_right_dimension(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        embed(connection_factory, config)
        assert scalar(conn, "SELECT vector_dims(embedding) FROM chunks LIMIT 1") == 384

    def test_provenance_is_recorded(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        embed(connection_factory, config)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT embedding_provider, embedding_model, embedding_dimension,
                       embedding_version, embedded_at
                FROM document_revisions
            """)
            provider, model, dimension, version, embedded_at = cur.fetchone()
        assert provider == "local"
        assert model == "intfloat/multilingual-e5-small"
        assert dimension == 384
        assert version == config.embedding_model_revision
        assert embedded_at is not None


# ---------------------------------------------------------------------------
# READY
# ---------------------------------------------------------------------------

class TestReady:
    def test_not_ready_while_embedding_pending(self, ingest, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        with conn.cursor() as cur:
            cur.execute("SELECT embedding_status, is_ready FROM document_revisions")
            assert cur.fetchone() == ("PENDING", False)

    def test_ready_after_embedding_success(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        embed(connection_factory, config)
        with conn.cursor() as cur:
            cur.execute("SELECT embedding_status, is_ready FROM document_revisions")
            assert cur.fetchone() == ("SUCCESS", True)

    def test_not_ready_for_ocr_required(self, ingest, conn, shared_root):
        builders.write_hwpx(shared_root / "scan.hwpx", text="",
                            extra_parts={"BinData/i.png": "x"})
        ingest()
        with conn.cursor() as cur:
            cur.execute("SELECT parse_result_code, embedding_status, is_ready FROM document_revisions")
            assert cur.fetchone() == ("OCR_REQUIRED", "SKIPPED", False)

    def test_is_ready_is_computed_by_the_database(self, ingest, connection_factory, config, conn, shared_root):
        """The application never writes is_ready; flipping status flips it."""
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        embed(connection_factory, config)
        assert scalar(conn, "SELECT is_ready FROM document_revisions") is True
        with conn.cursor() as cur:
            cur.execute("UPDATE document_revisions SET embedding_status='FAILED'")
        assert scalar(conn, "SELECT is_ready FROM document_revisions") is False


# ---------------------------------------------------------------------------
# Current revision promotion
# ---------------------------------------------------------------------------

def add_revision(conn, document_id, revision_no, *, ready: bool) -> str:
    """Insert a revision directly, to build promotion scenarios precisely."""
    embedding_status = "SUCCESS" if ready else "PENDING"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_revisions
                (document_id, revision_no, content_hash, source_path_at_ingest,
                 parse_status, parse_result_code, embedding_status)
            VALUES (%s, %s, %s, 'a.hwpx', 'SUCCESS', 'TEXT_EXTRACTED', %s)
            RETURNING id
            """,
            (document_id, revision_no, f"hash{revision_no}", embedding_status),
        )
        return str(cur.fetchone()[0])


class TestPromotion:
    @pytest.fixture
    def document_id(self, conn) -> str:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type) "
                "VALUES ('문서','a.hwpx','a.hwpx','hwpx') RETURNING id"
            )
            return str(cur.fetchone()[0])

    def promote(self, conn, document_id):
        with psycopg.connect(conn.info.dsn) as c:
            promoted = IngestionRepository(c).promote_current_revision(document_id)
            c.commit()
        return promoted

    def test_first_ready_revision_is_promoted(self, conn, document_id):
        revision = add_revision(conn, document_id, 1, ready=True)
        assert self.promote(conn, document_id) == revision
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == revision

    def test_nothing_promoted_when_no_revision_is_ready(self, conn, document_id):
        add_revision(conn, document_id, 1, ready=False)
        assert self.promote(conn, document_id) is None
        assert scalar(conn, "SELECT current_revision_id FROM documents") is None

    def test_newer_pending_does_not_change_current(self, conn, document_id):
        first = add_revision(conn, document_id, 1, ready=True)
        self.promote(conn, document_id)
        add_revision(conn, document_id, 2, ready=False)
        self.promote(conn, document_id)
        # revision 2 is not READY, so the working revision 1 stays searchable.
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == first

    def test_newer_ready_is_promoted(self, conn, document_id):
        add_revision(conn, document_id, 1, ready=True)
        self.promote(conn, document_id)
        second = add_revision(conn, document_id, 2, ready=True)
        assert self.promote(conn, document_id) == second

    def test_newer_failure_keeps_the_old_current(self, conn, document_id):
        first = add_revision(conn, document_id, 1, ready=True)
        self.promote(conn, document_id)
        second = add_revision(conn, document_id, 2, ready=False)
        with conn.cursor() as cur:
            cur.execute("UPDATE document_revisions SET embedding_status='FAILED' WHERE id=%s", (second,))
        self.promote(conn, document_id)
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == first

    def test_out_of_order_completion_does_not_downgrade(self, conn, document_id):
        """A slow revision 1 finishing after revision 2 must not drag current back."""
        add_revision(conn, document_id, 1, ready=False)
        second = add_revision(conn, document_id, 2, ready=True)
        self.promote(conn, document_id)
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == second

        # revision 1 finishes late.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET embedding_status='SUCCESS' WHERE revision_no=1"
            )
        self.promote(conn, document_id)
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == second

    def test_latest_may_differ_from_current(self, conn, document_id):
        add_revision(conn, document_id, 1, ready=True)
        second = add_revision(conn, document_id, 2, ready=True)
        third = add_revision(conn, document_id, 3, ready=False)
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET latest_revision_id=%s", (third,))
        self.promote(conn, document_id)
        with conn.cursor() as cur:
            cur.execute("SELECT latest_revision_id, current_revision_id FROM documents")
            latest, current = cur.fetchone()
        assert str(latest) == third
        assert str(current) == second, "current is the newest READY, not the newest"

    def test_promotion_is_idempotent(self, conn, document_id):
        revision = add_revision(conn, document_id, 1, ready=True)
        for _ in range(3):
            assert self.promote(conn, document_id) == revision

    def test_current_always_points_at_a_ready_revision_of_the_same_document(
        self, conn, document_id
    ):
        add_revision(conn, document_id, 1, ready=True)
        add_revision(conn, document_id, 2, ready=False)
        self.promote(conn, document_id)
        assert scalar(conn, """
            SELECT count(*) FROM documents d
            JOIN document_revisions r ON r.id = d.current_revision_id
            WHERE r.document_id = d.id AND r.is_ready = TRUE
        """) == 1

    def test_cross_document_pointer_is_still_impossible(self, conn, document_id):
        """The composite FK from the migration must still hold."""
        other_revision = None
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type) "
                "VALUES ('다른','b.hwpx','b.hwpx','hwpx') RETURNING id"
            )
            other = str(cur.fetchone()[0])
        other_revision = add_revision(conn, other, 1, ready=True)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET current_revision_id=%s WHERE id=%s",
                    (other_revision, document_id),
                )
                cur.execute("SET CONSTRAINTS ALL IMMEDIATE")


class TestPromotionThroughTheService:
    def test_pipeline_promotes_on_first_success(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        assert scalar(conn, "SELECT current_revision_id FROM documents") is None

        result = embed(connection_factory, config)
        assert result.promoted == 1
        current = scalar(conn, "SELECT current_revision_id FROM documents")
        assert current is not None
        assert scalar(
            conn, "SELECT is_ready FROM document_revisions WHERE id=%s", (current,)
        ) is True

    def test_new_revision_keeps_old_current_until_embedded(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="첫 번째 본문")
        ingest()
        embed(connection_factory, config)
        first_current = scalar(conn, "SELECT current_revision_id FROM documents")

        builders.write_hwpx(shared_root / "a.hwpx", text="두 번째 본문으로 수정")
        ingest()
        # Parsed and chunked but not embedded yet.
        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == str(first_current)
        assert scalar(conn, "SELECT count(*) FROM document_revisions") == 2

        embed(connection_factory, config)
        new_current = scalar(conn, "SELECT current_revision_id FROM documents")
        assert str(new_current) != str(first_current)
        assert scalar(
            conn, "SELECT revision_no FROM document_revisions WHERE id=%s", (new_current,)
        ) == 2

    def test_failed_new_revision_keeps_the_old_one_searchable(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="첫 번째 본문")
        ingest()
        embed(connection_factory, config)
        first_current = scalar(conn, "SELECT current_revision_id FROM documents")

        builders.write_hwpx(shared_root / "a.hwpx", text="두 번째 본문")
        ingest()
        embed(connection_factory, config, model=BrokenModel())

        assert str(scalar(conn, "SELECT current_revision_id FROM documents")) == str(first_current)
        assert scalar(
            conn, "SELECT embedding_status FROM document_revisions WHERE revision_no=2"
        ) == "FAILED"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailures:
    def _one_doc(self, ingest, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(40)))
        ingest()

    def test_model_exception_marks_failed_without_partial_vectors(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        self._one_doc(ingest, shared_root)
        result = embed(connection_factory, config, model=BrokenModel())

        assert result.failed == 1
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "FAILED"
        assert scalar(conn, "SELECT is_ready FROM document_revisions") is False
        # No half-embedded revision.
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 0

    def test_wrong_dimension_is_rejected_before_writing(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        self._one_doc(ingest, shared_root)
        embed(connection_factory, config, model=WrongDimensionModel())
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "FAILED"
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 0
        assert scalar(
            conn, "SELECT result_code FROM processing_jobs WHERE job_type='EMBED'"
        ) == "INVALID_VECTOR"

    def test_nan_vector_is_rejected(self, ingest, connection_factory, config, conn, shared_root):
        self._one_doc(ingest, shared_root)
        embed(connection_factory, config, model=NaNModel())
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "FAILED"
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 0

    def test_job_records_result_code_and_message(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        self._one_doc(ingest, shared_root)
        embed(connection_factory, config, model=BrokenModel())
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result_code, error_message FROM processing_jobs WHERE job_type='EMBED'"
            )
            status, code, message = cur.fetchone()
        assert (status, code) == ("FAILED", "EMBED_FAILED")
        assert "RuntimeError" in message

    def test_error_message_does_not_contain_chunk_text(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="기밀유지대상문장입니다")
        ingest()
        embed(connection_factory, config, model=BrokenModel())
        message = scalar(
            conn, "SELECT error_message FROM processing_jobs WHERE job_type='EMBED'"
        )
        assert "기밀유지대상문장" not in message

    def test_one_failure_does_not_block_other_revisions(
        self, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="첫 번째 문서 본문")
        builders.write_hwpx(shared_root / "b.hwpx", text="두 번째 문서 본문")
        SyncService(connection_factory, config).scan_once()
        IngestionService(connection_factory, config, tokenizer=SimpleTokenizer()).process_pending()

        class FlakyModel(FakeModel):
            def __init__(self, fail_on: str):
                super().__init__()
                self.fail_on = fail_on

            def embed_passages(self, texts):
                if any(self.fail_on in t for t in texts):
                    raise RuntimeError("boom")
                return super().embed_passages(texts)

        result = embed(connection_factory, config, model=FlakyModel("첫 번째"))
        assert result.processed == 2
        assert result.succeeded == 1 and result.failed == 1
        assert scalar(
            conn, "SELECT count(*) FROM document_revisions WHERE embedding_status='SUCCESS'"
        ) == 1

    def test_revision_with_no_chunks_is_not_marked_success(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunks")
        embed(connection_factory, config)
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "FAILED"
        assert scalar(
            conn, "SELECT result_code FROM processing_jobs WHERE job_type='EMBED'"
        ) == RESULT_NO_CHUNKS


class TestRetryAndIdempotency:
    def test_failed_job_can_be_retried_successfully(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문 내용")
        ingest()
        embed(connection_factory, config, model=BrokenModel())
        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "FAILED"

        job_id = scalar(conn, "SELECT id FROM processing_jobs WHERE job_type='EMBED'")
        with psycopg.connect(conn.info.dsn, autocommit=True) as c:
            IngestionRepository(c).reset_job_for_retry(str(job_id))
        embed(connection_factory, config)

        assert scalar(conn, "SELECT embedding_status FROM document_revisions") == "SUCCESS"
        assert scalar(conn, "SELECT is_ready FROM document_revisions") is True
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0

    def test_retry_replaces_vectors_rather_than_mixing_them(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(40)))
        ingest()
        embed(connection_factory, config)
        first = rows(conn, "SELECT id, embedding::text FROM chunks ORDER BY chunk_index")

        class ShiftedModel(FakeModel):
            def embed_passages(self, texts):
                out = []
                for index, _ in enumerate(texts):
                    vector = [0.0] * 384
                    vector[(index + 7) % 384] = 1.0
                    out.append(vector)
                return out

        revision_id = scalar(conn, "SELECT id FROM document_revisions")
        with conn.cursor() as cur:
            cur.execute("UPDATE document_revisions SET embedding_status='PENDING'")
            cur.execute(
                "INSERT INTO processing_jobs (document_revision_id, job_type, status) "
                "VALUES (%s,'EMBED','PENDING')", (revision_id,)
            )
        embed(connection_factory, config, model=ShiftedModel())

        second = rows(conn, "SELECT id, embedding::text FROM chunks ORDER BY chunk_index")
        assert len(second) == len(first)
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0
        assert [v for _, v in second] != [v for _, v in first], "vectors must be replaced"

    def test_rerunning_the_stage_does_not_reembed(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        model = FakeModel()
        EmbeddingService(connection_factory, config, model=model).process_pending()
        calls_after_first = model.calls

        # No pending jobs remain, so a second run is a no-op.
        result = EmbeddingService(connection_factory, config, model=model).process_pending()
        assert result.processed == 0
        assert model.calls == calls_after_first

    def test_full_pipeline_is_idempotent(self, ingest, connection_factory, config, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        builders.write_hwpx(shared_root / "b.hwpx", text="다른 본문")
        for _ in range(3):
            ingest()
            embed(connection_factory, config)

        def snapshot():
            return {
                t: scalar(conn, f"SELECT count(*) FROM {t}")
                for t in ("documents", "document_revisions", "processing_jobs", "chunks")
            }

        assert snapshot() == {
            "documents": 2, "document_revisions": 2, "processing_jobs": 4, "chunks": 2
        }
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0


# ---------------------------------------------------------------------------
# Real model
# ---------------------------------------------------------------------------

def model_is_cached() -> bool:
    """Is multilingual-e5-small already in a local HF cache?"""
    import glob

    roots = [
        os.environ.get("EMBEDDING_CACHE_DIR"),
        os.environ.get("HF_HOME"),
        os.path.expanduser("~/.cache/huggingface"),
    ]
    for root in roots:
        if not root:
            continue
        if glob.glob(os.path.join(root, "**", "models--intfloat--multilingual-e5-small"),
                     recursive=True):
            return True
    return False


@pytest.mark.skipif(
    not model_is_cached(),
    reason="multilingual-e5-small is not in a local cache; "
           "pre-populate it (downloads are disabled by design)",
)
class TestRealModel:
    """Runs the actual embedding model.

    Vector correctness is asserted here, not from a stub: a fake would happily
    report 384 unit-norm dimensions no matter what the real model does.
    """

    def test_real_model_produces_valid_stored_vectors(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="2026년 사업 예산은 300,000,000원이다.")
        ingest()

        result = EmbeddingService(connection_factory, config).process_pending()
        assert result.succeeded == 1

        assert scalar(conn, "SELECT vector_dims(embedding) FROM chunks LIMIT 1") == 384
        assert scalar(conn, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0
        # pgvector computes the L2 norm; the stored vector must be a unit vector.
        norm = scalar(conn, "SELECT vector_norm(embedding) FROM chunks LIMIT 1")
        assert abs(norm - 1.0) < 1e-3
        assert scalar(conn, "SELECT is_ready FROM document_revisions") is True
        assert scalar(conn, "SELECT current_revision_id FROM documents") is not None

    def test_real_model_vectors_are_semantically_ordered(
        self, ingest, connection_factory, config, conn, shared_root
    ):
        """A sanity check that the vectors carry meaning, not just shape."""
        builders.write_hwpx(shared_root / "server.hwpx",
                            text="서버 장애 발생 시 담당 엔지니어에게 즉시 통보한다.")
        builders.write_hwpx(shared_root / "travel.hwpx",
                            text="국내 출장 일비는 60,000원이다.")
        ingest()
        EmbeddingService(connection_factory, config).process_pending()

        from ingestion.embedding import LocalE5Model, QUERY_PREFIX, to_pgvector

        model = LocalE5Model(
            name=config.embedding_model, revision=config.embedding_model_revision,
            cache_dir=config.embedding_cache_dir, allow_download=False,
        )
        # Query prefix, as the search stage will use.
        raw = model._load().encode(
            [QUERY_PREFIX + "시스템이 다운되면 누구에게 연락하나요?"],
            normalize_embeddings=True, show_progress_bar=False,
        )[0].tolist()

        best = rows(conn, """
            SELECT d.source_path
            FROM chunks c
            JOIN document_revisions r ON r.id = c.document_revision_id
            JOIN documents d ON d.id = r.document_id
            ORDER BY c.embedding <=> %s::vector
            LIMIT 1
        """, (to_pgvector(raw),))
        assert best[0][0] == "server.hwpx"

    def test_real_model_matches_the_pinned_revision(self, config):
        from ingestion.embedding import LocalE5Model

        model = LocalE5Model(
            name=config.embedding_model, revision=config.embedding_model_revision,
            cache_dir=config.embedding_cache_dir, allow_download=False,
        )
        assert model.dimension == 384


class TestNoNetwork:
    def test_embedding_stage_opens_no_tcp_connection(
        self, ingest, connection_factory, config, shared_root, monkeypatch
    ):
        """The embedding stage must not reach an external service."""
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("embedding attempted a network connection")

        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        ingest()
        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        # PostgreSQL is reached over a unix socket; any TCP call would raise.
        result = embed(connection_factory, config)
        assert result.succeeded == 1

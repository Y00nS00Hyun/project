"""EMBED stage: chunk vectors, READY, and current-revision promotion.

Runs after parsing. Takes queued EMBED jobs, embeds every chunk of the
revision, and — only if all of them succeeded — records SUCCESS, which makes
the database's generated ``is_ready`` true and allows promotion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from .config import IngestionConfig
from .embedding import (
    EmbeddingModel,
    EmbeddingModelUnavailableError,
    LocalE5Model,
    VectorValidationError,
    to_pgvector,
    validate_vector,
)
from .repository import IngestionRepository

logger = logging.getLogger("ingestion.embed")

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

#: processing_jobs.result_code for this stage. PARSE reuses the parser's
#: taxonomy; EMBED needs its own small, closed set.
RESULT_EMBEDDED = "EMBEDDED"
RESULT_NO_CHUNKS = "NO_CHUNKS"
RESULT_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
RESULT_INVALID_VECTOR = "INVALID_VECTOR"
RESULT_EMBED_FAILED = "EMBED_FAILED"


@dataclass
class EmbeddingRunResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    vectors_written: int = 0
    promoted: int = 0
    by_result_code: dict[str, int] = field(default_factory=dict)

    def record(self, code: str) -> None:
        self.by_result_code[code] = self.by_result_code.get(code, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "vectors_written": self.vectors_written,
            "promoted": self.promoted,
            "by_result_code": dict(self.by_result_code),
        }


class EmbeddingService:
    """Embeds chunks and promotes revisions that become READY."""

    def __init__(
        self,
        connection_factory,
        config: IngestionConfig,
        model: EmbeddingModel | None = None,
    ):
        self.connection_factory = connection_factory
        self.config = config
        # One model per worker, loaded lazily on the first job. Loading per
        # chunk (or per job) would dominate the run time of a batch.
        self._model = model or LocalE5Model(
            name=config.embedding_model,
            revision=config.embedding_model_revision,
            device=config.embedding_device,
            cache_dir=config.embedding_cache_dir,
            allow_download=config.embedding_allow_download,
            batch_size=config.embedding_batch_size,
        )

    def process_pending(self, limit: int = 100) -> EmbeddingRunResult:
        """Claim and run queued EMBED jobs."""
        result = EmbeddingRunResult()

        with self.connection_factory() as conn:
            conn.autocommit = False
            try:
                jobs = IngestionRepository(conn).claim_embed_jobs(limit)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        for job in jobs:
            try:
                self._process_job(job, result)
            except psycopg.OperationalError:
                # Infrastructure failure: stop rather than mark every remaining
                # revision as broken.
                logger.exception("embed.database_unavailable")
                raise
            except Exception:  # noqa: BLE001 - one revision must not kill the worker
                result.failed += 1
                logger.exception("embed.job_error", extra={"job_id": str(job["id"])})

        logger.info("embed.finish", extra=result.as_dict())
        return result

    # -- internals ---------------------------------------------------------

    def _process_job(self, job: dict, result: EmbeddingRunResult) -> None:
        revision_id = str(job["document_revision_id"])

        with self.connection_factory() as conn:
            conn.autocommit = True
            repo = IngestionRepository(conn)
            state = repo.revision_processing_state(revision_id)
            chunks = repo.chunks_for_embedding(revision_id)

        if state is None:
            logger.warning("embed.revision_missing", extra={"job_id": str(job["id"])})
            return

        if state["embedding_status"] == STATUS_SUCCESS:
            # Already embedded; a duplicate job is a no-op rather than a
            # needless re-run of the model over the whole revision.
            self._finish_job_only(job, STATUS_SUCCESS, RESULT_EMBEDDED)
            result.processed += 1
            result.record(RESULT_EMBEDDED)
            return

        if not chunks:
            # TEXT_EXTRACTED with no chunks should not happen, but embedding
            # "nothing" must never be reported as success: is_ready would go
            # true for a revision with no searchable vectors.
            self._fail(job, revision_id, result, RESULT_NO_CHUNKS,
                       "revision has no chunks to embed")
            return

        # Inference happens outside any transaction: it is slow, and holding a
        # DB lock for its duration would block the rest of the pipeline.
        try:
            vectors = self._embed_all(chunks)
        except EmbeddingModelUnavailableError as exc:
            self._fail(job, revision_id, result, RESULT_MODEL_UNAVAILABLE, str(exc))
            return
        except VectorValidationError as exc:
            self._fail(job, revision_id, result, RESULT_INVALID_VECTOR, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            # Message only -- never chunk text, which may be internal content.
            self._fail(job, revision_id, result, RESULT_EMBED_FAILED,
                       f"{type(exc).__name__}: {exc}")
            return

        self._persist_success(job, revision_id, state["document_id"], vectors, result)

    def _embed_all(self, chunks: list[dict]) -> list[tuple[str, str]]:
        """Embed and validate every chunk before anything is written.

        All-or-nothing by construction: a failure anywhere raises before the
        write transaction opens, so a revision can never end up with vectors
        for some chunks and a SUCCESS status.
        """
        model = self._model
        dimension = self.config.embedding_dimension
        out: list[tuple[str, str]] = []

        batch_size = self.config.embedding_batch_size
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            produced = model.embed_passages([c["text"] for c in batch])
            if len(produced) != len(batch):
                raise VectorValidationError(
                    f"model returned {len(produced)} vectors for {len(batch)} chunks"
                )
            for chunk, vector in zip(batch, produced):
                validate_vector(vector, dimension)
                out.append((str(chunk["id"]), to_pgvector(vector)))
        return out

    def _persist_success(self, job, revision_id, document_id, vectors, result) -> None:
        """One transaction: vectors, provenance, status, job, promotion."""
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                # Clear first so a retry cannot leave stale vectors from an
                # earlier model or a previous partial attempt.
                repo.clear_chunk_embeddings(revision_id)
                written = repo.save_chunk_embeddings(revision_id, vectors)
                repo.save_embedding_result(
                    revision_id=revision_id,
                    status=STATUS_SUCCESS,
                    provider=self.config.embedding_provider,
                    model=self.config.embedding_model,
                    dimension=self.config.embedding_dimension,
                    version=self.config.embedding_model_revision,
                )
                repo.finish_job(
                    str(job["id"]), status=STATUS_SUCCESS, result_code=RESULT_EMBEDDED
                )
                # is_ready is now true for this revision, so promotion runs in
                # the same transaction and sees a consistent view.
                promoted = repo.promote_current_revision(str(document_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result.processed += 1
        result.succeeded += 1
        result.vectors_written += written
        result.record(RESULT_EMBEDDED)
        if promoted:
            result.promoted += 1
        logger.info(
            "embed.revision_done",
            extra={"chunks": written, "model": self.config.embedding_model,
                   "promoted": bool(promoted)},
        )

    def _fail(self, job, revision_id, result, result_code, message) -> None:
        """Mark the revision and job FAILED, leaving current_revision_id alone.

        A newer revision failing to embed must not remove the older, working
        revision from search.
        """
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                repo.clear_chunk_embeddings(revision_id)
                repo.save_embedding_result(revision_id=revision_id, status=STATUS_FAILED)
                repo.finish_job(
                    str(job["id"]), status=STATUS_FAILED,
                    result_code=result_code, error_message=message[:2000],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result.processed += 1
        result.failed += 1
        result.record(result_code)
        logger.warning("embed.revision_failed", extra={"result_code": result_code})

    def _finish_job_only(self, job, status, result_code) -> None:
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                repo.finish_job(str(job["id"]), status=status, result_code=result_code)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

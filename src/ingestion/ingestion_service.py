"""Parse + chunk stage.

Takes PARSE jobs the sync service queued, runs the existing HWP/HWPX parser,
persists the result, and chunks only when body text was actually obtained.

The parser itself is reused as-is from :mod:`document_processing` -- it is not
reimplemented or copied here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from document_processing.corpus import RESULT_TEXT_EXTRACTED, parse_status_for
from document_processing.normalize import build_normalized_text, canonicalize
from document_processing.parsers import parse_document
from document_processing.parsers.exceptions import DocumentParseError

from .chunker import chunk_document
from .config import IngestionConfig
from .document_year import extract_year
from .file_scanner import resolve_source_path
from .repository import IngestionRepository
from .tokenizers import HuggingFaceTokenizer, Tokenizer

logger = logging.getLogger("ingestion.parse")

#: Downstream stages are skipped when parsing produced no searchable body text.
DOWNSTREAM_SKIPPED = "SKIPPED"

#: Body text was obtained; embedding is simply not done yet. It must stay
#: PENDING -- forcing SUCCESS would make is_ready true and expose a revision
#: with no vectors to search.
DOWNSTREAM_PENDING = "PENDING"


@dataclass
class IngestionResult:
    processed: int = 0
    text_extracted: int = 0
    chunks_written: int = 0
    failed: int = 0
    by_result_code: dict[str, int] = field(default_factory=dict)

    def record(self, code: str) -> None:
        self.by_result_code[code] = self.by_result_code.get(code, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "text_extracted": self.text_extracted,
            "chunks_written": self.chunks_written,
            "failed": self.failed,
            "by_result_code": dict(self.by_result_code),
        }


class IngestionService:
    """Runs queued PARSE jobs through parser and chunker."""

    def __init__(
        self,
        connection_factory,
        config: IngestionConfig,
        tokenizer: Tokenizer | None = None,
    ):
        self.connection_factory = connection_factory
        self.config = config
        # Lazy by default: a scan that produces no chunkable document never
        # touches the tokenizer, and no model weights are loaded either way.
        self._tokenizer = tokenizer or HuggingFaceTokenizer(config.tokenizer_name)

    def process_pending(self, limit: int = 100) -> IngestionResult:
        """Claim and process pending PARSE jobs.

        Each job runs in its own transaction pair, so one unparseable document
        cannot abort the batch.
        """
        result = IngestionResult()

        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                # Reclaim anything a dead worker left in RUNNING before
                # claiming new work: otherwise those rows are invisible to
                # every query in the system and the document silently stops
                # being processed.
                recovered = repo.recover_stale_jobs(
                    self.config.job_stale_seconds, job_type="PARSE"
                )
                if recovered["requeued"] or recovered["failed"]:
                    logger.warning(
                        "parse.stale_jobs_recovered",
                        extra={
                            "requeued": len(recovered["requeued"]),
                            "failed": len(recovered["failed"]),
                        },
                    )
                jobs = repo.claim_parse_jobs(limit)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        for job in jobs:
            try:
                self._process_job(job, result)
            except psycopg.OperationalError:
                # Infrastructure failure: stop rather than mark every remaining
                # document as broken.
                logger.exception("parse.database_unavailable")
                raise
            except Exception:  # noqa: BLE001 - per-document isolation
                result.failed += 1
                logger.exception("parse.job_error")

        logger.info("parse.finish", extra=result.as_dict())
        return result

    # -- internals ---------------------------------------------------------

    def _process_job(self, job: dict, result: IngestionResult) -> None:
        revision_id = str(job["document_revision_id"])

        with self.connection_factory() as conn:
            conn.autocommit = True
            state = IngestionRepository(conn).revision_processing_state(revision_id)
        if state is None:
            logger.warning("parse.revision_missing")
            return

        # Re-validate the stored path against the shared root before opening it.
        # source_path is data; data can be wrong or tampered with.
        try:
            absolute = resolve_source_path(state["source_path"], self.config.resolved_root)
        except Exception:
            self._record_failure(
                job, revision_id, result,
                result_code="PARSE_FAILED",
                error_message="source path is not inside the configured shared root",
            )
            return

        # Parsing happens outside any transaction: it is slow, and a long DB
        # transaction around it would hold locks for the duration.
        parsed = None
        try:
            parsed = parse_document(absolute)
            result_code = RESULT_TEXT_EXTRACTED
            error_message = None
        except DocumentParseError as exc:
            # The parser classified the document; that is a determination, not
            # a crash. ENCRYPTED / OCR_REQUIRED / EMPTY_DOCUMENT are successful
            # determinations even though no text results.
            result_code = exc.error_code
            error_message = str(exc)
        except FileNotFoundError:
            result_code = "PARSE_FAILED"
            error_message = "file disappeared before parsing"
        except Exception as exc:  # noqa: BLE001
            # An unexpected parser failure must not take the worker down.
            result_code = "PARSE_FAILED"
            error_message = f"{type(exc).__name__}: {exc}"

        self._persist(job, revision_id, parsed, result_code, error_message, result,
                      title=state.get("title") or "")

    def _persist(self, job, revision_id, parsed, result_code, error_message, result,
                 *, title: str = "") -> None:
        """Transaction B: parse outcome, chunks and job status together."""
        parse_status = parse_status_for(result_code)
        extracted_text = None
        parsed_structure = None
        parser_name = parser_version = None
        chunks = []

        if parsed is not None and result_code == RESULT_TEXT_EXTRACTED:
            extracted_text = build_normalized_text(parsed)
            # canonicalize() keeps the table row/column/cell/span structure the
            # parser recovered. Independent table chunking is OFF, but the
            # structure itself is never discarded.
            parsed_structure = canonicalize(parsed)
            parser_name = parsed.parser_name
            parser_version = parsed.parser_version
            chunks = chunk_document(parsed, self._tokenizer, self.config)

        downstream = (
            DOWNSTREAM_PENDING if result_code == RESULT_TEXT_EXTRACTED else DOWNSTREAM_SKIPPED
        )

        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                repo.save_parse_result(
                    revision_id=revision_id,
                    parse_status=parse_status,
                    parse_result_code=result_code,
                    extracted_text=extracted_text,
                    parsed_structure=parsed_structure,
                    parser_name=parser_name,
                    parser_version=parser_version,
                    chunking_version=(
                        self.config.chunking_version
                        if result_code == RESULT_TEXT_EXTRACTED else None
                    ),
                    downstream_status=downstream,
                )
                if extracted_text:
                    # The parsed front matter is now readable and outranks the
                    # file name, which is all the discovery stage had. A report
                    # named "완료보고서_d251126" whose cover reads "2025. 11. 26."
                    # is a 2025 document; nothing is inferred from "d251126".
                    extraction = extract_year(title, extracted_text)
                    repo.set_document_year(revision_id, extraction.year)
                    logger.info(
                        "parse.document_year",
                        extra={"reason": extraction.reason, "year": extraction.year},
                    )
                # Only TEXT_EXTRACTED is chunked. Everything else has no body
                # text to chunk, and writing empty chunks would pollute search.
                written = repo.replace_chunks(revision_id, chunks)
                if result_code == RESULT_TEXT_EXTRACTED:
                    # Hand off to the embedding stage in the same transaction,
                    # so a crash between "chunks written" and "job queued"
                    # cannot strand a revision with nothing scheduled to embed
                    # it. The model is NOT invoked here.
                    repo.enqueue_embed_job(revision_id)
                repo.finish_job(
                    str(job["id"]),
                    status="SUCCESS" if parse_status == "SUCCESS" else "FAILED",
                    result_code=result_code,
                    error_message=error_message,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result.processed += 1
        result.record(result_code)
        if result_code == RESULT_TEXT_EXTRACTED:
            result.text_extracted += 1
            result.chunks_written += written
        logger.info(
            "parse.document_done",
            extra={"result_code": result_code, "chunks": written if chunks else 0},
        )

    def _record_failure(self, job, revision_id, result, *, result_code, error_message) -> None:
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                repo.save_parse_result(
                    revision_id=revision_id,
                    parse_status=parse_status_for(result_code),
                    parse_result_code=result_code,
                    extracted_text=None,
                    parsed_structure=None,
                    parser_name=None,
                    parser_version=None,
                    chunking_version=None,
                    downstream_status=DOWNSTREAM_SKIPPED,
                )
                repo.finish_job(
                    str(job["id"]), status="FAILED",
                    result_code=result_code, error_message=error_message,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result.processed += 1
        result.failed += 1
        result.record(result_code)


def default_connection_factory(dsn: str):
    """Return a callable producing new psycopg connections."""

    def factory() -> psycopg.Connection:
        return psycopg.connect(dsn)

    return factory


def resolve_shared_path(relative_path: str, root: Path) -> Path:
    """Public helper: validate a stored source_path against the shared root."""
    return resolve_source_path(relative_path, root)

"""File Sync: reconcile the shared folder with `documents` / `document_revisions`.

The shared folder is the source of truth. This service only reads it. It never
modifies, moves, renames or deletes a file there.

One `scan_once()` call is a complete, deterministic reconciliation: it is safe
to run repeatedly, and running it on an unchanged folder changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .config import IngestionConfig
from .document_type import classify_filename
from .document_year import year_for_file
from .exceptions import FileUnstableError, FileVanishedError, PathOutsideRootError
from .file_identity import fingerprint
from .file_scanner import DiscoveredFile, scan_files
from .repository import IngestionRepository

logger = logging.getLogger("ingestion.sync")


@dataclass
class ScanResult:
    """Counts for one scan. Deliberately free of paths and document content."""

    discovered: int = 0
    new_documents: int = 0
    new_revisions: int = 0
    unchanged: int = 0
    missing: int = 0
    soft_deleted: int = 0
    restored: int = 0
    unstable: int = 0
    errors: int = 0
    jobs_created: int = 0
    error_reasons: dict[str, int] = field(default_factory=dict)

    def record_error(self, reason: str) -> None:
        self.errors += 1
        self.error_reasons[reason] = self.error_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, int | dict[str, int]]:
        return {
            "discovered": self.discovered,
            "new_documents": self.new_documents,
            "new_revisions": self.new_revisions,
            "unchanged": self.unchanged,
            "missing": self.missing,
            "soft_deleted": self.soft_deleted,
            "restored": self.restored,
            "unstable": self.unstable,
            "errors": self.errors,
            "jobs_created": self.jobs_created,
            "error_reasons": dict(self.error_reasons),
        }


class SyncService:
    """Discovers files and registers documents, revisions and PARSE jobs."""

    def __init__(self, connection_factory, config: IngestionConfig):
        """``connection_factory`` returns a fresh psycopg connection.

        The service opens a short transaction per file rather than one long one
        for the whole scan, so a failure on file 900 cannot roll back the first
        899 and a large scan does not hold locks for its full duration.
        """
        self.connection_factory = connection_factory
        self.config = config

    # -- public API --------------------------------------------------------

    def scan_once(self, root: Path | None = None) -> ScanResult:
        """Reconcile the shared folder once.

        Steps: register or update every discovered file, then mark whatever was
        previously known but is no longer present.
        """
        config = self.config
        if root is not None:
            config = IngestionConfig(
                shared_root=Path(root),
                chunk_target_tokens=config.chunk_target_tokens,
                chunk_max_tokens=config.chunk_max_tokens,
                chunk_overlap=config.chunk_overlap,
                chunking_version=config.chunking_version,
                tokenizer_name=config.tokenizer_name,
                follow_symlinks=config.follow_symlinks,
                missing_grace_seconds=config.missing_grace_seconds,
                discoverable_extensions=config.discoverable_extensions,
            )

        result = ScanResult()
        logger.info("scan.start", extra={"root_configured": True})

        seen_paths: set[str] = set()
        for discovered in scan_files(config):
            result.discovered += 1
            seen_paths.add(discovered.relative_path)
            try:
                self._register_file(discovered, result)
            except (FileVanishedError, FileUnstableError) as exc:
                # Live folder: the file was being written or removed. Leave it
                # for the next run rather than storing a partial revision.
                result.unstable += 1
                logger.warning("scan.file_unstable", extra={"reason": type(exc).__name__})
            except PathOutsideRootError:
                result.record_error("PATH_OUTSIDE_ROOT")
                logger.error("scan.path_outside_root")
            except psycopg.Error as exc:
                # One document's failure must not abort the scan.
                result.record_error(type(exc).__name__)
                logger.exception("scan.database_error", extra={"reason": type(exc).__name__})
            except Exception as exc:  # noqa: BLE001 - per-file isolation
                result.record_error(type(exc).__name__)
                logger.exception("scan.file_error", extra={"reason": type(exc).__name__})

        self._reconcile_missing(seen_paths, result)

        logger.info("scan.finish", extra=result.as_dict())
        return result

    # -- internals ---------------------------------------------------------

    def _register_file(self, discovered: DiscoveredFile, result: ScanResult) -> None:
        """Register one file. Hashing happens outside the DB transaction."""
        # Reading a large file can take a while; do it before opening a
        # transaction so the database is not held open during I/O.
        finger = fingerprint(discovered.absolute_path)

        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                existing = repo.find_document_by_source_path(discovered.relative_path)

                if existing is None:
                    self._create_new_document(repo, discovered, finger, result)
                else:
                    self._update_existing_document(repo, existing, discovered, finger, result)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _apply_document_type(self, repo, document_id: str, discovered) -> None:
        """Store the document's kind, derived from its file name.

        Deterministic rules only -- no LLM, no body text. Runs inside the
        caller's transaction so the kind and the document are committed
        together.
        """
        # Classified on the readable name: the rules match Korean keywords, and
        # a canonical name full of %XX escapes would match none of them.
        classification = classify_filename(discovered.display_filename)
        changed = repo.set_document_type(document_id, classification.tag_name)
        if changed:
            # The file name is not logged: it can itself be sensitive.
            logger.info(
                "scan.document_type_set",
                extra={"code": classification.code, "reason": classification.reason},
            )

    def _create_new_document(self, repo, discovered, finger, result: ScanResult) -> None:
        """Transaction A: document + revision + latest pointer + PARSE job.

        All four in one transaction, so a crash can never leave a revision with
        no job to process it, or a document with no revision.
        """
        document_id = repo.create_document(
            title=discovered.title,
            # Canonical, like source_path: original_filename is a text column
            # and a raw filesystem string may not be valid UTF-8.
            original_filename=discovered.canonical_filename,
            source_path=discovered.relative_path,
            file_type=discovered.extension,
        )
        revision_id = repo.create_revision(
            document_id=document_id,
            content_hash=finger.content_hash,
            file_size=finger.size,
            source_mtime=discovered.mtime,
            source_path_at_ingest=discovered.relative_path,
            document_year=year_for_file(discovered.title),
        )
        repo.set_latest_revision(document_id, revision_id)
        if repo.enqueue_parse_job(revision_id):
            result.jobs_created += 1

        self._apply_document_type(repo, document_id, discovered)

        result.new_documents += 1
        result.new_revisions += 1
        logger.info("scan.document_new", extra={"file_type": discovered.extension})

    def _update_existing_document(self, repo, existing, discovered, finger, result) -> None:
        latest = repo.latest_revision(existing.id)

        if existing.is_deleted or existing.missing_since is not None:
            result.restored += 1
            logger.info("scan.document_restored")
        repo.touch_document(existing.id)

        # Re-applied on every scan, not only at creation.
        #
        # A rename is a new source_path, and source_path is the document's
        # identity, so a renamed file arrives as a *new* document and is
        # classified there. Re-applying here costs one indexed read when the
        # kind is already correct, and it is what makes the corpus converge
        # after the rules change -- otherwise old documents would keep a kind
        # the current rules would no longer assign.
        self._apply_document_type(repo, existing.id, discovered)

        if latest is not None and latest.content_hash == finger.content_hash:
            # Same bytes. An mtime-only change is not a content change and must
            # not create a revision -- that would re-parse and re-embed the
            # whole corpus after a metadata-touching backup restore.
            result.unchanged += 1
            return

        revision_id = repo.create_revision(
            document_id=existing.id,
            content_hash=finger.content_hash,
            file_size=finger.size,
            source_mtime=discovered.mtime,
            source_path_at_ingest=discovered.relative_path,
            document_year=year_for_file(discovered.title),
        )
        # latest moves; current stays where it is until a revision becomes
        # READY. latest != current is the normal state while processing.
        repo.set_latest_revision(existing.id, revision_id)
        if repo.enqueue_parse_job(revision_id):
            result.jobs_created += 1

        result.new_revisions += 1
        logger.info("scan.document_modified", extra={"file_type": discovered.extension})

    def _reconcile_missing(self, seen_paths: set[str], result: ScanResult) -> None:
        """Mark documents whose file was not found, and expire the grace period.

        Never a hard delete. Revisions, chunks and the provenance of past
        answers survive a file disappearing from the shared folder.
        """
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                for path, document_id in repo.active_document_paths().items():
                    if path not in seen_paths:
                        repo.mark_missing(document_id)
                        result.missing += 1
                deleted = repo.soft_delete_expired_missing(self.config.missing_grace_seconds)
                result.soft_deleted = len(deleted)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if result.soft_deleted:
            logger.info("scan.soft_deleted", extra={"count": result.soft_deleted})

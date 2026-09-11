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
from .file_identity import FileFingerprint, fingerprint
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
    renamed: int = 0
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
            "renamed": self.renamed,
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
        # Files at a path no document holds. Held back rather than registered
        # on sight: a renamed file looks exactly like a new one until the rest
        # of the scan has been read, and creating the document first would mean
        # merging two rows afterwards.
        unplaced: list[tuple[DiscoveredFile, FileFingerprint]] = []

        for discovered in scan_files(config):
            result.discovered += 1
            seen_paths.add(discovered.relative_path)
            try:
                pending = self._register_file(discovered, result)
                if pending is not None:
                    unplaced.append(pending)
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

        self._reconcile(seen_paths, unplaced, result)

        logger.info("scan.finish", extra=result.as_dict())
        return result

    # -- internals ---------------------------------------------------------

    def _register_file(
        self, discovered: DiscoveredFile, result: ScanResult
    ) -> tuple[DiscoveredFile, FileFingerprint] | None:
        """Register one file, or hand it back for the reconciliation stage.

        A file whose path some document already holds is settled here and now:
        same bytes means nothing to do, different bytes means a new revision.
        Neither can be a rename, because the path did not change.

        A file at an unknown path cannot be settled yet. It is either genuinely
        new or the far end of a rename, and telling those apart needs to know
        which paths went missing -- which is only known once the whole folder
        has been read. It is returned instead, and the caller decides.
        """
        # Reading a large file can take a while; do it before opening a
        # transaction so the database is not held open during I/O.
        finger = fingerprint(discovered.absolute_path)

        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                existing = repo.find_document_by_source_path(discovered.relative_path)
                if existing is None:
                    conn.rollback()
                    return (discovered, finger)
                self._update_existing_document(repo, existing, discovered, finger, result)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return None

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
            # Part of the same transaction as the document row, so a document
            # is never momentarily in a state the deployment's policy did not
            # ask for. Defaults to false; see IngestionConfig.document_access.
            grant_public_read=self.config.document_access == "all_active_users",
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

    def _reconcile(
        self,
        seen_paths: set[str],
        unplaced: list[tuple[DiscoveredFile, FileFingerprint]],
        result: ScanResult,
    ) -> None:
        """Match vanished paths against new ones, then settle what is left.

        Three stages, in this order and for a reason:

          1. pair renames -- a document whose path vanished and a file at a new
             path with identical bytes are the same document, moved
          2. create whatever is still unaccounted for
          3. mark whatever is still missing

        Pairing first is what keeps a rename from becoming two rows. Creating
        first and merging afterwards would mean a window in which the same
        document exists twice, and a merge that has to move citations,
        favourites and permissions between them.

        Stage 3 last is load-bearing too, not just tidy: pairing reads
        `missing_since` to tell a path that vanished *this* scan from one that
        was already gone, and stage 3 is what writes that column. Marking
        missing first would stamp this scan's disappearances and erase the
        distinction.
        """
        paired_documents, placed_paths = self._pair_renames(seen_paths, unplaced, result)

        # Each in its own transaction, as before: one bad document must not
        # roll back the rest of the scan.
        for discovered, finger in unplaced:
            if discovered.relative_path in placed_paths:
                continue
            try:
                self._create_document_transaction(discovered, finger, result)
            except psycopg.Error as exc:
                result.record_error(type(exc).__name__)
                logger.exception("scan.database_error", extra={"reason": type(exc).__name__})
            except Exception as exc:  # noqa: BLE001 - per-file isolation
                result.record_error(type(exc).__name__)
                logger.exception("scan.file_error", extra={"reason": type(exc).__name__})

        self._reconcile_missing(seen_paths, paired_documents, result)

    def _pair_renames(
        self,
        seen_paths: set[str],
        unplaced: list[tuple[DiscoveredFile, FileFingerprint]],
        result: ScanResult,
    ) -> tuple[set[str], set[str]]:
        """Move documents whose file was renamed. Returns (documents, paths) handled.

        A pair is only accepted when it is unambiguous: one path gone, one path
        appeared, identical content hash, and exactly one candidate on each
        side for that hash.

        "Gone" means gone *during this scan*. A document already missing when
        this scan started is not a rename candidate however well its hash
        matches: the two events are unrelated, separated by however many scans
        happened in between, and the same bytes turning up later is far more
        likely to be someone restoring a copy from a backup or a colleague's
        folder than the original file finally arriving somewhere else. Pairing
        those would silently graft an old document's history -- its citations,
        its revisions -- onto an unrelated file.

        Everything else is left alone on purpose. Two copies of the same file
        appearing while one vanishes is not a rename anyone can identify -- and
        getting it wrong attaches a document's history to the wrong file, which
        nobody would notice and no scan would correct. A spurious new document
        is visible and harmless by comparison.

        Nothing here guesses from names. A rename plus an edit produces a
        different hash and is treated as a new document, which is the stated
        policy rather than a limitation to work around.
        """
        if not unplaced:
            return set(), set()

        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                gone: dict[str, list[str]] = {}
                # Documents present as of the previous scan, so a path absent
                # from `seen_paths` here is one that vanished during this scan.
                # This has to run before `_reconcile_missing` stamps them --
                # which is the order `_reconcile` already establishes.
                for path, document_id in repo.present_document_paths().items():
                    if path in seen_paths:
                        continue
                    latest = repo.latest_revision(document_id)
                    if latest is None:
                        # No revision, so no bytes to compare. Falls through to
                        # the missing policy.
                        continue
                    gone.setdefault(latest.content_hash, []).append(document_id)

                arrived: dict[str, list[tuple[DiscoveredFile, FileFingerprint]]] = {}
                for candidate in unplaced:
                    arrived.setdefault(candidate[1].content_hash, []).append(candidate)

                paired_documents: set[str] = set()
                placed_paths: set[str] = set()
                for content_hash, documents in gone.items():
                    candidates = arrived.get(content_hash, ())
                    if len(documents) != 1 or len(candidates) != 1:
                        # Ambiguous on one side or the other. Leaving both
                        # alone is the recoverable mistake.
                        continue
                    document_id = documents[0]
                    discovered, _ = candidates[0]
                    repo.relocate_document(
                        document_id,
                        source_path=discovered.relative_path,
                        title=discovered.title,
                        # Canonical, like source_path: a raw filesystem name
                        # may not be valid UTF-8.
                        original_filename=discovered.canonical_filename,
                    )
                    # The name decides the kind, and the name just changed.
                    self._apply_document_type(repo, document_id, discovered)
                    paired_documents.add(document_id)
                    placed_paths.add(discovered.relative_path)
                    result.renamed += 1
                    # No path in the log line: a file name can itself be
                    # sensitive.
                    logger.info("scan.document_moved",
                                extra={"file_type": discovered.extension})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return paired_documents, placed_paths

    def _create_document_transaction(self, discovered, finger, result: ScanResult) -> None:
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                self._create_new_document(repo, discovered, finger, result)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _reconcile_missing(
        self, seen_paths: set[str], paired_documents: set[str], result: ScanResult
    ) -> None:
        """Mark documents whose file was not found, and expire the grace period.

        Never a hard delete. Revisions, chunks and the provenance of past
        answers survive a file disappearing from the shared folder.

        A document that was paired with a rename is not missing -- its file is
        right there under a different name, and its path has already been
        updated.
        """
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                for path, document_id in repo.active_document_paths().items():
                    if path not in seen_paths and document_id not in paired_documents:
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

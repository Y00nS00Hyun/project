"""Development CLI for the ingestion foundation.

    python -m ingestion sync    --root /tmp/shared-test
    python -m ingestion parse   --root /tmp/shared-test
    python -m ingestion embed   --root /tmp/shared-test
    python -m ingestion summarize
    python -m ingestion resume-summaries
    python -m ingestion run     --root /tmp/shared-test

This exists so the pipeline can be driven by hand and by integration tests. It
is not a production scheduler: there is no daemon, no filesystem watcher and no
retry backoff loop here.

Output is counts only. Document paths and body text are never printed, because
a terminal transcript is one of the easiest ways for internal content to leave
the network.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import IngestionConfig, config_from_env, database_url
from .exceptions import ConfigurationError
from .embedding_service import EmbeddingService
from .ingestion_service import IngestionService, default_connection_factory
from .sync_service import SyncService
from .tokenizers import HuggingFaceTokenizer, SimpleTokenizer


def build_config(args) -> IngestionConfig:
    config = config_from_env(args.root)
    if args.missing_grace_seconds is not None:
        config = IngestionConfig(
            shared_root=config.shared_root,
            chunk_target_tokens=config.chunk_target_tokens,
            chunk_max_tokens=config.chunk_max_tokens,
            chunk_overlap=config.chunk_overlap,
            chunking_version=config.chunking_version,
            tokenizer_name=config.tokenizer_name,
            follow_symlinks=config.follow_symlinks,
            missing_grace_seconds=args.missing_grace_seconds,
            discoverable_extensions=config.discoverable_extensions,
        )
    return config


def make_tokenizer(args, config: IngestionConfig):
    if args.simple_tokenizer:
        # Offline/dev only: chunk sizes will not match the embedding model.
        return SimpleTokenizer()
    return HuggingFaceTokenizer(config.tokenizer_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingestion", description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["sync", "parse", "embed", "summarize", "resume-summaries", "run"],
        help="sync = discover files; parse = process PARSE jobs; "
             "embed = process EMBED jobs; summarize = process SUMMARIZE jobs; "
             "resume-summaries = re-open summaries skipped while generation was "
             "off; run = sync, parse and embed",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="shared folder root (defaults to $SHARED_ROOT)")
    parser.add_argument("--limit", type=int, default=100, help="max jobs per stage")
    parser.add_argument("--missing-grace-seconds", type=int, default=None,
                        help="override the grace period before a missing file is soft-deleted")
    parser.add_argument("--simple-tokenizer", action="store_true",
                        help="use the offline word-like tokenizer instead of the model tokenizer")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = build_config(args)
        dsn = database_url()
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    factory = default_connection_factory(dsn)
    output: dict[str, object] = {}

    if args.command in {"sync", "run"}:
        output["sync"] = SyncService(factory, config).scan_once().as_dict()

    if args.command in {"parse", "run"}:
        service = IngestionService(factory, config, tokenizer=make_tokenizer(args, config))
        output["parse"] = service.process_pending(args.limit).as_dict()

    if args.command in {"embed", "run"}:
        # Loads the local model once for the whole batch.
        output["embed"] = EmbeddingService(factory, config).process_pending(args.limit).as_dict()

    # Deliberately not part of "run". Summarizing is the one stage that leaves
    # the network, so it is never something a general "process everything"
    # command starts on its own.
    if args.command == "summarize":
        from api.dependencies import get_llm_provider
        from rag.summary_service import SummaryService

        summary_service = SummaryService(factory, config, get_llm_provider())
        # Reconcile first: revisions that became READY before this stage
        # existed have no job, so claiming alone would leave them waiting.
        reconciled = summary_service.reconcile()
        output["summarize"] = {
            **summary_service.process_pending(args.limit).as_dict(),
            "reconciled": reconciled,
        }

    if args.command == "resume-summaries":
        # The backfill path for enabling generation after documents have
        # already been ingested: revisions skipped while there was no provider
        # go back to PENDING and get a job each. Idempotent -- a revision that
        # already has a summary is not touched.
        from .repository import IngestionRepository

        with factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                revisions = repo.resume_skipped_summaries(args.limit)
                queued = sum(
                    1 for revision_id in revisions
                    if repo.enqueue_summarize_job(revision_id) is not None
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        output["resume_summaries"] = {"reopened": len(revisions), "queued": queued}

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

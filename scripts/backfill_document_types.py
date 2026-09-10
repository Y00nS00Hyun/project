#!/usr/bin/env python3
"""Assign a document kind to documents that predate the classifier.

    python scripts/backfill_document_types.py --dry-run   # show, change nothing
    python scripts/backfill_document_types.py

Reads DATABASE_URL from the environment. Deterministic rules over the file name
only -- no LLM, no body text, no network. Safe to re-run: a document whose kind
is already correct is left untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg  # noqa: E402

from ingestion.document_type import all_tag_names, classify_filename  # noqa: E402
from ingestion.repository import IngestionRepository  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    counts: Counter[str] = Counter()
    changed = 0

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        repo = IngestionRepository(conn)
        if not args.dry_run:
            # The five kinds are a fixed vocabulary, so seed all of them rather
            # than letting the filter's option list depend on what happens to
            # have been ingested. A dropdown that gains and loses options as
            # documents arrive is harder to trust than one where a kind simply
            # returns nothing.
            for name in all_tag_names():
                repo.ensure_tag(name)

        with conn.cursor() as cur:
            # original_filename, not source_path: the kind comes from the file
            # name, and a folder name in the path must not leak into it.
            cur.execute(
                "SELECT id, original_filename FROM documents ORDER BY original_filename"
            )
            rows = cur.fetchall()

        for document_id, filename in rows:
            classification = classify_filename(filename or "")
            counts[classification.label] += 1
            if args.dry_run:
                current = repo.document_type_tag(str(document_id))
                mark = " (변경)" if current != classification.tag_name else ""
                print(f"  {classification.label:12} {filename}{mark}")
                continue
            if repo.set_document_type(str(document_id), classification.tag_name):
                changed += 1
                print(f"  {classification.label:12} {filename}")

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    print()
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:12} {n}건")
    print(f"\n  총 {sum(counts.values())}건" + ("" if args.dry_run else f", {changed}건 변경"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

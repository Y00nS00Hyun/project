"""Normalized (searchable) representation and deterministic hashing.

Two separate concerns live here, both operating on an already-parsed
:class:`~document_processing.models.ParsedDocument`:

1. ``build_normalized_text`` - a flat, chunker-friendly rendering that keeps
   body/table interleaving order and keeps table cells addressable.
2. ``canonicalize`` / ``content_hash`` - a stable serialisation used to prove
   that re-parsing the same bytes yields the same result.

The structured document is never mutated by either.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from .models import ParsedDocument, ParsedTable

#: Bumped whenever the normalized-text rendering changes.  A change here
#: invalidates downstream chunks, so it is versioned separately from the
#: parsers.
NORMALIZER_VERSION = "1.0.0"

PARAGRAPH_MARKER = "[문단]"
TABLE_MARKER = "[표]"
CELL_SEPARATOR = " | "


def normalize_text(text: str) -> str:
    """Normalise a single text run for search.

    Deliberately conservative -- it must not damage Korean text, numbers or
    punctuation:

    * NFC composition, so decomposed Hangul jamo compare equal to composed
      syllables (a real risk with text that has passed through macOS).
    * Non-breaking / fixed-width spaces folded to a plain space.
    * Whitespace runs collapsed; leading and trailing whitespace stripped.

    Case is *not* folded and no characters are dropped.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    # Space-like characters HWP uses that would otherwise survive as
    # non-breaking and break naive tokenisers.
    for ch in (" ", " ", " ", "　", "﻿"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


def _render_table(table: ParsedTable) -> list[str]:
    lines = [TABLE_MARKER]
    for row in table.rows:
        cells = [normalize_text(c) for c in row]
        lines.append(CELL_SEPARATOR.join(cells))
    return lines


def build_normalized_text(doc: ParsedDocument) -> str:
    """Render the document as flat searchable text.

    Tables are emitted at the position of the paragraph they are anchored to,
    so the reading order of the original document is preserved.  Tables whose
    anchor is unknown are appended at the end rather than silently dropped.
    """
    tables_by_anchor: dict[int, list[ParsedTable]] = {}
    unanchored: list[ParsedTable] = []
    for table in doc.tables:
        if table.paragraph_index is None:
            unanchored.append(table)
        else:
            tables_by_anchor.setdefault(table.paragraph_index, []).append(table)

    lines: list[str] = []
    for para in doc.paragraphs:
        text = normalize_text(para.text)
        if text:
            lines.append(PARAGRAPH_MARKER)
            lines.append(text)
        for table in tables_by_anchor.get(para.index, ()):
            lines.extend(_render_table(table))

    # Anchored to a paragraph index that does not exist (should not happen, but
    # never lose content silently).
    known = {p.index for p in doc.paragraphs}
    for index in sorted(tables_by_anchor):
        if index not in known:
            for table in tables_by_anchor[index]:
                lines.extend(_render_table(table))
    for table in unanchored:
        lines.extend(_render_table(table))

    return "\n".join(lines)


def canonicalize(doc: ParsedDocument) -> dict[str, Any]:
    """Produce the canonical, hashable form of a parse result.

    Excluded on purpose:

    * ``file_path`` - the same bytes must hash identically wherever they live.
    * anything time- or machine-dependent (durations, absolute paths, PIDs).

    ``metadata`` is included but filtered to primitive, document-derived values
    so that an incidental non-deterministic entry cannot silently change the
    hash.
    """
    return {
        "schema": 1,
        "file_type": doc.file_type,
        "parser_name": doc.parser_name,
        "parser_version": doc.parser_version,
        "normalizer_version": NORMALIZER_VERSION,
        "page_count": doc.page_count,
        "paragraphs": [
            {
                "index": p.index,
                "text": p.text,
                "page_number": p.page_number,
                "section_title": p.section_title,
                "paragraph_type": p.paragraph_type,
                "section_index": p.section_index,
                "style_name": p.style_name,
            }
            for p in doc.paragraphs
        ],
        "tables": [
            {
                "index": t.index,
                "rows": t.rows,
                "cells": [
                    {
                        "row": c.row,
                        "column": c.column,
                        "text": c.text,
                        "row_span": c.row_span,
                        "column_span": c.column_span,
                    }
                    for c in t.cells
                ],
                "page_number": t.page_number,
                "paragraph_index": t.paragraph_index,
                "section_index": t.section_index,
                "declared_row_count": t.declared_row_count,
                "declared_column_count": t.declared_column_count,
                "has_merged_cells": t.has_merged_cells,
            }
            for t in doc.tables
        ],
        "metadata": {
            k: v
            for k, v in sorted(doc.metadata.items())
            if isinstance(v, (str, int, float, bool, type(None)))
        },
        # Sorted: warning discovery order is an implementation detail.
        "warnings": sorted(doc.warnings),
        "normalized_text": build_normalized_text(doc),
    }


def canonical_json(doc: ParsedDocument) -> str:
    return json.dumps(
        canonicalize(doc),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(doc: ParsedDocument) -> str:
    """SHA-256 over the canonical JSON of the parse result."""
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def normalized_text_hash(doc: ParsedDocument) -> str:
    return hashlib.sha256(build_normalized_text(doc).encode("utf-8")).hexdigest()

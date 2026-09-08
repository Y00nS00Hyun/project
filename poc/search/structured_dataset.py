"""Loss-auditable layout adapter for the UNMODIFIED search fixtures.

The source has sentences, not parser anchors or native tables. Sentence
boundaries are explicitly synthetic paragraphs. Three existing monetary lists
are rendered as tables using a reviewed sidecar, shared by ALL chunkers.
No documents, query labels, amounts, or substantive facts are added.
"""
from __future__ import annotations

import json
from pathlib import Path

from chunkers import Block
from dataset import Document

LAYOUT = Path(__file__).parent / "fixtures" / "chunking_layout.json"


def structure_document(document: Document, layouts: dict) -> list[Block]:
    text = document.text
    replacements = []
    for table in layouts.get(document.document_id, []):
        source = table["source_text"]
        if text.count(source) != 1:
            raise ValueError(f"{document.document_id}: table source does not match uniquely")
        replacements.append((text.index(source), source, table))
    replacements.sort()
    pieces: list[tuple[str, dict | None]] = []
    cursor = 0
    for start, source, table in replacements:
        if start < cursor:
            raise ValueError("overlapping table sources")
        pieces.append((text[cursor:start], None))
        pieces.append((source, table))
        cursor = start + len(source)
    pieces.append((text[cursor:], None))
    blocks = [Block(0, f"{document.title} ({document.department}, {document.year})")]
    for prose, table in pieces:
        if table:
            header = " | ".join(table["header"])
            rows = tuple(" | ".join(row) for row in table["rows"])
            blocks.append(Block(len(blocks), "\n".join((header, *rows)), "TABLE", header, rows))
        else:
            for paragraph in prose.replace("다. ", "다.\n").split("\n"):
                if paragraph.strip():
                    blocks.append(Block(len(blocks), paragraph.strip()))
    return blocks


def load_layout(path: Path = LAYOUT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["tables"]

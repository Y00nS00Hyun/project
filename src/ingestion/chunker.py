"""Paragraph-aware chunking of a parsed document.

Design Freeze v1 implementation default:

    strategy   paragraph-aware
    target     64 tokens
    hard max   64 tokens
    overlap    0
    table-aware (independent TABLE chunks)  OFF

64 tokens is provisional -- it comes from a short synthetic corpus, not from
internal long-form documents. It is configuration, not a constant.

"table-aware OFF" means tables are not forced into their own chunk type with
repeated headers. It does **not** mean table content is dropped: a table is
rendered as a text block in reading order so its cells remain searchable, and
the full row/column/cell/span structure is preserved separately in
``document_revisions.parsed_structure``.
"""

from __future__ import annotations

from dataclasses import dataclass

from document_processing.models import ParsedDocument, ParsedTable
from document_processing.normalize import CELL_SEPARATOR, normalize_text

from .config import IngestionConfig
from .tokenizers import Tokenizer


@dataclass(frozen=True)
class Block:
    """A unit of text with the paragraph anchor it came from.

    ``paragraph_index`` is the parser's own paragraph ordinal -- never a
    sentence split or any other reconstruction. Citations depend on it.
    """

    paragraph_index: int
    text: str
    is_table: bool = False
    section_title: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class Chunk:
    """One chunk, ready to be stored in ``chunks``."""

    chunk_index: int
    text: str
    token_count: int
    paragraph_start: int
    paragraph_end: int
    section_title: str | None = None
    page_number: int | None = None


def render_table(table: ParsedTable) -> str:
    """Flatten a table to text, preserving row and column order."""
    return "\n".join(
        CELL_SEPARATOR.join(normalize_text(cell) for cell in row) for row in table.rows
    )


def build_blocks(document: ParsedDocument) -> list[Block]:
    """Turn a parsed document into ordered blocks.

    Tables are emitted immediately after the paragraph they are anchored to, so
    reading order survives. A table anchored to no paragraph is appended at the
    end rather than dropped.
    """
    tables_by_anchor: dict[int, list[ParsedTable]] = {}
    unanchored: list[ParsedTable] = []
    for table in document.tables:
        if table.paragraph_index is None:
            unanchored.append(table)
        else:
            tables_by_anchor.setdefault(table.paragraph_index, []).append(table)

    blocks: list[Block] = []
    known_indices = set()
    for paragraph in document.paragraphs:
        known_indices.add(paragraph.index)
        text = normalize_text(paragraph.text)
        if text:
            blocks.append(
                Block(
                    paragraph_index=paragraph.index,
                    text=text,
                    section_title=paragraph.section_title,
                    page_number=paragraph.page_number,
                )
            )
        for table in tables_by_anchor.get(paragraph.index, ()):
            rendered = render_table(table)
            if rendered.strip():
                blocks.append(
                    Block(
                        paragraph_index=paragraph.index,
                        text=rendered,
                        is_table=True,
                        section_title=paragraph.section_title,
                        page_number=table.page_number,
                    )
                )

    # Tables anchored to a paragraph index that does not exist, plus unanchored
    # ones. Losing their content silently would be worse than an imprecise anchor.
    trailing = [t for index, group in sorted(tables_by_anchor.items())
                if index not in known_indices for t in group] + unanchored
    last_index = document.paragraphs[-1].index if document.paragraphs else 0
    for table in trailing:
        rendered = render_table(table)
        if rendered.strip():
            anchor = table.paragraph_index if table.paragraph_index is not None else last_index
            blocks.append(Block(paragraph_index=anchor, text=rendered, is_table=True))

    return blocks


def token_spans(tokenizer: Tokenizer, text: str, limit: int) -> list[tuple[int, int]]:
    """Split ``text`` into consecutive character spans of at most ``limit`` tokens.

    No text is lost and no span splits a Unicode character.

    The re-check loop matters: a substring can retokenize differently from the
    same range inside the full string (a word boundary changes what the
    tokenizer merges), so a span cut at the limit-th offset can come back over
    budget. Shrinking until it actually measures within budget is what keeps
    the hard maximum a real guarantee instead of an estimate.
    """
    if limit < 1:
        raise ValueError("token limit must be >= 1")

    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        rest = text[start:]
        offsets = tokenizer.offsets(rest)
        if len(offsets) <= limit:
            spans.append((start, len(text)))
            return spans

        end = offsets[limit - 1][1]
        while end > 0 and tokenizer.count_tokens(rest[:end]) > limit:
            end -= 1
        if end <= 0:
            raise ValueError(
                f"token limit {limit} cannot represent a single character of this text"
            )
        spans.append((start, start + end))
        start += end
    return spans


def chunk_document(
    document: ParsedDocument, tokenizer: Tokenizer, config: IngestionConfig
) -> list[Chunk]:
    """Chunk a parsed document, paragraph-aware, with no overlap.

    Paragraph boundaries are preserved wherever possible: paragraphs are packed
    together until the next one would exceed the budget. Only a paragraph that
    is itself over budget is split internally, and each of its pieces keeps that
    paragraph's index as both start and end anchor, so provenance survives the
    split.
    """
    if config.chunk_overlap != 0:
        # Overlap is 0 in Design Freeze v1. Implementing a different value
        # silently would produce chunks the anchors no longer describe.
        raise NotImplementedError(
            "chunk_overlap > 0 is not implemented; Design Freeze v1 fixes overlap at 0"
        )

    blocks = build_blocks(document)
    max_tokens = config.chunk_max_tokens
    chunks: list[Chunk] = []
    pending: list[Block] = []

    def flush() -> None:
        if not pending:
            return
        text = "\n\n".join(b.text for b in pending)
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                text=text,
                token_count=tokenizer.count_tokens(text),
                paragraph_start=min(b.paragraph_index for b in pending),
                paragraph_end=max(b.paragraph_index for b in pending),
                section_title=next((b.section_title for b in pending if b.section_title), None),
                page_number=next((b.page_number for b in pending if b.page_number), None),
            )
        )
        pending.clear()

    for block in blocks:
        block_tokens = tokenizer.count_tokens(block.text)

        if block_tokens > max_tokens:
            flush()
            for start, end in token_spans(tokenizer, block.text, max_tokens):
                piece = block.text[start:end].strip()
                if not piece:
                    continue
                chunks.append(
                    Chunk(
                        chunk_index=len(chunks),
                        text=piece,
                        token_count=tokenizer.count_tokens(piece),
                        paragraph_start=block.paragraph_index,
                        paragraph_end=block.paragraph_index,
                        section_title=block.section_title,
                        page_number=block.page_number,
                    )
                )
            continue

        if pending:
            combined = "\n\n".join(b.text for b in (*pending, block))
            if tokenizer.count_tokens(combined) > max_tokens:
                flush()
        pending.append(block)

    flush()
    return chunks

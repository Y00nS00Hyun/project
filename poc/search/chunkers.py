"""Token-bounded chunkers over explicit paragraph/table blocks (PoC only).

Offsets slice the original Unicode string, never decoded subword fragments.
paragraph_start/end are inclusive, zero-based anchors supplied by the caller.
The synthetic fixture adapter is separate from these algorithms.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    paragraph_index: int
    text: str
    content_type: str = "TEXT"
    header: str = ""
    rows: tuple[str, ...] = ()

    def __post_init__(self):
        if self.content_type not in {"TEXT", "TABLE"}:
            raise ValueError("unknown block content_type")
        if self.content_type == "TABLE":
            if not self.header or not self.rows:
                raise ValueError("table requires a header and rows")
            if self.text != "\n".join((self.header, *self.rows)):
                raise ValueError("table text must match its header and rows")


@dataclass(frozen=True)
class Chunk:
    document_id: str
    chunk_index: int
    text: str
    token_count: int
    content_type: str
    paragraph_start: int
    paragraph_end: int


def count_tokens(tokenizer, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False))


def token_spans(tokenizer, text: str, limit: int):
    """Yield contiguous character spans within a token budget; no text is lost."""
    if limit < 1:
        raise ValueError("token budget must be positive")
    start = 0
    while start < len(text):
        rest = text[start:]
        offsets = tokenizer(rest, add_special_tokens=False, return_offsets_mapping=True)["offset_mapping"]
        if len(offsets) <= limit:
            yield start, len(text)
            return
        end = offsets[limit - 1][1]
        # A substring can retokenize differently at a word boundary. Recheck it.
        while end > 0 and count_tokens(tokenizer, rest[:end]) > limit:
            end -= 1
        if end <= 0:
            raise ValueError("token budget cannot represent one Unicode character")
        yield start, start + end
        start += end


def chunk_blocks(document_id: str, blocks: list[Block], tokenizer,
                 strategy: str, size: int) -> list[Chunk]:
    if strategy not in {"fixed", "paragraph", "table"}:
        raise ValueError(f"unknown strategy: {strategy}")
    if size < 1:
        raise ValueError("size must be positive")
    if any(a.paragraph_index >= b.paragraph_index for a, b in zip(blocks, blocks[1:])):
        raise ValueError("block anchors must be strictly increasing")
    chunks: list[Chunk] = []

    def emit(text, first, last, kind="TEXT"):
        if not text.strip():
            return
        tokens = count_tokens(tokenizer, text)
        if tokens > size:
            raise ValueError("chunk exceeded token budget")
        chunks.append(Chunk(document_id, len(chunks), text, tokens, kind, first, last))

    if strategy == "fixed":
        text = "\n\n".join(b.text for b in blocks)
        ranges = []
        pos = 0
        for b in blocks:
            ranges.append((pos, pos + len(b.text), b.paragraph_index))
            pos += len(b.text) + 2
        for start, end in token_spans(tokenizer, text, size):
            anchors = [p for a, z, p in ranges if a < end and z > start]
            if anchors:
                emit(text[start:end], min(anchors), max(anchors))
        return chunks

    pending: list[Block] = []

    def flush():
        if pending:
            emit("\n\n".join(b.text for b in pending),
                 pending[0].paragraph_index, pending[-1].paragraph_index)
            pending.clear()

    for block in blocks:
        if not block.text.strip():
            continue
        if strategy == "table" and block.content_type == "TABLE":
            flush()
            rows: list[str] = []
            for row in block.rows:
                if count_tokens(tokenizer, block.header + "\n" + row) > size:
                    # Never silently discard cells or separate a value from its header.
                    raise ValueError(f"{document_id}: table header + one row exceeds size {size}")
                if rows and count_tokens(tokenizer, "\n".join((block.header, *rows, row))) > size:
                    emit("\n".join((block.header, *rows)), block.paragraph_index,
                         block.paragraph_index, "TABLE")
                    rows = []
                rows.append(row)
            emit("\n".join((block.header, *rows)), block.paragraph_index,
                 block.paragraph_index, "TABLE")
        elif count_tokens(tokenizer, block.text) > size:
            flush()
            for start, end in token_spans(tokenizer, block.text, size):
                emit(block.text[start:end], block.paragraph_index, block.paragraph_index)
        else:
            joined = "\n\n".join(b.text for b in [*pending, block])
            if pending and count_tokens(tokenizer, joined) > size:
                flush()
            pending.append(block)
    flush()
    return chunks

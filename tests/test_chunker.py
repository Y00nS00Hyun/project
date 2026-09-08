"""Unit tests for the paragraph-aware chunker.

No embedding model is loaded: chunking only needs a token *counter*, and these
tests supply a deterministic offline one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from document_processing.models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from ingestion.chunker import build_blocks, chunk_document, render_table, token_spans
from ingestion.config import IngestionConfig
from ingestion.tokenizers import SimpleTokenizer


@pytest.fixture
def tokenizer():
    return SimpleTokenizer()


def make_config(max_tokens: int = 64, overlap: int = 0) -> IngestionConfig:
    return IngestionConfig(
        shared_root=Path("/tmp"),
        chunk_target_tokens=max_tokens,
        chunk_max_tokens=max_tokens,
        chunk_overlap=overlap,
    )


def make_document(paragraphs, tables=()) -> ParsedDocument:
    doc = ParsedDocument(file_path="x.hwp", file_type="hwp")
    doc.paragraphs = [
        ParsedParagraph(index=i, text=t) if isinstance(t, str) else t
        for i, t in enumerate(paragraphs)
    ]
    doc.tables = list(tables)
    return doc


def table(rows, anchor, index=0) -> ParsedTable:
    return ParsedTable(
        index=index,
        rows=rows,
        cells=[
            ParsedCell(row=r, column=c, text=v)
            for r, row in enumerate(rows) for c, v in enumerate(row)
        ],
        paragraph_index=anchor,
    )


class TestBlocks:
    def test_empty_paragraphs_are_dropped(self):
        blocks = build_blocks(make_document(["가나다", "   ", "\n", "라마바"]))
        assert [b.text for b in blocks] == ["가나다", "라마바"]

    def test_paragraph_index_comes_from_the_parser(self):
        doc = make_document(["첫 문단", "", "셋째 문단"])
        blocks = build_blocks(doc)
        # Index 1 was blank and dropped; the surviving anchors keep the
        # parser's own numbering rather than being renumbered 0,1.
        assert [b.paragraph_index for b in blocks] == [0, 2]

    def test_table_is_placed_after_its_anchor_paragraph(self):
        doc = make_document(["앞 문단", "뒤 문단"], [table([["부서", "예산"]], anchor=0)])
        blocks = build_blocks(doc)
        assert [b.is_table for b in blocks] == [False, True, False]
        assert blocks[1].paragraph_index == 0

    def test_unanchored_table_is_kept_not_dropped(self):
        doc = make_document(["본문"], [ParsedTable(index=0, rows=[["셀"]], paragraph_index=None)])
        blocks = build_blocks(doc)
        assert any(b.is_table for b in blocks)

    def test_table_anchored_to_a_missing_paragraph_is_kept(self):
        doc = make_document(["본문"], [table([["셀"]], anchor=99)])
        assert any(b.is_table for b in build_blocks(doc))

    def test_table_rendering_preserves_row_and_column_order(self):
        rendered = render_table(table([["부서", "예산"], ["기획실", "300000000"]], anchor=0))
        assert rendered == "부서 | 예산\n기획실 | 300000000"


class TestTokenSpans:
    def test_spans_cover_the_whole_text(self, tokenizer):
        text = " ".join(f"단어{i}" for i in range(50))
        spans = token_spans(tokenizer, text, 10)
        assert spans[0][0] == 0
        assert spans[-1][1] == len(text)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            assert end == start, "spans must be contiguous"

    def test_every_span_respects_the_budget(self, tokenizer):
        text = " ".join(f"단어{i}" for i in range(80))
        for start, end in token_spans(tokenizer, text, 7):
            assert tokenizer.count_tokens(text[start:end]) <= 7

    def test_short_text_is_a_single_span(self, tokenizer):
        assert token_spans(tokenizer, "짧은 문장", 64) == [(0, len("짧은 문장"))]

    def test_zero_limit_is_rejected(self, tokenizer):
        with pytest.raises(ValueError):
            token_spans(tokenizer, "본문", 0)


class TestChunking:
    def test_paragraphs_are_packed_up_to_the_budget(self, tokenizer):
        doc = make_document(["가 나", "다 라", "마 바"])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=64))
        assert len(chunks) == 1
        assert chunks[0].paragraph_start == 0
        assert chunks[0].paragraph_end == 2

    def test_paragraph_boundaries_are_respected_when_splitting(self, tokenizer):
        # Each paragraph is 2 tokens; a 4-token budget fits exactly two.
        doc = make_document(["가 나", "다 라", "마 바", "사 아"])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=4))
        assert [(c.paragraph_start, c.paragraph_end) for c in chunks] == [(0, 1), (2, 3)]

    def test_no_chunk_exceeds_the_hard_maximum(self, tokenizer):
        doc = make_document([" ".join(f"단어{i}" for i in range(200))])
        for chunk in chunk_document(doc, tokenizer, make_config(max_tokens=16)):
            assert chunk.token_count <= 16

    def test_long_paragraph_is_split_keeping_its_anchor(self, tokenizer):
        doc = make_document(["짧은 문단", " ".join(f"단어{i}" for i in range(100))])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=10))
        long_chunks = [c for c in chunks if c.paragraph_start == 1]
        assert len(long_chunks) > 1, "the long paragraph should be split"
        for chunk in long_chunks:
            # Provenance survives the split: every piece still points at the
            # paragraph it came from.
            assert chunk.paragraph_start == chunk.paragraph_end == 1

    def test_chunk_index_is_dense_and_ordered(self, tokenizer):
        doc = make_document([" ".join(f"단어{i}" for i in range(60))])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=8))
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_chunking_is_deterministic(self, tokenizer):
        doc = make_document(["가 나 다", " ".join(f"단어{i}" for i in range(40)), "마지막"])
        config = make_config(max_tokens=12)
        first = chunk_document(doc, tokenizer, config)
        second = chunk_document(doc, tokenizer, config)
        assert [(c.chunk_index, c.text, c.paragraph_start, c.paragraph_end) for c in first] == [
            (c.chunk_index, c.text, c.paragraph_start, c.paragraph_end) for c in second
        ]

    def test_paragraph_start_is_usable_as_a_citation_anchor(self, tokenizer):
        doc = make_document(["가", "나", "다"])
        for chunk in chunk_document(doc, tokenizer, make_config(max_tokens=2)):
            assert chunk.paragraph_start <= chunk.paragraph_end
            assert chunk.paragraph_start >= 0

    def test_empty_document_yields_no_chunks(self, tokenizer):
        assert chunk_document(make_document([]), tokenizer, make_config()) == []

    def test_whitespace_only_document_yields_no_chunks(self, tokenizer):
        assert chunk_document(make_document(["  ", "\n"]), tokenizer, make_config()) == []


class TestTableHandling:
    def test_table_text_is_chunked_with_the_body(self, tokenizer):
        doc = make_document(["예산 현황"], [table([["부서", "예산"], ["기획실", "300000000"]], 0)])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=64))
        combined = "\n".join(c.text for c in chunks)
        # independent table-aware chunking is OFF, but the cells must still be
        # searchable -- they are not dropped.
        assert "기획실" in combined and "300000000" in combined

    def test_tables_do_not_become_a_separate_chunk_type(self, tokenizer):
        doc = make_document(["앞 문단"], [table([["가", "나"]], 0)])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=64))
        assert len(chunks) == 1, "table must not be forced into its own chunk"

    def test_table_chunk_anchors_to_its_paragraph(self, tokenizer):
        doc = make_document(["짧음", "긴 " * 1], [table([["가"]], 1)])
        chunks = chunk_document(doc, tokenizer, make_config(max_tokens=3))
        anchors = {(c.paragraph_start, c.paragraph_end) for c in chunks}
        assert all(0 <= a <= 1 and 0 <= b <= 1 for a, b in anchors)


class TestConfigGuards:
    def test_nonzero_overlap_is_refused(self, tokenizer):
        # Silently ignoring overlap would produce chunks whose anchors no longer
        # describe them.
        with pytest.raises(NotImplementedError):
            chunk_document(make_document(["가"]), tokenizer, make_config(overlap=3))

    def test_target_above_max_is_rejected(self):
        with pytest.raises(Exception):
            IngestionConfig(shared_root=Path("/tmp"), chunk_target_tokens=128, chunk_max_tokens=64)

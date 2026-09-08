"""Verify evidence preservation and the experiment contract without downloads."""
from __future__ import annotations

import json
import csv
import hashlib
import re
from pathlib import Path

import pytest

from chunkers import Block, chunk_blocks, count_tokens
from dataset import load_dataset
from structured_dataset import LAYOUT, load_layout, structure_document


class CharacterTokenizer:
    """Every non-space Unicode character is one token, with original offsets."""
    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text if not c.isspace()]

    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(i, i + 1) for i, c in enumerate(text) if not c.isspace()]}


TOKENIZER = CharacterTokenizer()


def test_fixed_preserves_unicode_without_loss_and_maps_crossing_anchors():
    blocks = [Block(3, "한국어 가나다🧑🏽‍💻"), Block(7, "서버 장애 30분 복구")]
    chunks = chunk_blocks("D", blocks, TOKENIZER, "fixed", 8)
    assert "".join(c.text for c in chunks) == "\n\n".join(b.text for b in blocks)
    assert all(c.token_count <= 8 for c in chunks)
    assert chunks[0].paragraph_start == 3
    assert chunks[-1].paragraph_end == 7
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_paragraph_boundary_preserved_unless_single_paragraph_is_oversize():
    blocks = [Block(0, "가나다라"), Block(1, "마바사아"), Block(2, "자차카타파하거너더러")]
    chunks = chunk_blocks("D", blocks, TOKENIZER, "paragraph", 6)
    assert [c.text for c in chunks] == ["가나다라", "마바사아", "자차카타파하", "거너더러"]
    assert [(c.paragraph_start, c.paragraph_end) for c in chunks] == [(0, 0), (1, 1), (2, 2), (2, 2)]


def test_paragraph_accumulates_whole_blocks():
    chunks = chunk_blocks("D", [Block(0, "가나"), Block(4, "다라"), Block(8, "마바사")], TOKENIZER, "paragraph", 5)
    assert [(c.paragraph_start, c.paragraph_end) for c in chunks] == [(0, 4), (8, 8)]
    assert chunks[0].text == "가나\n\n다라"


def test_long_table_repeats_header_and_preserves_rows_once_with_no_prose_mix():
    header, rows = "항목|금액", ("서버|120원", "교육|90원", "인건비|90원")
    table = Block(4, "\n".join((header, *rows)), "TABLE", header, rows)
    chunks = chunk_blocks("D", [Block(0, "예산안"), table, Block(9, "집행부서")], TOKENIZER, "table", 15)
    tables = [c for c in chunks if c.content_type == "TABLE"]
    assert len(tables) == 3
    assert all(c.text.splitlines()[0] == header for c in tables)
    assert [line for c in tables for line in c.text.splitlines()[1:]] == list(rows)
    assert all(c.paragraph_start == c.paragraph_end == 4 for c in tables)
    assert all(c.token_count <= 15 for c in chunks)
    assert [c.text for c in chunks if c.content_type == "TEXT"] == ["예산안", "집행부서"]


def test_oversize_table_row_fails_explicitly_instead_of_dropping_cells():
    table = Block(0, "헤더\n아주긴행내용", "TABLE", "헤더", ("아주긴행내용",))
    with pytest.raises(ValueError, match="header.*row"):
        chunk_blocks("D", [table], TOKENIZER, "table", 5)


@pytest.mark.parametrize("strategy", ["fixed", "paragraph", "table"])
def test_empty_input_and_invalid_budget(strategy):
    assert chunk_blocks("D", [], TOKENIZER, strategy, 8) == []
    with pytest.raises(ValueError, match="positive"):
        chunk_blocks("D", [], TOKENIZER, strategy, 0)


def test_invalid_structure_rejected():
    with pytest.raises(ValueError, match="match"):
        Block(0, "different", "TABLE", "H", ("row",))
    with pytest.raises(ValueError, match="increasing"):
        chunk_blocks("D", [Block(1, "a"), Block(0, "b")], TOKENIZER, "fixed", 8)


def test_layout_has_no_new_numeric_facts_and_preserves_unaffected_source():
    dataset, layout = load_dataset(), load_layout()
    assert dataset.validate() == []
    assert set(layout) == {"DOC-003", "DOC-006", "DOC-007"}
    for document in dataset.documents:
        blocks = structure_document(document, layout)
        rendered = "\n\n".join(b.text for b in blocks[1:])
        assert re.findall(r"\d[\d,]*", document.text) == re.findall(r"\d[\d,]*", rendered)
        if document.document_id not in layout:
            assert " ".join(b.text for b in blocks[1:]) == document.text
        for table in layout.get(document.document_id, []):
            assert document.text.count(table["source_text"]) == 1
    assert set(json.loads(LAYOUT.read_text())["table_queries"]) <= {q.query_id for q in dataset.queries}


def test_stale_layout_cannot_silently_change_corpus():
    document = load_dataset().documents[0]
    with pytest.raises(ValueError, match="uniquely"):
        structure_document(document, {document.document_id: [{"source_text": "not in source"}]})


def test_bounded_matrix_and_backward_compatible_db_dimension():
    from chunking_evaluate import CONFIGURATIONS
    from db import EMBEDDING_DIM, load_corpus

    assert len(CONFIGURATIONS) == 9
    assert len({c["size"] for c in CONFIGURATIONS}) == 2
    assert {c["model"] for c in CONFIGURATIONS} == {"e5-small", "e5-base"}
    assert all(c["overlap"] == 0 for c in CONFIGURATIONS)
    assert EMBEDDING_DIM == 384
    for value in (0, -1, 16001, "768", True):
        with pytest.raises(ValueError, match="embedding_dim"):
            load_corpus(None, [], [], embedding_dim=value)


def test_real_tokenizer_preserves_korean_and_enforces_actual_budget():
    cache = Path("/tmp/chunking-embedding-hf/hub")
    if not cache.exists():
        pytest.skip("optional offline tokenizer check; no model downloads in tests")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained("intfloat/multilingual-e5-small", cache_dir=str(cache),
                                              revision="614241f622f53c4eeff9890bdc4f31cfecc418b3", local_files_only=True)
    text = ("한국어 및 숫자 120,000,000원과 🧑🏽‍💻 표를 확인한다. " * 40).strip()
    chunks = chunk_blocks("D", [Block(0, text)], tokenizer, "fixed", 64)
    assert "".join(c.text for c in chunks) == text
    assert all(c.token_count == count_tokens(tokenizer, c.text) <= 64 for c in chunks)


def test_saved_results_recompute_from_unchanged_query_labels():
    from evaluate import ndcg_at_k, recall_at_k, reciprocal_rank

    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts/chunking-embedding-poc"
    if not (output / "query_results.jsonl").exists():
        pytest.skip("run the measured PoC to validate its artifacts")
    manifest = json.loads((output / "configurations.json").read_text())
    assert manifest["status"] == "COMPLETE"
    for filename, checksum in manifest["input_sha256"].items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == checksum
    dataset = load_dataset()
    queries = {q.query_id: q for q in dataset.queries}
    results = [json.loads(line) for line in (output / "query_results.jsonl").read_text().splitlines()]
    assert len(results) == 300
    assert len({(r["method"], r["query_id"]) for r in results}) == 300
    for result in results:
        query = queries[result["query_id"]]
        ranked = result["ranked_documents"]
        assert len(set(ranked)) == len(ranked) == 10
        assert set(ranked) <= dataset.document_ids
        assert result["relevant_documents"] == list(query.relevant_documents)
        assert result["primary_document"] == query.primary_document
        if query.is_answerable:
            relevant = set(query.relevant_documents)
            assert result["recall_at_1"] == recall_at_k(ranked, relevant, 1)
            assert result["recall_at_5"] == recall_at_k(ranked, relevant, 5)
            assert result["reciprocal_rank"] == reciprocal_rank(ranked, relevant)
            assert result["ndcg_at_5"] == ndcg_at_k(ranked, query, 5)
        else:
            assert all(result[k] is None for k in ["recall_at_1", "recall_at_5", "reciprocal_rank", "ndcg_at_5"])
    for metric in csv.DictReader((output / "metrics.csv").open()):
        subset = [r for r in results if r["method"] == metric["method"] and r["relevant_documents"]]
        assert len(subset) == 28
        for column, field in [("recall_at_1", "recall_at_1"), ("recall_at_5", "recall_at_5"),
                              ("mrr", "reciprocal_rank"), ("ndcg_at_5", "ndcg_at_5")]:
            assert float(metric[column]) == pytest.approx(sum(r[field] for r in subset) / 28, abs=0.00005)
        assert float(metric["primary_at_1"]) == pytest.approx(sum(r["primary_rank"] == 1 for r in subset) / 28, abs=0.00005)


def test_saved_chunk_statistics_and_model_pairing():
    output = Path(__file__).resolve().parents[1] / "artifacts/chunking-embedding-poc"
    if not (output / "chunks.jsonl").exists():
        pytest.skip("run the measured PoC to validate its artifacts")
    chunks = [json.loads(line) for line in (output / "chunks.jsonl").read_text().splitlines()]
    for row in csv.DictReader((output / "chunk_statistics.csv").open()):
        subset = [c for c in chunks if c["configuration"] == row["configuration"]]
        assert len(subset) == int(row["chunk_count"])
        assert all(0 < c["token_count"] <= int(row["size"]) for c in subset)
        assert all(c["paragraph_start"] <= c["paragraph_end"] for c in subset)
        assert sum(c["content_type"] == "TABLE" for c in subset) == int(row["table_chunks"])
        assert sum(c["token_count"] < 16 for c in subset) == int(row["tiny_chunks_lt_16"])
    for a, b in [("C1", "C4"), ("C2", "C5"), ("C3", "C6")]:
        def records(config):
            return [{k: v for k, v in c.items() if k != "configuration"} for c in chunks if c["configuration"] == config]
        assert records(a) == records(b)

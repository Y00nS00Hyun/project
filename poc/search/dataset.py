"""Dataset loading for the Korean search PoC.

The evaluation harness only ever sees ``Document`` and ``Query`` objects, so
swapping ``fixtures/*.jsonl`` for a real internal export re-runs the whole
comparison with no code change (요청 section 13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class Document:
    document_id: str
    title: str
    department: str
    year: int
    document_type: str
    text: str

    @property
    def searchable_content(self) -> str:
        """Everything a lexical index sees.

        Title, department and type are included because a real user query mixes
        metadata terms with body terms ("재무관리팀이 관리하는 예산 문서").
        """
        return f"{self.title} {self.department} {self.document_type} {self.year} {self.text}"


@dataclass(frozen=True)
class Query:
    query_id: str
    query: str
    relevant_documents: tuple[str, ...]
    primary_document: str | None
    category: str
    difficulty: str

    @property
    def is_answerable(self) -> bool:
        return bool(self.relevant_documents)


@dataclass
class Dataset:
    documents: list[Document] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)

    @property
    def document_ids(self) -> set[str]:
        return {d.document_id for d in self.documents}

    def validate(self) -> list[str]:
        """Label sanity checks. Returns a list of problems (empty is good)."""
        problems: list[str] = []
        ids = self.document_ids
        if len(ids) != len(self.documents):
            problems.append("duplicate document_id")
        seen: set[str] = set()
        for q in self.queries:
            if q.query_id in seen:
                problems.append(f"{q.query_id}: duplicate query_id")
            seen.add(q.query_id)
            for d in q.relevant_documents:
                if d not in ids:
                    problems.append(f"{q.query_id}: unknown relevant document {d}")
            if q.primary_document is not None:
                if q.primary_document not in q.relevant_documents:
                    problems.append(f"{q.query_id}: primary not in relevant_documents")
            elif q.relevant_documents:
                problems.append(f"{q.query_id}: answerable query without a primary")
        return problems


def load_documents(path: Path | str) -> list[Document]:
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                out.append(Document(**json.loads(line)))
    return out


def load_queries(path: Path | str) -> list[Query]:
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            out.append(
                Query(
                    query_id=raw["query_id"],
                    query=raw["query"],
                    relevant_documents=tuple(raw["relevant_documents"]),
                    primary_document=raw.get("primary_document"),
                    category=raw["category"],
                    difficulty=raw.get("difficulty", "unknown"),
                )
            )
    return out


def load_dataset(
    documents_path: Path | str | None = None,
    queries_path: Path | str | None = None,
) -> Dataset:
    return Dataset(
        documents=load_documents(documents_path or FIXTURES / "documents.jsonl"),
        queries=load_queries(queries_path or FIXTURES / "queries.jsonl"),
    )


# ---------------------------------------------------------------------------
# Baseline chunking (요청 section 18)
#
# Deliberately naive: split the body into sentences. This is a *baseline for
# comparing search methods*, not a chunking proposal. The real chunking policy
# is decided later, after the internal corpus is measured.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Chunk:
    document_id: str
    chunk_index: int
    text: str


def chunk_document(document: Document) -> list[Chunk]:
    """Title becomes chunk 0; each body sentence becomes its own chunk."""
    chunks = [Chunk(document.document_id, 0, f"{document.title} ({document.department}, {document.year})")]
    sentences = [s.strip() for s in document.text.replace("다. ", "다.\n").split("\n")]
    for n, sentence in enumerate((s for s in sentences if s), start=1):
        chunks.append(Chunk(document.document_id, n, sentence))
    return chunks

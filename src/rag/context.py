"""Bounded, whole-chunk context assembled in existing document-rank order."""
from dataclasses import asdict, dataclass
import json
import os
from typing import Sequence

from .models import EvidenceChunk


@dataclass(frozen=True)
class RagConfig:
    # Implementation safety defaults, NOT production optimums or thresholds.
    retrieval_limit: int = 5
    max_context_chunks: int = 5
    max_context_chars: int = 12000

    def __post_init__(self):
        if not 1 <= self.retrieval_limit <= 100:
            raise ValueError('RAG_RETRIEVAL_LIMIT must be between 1 and 100')
        if not 1 <= self.max_context_chunks <= 100:
            raise ValueError('RAG_MAX_CONTEXT_CHUNKS must be between 1 and 100')
        if not 2 <= self.max_context_chars <= 1_000_000:
            raise ValueError('RAG_MAX_CONTEXT_CHARS must be between 2 and 1000000')

    @classmethod
    def from_env(cls):
        return cls(
            retrieval_limit=int(os.environ.get('RAG_RETRIEVAL_LIMIT', '5')),
            max_context_chunks=int(os.environ.get('RAG_MAX_CONTEXT_CHUNKS', '5')),
            max_context_chars=int(os.environ.get('RAG_MAX_CONTEXT_CHARS', '12000')),
        )


def serialize_context(chunks: Sequence[EvidenceChunk]) -> str:
    # Escapes quotes/newlines in document text: it cannot close a JSON item.
    return json.dumps([asdict(chunk) for chunk in chunks], ensure_ascii=False)


def assemble_context(chunks: Sequence[EvidenceChunk], config: RagConfig) -> tuple[EvidenceChunk, ...]:
    selected: list[EvidenceChunk] = []
    for chunk in chunks:
        if len(selected) >= config.max_context_chunks:
            break
        if not chunk.text.strip() or any(c.chunk_id == chunk.chunk_id for c in selected):
            continue
        if len(serialize_context([*selected, chunk])) <= config.max_context_chars:
            selected.append(chunk)
    return tuple(selected)

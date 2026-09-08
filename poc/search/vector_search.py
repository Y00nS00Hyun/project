"""Vector search over pgvector.

The embedding model here is a **PoC choice, not a production decision**
(요청 section 17). It was picked because it runs locally on CPU, supports
Korean, and has a checkable permissive licence -- not because it was compared
against alternatives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg

from dataset import Chunk
from lexical_search import ScoredDocument

#: PoC embedding model. MIT licence, 118M params, 384 dimensions, CPU-friendly,
#: multilingual (Korean included). NOT confirmed for production.
DEFAULT_MODEL = "intfloat/multilingual-e5-small"

#: e5-family models require asymmetric prefixes; omitting them measurably hurts
#: retrieval quality, so they are part of the method, not a tuning knob.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


@dataclass
class EmbeddingModel:
    """Thin wrapper so the harness never imports sentence-transformers directly."""

    model_name: str = DEFAULT_MODEL

    def __post_init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        self._model = SentenceTransformer(self.model_name, device="cpu")

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            [PASSAGE_PREFIX + t for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode(
            [QUERY_PREFIX + text], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return vector.tolist()


def embed_chunks(model: EmbeddingModel, chunks: list[Chunk]) -> dict[tuple[str, int], list[float]]:
    vectors = model.embed_passages([c.text for c in chunks])
    return {(c.document_id, c.chunk_index): v for c, v in zip(chunks, vectors)}


class VectorSearch:
    """Chunk-level vector retrieval aggregated to documents.

    Document score = best matching chunk (요청 section 20). No fancier
    aggregation is attempted; this is a baseline for method comparison.
    """

    name = "vector"

    def __init__(self, conn: psycopg.Connection, model: EmbeddingModel):
        self.conn = conn
        self.model = model

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        vector = str(self.model.embed_query(query))
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, MAX(1 - (embedding <=> %s::vector)) AS score
                FROM poc_chunks
                WHERE embedding IS NOT NULL
                GROUP BY document_id
                ORDER BY score DESC, document_id ASC
                LIMIT %s
                """,
                (vector, top_k),
            )
            return [ScoredDocument(row[0], float(row[1])) for row in cur.fetchall()]

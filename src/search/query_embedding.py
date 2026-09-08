"""Query embedding for semantic search.

Reuses the ingestion embedding wrapper rather than loading a second model: a
query must land in the *same* vector space as the documents it is compared
against, which means the same model, the same pinned revision and the same
normalization. A separate loader here would be a silent way for those to drift
apart.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ingestion.config import IngestionConfig
from ingestion.embedding import (
    EmbeddingModelUnavailableError,
    LocalE5Model,
    to_pgvector,
    validate_vector,
)

from .exceptions import SemanticSearchUnavailableError


@runtime_checkable
class QueryEmbedder(Protocol):
    """What semantic search needs. Narrower than the ingestion-side protocol.

    Kept separate so a test double only has to provide query embedding, and so
    adding query support did not have to change the protocol the embedding
    worker already depends on.
    """

    def embed_query(self, text: str) -> list[float]:
        ...


class LocalQueryEmbedder:
    """Embeds queries with the same local model ingestion used.

    Loading is lazy: lexical search and browsing never touch the model, so a
    deployment with no model cache still serves those paths.
    """

    def __init__(self, config: IngestionConfig, model: QueryEmbedder | None = None):
        self.config = config
        self._model = model or LocalE5Model(
            name=config.embedding_model,
            revision=config.embedding_model_revision,
            device=config.embedding_device,
            cache_dir=config.embedding_cache_dir,
            allow_download=config.embedding_allow_download,
            batch_size=config.embedding_batch_size,
        )

    def embed_query_literal(self, text: str) -> str:
        """Return a validated query vector as a pgvector literal.

        Validated with the same rules as document vectors -- a query vector of
        the wrong dimension or with a NaN would otherwise produce silently
        meaningless distances rather than an error.
        """
        try:
            vector = self._model.embed_query(text)
        except EmbeddingModelUnavailableError as exc:
            # Degrade this one path, not the whole service.
            raise SemanticSearchUnavailableError(str(exc)) from exc
        validate_vector(vector, self.config.embedding_dimension)
        return to_pgvector(vector)

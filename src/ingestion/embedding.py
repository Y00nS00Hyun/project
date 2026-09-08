"""Local embedding model wrapper and vector validation.

Design Freeze v1 defaults: ``intfloat/multilingual-e5-small``, 384 dimensions,
``passage: `` prefix for documents, L2-normalized vectors, local CPU inference.

Two rules shape this module:

* **Local only.** No embedding SaaS, no inference API, and by default no model
  download at run time. A service that quietly reaches the internet mid-scan is
  both an availability risk and a data-egress question nobody approved.
* **Nothing invalid reaches the database.** A vector is checked for dimension,
  finiteness and unit norm *before* it can be written, because a bad vector in
  `chunks.embedding` is far harder to find later than a failed job now.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from .exceptions import IngestionError

#: Document-side prefix required by the e5 family. Omitting it measurably hurts
#: retrieval, so it is part of the contract, not a tuning knob.
PASSAGE_PREFIX = "passage: "

#: Query-side prefix, recorded here for the search stage. Not used yet.
QUERY_PREFIX = "query: "

#: How far a vector's L2 norm may drift from 1.0 before it is rejected.
#: Generous enough for float32 accumulation, tight enough to catch a model that
#: was not normalized at all.
NORM_TOLERANCE = 1e-3


class EmbeddingModelUnavailableError(IngestionError):
    """The configured model could not be loaded from local storage."""


class VectorValidationError(IngestionError):
    """A produced vector is not safe to store."""


@runtime_checkable
class EmbeddingModel(Protocol):
    """What the embedding service needs from a model.

    Injectable so tests can substitute a deterministic stand-in -- but the real
    model is still exercised by an integration test, because a fake proves
    nothing about the vectors that actually land in the database.
    """

    name: str
    revision: str | None
    dimension: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document chunks, already prefixed and L2-normalized."""
        ...


@dataclass
class LocalE5Model:
    """``sentence-transformers`` model loaded from a local cache.

    Loading is lazy and happens once per worker: re-loading per chunk (or per
    job) would dominate the run time of a batch.
    """

    name: str
    revision: str | None = None
    device: str = "cpu"
    cache_dir: str | None = None
    allow_download: bool = False
    batch_size: int = 8

    _model = None
    _dimension: int | None = None

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._load()
        return int(self._dimension)

    def _load(self):
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingModelUnavailableError(
                "sentence-transformers is not installed; embedding cannot run"
            ) from exc

        previous_offline = os.environ.get("HF_HUB_OFFLINE")
        if not self.allow_download:
            # Belt and braces: the loader is told to stay local, and the hub
            # client is put in offline mode so no transport can be opened.
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            kwargs = {"device": self.device}
            if self.cache_dir:
                kwargs["cache_folder"] = self.cache_dir
            if self.revision:
                kwargs["revision"] = self.revision
            if not self.allow_download:
                kwargs["local_files_only"] = True
            self._model = SentenceTransformer(self.name, **kwargs)
        except Exception as exc:
            raise EmbeddingModelUnavailableError(
                f"embedding model {self.name!r} could not be loaded locally "
                f"(revision={self.revision or 'unpinned'}, download allowed="
                f"{self.allow_download}). Pre-populate the model cache or set "
                f"EMBEDDING_ALLOW_DOWNLOAD=1 deliberately."
            ) from exc
        finally:
            if not self.allow_download:
                if previous_offline is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = previous_offline

        # get_sentence_embedding_dimension was renamed; support both so the
        # pinned sentence-transformers version can move without a code change.
        getter = getattr(self._model, "get_embedding_dimension", None) or (
            self._model.get_sentence_embedding_dimension
        )
        self._dimension = getter()
        return self._model

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, PASSAGE_PREFIX)

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query into the same vector space as the documents.

        Uses the ``query: `` prefix, not ``passage: ``. e5 is trained
        asymmetrically: mixing the two puts queries and documents in subtly
        different places and quietly degrades retrieval.

        Deliberately the same class -- and the same loaded model instance -- as
        document embedding, so a query can never be compared against vectors
        produced by a different model or revision.
        """
        return self._encode([text], QUERY_PREFIX)[0]

    def _encode(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(
            [prefix + text for text in texts],
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [vector.tolist() for vector in vectors]


def validate_vector(vector: Sequence[float], expected_dimension: int) -> None:
    """Reject anything that must not be written to ``chunks.embedding``.

    Checks, in order: dimension, finiteness, unit norm. A dimension mismatch
    would be refused by PostgreSQL anyway, but failing here gives a precise
    message instead of a driver-level error mid-batch.
    """
    if len(vector) != expected_dimension:
        raise VectorValidationError(
            f"vector has {len(vector)} dimensions, expected {expected_dimension}"
        )

    for value in vector:
        if not math.isfinite(value):
            # NaN/Inf silently poison every cosine distance computed against it.
            raise VectorValidationError("vector contains a non-finite value (NaN or Inf)")

    norm = math.sqrt(sum(value * value for value in vector))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise VectorValidationError(
            f"vector is not L2-normalized (norm={norm:.6f}, tolerance={NORM_TOLERANCE})"
        )


def to_pgvector(vector: Sequence[float]) -> str:
    """Render a vector in the literal form pgvector accepts."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"

"""Unit tests for the embedding model wrapper and vector validation.

Fast and offline. The real model is exercised separately in
tests/test_embedding_pipeline.py -- a fake proves nothing about the vectors
that actually reach the database.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from ingestion.config import IngestionConfig
from ingestion.embedding import (
    NORM_TOLERANCE,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    EmbeddingModel,
    EmbeddingModelUnavailableError,
    LocalE5Model,
    VectorValidationError,
    to_pgvector,
    validate_vector,
)
from ingestion.exceptions import ConfigurationError


def unit_vector(dimension: int = 384) -> list[float]:
    value = 1.0 / math.sqrt(dimension)
    return [value] * dimension


class TestValidateVector:
    def test_accepts_a_normalized_vector(self):
        validate_vector(unit_vector(), 384)

    def test_rejects_wrong_dimension(self):
        with pytest.raises(VectorValidationError, match="dimensions"):
            validate_vector(unit_vector(128), 384)

    def test_rejects_nan(self):
        vector = unit_vector()
        vector[0] = float("nan")
        with pytest.raises(VectorValidationError, match="non-finite"):
            validate_vector(vector, 384)

    def test_rejects_inf(self):
        vector = unit_vector()
        vector[5] = float("inf")
        with pytest.raises(VectorValidationError, match="non-finite"):
            validate_vector(vector, 384)

    def test_rejects_unnormalized_vector(self):
        with pytest.raises(VectorValidationError, match="normalized"):
            validate_vector([1.0] * 384, 384)

    def test_rejects_zero_vector(self):
        with pytest.raises(VectorValidationError, match="normalized"):
            validate_vector([0.0] * 384, 384)

    def test_accepts_small_float_drift(self):
        vector = unit_vector()
        vector[0] += NORM_TOLERANCE / 10
        validate_vector(vector, 384)

    def test_dimension_is_checked_before_norm(self):
        # A short vector should report the dimension problem, which is the
        # actionable one, not a confusing norm complaint.
        with pytest.raises(VectorValidationError, match="dimensions"):
            validate_vector([1.0, 2.0], 384)


class TestPgvectorLiteral:
    def test_renders_bracketed_csv(self):
        assert to_pgvector([1.0, -0.5, 0.25]) == "[1.0,-0.5,0.25]"

    def test_round_trips_through_float(self):
        vector = unit_vector(3)
        rendered = to_pgvector(vector)
        parsed = [float(v) for v in rendered.strip("[]").split(",")]
        assert parsed == pytest.approx(vector)


class TestPrefixes:
    def test_document_prefix_is_the_e5_passage_prefix(self):
        assert PASSAGE_PREFIX == "passage: "

    def test_query_prefix_is_defined_for_the_search_stage(self):
        assert QUERY_PREFIX == "query: "


class TestLocalModelLoading:
    def test_missing_model_fails_loudly_without_downloading(self, tmp_path):
        """No silent fallback and no surprise network fetch."""
        model = LocalE5Model(
            name="definitely/not-a-real-model-xyz",
            cache_dir=str(tmp_path / "empty-cache"),
            allow_download=False,
        )
        with pytest.raises(EmbeddingModelUnavailableError) as excinfo:
            model.embed_passages(["본문"])
        assert "could not be loaded locally" in str(excinfo.value)

    def test_error_mentions_how_to_fix_it(self, tmp_path):
        model = LocalE5Model(
            name="definitely/not-a-real-model-xyz",
            cache_dir=str(tmp_path / "empty"),
            allow_download=False,
        )
        with pytest.raises(EmbeddingModelUnavailableError) as excinfo:
            model.embed_passages(["본문"])
        assert "EMBEDDING_ALLOW_DOWNLOAD" in str(excinfo.value)

    def test_offline_env_is_restored_after_a_failed_load(self, tmp_path, monkeypatch):
        import os

        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        model = LocalE5Model(name="nope/nope", cache_dir=str(tmp_path), allow_download=False)
        with pytest.raises(EmbeddingModelUnavailableError):
            model.embed_passages(["x"])
        assert "HF_HUB_OFFLINE" not in os.environ

    def test_local_model_applies_the_passage_prefix(self, monkeypatch):
        """LocalE5Model is where the e5 document prefix is added."""
        captured: dict[str, list[str]] = {}

        class StubEncoder:
            def encode(self, texts, **kwargs):
                captured["texts"] = list(texts)
                import numpy

                return numpy.array([[1.0] + [0.0] * 383 for _ in texts])

        model = LocalE5Model(name="stub")
        model._model = StubEncoder()
        model._dimension = 384

        model.embed_passages(["첫 본문", "둘째 본문"])
        assert captured["texts"] == ["passage: 첫 본문", "passage: 둘째 본문"]

    def test_local_model_requests_normalized_embeddings(self, monkeypatch):
        captured: dict[str, object] = {}

        class StubEncoder:
            def encode(self, texts, **kwargs):
                captured.update(kwargs)
                import numpy

                return numpy.array([[1.0] + [0.0] * 383 for _ in texts])

        model = LocalE5Model(name="stub", batch_size=4)
        model._model = StubEncoder()
        model._dimension = 384
        model.embed_passages(["본문"])
        assert captured["normalize_embeddings"] is True
        assert captured["batch_size"] == 4

    def test_empty_input_needs_no_model(self):
        # Must not attempt a load just to return nothing.
        model = LocalE5Model(name="nope/nope", allow_download=False)
        assert model.embed_passages([]) == []

    def test_satisfies_the_protocol_surface(self):
        # isinstance() against the Protocol would touch `dimension`, whose
        # getter loads the model -- so check the surface without forcing a load.
        model = LocalE5Model(name="x")
        assert callable(model.embed_passages)
        assert {"name", "revision", "dimension"} <= set(dir(model))
        assert EmbeddingModel is not None


class TestEmbeddingConfig:
    def test_defaults_match_design_freeze(self):
        config = IngestionConfig(shared_root=Path("/tmp"))
        assert config.embedding_model == "intfloat/multilingual-e5-small"
        assert config.embedding_dimension == 384
        assert config.embedding_provider == "local"
        assert config.embedding_device == "cpu"

    def test_model_revision_is_pinned_by_default(self):
        config = IngestionConfig(shared_root=Path("/tmp"))
        assert config.embedding_model_revision
        assert len(config.embedding_model_revision) == 40, "expect a full commit sha"

    def test_download_is_off_by_default(self):
        assert IngestionConfig(shared_root=Path("/tmp")).embedding_allow_download is False

    @pytest.mark.parametrize("dimension", [128, 768, 1536])
    def test_dimension_must_match_the_column(self, dimension):
        # chunks.embedding is VECTOR(384); a mismatch must fail at startup, not
        # per-row in the middle of a batch.
        with pytest.raises(ConfigurationError, match="384"):
            IngestionConfig(shared_root=Path("/tmp"), embedding_dimension=dimension)

    def test_batch_size_must_be_positive(self):
        with pytest.raises(ConfigurationError):
            IngestionConfig(shared_root=Path("/tmp"), embedding_batch_size=0)

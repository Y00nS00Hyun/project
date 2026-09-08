"""Two pinned, local CPU E5 candidates; no external embedding API.

Download time is excluded from load time. Refuse implicit truncation, check
dimensions and normalized outputs, and share the same tokenizer across models.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from vector_search import PASSAGE_PREFIX, QUERY_PREFIX


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    revision: str
    dimension: int
    parameters_approx: int
    family: str = "multilingual E5"
    license: str = "MIT"
    max_input_length: int = 512
    query_prefix: str = QUERY_PREFIX
    document_prefix: str = PASSAGE_PREFIX
    languages: str = "100 languages including Korean; XLM-R tokenizer"
    device: str = "CPU, float32; GPU not required for this PoC"


MODELS = {
    "e5-small": ModelSpec("e5-small", "intfloat/multilingual-e5-small",
                          "614241f622f53c4eeff9890bdc4f31cfecc418b3", 384, 118_000_000),
    "e5-base": ModelSpec("e5-base", "intfloat/multilingual-e5-base",
                         "d128750597153bb5987e10b1c3493a34e5a4502a", 768, 278_000_000),
}


class LocalEmbeddingModel:
    def __init__(self, spec: ModelSpec, cache_dir: str, threads: int = 2):
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        torch.manual_seed(0)
        self.spec = spec
        start = time.perf_counter()
        self.model = SentenceTransformer(spec.name, revision=spec.revision,
                                         cache_folder=cache_dir, device="cpu",
                                         local_files_only=True, trust_remote_code=False)
        self.load_seconds = time.perf_counter() - start
        self.tokenizer = self.model.tokenizer
        self.dimension = int(self.model.get_embedding_dimension())
        if self.dimension != spec.dimension:
            raise ValueError("model dimension differs from the recorded candidate")
        self.max_input_length = min(spec.max_input_length, self.model.max_seq_length)
        self.parameter_count = sum(p.numel() for p in self.model.parameters())
        self.parameter_bytes = sum(p.numel() * p.element_size() for p in self.model.parameters())

    def encode(self, texts: list[str], prefix: str, batch_size: int = 8):
        import numpy as np

        inputs = [prefix + text for text in texts]
        counts = [len(self.tokenizer.encode(text, add_special_tokens=True)) for text in inputs]
        if any(n > self.max_input_length for n in counts):
            raise ValueError("input exceeds model limit including prefix/special tokens; truncation forbidden")
        vectors = self.model.encode(inputs, batch_size=batch_size, normalize_embeddings=True,
                                    show_progress_bar=False, convert_to_numpy=True)
        if vectors.shape != (len(texts), self.dimension) or not np.isfinite(vectors).all():
            raise ValueError("invalid embedding output")
        if not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5):
            raise ValueError("embedding is not normalized")
        return vectors

    def metadata(self):
        return {**asdict(self.spec), "load_seconds": self.load_seconds,
                "parameter_count": self.parameter_count, "parameter_bytes": self.parameter_bytes,
                "effective_max_input_length": self.max_input_length}

"""Tokenizer abstraction for chunk sizing.

Chunk boundaries are measured in tokens of the embedding model's tokenizer, so
a chunk that fits here also fits the model later. Ingestion therefore needs the
*tokenizer*, never the model weights -- no inference happens at this stage.

The abstraction exists so tests (and any future model change) can supply a
different counter without touching the chunking algorithm.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class Tokenizer(Protocol):
    """Counts tokens and reports where each token starts and ends."""

    name: str

    def count_tokens(self, text: str) -> int:
        """Number of tokens in ``text``, excluding special tokens."""
        ...

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Character span of each token, in order.

        Spans index the original Unicode string, so slicing by them never
        produces a broken subword fragment.
        """
        ...


class HuggingFaceTokenizer:
    """Tokenizer of the configured embedding model.

    Loads the tokenizer only. ``AutoTokenizer`` does not read model weights, so
    ingestion stays free of inference dependencies (Design Freeze: embedding is
    a separate, later stage).

    Construction is lazy: the tokenizer is fetched on first use, so importing
    this module -- or running a scan that produces no chunks -- costs nothing.
    """

    def __init__(self, name: str):
        self.name = name
        self._tokenizer = None

    def _load(self):
        if self._tokenizer is None:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.name)
        return self._tokenizer

    def count_tokens(self, text: str) -> int:
        return len(self._load().encode(text, add_special_tokens=False))

    def offsets(self, text: str) -> list[tuple[int, int]]:
        encoded = self._load()(text, add_special_tokens=False, return_offsets_mapping=True)
        return [tuple(span) for span in encoded["offset_mapping"]]


_WORDLIKE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class SimpleTokenizer:
    """Deterministic, dependency-free tokenizer.

    Splits on word characters and standalone punctuation. It is **not** the
    embedding model's tokenizer and must not be used to produce chunks destined
    for embedding -- it exists so the chunking algorithm can be tested offline
    and so a deployment can fail loudly rather than silently mis-sizing chunks.
    """

    name = "simple-wordlike"

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in _WORDLIKE.finditer(text)]

    def count_tokens(self, text: str) -> int:
        return len(self.offsets(text))

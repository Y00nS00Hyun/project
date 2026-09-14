"""Embedding smoke test: does this image's embedding runtime actually run here?

The EMBED worker has died three times with SIGILL (exit 132) inside
libtorch_cpu.so -- always at the same instruction, an AVX-512 (EVEX/zmm) one,
on a VM whose CPU has only AVX/AVX2. It is intermittent, so a run that exits 0
proves nothing on its own; a run that exits 132 proves the image cannot be
trusted on this host.

Run it after any rebuild, before trusting the image with a corpus. Piped in
over stdin so it needs nothing inside the image -- it tests the image you
already have rather than requiring a rebuild to add itself to it:

    docker compose exec -T backend python - < scripts/embedding-smoke.py
    docker compose exec -T backend python - 20 44 < scripts/embedding-smoke.py

The exit code is the result: 0 passed, 132 is the SIGILL.

Each stage is printed before it is attempted and flushed immediately, because
SIGILL leaves no traceback -- the last line printed is the whole diagnosis.
The sentences are synthetic: no document text is ever read or logged here.
"""

from __future__ import annotations

import sys
import time

MODEL = "intfloat/multilingual-e5-small"
DIMENSION = 384


def main(argv: list[str]) -> int:
    rounds = int(argv[1]) if len(argv) > 1 else 10
    # 44 is what one of the DOCX documents chunked into, and what was in flight
    # for two of the three crashes.
    batch = int(argv[2]) if len(argv) > 2 else 44

    print(f"stage=start rounds={rounds} batch={batch}", flush=True)

    print("stage=import", flush=True)
    from sentence_transformers import SentenceTransformer

    import torch

    print(f"stage=versions torch={torch.__version__} "
          f"cpu_capability={torch.backends.cpu.get_cpu_capability()}", flush=True)

    print("stage=model_load", flush=True)
    model = SentenceTransformer(MODEL, device="cpu")

    texts = [
        f"passage: 오늘 점심 메뉴는 김치찌개와 제육볶음 {i}입니다. " * 6
        for i in range(batch)
    ]

    # Tokenizing separately so a crash here is distinguishable from one in the
    # forward pass; the tokenizer is Rust, the forward pass is libtorch_cpu.
    print("stage=tokenize", flush=True)
    preprocess = getattr(model, "preprocess", None) or model.tokenize
    preprocess(texts)

    started = time.monotonic()
    for attempt in range(1, rounds + 1):
        print(f"stage=encode round={attempt}", flush=True)
        vectors = model.encode(texts, batch_size=32, show_progress_bar=False)
        if vectors.shape != (batch, DIMENSION):
            print(f"stage=failed reason=shape got={vectors.shape}", flush=True)
            return 1

    elapsed = time.monotonic() - started
    print(f"stage=done rounds={rounds} seconds={elapsed:.1f} ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

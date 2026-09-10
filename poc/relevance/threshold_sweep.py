"""How the existing pg_trgm threshold changes the lexical-gated strategies.

`trigram_threshold` is already a production setting (ingestion/config.py), and
its current 0.20 is documented as provisional -- from the Korean search PoC,
never re-measured on real documents. This sweeps it rather than inventing a new
knob, and it does not touch cosine at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import QUERIES, validate  # noqa: E402
import strategies as S  # noqa: E402

THRESHOLDS = (0.20, 0.30, 0.40, 0.50, 0.60)
TOP_K = 3


def main() -> int:
    validate()
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)

    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    answerable = [q for q in QUERIES if q.answerable]
    no_answer = [q for q in QUERIES if not q.answerable]

    print(f"{'thr':>5} {'전략':>4} {'R@1':>6} {'R@3':>6} {'무관/질의':>9} "
          f"{'no-ans 0건율':>13} {'FP':>5}  놓친 질의")
    print("-" * 84)

    with psycopg.connect(dsn) as conn:
        engine = S.Engine(conn, model.embed_query)
        for thr in THRESHOLDS:
            S.TRIGRAM_THRESHOLD = thr
            for key, fn in (("C", S.strategy_c_lexical_gate),
                            ("D2", S.strategy_d2_lexical_first_rrf)):
                hits1 = hits3 = 0
                irrelevant = 0
                missed = []
                for q in answerable:
                    got = fn(engine, q.text)
                    top = got[:TOP_K]
                    if top and top[0] in q.relevant:
                        hits1 += 1
                    if any(t in q.relevant for t in top):
                        hits3 += 1
                    else:
                        missed.append(q.id)
                    irrelevant += sum(1 for t in got if t not in q.relevant)
                zero = sum(1 for q in no_answer if not fn(engine, q.text))
                fp = sum(len(fn(engine, q.text)) for q in no_answer)
                n = len(answerable)
                print(f"{thr:5.2f} {key:>4} {hits1/n:6.2f} {hits3/n:6.2f} {irrelevant/n:9.1f} "
                      f"{zero/len(no_answer):13.2f} {fp:5d}  {','.join(missed) or '-'}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

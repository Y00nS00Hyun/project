"""How fragile is D'? Sweep its two constants and check the no-answer queries.

A result that only holds at one setting is luck. This varies the boost and the
selectivity ceiling independently and reports what moves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import (  # noqa: E402
    EXACT, NO_ANSWER, PARAPHRASE, PARTIAL, QUERIES as BENCH, by_kind,
)
from body_boost import Scorer, make_strategy_d_selective, strategy_a  # noqa: E402
from body_queries import QUERIES as BODY  # noqa: E402
from metrics import evaluate  # noqa: E402


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    with psycopg.connect(dsn) as conn:
        scorer = Scorer(conn, model.embed_query)
        answerable = [q for q in BENCH if q.answerable]
        no_answer = by_kind(NO_ANSWER)
        base_tops = [strategy_a(scorer, q.text)[0] for q in no_answer]

        print("=" * 100)
        print("D' 민감도 — boost x 선택도 상한")
        print("=" * 100)
        print(f"{'boost':>7}{'상한':>7}{'본문nDCG':>10}{'exactH@1':>10}{'paraH@1':>9}"
              f"{'partH@3':>9}{'partNDCG':>10}{'전체nDCG':>10}{'top3변화':>10}{'무답1위변화':>12}")
        print("-" * 100)

        a_body = evaluate([(strategy_a(scorer, q.text), q.expected) for q in BODY])
        print(f"{'A':>7}{'-':>7}{a_body.ndcg_3:>10.3f}"
              f"{evaluate([(strategy_a(scorer,q.text), q.relevant) for q in by_kind(EXACT)]).hit_1:>10.2f}"
              f"{evaluate([(strategy_a(scorer,q.text), q.relevant) for q in by_kind(PARAPHRASE)]).hit_1:>9.2f}"
              f"{evaluate([(strategy_a(scorer,q.text), q.relevant) for q in by_kind(PARTIAL)]).hit_3:>9.2f}"
              f"{evaluate([(strategy_a(scorer,q.text), q.relevant) for q in by_kind(PARTIAL)]).ndcg_3:>10.3f}"
              f"{evaluate([(strategy_a(scorer,q.text), q.relevant) for q in answerable]).ndcg_3:>10.3f}"
              f"{0:>10}{0:>12}")

        for boost in (0.02, 0.05, 0.10, 0.30, 1.00):
            for ceiling in (0.3, 0.5, 0.7):
                fn = make_strategy_d_selective(boost, ceiling)
                body = evaluate([(fn(scorer, q.text), q.expected) for q in BODY])
                ex = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(EXACT)])
                pa = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARAPHRASE)])
                pt = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARTIAL)])
                wh = evaluate([(fn(scorer, q.text), q.relevant) for q in answerable])
                moved = sum(
                    1 for q in BENCH
                    if strategy_a(scorer, q.text)[:3] != fn(scorer, q.text)[:3]
                )
                na = sum(1 for q, before in zip(no_answer, base_tops)
                         if fn(scorer, q.text)[0] != before)
                print(f"{boost:>7.2f}{ceiling:>7.1f}{body.ndcg_3:>10.3f}{ex.hit_1:>10.2f}"
                      f"{pa.hit_1:>9.2f}{pt.hit_3:>9.2f}{pt.ndcg_3:>10.3f}{wh.ndcg_3:>10.3f}"
                      f"{moved:>10}{na:>12}")

        print()
        print("=" * 100)
        print("boost 가 작아도 Z1 이 고쳐지는 최소값 찾기 — 선택도 0.5 고정")
        print("=" * 100)
        z1 = BODY[0]
        for boost in (0.005, 0.01, 0.02, 0.03, 0.05, 0.10):
            ranked = make_strategy_d_selective(boost, 0.5)(scorer, z1.text)[:3]
            fixed = all(t in z1.expected for t in ranked[:2])
            print(f"  boost={boost:<6} top3: "
                  + " | ".join(("*" if t in z1.expected else " ") + t[:24] for t in ranked)
                  + f"   {'고쳐짐' if fixed else '미해결'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

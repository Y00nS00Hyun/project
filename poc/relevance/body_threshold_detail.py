"""Why C's threshold matters, query by query.

The sweep showed C at threshold 0.5 scoring better on paraphrase than C at 0.9
while matching the baseline overall. A lower threshold fires more often, so
that is the direction where a gain is most likely to be luck -- this looks at
which individual queries move and why, rather than trusting the average.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import EXACT, PARAPHRASE, PARTIAL, QUERIES as BENCH, by_kind  # noqa: E402
from body_boost import Scorer, make_strategy_c, strategy_a  # noqa: E402
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

        print("=" * 96)
        print("1. threshold 를 촘촘히 — boost 는 0.1 고정")
        print("=" * 96)
        print(f"{'threshold':>10}{'본문nDCG':>10}{'exactH@1':>10}{'paraH@1':>9}"
              f"{'paraNDCG':>10}{'partH@3':>9}{'partNDCG':>10}{'전체nDCG':>10}"
              f"{'boost발동':>10}")
        print("-" * 96)
        print(f"{'A (없음)':>10}", end="")
        report(scorer, strategy_a, answerable, fired=0)
        for threshold in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
            fn = make_strategy_c(threshold, 0.1)
            fired = sum(
                1
                for q in answerable
                for comp in scorer.components(q.text)
                if comp.body_lexical >= threshold
            )
            print(f"{threshold:>10.1f}", end="")
            report(scorer, fn, answerable, fired)

        print()
        print("=" * 96)
        print("2. threshold 0.5 에서 순위가 바뀌는 benchmark 질의")
        print("=" * 96)
        low = make_strategy_c(0.5, 0.1)
        high = make_strategy_c(0.9, 0.1)
        for q in answerable:
            a = strategy_a(scorer, q.text)[:3]
            l = low(scorer, q.text)[:3]
            h = high(scorer, q.text)[:3]
            if a == l and a == h:
                continue
            print()
            print(f"[{q.id}] ({q.kind}) {q.text[:56]}")
            print(f"    정답: {', '.join(q.relevant)}")
            fired = [f"{c.title[:22]}={c.body_lexical:.2f}"
                     for c in sorted(scorer.components(q.text),
                                     key=lambda c: -c.body_lexical)[:3]]
            print(f"    body lexical 상위: {' | '.join(fired)}")
            for label, ranked in (("A     ", a), ("th=0.5", l), ("th=0.9", h)):
                marked = [("*" if t in q.relevant else " ") + t[:24] for t in ranked]
                print(f"    {label}: " + " | ".join(marked))
    return 0


def report(scorer, fn, answerable, fired: int) -> None:
    body = evaluate([(fn(scorer, q.text), q.expected) for q in BODY])
    ex = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(EXACT)])
    pa = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARAPHRASE)])
    pt = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARTIAL)])
    wh = evaluate([(fn(scorer, q.text), q.relevant) for q in answerable])
    print(f"{body.ndcg_3:>10.3f}{ex.hit_1:>10.2f}{pa.hit_1:>9.2f}{pa.ndcg_3:>10.3f}"
          f"{pt.hit_3:>9.2f}{pt.ndcg_3:>10.3f}{wh.ndcg_3:>10.3f}{fired:>10}")


if __name__ == "__main__":
    raise SystemExit(main())

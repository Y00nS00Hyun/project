"""Compare title-aware ranking variants on the title regression queries and on
the existing 30-query benchmark.

Two questions, kept separate:
  1. does a title signal fix the queries that are visibly broken?
  2. does it cost anything on paraphrase queries, where the answer's title
     shares no words with the query?
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import EXACT, NO_ANSWER, PARAPHRASE, PARTIAL, QUERIES, TYPE_LABELS, by_kind  # noqa: E402
from title_aware import STRATEGIES, Scorer  # noqa: E402
from title_queries import QUERIES as TITLE_QUERIES  # noqa: E402

TOP_K = 3


def metrics(outcomes):
    n = len(outcomes)
    if not n:
        return 0.0, 0.0, 0.0
    r1 = sum(1 for got, rel in outcomes if got[:1] and got[0] in rel) / n
    r3 = sum(1 for got, rel in outcomes if any(t in rel for t in got[:TOP_K])) / n
    mrr = 0.0
    for got, rel in outcomes:
        for i, t in enumerate(got[:TOP_K], 1):
            if t in rel:
                mrr += 1 / i
                break
    return r1, r3, mrr / n


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)

    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    with psycopg.connect(dsn) as conn:
        scorer = Scorer(conn, model.embed_query)

        print("=" * 76)
        print("제목 회귀 질의 5건 — 제목에 질의가 들어 있는 문서가 상위에 오는가")
        print("=" * 76)
        header = f"{'전략':36} {'R@1':>6} {'R@3':>6} {'MRR@3':>7}  실패"
        print(header); print("-" * len(header))
        for key, (name, fn) in STRATEGIES.items():
            outs = [(fn(scorer, q.text), q.expected) for q in TITLE_QUERIES]
            r1, r3, mrr = metrics(outs)
            fails = [q.id for q, (got, rel) in zip(TITLE_QUERIES, outs)
                     if not any(t in rel for t in got[:TOP_K])]
            print(f"{key}. {name:33} {r1:6.2f} {r3:6.2f} {mrr:7.2f}  {','.join(fails) or '-'}")

        print()
        print("=" * 76)
        print("질의별 상위 3건")
        print("=" * 76)
        for q in TITLE_QUERIES:
            print(f"\n  [{q.id}] {q.text!r}   기대: {', '.join(t[:18] for t in q.expected)}")
            for key, (name, fn) in STRATEGIES.items():
                top = fn(scorer, q.text)[:3]
                mark = "✅" if top and top[0] in q.expected else "❌"
                print(f"    {mark} {key}: " + " | ".join(t[:20] for t in top))

        print()
        print("=" * 76)
        print("기존 30-query 벤치마크 — paraphrase 성능이 떨어지지 않는가")
        print("=" * 76)
        answerable = [q for q in QUERIES if q.answerable]
        for kind in (EXACT, PARAPHRASE, PARTIAL):
            print(f"\n[{TYPE_LABELS[kind]}]  {len(by_kind(kind))}건")
            h = f"  {'전략':34} {'R@1':>6} {'R@3':>6} {'MRR@3':>7}  실패"
            print(h); print("  " + "-" * (len(h) - 2))
            for key, (name, fn) in STRATEGIES.items():
                qs = by_kind(kind)
                outs = [(fn(scorer, q.text), q.relevant) for q in qs]
                r1, r3, mrr = metrics(outs)
                fails = [q.id for q, (got, rel) in zip(qs, outs)
                         if not any(t in rel for t in got[:TOP_K])]
                print(f"  {key}. {name:31} {r1:6.2f} {r3:6.2f} {mrr:7.2f}  {','.join(fails) or '-'}")

        print(f"\n[전체 answerable {len(answerable)}건]")
        h = f"  {'전략':34} {'R@1':>6} {'R@3':>6} {'MRR@3':>7}"
        print(h); print("  " + "-" * (len(h) - 2))
        for key, (name, fn) in STRATEGIES.items():
            outs = [(fn(scorer, q.text), q.relevant) for q in answerable]
            r1, r3, mrr = metrics(outs)
            print(f"  {key}. {name:31} {r1:6.2f} {r3:6.2f} {mrr:7.2f}")

        print(f"\n[{TYPE_LABELS[NO_ANSWER]}]  관련 문서 없는 8건")
        print("  모든 전략이 7건 전부 반환한다 (semantic recall을 유지하므로 당연).")
        print("  이 tail을 어떻게 자를지는 ranking 수정과 별개 문제.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

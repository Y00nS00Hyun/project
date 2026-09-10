"""Compare title-aware ranking strategies with standard metrics.

Reports Hit@k and Recall@k separately. They answer different questions and the
earlier version of this file conflated them: it computed Hit@k and printed it
under the name R@k, which reads as Recall and overstates the result whenever a
query has more than one relevant document.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import EXACT, PARAPHRASE, PARTIAL, TYPE_LABELS, by_kind  # noqa: E402
from metrics import Scores, evaluate  # noqa: E402
from title_aware import STRATEGIES, Scorer  # noqa: E402
from title_queries import QUERIES as TITLE_QUERIES  # noqa: E402

TOP_K = 3


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    with psycopg.connect(dsn) as conn:
        scorer = Scorer(conn, model.embed_query)

        print("=" * 88)
        print("제목 회귀 질의 5건")
        print("=" * 88)
        print(f"{'전략':34} " + Scores.header() + "  실패")
        print("-" * 88)
        for key, (name, fn) in STRATEGIES.items():
            outs = [(fn(scorer, q.text), q.expected) for q in TITLE_QUERIES]
            fails = [q.id for q, (got, rel) in zip(TITLE_QUERIES, outs)
                     if not any(t in rel for t in got[:TOP_K])]
            print(f"{key}. {name:31} " + evaluate(outs).as_row()
                  + f"  {','.join(fails) or '-'}")

        print()
        print("=" * 88)
        print("질의별 상위 3건")
        print("=" * 88)
        for q in TITLE_QUERIES:
            print(f"\n  [{q.id}] {q.text!r}   기대: {', '.join(t[:18] for t in q.expected)}")
            for key, (name, fn) in STRATEGIES.items():
                top = fn(scorer, q.text)[:TOP_K]
                mark = "✅" if top and top[0] in q.expected else "❌"
                print(f"    {mark} {key}: " + " | ".join(t[:20] for t in top))

        print()
        print("=" * 88)
        print("기존 30-query 벤치마크")
        print("=" * 88)
        for kind in (EXACT, PARAPHRASE, PARTIAL):
            queries = by_kind(kind)
            print(f"\n[{TYPE_LABELS[kind]}]  {len(queries)}건")
            print(f"  {'전략':32} " + Scores.header() + "  실패")
            print("  " + "-" * 86)
            for key, (name, fn) in STRATEGIES.items():
                outs = [(fn(scorer, q.text), q.relevant) for q in queries]
                fails = [q.id for q, (got, rel) in zip(queries, outs)
                         if not any(t in rel for t in got[:TOP_K])]
                print(f"  {key}. {name:29} " + evaluate(outs).as_row()
                      + f"  {','.join(fails) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

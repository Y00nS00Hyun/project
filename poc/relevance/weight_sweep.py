"""How much title boost is enough, and how much is too much.

Smallest weight that satisfies every condition wins. A larger weight buys
nothing once the title-match queries are correct, and each increment is more
room for a common word in a file name to overpower semantic relevance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import EXACT, PARAPHRASE, PARTIAL, by_kind  # noqa: E402
from generic_queries import QUERIES as GENERIC  # noqa: E402
from title_aware import Scorer  # noqa: E402
from title_queries import QUERIES as TITLE_QUERIES  # noqa: E402

WEIGHTS = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)
TOP_K = 3


def rank(scorer: Scorer, query: str, weight: float) -> list[str]:
    comps = scorer.components(query)
    return [
        c.title
        for c in sorted(comps, key=lambda c: (-(c.semantic + weight * c.title_trigram), c.title))
    ]


def score_set(scorer, queries, weight, get_text, get_relevant):
    hits1 = hits3 = 0
    mrr = 0.0
    misses = []
    for q in queries:
        got = rank(scorer, get_text(q), weight)[:TOP_K]
        relevant = get_relevant(q)
        if got and got[0] in relevant:
            hits1 += 1
        if any(t in relevant for t in got):
            hits3 += 1
            for i, t in enumerate(got, 1):
                if t in relevant:
                    mrr += 1 / i
                    break
        else:
            misses.append(q.id)
    n = len(queries) or 1
    return hits1 / n, hits3 / n, mrr / n, misses


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    with psycopg.connect(dsn) as conn:
        scorer = Scorer(conn, model.embed_query)
        baseline = {q.text: rank(scorer, q.text, 0.0) for q in GENERIC}

        header = (f"{'weight':>7} │ {'제목회귀 R@1':>12} {'MRR':>6} │ "
                  f"{'para R@1':>9} {'para R@3':>9} │ {'part R@1':>9} {'part R@3':>9} │ "
                  f"{'exact R@1':>10} │ {'범용 순위변화':>13}")
        print(header)
        print("─" * len(header))

        for w in WEIGHTS:
            t1, _, tmrr, tmiss = score_set(
                scorer, TITLE_QUERIES, w, lambda q: q.text, lambda q: q.expected)
            p1, p3, _, _ = score_set(
                scorer, by_kind(PARAPHRASE), w, lambda q: q.text, lambda q: q.relevant)
            a1, a3, _, _ = score_set(
                scorer, by_kind(PARTIAL), w, lambda q: q.text, lambda q: q.relevant)
            e1, _, _, _ = score_set(
                scorer, by_kind(EXACT), w, lambda q: q.text, lambda q: q.relevant)

            # How far the generic queries drifted from semantic-only order.
            changed = sum(
                1 for q in GENERIC if rank(scorer, q.text, w)[:3] != baseline[q.text][:3]
            )
            mark = "  ←" if w == 0.0 else ""
            print(f"{w:7.2f} │ {t1:12.2f} {tmrr:6.2f} │ {p1:9.2f} {p3:9.2f} │ "
                  f"{a1:9.2f} {a3:9.2f} │ {e1:10.2f} │ {changed:>10}/8건{mark}")

        print()
        print("범용 질의 상위 1건 변화 (semantic-only 대비)")
        print("─" * 78)
        for q in GENERIC:
            fired = "발동" if q.fires_title_boost else "무관"
            row = f"  {q.id} {q.text:6} [{fired}] "
            for w in WEIGHTS:
                top = rank(scorer, q.text, w)[0]
                ok = "✅" if top in q.acceptable else "❌"
                row += f" {w:.2f}:{ok}"
            print(row)

        print()
        print("제목 회귀 질의 상위 3건")
        print("─" * 78)
        for q in TITLE_QUERIES:
            print(f"\n  [{q.id}] {q.text!r}")
            for w in WEIGHTS:
                top = rank(scorer, q.text, w)[:3]
                ok = "✅" if top and top[0] in q.expected else "❌"
                print(f"    {w:.2f} {ok} " + " | ".join(t[:20] for t in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

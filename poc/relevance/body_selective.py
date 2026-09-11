"""Does requiring the evidence to be *rare* as well as strong fix the regression?

C and D both lose partial/ambiguous accuracy on short Korean queries, because a
common phrase matches strongly almost everywhere. This measures the same two
rules with a selectivity ceiling added: boost only when few enough documents
carry the evidence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import EXACT, PARAPHRASE, PARTIAL, QUERIES as BENCH, by_kind  # noqa: E402
from body_boost import (  # noqa: E402
    Scorer, make_strategy_c, make_strategy_c_selective, make_strategy_d,
    make_strategy_d_selective, strategy_a, strategy_b,
)
from body_queries import QUERIES as BODY  # noqa: E402
from metrics import Scores, evaluate  # noqa: E402


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

        print("=" * 104)
        print("얼마나 많은 문서가 증거를 갖는가 — 7개 문서 중")
        print("=" * 104)
        print(f"{'query':<34}{'유형':<12}{'lex>=0.9':>10}{'포함':>8}   상위 body lexical")
        print("-" * 104)
        for q in list(BODY) + by_kind(PARTIAL) + by_kind(PARAPHRASE)[:3]:
            text = q.text
            kind = getattr(q, "kind", "body-term")
            comps = scorer.components(text)
            strong = sum(1 for x in comps if x.body_lexical >= 0.9)
            contains = sum(1 for x in comps if x.body_contains)
            top = sorted(comps, key=lambda x: -x.body_lexical)[:3]
            print(f"{text[:32]:<34}{kind:<12}{strong:>10}{contains:>8}   "
                  + " ".join(f"{x.body_lexical:.2f}" for x in top))

        print()
        print("=" * 104)
        print("선택도 조건을 더했을 때")
        print("=" * 104)
        variants = {
            "A  현재": strategy_a,
            "B  RRF hybrid": strategy_b,
            "C  강한 body lexical (>=0.9, +0.1)": make_strategy_c(0.9, 0.1),
            "C' 위 + 선택도 <=50%": make_strategy_c_selective(0.9, 0.1),
            "D  본문 정확 일치 (+0.1)": make_strategy_d(0.1),
            "D' 위 + 선택도 <=50%": make_strategy_d_selective(0.1),
        }
        print(f"{'전략':<36}{'본문nDCG':>10}{'exactH@1':>10}{'paraH@1':>9}"
              f"{'partH@3':>9}{'partNDCG':>10}{'전체H@1':>9}{'전체nDCG':>10}")
        print("-" * 104)
        for label, fn in variants.items():
            body = evaluate([(fn(scorer, q.text), q.expected) for q in BODY])
            ex = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(EXACT)])
            pa = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARAPHRASE)])
            pt = evaluate([(fn(scorer, q.text), q.relevant) for q in by_kind(PARTIAL)])
            wh = evaluate([(fn(scorer, q.text), q.relevant) for q in answerable])
            print(f"{label:<36}{body.ndcg_3:>10.3f}{ex.hit_1:>10.2f}{pa.hit_1:>9.2f}"
                  f"{pt.hit_3:>9.2f}{pt.ndcg_3:>10.3f}{wh.hit_1:>9.2f}{wh.ndcg_3:>10.3f}")

        print()
        print("=" * 104)
        print("D' 로 순위가 바뀌는 모든 benchmark 질의 (없으면 회귀 없음)")
        print("=" * 104)
        dprime = make_strategy_d_selective(0.1)
        changed = 0
        for q in BENCH:
            a = strategy_a(scorer, q.text)[:3]
            d = dprime(scorer, q.text)[:3]
            if a == d:
                continue
            changed += 1
            print(f"\n[{q.id}] ({q.kind}) {q.text[:56]}   정답: {', '.join(q.relevant) or '없음'}")
            for label, ranked in (("A ", a), ("D'", d)):
                print(f"    {label}: " + " | ".join(
                    ("*" if t in q.relevant else " ") + t[:24] for t in ranked))
        if not changed:
            print("\n  (없음 — 30-query benchmark 의 top 3 가 전혀 변하지 않는다)")

        print()
        print("=" * 104)
        print("본문 기술용어 질의 top 3 — A 대 D'")
        print("=" * 104)
        for q in BODY:
            print(f"\n[{q.id}] {q.text}   정답: {', '.join(q.expected)}")
            for label, fn in (("A ", strategy_a), ("D'", dprime)):
                ranked = fn(scorer, q.text)[:3]
                print(f"    {label}: " + " | ".join(
                    ("*" if t in q.expected else " ") + t[:24] for t in ranked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

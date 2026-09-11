"""Compare four ranking rules for body-level lexical evidence.

Evaluation only -- production search is not modified by anything here.

Reports, in order:

  1. the raw signal separation, so the choice of threshold is visible rather
     than asserted
  2. every strategy against the 30-query benchmark, broken out by query type,
     and against the 7 body-lexical regression queries
  3. a sweep of C's threshold and boost, and of D's boost
  4. per-query top 5, before and after
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import (  # noqa: E402
    EXACT, NO_ANSWER, PARAPHRASE, PARTIAL, QUERIES as BENCH_QUERIES, TYPE_LABELS, by_kind,
)
from body_boost import (  # noqa: E402
    Scorer, make_strategy_c, make_strategy_d, strategy_a, strategy_b,
)
from body_queries import QUERIES as BODY_QUERIES, validate as validate_body  # noqa: E402
from metrics import Scores, evaluate  # noqa: E402

TOP_K = 3

#: Chosen after looking at the separation printed in section 1, not before.
C_THRESHOLD = 0.90
C_BOOST = 0.10
D_BOOST = 0.10


def strategies():
    return {
        "A": ("현재: semantic + title boost", strategy_a),
        "B": ("RRF hybrid (semantic + lexical)", strategy_b),
        "C": (f"strong body lexical (>={C_THRESHOLD}, +{C_BOOST})",
              make_strategy_c(C_THRESHOLD, C_BOOST)),
        "D": (f"exact body match (+{D_BOOST})", make_strategy_d(D_BOOST)),
    }


def rule(title: str, width: int = 96) -> None:
    print()
    print("=" * width)
    print(title)
    print("=" * width)


def section_signal_separation(scorer: Scorer) -> None:
    rule("1. 신호 분리 — body lexical 이 강한 증거와 잡음을 실제로 가르는가")
    print(f"{'query':<16}{'포함 문서 최소':>14}{'미포함 문서 최대':>16}{'간격':>10}   판정")
    print("-" * 96)
    for q in BODY_QUERIES:
        components = scorer.components(q.text)
        hit = [c.body_lexical for c in components if c.title in q.expected]
        miss = [c.body_lexical for c in components if c.title not in q.expected]
        low, high = min(hit), max(miss) if miss else 0.0
        gap = low - high
        print(f"{q.text:<16}{low:>14.4f}{high:>16.4f}{gap:>10.4f}   "
              f"{'분리됨' if gap > 0.3 else '겹침'}")

    # The same signal on ordinary benchmark queries, where there is no rare
    # token to find. If it were high here too, a threshold could not separate
    # anything.
    print()
    print("참고 — 일반 benchmark 질의에서의 body lexical 분포")
    values = [c.body_lexical for q in BENCH_QUERIES for c in scorer.components(q.text)]
    values.sort()
    print(f"  n={len(values)}  최소={values[0]:.3f}  중앙={values[len(values)//2]:.3f}  "
          f">=0.9인 비율={sum(v >= 0.9 for v in values) / len(values):.1%}")


def score_table(label: str, queries, expected_of, names) -> None:
    print(f"{label:<40} " + Scores.header() + "  실패 질의")
    print("-" * 96)
    for key, (name, fn) in names.items():
        outs = [(fn(SCORER, q.text), expected_of(q)) for q in queries]
        fails = [q.id for q, (got, rel) in zip(queries, outs)
                 if rel and not any(t in rel for t in got[:TOP_K])]
        print(f"{key}. {name:<37} " + evaluate(outs).as_row()
              + f"  {','.join(fails) or '-'}")


def section_metrics() -> None:
    names = strategies()

    rule("2-1. 본문 기술용어 회귀 질의 7건")
    score_table("전략", BODY_QUERIES, lambda q: q.expected, names)

    rule("2-2. 기존 30-query benchmark — 유형별")
    for kind in (EXACT, PARAPHRASE, PARTIAL):
        subset = by_kind(kind)
        print()
        score_table(f"[{TYPE_LABELS[kind]}] {len(subset)}건",
                    subset, lambda q: q.relevant, names)

    print()
    answerable = [q for q in BENCH_QUERIES if q.answerable]
    score_table(f"[답이 있는 질의 전체] {len(answerable)}건",
                answerable, lambda q: q.relevant, names)

    # No-answer queries have no relevant document, so ranking metrics are
    # undefined for them. What can change is which document is offered first,
    # so that is what gets reported.
    rule("2-3. No-answer 질의 8건 — 1위에 오는 문서가 바뀌는가")
    no_answer = by_kind(NO_ANSWER)
    for key, (name, fn) in names.items():
        tops = [fn(SCORER, q.text)[0] for q in no_answer]
        changed = sum(1 for a, b in zip(tops, [strategy_a(SCORER, q.text)[0]
                                               for q in no_answer]) if a != b)
        print(f"  {key}. {name:<40} 1위 변경 {changed}/{len(no_answer)}건")


def section_sweep() -> None:
    rule("3. C 의 threshold / boost 와 D 의 boost 민감도")
    answerable = [q for q in BENCH_QUERIES if q.answerable]

    def summarise(fn) -> str:
        body = evaluate([(fn(SCORER, q.text), q.expected) for q in BODY_QUERIES])
        para = evaluate([(fn(SCORER, q.text), q.relevant) for q in by_kind(PARAPHRASE)])
        part = evaluate([(fn(SCORER, q.text), q.relevant) for q in by_kind(PARTIAL)])
        exact = evaluate([(fn(SCORER, q.text), q.relevant) for q in by_kind(EXACT)])
        whole = evaluate([(fn(SCORER, q.text), q.relevant) for q in answerable])
        return (f"{body.hit_1:>7.2f}{body.hit_3:>7.2f}{body.ndcg_3:>8.3f}"
                f"{exact.hit_1:>9.2f}{para.hit_1:>9.2f}{part.hit_1:>9.2f}{whole.ndcg_3:>9.3f}")

    header = (f"{'설정':<30}{'본문H@1':>7}{'H@3':>7}{'nDCG':>8}"
              f"{'exactH@1':>9}{'paraH@1':>9}{'partH@1':>9}{'전체nDCG':>9}")
    print(header)
    print("-" * 96)
    print(f"{'A (현재)':<30}" + summarise(strategy_a))
    print(f"{'B (RRF)':<30}" + summarise(strategy_b))
    print("-" * 96)
    for threshold in (0.5, 0.7, 0.9, 1.0):
        for boost in (0.05, 0.10, 0.30):
            print(f"{f'C  th={threshold}  boost={boost}':<30}"
                  + summarise(make_strategy_c(threshold, boost)))
    print("-" * 96)
    for boost in (0.05, 0.10, 0.30):
        print(f"{f'D  boost={boost}':<30}" + summarise(make_strategy_d(boost)))


def section_top5() -> None:
    rule("4. 질의별 top 5 — A(현재) 대 C / D")
    names = strategies()
    for q in BODY_QUERIES:
        print()
        print(f"[{q.id}] {q.text}    정답: {', '.join(q.expected)}   ({q.occurrences})")
        for key in ("A", "B", "C", "D"):
            ranked = names[key][1](SCORER, q.text)[:5]
            marked = [("*" if t in q.expected else " ") + t[:26] for t in ranked]
            print(f"   {key}: " + " | ".join(marked))

    print()
    print("표시 — * 는 해당 용어를 실제로 포함한 문서")

    rule("4-2. paraphrase 질의 top 5 — 변화가 없어야 정상")
    for q in by_kind(PARAPHRASE)[:4]:
        print()
        print(f"[{q.id}] {q.text[:60]}    정답: {', '.join(q.relevant)}")
        for key in ("A", "C", "D"):
            ranked = names[key][1](SCORER, q.text)[:5]
            marked = [("*" if t in q.relevant else " ") + t[:26] for t in ranked]
            print(f"   {key}: " + " | ".join(marked))


def main() -> int:
    global SCORER
    validate_body()
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    with psycopg.connect(dsn) as conn:
        SCORER = Scorer(conn, model.embed_query)
        section_signal_separation(SCORER)
        section_metrics()
        section_sweep()
        section_top5()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Before/after on the shipped code path, not on an evaluation replica.

Runs SearchService itself with the body boost weight at its configured value
and at zero, so "before" is the production ranking with one setting changed and
nothing else.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import (  # noqa: E402
    EXACT, NO_ANSWER, PARAPHRASE, PARTIAL, QUERIES as BENCH, TYPE_LABELS, by_kind,
)
from body_queries import QUERIES as BODY  # noqa: E402
from metrics import Scores, evaluate  # noqa: E402


def main() -> int:
    from ingestion.config import config_from_env
    from search.models import SearchMode, SearchRequest
    from search.service import SearchService

    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    user = os.environ["EVAL_USER_ID"]
    config = config_from_env()

    def factory():
        return psycopg.connect(dsn)

    after = SearchService(factory, config)
    before = SearchService(factory, replace(config, body_exact_boost_weight=0.0))

    def rank(service, text: str) -> list[str]:
        result = service.search(SearchRequest(
            user_id=user, query=text, mode=SearchMode.SEMANTIC, page=1, size=20,
        ))
        return [item.title for item in result.items]

    print("=" * 92)
    print(f"body boost = {config.body_exact_boost_weight} / "
          f"selectivity <= {config.body_exact_selectivity_max} / "
          f"title boost = {config.title_boost_weight}")
    print("=" * 92)

    groups = [("Body technical term", BODY, lambda q: q.expected)]
    for kind in (EXACT, PARAPHRASE, PARTIAL):
        groups.append((TYPE_LABELS[kind], by_kind(kind), lambda q: q.relevant))
    groups.append(("답이 있는 질의 전체", [q for q in BENCH if q.answerable],
                   lambda q: q.relevant))

    for label, queries, expected_of in groups:
        print()
        print(f"[{label}] {len(queries)}건")
        print(f"{'':<8} " + Scores.header())
        for name, service in (("before", before), ("after", after)):
            outs = [(rank(service, q.text), expected_of(q)) for q in queries]
            print(f"{name:<8} " + evaluate(outs).as_row())

    no_answer = by_kind(NO_ANSWER)
    moved = [q.id for q in no_answer
             if rank(before, q.text)[:1] != rank(after, q.text)[:1]]
    print()
    print(f"[No-answer] {len(no_answer)}건 — 1위 변경: {len(moved)}건 "
          f"{'(' + ','.join(moved) + ')' if moved else ''}")

    changed = [q.id for q in BENCH
               if rank(before, q.text)[:3] != rank(after, q.text)[:3]]
    print(f"[30-query benchmark] top 3 가 바뀐 질의: {len(changed)}건 "
          f"{'(' + ','.join(changed) + ')' if changed else '(없음)'}")

    print()
    print("=" * 92)
    print("본문 기술용어 top 3 — before / after")
    print("=" * 92)
    for q in BODY:
        b, a = rank(before, q.text)[:3], rank(after, q.text)[:3]
        flag = "" if b == a else "   <- 변경"
        print(f"\n[{q.id}] {q.text}   정답: {', '.join(q.expected)}{flag}")
        for name, order in (("before", b), ("after ", a)):
            print(f"    {name}: " + " | ".join(
                ("*" if t in q.expected else " ") + t[:24] for t in order))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

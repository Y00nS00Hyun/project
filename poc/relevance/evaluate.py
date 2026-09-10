"""Run the benchmark against every strategy and print the comparison.

    docker compose exec -T backend python /dev/stdin < poc/relevance/evaluate.py

Answerable and no-answer queries are scored on different things and are never
averaged together: a strategy that returns the whole corpus scores perfectly on
recall and catastrophically on garbage, and one number would hide both.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import psycopg  # noqa: E402

from benchmark import (  # noqa: E402
    EXACT,
    NO_ANSWER,
    PARAPHRASE,
    PARTIAL,
    QUERIES,
    TYPE_LABELS,
    Query,
    by_kind,
    validate,
)
from metrics import evaluate  # noqa: E402
from strategies import STRATEGIES, Engine  # noqa: E402

TOP_K = 3


@dataclass
class QueryOutcome:
    query: Query
    returned: list[str]

    @property
    def top(self) -> list[str]:
        return self.returned[:TOP_K]

    @property
    def hit_at_1(self) -> bool:
        return bool(self.returned) and self.returned[0] in self.query.relevant

    @property
    def hit_at_3(self) -> bool:
        return any(t in self.query.relevant for t in self.top)

    @property
    def reciprocal_rank(self) -> float:
        for position, title in enumerate(self.top, start=1):
            if title in self.query.relevant:
                return 1.0 / position
        return 0.0

    @property
    def irrelevant_returned(self) -> int:
        """Every returned document that is not relevant -- the whole list, not
        just the top 3. This is what the user actually scrolls through."""
        return sum(1 for t in self.returned if t not in self.query.relevant)


@dataclass
class Metrics:
    """Standard definitions -- see metrics.py.

    An earlier version of this file printed these as "R@1"/"R@3" while
    computing Hit@1/Hit@3. With three relevant documents and one at rank 1,
    Hit@1 is 1.0 but Recall@1 is 1/3; calling the first one Recall overstated
    the result. Both are reported now, under their own names.
    """

    label: str
    n: int = 0
    hit_1: float = 0.0
    hit_3: float = 0.0
    recall_1: float = 0.0
    recall_3: float = 0.0
    precision_1: float = 0.0
    precision_3: float = 0.0
    mrr_3: float = 0.0
    ndcg_3: float = 0.0
    irrelevant_per_query: float = 0.0
    zero_result_rate: float = 0.0
    false_positives: int = 0
    failures: list[str] = field(default_factory=list)


def score_answerable(outcomes: list[QueryOutcome], label: str) -> Metrics:
    n = len(outcomes)
    m = Metrics(label=label, n=n)
    if not n:
        return m
    scores = evaluate([(o.returned, o.query.relevant) for o in outcomes])
    m.hit_1, m.hit_3 = scores.hit_1, scores.hit_3
    m.recall_1, m.recall_3 = scores.recall_1, scores.recall_3
    m.precision_1, m.precision_3 = scores.precision_1, scores.precision_3
    m.mrr_3, m.ndcg_3 = scores.mrr_3, scores.ndcg_3
    m.irrelevant_per_query = sum(o.irrelevant_returned for o in outcomes) / n
    m.failures = [o.query.id for o in outcomes if not o.hit_at_3]
    return m


def score_no_answer(outcomes: list[QueryOutcome], label: str) -> Metrics:
    n = len(outcomes)
    m = Metrics(label=label, n=n)
    if not n:
        return m
    m.zero_result_rate = sum(1 for o in outcomes if not o.returned) / n
    m.false_positives = sum(len(o.returned) for o in outcomes)
    m.irrelevant_per_query = m.false_positives / n
    m.failures = [o.query.id for o in outcomes if o.returned]
    return m


def main() -> int:
    validate()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    config = config_from_env()
    model = LocalE5Model(
        name=config.embedding_model,
        revision=config.embedding_model_revision,
        cache_dir=config.embedding_cache_dir,
        device=config.embedding_device,
    )

    results: dict[str, list[QueryOutcome]] = {}
    with psycopg.connect(dsn) as conn:
        engine = Engine(conn, model.embed_query)
        for key, (_, fn) in STRATEGIES.items():
            results[key] = [QueryOutcome(q, fn(engine, q.text)) for q in QUERIES]

    payload = {
        key: [
            {"id": o.query.id, "kind": o.query.kind, "text": o.query.text,
             "relevant": list(o.query.relevant), "returned": o.returned}
            for o in outcomes
        ]
        for key, outcomes in results.items()
    }
    out = Path(os.environ.get("EVAL_OUT", "/tmp/relevance-results.json"))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    answerable = [q for q in QUERIES if q.answerable]

    print("=" * 78)
    print("전체 요약  (answerable %d건 / no-answer %d건)"
          % (len(answerable), len(QUERIES) - len(answerable)))
    print("=" * 78)
    header = (f"{'전략':32} {'Hit@1':>6} {'Hit@3':>6} {'Rec@1':>7} {'Rec@3':>7} "
              f"{'P@3':>6} {'MRR@3':>7} {'nDCG@3':>7} {'무관/질의':>9}")
    print(header)
    print("-" * len(header))
    for key, (name, _) in STRATEGIES.items():
        outs = [o for o in results[key] if o.query.answerable]
        m = score_answerable(outs, name)
        print(f"{key}. {name:29} {m.hit_1:6.2f} {m.hit_3:6.2f} {m.recall_1:7.2f} "
              f"{m.recall_3:7.2f} {m.precision_3:6.2f} {m.mrr_3:7.2f} "
              f"{m.ndcg_3:7.2f} {m.irrelevant_per_query:9.1f}")

    print()
    print("=" * 78)
    print("No-answer 8건  (관련 문서가 없는 질의)")
    print("=" * 78)
    header2 = f"{'전략':32} {'0건 반환율':>11} {'false positive 총계':>20}"
    print(header2)
    print("-" * len(header2))
    for key, (name, _) in STRATEGIES.items():
        outs = [o for o in results[key] if not o.query.answerable]
        m = score_no_answer(outs, name)
        print(f"{key}. {name:29} {m.zero_result_rate:11.2f} {m.false_positives:20d}")

    print()
    print("=" * 78)
    print("질의 유형별")
    print("=" * 78)
    for kind in (EXACT, PARAPHRASE, PARTIAL):
        print(f"\n[{TYPE_LABELS[kind]}]  {len(by_kind(kind))}건")
        h = (f"  {'전략':30} {'Hit@1':>6} {'Hit@3':>6} {'Rec@1':>7} {'Rec@3':>7} "
             f"{'MRR@3':>7} {'nDCG@3':>7} {'무관/질의':>9}  실패")
        print(h)
        print("  " + "-" * (len(h) - 2))
        for key, (name, _) in STRATEGIES.items():
            outs = [o for o in results[key] if o.query.kind == kind]
            m = score_answerable(outs, name)
            fail = ",".join(m.failures) if m.failures else "-"
            print(f"  {key}. {name:27} {m.hit_1:6.2f} {m.hit_3:6.2f} {m.recall_1:7.2f} "
                  f"{m.recall_3:7.2f} {m.mrr_3:7.2f} {m.ndcg_3:7.2f} "
                  f"{m.irrelevant_per_query:9.1f}  {fail}")

    print(f"\n[{TYPE_LABELS[NO_ANSWER]}]  {len(by_kind(NO_ANSWER))}건")
    h = f"  {'전략':30} {'0건 반환율':>11} {'FP 총계':>9}  0건이 아니었던 질의"
    print(h)
    print("  " + "-" * (len(h) - 2))
    for key, (name, _) in STRATEGIES.items():
        outs = [o for o in results[key] if not o.query.answerable]
        m = score_no_answer(outs, name)
        fail = ",".join(m.failures) if m.failures else "-"
        print(f"  {key}. {name:27} {m.zero_result_rate:11.2f} {m.false_positives:9d}  {fail}")

    print()
    print("=" * 78)
    print("전략별 실패 질의 상세 (answerable 중 R@3 미달)")
    print("=" * 78)
    for key, (name, _) in STRATEGIES.items():
        misses = [o for o in results[key] if o.query.answerable and not o.hit_at_3]
        print(f"\n{key}. {name} — {len(misses)}건 실패")
        for o in misses:
            got = ", ".join(o.top) if o.top else "(0건)"
            print(f"  {o.query.id} {o.query.text}")
            print(f"     기대: {', '.join(o.query.relevant)}")
            print(f"     상위3: {got}")

    print(f"\n원시 결과: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

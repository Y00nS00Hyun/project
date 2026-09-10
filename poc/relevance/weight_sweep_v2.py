"""Weight sweep, recomputed with standard metric definitions.

The earlier sweep printed "R@1" for what was really Hit@1. The rankings and
labels are unchanged -- only the arithmetic and the names are corrected -- so
this re-answers one question: does 0.075 still hold?
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
from metrics import Scores, evaluate  # noqa: E402
from title_aware import Scorer  # noqa: E402
from title_queries import QUERIES as TITLE_QUERIES  # noqa: E402

WEIGHTS = (0.0, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0)


def rank(scorer: Scorer, query: str, weight: float) -> list[str]:
    comps = scorer.components(query)
    return [
        c.title
        for c in sorted(comps, key=lambda c: (-(c.semantic + weight * c.title_trigram), c.title))
    ]


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    from ingestion.config import config_from_env
    from ingestion.embedding import LocalE5Model

    c = config_from_env()
    model = LocalE5Model(name=c.embedding_model, revision=c.embedding_model_revision,
                         cache_dir=c.embedding_cache_dir, device=c.embedding_device)

    groups = [
        ("제목 회귀", TITLE_QUERIES, lambda q: q.expected),
        ("paraphrase", by_kind(PARAPHRASE), lambda q: q.relevant),
        ("partial", by_kind(PARTIAL), lambda q: q.relevant),
        ("exact", by_kind(EXACT), lambda q: q.relevant),
        ("범용", GENERIC, lambda q: q.acceptable),
    ]

    with psycopg.connect(dsn) as conn:
        scorer = Scorer(conn, model.embed_query)
        for label, queries, relevant_of in groups:
            print(f"\n[{label}]  {len(queries)}건")
            print(f"  {'weight':>7} " + Scores.header())
            print("  " + "-" * 72)
            for w in WEIGHTS:
                outs = [(rank(scorer, q.text, w), relevant_of(q)) for q in queries]
                print(f"  {w:7.3f} " + evaluate(outs).as_row())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

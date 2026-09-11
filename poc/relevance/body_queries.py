"""Body-lexical regression queries: rare technical terms inside document text.

The gap these exist to measure. The title-boost work fixed queries whose answer
is named in the *file name*; these are the opposite -- a term that appears
nowhere in any title and many times inside one or two documents.

Ground truth was taken from the corpus with SQL before any strategy was run:

    SELECT d.title, count(*) FROM chunks c ... WHERE c.text ILIKE '%<term>%'

so a document is relevant exactly when it contains the term. That is a
defensible rule *for this query type only* -- someone searching "Zookeeper"
wants the documents that talk about Zookeeper -- and it is why these are kept
apart from the hand-labelled benchmark, where relevance means "substantively
addresses the question" rather than "contains the word".

Redis and PostgreSQL were on the requested list and appear nowhere in the
corpus. They are left out rather than scored against an empty answer set: a
query with no relevant document measures no-answer behaviour, which the
30-query benchmark already covers with eight labelled cases.
"""

from __future__ import annotations

from dataclasses import dataclass

REPORT = "완료보고서_d251126"
SMARTER = "[KISTI]SMARTer고도화-매뉴얼"
DEVMAN = "매뉴얼_윤수현"


@dataclass(frozen=True)
class BodyQuery:
    id: str
    text: str
    expected: tuple[str, ...]
    #: Occurrence counts per document, from the corpus. Recorded so a later
    #: re-ingest that changes them is visible rather than silently shifting the
    #: ground truth.
    occurrences: str


QUERIES: tuple[BodyQuery, ...] = (
    BodyQuery("Z1", "Zookeeper", (REPORT, SMARTER), "완료보고서 8 / SMARTer 6"),
    BodyQuery("Z2", "Kafka", (REPORT, SMARTER), "완료보고서 17 / SMARTer 11"),
    BodyQuery("Z3", "Elasticsearch", (REPORT, SMARTER), "완료보고서 84 / SMARTer 50"),
    # Single-document answers, and one of them lands on a different file from
    # the rest -- otherwise every query here could be satisfied by always
    # ranking the same two documents first.
    BodyQuery("Z4", "FastAPI", (DEVMAN,), "매뉴얼_윤수현 1"),
    BodyQuery("Z5", "MongoDB", (REPORT,), "완료보고서 11"),
    BodyQuery("Z6", "Jenkins", (REPORT,), "완료보고서 4"),
    BodyQuery("Z7", "GitLab", (REPORT,), "완료보고서 6"),
)


def validate() -> None:
    ids = [q.id for q in QUERIES]
    assert len(ids) == len(set(ids)), "duplicate query id"
    assert all(q.expected for q in QUERIES), "every body query must have an answer"

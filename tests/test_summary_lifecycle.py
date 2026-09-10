"""Revision summaries: lifecycle, fencing, and the provider-off path.

No network and no real document. The provider is a local fake whose behaviour
each test chooses, which is what makes "a stale attempt's result is discarded"
and "a disabled provider does not leave a revision waiting forever" testable at
all -- both are about what happens around the call, not inside it.
"""

from __future__ import annotations

import pytest

from ingestion.config import IngestionConfig
from ingestion.repository import IngestionRepository
from rag.models import GenerationResult
from rag.summary import (
    ITEM_OVERHEAD_CHARS, MAX_GROUP_CHARS, MAX_PARTIAL_SUMMARY_CHARS, MAX_SUMMARY_CALLS,
    MAX_SUMMARY_CHARS, REDUCTION_FAN_IN, SINGLE_PASS_CHARS, SummaryChunk, plan_summary,
    reduce_level, truncate_summary,
)
from rag.summary_service import (
    RESULT_NO_TEXT, RESULT_PROVIDER_DISABLED, RESULT_SUMMARIZED, RESULT_SUPERSEDED,
    RESULT_TOO_LARGE, SummaryService,
)

from test_search_backend import (  # noqa: F401
    Corpus, DeterministicEmbedder, config, conn, connection_factory, corpus,
    dsn, embedder, search_db, server,
)


# ---------------------------------------------------------------------------
# Planning: no database, no provider
# ---------------------------------------------------------------------------

def chunk(index: int, size: int, section: str | None = None) -> SummaryChunk:
    return SummaryChunk(f'c{index}', index, section, '가' * size)


def group_weight(group) -> int:
    return sum(len(c.text) + ITEM_OVERHEAD_CHARS for c in group)


def test_a_short_document_is_one_call():
    plan = plan_summary([chunk(0, 100), chunk(1, 100)])
    assert plan.hierarchical is False and plan.max_call_count == 1
    assert len(plan.groups) == 1 and len(plan.groups[0]) == 2


def test_a_long_document_is_grouped_and_reduced():
    plan = plan_summary([chunk(i, 1000) for i in range(30)])
    assert plan.hierarchical is True
    # Every group summarized, then reduced to one.
    assert plan.max_call_count > len(plan.groups)


def test_every_chunk_survives_grouping_exactly_once():
    chunks = [chunk(i, 800, f'section-{i // 4}') for i in range(40)]
    plan = plan_summary(chunks)
    flattened = [c.chunk_id for group in plan.groups for c in group]
    # Order preserved and nothing dropped: a summary that silently omitted part
    # of a document would still describe itself as a summary of the document.
    assert flattened == [c.chunk_id for c in chunks]


def test_sections_are_preferred_as_group_boundaries():
    chunks = [chunk(0, 4000, '1. 개요'), chunk(1, 4000, '2. 예산'), chunk(2, 4000, '3. 일정')]
    plan = plan_summary(chunks)
    for group in plan.groups:
        assert len({c.section_title for c in group}) == 1


def test_blank_and_empty_documents_need_no_call():
    assert plan_summary([]).max_call_count == 0
    assert plan_summary([SummaryChunk('c0', 0, None, '   ')]).max_call_count == 0


# -- the hard bound -----------------------------------------------------------

@pytest.mark.parametrize('chunks', [
    [chunk(i, 1000) for i in range(30)],
    [chunk(i, 40) for i in range(20000)],
    [chunk(i, 800, f's{i // 4}') for i in range(400)],
    [chunk(0, MAX_GROUP_CHARS * 50)],
    [chunk(0, MAX_GROUP_CHARS * 3), chunk(1, 10)],
    [chunk(i, 5900, f's{i}') for i in range(50)],
])
def test_no_first_pass_group_exceeds_the_budget(chunks):
    """The bound holds for every shape of document, without exception.

    Including the ones that used to be handled by widening the budget: many
    tiny chunks, one enormous chunk, and sections that each nearly fill a group
    on their own.
    """
    for group in plan_summary(chunks).groups:
        assert group_weight(group) <= MAX_GROUP_CHARS


def test_an_oversized_chunk_is_split_rather_than_sent_whole():
    plan = plan_summary([chunk(0, MAX_GROUP_CHARS * 3)])
    text = ''.join(c.text for group in plan.groups for c in group)
    # Split, not truncated: every character still reaches a call.
    assert text == '가' * (MAX_GROUP_CHARS * 3)
    for group in plan.groups:
        assert group_weight(group) <= MAX_GROUP_CHARS


def test_group_count_grows_with_length_instead_of_the_budget():
    """The trade the hard bound makes, stated as a test.

    A document ten times longer produces roughly ten times the groups, and the
    same per-call input. The previous design kept the group count at twelve by
    multiplying the budget, which is what put a long document back at risk of
    overflowing a context window.
    """
    small = len(plan_summary([chunk(i, 1000) for i in range(30)]).groups)
    large = len(plan_summary([chunk(i, 1000) for i in range(300)]).groups)
    assert large > small * 5


# -- the reduction ------------------------------------------------------------

def summary_items(count, size=MAX_PARTIAL_SUMMARY_CHARS):
    return [SummaryChunk(f'p{i}', i, None, '가' * size) for i in range(count)]


def test_no_reduction_group_exceeds_the_budget():
    for count in (2, 7, 40, 500):
        for group in reduce_level(summary_items(count)):
            assert group_weight(group) <= MAX_GROUP_CHARS


def test_each_reduction_level_is_strictly_smaller():
    """What makes the recursion terminate.

    Asserted rather than assumed: if a change to the two length bounds made a
    group hold one summary, the worker would loop against a paid API.
    """
    items = summary_items(500)
    while len(items) > 1:
        groups = reduce_level(items)
        assert len(groups) < len(items)
        items = summary_items(len(groups))


def test_the_fan_in_matches_the_declared_bounds():
    groups = reduce_level(summary_items(REDUCTION_FAN_IN))
    assert len(groups) == 1
    assert len(reduce_level(summary_items(REDUCTION_FAN_IN + 1))) == 2


def test_the_call_bound_is_never_an_underestimate():
    """max_call_count must bound the real run, worst case included."""
    for size in (5, 50, 500):
        plan = plan_summary([chunk(i, 1000) for i in range(size)])
        actual = len(plan.groups)
        items = len(plan.groups)
        while items > 1:
            groups = len(reduce_level(summary_items(items)))
            actual += groups
            items = groups
        assert actual <= plan.max_call_count


def test_a_document_past_the_call_ceiling_is_marked_oversized():
    plan = plan_summary([chunk(i, 5000) for i in range(MAX_SUMMARY_CALLS + 50)])
    assert plan.oversized is True
    assert plan_summary([chunk(i, 1000) for i in range(30)]).oversized is False


def test_stored_summary_length_is_bounded():
    assert len(truncate_summary('가' * 100_000)) <= MAX_SUMMARY_CHARS + 1
    assert len(truncate_summary('가' * 100_000, MAX_PARTIAL_SUMMARY_CHARS)) \
        <= MAX_PARTIAL_SUMMARY_CHARS + 1


# ---------------------------------------------------------------------------
# The worker, against a real database
# ---------------------------------------------------------------------------

class ScriptedProvider:
    identifier = 'fake'
    model = 'fake-model'

    def __init__(self, *, fail: bool = False, refuse: bool = False):
        self.fail = fail
        self.refuse = refuse
        self.calls: list[str] = []
        #: Serialized size of every request, so the per-call bound can be
        #: checked against what was actually sent rather than against the plan.
        self.context_sizes: list[int] = []
        self.answer_size = 400
        self.on_call = None

    def generate(self, request):
        import json

        self.calls.append(request.question)
        self.context_sizes.append(len(request.document_context_json))
        if self.on_call is not None:
            self.on_call()
        if self.fail:
            raise RuntimeError('provider exploded with 문서 본문 in the message')
        context = json.loads(request.document_context_json)
        ids = [item['chunk_id'] for item in context]
        if self.refuse:
            return GenerationResult(answerable=False, answer='', citation_chunk_ids=[])
        return GenerationResult(
            answerable=True, answer='요' * self.answer_size,
            citation_chunk_ids=ids,
        )


@pytest.fixture
def provider():
    return ScriptedProvider()


@pytest.fixture
def enabled(monkeypatch):
    """Both switches. Either one alone leaves generation off."""
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('DOCUMENT_EXTERNAL_LLM_ENABLED', 'true')


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.delenv('DOCUMENT_EXTERNAL_LLM_ENABLED', raising=False)


@pytest.fixture
def summarizer(connection_factory, config, provider):  # noqa: F811
    return SummaryService(connection_factory, config, provider)


@pytest.fixture
def ready_revision(corpus):  # noqa: F811
    """One READY revision waiting for a summary."""
    user = corpus.user('reader')
    document_id, revision_id = corpus.document('서버 장애 대응', text='서버 장애 대응 절차 본문')
    corpus.grant(document_id, user_id=user)
    return {'user': user, 'document_id': document_id, 'revision_id': revision_id}


def repo(conn):  # noqa: F811
    return IngestionRepository(conn)


def revision_state(conn, revision_id):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute(
            'SELECT summary_status, summary, summary_provider, summary_prompt_version '
            'FROM document_revisions WHERE id = %s', (revision_id,))
        row = cur.fetchone()
    return {'status': row[0], 'summary': row[1], 'provider': row[2], 'prompt_version': row[3]}


def job_state(conn, revision_id):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, result_code, attempt_count FROM processing_jobs "
            "WHERE document_revision_id = %s AND job_type = 'SUMMARIZE'", (revision_id,))
        return [{'status': r[0], 'result_code': r[1], 'attempt_count': r[2]}
                for r in cur.fetchall()]


# -- queueing ---------------------------------------------------------------

def test_summary_job_is_created_once_per_revision(conn, ready_revision):  # noqa: F811
    revision_id = ready_revision['revision_id']
    first = repo(conn).enqueue_summarize_job(revision_id)
    second = repo(conn).enqueue_summarize_job(revision_id)
    assert first is not None and second is None
    assert len(job_state(conn, revision_id)) == 1


def test_a_not_ready_revision_is_not_queued(conn, corpus):  # noqa: F811
    _, revision_id = corpus.document('미완성', ready=False, promote=False)
    assert repo(conn).enqueue_summarize_job(revision_id) is None


def test_a_second_job_is_allowed_once_the_first_finishes(conn, ready_revision):  # noqa: F811
    revision_id = ready_revision['revision_id']
    job = repo(conn).enqueue_summarize_job(revision_id)
    repo(conn).finish_job(job, status='FAILED', result_code='SUMMARY_FAILED')
    with conn.cursor() as cur:
        cur.execute("UPDATE document_revisions SET summary_status = 'PENDING' WHERE id = %s",
                    (revision_id,))
    # uq_jobs_active only covers PENDING and RUNNING, so a retry is possible
    # while a duplicate is not.
    assert repo(conn).enqueue_summarize_job(revision_id) is not None


# -- the happy path ---------------------------------------------------------

def test_summary_is_generated_and_stored_against_its_revision(
    conn, summarizer, provider, ready_revision, enabled,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    result = summarizer.process_pending()

    assert result.succeeded == 1 and result.by_result_code == {RESULT_SUMMARIZED: 1}
    state = revision_state(conn, revision_id)
    assert state['status'] == 'SUCCESS' and state['summary']
    assert state['provider'] == 'fake' and state['prompt_version']
    assert job_state(conn, revision_id)[0]['status'] == 'SUCCESS'


def test_a_new_revision_does_not_overwrite_an_earlier_summary(
    conn, corpus, summarizer, ready_revision, enabled,  # noqa: F811
):
    old_revision = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(old_revision)
    summarizer.process_pending()
    with conn.cursor() as cur:
        cur.execute('UPDATE document_revisions SET summary = %s WHERE id = %s',
                    ('1차 개정본 요약', old_revision))

    new_revision = corpus.revision(ready_revision['document_id'], 2, text='개정된 본문')
    repo(conn).enqueue_summarize_job(new_revision)
    summarizer.process_pending()

    assert revision_state(conn, old_revision)['summary'] == '1차 개정본 요약'
    assert revision_state(conn, new_revision)['summary'] not in (None, '1차 개정본 요약')


def test_a_long_document_reduces_within_its_call_bound(
    conn, corpus, summarizer, provider, enabled,  # noqa: F811
):
    body = ['가' * 3000 for _ in range(10)]
    _, revision_id = corpus.document('긴 문서', text='긴 문서', chunks=body)
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()

    plan = plan_summary([SummaryChunk(str(i), i, None, text) for i, text in enumerate(body)])
    assert plan.hierarchical
    assert len(plan.groups) < len(provider.calls) <= plan.max_call_count


def test_no_single_call_exceeds_the_budget_however_long_the_document(
    conn, corpus, summarizer, provider, enabled,  # noqa: F811
):
    """The property item 3 is about, measured on the requests themselves.

    A document long enough to need several reduction levels, with the model
    returning full-length summaries every time -- the worst case for the input
    size of the levels above the first.
    """
    provider.answer_size = MAX_PARTIAL_SUMMARY_CHARS * 3
    body = ['가' * 2000 for _ in range(120)]
    _, revision_id = corpus.document('아주 긴 문서', text='긴 문서', chunks=body)
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()

    plan = plan_summary([SummaryChunk(str(i), i, None, text) for i, text in enumerate(body)])
    assert revision_state(conn, revision_id)['status'] == 'SUCCESS'
    # More than level 0 plus a single synthesis: the reduction genuinely
    # recursed, which is the case a two-level design would not cover.
    assert len(provider.calls) > len(plan.groups) + 1
    assert max(provider.context_sizes) <= MAX_GROUP_CHARS


def test_a_document_past_the_call_ceiling_is_skipped_without_calling_out(
    conn, corpus, summarizer, provider, enabled,  # noqa: F811
):
    body = ['가' * 5500 for _ in range(MAX_SUMMARY_CALLS + 20)]
    _, revision_id = corpus.document('한계 초과 문서', text='본문', chunks=body)
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()

    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'
    assert job_state(conn, revision_id)[0]['result_code'] == RESULT_TOO_LARGE
    # Refused before the first call, not part-way through a paid run.
    assert provider.calls == []


# -- failure must not damage the document -----------------------------------

def test_a_failed_summary_leaves_the_document_ready_and_searchable(
    conn, summarizer, provider, ready_revision, enabled,  # noqa: F811
):
    provider.fail = True
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()

    assert revision_state(conn, revision_id)['status'] == 'FAILED'
    with conn.cursor() as cur:
        cur.execute('SELECT is_ready FROM document_revisions WHERE id = %s', (revision_id,))
        assert cur.fetchone()[0] is True
        cur.execute('SELECT current_revision_id FROM documents WHERE id = %s',
                    (ready_revision['document_id'],))
        assert str(cur.fetchone()[0]) == revision_id


def test_a_provider_error_message_is_not_stored_verbatim_in_the_summary(
    conn, summarizer, provider, ready_revision, enabled,  # noqa: F811
):
    provider.fail = True
    repo(conn).enqueue_summarize_job(ready_revision['revision_id'])
    summarizer.process_pending()
    assert revision_state(conn, ready_revision['revision_id'])['summary'] is None


def test_a_refused_generation_is_recorded_as_failed_not_as_a_summary(
    conn, summarizer, provider, ready_revision, enabled,  # noqa: F811
):
    provider.refuse = True
    repo(conn).enqueue_summarize_job(ready_revision['revision_id'])
    summarizer.process_pending()
    state = revision_state(conn, ready_revision['revision_id'])
    assert state['status'] == 'FAILED' and state['summary'] is None


# -- supplement A: the provider is off --------------------------------------

def test_a_disabled_provider_skips_instead_of_waiting_forever(
    conn, summarizer, provider, ready_revision, disabled,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()

    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'
    assert job_state(conn, revision_id)[0]['result_code'] == RESULT_PROVIDER_DISABLED
    # Nothing left the machine.
    assert provider.calls == []


def test_becoming_ready_with_no_provider_queues_nothing_and_skips(
    conn, corpus, connection_factory, disabled,  # noqa: F811
):
    from ingestion.embedding_service import EmbeddingService

    _, revision_id = corpus.document('요약 없는 문서', text='본문')
    with conn.cursor() as cur:
        cur.execute("UPDATE document_revisions SET summary_status = 'PENDING' WHERE id = %s",
                    (revision_id,))
    with connection_factory() as c:
        c.autocommit = True
        assert EmbeddingService._request_summary(IngestionRepository(c), revision_id) is False
    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'
    assert job_state(conn, revision_id) == []


def test_skipped_summaries_are_reopened_when_generation_is_enabled_later(
    conn, summarizer, ready_revision, monkeypatch,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    monkeypatch.delenv('DOCUMENT_EXTERNAL_LLM_ENABLED', raising=False)
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()
    # A provider is configured; the corpus is simply not cleared to be sent.
    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'

    monkeypatch.setenv('DOCUMENT_EXTERNAL_LLM_ENABLED', 'true')
    reopened = repo(conn).resume_skipped_summaries()
    assert reopened == [revision_id]
    assert repo(conn).enqueue_summarize_job(revision_id) is not None
    summarizer.process_pending()
    assert revision_state(conn, revision_id)['status'] == 'SUCCESS'


def test_reopening_never_touches_a_revision_that_already_has_a_summary(
    conn, summarizer, ready_revision, enabled,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()
    with conn.cursor() as cur:
        cur.execute("UPDATE document_revisions SET summary_status = 'SKIPPED' WHERE id = %s",
                    (revision_id,))
    assert repo(conn).resume_skipped_summaries() == []


def test_a_revision_with_no_text_is_skipped_rather_than_summarized(
    conn, corpus, summarizer, provider, enabled,  # noqa: F811
):
    _, revision_id = corpus.document('빈 문서', text='본문', chunks=['   '])
    repo(conn).enqueue_summarize_job(revision_id)
    summarizer.process_pending()
    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'
    assert job_state(conn, revision_id)[0]['result_code'] == RESULT_NO_TEXT
    assert provider.calls == []


# -- supplement B: a stale attempt must not write ----------------------------

def test_a_superseded_attempt_discards_its_summary(
    conn, summarizer, provider, ready_revision, enabled, config, connection_factory,  # noqa: F811
):
    """Attempt 1 hangs, is requeued, attempt 2 takes over -- then attempt 1 wakes.

    The requeue and the second claim are performed while attempt 1 is inside
    the provider call, which is exactly the sequence that makes a
    `status = 'RUNNING'` check insufficient: by the time attempt 1 tries to
    write, the row is RUNNING again, just for somebody else.
    """
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)

    def supersede():
        with conn.cursor() as cur:
            # Age the job past the staleness window, then let recovery and a
            # second claim run exactly as the live worker would.
            cur.execute(
                "UPDATE processing_jobs SET started_at = now() - make_interval(secs => %s) "
                "WHERE document_revision_id = %s AND job_type = 'SUMMARIZE'",
                (config.job_stale_seconds + 60, revision_id),
            )
        assert repo(conn).recover_stale_jobs(config.job_stale_seconds,
                                             job_type='SUMMARIZE')['requeued']
        assert repo(conn).claim_summarize_jobs() != []
        provider.on_call = None

    provider.on_call = supersede
    result = summarizer.process_pending()

    assert result.discarded == 1 and result.by_result_code == {RESULT_SUPERSEDED: 1}
    # The late attempt wrote neither the summary nor the job outcome.
    assert revision_state(conn, revision_id)['summary'] is None
    job = job_state(conn, revision_id)[0]
    assert job['status'] == 'RUNNING' and job['attempt_count'] == 2


def test_a_late_write_is_also_blocked_while_the_job_sits_requeued(conn, ready_revision):  # noqa: F811
    """The gap between recovery and the next claim is covered too.

    Here the counter still matches what attempt 1 was handed -- only the status
    has changed -- so this is the case the attempt_count check alone would miss.
    """
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    claimed = repo(conn).claim_summarize_jobs()[0]
    with conn.cursor() as cur:
        cur.execute("UPDATE processing_jobs SET status = 'PENDING' WHERE id = %s",
                    (claimed['id'],))

    assert repo(conn).finish_job(
        str(claimed['id']), status='SUCCESS', result_code=RESULT_SUMMARIZED,
        attempt_count=claimed['attempt_count'],
    ) is False


def test_the_owning_attempt_is_allowed_to_write(conn, ready_revision):  # noqa: F811
    revision_id = ready_revision['revision_id']
    repo(conn).enqueue_summarize_job(revision_id)
    claimed = repo(conn).claim_summarize_jobs()[0]
    assert repo(conn).finish_job(
        str(claimed['id']), status='SUCCESS', result_code=RESULT_SUMMARIZED,
        attempt_count=claimed['attempt_count'],
    ) is True


def test_an_untokened_finish_keeps_the_previous_behaviour(conn, ready_revision):  # noqa: F811
    """PARSE and EMBED still call finish_job without a token."""
    repo(conn).enqueue_summarize_job(ready_revision['revision_id'])
    claimed = repo(conn).claim_summarize_jobs()[0]
    assert repo(conn).finish_job(
        str(claimed['id']), status='SUCCESS', result_code=RESULT_SUMMARIZED,
    ) is True


# -- reconciling revisions that predate this stage ---------------------------

def test_reconcile_queues_ready_revisions_that_have_no_job(
    conn, summarizer, ready_revision, enabled,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    assert job_state(conn, revision_id) == []

    assert summarizer.reconcile() == {'queued': 1, 'skipped': 0}
    assert len(job_state(conn, revision_id)) == 1
    # Idempotent: the second run sees an active job and leaves it alone.
    assert summarizer.reconcile() == {'queued': 0, 'skipped': 0}


def test_reconcile_skips_instead_of_queueing_when_generation_is_off(
    conn, summarizer, ready_revision, disabled,  # noqa: F811
):
    revision_id = ready_revision['revision_id']
    assert summarizer.reconcile() == {'queued': 0, 'skipped': 1}
    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'
    assert job_state(conn, revision_id) == []


def test_reconcile_ignores_revisions_that_are_not_ready(
    conn, corpus, summarizer, enabled,  # noqa: F811
):
    corpus.document('미완성', ready=False, promote=False)
    assert summarizer.reconcile() == {'queued': 0, 'skipped': 0}


def test_reconcile_leaves_a_finished_summary_alone(
    conn, summarizer, ready_revision, enabled,  # noqa: F811
):
    repo(conn).enqueue_summarize_job(ready_revision['revision_id'])
    summarizer.process_pending()
    assert summarizer.reconcile() == {'queued': 0, 'skipped': 0}
    assert revision_state(conn, ready_revision['revision_id'])['status'] == 'SUCCESS'


def test_turning_generation_on_recovers_revisions_reconciled_while_it_was_off(
    conn, summarizer, ready_revision, monkeypatch,  # noqa: F811
):
    """The full round trip: off at ingest, on later, summary appears."""
    revision_id = ready_revision['revision_id']
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.delenv('DOCUMENT_EXTERNAL_LLM_ENABLED', raising=False)
    summarizer.reconcile()
    assert revision_state(conn, revision_id)['status'] == 'SKIPPED'

    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('DOCUMENT_EXTERNAL_LLM_ENABLED', 'true')
    assert repo(conn).resume_skipped_summaries() == [revision_id]
    assert summarizer.reconcile() == {'queued': 1, 'skipped': 0}
    summarizer.process_pending()
    assert revision_state(conn, revision_id)['status'] == 'SUCCESS'

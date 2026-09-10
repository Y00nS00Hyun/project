"""SUMMARIZE stage: one precomputed overview per document revision.

Runs in the background, after a revision becomes READY. Nothing in the document
detail request path calls a provider -- opening a document must not depend on
an external service being up, being fast, or being paid for, and it must not
send a document anywhere just because somebody clicked on it.

Three rules shape the rest of this module:

* A summary belongs to the revision it was built from. Rev N gets summary N,
  and promoting rev N+1 adds a summary rather than rewriting one.
* A failed summary is not a failed document. summary_status is not part of the
  generated is_ready column, so nothing here can remove a document from search
  or make it unreadable.
* Text that arrives from a document is data. It is placed in the context field
  of a request, never in the instruction field, and the instruction says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from ingestion.config import IngestionConfig
from ingestion.repository import IngestionRepository

from .models import EvidenceChunk
from .prompts import GenerationRequest
from .provider import LLMProvider, document_generation_enabled
from .summary import (
    MAX_PARTIAL_SUMMARY_CHARS,
    PARTIAL_QUESTION,
    REDUCTION_QUESTION,
    SINGLE_PASS_QUESTION,
    SUMMARY_PROMPT_VERSION,
    SUMMARY_SYSTEM_INSTRUCTION,
    SYNTHESIS_QUESTION,
    SummaryChunk,
    plan_summary,
    reduce_level,
    truncate_summary,
)
from .context import serialize_context
from .validation import validate_generation

logger = logging.getLogger('rag.summary')

STATUS_SUCCESS = 'SUCCESS'
STATUS_FAILED = 'FAILED'
STATUS_SKIPPED = 'SKIPPED'

#: processing_jobs.result_code for this stage. Small and closed, like EMBED's.
RESULT_SUMMARIZED = 'SUMMARIZED'
RESULT_NO_TEXT = 'NO_TEXT'
RESULT_TOO_LARGE = 'TOO_LARGE'
RESULT_PROVIDER_DISABLED = 'PROVIDER_DISABLED'
RESULT_REFUSED = 'SUMMARY_REFUSED'
RESULT_FAILED = 'SUMMARY_FAILED'
#: Not an outcome of the work, but of the attempt: a later attempt owns the job.
RESULT_SUPERSEDED = 'SUPERSEDED'


@dataclass
class SummaryRunResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    discarded: int = 0
    provider_calls: int = 0
    by_result_code: dict[str, int] = field(default_factory=dict)

    def record(self, code: str) -> None:
        self.by_result_code[code] = self.by_result_code.get(code, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            'processed': self.processed, 'succeeded': self.succeeded,
            'failed': self.failed, 'skipped': self.skipped,
            'discarded': self.discarded, 'provider_calls': self.provider_calls,
            'by_result_code': dict(self.by_result_code),
        }


class SummaryService:
    """Turns queued SUMMARIZE jobs into revision summaries."""

    def __init__(self, connection_factory, config: IngestionConfig, provider: LLMProvider):
        self.connection_factory = connection_factory
        self.config = config
        self.provider = provider

    # -- queue -------------------------------------------------------------

    def reconcile(self, limit: int = 1000) -> dict[str, int]:
        """Give every READY revision an answer about its summary.

        Run before claiming work. A revision that reached READY without a job
        being queued -- ingested before this stage existed, or its job lost --
        would otherwise stay at PENDING indefinitely, which is the state
        supplement A exists to eliminate.

        What it does depends on whether this deployment can generate at all:
        queue the work, or record that there will not be any. Both are
        reversible, and both are idempotent -- a revision with an active job is
        never touched twice.
        """
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                revisions = repo.unsummarized_revisions(limit)
                if document_generation_enabled():
                    queued = sum(1 for revision_id in revisions
                                 if repo.enqueue_summarize_job(revision_id) is not None)
                    outcome = {'queued': queued, 'skipped': 0}
                else:
                    skipped = sum(1 for revision_id in revisions
                                  if repo.skip_summary(revision_id))
                    outcome = {'queued': 0, 'skipped': skipped}
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if outcome['queued'] or outcome['skipped']:
            logger.info('summary.reconciled', extra=outcome)
        return outcome

    def process_pending(self, limit: int = 20) -> SummaryRunResult:
        result = SummaryRunResult()

        with self.connection_factory() as conn:
            conn.autocommit = False
            try:
                repo = IngestionRepository(conn)
                recovered = repo.recover_stale_jobs(
                    self.config.job_stale_seconds, job_type='SUMMARIZE'
                )
                if recovered['requeued'] or recovered['failed']:
                    logger.warning('summary.stale_jobs_recovered', extra={
                        'requeued': len(recovered['requeued']),
                        'failed': len(recovered['failed']),
                    })
                jobs = repo.claim_summarize_jobs(limit)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        for job in jobs:
            try:
                self._process_job(job, result)
            except psycopg.OperationalError:
                logger.exception('summary.database_unavailable')
                raise
            except Exception:  # noqa: BLE001 - one revision must not kill the worker
                result.failed += 1
                logger.exception('summary.job_error', extra={'job_id': str(job['id'])})

        logger.info('summary.finish', extra=result.as_dict())
        return result

    # -- one revision ------------------------------------------------------

    def _process_job(self, job: dict, result: SummaryRunResult) -> None:
        revision_id = str(job['document_revision_id'])
        # The attempt number this worker was handed. Everything it writes later
        # is conditional on the job still being on this attempt.
        token = int(job['attempt_count'])

        with self.connection_factory() as conn:
            conn.autocommit = True
            source = IngestionRepository(conn).summary_input(revision_id)

        if source is None:
            logger.warning('summary.revision_missing', extra={'job_id': str(job['id'])})
            self._settle(job, token, revision_id, result,
                         summary_status=None, job_status=STATUS_FAILED,
                         result_code=RESULT_FAILED, message='revision no longer exists')
            return

        if not document_generation_enabled():
            # Supplement A. Leaving this at PENDING would tell every reader
            # that a summary is being written, in a deployment where nothing
            # can write one. SKIPPED is honest and is reversible:
            # resume_skipped_summaries re-opens these if the feature is enabled
            # later.
            self._settle(job, token, revision_id, result,
                         summary_status=STATUS_SKIPPED, job_status=STATUS_SUCCESS,
                         result_code=RESULT_PROVIDER_DISABLED)
            return

        chunks = [
            SummaryChunk(str(row['chunk_index']), row['chunk_index'],
                         row['section_title'], row['text'] or '')
            for row in source['chunks']
        ]
        plan = plan_summary(chunks)
        if not plan.groups:
            self._settle(job, token, revision_id, result,
                         summary_status=STATUS_SKIPPED, job_status=STATUS_SUCCESS,
                         result_code=RESULT_NO_TEXT)
            return

        if plan.oversized:
            # Recorded as skipped rather than failed: the document will be just
            # as large on every retry, so three attempts would only cost three
            # times as much. Deliberately not "summarize the first N groups" --
            # a summary of part of a document, offered as a summary of the
            # document, is worse than none.
            logger.warning('summary.too_large', extra={
                'groups': len(plan.groups), 'max_calls': plan.max_call_count,
            })
            self._settle(job, token, revision_id, result,
                         summary_status=STATUS_SKIPPED, job_status=STATUS_SUCCESS,
                         result_code=RESULT_TOO_LARGE)
            return

        with self.connection_factory() as conn:
            conn.autocommit = True
            IngestionRepository(conn).mark_summary_running(revision_id)

        # Generation happens outside every transaction: it is slow and external,
        # and holding a row lock across it would block the pipeline for as long
        # as the provider takes to answer.
        try:
            summary = self._generate(source['title'], plan, result)
        except Exception as exc:  # noqa: BLE001
            # Type and message only. A provider error string can quote the
            # prompt back, and the prompt contains document text.
            self._settle(job, token, revision_id, result,
                         summary_status=STATUS_FAILED, job_status=STATUS_FAILED,
                         result_code=RESULT_FAILED, message=f'{type(exc).__name__}: {exc}'[:2000])
            return

        if summary is None:
            self._settle(job, token, revision_id, result,
                         summary_status=STATUS_FAILED, job_status=STATUS_FAILED,
                         result_code=RESULT_REFUSED,
                         message='provider produced no grounded summary')
            return

        self._settle(job, token, revision_id, result,
                     summary_status=STATUS_SUCCESS, job_status=STATUS_SUCCESS,
                     result_code=RESULT_SUMMARIZED, summary=summary)

    # -- generation --------------------------------------------------------

    def _evidence(self, title: str, chunks) -> tuple[EvidenceChunk, ...]:
        return tuple(
            EvidenceChunk(document_id='', revision_id='', chunk_id=chunk.chunk_id,
                          title=title, file_type='', section_title=chunk.section_title,
                          anchor={'type': 'none'}, text=chunk.text)
            for chunk in chunks
        )

    def _ask(self, question: str, evidence: tuple[EvidenceChunk, ...], result: SummaryRunResult):
        result.provider_calls += 1
        raw = self.provider.generate(GenerationRequest(
            SUMMARY_SYSTEM_INSTRUCTION, question, serialize_context(evidence),
        ))
        # Same validator as question answering: strict structure, and a citation
        # allow-list so a summary cannot claim to rest on text it was not given.
        answer = validate_generation(raw, evidence)
        return None if answer.refused else answer.answer

    def _generate(self, title: str, plan, result: SummaryRunResult) -> str | None:
        """Reduce the document to one summary, one bounded call at a time.

        Every call at every depth receives at most MAX_GROUP_CHARS, including
        the last. A longer document means more calls and a deeper reduction --
        never a bigger call.
        """
        if not plan.hierarchical:
            answer = self._ask(SINGLE_PASS_QUESTION, self._evidence(title, plan.groups[0]), result)
            return truncate_summary(answer) if answer else None

        # Level 0: the source text itself.
        current: list[SummaryChunk] = []
        for number, group in enumerate(plan.groups, 1):
            answer = self._ask(PARTIAL_QUESTION, self._evidence(title, group), result)
            if answer:
                # Each partial becomes ordinary context for the next level,
                # with its own id, so the same citation allow-list applies at
                # every depth. Truncated here, which is what makes the fan-in a
                # guarantee rather than a hope about model brevity.
                current.append(SummaryChunk(
                    f'part-{number}', number, None,
                    truncate_summary(answer, MAX_PARTIAL_SUMMARY_CHARS),
                ))
        if not current:
            return None

        # Levels 1..n: summaries of summaries, until one group remains. Entered
        # unconditionally so a document that survives as a single partial still
        # gets a document-level statement -- the partial was written under an
        # instruction not to conclude about the whole.
        depth = 1
        while True:
            groups = reduce_level(current)
            final = len(groups) == 1
            if not final and len(groups) >= len(current):
                # Cannot happen while REDUCTION_FAN_IN >= 2, which is asserted
                # at import. Checked anyway: the alternative to noticing here is
                # a background worker looping on a provider's bill.
                logger.error('summary.reduction_stalled', extra={
                    'depth': depth, 'inputs': len(current), 'groups': len(groups),
                })
                return None

            produced: list[SummaryChunk] = []
            for number, group in enumerate(groups, 1):
                question = SYNTHESIS_QUESTION if final else REDUCTION_QUESTION
                answer = self._ask(question, self._evidence(title, group), result)
                if answer is None:
                    continue
                limit = None if final else MAX_PARTIAL_SUMMARY_CHARS
                produced.append(SummaryChunk(
                    f'level{depth}-{number}', number, None,
                    truncate_summary(answer) if limit is None
                    else truncate_summary(answer, limit),
                ))
            if not produced:
                return None
            if final:
                return produced[0].text
            current = produced
            depth += 1

    # -- persistence -------------------------------------------------------

    def _settle(
        self, job, token: int, revision_id: str, result: SummaryRunResult, *,
        summary_status: str | None, job_status: str, result_code: str,
        summary: str | None = None, message: str | None = None,
    ) -> None:
        """Close out the job and the revision together, or neither.

        The job is closed first, with the fencing token. If this attempt has
        been superseded -- it hung, recovery requeued it, another attempt took
        over -- that write matches nothing and the whole transaction is rolled
        back, so a stale summary never reaches the revision. The order matters:
        checking the fence after writing the summary would mean checking it
        after the damage was already done.
        """
        with self.connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            try:
                owned = repo.finish_job(
                    str(job['id']), status=job_status, result_code=result_code,
                    error_message=message, attempt_count=token,
                )
                if not owned:
                    conn.rollback()
                    result.discarded += 1
                    result.record(RESULT_SUPERSEDED)
                    logger.warning('summary.attempt_superseded', extra={
                        'job_id': str(job['id']), 'attempt': token,
                    })
                    return
                if summary_status is not None:
                    repo.save_summary_result(
                        revision_id=revision_id, status=summary_status, summary=summary,
                        provider=self.provider.identifier if summary else None,
                        model=getattr(self.provider, 'model', None) if summary else None,
                        prompt_version=SUMMARY_PROMPT_VERSION if summary else None,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result.processed += 1
        result.record(result_code)
        if job_status == STATUS_FAILED:
            result.failed += 1
        elif summary_status == STATUS_SKIPPED:
            result.skipped += 1
        else:
            result.succeeded += 1

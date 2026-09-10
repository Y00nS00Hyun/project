"""Reclaiming jobs a dead worker left in RUNNING.

Real PostgreSQL: the recovery is a locked read-modify-write over the same rows
the claim queries take, and the interaction between the two is the whole point.

The failure this prevents was observed for real -- a parser crash on a 9MB HWP
left its PARSE job in RUNNING, and re-running the worker processed nothing at
all, because every query in the system looks only at PENDING rows.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from support import pgtest

from ingestion.repository import IngestionRepository


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for these DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def stale_db(server) -> str:
    return pgtest.migrated_database(server, "stale_job_test")


@pytest.fixture
def dsn(stale_db) -> str:
    url = pgtest.psycopg_url(stale_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def repo(conn):
    return IngestionRepository(conn)


def make_revision(repo, name: str = "doc.hwp") -> str:
    document_id = repo.create_document(
        title=name.rsplit(".", 1)[0],
        original_filename=name,
        source_path=name,
        file_type="hwp",
    )
    return repo.create_revision(
        document_id=document_id,
        content_hash="a" * 64,
        file_size=1,
        source_mtime=None,
        source_path_at_ingest=name,
        document_year=None,
    )


def age_job(conn, job_id: str, seconds: int) -> None:
    """Simulate time passing since the worker claimed the job."""
    conn.execute(
        "UPDATE processing_jobs SET started_at = now() - make_interval(secs => %s) WHERE id = %s",
        (seconds, job_id),
    )


def job_row(conn, job_id: str) -> dict:
    row = conn.execute(
        "SELECT status, attempt_count, max_attempts, started_at, error_message "
        "FROM processing_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    return {
        "status": row[0], "attempt_count": row[1], "max_attempts": row[2],
        "started_at": row[3], "error_message": row[4],
    }


class TestCrashLeavesJobStranded:
    def test_a_running_job_is_invisible_to_the_claim_query(self, repo, conn):
        """The bug being fixed, pinned so it cannot come back quietly."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        claimed = repo.claim_parse_jobs(10)
        assert len(claimed) == 1

        # Worker dies here: the row stays RUNNING and nothing finishes it.
        assert repo.claim_parse_jobs(10) == []
        assert job_row(conn, str(claimed[0]["id"]))["status"] == "RUNNING"


class TestRecovery:
    def test_a_stale_job_goes_back_to_the_queue(self, repo, conn):
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        age_job(conn, job_id, 1000)

        recovered = repo.recover_stale_jobs(900)
        assert recovered["requeued"] == [job_id]
        assert recovered["failed"] == []

        row = job_row(conn, job_id)
        assert row["status"] == "PENDING"
        assert row["started_at"] is None
        # The next worker can now pick it up, which is the whole point.
        assert len(repo.claim_parse_jobs(10)) == 1

    def test_a_job_still_within_the_timeout_is_left_alone(self, repo, conn):
        """Requeuing a merely slow job would let two workers write one revision."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        age_job(conn, job_id, 100)

        assert repo.recover_stale_jobs(900) == {"requeued": [], "failed": []}
        assert job_row(conn, job_id)["status"] == "RUNNING"

    def test_attempts_are_not_consumed_twice_by_recovery(self, repo, conn):
        """Recovery resets the row; the *claim* is what counts an attempt."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        assert job_row(conn, job_id)["attempt_count"] == 1

        age_job(conn, job_id, 1000)
        repo.recover_stale_jobs(900)
        assert job_row(conn, job_id)["attempt_count"] == 1

    def test_a_job_out_of_attempts_becomes_failed(self, repo, conn):
        """A document that crashes the worker every time must not loop forever."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        conn.execute(
            "UPDATE processing_jobs SET attempt_count = max_attempts WHERE id = %s",
            (job_id,),
        )
        age_job(conn, job_id, 1000)

        recovered = repo.recover_stale_jobs(900)
        assert recovered["failed"] == [job_id]
        assert recovered["requeued"] == []

        row = job_row(conn, job_id)
        assert row["status"] == "FAILED"
        assert row["error_message"]
        # And it stays out of the queue.
        assert repo.claim_parse_jobs(10) == []

    def test_repeated_crashes_terminate(self, repo, conn):
        """crash -> recover -> claim, until the attempts run out."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)

        statuses = []
        for _ in range(6):
            claimed = repo.claim_parse_jobs(10)
            if claimed:
                age_job(conn, str(claimed[0]["id"]), 1000)  # crash
            repo.recover_stale_jobs(900)
            statuses.append(
                conn.execute("SELECT status FROM processing_jobs").fetchone()[0]
            )

        assert statuses[-1] == "FAILED", statuses
        row = conn.execute(
            "SELECT attempt_count, max_attempts FROM processing_jobs"
        ).fetchone()
        assert row[0] >= row[1]

    def test_a_job_that_never_started_is_untouched(self, repo, conn):
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        assert repo.recover_stale_jobs(1) == {"requeued": [], "failed": []}
        assert job_row(conn, conn.execute(
            "SELECT id FROM processing_jobs"
        ).fetchone()[0])["status"] == "PENDING"


class TestAllJobTypes:
    def test_embed_jobs_are_recovered_too(self, repo, conn):
        """PARSE and EMBED share this table and the same claim pattern."""
        revision_id = make_revision(repo)
        conn.execute(
            "UPDATE document_revisions SET parse_status='SUCCESS', "
            "parse_result_code='TEXT_EXTRACTED' WHERE id = %s",
            (revision_id,),
        )
        repo.enqueue_embed_job(revision_id)
        job_id = str(repo.claim_embed_jobs(10)[0]["id"])
        age_job(conn, job_id, 1000)

        assert repo.recover_stale_jobs(900)["requeued"] == [job_id]
        assert len(repo.claim_embed_jobs(10)) == 1

    def test_recovery_can_be_scoped_to_one_job_type(self, repo, conn):
        parse_rev = make_revision(repo, "a.hwp")
        repo.enqueue_parse_job(parse_rev)
        parse_job = str(repo.claim_parse_jobs(10)[0]["id"])

        embed_rev = make_revision(repo, "b.hwp")
        conn.execute(
            "UPDATE document_revisions SET parse_status='SUCCESS', "
            "parse_result_code='TEXT_EXTRACTED' WHERE id = %s",
            (embed_rev,),
        )
        repo.enqueue_embed_job(embed_rev)
        embed_job = str(repo.claim_embed_jobs(10)[0]["id"])

        age_job(conn, parse_job, 1000)
        age_job(conn, embed_job, 1000)

        assert repo.recover_stale_jobs(900, job_type="PARSE")["requeued"] == [parse_job]
        assert job_row(conn, embed_job)["status"] == "RUNNING"

    def test_recovering_everything_takes_both(self, repo, conn):
        parse_rev = make_revision(repo, "a.hwp")
        repo.enqueue_parse_job(parse_rev)
        age_job(conn, str(repo.claim_parse_jobs(10)[0]["id"]), 1000)

        embed_rev = make_revision(repo, "b.hwp")
        conn.execute(
            "UPDATE document_revisions SET parse_status='SUCCESS', "
            "parse_result_code='TEXT_EXTRACTED' WHERE id = %s",
            (embed_rev,),
        )
        repo.enqueue_embed_job(embed_rev)
        age_job(conn, str(repo.claim_embed_jobs(10)[0]["id"]), 1000)

        assert len(repo.recover_stale_jobs(900)["requeued"]) == 2


class TestConcurrency:
    def test_recovery_does_not_steal_a_row_a_live_worker_holds(self, dsn, repo, conn):
        """SKIP LOCKED: a worker's open transaction protects its own row."""
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        age_job(conn, job_id, 1000)

        with psycopg.connect(dsn) as worker:
            worker.autocommit = False
            worker.execute("SELECT 1 FROM processing_jobs WHERE id = %s FOR UPDATE", (job_id,))
            # Recovery must skip the locked row rather than block on it.
            assert repo.recover_stale_jobs(900) == {"requeued": [], "failed": []}
            worker.rollback()

        assert repo.recover_stale_jobs(900)["requeued"] == [job_id]

    def test_two_recoveries_do_not_both_requeue_the_same_job(self, dsn, repo, conn):
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        job_id = str(repo.claim_parse_jobs(10)[0]["id"])
        age_job(conn, job_id, 1000)

        first = repo.recover_stale_jobs(900)
        second = repo.recover_stale_jobs(900)
        assert first["requeued"] == [job_id]
        assert second == {"requeued": [], "failed": []}

    def test_a_recovered_job_is_claimed_by_exactly_one_worker(self, dsn, repo, conn):
        revision_id = make_revision(repo)
        repo.enqueue_parse_job(revision_id)
        age_job(conn, str(repo.claim_parse_jobs(10)[0]["id"]), 1000)
        repo.recover_stale_jobs(900)

        with psycopg.connect(dsn) as a, psycopg.connect(dsn) as b:
            a.autocommit = False
            b.autocommit = False
            first = IngestionRepository(a).claim_parse_jobs(10)
            second = IngestionRepository(b).claim_parse_jobs(10)
            a.commit()
            b.commit()
        assert len(first) + len(second) == 1

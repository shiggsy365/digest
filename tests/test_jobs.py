from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from digest.db import Base
from digest.jobs import claim, complete, enqueue, recover_stale, retry_or_fail
from digest.models import JobStatus, now


def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_job_can_be_enqueued_claimed_and_completed() -> None:
    with session() as db:
        queued = enqueue(db, "library_scan")

        claimed = claim(db, "test-worker")

        assert claimed is not None
        assert claimed.id == queued.id
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempts == 1
        assert claimed.claimed_by == "test-worker"

        complete(db, claimed)
        assert claimed.status == JobStatus.COMPLETE
        assert claim(db, "test-worker") is None


def test_failed_job_retries_with_backoff_then_stops() -> None:
    with session() as db:
        enqueue(db, "library_scan", max_attempts=2)
        claimed = claim(db, "test-worker")
        assert claimed is not None

        retry_or_fail(db, claimed, RuntimeError("temporary failure"))
        assert claimed.status == JobStatus.QUEUED
        assert claimed.run_after > now()
        assert claimed.last_error == "RuntimeError: temporary failure"

        claimed.run_after = now() - timedelta(seconds=1)
        db.commit()
        claimed_again = claim(db, "test-worker")
        assert claimed_again is not None
        retry_or_fail(db, claimed_again, RuntimeError("permanent failure"))

        assert claimed_again.status == JobStatus.FAILED
        assert claimed_again.attempts == 2


def test_future_job_is_not_claimed_early() -> None:
    with session() as db:
        enqueue(db, "library_scan", run_after=now() + timedelta(hours=1))
        assert claim(db, "test-worker") is None


def test_stale_running_job_is_recovered() -> None:
    with session() as db:
        enqueue(db, "library_scan")
        job = claim(db, "dead-worker")
        assert job is not None
        job.claimed_at = now() - timedelta(hours=1)
        db.commit()

        assert recover_stale(db) == 1
        assert job.status == JobStatus.QUEUED
        assert job.claimed_by is None
        assert job.last_error == "Recovered after stale worker claim"

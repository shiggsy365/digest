from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Job, JobStatus, now


def enqueue(
    db: Session,
    kind: str,
    *,
    payload_json: str = "{}",
    run_after=None,
    max_attempts: int = 5,
) -> Job:
    job = Job(
        kind=kind,
        payload_json=payload_json,
        run_after=run_after or now(),
        max_attempts=max_attempts,
    )
    db.add(job)
    db.commit()
    return job


def claim(db: Session, worker_id: str) -> Job | None:
    job = db.scalar(
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now())
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.claimed_at = now()
    job.claimed_by = worker_id
    job.attempts += 1
    db.commit()
    return job


def complete(db: Session, job: Job) -> None:
    job.status = JobStatus.COMPLETE
    job.claimed_at = None
    job.claimed_by = None
    job.last_error = None
    db.commit()


def retry_or_fail(db: Session, job: Job, error: Exception) -> None:
    job.last_error = f"{type(error).__name__}: {error}"[:4000]
    job.claimed_at = None
    job.claimed_by = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.QUEUED
        delay_seconds = min(30 * (2 ** (job.attempts - 1)), 3600)
        job.run_after = now() + timedelta(seconds=delay_seconds)
    db.commit()


def recover_stale(db: Session, *, older_than: timedelta = timedelta(minutes=30)) -> int:
    cutoff = now() - older_than
    result = db.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.claimed_at < cutoff)
        .values(
            status=JobStatus.QUEUED,
            claimed_at=None,
            claimed_by=None,
            run_after=now(),
            last_error="Recovered after stale worker claim",
        )
    )
    db.commit()
    return result.rowcount

"""A job queue in Postgres.

Every function takes a session and a time and never commits, so the caller decides what belongs
in one transaction and a test can move the clock. The rules:

* A queued job is claimed by one worker (``FOR UPDATE SKIP LOCKED``) and given a lease.
* The worker extends the lease while it works. A job whose lease has run out is put back in the
  queue by whichever worker looks next, or failed if it has used its attempts.
* Only the holder of the lease can finish a job. A worker that lost its lease finds out when it
  tries, and its result is dropped.
* A failed attempt waits before the next one, longer each time, up to a cap.
* Two requests for the same thing while it is still waiting are one job. A request made while it
  is running is a new job, because the running one may already have read its input.
* A queued job does not start while a job with the same key is running.

Times come from the caller. Workers on different hosts must keep their clocks within a small
fraction of the lease for the lease to mean what it says.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from recon.persistence.models import Job

ERROR_LIMIT = 2000


class UnknownJobError(LookupError):
    """There is no job with that id."""


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def backoff_seconds(
    attempts: int, *, base: float = 5.0, cap: float = 300.0, rng: random.Random | None = None
) -> float:
    """How long to wait after the ``attempts``-th failed attempt: doubling from ``base`` to
    ``cap``, plus up to a fifth more so that jobs that failed together do not return together."""
    delay = min(cap, base * float(2 ** max(0, attempts - 1)))
    return delay * (1 + 0.2 * (rng or random.Random()).random())  # noqa: S311


def enqueue(
    session: Session,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    *,
    now: datetime,
    max_attempts: int = 5,
) -> tuple[Job, bool]:
    """Queue a job, or return the one already waiting for the same key. The flag is True when a
    new job was made."""
    inserted = session.execute(
        insert(Job)
        .values(
            kind=kind,
            dedupe_key=dedupe_key,
            payload=payload,
            max_attempts=max_attempts,
            run_after=now,
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["kind", "dedupe_key"], index_where=text("status = 'queued'")
        )
        .returning(Job.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return session.get_one(Job, inserted), True
    waiting = session.scalars(
        select(Job).where(Job.kind == kind, Job.dedupe_key == dedupe_key, Job.status == "queued")
    ).one()
    return waiting, False


_CLAIM = text(
    """
    WITH next AS (
        SELECT j.id FROM job j
        WHERE j.status = 'queued' AND j.run_after <= :now
          AND NOT EXISTS (
              SELECT 1 FROM job r
              WHERE r.status = 'running' AND r.kind = j.kind AND r.dedupe_key = j.dedupe_key
          )
        ORDER BY j.run_after, j.created_at
        FOR UPDATE OF j SKIP LOCKED
        LIMIT 1
    )
    UPDATE job SET status = 'running', attempts = attempts + 1, locked_by = :worker,
                   lease_expires_at = :lease_until, heartbeat_at = :now, started_at = :now,
                   last_error = NULL
    FROM next WHERE job.id = next.id
    RETURNING job.id, job.kind, job.payload, job.attempts, job.max_attempts
    """
)


def claim(session: Session, worker: str, *, now: datetime, lease: timedelta) -> Claimed | None:
    """Take the next job that is due, or None. It counts as an attempt."""
    row = (
        session.execute(_CLAIM, {"now": now, "worker": worker, "lease_until": now + lease})
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return Claimed(row["id"], row["kind"], row["payload"], row["attempts"], row["max_attempts"])


def heartbeat(
    session: Session, job_id: uuid.UUID, worker: str, *, now: datetime, lease: timedelta
) -> bool:
    """Extend the lease. False means the worker no longer holds the job."""
    return (
        session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker)
            .values(lease_expires_at=now + lease, heartbeat_at=now)
            .returning(Job.id)
        ).scalar_one_or_none()
        is not None
    )


def complete(
    session: Session, job_id: uuid.UUID, worker: str, result: dict[str, Any], *, now: datetime
) -> bool:
    """Record success. False means the worker had lost the job and the result is dropped."""
    return (
        session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker)
            .values(
                status="succeeded",
                result=result,
                finished_at=now,
                locked_by=None,
                lease_expires_at=None,
                last_error=None,
            )
            .returning(Job.id)
        ).scalar_one_or_none()
        is not None
    )


def fail(
    session: Session,
    job_id: uuid.UUID,
    worker: str,
    error: str,
    *,
    now: datetime,
    retry: bool = True,
    backoff: timedelta | None = None,
) -> str | None:
    """Record a failed attempt. The job is queued again after ``backoff`` unless it has used
    its attempts or ``retry`` is False. Returns the new status, or None if the job was lost."""
    job = session.scalars(
        select(Job)
        .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker)
        .with_for_update()
    ).one_or_none()
    if job is None:
        return None
    return _release(job, error, now=now, retry=retry, backoff=backoff or timedelta())


def _release(job: Job, error: str, *, now: datetime, retry: bool, backoff: timedelta) -> str:
    job.last_error = error[:ERROR_LIMIT]
    job.locked_by = None
    job.lease_expires_at = None
    if retry and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = now + backoff
    else:
        job.status = "failed"
        job.finished_at = now
    return job.status


def recover_expired(session: Session, *, now: datetime, backoff: timedelta = timedelta()) -> int:
    """Put back the jobs whose worker stopped extending the lease, or fail those out of attempts.
    Returns how many were recovered."""
    expired = session.scalars(
        select(Job)
        .where(Job.status == "running", Job.lease_expires_at < now)
        .order_by(Job.created_at)
        .with_for_update(skip_locked=True)
    ).all()
    for job in expired:
        _release(
            job,
            f"the worker {job.locked_by} stopped answering (attempt {job.attempts})",
            now=now,
            retry=True,
            backoff=backoff,
        )
    return len(expired)

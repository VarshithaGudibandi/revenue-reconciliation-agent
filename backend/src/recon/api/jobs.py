"""Starting reconciliation from a request: now, or by handing it to a worker."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from recon.api.schemas import JobOut, RunOut, SyncOut
from recon.config import Settings
from recon.jobs import queue
from recon.jobs.handlers import RECONCILE_PERIOD, reconcile_key, reconcile_payload
from recon.persistence.models import Job
from recon.pipeline import PeriodOutcome, reconcile_period


@dataclass(frozen=True)
class Dispatched:
    """What a request can say about the reconciliation it asked for: the result if it ran in the
    request, or the job that will produce it."""

    run: RunOut | None
    cases: SyncOut | None
    job: JobOut | None


def outcome_out(outcome: PeriodOutcome) -> tuple[RunOut, SyncOut | None]:
    result = outcome.as_result()
    return RunOut.model_validate(result["run"]), (
        None if result["cases"] is None else SyncOut.model_validate(result["cases"])
    )


def job_out(job: Job) -> JobOut:
    result = job.result or {}
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
        created_at=job.created_at,
        finished_at=job.finished_at,
        run=RunOut.model_validate(result["run"]) if result.get("run") else None,
        cases=SyncOut.model_validate(result["cases"]) if result.get("cases") else None,
    )


def get_job(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise queue.UnknownJobError(f"no job {job_id}")
    return job


def dispatch_reconcile(
    sessions: sessionmaker[Session], settings: Settings, profile_id: uuid.UUID, period: str
) -> Dispatched:
    """Reconcile a period inside the request, or queue the work and return the job."""
    if settings.jobs == "inline":
        run, cases = outcome_out(reconcile_period(sessions, profile_id, period))
        return Dispatched(run, cases, None)
    with sessions.begin() as session:
        job, _ = queue.enqueue(
            session,
            RECONCILE_PERIOD,
            reconcile_key(profile_id, period),
            reconcile_payload(profile_id, period),
            now=datetime.now(UTC),
            max_attempts=settings.job_max_attempts,
        )
        return Dispatched(None, None, job_out(job))

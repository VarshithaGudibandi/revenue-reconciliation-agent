"""Deleting what is finished with, so that nothing grows without limit.

* Finished jobs (succeeded or failed) once they are old enough. A scan or an alert job is queued
  every few minutes, so these would otherwise be the biggest table.
* Mapping proposals that were never confirmed, together with the sample files kept for them.
* Idempotency records. A request repeated with an old key after this is applied again, so keys
  must not be reused over longer than the retention.
* Alert deliveries. What stops a retried job from sending twice only matters while it can retry.

Reconciliation results, cases, file versions and the log of ingested objects are never deleted
here: they are the history, and the log of ingested objects is what stops a file being read again.
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Delete, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.persistence.models import (
    AlertDelivery,
    IdempotencyRecord,
    Job,
    MappingProposalRow,
)


def _deleted(session: Session, statement: Delete) -> int:
    result = session.execute(statement)
    return int(result.rowcount) if isinstance(result, CursorResult) else 0


def purge(
    sessions: sessionmaker[Session], storage_dir: Path, settings: Settings, *, now: datetime
) -> dict[str, int]:
    """Delete what is older than its retention. Returns how many of each were deleted."""

    def before(days: int) -> datetime:
        return now - timedelta(days=days)

    with sessions.begin() as session:
        jobs = _deleted(
            session,
            delete(Job).where(
                Job.status.in_(("succeeded", "failed")),
                Job.finished_at < before(settings.retention_jobs_days),
            ),
        )
        keys = _deleted(
            session,
            delete(IdempotencyRecord).where(
                IdempotencyRecord.created_at < before(settings.retention_idempotency_days)
            ),
        )
        deliveries = _deleted(
            session,
            delete(AlertDelivery).where(
                AlertDelivery.status != "sending",
                AlertDelivery.created_at < before(settings.retention_deliveries_days),
            ),
        )
        stale = session.execute(
            select(MappingProposalRow.id, MappingProposalRow.invoices_key).where(
                MappingProposalRow.status == "proposed",
                MappingProposalRow.created_at < before(settings.retention_proposals_days),
            )
        ).all()
        if stale:
            session.execute(
                delete(MappingProposalRow).where(MappingProposalRow.id.in_([i for i, _ in stale]))
            )
    # Files go after the rows, so a failure leaves files without rows, which is harmless, and
    # never rows without files.
    for _, key in stale:
        shutil.rmtree((storage_dir / key).parent, ignore_errors=True)
    return {
        "jobs": jobs,
        "idempotency_records": keys,
        "alert_deliveries": deliveries,
        "proposals": len(stale),
    }

"""The kinds of job there are, and how to queue them."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from recon.jobs import queue
from recon.persistence.models import Job

RECONCILE_PERIOD = "reconcile_period"
SCAN_BUCKET = "scan_bucket"


def reconcile_key(profile_id: uuid.UUID, period: str) -> str:
    return f"{profile_id}/{period}"


def reconcile_payload(profile_id: uuid.UUID, period: str) -> dict[str, Any]:
    return {"profile_id": str(profile_id), "period": period}


def enqueue_reconcile(
    session: Session, profile_id: uuid.UUID, period: str, *, now: datetime, max_attempts: int = 5
) -> Job:
    """Queue the reconciliation of a period, or join the one already waiting."""
    job, _ = queue.enqueue(
        session,
        RECONCILE_PERIOD,
        reconcile_key(profile_id, period),
        reconcile_payload(profile_id, period),
        now=now,
        max_attempts=max_attempts,
    )
    return job

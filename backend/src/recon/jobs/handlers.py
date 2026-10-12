"""What each kind of job does."""

import uuid
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from recon.jobs.worker import Handler, Permanent, Retry
from recon.persistence.repository import check_period
from recon.pipeline import reconcile_period

RECONCILE_PERIOD = "reconcile_period"


def reconcile_key(profile_id: uuid.UUID, period: str) -> str:
    return f"{profile_id}/{period}"


def reconcile_payload(profile_id: uuid.UUID, period: str) -> dict[str, Any]:
    return {"profile_id": str(profile_id), "period": period}


def handle_reconcile(sessions: sessionmaker[Session]) -> Handler:
    """Reconcile the latest files of a period and bring its cases up to date."""

    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            profile_id = uuid.UUID(payload["profile_id"])
            period = str(payload["period"])
            check_period(period)
        except (KeyError, ValueError) as error:
            raise Permanent(f"the job has no usable profile and period: {error}") from error
        outcome = reconcile_period(sessions, profile_id, period)
        if outcome.run.status != "succeeded":
            raise Retry(f"the run is {outcome.run.status}, not finished")
        return outcome.as_result()

    return handle


def handlers_for(sessions: sessionmaker[Session]) -> dict[str, Handler]:
    return {RECONCILE_PERIOD: handle_reconcile(sessions)}

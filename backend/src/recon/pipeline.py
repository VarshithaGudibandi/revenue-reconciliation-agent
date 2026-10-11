"""The whole path for one period: run reconciliation on the latest files, then update cases."""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from recon.persistence.cases import SyncSummary, sync_cases
from recon.persistence.runs import RunSummary, get_or_create_run, run_reconciliation


@dataclass(frozen=True)
class PeriodOutcome:
    run: RunSummary
    cases: SyncSummary | None


def reconcile_period(
    sessions: sessionmaker[Session],
    profile_id: uuid.UUID,
    period: str,
    *,
    mode: Literal["auto", "full"] = "auto",
    verify: bool = False,
) -> PeriodOutcome:
    """Reconcile the latest files of a period and bring its cases up to date.

    Safe to call repeatedly and from several workers. If the run is still being
    executed elsewhere, or failed, the cases are left alone and ``cases`` is ``None``.
    """
    with sessions.begin() as session:
        run_id = get_or_create_run(session, profile_id=profile_id, period=period).run.id
    summary = run_reconciliation(sessions, run_id, mode=mode, verify=verify)
    if summary.status != "succeeded":
        return PeriodOutcome(summary, None)
    with sessions.begin() as session:
        return PeriodOutcome(summary, sync_cases(session, profile_id, period))

"""The whole path for one period: run reconciliation on the latest files, then update cases."""

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session, sessionmaker

from recon.persistence.cases import SyncSummary, sync_cases
from recon.persistence.runs import RunSummary, get_or_create_run, run_reconciliation


@dataclass(frozen=True)
class PeriodOutcome:
    run: RunSummary
    cases: SyncSummary | None

    def as_result(self) -> dict[str, Any]:
        """The outcome as JSON, for a job to keep."""
        run = self.run
        cases = self.cases
        return {
            "run": {
                "id": str(run.run_id),
                "status": run.status,
                "mode": run.mode,
                "partitions_total": run.partitions_total,
                "partitions_recomputed": run.partitions_recomputed,
                "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
            },
            "cases": None
            if cases is None
            else {
                "applied": cases.applied,
                "opened": cases.opened,
                "updated": cases.updated,
                "reopened": cases.reopened,
                "resolved": cases.resolved,
                "withdrawn": cases.withdrawn,
                "fix_not_verified": cases.fix_not_verified,
            },
        }


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

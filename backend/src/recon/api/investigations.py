"""Investigating a case with a model, and deciding on what it found."""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from recon.ai.investigation import service
from recon.ai.investigation.service import Investigator, real_text
from recon.api.routes import ERROR_RESPONSES
from recon.api.schemas import (
    FindingOut,
    InvestigationDecisionIn,
    InvestigationOut,
    InvestigationStepOut,
)
from recon.config import Settings
from recon.persistence.models import CaseInvestigation

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)


def _sessions(request: Request) -> sessionmaker[Session]:
    sessions: sessionmaker[Session] = request.app.state.sessions
    return sessions


def _investigator(request: Request) -> Investigator:
    investigator: Investigator = request.app.state.investigator
    return investigator


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


Sessions = Annotated[sessionmaker[Session], Depends(_sessions)]
InvestigatorDep = Annotated[Investigator, Depends(_investigator)]
SettingsDep = Annotated[Settings, Depends(_settings)]


def _real(value: Any, aliases: dict[str, list[str]]) -> Any:
    """A JSON value with every alias in it replaced by the real id."""
    return json.loads(real_text(json.dumps(value, ensure_ascii=False), aliases))


def investigation_out(row: CaseInvestigation) -> InvestigationOut:
    finding = None if row.finding is None else FindingOut(**_real(row.finding, row.aliases))
    return InvestigationOut(
        id=row.id,
        case_id=row.case_id,
        status=row.status,
        model=row.model,
        finding=finding,
        problems=row.problems,
        trace=[InvestigationStepOut(**_real(step, row.aliases)) for step in row.trace],
        error=row.error,
        outcome=row.outcome,
        decision_note=row.decision_note,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


def _read(sessions: sessionmaker[Session], investigation_id: uuid.UUID) -> InvestigationOut:
    with sessions() as session:
        row = session.get(CaseInvestigation, investigation_id)
        if row is None:
            raise service.DecisionError("there is no such investigation")
        return investigation_out(row)


@router.post("/cases/{case_id}/investigation", response_model=InvestigationOut)
def investigate_case(
    case_id: uuid.UUID, sessions: Sessions, investigator: InvestigatorDep, settings: SettingsDep
) -> InvestigationOut:
    """Ask a language model to investigate why the case arose.

    It reads the month the case is in with read-only tools and proposes a cause, the records it
    rests on and an action. Nothing about the case changes: a finding that passes its checks
    waits for a person's decision, and one that fails them is not shown.
    """
    found = service.start(sessions, case_id, investigator, settings.database_url)
    return _read(sessions, found)


@router.get("/cases/{case_id}/investigation", response_model=InvestigationOut | None)
def latest_investigation(case_id: uuid.UUID, sessions: Sessions) -> InvestigationOut | None:
    with sessions() as session:
        row = service.latest(session, case_id)
        return None if row is None else investigation_out(row)


@router.post("/investigations/{investigation_id}/decision", response_model=InvestigationOut)
def decide_investigation(
    investigation_id: uuid.UUID,
    body: InvestigationDecisionIn,
    sessions: Sessions,
    settings: SettingsDep,
) -> InvestigationOut:
    """Approve or reject what an investigation found. Approving adds the finding to the case's
    notes, and for a cost the business bears (a fee, a discount, a bank charge) accepts the
    variance; the case still closes only when the data says so. Deciding again the same way
    changes nothing."""
    service.decide(
        sessions,
        investigation_id,
        approve=body.approve,
        note=body.note,
        expected_version=body.expected_version,
        database_url=settings.database_url,
    )
    return _read(sessions, investigation_id)

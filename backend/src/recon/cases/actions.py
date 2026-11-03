"""What a person can do with a case, and how to read one.

Every action locks the case row, so concurrent actions and a sync take turns. Actions
that change a case's state take the version the caller last saw and refuse to act on a
newer one. An action whose result already holds is a no-op, so retrying a request that
did succeed is safe.

A person can report a fix, accept a variance, reopen an accepted one or add notes. They
cannot resolve a case: only the data can do that, in ``sync_cases``.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recon.persistence.models import CaseEvent, CaseNote, CaseRecord, CaseSync, ExceptionCase


class AcceptReason(StrEnum):
    TIMING_DIFFERENCE = "timing_difference"
    ROUNDING_DIFFERENCE = "rounding_difference"
    WRITE_OFF = "write_off"
    KNOWN_DATA_ISSUE = "known_data_issue"
    OTHER = "other"


class CaseNotFoundError(LookupError):
    pass


class StaleCaseError(RuntimeError):
    """The case changed since the caller last saw it."""

    def __init__(self, case_id: uuid.UUID, expected: int, actual: int) -> None:
        super().__init__(f"case {case_id} is at version {actual}, not {expected}")
        self.case_id, self.expected, self.actual = case_id, expected, actual


class InvalidTransitionError(ValueError):
    pass


def _lock(session: Session, case_id: uuid.UUID) -> ExceptionCase:
    case = session.scalars(
        select(ExceptionCase).where(ExceptionCase.id == case_id).with_for_update()
    ).first()
    if case is None:
        raise CaseNotFoundError(f"no case {case_id}")
    return case


def _check_version(case: ExceptionCase, expected_version: int | None) -> None:
    if expected_version is not None and case.version != expected_version:
        raise StaleCaseError(case.id, expected_version, case.version)


def _current_run(session: Session, case: ExceptionCase) -> uuid.UUID | None:
    return session.scalar(
        select(CaseSync.run_id).where(
            CaseSync.profile_id == case.profile_id, CaseSync.period == case.period
        )
    )


def _record(
    session: Session,
    case: ExceptionCase,
    event: str,
    from_status: str,
    to_status: str,
    **detail: object,
) -> None:
    case.version += 1
    case.updated_at = func.now()
    session.add(
        CaseEvent(
            case_id=case.id,
            kind=event,
            from_status=from_status,
            to_status=to_status,
            run_id=_current_run(session, case),
            detail={"by": "user", **detail},
        )
    )
    session.flush()


def _require_note(note: str) -> str:
    body = note.strip()
    if not body:
        raise ValueError("a note is required")
    return body


def report_fix(
    session: Session, case_id: uuid.UUID, *, expected_version: int | None = None
) -> ExceptionCase:
    """Say the problem was fixed in the source system and wait for the data to show it.

    The next sync with new data resolves the case if the records now reconcile, and
    reopens it, flagged, if they do not.
    """
    case = _lock(session, case_id)
    if case.status == "fix_reported":
        return case
    _check_version(case, expected_version)
    if case.status != "open":
        raise InvalidTransitionError(f"a {case.status} case cannot have a fix reported")

    # Read before changing anything: a query flushes pending changes, and the case must
    # never be written half-updated.
    reported_against = _current_run(session, case)
    if reported_against is None:
        raise InvalidTransitionError("the period has not been reconciled yet")
    case.status = "fix_reported"
    case.fix_unverified = False
    case.fix_reported_run_id = reported_against
    _record(session, case, "fix_reported", "open", "fix_reported")
    return case


def retract_fix(
    session: Session, case_id: uuid.UUID, *, expected_version: int | None = None
) -> ExceptionCase:
    case = _lock(session, case_id)
    if case.status == "open":
        return case
    _check_version(case, expected_version)
    if case.status != "fix_reported":
        raise InvalidTransitionError(f"a {case.status} case has no reported fix to retract")

    case.status = "open"
    case.fix_reported_run_id = None
    _record(session, case, "fix_retracted", "fix_reported", "open")
    return case


def accept_variance(
    session: Session,
    case_id: uuid.UUID,
    *,
    reason: AcceptReason,
    note: str,
    expected_version: int | None = None,
) -> ExceptionCase:
    """Accept a difference that is real. The case comes back if its data changes."""
    body = _require_note(note)
    case = _lock(session, case_id)
    if case.status == "accepted" and case.accepted_reason == reason.value:
        return case
    _check_version(case, expected_version)
    if case.status not in ("open", "fix_reported"):
        raise InvalidTransitionError(f"a {case.status} case cannot be accepted")

    previous = case.status
    case.status = "accepted"
    case.accepted_reason = reason.value
    case.accepted_signature = case.signature
    case.fix_reported_run_id = None
    case.fix_unverified = False
    session.add(CaseNote(case_id=case.id, body=body))
    _record(session, case, "accepted", previous, "accepted", reason=reason.value)
    return case


def reopen_accepted(
    session: Session, case_id: uuid.UUID, *, expected_version: int | None = None
) -> ExceptionCase:
    case = _lock(session, case_id)
    if case.status == "open":
        return case
    _check_version(case, expected_version)
    if case.status != "accepted":
        raise InvalidTransitionError(f"a {case.status} case is not accepted")

    case.status = "open"
    case.accepted_reason = None
    case.accepted_signature = None
    _record(session, case, "reopened_by_user", "accepted", "open")
    return case


def add_note(session: Session, case_id: uuid.UUID, body: str) -> CaseNote:
    """Notes can be added in any status and do not change the case's version."""
    text = _require_note(body)
    case = _lock(session, case_id)
    note = CaseNote(case_id=case.id, body=text)
    session.add(note)
    session.flush()
    return note


def list_cases(
    session: Session,
    profile_id: uuid.UUID,
    period: str,
    statuses: tuple[str, ...] | None = None,
) -> list[ExceptionCase]:
    query = (
        select(ExceptionCase)
        .where(ExceptionCase.profile_id == profile_id, ExceptionCase.period == period)
        .order_by(ExceptionCase.number)
    )
    if statuses is not None:
        query = query.where(ExceptionCase.status.in_(statuses))
    return list(session.scalars(query))


@dataclass(frozen=True)
class CaseDetail:
    case: ExceptionCase
    records: tuple[tuple[str, str], ...]
    events: tuple[CaseEvent, ...]
    notes: tuple[CaseNote, ...]


def get_case_detail(session: Session, case_id: uuid.UUID) -> CaseDetail:
    case = session.get(ExceptionCase, case_id)
    if case is None:
        raise CaseNotFoundError(f"no case {case_id}")
    records = session.execute(
        select(CaseRecord.role, CaseRecord.record_id)
        .where(CaseRecord.case_id == case_id)
        .order_by(CaseRecord.role, CaseRecord.record_id)
    ).all()
    events = session.scalars(
        select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.id)
    )
    notes = session.scalars(
        select(CaseNote).where(CaseNote.case_id == case_id).order_by(CaseNote.id)
    )
    return CaseDetail(
        case,
        tuple((role, record_id) for role, record_id in records),
        tuple(events),
        tuple(notes),
    )

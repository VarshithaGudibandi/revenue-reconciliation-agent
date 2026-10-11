import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from recon.persistence.models import CaseEvent, CaseNote, CaseRecord, ExceptionCase, Run
from tests.test_schema import add_profile, add_run

SIG = "a" * 64


def add_case(session: Session, run: Run, **fields: object) -> ExceptionCase:
    values: dict[str, object] = {
        "profile_id": run.profile_id,
        "period": run.period,
        "kind": "missing_payment",
        "signature": SIG,
        "first_run_id": run.id,
        "last_run_id": run.id,
    }
    values.update(fields)
    case = ExceptionCase(**values)
    session.add(case)
    session.flush()
    return case


@pytest.fixture
def run(session: Session) -> Run:
    return add_run(session, add_profile(session))


def test_a_new_case_starts_open_at_version_one(session: Session, run: Run) -> None:
    case = add_case(session, run)
    session.refresh(case)

    assert (case.status, case.version, case.fix_unverified) == ("open", 1, False)


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "closed"},
        {"version": 0},
        {"signature": "short"},
        {"variance_minor": 5},  # a variance needs a currency
        {"currency": "USD"},
        {"status": "resolved"},  # needs the run that resolved it
        {"status": "fix_reported"},  # needs the run it was reported against
        {"status": "accepted"},  # needs a reason and the signature it was accepted at
        {"status": "accepted", "accepted_reason": "write_off"},
        {"status": "withdrawn"},  # needs a reason
        {"status": "withdrawn", "closed_reason": "forgotten"},
        {"status": "withdrawn", "closed_reason": "merged"},  # a merge needs a successor
        {"status": "open", "fix_unverified": True, "accepted_reason": "write_off"},
        {"status": "accepted", "fix_unverified": True},
        {"accepted_signature": SIG},  # a signature without a reason
    ],
)
def test_the_database_refuses_inconsistent_cases(
    session: Session, run: Run, fields: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        add_case(session, run, **fields)


def test_each_status_is_accepted_with_the_fields_that_justify_it(
    session: Session, run: Run
) -> None:
    other = add_case(session, run)
    add_case(session, run, status="resolved", resolved_run_id=run.id)
    add_case(session, run, status="fix_reported", fix_reported_run_id=run.id)
    add_case(session, run, status="accepted", accepted_reason="write_off", accepted_signature=SIG)
    add_case(session, run, status="withdrawn", closed_reason="records_removed")
    add_case(session, run, status="withdrawn", closed_reason="merged", successor_case_id=other.id)
    add_case(session, run, fix_unverified=True)


def test_a_case_cannot_involve_the_same_record_twice_or_an_unknown_role(
    session: Session, run: Run
) -> None:
    case = add_case(session, run)
    session.add(CaseRecord(case_id=case.id, role="invoice", record_id="INV-1"))
    session.flush()

    with pytest.raises(IntegrityError):
        session.add(CaseRecord(case_id=case.id, role="invoice", record_id="INV-1"))
        session.flush()
    session.rollback()


def test_an_unknown_record_role_is_refused(session: Session, run: Run) -> None:
    case = add_case(session, run)

    with pytest.raises(IntegrityError):
        session.add(CaseRecord(case_id=case.id, role="refund", record_id="R-1"))
        session.flush()


def test_blank_notes_are_refused(session: Session, run: Run) -> None:
    case = add_case(session, run)
    session.add(CaseNote(case_id=case.id, body="checked with billing"))
    session.flush()

    with pytest.raises(IntegrityError):
        session.add(CaseNote(case_id=case.id, body="   "))
        session.flush()


def test_deleting_a_case_removes_its_records_and_notes_but_not_its_history(
    session: Session, run: Run
) -> None:
    case = add_case(session, run)
    session.add_all(
        [
            CaseRecord(case_id=case.id, role="invoice", record_id="INV-1"),
            CaseNote(case_id=case.id, body="note"),
            CaseEvent(case_id=case.id, kind="opened", to_status="open", run_id=run.id),
        ]
    )
    session.flush()

    # The audit log keeps a reference to the case, so the case cannot be deleted.
    with pytest.raises(IntegrityError):
        session.execute(delete(ExceptionCase).where(ExceptionCase.id == case.id))


def test_the_audit_log_cannot_be_changed_or_removed(session: Session, run: Run) -> None:
    case = add_case(session, run)
    event = CaseEvent(case_id=case.id, kind="opened", to_status="open", run_id=run.id)
    session.add(event)
    session.flush()
    event_id = event.id

    for statement in (
        update(CaseEvent).where(CaseEvent.id == event_id).values(kind="tampered"),
        delete(CaseEvent).where(CaseEvent.id == event_id),
        text("TRUNCATE case_event"),
    ):
        with (
            pytest.raises((IntegrityError, DBAPIError), match="append-only"),
            session.begin_nested(),
        ):
            session.execute(statement)

    assert session.scalar(select(CaseEvent.kind).where(CaseEvent.id == event_id)) == "opened"


def test_the_audit_log_accepts_new_entries_and_defaults_its_detail(
    session: Session, run: Run
) -> None:
    case = add_case(session, run)
    first = CaseEvent(case_id=case.id, kind="opened", to_status="open")
    second = CaseEvent(case_id=case.id, kind="note", detail={"length": 3})
    session.add_all([first, second])
    session.flush()
    session.refresh(first)

    assert first.detail == {}
    assert second.id > first.id
    assert isinstance(case.id, uuid.UUID)

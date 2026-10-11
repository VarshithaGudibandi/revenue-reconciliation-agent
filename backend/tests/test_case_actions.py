"""What a person does with a case, together with what a later sync does to it."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from recon.datagen.model import Anomaly, AnomalyKind
from recon.persistence.case_actions import (
    AcceptReason,
    CaseNotFoundError,
    InvalidTransitionError,
    StaleCaseError,
    accept_variance,
    add_note,
    get_case_detail,
    list_cases,
    reopen_accepted,
    report_fix,
    retract_fix,
)
from recon.persistence.models import ExceptionCase
from recon.pipeline import reconcile_period
from tests.conftest import PERIOD, Env
from tests.test_case_sync import all_cases, case_records, check_invariants, events, sync
from tests.test_incremental_runs import Rows, rewrite

FIXED = {
    AnomalyKind.MISSING_PAYMENT,
    AnomalyKind.AMOUNT_MISMATCH,
    AnomalyKind.DUPLICATE_PAYMENT,
    AnomalyKind.PARTIAL_PAYMENT,
    AnomalyKind.MISREFERENCED_PAYMENT,
}


def case_for(env: Env, anomaly: Anomaly) -> uuid.UUID:
    refs = {("invoice", i) for i in anomaly.invoice_ids} | {
        ("payment", p) for p in anomaly.payment_ids
    }
    return next(case_id for case_id, records in case_records(env).items() if records & refs)


def fixed_anomaly(env: Env) -> Anomaly:
    """An anomaly that the corrected revision fixes, other than the ambiguous kind."""
    resolved = set(env.second.truth.resolved)
    return next(
        a
        for a in env.first.truth.anomalies
        if a.anomaly_id in resolved and a.kind is not AnomalyKind.AMBIGUOUS_PAYMENT
    )


def unfixed_anomaly(env: Env, kind: AnomalyKind | None = None) -> Anomaly:
    """An anomaly the corrected revision leaves alone."""
    resolved = set(env.second.truth.resolved)
    return next(
        a
        for a in env.first.truth.anomalies
        if a.anomaly_id not in resolved and (kind is None or a.kind is kind)
    )


def act(
    env: Env, action: Callable[..., object], case_id: uuid.UUID, **kwargs: object
) -> ExceptionCase:
    with env.sessions.begin() as session:
        action(session, case_id, **kwargs)
    with env.sessions() as session:
        return session.get_one(ExceptionCase, case_id)


def fresh(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)


def test_a_reported_fix_is_confirmed_when_the_data_reconciles(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))

    reported = act(env, report_fix, case_id)
    assert reported.status == "fix_reported"
    assert reported.fix_reported_run_id is not None

    env.register("payments", 1)
    sync(env)
    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
    assert case.status == "resolved"
    assert case.resolved_run_id is not None
    assert case.fix_reported_run_id is None
    check_invariants(env)


def test_a_reported_fix_that_did_not_work_reopens_the_case_with_a_flag(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, unfixed_anomaly(env))
    act(env, report_fix, case_id)

    env.register("payments", 1)  # new data arrives, but this problem is still in it
    sync(env)

    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
    assert (case.status, case.fix_unverified) == ("open", True)
    assert [e.kind for e in events(env) if e.case_id == case_id] == [
        "opened",
        "fix_reported",
        "fix_not_verified",
    ]
    check_invariants(env)


def test_a_reported_fix_waits_while_there_is_no_new_data(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, unfixed_anomaly(env))
    act(env, report_fix, case_id)

    again = reconcile_period(env.sessions, env.profile_id, PERIOD)

    assert again.cases is not None and not again.cases.applied
    with env.sessions() as session:
        assert session.get_one(ExceptionCase, case_id).status == "fix_reported"


def test_reporting_the_same_fix_twice_is_a_no_op(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    first = act(env, report_fix, case_id)
    event_count = len(events(env))

    second = act(env, report_fix, case_id)

    assert second.version == first.version
    assert len(events(env)) == event_count


def test_a_reported_fix_can_be_retracted(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    act(env, report_fix, case_id)

    case = act(env, retract_fix, case_id)

    assert (case.status, case.fix_reported_run_id) == ("open", None)
    assert act(env, retract_fix, case_id).version == case.version


def test_an_accepted_variance_stays_accepted_while_its_data_is_unchanged(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, unfixed_anomaly(env, AnomalyKind.UNMATCHED_PAYMENT))
    accepted = act(
        env, accept_variance, case_id, reason=AcceptReason.WRITE_OFF, note="customer was refunded"
    )
    assert (accepted.status, accepted.accepted_reason) == ("accepted", "write_off")
    assert accepted.accepted_signature == accepted.signature

    env.register("payments", 1)  # other problems changed; this record did not
    sync(env)

    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
        detail = get_case_detail(session, case_id)
    assert (case.status, case.version) == ("accepted", accepted.version)
    assert [n.body for n in detail.notes] == ["customer was refunded"]
    check_invariants(env)


def test_an_accepted_variance_returns_when_its_data_changes(env: Env, tmp_path: Path) -> None:
    fresh(env)
    anomaly = unfixed_anomaly(env, AnomalyKind.UNMATCHED_PAYMENT)
    case_id = case_for(env, anomaly)
    act(env, accept_variance, case_id, reason=AcceptReason.OTHER, note="known")
    payment_id = anomaly.payment_ids[0]

    def change(rows: Rows) -> Rows:
        for row in rows:
            if row["payment_id"] == payment_id:
                row["amount_paid"] = "1.23"
        return rows

    changed = tmp_path / "changed" / "payments.csv"
    rewrite(env.directories[0] / "payments.csv", changed, change)
    env.register_path("payments", changed)
    sync(env)

    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
    assert (case.status, case.accepted_reason, case.accepted_signature) == ("open", None, None)
    assert "reopened_data_changed" in [e.kind for e in events(env) if e.case_id == case_id]
    check_invariants(env)


def test_the_data_beats_an_acceptance_when_it_reconciles(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    act(env, accept_variance, case_id, reason=AcceptReason.TIMING_DIFFERENCE, note="in transit")

    env.register("payments", 1)
    sync(env)

    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
    assert case.status == "resolved"
    assert case.accepted_reason is None


def test_an_acceptance_needs_a_note_and_can_be_undone(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))

    with pytest.raises(ValueError, match="note"), env.sessions.begin() as session:
        accept_variance(session, case_id, reason=AcceptReason.OTHER, note="  ")
    accepted = act(env, accept_variance, case_id, reason=AcceptReason.OTHER, note="ok")
    reopened = act(env, reopen_accepted, case_id)

    assert (accepted.status, reopened.status) == ("accepted", "open")
    assert reopened.accepted_reason is None


def test_accepting_twice_with_the_same_reason_is_a_no_op(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    first = act(env, accept_variance, case_id, reason=AcceptReason.WRITE_OFF, note="a")
    second = act(env, accept_variance, case_id, reason=AcceptReason.WRITE_OFF, note="a")

    assert second.version == first.version
    with env.sessions() as session:
        assert len(get_case_detail(session, case_id).notes) == 1


def test_transitions_that_make_no_sense_are_refused(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    act(env, accept_variance, case_id, reason=AcceptReason.OTHER, note="ok")
    env.register("payments", 1)
    sync(env)  # now resolved by the data

    attempts: list[Callable[[Session], object]] = [
        lambda s: report_fix(s, case_id),
        lambda s: retract_fix(s, case_id),
        lambda s: reopen_accepted(s, case_id),
        lambda s: accept_variance(s, case_id, reason=AcceptReason.OTHER, note="again"),
    ]
    for attempt in attempts:
        with pytest.raises(InvalidTransitionError), env.sessions.begin() as session:
            attempt(session)


def test_acting_on_a_case_that_changed_since_it_was_seen_is_refused(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    with env.sessions() as session:
        seen = session.get_one(ExceptionCase, case_id).version
    act(env, report_fix, case_id)
    act(env, retract_fix, case_id)

    with pytest.raises(StaleCaseError) as error, env.sessions.begin() as session:
        report_fix(session, case_id, expected_version=seen)

    assert error.value.expected == seen and error.value.actual > seen
    with env.sessions.begin() as session:
        current = session.get_one(ExceptionCase, case_id).version
        report_fix(session, case_id, expected_version=current)


def test_an_unknown_case_is_reported(env: Env) -> None:
    with env.sessions.begin() as session, pytest.raises(CaseNotFoundError):
        report_fix(session, uuid.uuid4())
    with env.sessions() as session, pytest.raises(CaseNotFoundError):
        get_case_detail(session, uuid.uuid4())


def test_notes_work_in_every_status_and_do_not_change_the_version(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    with env.sessions() as session:
        version = session.get_one(ExceptionCase, case_id).version

    with env.sessions.begin() as session:
        add_note(session, case_id, "asked billing")
    env.register("payments", 1)
    sync(env)  # the case is resolved by now
    with env.sessions.begin() as session:
        add_note(session, case_id, "confirmed with finance")
        with pytest.raises(ValueError, match="note"):
            add_note(session, case_id, " ")

    with env.sessions() as session:
        detail = get_case_detail(session, case_id)
    assert [n.body for n in detail.notes] == ["asked billing", "confirmed with finance"]
    assert detail.case.status == "resolved"
    assert detail.case.version > version  # the sync changed it; the notes did not


def test_the_detail_shows_records_history_and_notes_together(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    act(env, report_fix, case_id)
    with env.sessions.begin() as session:
        add_note(session, case_id, "fixed in the billing system")

    with env.sessions() as session:
        detail = get_case_detail(session, case_id)

    assert detail.records and all(role in ("invoice", "payment") for role, _ in detail.records)
    assert [e.kind for e in detail.events] == ["opened", "fix_reported"]
    assert detail.events[1].detail["by"] == "user"
    assert [n.body for n in detail.notes] == ["fixed in the billing system"]


def test_cases_can_be_listed_by_status(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    act(env, report_fix, case_id)

    with env.sessions() as session:
        waiting = list_cases(session, env.profile_id, PERIOD, ("fix_reported",))
        everything = list_cases(session, env.profile_id, PERIOD)
        none = list_cases(session, env.profile_id, PERIOD, ("resolved",))

    assert [c.id for c in waiting] == [case_id]
    assert len(everything) == len(all_cases(env))
    assert none == []


def test_the_same_action_from_many_threads_happens_once(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    barrier = threading.Barrier(6)

    def work(_: int) -> str:
        barrier.wait()
        with env.sessions.begin() as session:
            return report_fix(session, case_id).status

    with ThreadPoolExecutor(6) as pool:
        statuses = [f.result(timeout=60) for f in [pool.submit(work, i) for i in range(6)]]

    assert set(statuses) == {"fix_reported"}
    assert [e.kind for e in events(env) if e.case_id == case_id].count("fix_reported") == 1


def test_competing_actions_leave_one_winner(env: Env) -> None:
    fresh(env)
    case_id = case_for(env, fixed_anomaly(env))
    barrier = threading.Barrier(2)

    def fix(_: int) -> str:
        barrier.wait()
        with env.sessions.begin() as session:
            return report_fix(session, case_id).status

    def accept(_: int) -> str:
        barrier.wait()
        with env.sessions.begin() as session:
            return accept_variance(session, case_id, reason=AcceptReason.OTHER, note="n").status

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(fix, 0), pool.submit(accept, 0)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=60))
            except InvalidTransitionError:
                outcomes.append("refused")

    with env.sessions() as session:
        final = session.get_one(ExceptionCase, case_id).status
    assert final in ("fix_reported", "accepted")
    assert final in outcomes
    # The audit log shows a consistent chain of transitions, whichever acted first.
    chain = [(e.from_status, e.to_status) for e in events(env) if e.case_id == case_id]
    assert chain[0] == (None, "open")
    for (_, previous_to), (next_from, _) in zip(chain, chain[1:], strict=False):
        assert previous_to == next_from


def test_actions_and_a_sync_taking_turns_do_not_deadlock(env: Env) -> None:
    fresh(env)
    ids = [case_for(env, a) for a in env.first.truth.anomalies if a.kind in FIXED][:6]
    env.register("payments", 1)
    barrier = threading.Barrier(len(ids) + 1)

    def act_on(case_id: uuid.UUID) -> str:
        barrier.wait()
        with env.sessions.begin() as session:
            return report_fix(session, case_id).status

    def run_sync() -> bool:
        barrier.wait()
        outcome = reconcile_period(env.sessions, env.profile_id, PERIOD)
        return outcome.cases is not None

    with ThreadPoolExecutor(len(ids) + 1) as pool:
        futures = [pool.submit(act_on, case_id) for case_id in ids] + [pool.submit(run_sync)]
        for future in futures:
            future.result(timeout=60)  # a deadlock would time out here

    check_invariants(env)
    with env.sessions() as session:
        statuses = {c.status for c in list_cases(session, env.profile_id, PERIOD)}
    assert statuses <= {"open", "fix_reported", "resolved", "withdrawn", "accepted"}

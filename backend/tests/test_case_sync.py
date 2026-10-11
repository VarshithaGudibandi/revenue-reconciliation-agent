"""Case sync against a real database, driven through the whole pipeline."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import func, select

from recon.datagen.generate import apply_corrections
from recon.datagen.model import AnomalyKind
from recon.datagen.writer import write_dataset
from recon.engine.model import ExceptionKind, ReconciliationResult
from recon.persistence.cases import sync_cases
from recon.persistence.models import CaseEvent, CaseRecord, ExceptionCase
from recon.persistence.runs import find_current_run, load_result, run_reconciliation
from recon.pipeline import reconcile_period
from tests.conftest import PERIOD, Env, make_env
from tests.test_incremental_runs import Rows, rewrite

ACTIVE = ("open", "fix_reported", "accepted")


def sync(env: Env) -> None:
    reconcile_period(env.sessions, env.profile_id, PERIOD, verify=True)


def all_cases(env: Env) -> list[ExceptionCase]:
    with env.sessions() as session:
        cases = list(session.scalars(select(ExceptionCase).order_by(ExceptionCase.number)))
        session.expunge_all()
    return cases


def case_records(env: Env) -> dict[uuid.UUID, set[tuple[str, str]]]:
    with env.sessions() as session:
        rows = session.execute(
            select(CaseRecord.case_id, CaseRecord.role, CaseRecord.record_id)
        ).all()
    records: dict[uuid.UUID, set[tuple[str, str]]] = {}
    for case_id, role, record_id in rows:
        records.setdefault(case_id, set()).add((role, record_id))
    return records


def events(env: Env, kind: str | None = None) -> list[CaseEvent]:
    with env.sessions() as session:
        query = select(CaseEvent).order_by(CaseEvent.id)
        if kind is not None:
            query = query.where(CaseEvent.kind == kind)
        rows = list(session.scalars(query))
        session.expunge_all()
    return rows


def current_result(env: Env) -> ReconciliationResult:
    with env.sessions() as session:
        run = find_current_run(session, env.profile_id, PERIOD)
        assert run is not None
        return load_result(session, run.id)


def check_invariants(env: Env) -> None:
    """What must hold after every sync, whatever the history."""
    result = current_result(env)
    cases = all_cases(env)
    records = case_records(env)
    active = [c for c in cases if c.status in ACTIVE]

    # Every exception belongs to exactly one active case, and every active case to one exception.
    exception_refs = [
        {("invoice", i) for i in e.invoice_ids} | {("payment", p) for p in e.payment_ids}
        for e in result.exceptions
    ]
    active_refs = [records[c.id] for c in active]
    assert sorted(map(sorted, exception_refs)) == sorted(map(sorted, active_refs))

    matched = {("invoice", m.invoice_id) for m in result.matches} | {
        ("payment", p) for m in result.matches for p in m.payment_ids
    }
    in_exception = {ref for refs in exception_refs for ref in refs}
    for case in cases:
        if case.status == "resolved":
            assert case.resolved_run_id is not None
            assert records[case.id] & matched, "a resolved case needs matched records as evidence"
            assert not records[case.id] <= in_exception, (
                "a case whose records are all still excepted is not resolved"
            )


def test_the_first_sync_opens_one_case_per_exception(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)

    outcome = reconcile_period(env.sessions, env.profile_id, PERIOD, verify=True)

    result = current_result(env)
    cases = all_cases(env)
    assert outcome.cases is not None and outcome.cases.applied
    assert len(cases) == len(result.exceptions) == outcome.cases.opened
    assert all(c.status == "open" for c in cases)
    assert [c.number for c in cases] == sorted(c.number for c in cases)
    assert len(events(env, "opened")) == len(cases)
    check_invariants(env)


def test_syncing_again_changes_nothing(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    before = [(c.id, c.status, c.version) for c in all_cases(env)]
    event_count = len(events(env))

    again = reconcile_period(env.sessions, env.profile_id, PERIOD)
    with env.sessions.begin() as session:
        direct = sync_cases(session, env.profile_id, PERIOD)

    assert again.cases is not None and not again.cases.applied
    assert not direct.applied
    assert [(c.id, c.status, c.version) for c in all_cases(env)] == before
    assert len(events(env)) == event_count


def test_corrections_resolve_cases_and_leave_the_others_untouched(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    first_cases = {c.id: c for c in all_cases(env)}
    first_records = case_records(env)

    env.register("payments", 1)
    sync(env)
    second_cases = {c.id: c for c in all_cases(env)}

    # No case was created for an existing problem, and no case disappeared.
    assert set(first_cases) <= set(second_cases)
    resolved = [c for c in second_cases.values() if c.status == "resolved"]
    assert resolved
    for case in resolved:
        assert case.resolved_run_id is not None and case.resolved_at is not None
    # The truth says which anomalies were fixed; their cases are the resolved ones.
    truth = {a.anomaly_id: a for a in env.first.truth.anomalies}
    for anomaly_id in env.second.truth.resolved:
        anomaly = truth[anomaly_id]
        refs = {("invoice", i) for i in anomaly.invoice_ids} | {
            ("payment", p) for p in anomaly.payment_ids
        }
        owner = next(cid for cid, records in first_records.items() if records & refs)
        if anomaly.kind is AnomalyKind.AMBIGUOUS_PAYMENT:
            # The look-alike invoices are still unpaid, so the case carries on as a missing payment.
            assert second_cases[owner].status == "open"
            assert second_cases[owner].kind == ExceptionKind.MISSING_PAYMENT
        else:
            assert second_cases[owner].status == "resolved", anomaly.kind
    # Problems that were not fixed keep their case, status and identity.
    for case_id, case in first_cases.items():
        if case.status == "open" and second_cases[case_id].status == "open":
            assert second_cases[case_id].first_run_id == case.first_run_id
    check_invariants(env)


def test_a_partial_payment_turns_a_missing_payment_into_the_same_case_not_a_new_one(
    env: Env, tmp_path: Path
) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    with env.sessions() as session:
        missing = session.scalars(
            select(ExceptionCase).where(ExceptionCase.kind == "missing_payment").limit(1)
        ).one()
        invoice_id = next(
            record_id
            for (record_id,) in session.execute(
                select(CaseRecord.record_id).where(CaseRecord.case_id == missing.id)
            )
        )
        invoice_total = missing.variance_minor
        case_id = missing.id
    assert invoice_total is not None

    def add_partial(rows: Rows) -> Rows:
        customer = next(
            r["customer_id"]
            for r in read_rows(env.directories[0] / "invoices.csv")
            if r["invoice_id"] == invoice_id
        )
        currency = next(
            r["currency"]
            for r in read_rows(env.directories[0] / "invoices.csv")
            if r["invoice_id"] == invoice_id
        )
        rows.append(
            {
                "payment_id": "PAY-777777",
                "reference": invoice_id,
                "customer": customer,
                "received_on": "01/20/2026",
                "ccy": currency,
                "amount_paid": f"{invoice_total // 2 / 100:.2f}",
            }
        )
        return rows

    changed = tmp_path / "partial" / "payments.csv"
    rewrite(env.directories[0] / "payments.csv", changed, add_partial)
    env.register_path("payments", changed)
    sync(env)

    with env.sessions() as session:
        same = session.get_one(ExceptionCase, case_id)
        assert same.status == "open"
        assert same.kind == "partial_payment"
        assert ("payment", "PAY-777777") in case_records(env)[case_id]
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExceptionCase)
                .where(ExceptionCase.status == "resolved")
            )
            == 0
        )
    assert any(e.case_id == case_id and e.kind == "updated" for e in events(env))
    check_invariants(env)


def read_rows(path: Path) -> Rows:
    import csv

    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_reverting_a_file_reopens_the_same_cases_and_creates_no_new_ones(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    original = {c.id: c.status for c in all_cases(env)}
    env.register("payments", 1)
    sync(env)
    while_fixed = {c.id: c.status for c in all_cases(env)}
    assert "resolved" in while_fixed.values()

    env.register("payments", 0)  # the source goes back to what it was
    sync(env)

    after = {c.id: c for c in all_cases(env)}
    assert set(after) == set(while_fixed)  # going back created nothing new
    assert set(original) <= set(after)
    # Every case the first sync opened is a live problem again.
    assert all(after[case_id].status in ACTIVE for case_id in original)
    # Cases created only while the file was fixed have folded back into the originals.
    for case_id in set(after) - set(original):
        assert (after[case_id].status, after[case_id].closed_reason) == ("withdrawn", "merged")
    assert events(env, "reopened")
    check_invariants(env)


def test_a_stale_run_that_finishes_late_cannot_roll_the_cases_back(
    env: Env, tmp_path: Path
) -> None:
    third = apply_corrections(env.second, seed=99, fix_fraction=1.0)
    write_dataset(third, tmp_path / "v3")
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)  # the cases reflect the first files
    env.register("payments", 1)
    stale_run = env.run_id()  # created for the second files, not executed yet
    env.register_path("payments", tmp_path / "v3" / "payments.csv")
    sync(env)  # the third files are processed first, and resolve more
    snapshot = [(c.id, c.status, c.version) for c in all_cases(env)]
    assert any(status == "resolved" for _, status, _ in snapshot)
    event_count = len(events(env))

    run_reconciliation(env.sessions, stale_run)  # the older run finishes late
    with env.sessions.begin() as session:
        late = sync_cases(session, env.profile_id, PERIOD)

    assert not late.applied
    assert [(c.id, c.status, c.version) for c in all_cases(env)] == snapshot
    assert len(events(env)) == event_count
    check_invariants(env)


def test_cases_wait_while_the_current_run_has_not_finished(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    env.run_id()  # pending, never executed

    with env.sessions.begin() as session:
        summary = sync_cases(session, env.profile_id, PERIOD)

    assert not summary.applied
    assert all_cases(env) == []


def test_a_case_whose_records_were_removed_is_withdrawn_not_resolved(
    env: Env, tmp_path: Path
) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    with env.sessions() as session:
        missing = session.scalars(
            select(ExceptionCase).where(ExceptionCase.kind == "missing_payment").limit(1)
        ).one()
        case_id = missing.id
        (invoice_id,) = session.scalars(
            select(CaseRecord.record_id).where(CaseRecord.case_id == case_id)
        ).all()

    def drop_invoice(rows: Rows) -> Rows:
        return [r for r in rows if r["invoice_id"] != invoice_id]

    changed = tmp_path / "fewer" / "invoices.csv"
    rewrite(env.directories[0] / "invoices.csv", changed, drop_invoice)
    env.register_path("invoices", changed)
    sync(env)

    with env.sessions() as session:
        case = session.get_one(ExceptionCase, case_id)
    assert (case.status, case.closed_reason) == ("withdrawn", "records_removed")
    assert case.resolved_run_id is None
    check_invariants(env)


def test_two_cases_that_become_one_problem_are_merged(env: Env, tmp_path: Path) -> None:
    root = tmp_path / "small"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    invoices = "invoice_id,customer_id,issue_date,currency,total\nINV-1,C1,2026-01-05,USD,100.00\n"
    header = "payment_id,reference,customer,received_on,ccy,amount_paid\n"
    (root / "a" / "invoices.csv").write_text(invoices)
    (root / "a" / "payments.csv").write_text(header + "P1,INV-999,C1,01/10/2026,USD,90.00\n")
    (root / "b" / "payments.csv").write_text(header + "P1,INV-999,C1,01/10/2026,USD,100.00\n")
    env.register_path("invoices", root / "a" / "invoices.csv")
    env.register_path("payments", root / "a" / "payments.csv")
    sync(env)
    first = all_cases(env)
    assert sorted(c.kind for c in first) == ["missing_payment", "unmatched_payment"]

    env.register_path("payments", root / "b" / "payments.csv")  # the amounts now agree
    sync(env)

    cases = {c.id: c for c in all_cases(env)}
    assert set(cases) == {c.id for c in first}
    survivor = next(c for c in cases.values() if c.status == "open")
    merged = next(c for c in cases.values() if c.status == "withdrawn")
    assert survivor.kind == "misreferenced_payment"
    assert (merged.closed_reason, merged.successor_case_id) == ("merged", survivor.id)
    check_invariants(env)


def test_a_chain_of_revisions_keeps_the_invariants(env: Env, tmp_path: Path) -> None:
    third = apply_corrections(env.second, seed=99, fix_fraction=1.0)
    write_dataset(third, tmp_path / "v3")
    env.register("invoices", 0)
    env.register("payments", 0)
    sync(env)
    check_invariants(env)
    env.register("payments", 1)
    sync(env)
    check_invariants(env)
    env.register_path("payments", tmp_path / "v3" / "payments.csv")
    sync(env)
    check_invariants(env)

    statuses = {c.status for c in all_cases(env)}
    assert "resolved" in statuses
    # Every transition is on record.
    assert len(events(env, "resolved")) == sum(c.status == "resolved" for c in all_cases(env))


@pytest.mark.parametrize("seed", [3, 27])
def test_other_seeds_keep_the_invariants(
    migrated_database_url: str, tmp_path: Path, seed: int
) -> None:
    with make_env(migrated_database_url, tmp_path, seed) as other:
        other.register("invoices", 0)
        other.register("payments", 0)
        sync(other)
        check_invariants(other)
        other.register("payments", 1)
        sync(other)
        check_invariants(other)


def test_concurrent_syncs_apply_once(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    barrier = threading.Barrier(5)

    def work(_: int) -> bool:
        barrier.wait()
        outcome = reconcile_period(env.sessions, env.profile_id, PERIOD)
        return outcome.cases is not None and outcome.cases.applied

    with ThreadPoolExecutor(5) as pool:
        applied = list(pool.map(work, range(5)))

    assert sum(applied) == 1
    result = current_result(env)
    assert len(all_cases(env)) == len(result.exceptions)
    assert len(events(env, "opened")) == len(result.exceptions)
    check_invariants(env)

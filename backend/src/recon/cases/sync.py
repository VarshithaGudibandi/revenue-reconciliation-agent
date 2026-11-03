"""Cases: keep the problems of a period in line with the run for its current files.

``sync_cases`` is idempotent and order-safe. It always works from the run for the
latest files of the period and records which run the cases reflect, so running it
again changes nothing, a stale run that finishes late cannot roll the cases back, and
a file reverted to earlier content brings back the cases that content had.

A case closes as ``resolved`` only when its records now appear in a match. If its
records are gone from the data it is ``withdrawn``, which claims nothing.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from recon.cases.assign import CaseView, RecordRef, assign
from recon.cases.signature import exception_signature
from recon.engine.model import ReconciliationException, ReconciliationResult
from recon.persistence.models import (
    CaseEvent,
    CaseRecord,
    CaseSync,
    ExceptionCase,
    Run,
    SourceRecord,
)
from recon.persistence.repository import check_period, role_sources_of
from recon.persistence.runs import find_current_run, load_result, run_versions

_IN_CHUNK = 10_000


@dataclass
class SyncSummary:
    run_id: uuid.UUID | None
    applied: bool
    opened: int = 0
    updated: int = 0
    reopened: int = 0
    resolved: int = 0
    withdrawn: int = 0
    fix_not_verified: int = 0
    events: list[CaseEvent] = field(default_factory=list, repr=False)


def sync_cases(session: Session, profile_id: uuid.UUID, period: str) -> SyncSummary:
    """Bring the cases of a period in line with the run for its latest files.

    Does nothing when there is no finished run for those files, or when the cases
    already reflect it.
    """
    check_period(period)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"case-sync:{profile_id}:{period}"},
    )

    run = find_current_run(session, profile_id, period)
    if run is None or run.status != "succeeded":
        return SyncSummary(None if run is None else run.id, applied=False)
    synced = session.get(CaseSync, (profile_id, period))
    if synced is not None and synced.run_id == run.id:
        return SyncSummary(run.id, applied=False)

    result = load_result(session, run.id)
    versions = run_versions(session, run.id)
    role_source = role_sources_of(session, profile_id)
    exception_refs = [_refs(e) for e in result.exceptions]

    cases = list(
        session.scalars(
            select(ExceptionCase)
            .where(ExceptionCase.profile_id == profile_id, ExceptionCase.period == period)
            .order_by(ExceptionCase.number)
            .with_for_update()
        )
    )
    members = _case_members(session, [case.id for case in cases])
    views = [
        CaseView(str(case.id), case.status, case.closed_reason, rank, frozenset(members[case.id]))
        for rank, case in enumerate(cases)
    ]
    assignment = assign(views, exception_refs)

    all_refs = {ref for refs in exception_refs for ref in refs}
    hashes = _content_hashes(
        session, {role: versions[name].id for role, name in role_source.items()}, all_refs
    )

    summary = SyncSummary(run.id, applied=True)
    by_id = {str(case.id): case for case in cases}
    owner_of: dict[RecordRef, str] = {}
    for index, case_id in assignment.case_for.items():
        for ref in exception_refs[index]:
            owner_of[ref] = case_id

    for index, case_id in sorted(assignment.case_for.items()):
        exception = result.exceptions[index]
        _follow(
            session, by_id[case_id], exception, exception_refs[index], hashes, members, run, summary
        )

    for index in assignment.new:
        _open(
            session,
            profile_id,
            period,
            result.exceptions[index],
            exception_refs[index],
            hashes,
            run,
            summary,
        )

    if assignment.unassigned:
        _close_vanished(
            session,
            [by_id[case_id] for case_id in assignment.unassigned],
            members,
            owner_of,
            result,
            {role: versions[name].id for role, name in role_source.items()},
            run,
            summary,
        )

    session.add_all(summary.events)
    if synced is None:
        session.add(CaseSync(profile_id=profile_id, period=period, run_id=run.id))
    else:
        synced.run_id = run.id
        synced.synced_at = func.now()
    session.flush()
    return summary


def _refs(exception: ReconciliationException) -> frozenset[RecordRef]:
    return frozenset(
        [("invoice", i) for i in exception.invoice_ids]
        + [("payment", p) for p in exception.payment_ids]
    )


def _case_members(
    session: Session, case_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, set[RecordRef]]:
    members: dict[uuid.UUID, set[RecordRef]] = defaultdict(set)
    ids = list(case_ids)
    for start in range(0, len(ids), _IN_CHUNK):
        for case_id, role, record_id in session.execute(
            select(CaseRecord.case_id, CaseRecord.role, CaseRecord.record_id).where(
                CaseRecord.case_id.in_(ids[start : start + _IN_CHUNK])
            )
        ):
            members[case_id].add((role, record_id))
    return members


def _content_hashes(
    session: Session, version_ids: Mapping[str, uuid.UUID], refs: Iterable[RecordRef]
) -> dict[RecordRef, str]:
    wanted: dict[str, list[str]] = defaultdict(list)
    for role, record_id in refs:
        wanted[role].append(record_id)
    hashes: dict[RecordRef, str] = {}
    for role, ids in wanted.items():
        for start in range(0, len(ids), _IN_CHUNK):
            for record_id, content_hash in session.execute(
                select(SourceRecord.record_id, SourceRecord.content_hash).where(
                    SourceRecord.file_version_id == version_ids[role],
                    SourceRecord.record_id.in_(ids[start : start + _IN_CHUNK]),
                )
            ):
                hashes[(role, record_id)] = content_hash
    return hashes


def _signature(
    exception: ReconciliationException, refs: frozenset[RecordRef], hashes: Mapping[RecordRef, str]
) -> str:
    return exception_signature(
        exception.kind.value,
        exception.variance,
        [(role, rid, hashes[(role, rid)]) for role, rid in refs],
    )


def _event(
    case: ExceptionCase,
    event: str,
    run: Run,
    from_status: str | None,
    to_status: str | None,
    **detail: object,
) -> CaseEvent:
    return CaseEvent(
        case_id=case.id,
        kind=event,
        from_status=from_status,
        to_status=to_status,
        run_id=run.id,
        detail=detail,
    )


def _set_records(
    session: Session,
    case: ExceptionCase,
    members: dict[uuid.UUID, set[RecordRef]],
    refs: frozenset[RecordRef],
) -> tuple[set[RecordRef], set[RecordRef]]:
    current = members.get(case.id, set())
    added, removed = set(refs) - current, current - set(refs)
    for role, record_id in removed:
        session.execute(
            delete(CaseRecord).where(
                CaseRecord.case_id == case.id,
                CaseRecord.role == role,
                CaseRecord.record_id == record_id,
            )
        )
    if added:
        session.execute(
            insert(CaseRecord),
            [{"case_id": case.id, "role": role, "record_id": rid} for role, rid in sorted(added)],
        )
    members[case.id] = set(refs)
    return added, removed


def _describe(case: ExceptionCase) -> dict[str, object]:
    return {"kind": case.kind, "variance_minor": case.variance_minor, "currency": case.currency}


def _open(
    session: Session,
    profile_id: uuid.UUID,
    period: str,
    exception: ReconciliationException,
    refs: frozenset[RecordRef],
    hashes: Mapping[RecordRef, str],
    run: Run,
    summary: SyncSummary,
) -> None:
    variance = exception.variance
    case = ExceptionCase(
        id=uuid.uuid4(),
        profile_id=profile_id,
        period=period,
        kind=exception.kind.value,
        variance_minor=None if variance is None else variance.minor,
        currency=None if variance is None else variance.currency,
        proposed_invoice_id=exception.proposed_invoice_id,
        signature=_signature(exception, refs, hashes),
        first_run_id=run.id,
        last_run_id=run.id,
    )
    session.add(case)
    session.flush()
    session.execute(
        insert(CaseRecord),
        [{"case_id": case.id, "role": role, "record_id": rid} for role, rid in sorted(refs)],
    )
    summary.events.append(_event(case, "opened", run, None, "open", **_describe(case)))
    summary.opened += 1


def _follow(
    session: Session,
    case: ExceptionCase,
    exception: ReconciliationException,
    refs: frozenset[RecordRef],
    hashes: Mapping[RecordRef, str],
    members: dict[uuid.UUID, set[RecordRef]],
    run: Run,
    summary: SyncSummary,
) -> None:
    """The case's problem is still there: update the case and decide what its status becomes.

    The version only moves when something a person could have seen changes, so an
    unchanged case does not invalidate what a user is looking at.
    """
    before = _describe(case)
    previous_status, previous_signature = case.status, case.signature
    signature = _signature(exception, refs, hashes)
    variance = exception.variance

    case.kind = exception.kind.value
    case.variance_minor = None if variance is None else variance.minor
    case.currency = None if variance is None else variance.currency
    case.proposed_invoice_id = exception.proposed_invoice_id
    case.signature = signature
    case.last_run_id = run.id
    added, removed = _set_records(session, case, members, refs)
    content_changed = bool(added or removed) or signature != previous_signature

    status_changed = True
    if previous_status == "open":
        status_changed = False
    elif previous_status == "fix_reported":
        case.status = "open"
        case.fix_reported_run_id = None
        case.fix_unverified = True
        summary.events.append(_event(case, "fix_not_verified", run, previous_status, "open"))
        summary.fix_not_verified += 1
    elif previous_status == "accepted":
        if signature == case.accepted_signature:
            status_changed = False
            content_changed = False
        else:
            case.status = "open"
            case.accepted_reason = None
            case.accepted_signature = None
            summary.events.append(
                _event(case, "reopened_data_changed", run, previous_status, "open")
            )
            summary.reopened += 1
    else:  # resolved or withdrawn: the problem has come back
        case.status = "open"
        case.resolved_run_id = None
        case.resolved_at = None
        case.closed_reason = None
        case.successor_case_id = None
        summary.events.append(_event(case, "reopened", run, previous_status, "open"))
        summary.reopened += 1

    if content_changed and previous_status == "open":
        summary.events.append(
            _event(
                case,
                "updated",
                run,
                "open",
                "open",
                before=before,
                after=_describe(case),
                records_added=sorted(added),
                records_removed=sorted(removed),
            )
        )
        summary.updated += 1
    if status_changed or content_changed:
        _touch(case)


def _touch(case: ExceptionCase) -> None:
    case.version += 1
    case.updated_at = func.now()


def _close_vanished(
    session: Session,
    cases: list[ExceptionCase],
    members: dict[uuid.UUID, set[RecordRef]],
    owner_of: Mapping[RecordRef, str],
    result: ReconciliationResult,
    version_ids: Mapping[str, uuid.UUID],
    run: Run,
    summary: SyncSummary,
) -> None:
    """Cases whose exception is gone: resolved if their records reconcile, else withdrawn."""
    matched = {("invoice", m.invoice_id) for m in result.matches} | {
        ("payment", p) for m in result.matches for p in m.payment_ids
    }
    candidates = {ref for case in cases for ref in members.get(case.id, set())}
    present = set(_content_hashes(session, version_ids, candidates))

    for case in cases:
        refs = members.get(case.id, set())
        previous_status = case.status
        elsewhere = Counter(owner_of[ref] for ref in refs if ref in owner_of)
        reconciled = sorted(ref for ref in refs if ref in matched)

        if elsewhere:
            successor = min(elsewhere, key=lambda owner: (-elsewhere[owner], owner))
            case.status = "withdrawn"
            case.closed_reason = "merged"
            case.successor_case_id = uuid.UUID(successor)
            summary.events.append(
                _event(
                    case,
                    "withdrawn",
                    run,
                    previous_status,
                    "withdrawn",
                    reason="merged",
                    successor=successor,
                )
            )
            summary.withdrawn += 1
        elif reconciled:
            case.status = "resolved"
            case.resolved_run_id = run.id
            case.resolved_at = func.now()
            summary.events.append(
                _event(case, "resolved", run, previous_status, "resolved", matched=reconciled)
            )
            summary.resolved += 1
        else:
            gone = sorted(ref for ref in refs if ref not in present)
            case.status = "withdrawn"
            case.closed_reason = "records_removed"
            summary.events.append(
                _event(
                    case,
                    "withdrawn",
                    run,
                    previous_status,
                    "withdrawn",
                    reason="records_removed",
                    records=gone,
                )
            )
            summary.withdrawn += 1

        case.fix_reported_run_id = None
        case.fix_unverified = False
        case.accepted_reason = None
        case.accepted_signature = None
        case.last_run_id = run.id
        _touch(case)

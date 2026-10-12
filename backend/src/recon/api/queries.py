"""Read models: what the API shows, assembled from the tables."""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from recon.api.explain import RecordFacts, explain
from recon.api.schemas import (
    Amount,
    AnalysisOut,
    CaseDetailOut,
    CasePage,
    CaseRow,
    EventOut,
    FileOut,
    KindTotal,
    MatchPage,
    MatchRow,
    NoteOut,
    PeriodOut,
    PeriodSummary,
    RecordOut,
    RunOut,
    amount_of,
)
from recon.domain.money import Money
from recon.persistence.case_actions import get_case_detail
from recon.persistence.models import (
    CaseRecord,
    CaseSync,
    ExceptionCase,
    Profile,
    ResultMatch,
    ResultMatchPayment,
    Run,
    SourceFileVersion,
    SourceRecord,
)
from recon.persistence.repository import check_period, get_profile, latest_file_versions
from recon.persistence.runs import find_current_run
from recon.profiles import RECONCILIATION_SOURCES

NEEDS_ATTENTION = ("open", "fix_reported")
_ROLE_SOURCE = {"invoice": "invoices", "payment": "payments"}
RecordKey = tuple[str, str]


def list_profiles(session: Session) -> list[Profile]:
    return list(session.scalars(select(Profile).order_by(Profile.name)))


def file_out(version: SourceFileVersion) -> FileOut:
    return FileOut(
        source=version.source_name,
        version_number=version.version_number,
        record_count=version.record_count,
        rejected_count=version.rejected_count,
        size_bytes=version.size_bytes,
        object_key=version.object_key,
        registered_at=version.created_at,
    )


def run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        status=run.status,
        mode=run.mode,
        partitions_total=run.partitions_total,
        partitions_recomputed=run.partitions_recomputed,
        finished_at=run.finished_at,
    )


def list_periods(session: Session, profile_id: uuid.UUID) -> list[PeriodOut]:
    get_profile(session, profile_id)
    periods = session.scalars(
        select(SourceFileVersion.period)
        .where(SourceFileVersion.profile_id == profile_id)
        .distinct()
        .order_by(SourceFileVersion.period.desc())
    )
    return [
        PeriodOut(
            period=period,
            files=[file_out(v) for v in latest_file_versions(session, profile_id, period).values()],
        )
        for period in periods
    ]


def period_summary(session: Session, profile_id: uuid.UUID, period: str) -> PeriodSummary:
    check_period(period)
    profile = get_profile(session, profile_id)
    versions = latest_file_versions(session, profile_id, period)
    run = find_current_run(session, profile_id, period)

    needed = RECONCILIATION_SOURCES[profile.reconciliation_type]
    if not versions:
        state: Literal["no_files", "waiting_for_files", "not_reconciled", "reconciled"] = "no_files"
    elif any(name not in versions for name in needed):
        state = "waiting_for_files"
    elif run is not None and run.status == "succeeded":
        state = "reconciled"
    else:
        state = "not_reconciled"

    scope = (ExceptionCase.profile_id == profile_id, ExceptionCase.period == period)
    status_counts = dict(
        session.execute(
            select(ExceptionCase.status, func.count()).where(*scope).group_by(ExceptionCase.status)
        ).all()
    )
    counts = {
        kind: count
        for kind, count in session.execute(
            select(ExceptionCase.kind, func.count())
            .where(*scope, ExceptionCase.status.in_(NEEDS_ATTENTION))
            .group_by(ExceptionCase.kind)
        )
    }
    totals: dict[str, list[Amount]] = defaultdict(list)
    for kind, currency, total in session.execute(
        select(
            ExceptionCase.kind,
            ExceptionCase.currency,
            func.sum(func.abs(ExceptionCase.variance_minor)),
        )
        .where(
            *scope,
            ExceptionCase.status.in_(NEEDS_ATTENTION),
            ExceptionCase.variance_minor.is_not(None),
        )
        .group_by(ExceptionCase.kind, ExceptionCase.currency)
        .order_by(ExceptionCase.currency)
    ):
        totals[kind].append(amount_of(int(total), str(currency)))

    invoices = versions["invoices"].record_count if "invoices" in versions else 0
    payments = versions["payments"].record_count if "payments" in versions else 0
    matched = 0
    if state == "reconciled" and run is not None:
        matched = (
            session.scalar(
                select(func.count()).select_from(ResultMatch).where(ResultMatch.run_id == run.id)
            )
            or 0
        )
    synced = session.get(CaseSync, (profile_id, period))
    return PeriodSummary(
        profile_id=profile_id,
        period=period,
        state=state,
        files=[file_out(v) for v in versions.values()],
        run=None if run is None else run_out(run),
        invoices=invoices,
        payments=payments,
        matched_invoices=matched,
        reconciliation_rate=(matched / invoices) if state == "reconciled" and invoices else None,
        needs_attention=sum(counts.values()),
        unverified_fixes=session.scalar(
            select(func.count())
            .select_from(ExceptionCase)
            .where(*scope, ExceptionCase.fix_unverified)
        )
        or 0,
        status_counts=status_counts,
        by_kind=[
            KindTotal(kind=kind, count=count, amounts=totals.get(kind, []))
            for kind, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        cases_up_to_date=run is not None and synced is not None and synced.run_id == run.id,
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_matches(
    session: Session,
    profile_id: uuid.UUID,
    period: str,
    *,
    search: str | None,
    limit: int,
    offset: int,
) -> MatchPage:
    check_period(period)
    get_profile(session, profile_id)
    run = find_current_run(session, profile_id, period)
    if run is None or run.status != "succeeded":
        return MatchPage(total=0, items=[])

    invoice = aliased(SourceRecord)
    conditions = [ResultMatch.run_id == run.id]
    if search:
        pattern = _like(search.strip())
        conditions.append(
            or_(
                invoice.record_id.ilike(pattern, escape="\\"),
                invoice.counterparty.ilike(pattern, escape="\\"),
            )
        )
    base = (
        select(ResultMatch.id)
        .join(invoice, invoice.id == ResultMatch.invoice_record_id)
        .where(*conditions)
    )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0

    rows = session.execute(
        select(
            ResultMatch.id,
            invoice.record_id,
            invoice.counterparty,
            invoice.occurred_on,
            invoice.amount_minor,
            invoice.currency,
            ResultMatch.variance_minor,
            ResultMatch.rule,
        )
        .join(invoice, invoice.id == ResultMatch.invoice_record_id)
        .where(*conditions)
        .order_by(invoice.record_id)
        .limit(limit)
        .offset(offset)
    ).all()

    paid_by: dict[int, list[tuple[str, int]]] = defaultdict(list)
    ids = [row.id for row in rows]
    if ids:
        for match_id, record_id, amount in session.execute(
            select(ResultMatchPayment.match_id, SourceRecord.record_id, SourceRecord.amount_minor)
            .join(SourceRecord, SourceRecord.id == ResultMatchPayment.payment_record_id)
            .where(ResultMatchPayment.match_id.in_(ids))
            .order_by(SourceRecord.record_id)
        ):
            paid_by[match_id].append((record_id, amount))

    items = []
    for row in rows:
        payments = paid_by[row.id]
        items.append(
            MatchRow(
                invoice_id=row.record_id,
                customer=row.counterparty,
                issue_date=row.occurred_on,
                amount=amount_of(row.amount_minor, row.currency),
                payment_ids=[record_id for record_id, _ in payments],
                paid=amount_of(sum(amount for _, amount in payments), row.currency),
                variance=amount_of(row.variance_minor, row.currency),
                rule=row.rule,
            )
        )
    return MatchPage(total=total, items=items)


def _record_facts(
    session: Session, versions: dict[str, SourceFileVersion], refs: Iterable[RecordKey]
) -> dict[RecordKey, SourceRecord]:
    wanted: dict[str, list[str]] = defaultdict(list)
    for role, record_id in refs:
        wanted[role].append(record_id)
    found: dict[RecordKey, SourceRecord] = {}
    for role, ids in wanted.items():
        version = versions.get(_ROLE_SOURCE[role])
        if version is None:
            continue
        for start in range(0, len(ids), 5_000):
            for record in session.scalars(
                select(SourceRecord).where(
                    SourceRecord.file_version_id == version.id,
                    SourceRecord.record_id.in_(ids[start : start + 5_000]),
                )
            ):
                found[(role, record.record_id)] = record
    return found


def _facts_for(
    refs: Sequence[RecordKey], found: dict[RecordKey, SourceRecord]
) -> list[RecordFacts]:
    facts = []
    for role, record_id in refs:
        record = found.get((role, record_id))
        facts.append(
            RecordFacts(
                role,
                record_id,
                None if record is None else record.counterparty,
                None if record is None else Money(record.amount_minor, record.currency),
            )
        )
    return facts


def _variance(case: ExceptionCase) -> Money | None:
    if case.variance_minor is None or case.currency is None:
        return None
    return Money(case.variance_minor, case.currency)


def _case_refs(session: Session, case_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[RecordKey]]:
    refs: dict[uuid.UUID, list[RecordKey]] = defaultdict(list)
    for case_id, role, record_id in session.execute(
        select(CaseRecord.case_id, CaseRecord.role, CaseRecord.record_id)
        .where(CaseRecord.case_id.in_(list(case_ids)))
        .order_by(CaseRecord.role, CaseRecord.record_id)
    ):
        refs[case_id].append((role, record_id))
    return refs


def list_case_page(
    session: Session,
    profile_id: uuid.UUID,
    period: str,
    *,
    statuses: Sequence[str] | None,
    kinds: Sequence[str] | None,
    search: str | None,
    sort: Literal["amount", "number", "updated"],
    limit: int,
    offset: int,
) -> CasePage:
    check_period(period)
    get_profile(session, profile_id)
    versions = latest_file_versions(session, profile_id, period)

    conditions = [ExceptionCase.profile_id == profile_id, ExceptionCase.period == period]
    if statuses:
        conditions.append(ExceptionCase.status.in_(list(statuses)))
    if kinds:
        conditions.append(ExceptionCase.kind.in_(list(kinds)))
    if search and search.strip():
        conditions.append(_search_condition(search.strip(), versions))

    total = session.scalar(select(func.count()).select_from(ExceptionCase).where(*conditions)) or 0
    orderings: dict[str, list[Any]] = {
        "amount": [
            func.abs(ExceptionCase.variance_minor).desc().nulls_last(),
            ExceptionCase.number,
        ],
        "number": [ExceptionCase.number],
        "updated": [ExceptionCase.updated_at.desc(), ExceptionCase.number],
    }
    order = orderings[sort]
    cases = list(
        session.scalars(
            select(ExceptionCase).where(*conditions).order_by(*order).limit(limit).offset(offset)
        )
    )

    refs = _case_refs(session, [case.id for case in cases])
    found = _record_facts(session, versions, {r for rs in refs.values() for r in rs})
    items = []
    for case in cases:
        facts = _facts_for(refs[case.id], found)
        explanation = explain(case.kind, _variance(case), facts, case.proposed_invoice_id)
        variance = _variance(case)
        items.append(
            CaseRow(
                id=case.id,
                number=case.number,
                status=case.status,
                kind=case.kind,
                summary=explanation.summary,
                customer=next((f.customer for f in facts if f.customer), None),
                variance=None if variance is None else amount_of(variance.minor, variance.currency),
                invoice_ids=[f.record_id for f in facts if f.role == "invoice"],
                payment_ids=[f.record_id for f in facts if f.role == "payment"],
                proposed_invoice_id=case.proposed_invoice_id,
                fix_unverified=case.fix_unverified,
                accepted_reason=case.accepted_reason,
                version=case.version,
                updated_at=case.updated_at,
            )
        )
    return CasePage(total=total, items=items)


def _search_condition(text: str, versions: dict[str, SourceFileVersion]):  # type: ignore[no-untyped-def]
    """Cases with a record whose id or customer contains the text, or with that number."""
    pattern = _like(text)
    clauses = []
    if text.lstrip("#").isdigit():
        clauses.append(ExceptionCase.number == int(text.lstrip("#")))
    for role, source in _ROLE_SOURCE.items():
        version = versions.get(source)
        if version is None:
            continue
        clauses.append(
            exists(
                select(1)
                .select_from(CaseRecord)
                .join(
                    SourceRecord,
                    and_(
                        SourceRecord.record_id == CaseRecord.record_id,
                        SourceRecord.file_version_id == version.id,
                    ),
                )
                .where(
                    CaseRecord.case_id == ExceptionCase.id,
                    CaseRecord.role == role,
                    or_(
                        SourceRecord.record_id.ilike(pattern, escape="\\"),
                        SourceRecord.counterparty.ilike(pattern, escape="\\"),
                    ),
                )
            )
        )
    clauses.append(
        exists(
            select(1).where(
                CaseRecord.case_id == ExceptionCase.id,
                CaseRecord.record_id.ilike(pattern, escape="\\"),
            )
        )
    )
    return or_(*clauses)


def case_detail_out(session: Session, case_id: uuid.UUID) -> CaseDetailOut:
    detail = get_case_detail(session, case_id)
    case = detail.case
    versions = latest_file_versions(session, case.profile_id, case.period)
    found = _record_facts(session, versions, detail.records)
    facts = _facts_for(detail.records, found)
    variance = _variance(case)
    explanation = explain(case.kind, variance, facts, case.proposed_invoice_id)

    records = []
    for (role, record_id), fact in zip(detail.records, facts, strict=True):
        record = found.get((role, record_id))
        records.append(
            RecordOut(
                role=role,
                record_id=record_id,
                customer=fact.customer,
                occurred_on=None if record is None else record.occurred_on,
                amount=None
                if fact.amount is None
                else amount_of(fact.amount.minor, fact.amount.currency),
                in_current_files=record is not None,
                text=None if record is None else record.free_text,
            )
        )
    return CaseDetailOut(
        id=case.id,
        number=case.number,
        profile_id=case.profile_id,
        period=case.period,
        status=case.status,
        kind=case.kind,
        variance=None if variance is None else amount_of(variance.minor, variance.currency),
        proposed_invoice_id=case.proposed_invoice_id,
        fix_unverified=case.fix_unverified,
        accepted_reason=case.accepted_reason,
        closed_reason=case.closed_reason,
        successor_case_id=case.successor_case_id,
        resolved_at=case.resolved_at,
        version=case.version,
        created_at=case.created_at,
        updated_at=case.updated_at,
        analysis=AnalysisOut(
            summary=explanation.summary,
            why=explanation.why,
            next_steps=list(explanation.next_steps),
        ),
        records=records,
        events=[
            EventOut(
                id=e.id,
                kind=e.kind,
                from_status=e.from_status,
                to_status=e.to_status,
                run_id=e.run_id,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in detail.events
        ],
        notes=[NoteOut(id=n.id, body=n.body, created_at=n.created_at) for n in detail.notes],
    )

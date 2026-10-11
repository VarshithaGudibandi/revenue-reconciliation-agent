"""Runs: create one idempotently, execute it once, store and load its results.

A run is identified by the contents of the files it reads and the profile
configuration, so asking again for the same inputs returns the same run. Executing
claims the run with a conditional update, which lets only one worker do the work;
if the work fails the run is marked failed and may be executed again.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from recon.domain.money import Money
from recon.engine.model import (
    ExceptionKind,
    Match,
    ReconciliationException,
    ReconciliationResult,
)
from recon.engine.reconcile import reconcile
from recon.persistence.models import (
    ResultException,
    ResultExceptionRecord,
    ResultMatch,
    ResultMatchPayment,
    Run,
    RunInput,
    SourceFileVersion,
    SourceRecord,
)
from recon.persistence.repository import (
    check_period,
    get_profile,
    latest_file_versions,
    load_records,
)
from recon.profiles import RECONCILIATION_SOURCES, ProfileConfig

_ERROR_LIMIT = 2_000


class MissingInputsError(ValueError):
    pass


class UnknownRunError(LookupError):
    pass


@dataclass(frozen=True)
class RunHandle:
    run: Run
    created: bool


@dataclass(frozen=True)
class RunSummary:
    run_id: uuid.UUID
    status: str
    error: str | None
    finished_at: datetime | None


def input_hash(content_hashes: Mapping[str, str], config_hash: str) -> str:
    """Identity of a run: what each source file contained, and how matching was configured."""
    canonical = json.dumps(
        {"sources": content_hashes, "config": config_hash}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def get_or_create_run(session: Session, *, profile_id: uuid.UUID, period: str) -> RunHandle:
    """The run for the latest files of a period, created if it does not exist yet."""
    check_period(period)
    profile = get_profile(session, profile_id)
    config = ProfileConfig.model_validate(profile.config)
    sources = RECONCILIATION_SOURCES[profile.reconciliation_type]

    versions = latest_file_versions(session, profile_id, period)
    missing = [name for name in sources if name not in versions]
    if missing:
        raise MissingInputsError(f"no file registered for {', '.join(missing)} in {period}")

    config_hash = config.fingerprint()
    run_hash = input_hash({name: versions[name].content_sha256 for name in sources}, config_hash)

    run_id = session.scalar(
        pg_insert(Run)
        .values(
            profile_id=profile_id,
            period=period,
            input_hash=run_hash,
            config_hash=config_hash,
            config_snapshot=config.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(constraint="uq_run_inputs")
        .returning(Run.id)
    )
    if run_id is None:
        existing = session.scalars(
            select(Run).where(
                Run.profile_id == profile_id, Run.period == period, Run.input_hash == run_hash
            )
        ).one()
        return RunHandle(existing, created=False)

    session.execute(
        insert(RunInput),
        [{"run_id": run_id, "file_version_id": versions[name].id} for name in sources],
    )
    session.flush()
    return RunHandle(session.get_one(Run, run_id), created=True)


def run_reconciliation(sessions: sessionmaker[Session], run_id: uuid.UUID) -> RunSummary:
    """Execute a run once. A run that is running or finished is returned unchanged."""
    with sessions.begin() as session:
        claimed = session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(("pending", "failed")))
            .values(status="running", started_at=func.now(), finished_at=None, error=None)
            .returning(Run.id)
        ).scalar_one_or_none()
    if claimed is None:
        with sessions() as session:
            return summarize(session, run_id)

    try:
        with sessions.begin() as session:
            _execute(session, run_id)
    except Exception as error:
        with sessions.begin() as session:
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    status="failed",
                    error=f"{type(error).__name__}: {error}"[:_ERROR_LIMIT],
                    finished_at=func.now(),
                )
            )
        raise

    with sessions() as session:
        return summarize(session, run_id)


def summarize(session: Session, run_id: uuid.UUID) -> RunSummary:
    run = session.get(Run, run_id)
    if run is None:
        raise UnknownRunError(f"no run {run_id}")
    return RunSummary(run.id, run.status, run.error, run.finished_at)


def _execute(session: Session, run_id: uuid.UUID) -> None:
    run = session.get_one(Run, run_id)
    versions = {
        version.source_name: version
        for version in session.scalars(
            select(SourceFileVersion)
            .join(RunInput, RunInput.file_version_id == SourceFileVersion.id)
            .where(RunInput.run_id == run_id)
        )
    }
    config = ProfileConfig.model_validate(run.config_snapshot)
    invoices = load_records(session, versions["invoices"].id)
    payments = load_records(session, versions["payments"].id)

    result = reconcile(invoices, payments, config.engine_config())
    _save_result(session, run_id, versions["invoices"].id, versions["payments"].id, result)

    session.execute(
        update(Run).where(Run.id == run_id).values(status="succeeded", finished_at=func.now())
    )


def _save_result(
    session: Session,
    run_id: uuid.UUID,
    invoice_version: uuid.UUID,
    payment_version: uuid.UUID,
    result: ReconciliationResult,
) -> None:
    invoice_ids = _record_ids(session, invoice_version)
    payment_ids = _record_ids(session, payment_version)

    if result.matches:
        rows = [
            {
                "run_id": run_id,
                "invoice_record_id": invoice_ids[m.invoice_id],
                "variance_minor": m.variance.minor,
                "currency": m.variance.currency,
                "rule": m.rule,
            }
            for m in result.matches
        ]
        stored = dict(
            session.execute(
                insert(ResultMatch).returning(ResultMatch.invoice_record_id, ResultMatch.id), rows
            ).all()
        )
        session.execute(
            insert(ResultMatchPayment),
            [
                {"match_id": stored[invoice_ids[m.invoice_id]], "payment_record_id": payment_ids[p]}
                for m in result.matches
                for p in m.payment_ids
            ],
        )

    if result.exceptions:
        exception_ids = session.scalars(
            insert(ResultException).returning(ResultException.id, sort_by_parameter_order=True),
            [
                {
                    "run_id": run_id,
                    "kind": e.kind.value,
                    "variance_minor": None if e.variance is None else e.variance.minor,
                    "currency": None if e.variance is None else e.variance.currency,
                    "proposed_invoice_record_id": (
                        None
                        if e.proposed_invoice_id is None
                        else invoice_ids[e.proposed_invoice_id]
                    ),
                }
                for e in result.exceptions
            ],
        ).all()
        links = [
            {"exception_id": exception_id, "record_id": record_id, "role": role}
            for exception_id, e in zip(exception_ids, result.exceptions, strict=True)
            for role, ids, lookup in (
                ("invoice", e.invoice_ids, invoice_ids),
                ("payment", e.payment_ids, payment_ids),
            )
            for record_id in (lookup[i] for i in ids)
        ]
        if links:
            session.execute(insert(ResultExceptionRecord), links)


def _record_ids(session: Session, file_version_id: uuid.UUID) -> dict[str, int]:
    return dict(
        session.execute(
            select(SourceRecord.record_id, SourceRecord.id).where(
                SourceRecord.file_version_id == file_version_id
            )
        ).all()
    )


def load_result(session: Session, run_id: uuid.UUID) -> ReconciliationResult:
    """The stored result of a run, in the same order the engine returns it."""
    names = {
        row_id: record_id
        for row_id, record_id in session.execute(
            select(SourceRecord.id, SourceRecord.record_id)
            .join(RunInput, RunInput.file_version_id == SourceRecord.file_version_id)
            .where(RunInput.run_id == run_id)
        )
    }

    payments_by_match: dict[int, list[str]] = {}
    for match_id, payment_record_id in session.execute(
        select(ResultMatchPayment.match_id, ResultMatchPayment.payment_record_id)
        .join(ResultMatch, ResultMatch.id == ResultMatchPayment.match_id)
        .where(ResultMatch.run_id == run_id)
    ):
        payments_by_match.setdefault(match_id, []).append(names[payment_record_id])

    matches = [
        Match(
            invoice_id=names[match.invoice_record_id],
            payment_ids=tuple(sorted(payments_by_match[match.id])),
            variance=Money(match.variance_minor, match.currency),
            rule=match.rule,
        )
        for match in session.scalars(select(ResultMatch).where(ResultMatch.run_id == run_id))
    ]

    linked: dict[int, dict[str, list[str]]] = {}
    for exception_id, record_id, role in session.execute(
        select(
            ResultExceptionRecord.exception_id,
            ResultExceptionRecord.record_id,
            ResultExceptionRecord.role,
        )
        .join(ResultException, ResultException.id == ResultExceptionRecord.exception_id)
        .where(ResultException.run_id == run_id)
    ):
        linked.setdefault(exception_id, {"invoice": [], "payment": []})[role].append(
            names[record_id]
        )

    exceptions = []
    for stored in session.scalars(select(ResultException).where(ResultException.run_id == run_id)):
        records = linked.get(stored.id, {"invoice": [], "payment": []})
        variance = (
            None
            if stored.variance_minor is None or stored.currency is None
            else Money(stored.variance_minor, stored.currency)
        )
        exceptions.append(
            ReconciliationException(
                kind=ExceptionKind(stored.kind),
                invoice_ids=tuple(sorted(records["invoice"])),
                payment_ids=tuple(sorted(records["payment"])),
                variance=variance,
                proposed_invoice_id=(
                    None
                    if stored.proposed_invoice_record_id is None
                    else names[stored.proposed_invoice_record_id]
                ),
            )
        )

    matches.sort(key=lambda m: m.invoice_id)
    exceptions.sort(key=lambda e: (e.invoice_ids, e.payment_ids, e.kind))
    return ReconciliationResult(tuple(matches), tuple(exceptions))

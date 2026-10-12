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
from typing import Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from recon.domain.money import Money
from recon.engine.incremental import merge_incremental
from recon.engine.model import (
    EngineConfig,
    ExceptionKind,
    Match,
    ReconciliationException,
    ReconciliationResult,
)
from recon.engine.registry import Engine, engine_for
from recon.ingestion.diff import diff_fingerprints
from recon.persistence.models import (
    Profile,
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
    load_fingerprints,
    load_records,
)
from recon.profiles import RECONCILIATION_SOURCES, ProfileConfig, reconciliation_type

_ERROR_LIMIT = 2_000


class MissingInputsError(ValueError):
    pass


class UnknownRunError(LookupError):
    pass


class IncrementalMismatchError(RuntimeError):
    """The incrementally computed result differs from a full recompute."""


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
    mode: str | None = None
    base_run_id: uuid.UUID | None = None
    partitions_total: int | None = None
    partitions_recomputed: int | None = None


def input_hash(content_hashes: Mapping[str, str], config_hash: str) -> str:
    """Identity of a run: what each source file contained, and how matching was configured."""
    canonical = json.dumps(
        {"sources": content_hashes, "config": config_hash}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CurrentInputs:
    config: ProfileConfig
    config_hash: str
    versions: dict[str, SourceFileVersion]
    input_hash: str


def current_inputs(session: Session, profile_id: uuid.UUID, period: str) -> CurrentInputs:
    """What a run for the latest files of a period would read, and its identity."""
    check_period(period)
    profile = get_profile(session, profile_id)
    config = ProfileConfig.model_validate(profile.config)
    sources = RECONCILIATION_SOURCES[profile.reconciliation_type]

    versions = latest_file_versions(session, profile_id, period)
    missing = [name for name in sources if name not in versions]
    if missing:
        raise MissingInputsError(f"no file registered for {', '.join(missing)} in {period}")

    config_hash = config.fingerprint()
    content = {name: versions[name].content_sha256 for name in sources}
    return CurrentInputs(
        config,
        config_hash,
        {name: versions[name] for name in sources},
        input_hash(content, config_hash),
    )


def find_current_run(session: Session, profile_id: uuid.UUID, period: str) -> Run | None:
    """The run for the latest files of a period, if there is one."""
    try:
        inputs = current_inputs(session, profile_id, period)
    except MissingInputsError:
        return None
    return session.scalars(
        select(Run).where(
            Run.profile_id == profile_id,
            Run.period == period,
            Run.input_hash == inputs.input_hash,
        )
    ).first()


def get_or_create_run(session: Session, *, profile_id: uuid.UUID, period: str) -> RunHandle:
    """The run for the latest files of a period, created if it does not exist yet."""
    inputs = current_inputs(session, profile_id, period)
    config, config_hash, run_hash = inputs.config, inputs.config_hash, inputs.input_hash
    versions = inputs.versions
    sources = tuple(versions)

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


def run_reconciliation(
    sessions: sessionmaker[Session],
    run_id: uuid.UUID,
    *,
    mode: Literal["auto", "full"] = "auto",
    verify: bool = False,
) -> RunSummary:
    """Execute a run once. A run that is running or finished is returned unchanged.

    In ``auto`` mode a run reuses the result of the latest earlier run with the same
    configuration for the customers whose records did not change; ``full`` matches
    everything again. ``verify`` also matches everything and fails the run if the two
    results differ, which is a safety net while the incremental path is young.
    """
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
            _execute(session, run_id, mode, verify)
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
    return RunSummary(
        run.id,
        run.status,
        run.error,
        run.finished_at,
        run.mode,
        run.base_run_id,
        run.partitions_total,
        run.partitions_recomputed,
    )


def _execute(session: Session, run_id: uuid.UUID, mode: str, verify: bool) -> None:
    run = session.get_one(Run, run_id)
    versions = run_versions(session, run_id)
    engine_config = ProfileConfig.model_validate(run.config_snapshot).engine_config()
    profile = session.get_one(Profile, run.profile_id)
    kind = reconciliation_type(profile.reconciliation_type)
    engine = engine_for(kind.key)
    left, right = kind.left, kind.right

    base = _find_base_run(session, run) if mode == "auto" else None
    if base is None:
        left_records = load_records(session, versions[left].id)
        right_records = load_records(session, versions[right].id)
        result = engine(left_records, right_records, engine_config)
        total = len({record.counterparty for record in (*left_records, *right_records)})
        recomputed = total
    else:
        result, total, recomputed = _reconcile_incrementally(
            session, base, versions, engine_config, verify, (left, right), engine
        )

    _save_result(session, run_id, versions[left].id, versions[right].id, result)
    session.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(
            status="succeeded",
            finished_at=func.now(),
            mode="full" if base is None else "incremental",
            base_run_id=None if base is None else base.id,
            partitions_total=total,
            partitions_recomputed=recomputed,
        )
    )


def run_versions(session: Session, run_id: uuid.UUID) -> dict[str, SourceFileVersion]:
    return {
        version.source_name: version
        for version in session.scalars(
            select(SourceFileVersion)
            .join(RunInput, RunInput.file_version_id == SourceFileVersion.id)
            .where(RunInput.run_id == run_id)
        )
    }


def _find_base_run(session: Session, run: Run) -> Run | None:
    """The latest finished run of the period computed with the same configuration."""
    return session.scalars(
        select(Run)
        .where(
            Run.profile_id == run.profile_id,
            Run.period == run.period,
            Run.config_hash == run.config_hash,
            Run.status == "succeeded",
            Run.id != run.id,
        )
        .order_by(Run.finished_at.desc())
        .limit(1)
    ).first()


def _reconcile_incrementally(
    session: Session,
    base: Run,
    versions: dict[str, SourceFileVersion],
    config: EngineConfig,
    verify: bool,
    sides: tuple[str, str],
    engine: Engine,
) -> tuple[ReconciliationResult, int, int]:
    left, right = sides
    base_versions = run_versions(session, base.id)

    dirty: set[str] = set()
    present: set[str] = set()
    counterparties: dict[str, dict[str, str]] = {}
    for source in sides:
        old = load_fingerprints(session, base_versions[source].id)
        new = load_fingerprints(session, versions[source].id)
        dirty |= diff_fingerprints(old, new).counterparties
        present |= {fingerprint.counterparty for fingerprint in new}
        # A record that moved appears under both counterparties; both are dirty, so
        # which one the map holds does not matter.
        counterparties[source] = {f.record_id: f.counterparty for f in old}
        counterparties[source].update({f.record_id: f.counterparty for f in new})

    recomputed = engine(
        load_records(session, versions[left].id, dirty),
        load_records(session, versions[right].id, dirty),
        config,
    )
    result = merge_incremental(
        load_result(session, base.id),
        counterparties[left],
        counterparties[right],
        dirty,
        recomputed,
    )

    if verify:
        full = engine(
            load_records(session, versions[left].id),
            load_records(session, versions[right].id),
            config,
        )
        if full != result:
            raise IncrementalMismatchError(
                f"incremental result differs from a full recompute (base run {base.id})"
            )
    return result, len(present), len(dirty & present)


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

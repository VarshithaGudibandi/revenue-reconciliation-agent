"""Storage operations for profiles, source file versions and records.

Functions take a session and never commit, so the caller decides the transaction
boundary. Registering a file takes a transaction-scoped advisory lock for its source
and period, so concurrent registrations cannot produce duplicate or clashing versions.
"""

import hashlib
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recon.domain.money import Money
from recon.ingestion.diff import Fingerprint
from recon.ingestion.normalize import (
    NormalizedRecord,
    RejectedRow,
    normalize_csv_bytes,
)
from recon.persistence.models import Profile, SourceFileVersion, SourceRecord
from recon.persistence.models import RejectedRow as RejectedRowModel
from recon.profiles import RECONCILIATION_SOURCES, ProfileConfig

_PERIOD = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_INSERT_CHUNK = 5_000
_IN_CHUNK = 10_000


def check_period(period: str) -> None:
    if not _PERIOD.fullmatch(period):
        raise ValueError(f"period must look like YYYY-MM, got {period!r}")


class ProfileExistsError(ValueError):
    pass


class UnknownProfileError(LookupError):
    pass


class UnknownSourceError(ValueError):
    pass


@dataclass(frozen=True)
class Registration:
    version: SourceFileVersion
    created: bool


def create_profile(
    session: Session, *, name: str, reconciliation_type: str, config: ProfileConfig
) -> Profile:
    sources = RECONCILIATION_SOURCES.get(reconciliation_type)
    if sources is None:
        raise ValueError(f"unknown reconciliation type {reconciliation_type!r}")
    if set(config.sources) != set(sources):
        raise ValueError(
            f"{reconciliation_type} reads sources {sorted(sources)}, got {sorted(config.sources)}"
        )

    profile = Profile(
        name=name, reconciliation_type=reconciliation_type, config=config.model_dump(mode="json")
    )
    try:
        with session.begin_nested():
            session.add(profile)
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "uq_profile_name":
            raise ProfileExistsError(f"a profile named {name!r} already exists") from error
        raise
    return profile


def get_profile(session: Session, profile_id: uuid.UUID) -> Profile:
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise UnknownProfileError(f"no profile {profile_id}")
    return profile


def register_file(
    session: Session,
    *,
    profile_id: uuid.UUID,
    source_name: str,
    period: str,
    path: Path,
    object_key: str | None = None,
) -> Registration:
    """Record the current content of a source file for a period.

    Content equal to the latest version changes nothing. Anything else, including
    content that matches an older version, becomes the next version.
    """
    check_period(period)
    profile = get_profile(session, profile_id)
    mapping = ProfileConfig.model_validate(profile.config).sources.get(source_name)
    if mapping is None:
        raise UnknownSourceError(f"profile {profile.name!r} has no source {source_name!r}")

    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"file-version:{profile_id}:{source_name}:{period}"},
    )
    latest = _latest_version(session, profile_id, source_name, period)
    if latest is not None and latest.content_sha256 == digest:
        return Registration(latest, created=False)

    result = normalize_csv_bytes(data, path.name, mapping)
    version = SourceFileVersion(
        profile_id=profile_id,
        source_name=source_name,
        period=period,
        version_number=1 if latest is None else latest.version_number + 1,
        content_sha256=digest,
        object_key=object_key or path.name,
        size_bytes=len(data),
        record_count=len(result.records),
        rejected_count=len(result.rejected),
    )
    session.add(version)
    session.flush()

    record_rows = [
        {
            "file_version_id": version.id,
            "row_number": record.source_row,
            "record_id": record.record_id,
            "reference": record.reference,
            "counterparty": record.counterparty,
            "occurred_on": record.occurred_on,
            "amount_minor": record.amount.minor,
            "currency": record.amount.currency,
            "content_hash": record.content_hash,
        }
        for record in result.records
    ]
    rejected_rows = [
        {
            "file_version_id": version.id,
            "row_number": row.source_row,
            "reason": row.reason,
            "raw": dict(row.raw),
        }
        for row in result.rejected
    ]
    for start in range(0, len(record_rows), _INSERT_CHUNK):
        session.execute(insert(SourceRecord), record_rows[start : start + _INSERT_CHUNK])
    for start in range(0, len(rejected_rows), _INSERT_CHUNK):
        session.execute(insert(RejectedRowModel), rejected_rows[start : start + _INSERT_CHUNK])
    return Registration(version, created=True)


def _latest_version(
    session: Session, profile_id: uuid.UUID, source_name: str, period: str
) -> SourceFileVersion | None:
    return session.scalars(
        select(SourceFileVersion)
        .where(
            SourceFileVersion.profile_id == profile_id,
            SourceFileVersion.source_name == source_name,
            SourceFileVersion.period == period,
        )
        .order_by(SourceFileVersion.version_number.desc())
        .limit(1)
    ).first()


def latest_file_versions(
    session: Session, profile_id: uuid.UUID, period: str
) -> dict[str, SourceFileVersion]:
    """The newest version of every source that has a file for the period."""
    versions = session.scalars(
        select(SourceFileVersion)
        .where(SourceFileVersion.profile_id == profile_id, SourceFileVersion.period == period)
        .ext(distinct_on(SourceFileVersion.source_name))
        .order_by(SourceFileVersion.source_name, SourceFileVersion.version_number.desc())
    )
    return {version.source_name: version for version in versions}


def load_records(
    session: Session,
    file_version_id: uuid.UUID,
    counterparties: Collection[str] | None = None,
) -> list[NormalizedRecord]:
    """The records of a file version, optionally only those of the given counterparties."""
    if counterparties is None:
        batches: list[list[str] | None] = [None]
    else:
        wanted = sorted(counterparties)
        batches = [wanted[i : i + _IN_CHUNK] for i in range(0, len(wanted), _IN_CHUNK)]

    rows: list[SourceRecord] = []
    for batch in batches:
        query = select(SourceRecord).where(SourceRecord.file_version_id == file_version_id)
        if batch is not None:
            query = query.where(SourceRecord.counterparty.in_(batch))
        rows.extend(session.scalars(query))
    rows.sort(key=lambda row: row.row_number)
    return [
        NormalizedRecord(
            source_row=row.row_number,
            record_id=row.record_id,
            reference=row.reference,
            counterparty=row.counterparty,
            occurred_on=row.occurred_on,
            amount=Money(row.amount_minor, row.currency),
        )
        for row in rows
    ]


def load_fingerprints(session: Session, file_version_id: uuid.UUID) -> list[Fingerprint]:
    """Record ids, counterparties and content hashes of a file version, without the rest."""
    return [
        Fingerprint(record_id, counterparty, content_hash)
        for record_id, counterparty, content_hash in session.execute(
            select(
                SourceRecord.record_id, SourceRecord.counterparty, SourceRecord.content_hash
            ).where(SourceRecord.file_version_id == file_version_id)
        )
    ]


def load_rejected(session: Session, file_version_id: uuid.UUID) -> list[RejectedRow]:
    rows = session.scalars(
        select(RejectedRowModel)
        .where(RejectedRowModel.file_version_id == file_version_id)
        .order_by(RejectedRowModel.row_number)
    )
    return [RejectedRow(row.row_number, row.reason, row.raw) for row in rows]

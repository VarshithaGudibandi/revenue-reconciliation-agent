"""Looking through a bucket for files to register.

For every object whose key and ETag have not been dealt with, find the profile, period and source
from its name, download it and register it as a file version. The result for each object is kept
in ``ingested_object``, so a second scan reads nothing it has already read, an object that is
overwritten counts as new, and an object that cannot be placed says why instead of vanishing.

When a registration completes a period (every source of the profile has a file), a reconciliation
job is queued for it.
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recon.ingest.naming import BadKeyError, parse_key, slugify
from recon.ingest.store import ObjectInfo, ObjectStore, ObjectTooLargeError
from recon.ingestion.normalize import MappingError
from recon.jobs.kinds import enqueue_reconcile
from recon.persistence.models import IngestedObject, Profile
from recon.persistence.repository import latest_file_versions, register_file
from recon.profiles import RECONCILIATION_SOURCES

log = logging.getLogger("recon.ingest")

FINAL = ("registered", "unchanged", "failed")


@dataclass
class ScanResult:
    seen: int = 0
    already_done: int = 0
    registered: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    reconciliations_queued: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "already_done": self.already_done,
            "registered": self.registered,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "reconciliations_queued": self.reconciliations_queued,
        }


def _profiles_by_slug(session: Session) -> dict[str, list[Profile]]:
    found: dict[str, list[Profile]] = defaultdict(list)
    for profile in session.scalars(select(Profile)):
        found[slugify(profile.name)].append(profile)
    return found


def _record(
    session: Session,
    store: ObjectStore,
    info: ObjectInfo,
    existing: IngestedObject | None,
    *,
    status: str,
    now: datetime,
    reason: str | None = None,
    profile_id: uuid.UUID | None = None,
    period: str | None = None,
    source: str | None = None,
    version_id: uuid.UUID | None = None,
) -> None:
    row = existing or IngestedObject(
        bucket=store.bucket, object_key=info.key, etag=info.etag, size_bytes=info.size
    )
    row.status = status
    row.reason = reason
    row.profile_id = profile_id
    row.period = period
    row.source = source
    row.file_version_id = version_id
    row.processed_at = now
    session.add(row)
    session.flush()


def scan_bucket(
    sessions: sessionmaker[Session],
    store: ObjectStore,
    *,
    prefix: str,
    storage_dir: Path,
    max_bytes: int,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> ScanResult:
    """One pass over the bucket. Safe to repeat and to run from several workers at once: a
    version is registered only once, whoever gets there first."""
    result = ScanResult()
    completed: set[tuple[uuid.UUID, str]] = set()
    for info in store.list(prefix):
        result.seen += 1
        when = now or datetime.now(UTC)
        with sessions.begin() as session:
            existing = session.scalars(
                select(IngestedObject).where(
                    IngestedObject.bucket == store.bucket,
                    IngestedObject.object_key == info.key,
                    IngestedObject.etag == info.etag,
                )
            ).one_or_none()
            if existing is not None and existing.status in FINAL:
                result.already_done += 1
                continue
            outcome = _ingest(session, store, info, existing, prefix, storage_dir, max_bytes, when)
        match outcome:
            case ("registered" | "unchanged", profile_id, period) as done:
                if done[0] == "registered":
                    result.registered += 1
                    completed.add((profile_id, period))
                else:
                    result.unchanged += 1
            case ("skipped", reason):
                result.skipped += 1
                result.reasons.append(f"{info.key}: {reason}")
            case ("failed", reason):
                result.failed += 1
                result.reasons.append(f"{info.key}: {reason}")

    for profile_id, period in completed:
        with sessions.begin() as session:
            profile = session.get_one(Profile, profile_id)
            needed = RECONCILIATION_SOURCES[profile.reconciliation_type]
            if all(name in latest_file_versions(session, profile_id, period) for name in needed):
                enqueue_reconcile(
                    session,
                    profile_id,
                    period,
                    now=now or datetime.now(UTC),
                    max_attempts=max_attempts,
                )
                result.reconciliations_queued += 1
    return result


def _ingest(
    session: Session,
    store: ObjectStore,
    info: ObjectInfo,
    existing: IngestedObject | None,
    prefix: str,
    storage_dir: Path,
    max_bytes: int,
    now: datetime,
) -> tuple[Any, ...]:
    def skip(reason: str) -> tuple[Any, ...]:
        _record(session, store, info, existing, status="skipped", reason=reason, now=now)
        return ("skipped", reason)

    try:
        parsed = parse_key(info.key, prefix)
    except BadKeyError as error:
        return skip(str(error))
    matches = _profiles_by_slug(session).get(parsed.profile, [])
    if not matches:
        return skip(f"no profile is named {parsed.profile!r}")
    if len(matches) > 1:
        return skip(f"{len(matches)} profiles share the name {parsed.profile!r}")
    profile = matches[0]
    if parsed.source not in RECONCILIATION_SOURCES[profile.reconciliation_type]:
        return skip(f"profile {profile.name!r} has no source {parsed.source!r}")
    if info.size > max_bytes:
        return skip(f"the object is {info.size} bytes; the limit is {max_bytes}")

    def failed(reason: str) -> tuple[Any, ...]:
        _record(
            session,
            store,
            info,
            existing,
            status="failed",
            reason=reason,
            now=now,
            profile_id=profile.id,
            period=parsed.period,
            source=parsed.source,
        )
        return ("failed", reason)

    try:
        data = store.get(info.key, max_bytes)
    except ObjectTooLargeError as error:
        return skip(str(error))
    path = storage_dir / "ingest" / f"{uuid.uuid4().hex}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    keep = False
    try:
        registration = register_file(
            session,
            profile_id=profile.id,
            source_name=parsed.source,
            period=parsed.period,
            path=path,
            object_key=f"ingest/{path.name}",
        )
        keep = registration.created
    except (MappingError, ValueError) as error:
        return failed(f"the file could not be read: {error}")
    finally:
        if not keep:
            path.unlink(missing_ok=True)
    status = "registered" if registration.created else "unchanged"
    _record(
        session,
        store,
        info,
        existing,
        status=status,
        now=now,
        profile_id=profile.id,
        period=parsed.period,
        source=parsed.source,
        version_id=registration.version.id,
    )
    log.info(
        "%s %s as %s %s v%s",
        status,
        info.key,
        profile.name,
        parsed.source,
        registration.version.version_number,
    )
    return (status, profile.id, parsed.period)

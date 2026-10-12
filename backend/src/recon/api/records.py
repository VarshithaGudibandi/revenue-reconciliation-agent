"""Looking up the source records that cases and suggestions refer to."""

from collections import defaultdict
from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from recon.persistence.models import SourceFileVersion, SourceRecord

RecordKey = tuple[str, str]


def source_records(
    session: Session,
    versions: dict[str, SourceFileVersion],
    refs: Iterable[RecordKey],
    roles: Mapping[str, str],
) -> dict[RecordKey, SourceRecord]:
    """The records that ``refs`` (role, id) name. ``roles`` says which file each role reads."""
    wanted: dict[str, list[str]] = defaultdict(list)
    for role, record_id in refs:
        wanted[role].append(record_id)
    found: dict[RecordKey, SourceRecord] = {}
    for role, ids in wanted.items():
        version = versions.get(roles[role])
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

"""Compare two versions of a source file record by record."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Fingerprint:
    """Just enough of a record to tell whether it changed and which partition it is in."""

    record_id: str
    counterparty: str
    content_hash: str


@dataclass(frozen=True)
class VersionDiff:
    added: frozenset[str]
    removed: frozenset[str]
    changed: frozenset[str]
    # Every counterparty with at least one added, removed or changed record, counting both
    # the old and the new counterparty of a record that moved.
    counterparties: frozenset[str]


def diff_fingerprints(old: Iterable[Fingerprint], new: Iterable[Fingerprint]) -> VersionDiff:
    before = {f.record_id: f for f in old}
    after = {f.record_id: f for f in new}

    added = frozenset(after.keys() - before.keys())
    removed = frozenset(before.keys() - after.keys())
    changed = frozenset(
        record_id
        for record_id in before.keys() & after.keys()
        if before[record_id].content_hash != after[record_id].content_hash
    )

    counterparties = {after[record_id].counterparty for record_id in added | changed}
    counterparties |= {before[record_id].counterparty for record_id in removed | changed}
    return VersionDiff(added, removed, changed, frozenset(counterparties))

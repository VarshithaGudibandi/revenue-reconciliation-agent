"""Decide which case each exception of a new run belongs to.

A case follows a problem as it changes, so exceptions are matched to cases by the
records they share, not by their kind or amounts. Matching on the kind would turn an
unpaid invoice that receives a partial payment into a new problem and close the old
one as resolved, although the invoice is still unreconciled.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

# A record in a case: its role and the id it has in its source file.
RecordRef = tuple[str, str]

ACTIVE_STATUSES = frozenset({"open", "fix_reported", "accepted"})


@dataclass(frozen=True)
class CaseView:
    case_id: str
    status: str
    closed_reason: str | None
    created_rank: int
    records: frozenset[RecordRef]


@dataclass(frozen=True)
class Assignment:
    case_for: dict[int, str]
    new: tuple[int, ...]
    # Active cases that no exception of the new run belongs to.
    unassigned: tuple[str, ...]


def assign(cases: Sequence[CaseView], exceptions: Sequence[frozenset[RecordRef]]) -> Assignment:
    """Give each exception at most one case, and each case at most one exception.

    Exceptions are taken in the order given. An exception prefers an active case over a
    closed one, so a problem that comes back reopens its old case; then the case sharing
    the most records, then the oldest. A case that was merged into another is never used.
    """
    candidates = [c for c in cases if not (c.status == "withdrawn" and c.closed_reason == "merged")]
    by_record: dict[RecordRef, list[CaseView]] = defaultdict(list)
    for case in candidates:
        for record in case.records:
            by_record[record].append(case)

    claimed: set[str] = set()
    case_for: dict[int, str] = {}
    new: list[int] = []
    for index, records in enumerate(exceptions):
        overlap: Counter[str] = Counter()
        by_id: dict[str, CaseView] = {}
        for record in records:
            for case in by_record.get(record, ()):
                if case.case_id not in claimed:
                    overlap[case.case_id] += 1
                    by_id[case.case_id] = case
        if not overlap:
            new.append(index)
            continue
        best = min(
            by_id.values(),
            key=lambda c: (
                0 if c.status in ACTIVE_STATUSES else 1,
                -overlap[c.case_id],
                c.created_rank,
                c.case_id,
            ),
        )
        case_for[index] = best.case_id
        claimed.add(best.case_id)

    unassigned = sorted(
        (c for c in candidates if c.status in ACTIVE_STATUSES and c.case_id not in claimed),
        key=lambda c: (c.created_rank, c.case_id),
    )
    return Assignment(case_for, tuple(new), tuple(c.case_id for c in unassigned))

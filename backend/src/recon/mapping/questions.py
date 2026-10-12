"""Which question to put to the person next.

Every open question could be answered one of a few ways. For each way, the proposal is worked out
again with that answer pinned, and what is still open is counted. The question asked first is the
one whose answers leave the fewest others open on average, so that an answer which settles other
questions comes before one that settles only itself.

Every plausible answer is counted equally. That is a simplification: it ignores that the first
choice is likelier than the alternatives.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from recon.mapping.answers import Answer, Pins
from recon.mapping.proposal import OPTIONAL_FIELDS, REQUIRED_FIELDS, MappingProposal

MAX_OPTIONS = 6
Proposer = Callable[[Pins], MappingProposal]


@dataclass(frozen=True)
class Question:
    source: str
    field: str
    options: tuple[str | None, ...]
    open_now: int
    expected_open: float
    best_open: int = 0

    @property
    def expected_settled(self) -> float:
        """How many other questions an answer settles, on average."""
        return self.open_now - 1 - self.expected_open


def options_for(
    proposal: MappingProposal, source: str, field: str, headers: Mapping[str, tuple[str, ...]]
) -> tuple[str | None, ...]:
    """The answers worth trying for a question: the offered columns, or, when none was offered,
    the file's columns. An optional field may also have no column."""
    file = getattr(proposal, source)
    if field == "decimal_separator":
        return (".", ",")
    if field == "date_format":
        found = tuple(
            dict.fromkeys(
                (
                    *([file.date_format.value] if file.date_format.value else []),
                    *file.date_format.alternatives,
                )
            )
        )
        return found
    choice = file.choice(field)
    found_columns = tuple(
        dict.fromkeys(([choice.column] if choice.column else []) + list(choice.alternatives))
    )
    columns = found_columns or tuple(headers[source])
    extra: tuple[str | None, ...] = (None,) if field in OPTIONAL_FIELDS else ()
    return (*columns[:MAX_OPTIONS], *extra)


def choose_question(
    propose: Proposer,
    pins: Pins,
    headers: Mapping[str, tuple[str, ...]],
) -> Question | None:
    """The question to ask next, or None when nothing is open."""
    current = propose(pins)
    items = current.open_items()
    if not items:
        return None
    best: Question | None = None
    best_key: tuple[int, float, int, int] | None = None
    for position, (source, field) in enumerate(items):
        options = options_for(current, source, field, headers)
        if not options:
            continue
        left = []
        for value in options:
            trial = {**pins, (source, field): value}
            left.append(len(propose(trial).open_items()))
        expected = sum(left) / len(left)
        required = (
            0 if field in REQUIRED_FIELDS or field in ("date_format", "decimal_separator") else 1
        )
        key = (min(left), expected, required, position)
        if best_key is None or key < best_key:
            best_key = key
            best = Question(source, field, options, len(items), expected, min(left))
    if best is None:
        source, field = items[0]
        return Question(source, field, (), len(items), float(len(items) - 1))
    return best


def settled_by(
    before: MappingProposal, after: MappingProposal, answered: Answer
) -> list[tuple[str, str]]:
    """The questions that were open before an answer, are not now, and were not the one answered."""
    now = set(after.open_items())
    return [
        item
        for item in before.open_items()
        if item not in now and item != (answered.source, answered.field)
    ]

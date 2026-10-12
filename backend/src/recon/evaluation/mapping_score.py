"""Judge a mapping proposal against the known right mapping.

The outcomes separate the failures that matter:

* ``correct``: confident and right.
* ``asked``: not confident, and the right answer was among the offered ones. The user would
  be asked, and could answer from the options.
* ``missed``: not confident, and the right answer was not offered.
* ``wrong``: confident and wrong. This is the dangerous one, because nobody is asked.
"""

from collections.abc import Mapping
from typing import Literal

from recon.ingestion.normalize import ColumnMapping
from recon.mapping.proposal import FIELDS, OPTIONAL_FIELDS, FileProposal

Outcome = Literal["correct", "asked", "missed", "wrong"]
FILE_OUTCOMES = ("exact", "needs_user", "missed", "wrong")
SETTINGS = ("date_format", "decimal_separator")


def judge(
    proposed: str | None, confident: bool, alternatives: tuple[str, ...], truth: str | None
) -> Outcome:
    if confident:
        return "correct" if proposed == truth else "wrong"
    if truth is None:
        # Asked whether there is such a column at all.
        return "asked"
    return "asked" if truth == proposed or truth in alternatives else "missed"


def score_file(proposal: FileProposal, truth: ColumnMapping) -> dict[str, Outcome]:
    expected = truth.model_dump()
    outcomes: dict[str, Outcome] = {}
    for field in FIELDS:
        if field in OPTIONAL_FIELDS and field not in proposal.fields and expected[field] is None:
            # A file with no such column has nothing to say about it.
            outcomes[field] = "correct"
            continue
        choice = proposal.choice(field)
        outcomes[field] = judge(
            choice.column, choice.confident, choice.alternatives, expected[field]
        )
    for setting in SETTINGS:
        value = getattr(proposal, setting)
        outcomes[setting] = judge(
            value.value, value.confident, value.alternatives, expected[setting]
        )
    return outcomes


def classify(outcomes: Mapping[str, str]) -> str:
    """One word for a whole file: exact, needs_user, missed or wrong (worst outcome wins)."""
    values = set(outcomes.values())
    if "wrong" in values:
        return "wrong"
    if "missed" in values:
        return "missed"
    return "needs_user" if "asked" in values else "exact"

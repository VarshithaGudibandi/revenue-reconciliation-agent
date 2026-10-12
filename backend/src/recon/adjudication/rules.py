"""An adjudicator that uses no language model: invoice numbers and the words of the invoice.

It abstains when the memo does not tell the candidates apart, and it never decides from the
order or the dates of the invoices alone.
"""

import re

from recon.adjudication.case import AdjudicationCase, Candidate, Ruling

_YEARS = range(1990, 2101)
_WORD = re.compile(r"[a-zà-ÿ]{4,}")


def numbers_in(text: str) -> set[int]:
    """The whole numbers written in a text, leaving out anything that reads as a year."""
    return {n for n in (int(run) for run in re.findall(r"\d+", text)) if n not in _YEARS}


def service_words(candidate: Candidate) -> set[str]:
    """The words of an invoice description that name its service, without the month and year."""
    service = candidate.description.rsplit(" ", 2)[0]
    return set(_WORD.findall(service.lower()))


def adjudicate_by_rules(case: AdjudicationCase) -> Ruling:
    written = numbers_in(case.memo)
    named = [c for c in case.candidates if c.number in written]
    if len(named) == 1:
        winner = named[0]
        return Ruling(
            winner.label,
            True,
            evidence=str(winner.number),
            reasons=(f"The memo names invoice number {winner.number}",),
        )
    if len(named) > 1:
        return Ruling(
            None,
            False,
            reasons=("The memo names more than one of the candidates",),
            alternatives=tuple(c.label for c in named),
        )

    words = set(_WORD.findall(case.memo.lower()))
    matching = [c for c in case.candidates if service_words(c) & words]
    if len(matching) == 1:
        winner = matching[0]
        shared = sorted(service_words(winner) & words)
        return Ruling(
            winner.label,
            True,
            evidence=shared[0],
            reasons=(f"The memo uses the word '{shared[0]}', which only one invoice has",),
        )
    return Ruling(
        None,
        False,
        reasons=("Nothing in the memo separates the candidates",),
        alternatives=tuple(c.label for c in matching),
    )

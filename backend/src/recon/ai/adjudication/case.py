"""A payment that could settle any of several invoices, and what is said about it.

The engine finds these cases and never picks a candidate itself. An adjudicator reads what the
payer wrote and may suggest one; a suggestion is advice for a person, not a match.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """One invoice the payment might be for, as an adjudicator sees it.

    The customer, the amount and the payment id are not part of it: all candidates share the
    customer and the amount, and neither helps tell them apart.
    """

    label: str
    invoice_id: str
    description: str
    days_before: int

    @property
    def number(self) -> int:
        return int(self.invoice_id.rsplit("-", 1)[1])


@dataclass(frozen=True)
class AdjudicationCase:
    payment_id: str
    memo: str
    candidates: tuple[Candidate, ...]

    def labels(self) -> tuple[str, ...]:
        return tuple(c.label for c in self.candidates)

    def candidate(self, label: str) -> Candidate:
        return next(c for c in self.candidates if c.label == label)


@dataclass(frozen=True)
class Ruling:
    """An adjudicator's answer: a candidate and whether to rely on it, or no candidate.

    A choice that is not confident is a suggestion; ``alternatives`` lists other candidates that
    were in the running.
    """

    choice: str | None = None
    confident: bool = False
    evidence: str | None = None
    reasons: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()

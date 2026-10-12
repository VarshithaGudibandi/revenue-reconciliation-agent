"""What a model is told about a case, and the figures it may quote.

Records are known to the model by an alias (``R1``, ``R2``) and the customer is not named: the
explanation is written about the aliases and the real ids are put back afterwards. Amounts,
dates and the text of a record are given, because an explanation needs them.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from recon.domain.money import Money, exponent, format_amount


@dataclass(frozen=True)
class RecordView:
    ref: str
    role: str
    record_id: str
    occurred_on: date | None
    amount: Money | None
    text: str | None
    # What a person calls the record (Deposit, Payout); the model is shown it instead of the role.
    label: str = ""


@dataclass(frozen=True)
class ExplainInput:
    kind: str
    variance: Money | None
    records: tuple[RecordView, ...]
    proposed_ref: str | None = None

    def ids(self) -> dict[str, str]:
        """The real id behind each alias."""
        return {r.ref: r.record_id for r in self.records}

    def allowed_figures(self) -> set[Decimal]:
        """Every number a faithful explanation may state: the engine's amounts and totals, the
        counts of records, the parts of a date, and numbers written in a record's own text."""
        figures: set[Decimal] = set()
        amounts = [r.amount for r in self.records if r.amount is not None]
        if self.variance is not None:
            amounts.append(Money(abs(self.variance.minor), self.variance.currency))
        for role in ("invoice", "payment"):
            of_role = [r.amount for r in self.records if r.role == role and r.amount is not None]
            for currency in {m.currency for m in of_role}:
                same = [m.minor for m in of_role if m.currency == currency]
                amounts.append(Money(sum(same), currency))
        for money in amounts:
            figures.add(Decimal(money.minor).scaleb(-exponent(money.currency)))
        for role in ("invoice", "payment"):
            figures.add(Decimal(sum(1 for r in self.records if r.role == role)))
        for record in self.records:
            if record.occurred_on:
                figures.update(
                    Decimal(n)
                    for n in (
                        record.occurred_on.year,
                        record.occurred_on.month,
                        record.occurred_on.day,
                    )
                )
            if record.text:
                figures.update(Decimal(n.replace(",", "")) for n in re.findall(r"\d+", record.text))
        figures.update(Decimal(n) for n in range(0, 11))
        return figures

    def texts(self) -> str:
        return " ".join(r.text for r in self.records if r.text)


def amount_text(money: Money) -> str:
    return f"{format_amount(money.minor, money.currency, thousands=True)} {money.currency}"

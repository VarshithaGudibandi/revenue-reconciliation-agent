"""One period's records, as an investigation may read them, under aliases.

The model never sees a real record id or customer id. A record is known as I1, I2 (invoices) or
P1, P2 (payments) and a customer as C1, C2, numbered in the order they are first shown, so the
same questions always get the same aliases. Amounts, dates and the free text of a record are
shown, because an investigation needs them; a payer may have written a name in a memo.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from recon.files.normalize import NormalizedRecord
from recon.money import Money, exponent, format_amount

MAX_RESULTS = 20


@dataclass(frozen=True)
class OpenCase:
    kind: str
    invoice_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    variance: Money | None


def amount_text(money: Money) -> str:
    return format_amount(money.minor, money.currency)


@dataclass
class Aliases:
    to_alias: dict[tuple[str, str], str] = field(default_factory=dict)
    to_real: dict[str, tuple[str, str]] = field(default_factory=dict)

    def of(self, kind: str, real: str) -> str:
        key = (kind, real)
        if key not in self.to_alias:
            prefix = {"invoice": "I", "payment": "P", "customer": "C"}[kind]
            alias = f"{prefix}{sum(1 for k, _ in self.to_alias if k == kind) + 1}"
            self.to_alias[key] = alias
            self.to_real[alias] = key
        return self.to_alias[key]


class Ledger:
    """The invoices, payments and open cases of a period, with what each record's state is."""

    def __init__(
        self,
        invoices: Sequence[NormalizedRecord],
        payments: Sequence[NormalizedRecord],
        cases: Iterable[OpenCase],
    ) -> None:
        self.invoices = {r.record_id: r for r in invoices}
        self.payments = {r.record_id: r for r in payments}
        self.cases = list(cases)
        self.state: dict[tuple[str, str], str] = {}
        for case in self.cases:
            for i in case.invoice_ids:
                self.state[("invoice", i)] = case.kind
            for p in case.payment_ids:
                self.state[("payment", p)] = case.kind
        self.aliases = Aliases()
        # Every figure the model has been shown, so a finding can be checked against them.
        self.figures: set[str] = set()

    def record(self, role: str, record_id: str) -> NormalizedRecord:
        return (self.invoices if role == "invoice" else self.payments)[record_id]

    def show(self, role: str, record_id: str) -> dict[str, Any]:
        """A record as the model sees it."""
        r = self.record(role, record_id)
        self.figures.add(_plain(r.amount))
        return {
            "record": self.aliases.of(role, record_id),
            "customer": self.aliases.of("customer", r.counterparty),
            "date": r.occurred_on.isoformat(),
            "amount": _plain(r.amount),
            "currency": r.amount.currency,
            "text": r.text or "",
            "state": "open: " + self.state[(role, record_id)]
            if (role, record_id) in self.state
            else "matched",
        }

    def show_case(self, case: OpenCase) -> dict[str, Any]:
        if case.variance is not None:
            self.figures.add(_plain(case.variance))
            self.figures.add(_plain(Money(abs(case.variance.minor), case.variance.currency)))
        return {
            "kind": case.kind,
            "difference": None if case.variance is None else amount_text(case.variance),
            "records": [self.show("invoice", i) for i in case.invoice_ids]
            + [self.show("payment", p) for p in case.payment_ids],
        }

    def real(self, alias: str) -> tuple[str, str] | None:
        return self.aliases.to_real.get(alias)

    def customer_records(self, customer: str) -> dict[str, Any]:
        found = self.real(customer)
        if found is None or found[0] != "customer":
            return {"error": f"no customer {customer!r} has been shown"}
        cid = found[1]
        invoices = sorted((r for r in self.invoices.values() if r.counterparty == cid), key=_order)
        payments = sorted((r for r in self.payments.values() if r.counterparty == cid), key=_order)
        return {
            "invoices": [self.show("invoice", r.record_id) for r in invoices[:MAX_RESULTS]],
            "payments": [self.show("payment", r.record_id) for r in payments[:MAX_RESULTS]],
            "more": max(0, len(invoices) - MAX_RESULTS) + max(0, len(payments) - MAX_RESULTS),
        }

    def find(self, role: str, low: float, high: float, currency: str) -> dict[str, Any]:
        """Records of any customer whose amount is between ``low`` and ``high``."""
        pool = self.invoices if role == "invoice" else self.payments
        hits = sorted(
            (
                r
                for r in pool.values()
                if r.amount.currency == currency.upper() and low <= _float(r.amount) <= high
            ),
            key=_order,
        )
        return {
            "records": [self.show(role, r.record_id) for r in hits[:MAX_RESULTS]],
            "more": max(0, len(hits) - MAX_RESULTS),
        }

    def invoice_sums(self, customer: str, total: float, currency: str) -> dict[str, Any]:
        """Sets of two or three open invoices of a customer whose amounts add up to ``total``."""
        found = self.real(customer)
        if found is None or found[0] != "customer":
            return {"error": f"no customer {customer!r} has been shown"}
        open_invoices = [
            r
            for r in self.invoices.values()
            if r.counterparty == found[1]
            and r.amount.currency == currency.upper()
            and ("invoice", r.record_id) in self.state
        ]
        target = round(total * 10 ** exponent(currency.upper()))
        sets = [
            group
            for size in (2, 3)
            for group in combinations(sorted(open_invoices, key=_order), size)
            if sum(r.amount.minor for r in group) == target
        ]
        return {
            "sets": [
                [self.show("invoice", r.record_id)["record"] for r in group]
                for group in sets[:MAX_RESULTS]
            ]
        }


def _order(r: NormalizedRecord) -> tuple[str, str]:
    return (r.occurred_on.isoformat(), r.record_id)


def _float(money: Money) -> float:
    return float(money.minor) / float(10 ** exponent(money.currency))


def _plain(money: Money) -> str:
    return format_amount(money.minor, money.currency)

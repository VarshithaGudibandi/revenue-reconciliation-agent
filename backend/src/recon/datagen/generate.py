import calendar
import itertools
import random
from dataclasses import dataclass, replace
from datetime import date, timedelta

from recon.datagen.model import (
    Anomaly,
    AnomalyKind,
    Dataset,
    GroundTruth,
    Invoice,
    Match,
    Payment,
)

FIXABLE_KINDS = frozenset(
    {
        AnomalyKind.MISSING_PAYMENT,
        AnomalyKind.AMOUNT_MISMATCH,
        AnomalyKind.DUPLICATE_PAYMENT,
        AnomalyKind.PARTIAL_PAYMENT,
        AnomalyKind.MISREFERENCED_PAYMENT,
    }
)

_SPLIT = "split"
_SINGLE = "single"


@dataclass(frozen=True)
class Config:
    seed: int = 0
    period: str = "2026-01"
    invoices: int = 200
    customers: int = 40
    split_rate: float = 0.05
    noisy_reference_rate: float = 0.10
    missing_rate: float = 0.05
    amount_mismatch_rate: float = 0.04
    duplicate_rate: float = 0.03
    partial_rate: float = 0.03
    misreferenced_rate: float = 0.02
    currency_mismatch_rate: float = 0.01
    unmatched_payment_rate: float = 0.03

    def anomaly_counts(self) -> dict[AnomalyKind, int]:
        """How many invoices get each anomaly, plus how many orphan payments to add."""
        rates = {
            AnomalyKind.MISSING_PAYMENT: self.missing_rate,
            AnomalyKind.AMOUNT_MISMATCH: self.amount_mismatch_rate,
            AnomalyKind.DUPLICATE_PAYMENT: self.duplicate_rate,
            AnomalyKind.PARTIAL_PAYMENT: self.partial_rate,
            AnomalyKind.MISREFERENCED_PAYMENT: self.misreferenced_rate,
            AnomalyKind.CURRENCY_MISMATCH: self.currency_mismatch_rate,
            AnomalyKind.UNMATCHED_PAYMENT: self.unmatched_payment_rate,
        }
        return {kind: round(rate * self.invoices) for kind, rate in rates.items()}

    def split_count(self) -> int:
        return round(self.split_rate * self.invoices)

    def validate(self) -> None:
        _parse_period(self.period)
        if self.invoices < 1 or self.customers < 1:
            raise ValueError("invoices and customers must be at least 1")
        counts = self.anomaly_counts()
        affected = sum(
            count for kind, count in counts.items() if kind is not AnomalyKind.UNMATCHED_PAYMENT
        )
        if affected + self.split_count() > self.invoices:
            raise ValueError("anomaly and split rates exceed the number of invoices")


def _parse_period(period: str) -> tuple[int, int]:
    try:
        year_text, month_text = period.split("-")
        year, month = int(year_text), int(month_text)
        date(year, month, 1)
    except ValueError as error:
        raise ValueError(f"period must look like YYYY-MM, got {period!r}") from error
    return year, month


def _noisy(reference: str, rng: random.Random) -> str:
    variants = (
        reference.lower(),
        reference.replace("-", ""),
        reference.replace("-", " "),
        f" {reference} ",
    )
    return rng.choice(variants)


def generate_dataset(config: Config) -> Dataset:
    """Build one period of invoices and payments with a known set of problems.

    The same configuration always yields the same dataset.
    """
    config.validate()
    rng = random.Random(config.seed)
    year, month = _parse_period(config.period)
    days_in_month = calendar.monthrange(year, month)[1]
    customers = [f"CUST-{number:04d}" for number in range(1, config.customers + 1)]

    invoices: list[Invoice] = []
    for number in range(1, config.invoices + 1):
        magnitude = rng.choice((3, 4, 5, 6))
        invoices.append(
            Invoice(
                invoice_id=f"INV-{number:06d}",
                customer_id=rng.choice(customers),
                issue_date=date(year, month, rng.randint(1, days_in_month)),
                currency=rng.choices(("USD", "EUR"), weights=(85, 15))[0],
                total_minor=rng.randint(10**magnitude, 10 ** (magnitude + 1) - 1),
            )
        )

    plan = _plan_invoices(config, rng)

    payment_numbers = itertools.count(1)
    wrong_reference_numbers = itertools.count(900_001)
    payments: list[Payment] = []
    matches: list[Match] = []
    anomalies: list[Anomaly] = []

    def pay(reference: str, invoice: Invoice, amount: int, *, currency: str | None = None) -> str:
        payment = Payment(
            payment_id=f"PAY-{next(payment_numbers):06d}",
            reference=reference,
            customer_id=invoice.customer_id,
            received_on=invoice.issue_date + timedelta(days=rng.randint(1, 30)),
            currency=currency or invoice.currency,
            amount_minor=amount,
        )
        payments.append(payment)
        return payment.payment_id

    def flag(
        kind: AnomalyKind,
        invoice_ids: tuple[str, ...],
        payment_ids: tuple[str, ...],
        variance: int | None,
        currency: str,
    ) -> None:
        anomalies.append(
            Anomaly(
                anomaly_id=f"A-{len(anomalies) + 1:04d}",
                kind=kind,
                invoice_ids=invoice_ids,
                payment_ids=payment_ids,
                variance_minor=variance,
                currency=currency,
            )
        )

    for index, invoice in enumerate(invoices):
        total = invoice.total_minor
        ref = invoice.invoice_id
        outcome = plan[index]

        if outcome in (_SINGLE, _SPLIT):
            noisy = rng.random() < config.noisy_reference_rate
            reference = _noisy(ref, rng) if noisy else ref
            if outcome == _SINGLE:
                ids: tuple[str, ...] = (pay(reference, invoice, total),)
            else:
                first = rng.randint(total // 4, total // 2)
                ids = (pay(reference, invoice, first), pay(reference, invoice, total - first))
            matches.append(Match(ref, ids, noisy))
        elif outcome is AnomalyKind.MISSING_PAYMENT:
            flag(outcome, (ref,), (), total, invoice.currency)
        elif outcome is AnomalyKind.AMOUNT_MISMATCH:
            delta = rng.choice((-1, 1)) * rng.randint(1, max(1, total // 10))
            flag(outcome, (ref,), (pay(ref, invoice, total + delta),), -delta, invoice.currency)
        elif outcome is AnomalyKind.DUPLICATE_PAYMENT:
            ids = (pay(ref, invoice, total), pay(ref, invoice, total))
            flag(outcome, (ref,), ids, -total, invoice.currency)
        elif outcome is AnomalyKind.PARTIAL_PAYMENT:
            part = rng.randint(total // 4, total * 3 // 4)
            flag(outcome, (ref,), (pay(ref, invoice, part),), total - part, invoice.currency)
        elif outcome is AnomalyKind.MISREFERENCED_PAYMENT:
            wrong = f"INV-{next(wrong_reference_numbers):06d}"
            flag(outcome, (ref,), (pay(wrong, invoice, total),), 0, invoice.currency)
        elif outcome is AnomalyKind.CURRENCY_MISMATCH:
            other = "EUR" if invoice.currency == "USD" else "USD"
            flag(
                outcome, (ref,), (pay(ref, invoice, total, currency=other),), None, invoice.currency
            )

    for _ in range(config.anomaly_counts()[AnomalyKind.UNMATCHED_PAYMENT]):
        # The helper takes its customer, date and currency from an invoice, so borrow a random one
        # and override the amount and reference.
        donor = rng.choice(invoices)
        amount = rng.randint(10**3, 10**6)
        payment_id = pay(f"INV-{next(wrong_reference_numbers):06d}", donor, amount)
        flag(AnomalyKind.UNMATCHED_PAYMENT, (), (payment_id,), -amount, donor.currency)

    rng.shuffle(payments)
    return Dataset(
        revision=1,
        seed=config.seed,
        period=config.period,
        invoices=tuple(invoices),
        payments=tuple(payments),
        truth=GroundTruth(matches=tuple(matches), anomalies=tuple(anomalies), resolved=()),
    )


def _plan_invoices(config: Config, rng: random.Random) -> dict[int, AnomalyKind | str]:
    """Decide, for every invoice, whether it reconciles or carries a given anomaly."""
    order = list(range(config.invoices))
    rng.shuffle(order)
    plan: dict[int, AnomalyKind | str] = dict.fromkeys(order, _SINGLE)
    cursor = 0
    counts = config.anomaly_counts()
    assignments: list[tuple[AnomalyKind | str, int]] = [
        (kind, counts[kind])
        for kind in (
            AnomalyKind.MISSING_PAYMENT,
            AnomalyKind.AMOUNT_MISMATCH,
            AnomalyKind.DUPLICATE_PAYMENT,
            AnomalyKind.PARTIAL_PAYMENT,
            AnomalyKind.MISREFERENCED_PAYMENT,
            AnomalyKind.CURRENCY_MISMATCH,
        )
    ]
    assignments.append((_SPLIT, config.split_count()))
    for outcome, count in assignments:
        for index in order[cursor : cursor + count]:
            plan[index] = outcome
        cursor += count
    return plan


def apply_corrections(dataset: Dataset, *, seed: int, fix_fraction: float = 0.6) -> Dataset:
    """Return the next revision of a dataset in which some anomalies were fixed at the source.

    Only the payments touched by a fix change. Every other row keeps its content and
    position, and newly added payments are appended.
    """
    rng = random.Random(seed)
    invoices = {invoice.invoice_id: invoice for invoice in dataset.invoices}
    payments = {payment.payment_id: payment for payment in dataset.payments}
    fixable = [a for a in dataset.truth.anomalies if a.kind in FIXABLE_KINDS]
    chosen = {a.anomaly_id for a in rng.sample(fixable, k=round(fix_fraction * len(fixable)))}

    next_number = max(int(p.payment_id.removeprefix("PAY-")) for p in dataset.payments) + 1
    appended: list[Payment] = []

    def add_payment(invoice: Invoice, amount: int) -> str:
        nonlocal next_number
        payment = Payment(
            payment_id=f"PAY-{next_number:06d}",
            reference=invoice.invoice_id,
            customer_id=invoice.customer_id,
            received_on=invoice.issue_date + timedelta(days=rng.randint(1, 30)),
            currency=invoice.currency,
            amount_minor=amount,
        )
        next_number += 1
        appended.append(payment)
        return payment.payment_id

    matches = list(dataset.truth.matches)
    remaining: list[Anomaly] = []
    resolved = list(dataset.truth.resolved)

    for anomaly in dataset.truth.anomalies:
        if anomaly.anomaly_id not in chosen:
            remaining.append(anomaly)
            continue
        invoice = invoices[anomaly.invoice_ids[0]]
        ids: tuple[str, ...]
        if anomaly.kind is AnomalyKind.MISSING_PAYMENT:
            ids = (add_payment(invoice, invoice.total_minor),)
        elif anomaly.kind is AnomalyKind.AMOUNT_MISMATCH:
            ids = anomaly.payment_ids
            payments[ids[0]] = replace(payments[ids[0]], amount_minor=invoice.total_minor)
        elif anomaly.kind is AnomalyKind.DUPLICATE_PAYMENT:
            ids = anomaly.payment_ids[:1]
            del payments[anomaly.payment_ids[1]]
        elif anomaly.kind is AnomalyKind.PARTIAL_PAYMENT:
            paid = sum(payments[pid].amount_minor for pid in anomaly.payment_ids)
            ids = (*anomaly.payment_ids, add_payment(invoice, invoice.total_minor - paid))
        else:  # MISREFERENCED_PAYMENT
            ids = anomaly.payment_ids
            payments[ids[0]] = replace(payments[ids[0]], reference=invoice.invoice_id)
        matches.append(Match(invoice.invoice_id, ids, noisy_reference=False))
        resolved.append(anomaly.anomaly_id)

    return Dataset(
        revision=dataset.revision + 1,
        seed=dataset.seed,
        period=dataset.period,
        invoices=dataset.invoices,
        payments=(*payments.values(), *appended),
        truth=GroundTruth(
            matches=tuple(matches), anomalies=tuple(remaining), resolved=tuple(resolved)
        ),
    )

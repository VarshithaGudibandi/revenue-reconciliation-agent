"""Causes behind some of the generated problems, for evaluating an investigation.

The engine reports what is wrong: an invoice paid short, an invoice with no payment, a payment
that matches nothing. Why is a separate question, and the one a person spends time on. With
causes, a dataset says why for three kinds of problem:

- an amount mismatch is a processor fee taken out of the payment, an early-payment discount
  the customer took, a bank charge on a wire, or two digits swapped when the amount was keyed;
- an invoice with no payment is unpaid, or was paid by a related account (a parent company
  paying under its own customer id), or was paid in one lump sum with another invoice of the
  same customer;
- a payment that matches nothing is the other half of the last two, or unidentified.

The rates and charges vary from one case to the next, as they do between processors and banks,
so that a cause is recognised from the shape of the numbers and not from one exact formula.
Every change keeps the engine's view of the problem the same kind, so the dataset still says
exactly which problems the engine should find.
"""

import random
from dataclasses import replace
from datetime import timedelta

from recon.datagen.model import Anomaly, AnomalyKind, Dataset, Invoice, Match, Payment

AMOUNT_CAUSES = ("processor_fee", "early_payment_discount", "bank_charge", "keying_error")
MISSING_CAUSES = ("not_paid", "paid_by_related_account", "paid_in_lump_sum")
UNMATCHED_CAUSES = ("paid_by_related_account", "paid_in_lump_sum", "unidentified")
CAUSES = {
    "amount_mismatch": AMOUNT_CAUSES,
    "missing_payment": MISSING_CAUSES,
    "unmatched_payment": UNMATCHED_CAUSES,
}

_CARD_MEMOS = ("Card payment", "", "Online payment", "Paid by card")
_WIRE_MEMOS = ("Wire transfer", "International transfer", "SWIFT payment", "")
_PLAIN_MEMOS = ("", "Payment", "Bank transfer", "Thank you")


def _number(invoice_id: str) -> str:
    return invoice_id.split("-")[-1].lstrip("0")


def _swap_digits(amount: int) -> int:
    """The amount with its last two digits swapped, as when it is keyed; when they are the same,
    the last digit slips to the next key. Either way the difference is under a hundred minor
    units, so on these invoices it stays a mismatch and never reads as a partial payment."""
    tens, units = divmod(amount % 100, 10)
    if tens != units:
        return amount - amount % 100 + units * 10 + tens
    return amount + (1 if units < 9 else -1)


def _short_payment(total: int, rng: random.Random) -> tuple[str, int, str]:
    """A cause for an amount mismatch, what was paid, and the memo."""
    cause = rng.choice(AMOUNT_CAUSES)
    if cause == "bank_charge" and total < 300_000:
        cause = "processor_fee"  # a wire charge would be a large share of a small invoice
    if cause == "processor_fee":
        rate_bps = rng.randint(240, 340)  # 2.4% to 3.4%
        fixed = rng.choice((20, 25, 30, 35))
        return cause, total - (total * rate_bps // 10_000 + fixed), rng.choice(_CARD_MEMOS)
    if cause == "early_payment_discount":
        percent = rng.choice((1, 1.5, 2, 2.5, 3))
        memo = rng.choice(("", "", f"less {percent}% early payment", "Prompt payment discount"))
        return cause, total - round(total * percent / 100), memo
    if cause == "bank_charge":
        return cause, total - rng.choice((1000, 1500, 2000, 2500, 3500)), rng.choice(_WIRE_MEMOS)
    return cause, _swap_digits(total), rng.choice(_PLAIN_MEMOS)


def add_causes(dataset: Dataset, customers: list[str]) -> Dataset:
    rng = random.Random(f"{dataset.seed}:causes")
    invoices = {i.invoice_id: i for i in dataset.invoices}
    payments = {p.payment_id: p for p in dataset.payments}
    order = [p.payment_id for p in dataset.payments]
    matches = list(dataset.truth.matches)
    anomalies: list[Anomaly] = []
    added: list[Anomaly] = []
    numbers = iter(range(800_001, 900_000))
    taken: set[str] = set()  # invoices already given a cause, as partners of a lump sum

    def new_payment(customer: str, invoice_id: str, amount: int, memo: str) -> str:
        invoice = invoices[invoice_id]
        payment = Payment(
            payment_id=f"PAY-{next(numbers):06d}",
            reference="",
            customer_id=customer,
            received_on=invoice.issue_date + timedelta(days=rng.randint(3, 25)),
            currency=invoice.currency,
            amount_minor=amount,
            memo=memo,
        )
        payments[payment.payment_id] = payment
        order.insert(rng.randint(0, len(order)), payment.payment_id)
        return payment.payment_id

    def unmatched(payment_id: str, cause: str, related: str) -> Anomaly:
        payment = payments[payment_id]
        return Anomaly(
            anomaly_id=f"{related}-P",
            kind=AnomalyKind.UNMATCHED_PAYMENT,
            invoice_ids=(),
            payment_ids=(payment_id,),
            variance_minor=-payment.amount_minor,
            currency=payment.currency,
            cause=cause,
            related=(related,),
        )

    for anomaly in dataset.truth.anomalies:
        if anomaly.kind is AnomalyKind.AMOUNT_MISMATCH:
            invoice = invoices[anomaly.invoice_ids[0]]
            cause, paid, memo = _short_payment(invoice.total_minor, rng)
            payment_id = anomaly.payment_ids[0]
            payments[payment_id] = replace(payments[payment_id], amount_minor=paid, memo=memo)
            anomalies.append(
                replace(anomaly, variance_minor=invoice.total_minor - paid, cause=cause)
            )
        elif anomaly.kind is AnomalyKind.MISSING_PAYMENT:
            if anomaly.invoice_ids[0] in taken:
                continue  # already written as the partner of a lump sum
            invoice = invoices[anomaly.invoice_ids[0]]
            cause = rng.choice(MISSING_CAUSES)
            if cause == "paid_by_related_account":
                payer = rng.choice([c for c in customers if c != invoice.customer_id])
                memo = rng.choice((f"for invoice {_number(invoice.invoice_id)}", "Payment", ""))
                payment_id = new_payment(payer, invoice.invoice_id, invoice.total_minor, memo)
                anomalies.append(
                    replace(anomaly, cause=cause, related=(f"{anomaly.anomaly_id}-P",))
                )
                added.append(unmatched(payment_id, cause, anomaly.anomaly_id))
                continue
            if cause == "paid_in_lump_sum":
                partner = _partner(invoice.invoice_id, invoices, matches, taken, rng)
                if partner is not None:
                    match = next(m for m in matches if m.invoice_id == partner)
                    matches.remove(match)
                    for paid_id in match.payment_ids:
                        del payments[paid_id]
                        order.remove(paid_id)
                    taken.add(partner)
                    both = (invoice.invoice_id, partner)
                    total = invoice.total_minor + invoices[partner].total_minor
                    memo = rng.choice(
                        (f"invoices {_number(both[0])} and {_number(both[1])}", "Payment", "")
                    )
                    payment_id = new_payment(invoice.customer_id, invoice.invoice_id, total, memo)
                    partner_id = f"{anomaly.anomaly_id}-L"
                    lump = f"{anomaly.anomaly_id}-P"
                    anomalies.append(replace(anomaly, cause=cause, related=(partner_id, lump)))
                    added.append(
                        Anomaly(
                            anomaly_id=partner_id,
                            kind=AnomalyKind.MISSING_PAYMENT,
                            invoice_ids=(partner,),
                            payment_ids=(),
                            variance_minor=invoices[partner].total_minor,
                            currency=invoice.currency,
                            cause=cause,
                            related=(anomaly.anomaly_id, lump),
                        )
                    )
                    added.append(
                        replace(
                            unmatched(payment_id, cause, anomaly.anomaly_id),
                            related=(anomaly.anomaly_id, partner_id),
                        )
                    )
                    continue
            anomalies.append(replace(anomaly, cause="not_paid"))
        elif anomaly.kind is AnomalyKind.UNMATCHED_PAYMENT:
            anomalies.append(replace(anomaly, cause="unidentified"))
        else:
            anomalies.append(anomaly)

    truth = replace(dataset.truth, matches=tuple(matches), anomalies=(*anomalies, *added))
    return replace(dataset, payments=tuple(payments[p] for p in order), truth=truth)


def _partner(
    invoice_id: str,
    invoices: dict[str, Invoice],
    matches: list[Match],
    taken: set[str],
    rng: random.Random,
) -> str | None:
    """Another invoice of the same customer and currency, paid by one payment in full, that can
    become the other half of a lump sum."""
    invoice = invoices[invoice_id]
    candidates = [
        m.invoice_id
        for m in matches
        if m.invoice_id not in taken
        and len(m.payment_ids) == 1
        and invoices[m.invoice_id].customer_id == invoice.customer_id
        and invoices[m.invoice_id].currency == invoice.currency
        and m.invoice_id != invoice_id
    ]
    return rng.choice(sorted(candidates)) if candidates else None

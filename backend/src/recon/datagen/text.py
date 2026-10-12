"""Free text for the generated records: what an invoice is for, and what a payer wrote.

Real remittance text is where a human can tell which of several look-alike invoices a payment
was meant for. Ambiguous payments get one of three kinds of memo, recorded in the ground
truth, so a reader of the memo can be scored against what the payer meant:

* ``explicit``: names the intended invoice, in a form people actually type;
* ``descriptive``: says what the payment is for, which only the intended invoice matches;
* ``useless``: says nothing that tells the invoices apart.

The text comes from a random stream of its own, so adding it changes none of the numbers,
dates or ids of a dataset.
"""

import dataclasses
import random

from recon.datagen.model import AnomalyKind, Dataset

SERVICES = (
    "Hosting",
    "Support plan",
    "Consulting",
    "Licence renewal",
    "Training",
    "Implementation",
    "Maintenance",
    "Data storage",
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MEMO_KINDS = ("explicit", "descriptive", "useless")
_MEMO_WEIGHTS = (35, 35, 30)
_USELESS = ("Payment", "Thank you", "Bank transfer", "Payment as agreed", "")
_GENERIC = ("", "", "Payment", "Bank transfer", "Thank you", "As per invoice")
_TEMPLATES = ("{}", "pls apply to {}", "payment for {}", "{} thanks", "Re: {}")


def describe(service: str, year: int, month: int) -> str:
    return f"{service} {MONTHS[month - 1]} {year}"


def _explicit(invoice_id: str, rng: random.Random) -> str:
    number = int(invoice_id.rsplit("-", 1)[1])
    written = rng.choice(
        (
            f"INV-{number:06d}",
            f"inv {number}",
            f"INV{number:06d}",
            f"invoice #{number}",
            f"inv-{number:06d}",
        )
    )
    return rng.choice(_TEMPLATES).format(written)


def _descriptive(service: str, year: int, month: int, rng: random.Random) -> str:
    mon = MONTHS[month - 1]
    return rng.choice(
        (
            f"{service} {mon}",
            f"Re: {service.lower()} {mon.lower()}",
            f"{service} - {year}",
            f"payment for {service.lower()}",
        )
    )


def add_text(dataset: Dataset) -> Dataset:
    """Return the dataset with invoice descriptions, payment memos and the memo kinds recorded."""
    rng = random.Random(f"{dataset.seed}:text")
    year, month = int(dataset.period[:4]), int(dataset.period[5:7])
    ambiguous = [a for a in dataset.truth.anomalies if a.kind is AnomalyKind.AMBIGUOUS_PAYMENT]

    # Look-alike invoices must read differently, or a descriptive memo could not tell them apart.
    service: dict[str, str] = {}
    for anomaly in ambiguous:
        picks = rng.sample(SERVICES, k=len(anomaly.invoice_ids))
        service.update(zip(anomaly.invoice_ids, picks, strict=True))
    for invoice in dataset.invoices:
        service.setdefault(invoice.invoice_id, rng.choice(SERVICES))
    invoices = tuple(
        dataclasses.replace(i, description=describe(service[i.invoice_id], year, month))
        for i in dataset.invoices
    )

    memo_kind: dict[str, str] = {}
    intended_of: dict[str, str] = {}
    for anomaly in ambiguous:
        for payment_id in anomaly.payment_ids:
            memo_kind[payment_id] = rng.choices(MEMO_KINDS, weights=_MEMO_WEIGHTS)[0]
            intended_of[payment_id] = anomaly.intended_invoice_id or anomaly.invoice_ids[0]

    payments = []
    for payment in dataset.payments:
        kind = memo_kind.get(payment.payment_id)
        if kind == "explicit":
            memo = _explicit(intended_of[payment.payment_id], rng)
        elif kind == "descriptive":
            memo = _descriptive(service[intended_of[payment.payment_id]], year, month, rng)
        elif kind == "useless":
            memo = rng.choice(_USELESS)
        else:
            memo = rng.choice(_GENERIC)
        payments.append(dataclasses.replace(payment, memo=memo))

    anomalies = tuple(
        dataclasses.replace(a, memo_kind=memo_kind[a.payment_ids[0]])
        if a.kind is AnomalyKind.AMBIGUOUS_PAYMENT
        else a
        for a in dataset.truth.anomalies
    )
    truth = dataclasses.replace(dataset.truth, anomalies=anomalies)
    return dataclasses.replace(dataset, invoices=invoices, payments=tuple(payments), truth=truth)

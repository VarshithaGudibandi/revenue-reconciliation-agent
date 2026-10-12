from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class AnomalyKind(StrEnum):
    MISSING_PAYMENT = "missing_payment"
    UNMATCHED_PAYMENT = "unmatched_payment"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE_PAYMENT = "duplicate_payment"
    PARTIAL_PAYMENT = "partial_payment"
    MISREFERENCED_PAYMENT = "misreferenced_payment"
    CURRENCY_MISMATCH = "currency_mismatch"
    AMBIGUOUS_PAYMENT = "ambiguous_payment"


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    customer_id: str
    issue_date: date
    currency: str
    total_minor: int
    description: str = ""


@dataclass(frozen=True)
class Payment:
    payment_id: str
    reference: str
    customer_id: str
    received_on: date
    currency: str
    amount_minor: int
    memo: str = ""


@dataclass(frozen=True)
class Match:
    """An invoice that reconciles with the payments that settle it."""

    invoice_id: str
    payment_ids: tuple[str, ...]
    noisy_reference: bool


@dataclass(frozen=True)
class Anomaly:
    """A seeded problem.

    ``variance_minor`` is measured against the true counterpart: positive when the
    invoice is underpaid, negative when overpaid, ``None`` when it cannot be
    computed without an exchange rate.

    For ``AMBIGUOUS_PAYMENT`` the invoices are every unpaid invoice the payment could
    belong to, ``intended_invoice_id`` is the one it really belongs to, the variance is what
    stays unpaid however the payment is assigned, and ``memo_kind`` says whether the payer's
    memo names the invoice (``explicit``), describes what it is for (``descriptive``) or
    says nothing useful (``useless``).
    """

    anomaly_id: str
    kind: AnomalyKind
    invoice_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    variance_minor: int | None
    currency: str
    intended_invoice_id: str | None = None
    memo_kind: str | None = None


@dataclass(frozen=True)
class GroundTruth:
    matches: tuple[Match, ...]
    anomalies: tuple[Anomaly, ...]
    resolved: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    revision: int
    seed: int
    period: str
    invoices: tuple[Invoice, ...]
    payments: tuple[Payment, ...]
    truth: GroundTruth

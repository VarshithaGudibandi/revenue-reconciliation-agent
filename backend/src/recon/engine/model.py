from dataclasses import dataclass
from enum import StrEnum

from recon.domain.money import Money


class ExceptionKind(StrEnum):
    MISSING_PAYMENT = "missing_payment"
    UNMATCHED_PAYMENT = "unmatched_payment"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE_PAYMENT = "duplicate_payment"
    PARTIAL_PAYMENT = "partial_payment"
    MISREFERENCED_PAYMENT = "misreferenced_payment"
    CURRENCY_MISMATCH = "currency_mismatch"
    AMBIGUOUS_PAYMENT = "ambiguous_payment"
    # Bank deposits against processor payouts (recon.engine.deposits).
    DEPOSIT_MISMATCH = "deposit_mismatch"
    MISSING_PAYOUTS = "missing_payouts"
    UNDEPOSITED_PAYOUT = "undeposited_payout"
    PROPOSED_GROUPING = "proposed_grouping"
    AMBIGUOUS_GROUPING = "ambiguous_grouping"


@dataclass(frozen=True)
class EngineConfig:
    """Thresholds for matching. All amounts are in minor units; ratios are in basis points."""

    tolerance_minor: int = 0
    partial_threshold_bps: int = 1000
    pairing_window_days: int = 45

    def __post_init__(self) -> None:
        if self.tolerance_minor < 0:
            raise ValueError("tolerance_minor must not be negative")
        if not 0 <= self.partial_threshold_bps <= 10_000:
            raise ValueError("partial_threshold_bps must be between 0 and 10000")
        if self.pairing_window_days < 0:
            raise ValueError("pairing_window_days must not be negative")


@dataclass(frozen=True)
class Match:
    """An invoice that reconciles with the payments that settle it."""

    invoice_id: str
    payment_ids: tuple[str, ...]
    variance: Money
    rule: str


@dataclass(frozen=True)
class ReconciliationException:
    """Records that need attention.

    ``variance`` is invoices minus payments: positive when underpaid, negative when
    overpaid, ``None`` when currencies differ and no rate is available.
    ``proposed_invoice_id`` is set only when exactly one invoice could own the payment.
    """

    kind: ExceptionKind
    invoice_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    variance: Money | None
    proposed_invoice_id: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    matches: tuple[Match, ...]
    exceptions: tuple[ReconciliationException, ...]

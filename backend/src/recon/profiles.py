"""Profile configuration: which files a reconciliation reads and how it matches them."""

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, model_validator

from recon.engine.model import EngineConfig
from recon.ingestion.normalize import ColumnMapping


@dataclass(frozen=True)
class ReconciliationType:
    """A kind of reconciliation: the two files it reads and how to talk about them.

    In every type one *left* record is settled by several *right* records: an invoice by its
    payments, a bank deposit by the payouts it carries. Stored matches and cases call the two
    sides ``invoice`` and ``payment`` whatever the type, and the labels here are what a person
    sees instead.
    """

    key: str
    title: str
    left: str
    right: str
    left_label: str
    right_label: str
    left_plural: str
    right_plural: str


RECONCILIATION_TYPES: Final = {
    t.key: t
    for t in (
        ReconciliationType(
            "invoice_payment",
            "Invoices and payments",
            "invoices",
            "payments",
            "Invoice",
            "Payment",
            "invoices",
            "payments",
        ),
        ReconciliationType(
            "payout_deposit",
            "Bank deposits and processor payouts",
            "deposits",
            "payouts",
            "Deposit",
            "Payout",
            "deposits",
            "payouts",
        ),
    )
}

# Reconciliation types and the source files each one reads, the left one first.
RECONCILIATION_SOURCES: Final = {k: (t.left, t.right) for k, t in RECONCILIATION_TYPES.items()}


def reconciliation_type(key: str) -> ReconciliationType:
    try:
        return RECONCILIATION_TYPES[key]
    except KeyError:
        raise ValueError(f"unknown reconciliation type {key!r}") from None


def role_sources(key: str) -> dict[str, str]:
    """The file each stored role reads from: ``invoice`` is the left file, ``payment`` the right."""
    kind = reconciliation_type(key)
    return {"invoice": kind.left, "payment": kind.right}


class EngineSettings(BaseModel, frozen=True):
    tolerance_minor: int = 0
    partial_threshold_bps: int = 1000
    pairing_window_days: int = 45


class ProfileConfig(BaseModel, frozen=True):
    sources: dict[str, ColumnMapping]
    engine: EngineSettings = EngineSettings()

    @model_validator(mode="after")
    def _engine_settings_are_valid(self) -> "ProfileConfig":
        self.engine_config()
        return self

    def engine_config(self) -> EngineConfig:
        return EngineConfig(**self.engine.model_dump())

    def fingerprint(self) -> str:
        """A hash of the whole configuration. Changing any setting changes it."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

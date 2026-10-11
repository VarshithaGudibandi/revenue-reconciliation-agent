"""Profile configuration: which files a reconciliation reads and how it matches them."""

import hashlib
import json
from typing import Final

from pydantic import BaseModel, model_validator

from recon.engine.model import EngineConfig
from recon.ingestion.normalize import ColumnMapping

# Reconciliation types and the source files each one reads.
RECONCILIATION_SOURCES: Final = {"invoice_payment": ("invoices", "payments")}


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

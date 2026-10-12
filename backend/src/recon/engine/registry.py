"""Which engine reconciles which type of reconciliation."""

from collections.abc import Callable, Sequence

from recon.engine.deposits import reconcile_deposits
from recon.engine.model import EngineConfig, ReconciliationResult
from recon.engine.reconcile import reconcile
from recon.ingestion.normalize import NormalizedRecord

Engine = Callable[
    [Sequence[NormalizedRecord], Sequence[NormalizedRecord], EngineConfig | None],
    ReconciliationResult,
]

ENGINES: dict[str, Engine] = {
    "invoice_payment": reconcile,
    "payout_deposit": reconcile_deposits,
}


def engine_for(reconciliation_type: str) -> Engine:
    try:
        return ENGINES[reconciliation_type]
    except KeyError:
        raise ValueError(f"no engine for reconciliation type {reconciliation_type!r}") from None

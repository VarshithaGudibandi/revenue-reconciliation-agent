"""Reuse an earlier result for the customers whose records did not change.

Matching never looks across counterparties, so a result is the union of one result
per counterparty. When only some counterparties have changed records, the others keep
their earlier result and only the changed ones need matching again.
"""

from collections.abc import Collection, Mapping

from recon.engine.model import Match, ReconciliationException, ReconciliationResult


def merge_incremental(
    previous: ReconciliationResult,
    invoice_counterparties: Mapping[str, str],
    payment_counterparties: Mapping[str, str],
    dirty: Collection[str],
    recomputed: ReconciliationResult,
) -> ReconciliationResult:
    """Keep the earlier result outside the dirty counterparties; take the rest from ``recomputed``.

    The counterparty maps must cover every record the earlier result mentions, in the
    old or the new version of its file. Invoice and payment ids are separate namespaces.
    """
    changed = set(dirty)

    def invoice_kept(invoice_id: str) -> bool:
        return invoice_counterparties[invoice_id] not in changed

    def payment_kept(payment_id: str) -> bool:
        return payment_counterparties[payment_id] not in changed

    def exception_kept(exception: ReconciliationException) -> bool:
        if exception.invoice_ids:
            return invoice_kept(exception.invoice_ids[0])
        return payment_kept(exception.payment_ids[0])

    matches: list[Match] = [m for m in previous.matches if invoice_kept(m.invoice_id)]
    exceptions = [e for e in previous.exceptions if exception_kept(e)]
    matches.extend(recomputed.matches)
    exceptions.extend(recomputed.exceptions)

    matches.sort(key=lambda m: m.invoice_id)
    exceptions.sort(key=lambda e: (e.invoice_ids, e.payment_ids, e.kind))
    return ReconciliationResult(tuple(matches), tuple(exceptions))

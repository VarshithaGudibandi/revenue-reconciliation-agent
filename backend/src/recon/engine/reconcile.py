"""Invoice-to-payment reconciliation.

A pure, deterministic function of its inputs: no database, no clock, no LLM. Every
input record ends up in exactly one match or one exception.

Three passes run in order:

1. Reference: a payment whose reference equals an invoice's key, for the same
   counterparty, belongs to that invoice. The invoice and its payments are matched or
   classified by comparing the amounts.
2. Residual: payments and invoices left over are paired when counterparty, currency
   and amount agree and the payment falls within a window after the invoice. A lone
   pairing is proposed, never applied; several candidates are reported as ambiguous.
3. Leftovers: whatever remains is a missing or unmatched payment.

A reference that points at another counterparty's invoice is not a match.
"""

from collections import defaultdict, deque
from collections.abc import Callable, Sequence

from recon.domain.money import Money
from recon.engine.model import (
    EngineConfig,
    ExceptionKind,
    Match,
    ReconciliationException,
    ReconciliationResult,
)
from recon.ingestion.normalize import NormalizedRecord

Classifier = Callable[
    [NormalizedRecord, Sequence[NormalizedRecord], EngineConfig], Match | ReconciliationException
]
Ambiguous = Callable[
    [Sequence[NormalizedRecord], Sequence[NormalizedRecord]], ReconciliationException
]


def reconcile(
    invoices: Sequence[NormalizedRecord],
    payments: Sequence[NormalizedRecord],
    config: EngineConfig | None = None,
) -> ReconciliationResult:
    config = config or EngineConfig()
    for label, records in (("invoice", invoices), ("payment", payments)):
        ids = [record.record_id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label} record ids must be unique")
    matches: list[Match] = []
    exceptions: list[ReconciliationException] = []
    claimed_invoices: set[str] = set()
    claimed_payments: set[str] = set()

    reference_pass(
        invoices,
        payments,
        config,
        matches,
        exceptions,
        claimed_invoices,
        claimed_payments,
        _classify,
        _ambiguous,
    )
    _residual_pass(invoices, payments, config, exceptions, claimed_invoices, claimed_payments)

    for invoice in invoices:
        if invoice.record_id not in claimed_invoices:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.MISSING_PAYMENT, (invoice.record_id,), (), invoice.amount
                )
            )
    for payment in payments:
        if payment.record_id not in claimed_payments:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.UNMATCHED_PAYMENT, (), (payment.record_id,), -payment.amount
                )
            )

    matches.sort(key=lambda m: m.invoice_id)
    exceptions.sort(key=lambda e: (e.invoice_ids, e.payment_ids, e.kind))
    return ReconciliationResult(tuple(matches), tuple(exceptions))


def reference_pass(
    invoices: Sequence[NormalizedRecord],
    payments: Sequence[NormalizedRecord],
    config: EngineConfig,
    matches: list[Match],
    exceptions: list[ReconciliationException],
    claimed_invoices: set[str],
    claimed_payments: set[str],
    classify: Classifier,
    ambiguous: Ambiguous,
) -> None:
    """Group each record of the second kind under the record of the first that its reference
    names, for the same counterparty, and let ``classify`` say whether the group reconciles.
    Records whose reference fits several of the first kind go to ``ambiguous``. The engine for
    invoices and payments and the one for deposits and payouts share this pass."""
    by_key: dict[str, list[NormalizedRecord]] = defaultdict(list)
    for invoice in invoices:
        by_key[invoice.key].append(invoice)

    owned: dict[str, list[NormalizedRecord]] = defaultdict(list)
    shared: dict[tuple[str, ...], list[NormalizedRecord]] = defaultdict(list)
    for payment in payments:
        if payment.reference is None:
            continue
        candidates = [
            invoice
            for invoice in by_key.get(payment.reference, ())
            if invoice.counterparty == payment.counterparty
        ]
        if len(candidates) == 1:
            owned[candidates[0].record_id].append(payment)
        elif candidates:
            shared[tuple(sorted(c.record_id for c in candidates))].append(payment)

    invoice_by_id = {invoice.record_id: invoice for invoice in invoices}
    for invoice_id, paid_by in owned.items():
        invoice = invoice_by_id[invoice_id]
        outcome = classify(invoice, paid_by, config)
        if isinstance(outcome, Match):
            matches.append(outcome)
        else:
            exceptions.append(outcome)
        claimed_invoices.add(invoice_id)
        claimed_payments.update(p.record_id for p in paid_by)

    # Two invoices of one counterparty whose ids normalize to the same key cannot be
    # told apart by reference alone.
    for invoice_ids, paid_by in shared.items():
        group = [invoice_by_id[i] for i in invoice_ids]
        exceptions.append(ambiguous(group, paid_by))
        claimed_invoices.update(invoice_ids)
        claimed_payments.update(p.record_id for p in paid_by)


def _classify(
    invoice: NormalizedRecord, paid_by: Sequence[NormalizedRecord], config: EngineConfig
) -> Match | ReconciliationException:
    payment_ids = tuple(sorted(p.record_id for p in paid_by))
    currency = invoice.amount.currency
    if any(p.amount.currency != currency for p in paid_by):
        return ReconciliationException(
            ExceptionKind.CURRENCY_MISMATCH, (invoice.record_id,), payment_ids, None
        )

    paid = Money(sum(p.amount.minor for p in paid_by), currency)
    variance = invoice.amount - paid
    if abs(variance.minor) <= config.tolerance_minor:
        return Match(invoice.record_id, payment_ids, variance, "reference")

    if variance.minor < 0:
        extra_payment = any(
            abs(invoice.amount.minor - (paid.minor - p.amount.minor)) <= config.tolerance_minor
            for p in paid_by
        )
        kind = (
            ExceptionKind.DUPLICATE_PAYMENT
            if len(paid_by) > 1 and extra_payment
            else ExceptionKind.AMOUNT_MISMATCH
        )
    elif variance.minor * 10_000 > config.partial_threshold_bps * invoice.amount.minor:
        kind = ExceptionKind.PARTIAL_PAYMENT
    else:
        kind = ExceptionKind.AMOUNT_MISMATCH
    return ReconciliationException(kind, (invoice.record_id,), payment_ids, variance)


def _residual_pass(
    invoices: Sequence[NormalizedRecord],
    payments: Sequence[NormalizedRecord],
    config: EngineConfig,
    exceptions: list[ReconciliationException],
    claimed_invoices: set[str],
    claimed_payments: set[str],
) -> None:
    unpaid: dict[tuple[str, str], list[NormalizedRecord]] = defaultdict(list)
    for invoice in invoices:
        if invoice.record_id not in claimed_invoices:
            unpaid[(invoice.counterparty, invoice.amount.currency)].append(invoice)

    # A graph with payments and invoices as nodes and "could belong to" as edges.
    edges: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for payment in payments:
        if payment.record_id in claimed_payments:
            continue
        for invoice in unpaid.get((payment.counterparty, payment.amount.currency), ()):
            days = (payment.occurred_on - invoice.occurred_on).days
            close = abs(invoice.amount.minor - payment.amount.minor) <= config.tolerance_minor
            if close and 0 <= days <= config.pairing_window_days:
                edges[("payment", payment.record_id)].add(("invoice", invoice.record_id))
                edges[("invoice", invoice.record_id)].add(("payment", payment.record_id))

    invoice_by_id = {invoice.record_id: invoice for invoice in invoices}
    payment_by_id = {payment.record_id: payment for payment in payments}
    seen: set[tuple[str, str]] = set()
    for start in sorted(edges):
        if start in seen:
            continue
        component = component_of(start, edges)
        seen.update(component)
        invoice_ids = sorted(i for kind, i in component if kind == "invoice")
        payment_ids = sorted(p for kind, p in component if kind == "payment")
        group = [invoice_by_id[i] for i in invoice_ids]
        paid_by = [payment_by_id[p] for p in payment_ids]
        if len(group) == 1 and len(paid_by) == 1:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.MISREFERENCED_PAYMENT,
                    (group[0].record_id,),
                    (paid_by[0].record_id,),
                    group[0].amount - paid_by[0].amount,
                    proposed_invoice_id=group[0].record_id,
                )
            )
        else:
            exceptions.append(_ambiguous(group, paid_by))
        claimed_invoices.update(invoice_ids)
        claimed_payments.update(payment_ids)


def component_of(
    start: tuple[str, str], edges: dict[tuple[str, str], set[tuple[str, str]]]
) -> set[tuple[str, str]]:
    component = {start}
    queue = deque([start])
    while queue:
        for neighbour in edges[queue.popleft()]:
            if neighbour not in component:
                component.add(neighbour)
                queue.append(neighbour)
    return component


def _ambiguous(
    invoices: Sequence[NormalizedRecord], payments: Sequence[NormalizedRecord]
) -> ReconciliationException:
    currencies = {r.amount.currency for r in (*invoices, *payments)}
    variance: Money | None = None
    if len(currencies) == 1:
        (currency,) = currencies
        variance = Money(
            sum(i.amount.minor for i in invoices) - sum(p.amount.minor for p in payments), currency
        )
    return ReconciliationException(
        ExceptionKind.AMBIGUOUS_PAYMENT,
        tuple(sorted(i.record_id for i in invoices)),
        tuple(sorted(p.record_id for p in payments)),
        variance,
    )

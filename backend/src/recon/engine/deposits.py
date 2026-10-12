"""Bank deposits against the payouts that make them up.

A payout is money a processor says it sent. A deposit is a line on the bank statement. One deposit
usually carries several payouts, so the relation is many to one. In the engine's terms the
deposit is the record that is one and its payouts are the several records that settle it: a
``Match`` holds a deposit id in ``invoice_id`` and the payout ids in ``payment_ids``.

Like the engine for invoices, this is a pure, deterministic function, and every input record ends
up in exactly one match or exception. Three passes run in order:

1. Reference: a payout whose reference is a deposit's id, for the same counterparty, belongs to
   that deposit. The deposit matches if the payouts add up to it, and is otherwise a mismatch.
   This is the same pass the invoice engine uses.
2. Grouping: for each deposit left over, look for sets of leftover payouts (same counterparty
   and currency, paid out up to ten days before the deposit) that add up to it. A single set
   that no other deposit could also use is *proposed*, never applied, because amounts adding up is
   not a reference. Several sets are reported as ambiguous.
3. Leftovers: a deposit nothing explains, and a payout that no deposit holds.

The grouping search is bounded: at most 30 candidate payouts per deposit (the nearest in date),
at most 8 payouts in a group, and a fixed budget of steps. A search that runs out of budget is
reported as ambiguous rather than as nothing found.
"""

from collections import defaultdict
from collections.abc import Sequence

from recon.domain.money import Money
from recon.engine.model import (
    EngineConfig,
    ExceptionKind,
    Match,
    ReconciliationException,
    ReconciliationResult,
)
from recon.engine.reconcile import component_of, reference_pass
from recon.ingestion.normalize import NormalizedRecord

# How long before a deposit its payouts may have been paid out, for groups found by amount alone.
# Payouts reach the bank within days. A wider window lets unrelated payouts add up to the same
# total by chance: with a month of unreferenced payouts, many sets hit any given amount.
GROUPING_WINDOW_DAYS = 10
MAX_GROUP = 8
MAX_CANDIDATES = 30
STEP_BUDGET = 200_000


def reconcile_deposits(
    deposits: Sequence[NormalizedRecord],
    payouts: Sequence[NormalizedRecord],
    config: EngineConfig | None = None,
) -> ReconciliationResult:
    config = config or EngineConfig()
    for label, records in (("deposit", deposits), ("payout", payouts)):
        ids = [record.record_id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label} record ids must be unique")
    matches: list[Match] = []
    exceptions: list[ReconciliationException] = []
    claimed_deposits: set[str] = set()
    claimed_payouts: set[str] = set()

    reference_pass(
        deposits,
        payouts,
        config,
        matches,
        exceptions,
        claimed_deposits,
        claimed_payouts,
        _classify,
        _ambiguous,
    )
    _grouping_pass(deposits, payouts, config, exceptions, claimed_deposits, claimed_payouts)

    for deposit in deposits:
        if deposit.record_id not in claimed_deposits:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.MISSING_PAYOUTS, (deposit.record_id,), (), deposit.amount
                )
            )
    for payout in payouts:
        if payout.record_id not in claimed_payouts:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.UNDEPOSITED_PAYOUT, (), (payout.record_id,), -payout.amount
                )
            )

    matches.sort(key=lambda m: m.invoice_id)
    exceptions.sort(key=lambda e: (e.invoice_ids, e.payment_ids, e.kind))
    return ReconciliationResult(tuple(matches), tuple(exceptions))


def _classify(
    deposit: NormalizedRecord, payouts: Sequence[NormalizedRecord], config: EngineConfig
) -> Match | ReconciliationException:
    payout_ids = tuple(sorted(p.record_id for p in payouts))
    currency = deposit.amount.currency
    if any(p.amount.currency != currency for p in payouts):
        return ReconciliationException(
            ExceptionKind.CURRENCY_MISMATCH, (deposit.record_id,), payout_ids, None
        )
    variance = deposit.amount - Money(sum(p.amount.minor for p in payouts), currency)
    if abs(variance.minor) <= config.tolerance_minor:
        return Match(deposit.record_id, payout_ids, variance, "reference")
    return ReconciliationException(
        ExceptionKind.DEPOSIT_MISMATCH, (deposit.record_id,), payout_ids, variance
    )


def _ambiguous(
    deposits: Sequence[NormalizedRecord], payouts: Sequence[NormalizedRecord]
) -> ReconciliationException:
    currencies = {r.amount.currency for r in (*deposits, *payouts)}
    variance: Money | None = None
    if len(currencies) == 1:
        (currency,) = currencies
        variance = Money(
            sum(d.amount.minor for d in deposits) - sum(p.amount.minor for p in payouts), currency
        )
    return ReconciliationException(
        ExceptionKind.AMBIGUOUS_GROUPING,
        tuple(sorted(d.record_id for d in deposits)),
        tuple(sorted(p.record_id for p in payouts)),
        variance,
    )


def find_groupings(
    target: int, candidates: Sequence[tuple[str, int]], tolerance: int, *, limit: int = 2
) -> tuple[list[tuple[str, ...]], bool]:
    """Sets of candidates, as sorted id tuples, whose amounts add up to ``target`` within
    ``tolerance``. The search stops after ``limit`` sets. The second value is True when it ran
    out of its step budget before it could say there were no more. Amounts must be positive."""
    items = sorted(candidates, key=lambda c: (-c[1], c[0]))
    suffix = [0] * (len(items) + 1)
    for index in range(len(items) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + items[index][1]
    found: list[tuple[str, ...]] = []
    steps = 0
    exhausted = False

    def walk(start: int, chosen: list[str], total: int) -> None:
        nonlocal steps, exhausted
        for index in range(start, len(items)):
            if len(found) >= limit or exhausted:
                return
            steps += 1
            if steps > STEP_BUDGET:
                exhausted = True
                return
            if total + suffix[index] < target - tolerance:
                return  # even taking everything that is left is not enough
            name, amount = items[index]
            new_total = total + amount
            if new_total > target + tolerance:
                continue  # too much, but a smaller one further on may fit
            chosen.append(name)
            if abs(new_total - target) <= tolerance:
                found.append(tuple(sorted(chosen)))
            if len(chosen) < MAX_GROUP:
                walk(index + 1, chosen, new_total)
            chosen.pop()

    walk(0, [], 0)
    return found, exhausted


def _grouping_pass(
    deposits: Sequence[NormalizedRecord],
    payouts: Sequence[NormalizedRecord],
    config: EngineConfig,
    exceptions: list[ReconciliationException],
    claimed_deposits: set[str],
    claimed_payouts: set[str],
) -> None:
    free: dict[tuple[str, str], list[NormalizedRecord]] = defaultdict(list)
    for payout in payouts:
        if payout.record_id not in claimed_payouts and payout.amount.minor > 0:
            free[(payout.counterparty, payout.amount.currency)].append(payout)

    solutions: dict[str, list[tuple[str, ...]]] = {}
    uncertain: dict[str, list[str]] = {}
    for deposit in deposits:
        if deposit.record_id in claimed_deposits or deposit.amount.minor <= 0:
            continue
        window = [
            p
            for p in free.get((deposit.counterparty, deposit.amount.currency), ())
            if 0 <= (deposit.occurred_on - p.occurred_on).days <= GROUPING_WINDOW_DAYS
        ]
        window.sort(key=lambda p: ((deposit.occurred_on - p.occurred_on).days, p.record_id))
        window = window[:MAX_CANDIDATES]
        found, exhausted = find_groupings(
            deposit.amount.minor,
            [(p.record_id, p.amount.minor) for p in window],
            config.tolerance_minor,
        )
        if exhausted:
            uncertain[deposit.record_id] = [p.record_id for p in window]
        elif found:
            solutions[deposit.record_id] = found

    edges: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for deposit_id, payout_ids in (
        *((d, [i for group in groups for i in group]) for d, groups in solutions.items()),
        *uncertain.items(),
    ):
        for payout_id in payout_ids:
            edges[("deposit", deposit_id)].add(("payout", payout_id))
            edges[("payout", payout_id)].add(("deposit", deposit_id))

    deposit_by_id = {d.record_id: d for d in deposits}
    payout_by_id = {p.record_id: p for p in payouts}
    seen: set[tuple[str, str]] = set()
    for start in sorted(edges):
        if start in seen:
            continue
        component = component_of(start, edges)
        seen.update(component)
        deposit_ids = sorted(i for kind, i in component if kind == "deposit")
        payout_ids = sorted(i for kind, i in component if kind == "payout")
        group_deposits = [deposit_by_id[i] for i in deposit_ids]
        group_payouts = [payout_by_id[i] for i in payout_ids]
        only = solutions.get(deposit_ids[0], []) if len(deposit_ids) == 1 else []
        if len(only) == 1 and deposit_ids[0] not in uncertain:
            exceptions.append(
                ReconciliationException(
                    ExceptionKind.PROPOSED_GROUPING,
                    tuple(deposit_ids),
                    tuple(payout_ids),
                    group_deposits[0].amount
                    - Money(
                        sum(p.amount.minor for p in group_payouts),
                        group_deposits[0].amount.currency,
                    ),
                )
            )
        else:
            exceptions.append(_ambiguous(group_deposits, group_payouts))
        claimed_deposits.update(deposit_ids)
        claimed_payouts.update(payout_ids)

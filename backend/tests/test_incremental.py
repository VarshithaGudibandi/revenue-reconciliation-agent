"""The algorithm behind incremental recompute, without a database.

The properties here are what make reusing an earlier result safe: matching is
additive over counterparties, and the diff finds exactly the records that changed.
"""

import random
from collections import defaultdict
from dataclasses import replace
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from recon.domain.money import Money
from recon.engine.incremental import merge_incremental
from recon.engine.model import ReconciliationResult
from recon.engine.reconcile import reconcile
from recon.ingestion.diff import Fingerprint, diff_fingerprints
from recon.ingestion.normalize import NormalizedRecord, normalize_reference

CUSTOMERS = ["A", "B", "C"]


def fingerprints(records: list[NormalizedRecord]) -> list[Fingerprint]:
    return [Fingerprint(r.record_id, r.counterparty, r.content_hash) for r in records]


def by_counterparty(
    records: list[NormalizedRecord], counterparties: set[str]
) -> list[NormalizedRecord]:
    return [r for r in records if r.counterparty in counterparties]


def merged(results: list[ReconciliationResult]) -> ReconciliationResult:
    matches = [m for result in results for m in result.matches]
    exceptions = [e for result in results for e in result.exceptions]
    matches.sort(key=lambda m: m.invoice_id)
    exceptions.sort(key=lambda e: (e.invoice_ids, e.payment_ids, e.kind))
    return ReconciliationResult(tuple(matches), tuple(exceptions))


@st.composite
def books(draw: st.DrawFn) -> tuple[list[NormalizedRecord], list[NormalizedRecord]]:
    """Invoices and payments with many collisions: few customers, amounts and days."""
    amounts, days = st.sampled_from([100, 200, 300]), st.integers(min_value=1, max_value=25)
    currencies = st.sampled_from(["USD", "USD", "EUR"])
    invoices = [
        NormalizedRecord(
            1,
            f"I{n}",
            None,
            draw(st.sampled_from(CUSTOMERS)),
            date(2026, 1, draw(days)),
            Money(draw(amounts), draw(currencies)),
        )  # fmt: skip
        for n in range(draw(st.integers(min_value=0, max_value=9)))
    ]
    references = st.sampled_from([*(i.record_id for i in invoices), "X9", None])
    payments = [
        NormalizedRecord(
            1,
            f"P{n}",
            normalize_reference(ref) if (ref := draw(references)) else None,
            draw(st.sampled_from(CUSTOMERS)),
            date(2026, 1, draw(days)),
            Money(draw(amounts), draw(currencies)),
        )  # fmt: skip
        for n in range(draw(st.integers(min_value=0, max_value=12)))
    ]
    return invoices, payments


@given(books())
def test_matching_is_additive_over_counterparties(
    book: tuple[list[NormalizedRecord], list[NormalizedRecord]],
) -> None:
    invoices, payments = book

    whole = reconcile(invoices, payments)
    parts = merged(
        [
            reconcile(by_counterparty(invoices, {c}), by_counterparty(payments, {c}))
            for c in CUSTOMERS
        ]
    )

    assert whole == parts


def edit(
    rng: random.Random, records: list[NormalizedRecord], prefix: str, references: list[str]
) -> list[NormalizedRecord]:
    """Remove, alter and add records at random."""
    edited: list[NormalizedRecord] = []
    for record in records:
        roll = rng.random()
        if roll < 0.15:
            continue
        if roll < 0.40:
            record = replace(
                record,
                counterparty=rng.choice(CUSTOMERS),
                amount=Money(rng.choice([100, 200, 300, 150]), record.amount.currency),
                occurred_on=date(2026, 1, rng.randint(1, 25)),
                reference=(
                    normalize_reference(rng.choice([*references, "X9"]))
                    if record.reference
                    else None
                ),
            )
        edited.append(record)
    for n in range(rng.randint(0, 3)):
        edited.append(
            NormalizedRecord(
                1,
                f"{prefix}new{n}",
                normalize_reference(rng.choice([*references, "X9"])),
                rng.choice(CUSTOMERS),
                date(2026, 1, rng.randint(1, 25)),
                Money(rng.choice([100, 200, 300]), "USD"),
            )  # fmt: skip
        )
    return edited


def incremental(
    old: tuple[list[NormalizedRecord], list[NormalizedRecord]],
    new: tuple[list[NormalizedRecord], list[NormalizedRecord]],
) -> tuple[ReconciliationResult, frozenset[str]]:
    """The algorithm the runs use, with the records in memory."""
    previous = reconcile(*old)
    invoice_diff = diff_fingerprints(fingerprints(old[0]), fingerprints(new[0]))
    payment_diff = diff_fingerprints(fingerprints(old[1]), fingerprints(new[1]))
    dirty = invoice_diff.counterparties | payment_diff.counterparties

    invoice_counterparties = {r.record_id: r.counterparty for r in (*old[0], *new[0])}
    payment_counterparties = {r.record_id: r.counterparty for r in (*old[1], *new[1])}
    # Where a record moved, the new counterparty wins; both are dirty anyway.
    invoice_counterparties.update({r.record_id: r.counterparty for r in new[0]})
    payment_counterparties.update({r.record_id: r.counterparty for r in new[1]})

    recomputed = reconcile(by_counterparty(new[0], set(dirty)), by_counterparty(new[1], set(dirty)))
    result = merge_incremental(
        previous, invoice_counterparties, payment_counterparties, dirty, recomputed
    )
    return result, dirty


@settings(max_examples=400)
@given(books(), st.randoms(use_true_random=False))
def test_incremental_result_equals_a_full_recompute(
    old: tuple[list[NormalizedRecord], list[NormalizedRecord]], rng: random.Random
) -> None:
    references = [r.record_id for r in old[0]] or ["X9"]
    new = (edit(rng, old[0], "I", references), edit(rng, old[1], "P", references))

    result, _ = incremental(old, new)

    assert result == reconcile(*new)


@given(books())
def test_with_no_change_nothing_is_recomputed(
    book: tuple[list[NormalizedRecord], list[NormalizedRecord]],
) -> None:
    result, dirty = incremental(book, book)

    assert dirty == frozenset()
    assert result == reconcile(*book)


@settings(max_examples=200)
@given(books(), st.randoms(use_true_random=False))
def test_untouched_counterparties_are_not_recomputed(
    old: tuple[list[NormalizedRecord], list[NormalizedRecord]], rng: random.Random
) -> None:
    references = [r.record_id for r in old[0]] or ["X9"]
    new = (edit(rng, old[0], "I", references), edit(rng, old[1], "P", references))

    _, dirty = incremental(old, new)

    touched = defaultdict(set)
    for source, (before, after) in enumerate(zip(old, new, strict=True)):
        old_by_id = {r.record_id: r for r in before}
        new_by_id = {r.record_id: r for r in after}
        for record_id in old_by_id.keys() | new_by_id.keys():
            a, b = old_by_id.get(record_id), new_by_id.get(record_id)
            if a != b:
                for record in (a, b):
                    if record is not None:
                        touched[source].add(record.counterparty)
    assert dirty == touched[0] | touched[1]


def test_the_diff_finds_added_removed_and_changed_records() -> None:
    old = [Fingerprint("a", "C1", "h1"), Fingerprint("b", "C1", "h2"), Fingerprint("c", "C2", "h3")]
    new = [Fingerprint("b", "C1", "h2"), Fingerprint("c", "C3", "h9"), Fingerprint("d", "C4", "h4")]

    diff = diff_fingerprints(old, new)

    assert diff.added == {"d"}
    assert diff.removed == {"a"}
    assert diff.changed == {"c"}
    # "c" moved from C2 to C3, so both are affected.
    assert diff.counterparties == {"C1", "C2", "C3", "C4"}


def test_the_diff_ignores_unchanged_records_and_their_order() -> None:
    records = [Fingerprint("a", "C1", "h1"), Fingerprint("b", "C2", "h2")]

    diff = diff_fingerprints(records, list(reversed(records)))

    assert (diff.added, diff.removed, diff.changed, diff.counterparties) == (
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset(),
    )

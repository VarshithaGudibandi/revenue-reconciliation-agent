"""Bank deposits against processor payouts."""

import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from recon.datagen.batches import (
    DEPOSIT_MAPPING,
    PAYOUT_MAPPING,
    BatchAnomalyKind,
    BatchConfig,
    BatchDataset,
    generate_batches,
    write_batches,
)
from recon.domain.money import Money
from recon.engine.deposits import MAX_CANDIDATES, MAX_GROUP, find_groupings, reconcile_deposits
from recon.engine.model import EngineConfig, ExceptionKind, ReconciliationResult
from recon.ingestion.normalize import NormalizedRecord, normalize_csv, normalize_reference

DAY = date(2026, 1, 20)


def deposit(
    record_id: str,
    minor: int,
    *,
    on: date = DAY,
    who: str = "STRIPE",
    cur: str = "USD",
    text: str | None = None,
) -> NormalizedRecord:
    return NormalizedRecord(1, record_id, None, who, on, Money(minor, cur), text)


def payout(
    record_id: str,
    minor: int,
    ref: str | None = None,
    *,
    on: date = DAY - timedelta(days=3),
    who: str = "STRIPE",
    cur: str = "USD",
) -> NormalizedRecord:
    reference = normalize_reference(ref) if ref else None
    return NormalizedRecord(1, record_id, reference, who, on, Money(minor, cur))


# --- the search for groups ---------------------------------------------------------------------


def test_one_group_adds_up_to_the_target() -> None:
    found, exhausted = find_groupings(1_000, [("a", 400), ("b", 600), ("c", 250), ("d", 5_000)], 0)

    assert found == [("a", "b")] and not exhausted


def test_two_different_groups_are_both_reported() -> None:
    found, _ = find_groupings(1_000, [("a", 400), ("b", 600), ("c", 300), ("d", 700)], 0)

    assert sorted(found) == [("a", "b"), ("c", "d")]


def test_nothing_is_found_when_nothing_adds_up() -> None:
    assert find_groupings(1_001, [("a", 400), ("b", 600)], 0) == ([], False)


def test_a_tolerance_lets_a_near_sum_count() -> None:
    assert find_groupings(1_003, [("a", 400), ("b", 600)], 5)[0] == [("a", "b")]
    assert find_groupings(1_003, [("a", 400), ("b", 600)], 2)[0] == []


def test_a_single_record_can_be_the_whole_group() -> None:
    assert find_groupings(500, [("a", 500), ("b", 100)], 0)[0] == [("a",)]


def test_a_group_larger_than_the_limit_is_not_looked_for() -> None:
    nine = [(f"p{n}", 100) for n in range(MAX_GROUP + 1)]

    assert find_groupings(100 * (MAX_GROUP + 1), nine, 0) == ([], False)
    assert find_groupings(100 * MAX_GROUP, nine, 0)[0] != []


def test_a_search_that_runs_out_of_budget_says_so_instead_of_saying_nothing() -> None:
    # thirty equal even amounts cannot make an odd target, and nothing prunes the search
    candidates = [(f"p{n:02d}", 2) for n in range(MAX_CANDIDATES)]

    found, exhausted = find_groupings(2 * MAX_GROUP + 1, candidates, 0)

    assert found == [] and exhausted


# --- the engine on small cases -----------------------------------------------------------------


def only(
    result: ReconciliationResult,
) -> tuple[
    list[tuple[str, tuple[str, ...]]],
    list[tuple[str, tuple[str, ...], tuple[str, ...], int | None]],
]:
    """Matches and exceptions as plain tuples, exceptions in a fixed order of kind then ids."""
    return (
        [(m.invoice_id, m.payment_ids) for m in result.matches],
        sorted(
            (
                e.kind.value,
                e.invoice_ids,
                e.payment_ids,
                None if e.variance is None else e.variance.minor,
            )
            for e in result.exceptions
        ),
    )


def test_payouts_that_name_a_deposit_and_add_up_to_it_match() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000)], [payout("PO-1", 400, "BNK-1"), payout("PO-2", 600, "bnk1")]
    )

    assert only(result) == ([("BNK-1", ("PO-1", "PO-2"))], [])
    assert result.matches[0].rule == "reference"


def test_a_deposit_short_of_its_payouts_is_a_mismatch_with_the_difference() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 970)], [payout("PO-1", 400, "BNK-1"), payout("PO-2", 600, "BNK-1")]
    )

    assert only(result) == (
        [],
        [(ExceptionKind.DEPOSIT_MISMATCH.value, ("BNK-1",), ("PO-1", "PO-2"), -30)],
    )


def test_a_deposit_larger_than_its_payouts_has_a_positive_difference() -> None:
    result = reconcile_deposits([deposit("BNK-1", 1_050)], [payout("PO-1", 1_000, "BNK-1")])

    assert only(result)[1] == [(ExceptionKind.DEPOSIT_MISMATCH.value, ("BNK-1",), ("PO-1",), 50)]


def test_a_tolerance_is_honoured_in_the_reference_pass() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 998)], [payout("PO-1", 1_000, "BNK-1")], EngineConfig(tolerance_minor=2)
    )

    assert only(result) == ([("BNK-1", ("PO-1",))], [])


def test_payouts_in_another_currency_than_the_deposit_cannot_be_compared() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000, cur="GBP")], [payout("PO-1", 1_000, "BNK-1", cur="USD")]
    )

    assert only(result)[1] == [(ExceptionKind.CURRENCY_MISMATCH.value, ("BNK-1",), ("PO-1",), None)]


def test_a_reference_to_another_accounts_deposit_is_not_a_match() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000, who="PAYPAL")], [payout("PO-1", 1_000, "BNK-1", who="STRIPE")]
    )

    kinds = {e.kind for e in result.exceptions}
    assert result.matches == () and kinds == {
        ExceptionKind.MISSING_PAYOUTS,
        ExceptionKind.UNDEPOSITED_PAYOUT,
    }


def test_unreferenced_payouts_that_add_up_are_proposed_and_not_applied() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000)], [payout("PO-1", 400), payout("PO-2", 600), payout("PO-3", 77)]
    )

    assert result.matches == ()
    assert only(result)[1] == [
        (ExceptionKind.PROPOSED_GROUPING.value, ("BNK-1",), ("PO-1", "PO-2"), 0),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-3",), -77),
    ]


def test_a_payout_before_the_window_or_after_the_deposit_is_not_grouped() -> None:
    early = payout("PO-1", 400, on=DAY - timedelta(days=11))
    late = payout("PO-2", 600, on=DAY + timedelta(days=1))
    ok = payout("PO-3", 1_000, on=DAY)

    result = reconcile_deposits([deposit("BNK-1", 1_000)], [early, late, ok])

    assert only(result)[1][0] == (ExceptionKind.PROPOSED_GROUPING.value, ("BNK-1",), ("PO-3",), 0)


def test_two_ways_to_make_the_deposit_is_ambiguous() -> None:
    payouts = [payout("PO-1", 400), payout("PO-2", 600), payout("PO-3", 300), payout("PO-4", 700)]

    result = reconcile_deposits([deposit("BNK-1", 1_000)], payouts)

    assert only(result)[1] == [
        (
            ExceptionKind.AMBIGUOUS_GROUPING.value,
            ("BNK-1",),
            ("PO-1", "PO-2", "PO-3", "PO-4"),
            -1_000,
        )
    ]


def test_two_deposits_that_could_take_the_same_payouts_are_ambiguous_together() -> None:
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000), deposit("BNK-2", 1_000)],
        [payout("PO-1", 400), payout("PO-2", 600)],
    )

    assert only(result)[1] == [
        (ExceptionKind.AMBIGUOUS_GROUPING.value, ("BNK-1", "BNK-2"), ("PO-1", "PO-2"), 1_000)
    ]


def test_a_deposit_nothing_explains_and_a_payout_nothing_holds_are_reported() -> None:
    result = reconcile_deposits([deposit("BNK-1", 1_000)], [payout("PO-1", 123, "BNK-404")])

    assert only(result)[1] == [
        (ExceptionKind.MISSING_PAYOUTS.value, ("BNK-1",), (), 1_000),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-1",), -123),
    ]


def test_a_refund_or_zero_payout_is_never_part_of_a_group() -> None:
    # 1000 alone makes the deposit; with the refund 300 + -300 added it would "also" make it,
    # which would turn a clear proposal into an ambiguous one
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000)],
        [payout("PO-1", 1_000), payout("PO-2", 300), payout("PO-3", -300), payout("PO-4", 0)],
    )

    assert result.matches == ()
    assert only(result)[1] == [
        (ExceptionKind.PROPOSED_GROUPING.value, ("BNK-1",), ("PO-1",), 0),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-2",), -300),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-3",), 300),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-4",), 0),
    ]


def test_a_zero_payout_does_not_make_a_clear_grouping_ambiguous() -> None:
    # 1000 alone, and 1000 + 0, would be two different groups if zero amounts counted
    result = reconcile_deposits(
        [deposit("BNK-1", 1_000)], [payout("PO-1", 1_000), payout("PO-2", 0)]
    )

    assert only(result)[1] == [
        (ExceptionKind.PROPOSED_GROUPING.value, ("BNK-1",), ("PO-1",), 0),
        (ExceptionKind.UNDEPOSITED_PAYOUT.value, (), ("PO-2",), 0),
    ]


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="deposit record ids"):
        reconcile_deposits([deposit("A", 1), deposit("A", 2)], [])
    with pytest.raises(ValueError, match="payout record ids"):
        reconcile_deposits([], [payout("A", 1), payout("A", 2)])


# --- the engine against generated truth --------------------------------------------------------


def records(
    dataset: BatchDataset, directory: Path
) -> tuple[list[NormalizedRecord], list[NormalizedRecord]]:
    write_batches(dataset, directory)
    deposits = normalize_csv(directory / "deposits.csv", DEPOSIT_MAPPING)
    payouts = normalize_csv(directory / "payouts.csv", PAYOUT_MAPPING)
    assert not deposits.rejected and not payouts.rejected
    return list(deposits.records), list(payouts.records)


SEEDS = range(12)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_seeded_problem_is_found_with_the_right_kind_records_and_amount(
    seed: int, tmp_path: Path
) -> None:
    dataset = generate_batches(BatchConfig(seed=seed, deposits=120))
    deposits, payouts = records(dataset, tmp_path)

    result = reconcile_deposits(deposits, payouts)

    assert {(m.invoice_id, m.payment_ids) for m in result.matches} == {
        (m.deposit_id, m.payout_ids) for m in dataset.matches
    }
    found = sorted(
        (
            e.kind.value,
            e.invoice_ids,
            e.payment_ids,
            None if e.variance is None else e.variance.minor,
        )
        for e in result.exceptions
    )
    expected = sorted(
        (a.kind.value, a.deposit_ids, a.payout_ids, a.variance_minor) for a in dataset.anomalies
    )
    assert found == expected


def test_the_generated_data_has_every_kind_of_problem_and_mostly_clean_deposits() -> None:
    kinds = set()
    clean = total = 0
    for seed in SEEDS:
        dataset = generate_batches(BatchConfig(seed=seed, deposits=120))
        kinds |= {a.kind for a in dataset.anomalies}
        clean += len(dataset.matches)
        total += len(dataset.deposits)

    assert kinds == set(BatchAnomalyKind)
    assert clean / total > 0.7


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_every_record_ends_up_in_exactly_one_match_or_exception(seed: int, tmp_path: Path) -> None:
    deposits, payouts = records(generate_batches(BatchConfig(seed=seed, deposits=150)), tmp_path)

    result = reconcile_deposits(deposits, payouts)

    seen_deposits = [m.invoice_id for m in result.matches] + [
        i for e in result.exceptions for i in e.invoice_ids
    ]
    seen_payouts = [p for m in result.matches for p in m.payment_ids] + [
        p for e in result.exceptions for p in e.payment_ids
    ]
    assert sorted(seen_deposits) == sorted(d.record_id for d in deposits)
    assert sorted(seen_payouts) == sorted(p.record_id for p in payouts)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_result_does_not_depend_on_the_order_of_the_records(seed: int, tmp_path: Path) -> None:
    deposits, payouts = records(generate_batches(BatchConfig(seed=seed, deposits=100)), tmp_path)
    rng = random.Random(seed)  # noqa: S311
    shuffled_d, shuffled_p = deposits[:], payouts[:]
    rng.shuffle(shuffled_d)
    rng.shuffle(shuffled_p)

    assert reconcile_deposits(shuffled_d, shuffled_p) == reconcile_deposits(deposits, payouts)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_counterparties_are_independent_so_incremental_runs_can_reuse_them(
    seed: int, tmp_path: Path
) -> None:
    deposits, payouts = records(generate_batches(BatchConfig(seed=seed, deposits=120)), tmp_path)
    whole = reconcile_deposits(deposits, payouts)
    parts = [
        reconcile_deposits(
            [d for d in deposits if d.counterparty == who],
            [p for p in payouts if p.counterparty == who],
        )
        for who in sorted({d.counterparty for d in deposits})
    ]

    assert sorted((m for r in parts for m in r.matches), key=lambda m: m.invoice_id) == list(
        whole.matches
    )
    assert sorted(
        (e for r in parts for e in r.exceptions),
        key=lambda e: (e.invoice_ids, e.payment_ids, e.kind),
    ) == list(whole.exceptions)


def test_the_generator_is_reproducible_and_validates_its_rates() -> None:
    assert generate_batches(BatchConfig(seed=4)) == generate_batches(BatchConfig(seed=4))
    assert generate_batches(BatchConfig(seed=4)) != generate_batches(BatchConfig(seed=5))
    with pytest.raises(ValueError, match="rates"):
        BatchConfig(mismatch_rate=0.9, missing_rate=0.9)
    with pytest.raises(ValueError, match="at least 1"):
        BatchConfig(deposits=0)

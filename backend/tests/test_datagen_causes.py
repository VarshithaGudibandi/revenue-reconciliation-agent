"""Causes behind generated problems: what each looks like, and that the engine's view holds."""

import pytest

from recon.datagen.causes import CAUSES
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.model import AnomalyKind, Dataset
from recon.datagen.writer import truth_to_dict
from recon.engine.reconcile import reconcile
from recon.evaluation.score import records_from_dataset, score

SEEDS = range(8)


def with_causes(seed: int) -> Dataset:
    return generate_dataset(Config(seed=seed, invoices=200, causes=True))


@pytest.mark.parametrize("seed", SEEDS)
def test_the_engine_still_finds_exactly_the_seeded_problems(seed: int) -> None:
    dataset = with_causes(seed)
    assert score(dataset, reconcile(*records_from_dataset(dataset))).perfect


def test_without_causes_nothing_changes() -> None:
    plain = generate_dataset(Config(seed=3))
    assert plain == generate_dataset(Config(seed=3, causes=False))
    assert all(a.cause is None for a in plain.truth.anomalies)
    assert all("cause" not in a for a in truth_to_dict(plain)["anomalies"])


def test_every_problem_of_the_three_kinds_has_a_cause_of_its_kind() -> None:
    seen: set[str] = set()
    for seed in SEEDS:
        for a in with_causes(seed).truth.anomalies:
            if a.kind.value in CAUSES:
                assert a.cause in CAUSES[a.kind.value], a
                seen.add(a.cause)
            else:
                assert a.cause is None
    assert seen == {c for causes in CAUSES.values() for c in causes}


def test_each_cause_has_the_shape_it_says() -> None:
    for seed in SEEDS:
        dataset = with_causes(seed)
        invoices = {i.invoice_id: i for i in dataset.invoices}
        payments = {p.payment_id: p for p in dataset.payments}
        by_id = {a.anomaly_id: a for a in dataset.truth.anomalies}
        for a in dataset.truth.anomalies:
            if a.kind is AnomalyKind.AMOUNT_MISMATCH:
                total = invoices[a.invoice_ids[0]].total_minor
                paid = payments[a.payment_ids[0]].amount_minor
                assert a.variance_minor == total - paid
                share = (total - paid) / total
                if a.cause == "processor_fee":
                    assert 0.024 <= share <= 0.034 + 35 / total
                elif a.cause == "early_payment_discount":
                    assert any(abs(share - p / 100) < 0.001 for p in (1, 1.5, 2, 2.5, 3))
                elif a.cause == "bank_charge":
                    assert total - paid in (1000, 1500, 2000, 2500, 3500)
                elif a.cause == "keying_error":
                    assert 0 < abs(total - paid) < 100
            elif a.cause == "paid_by_related_account" and a.kind is AnomalyKind.MISSING_PAYMENT:
                payment = payments[by_id[a.related[0]].payment_ids[0]]
                invoice = invoices[a.invoice_ids[0]]
                assert payment.amount_minor == invoice.total_minor
                assert payment.customer_id != invoice.customer_id
            elif a.cause == "paid_in_lump_sum" and a.kind is AnomalyKind.UNMATCHED_PAYMENT:
                halves = [by_id[r].invoice_ids[0] for r in a.related]
                payment = payments[a.payment_ids[0]]
                assert payment.amount_minor == sum(invoices[i].total_minor for i in halves)
                assert {invoices[i].customer_id for i in halves} == {payment.customer_id}


def test_the_truth_file_carries_the_cause_and_what_it_links() -> None:
    truth = truth_to_dict(with_causes(1))
    lump = [a for a in truth["anomalies"] if a.get("cause") == "paid_in_lump_sum"]
    assert lump and all(len(a["related"]) == 2 for a in lump)

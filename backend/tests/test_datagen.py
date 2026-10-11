import csv
import hashlib
from collections import Counter
from pathlib import Path

import pytest

from recon.datagen.generate import FIXABLE_KINDS, Config, apply_corrections, generate_dataset
from recon.datagen.model import AnomalyKind, Dataset
from recon.datagen.writer import write_dataset

SEEDS = range(20)


def digest(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def assert_consistent(dataset: Dataset) -> None:
    """Every record is explained exactly once, and every variance matches the rows."""
    invoices = {i.invoice_id: i for i in dataset.invoices}
    payments = {p.payment_id: p for p in dataset.payments}
    assert len(invoices) == len(dataset.invoices)
    assert len(payments) == len(dataset.payments)

    seen_invoices: Counter[str] = Counter()
    seen_payments: Counter[str] = Counter()

    for match in dataset.truth.matches:
        invoice = invoices[match.invoice_id]
        paid = [payments[pid] for pid in match.payment_ids]
        assert sum(p.amount_minor for p in paid) == invoice.total_minor
        assert {p.currency for p in paid} == {invoice.currency}
        seen_invoices.update([match.invoice_id])
        seen_payments.update(match.payment_ids)

    for anomaly in dataset.truth.anomalies:
        seen_invoices.update(anomaly.invoice_ids)
        seen_payments.update(anomaly.payment_ids)
        paid_total = sum(payments[pid].amount_minor for pid in anomaly.payment_ids)
        invoice_total = sum(invoices[iid].total_minor for iid in anomaly.invoice_ids)
        if anomaly.kind is AnomalyKind.CURRENCY_MISMATCH:
            assert anomaly.variance_minor is None
            assert (
                payments[anomaly.payment_ids[0]].currency
                != invoices[anomaly.invoice_ids[0]].currency
            )
        elif anomaly.kind is AnomalyKind.MISREFERENCED_PAYMENT:
            assert anomaly.variance_minor == 0 == invoice_total - paid_total
            assert payments[anomaly.payment_ids[0]].reference not in invoices
        else:
            assert anomaly.variance_minor == invoice_total - paid_total

    assert set(seen_invoices) == set(invoices)
    assert set(seen_payments) == set(payments)
    assert max(seen_invoices.values()) == 1
    assert max(seen_payments.values()) == 1


def test_same_seed_writes_identical_files(tmp_path: Path) -> None:
    write_dataset(generate_dataset(Config(seed=7)), tmp_path / "a")
    write_dataset(generate_dataset(Config(seed=7)), tmp_path / "b")

    assert digest(tmp_path / "a") == digest(tmp_path / "b")


def test_different_seeds_write_different_files(tmp_path: Path) -> None:
    write_dataset(generate_dataset(Config(seed=1)), tmp_path / "a")
    write_dataset(generate_dataset(Config(seed=2)), tmp_path / "b")

    assert digest(tmp_path / "a") != digest(tmp_path / "b")


@pytest.mark.parametrize("seed", SEEDS)
def test_first_revision_is_consistent(seed: int) -> None:
    assert_consistent(generate_dataset(Config(seed=seed)))


@pytest.mark.parametrize("seed", SEEDS)
def test_corrected_revision_is_consistent(seed: int) -> None:
    first = generate_dataset(Config(seed=seed))

    assert_consistent(apply_corrections(first, seed=seed))


def test_anomaly_counts_follow_the_configuration() -> None:
    config = Config(seed=3, invoices=400)
    dataset = generate_dataset(config)

    found = Counter(anomaly.kind for anomaly in dataset.truth.anomalies)

    assert dict(found) == config.anomaly_counts()


def test_every_kind_appears_with_the_default_configuration() -> None:
    dataset = generate_dataset(Config())

    assert {a.kind for a in dataset.truth.anomalies} == set(AnomalyKind)


def test_some_matched_payments_have_noisy_references() -> None:
    dataset = generate_dataset(Config(seed=5, invoices=300))
    payments = {p.payment_id: p for p in dataset.payments}

    noisy = [m for m in dataset.truth.matches if m.noisy_reference]

    assert noisy
    assert all(payments[m.payment_ids[0]].reference != m.invoice_id for m in noisy)


def test_corrections_fix_only_fixable_kinds_and_keep_the_rest() -> None:
    first = generate_dataset(Config(seed=11))
    second = apply_corrections(first, seed=11)

    resolved = set(second.truth.resolved)
    by_id = {a.anomaly_id: a for a in first.truth.anomalies}

    assert resolved
    assert all(by_id[anomaly_id].kind in FIXABLE_KINDS for anomaly_id in resolved)
    assert {a.anomaly_id for a in second.truth.anomalies} == set(by_id) - resolved
    assert second.revision == 2
    assert second.invoices == first.invoices


def test_corrections_leave_untouched_payments_as_they_were() -> None:
    first = generate_dataset(Config(seed=11))
    second = apply_corrections(first, seed=11)

    touched = set()
    old = {p.payment_id: p for p in first.payments}
    for payment in second.payments:
        if old.get(payment.payment_id) != payment:
            touched.add(payment.payment_id)

    by_id = {a.anomaly_id: a for a in first.truth.anomalies}
    expected = {pid for aid in second.truth.resolved for pid in by_id[aid].payment_ids}
    added = {p.payment_id for p in second.payments} - set(old)
    removed = set(old) - {p.payment_id for p in second.payments}

    assert touched - added <= expected
    assert removed <= expected
    surviving_order = [p.payment_id for p in second.payments if p.payment_id in old]
    assert surviving_order == [
        p.payment_id for p in first.payments if p.payment_id in surviving_order
    ]


def test_corrections_are_deterministic() -> None:
    first = generate_dataset(Config(seed=4))

    assert apply_corrections(first, seed=4) == apply_corrections(first, seed=4)


def test_files_use_the_documented_formats(tmp_path: Path) -> None:
    dataset = generate_dataset(Config(seed=2))
    write_dataset(dataset, tmp_path)

    with (tmp_path / "invoices.csv").open(newline="") as handle:
        invoice_rows = list(csv.DictReader(handle))
    with (tmp_path / "payments.csv").open(newline="") as handle:
        payment_rows = list(csv.DictReader(handle))

    assert list(invoice_rows[0]) == ["invoice_id", "customer_id", "issue_date", "currency", "total"]
    assert list(payment_rows[0]) == [
        "payment_id",
        "reference",
        "customer",
        "received_on",
        "ccy",
        "amount_paid",
    ]
    assert len(invoice_rows) == len(dataset.invoices)
    assert len(payment_rows) == len(dataset.payments)
    assert all(len(row["issue_date"]) == 10 and row["issue_date"][4] == "-" for row in invoice_rows)
    assert all(
        row["received_on"][2] == "/" and row["received_on"][5] == "/" for row in payment_rows
    )
    assert any("," in row["amount_paid"] for row in payment_rows)


@pytest.mark.parametrize("period", ["2026-13", "202601", "January", ""])
def test_rejects_an_invalid_period(period: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        generate_dataset(Config(period=period))


def test_rejects_rates_that_exceed_the_invoice_count() -> None:
    with pytest.raises(ValueError, match="exceed"):
        generate_dataset(Config(invoices=10, missing_rate=0.9, amount_mismatch_rate=0.9))

from dataclasses import replace
from pathlib import Path

import pytest

from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.model import AnomalyKind
from recon.datagen.writer import write_dataset
from recon.domain.money import Money
from recon.engine.model import ExceptionKind, ReconciliationResult
from recon.engine.reconcile import reconcile
from recon.evaluation.score import records_from_dataset, score
from recon.ingestion.normalize import normalize_csv

SEEDS = range(30)


def run(config: Config) -> ReconciliationResult:
    invoices, payments = records_from_dataset(generate_dataset(config))
    return reconcile(invoices, payments)


@pytest.mark.parametrize("seed", SEEDS)
def test_engine_agrees_with_the_ground_truth(seed: int) -> None:
    dataset = generate_dataset(Config(seed=seed, invoices=300, ambiguous_rate=0.04))
    invoices, payments = records_from_dataset(dataset)

    result = score(dataset, reconcile(invoices, payments))

    assert result.perfect, result


@pytest.mark.parametrize("seed", SEEDS)
def test_engine_agrees_with_the_ground_truth_after_corrections(seed: int) -> None:
    first = generate_dataset(Config(seed=seed, invoices=300, ambiguous_rate=0.04))
    second = apply_corrections(first, seed=seed)
    invoices, payments = records_from_dataset(second)

    result = score(second, reconcile(invoices, payments))

    assert result.perfect, result


def test_every_anomaly_kind_is_exercised_and_scored() -> None:
    dataset = generate_dataset(Config(seed=1, invoices=400))
    invoices, payments = records_from_dataset(dataset)

    result = score(dataset, reconcile(invoices, payments))

    assert set(result.per_kind) == {kind.value for kind in AnomalyKind}
    assert all(k.expected > 0 for k in result.per_kind.values())


def test_the_whole_pipeline_from_files_agrees_with_the_ground_truth(tmp_path: Path) -> None:
    dataset = generate_dataset(Config(seed=21, invoices=300))
    write_dataset(dataset, tmp_path)

    invoices = normalize_csv(tmp_path / "invoices.csv", INVOICE_MAPPING)
    payments = normalize_csv(tmp_path / "payments.csv", PAYMENT_MAPPING)
    result = score(dataset, reconcile(invoices.records, payments.records))

    assert result.perfect, result


# The scorer must notice mistakes, or a perfect score means nothing.


def test_the_scorer_notices_a_dropped_exception() -> None:
    dataset = generate_dataset(Config(seed=2))
    invoices, payments = records_from_dataset(dataset)
    full = reconcile(invoices, payments)

    result = score(dataset, replace(full, exceptions=full.exceptions[1:]))

    assert result.recall < 1.0
    assert result.grouped < result.anomalies
    assert not result.perfect


def test_the_scorer_notices_a_spurious_exception() -> None:
    dataset = generate_dataset(Config(seed=2))
    invoices, payments = records_from_dataset(dataset)
    full = reconcile(invoices, payments)
    matched = full.matches[0]
    fake = replace(
        full.exceptions[0],
        invoice_ids=(matched.invoice_id,),
        payment_ids=matched.payment_ids,
    )

    result = score(dataset, replace(full, exceptions=(*full.exceptions, fake)))

    assert result.precision < 1.0
    assert not result.perfect


def test_the_scorer_notices_a_wrong_kind() -> None:
    dataset = generate_dataset(Config(seed=2))
    invoices, payments = records_from_dataset(dataset)
    full = reconcile(invoices, payments)
    changed = replace(full.exceptions[0], kind=ExceptionKind.AMOUNT_MISMATCH)
    if full.exceptions[0].kind is ExceptionKind.AMOUNT_MISMATCH:
        changed = replace(full.exceptions[0], kind=ExceptionKind.PARTIAL_PAYMENT)

    result = score(dataset, replace(full, exceptions=(changed, *full.exceptions[1:])))

    assert result.kind_correct == result.anomalies - 1
    assert not result.perfect


def test_the_scorer_notices_a_wrong_variance() -> None:
    dataset = generate_dataset(Config(seed=2))
    invoices, payments = records_from_dataset(dataset)
    full = reconcile(invoices, payments)
    target = next(e for e in full.exceptions if e.variance is not None)
    assert target.variance is not None
    changed = replace(target, variance=target.variance + Money(1, target.variance.currency))
    exceptions = tuple(changed if e is target else e for e in full.exceptions)

    result = score(dataset, replace(full, exceptions=exceptions))

    assert result.variance_correct == result.anomalies - 1
    assert not result.perfect


def test_the_scorer_notices_a_missing_match() -> None:
    dataset = generate_dataset(Config(seed=2))
    invoices, payments = records_from_dataset(dataset)
    full = reconcile(invoices, payments)

    result = score(dataset, replace(full, matches=full.matches[1:]))

    assert result.missing_matches == 1
    assert not result.perfect

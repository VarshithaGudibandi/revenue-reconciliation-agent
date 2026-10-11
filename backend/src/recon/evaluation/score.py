"""Score a reconciliation result against the generator's ground truth."""

from collections import defaultdict
from dataclasses import dataclass

from recon.datagen.model import Dataset
from recon.domain.money import Money
from recon.engine.model import ReconciliationResult
from recon.ingestion.normalize import NormalizedRecord, normalize_reference


@dataclass(frozen=True)
class KindScore:
    expected: int = 0
    grouped: int = 0
    kind_correct: int = 0
    variance_correct: int = 0


@dataclass(frozen=True)
class Score:
    """How closely a result agrees with the ground truth.

    ``precision`` and ``recall`` are over records the engine flagged as needing attention.
    ``grouped`` counts truth anomalies the engine reported with exactly the same records,
    and ``kind_correct`` and ``variance_correct`` count those whose kind and variance
    also agree.
    """

    precision: float
    recall: float
    missing_matches: int
    extra_matches: int
    anomalies: int
    grouped: int
    kind_correct: int
    variance_correct: int
    per_kind: dict[str, KindScore]

    @property
    def perfect(self) -> bool:
        return (
            self.precision == 1.0
            and self.recall == 1.0
            and self.missing_matches == 0
            and self.extra_matches == 0
            and self.grouped == self.kind_correct == self.variance_correct == self.anomalies
        )


def records_from_dataset(
    dataset: Dataset,
) -> tuple[list[NormalizedRecord], list[NormalizedRecord]]:
    """The dataset as normalized records, without going through files."""
    invoices = [
        NormalizedRecord(
            source_row=number,
            record_id=invoice.invoice_id,
            reference=None,
            counterparty=invoice.customer_id,
            occurred_on=invoice.issue_date,
            amount=Money(invoice.total_minor, invoice.currency),
        )
        for number, invoice in enumerate(dataset.invoices, start=1)
    ]
    payments = [
        NormalizedRecord(
            source_row=number,
            record_id=payment.payment_id,
            reference=normalize_reference(payment.reference) or None,
            counterparty=payment.customer_id,
            occurred_on=payment.received_on,
            amount=Money(payment.amount_minor, payment.currency),
        )
        for number, payment in enumerate(dataset.payments, start=1)
    ]
    return invoices, payments


def score(dataset: Dataset, result: ReconciliationResult) -> Score:
    truth = dataset.truth

    flagged = {("invoice", i) for e in result.exceptions for i in e.invoice_ids} | {
        ("payment", p) for e in result.exceptions for p in e.payment_ids
    }
    expected = {("invoice", i) for a in truth.anomalies for i in a.invoice_ids} | {
        ("payment", p) for a in truth.anomalies for p in a.payment_ids
    }
    true_positives = len(flagged & expected)
    precision = true_positives / len(flagged) if flagged else 1.0
    recall = true_positives / len(expected) if expected else 1.0

    engine_matches = {(m.invoice_id, frozenset(m.payment_ids)) for m in result.matches}
    truth_matches = {(m.invoice_id, frozenset(m.payment_ids)) for m in truth.matches}

    by_records = {
        (frozenset(e.invoice_ids), frozenset(e.payment_ids)): e for e in result.exceptions
    }
    per_kind: dict[str, KindScore] = defaultdict(KindScore)
    grouped = kind_correct = variance_correct = 0
    for anomaly in truth.anomalies:
        counts = per_kind[anomaly.kind.value]
        found = by_records.get((frozenset(anomaly.invoice_ids), frozenset(anomaly.payment_ids)))
        grouped_here = kind_here = variance_here = 0
        if found is not None:
            grouped_here = 1
            kind_here = int(found.kind.value == anomaly.kind.value)
            engine_variance = None if found.variance is None else found.variance.minor
            variance_here = int(engine_variance == anomaly.variance_minor)
        grouped += grouped_here
        kind_correct += kind_here
        variance_correct += variance_here
        per_kind[anomaly.kind.value] = KindScore(
            counts.expected + 1,
            counts.grouped + grouped_here,
            counts.kind_correct + kind_here,
            counts.variance_correct + variance_here,
        )

    return Score(
        precision=precision,
        recall=recall,
        missing_matches=len(truth_matches - engine_matches),
        extra_matches=len(engine_matches - truth_matches),
        anomalies=len(truth.anomalies),
        grouped=grouped,
        kind_correct=kind_correct,
        variance_correct=variance_correct,
        per_kind=dict(per_kind),
    )

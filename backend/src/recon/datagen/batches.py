"""Synthetic processor payouts and the bank deposits that carry them, with a known truth.

A deposit usually holds several payouts. Most are clean: the payouts name their deposit in a
reference column and add up to it. The rest are seeded problems of a known kind, so that the
engine can be scored against what is really there:

* ``deposit_mismatch``: the payouts name the deposit but it is short by a fee.
* ``missing_payouts``: a deposit that no payout accounts for.
* ``undeposited_payout``: a payout that names a deposit that is not in the bank file.
* ``proposed_grouping``: payouts with no reference that add up to one deposit.
* ``ambiguous_grouping``: unreferenced payouts that add up to a deposit in two different ways.
* ``currency_mismatch``: a deposit in another currency than the payouts that name it.

Everything is derived from the seed.
"""

import csv
import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from recon.domain.money import format_amount
from recon.ingestion.normalize import ColumnMapping

PROCESSORS = (("STRIPE", "USD"), ("PAYPAL", "USD"), ("ADYEN", "EUR"))


class BatchAnomalyKind(StrEnum):
    DEPOSIT_MISMATCH = "deposit_mismatch"
    MISSING_PAYOUTS = "missing_payouts"
    UNDEPOSITED_PAYOUT = "undeposited_payout"
    PROPOSED_GROUPING = "proposed_grouping"
    AMBIGUOUS_GROUPING = "ambiguous_grouping"
    CURRENCY_MISMATCH = "currency_mismatch"


@dataclass(frozen=True)
class Payout:
    payout_id: str
    reference: str
    processor: str
    paid_on: date
    currency: str
    amount_minor: int


@dataclass(frozen=True)
class Deposit:
    deposit_id: str
    processor: str
    booked_on: date
    currency: str
    amount_minor: int
    memo: str = ""


@dataclass(frozen=True)
class BatchMatch:
    deposit_id: str
    payout_ids: tuple[str, ...]


@dataclass(frozen=True)
class BatchAnomaly:
    """A seeded problem. ``variance_minor`` is deposits minus payouts, ``None`` when the
    currencies differ."""

    anomaly_id: str
    kind: BatchAnomalyKind
    deposit_ids: tuple[str, ...]
    payout_ids: tuple[str, ...]
    variance_minor: int | None
    currency: str


@dataclass(frozen=True)
class BatchDataset:
    seed: int
    period: str
    deposits: tuple[Deposit, ...]
    payouts: tuple[Payout, ...]
    matches: tuple[BatchMatch, ...]
    anomalies: tuple[BatchAnomaly, ...]


@dataclass(frozen=True)
class BatchConfig:
    seed: int = 0
    period: str = "2026-01"
    deposits: int = 100
    mismatch_rate: float = 0.05
    missing_rate: float = 0.03
    undeposited_rate: float = 0.04
    proposed_rate: float = 0.05
    ambiguous_rate: float = 0.02
    currency_rate: float = 0.02

    def __post_init__(self) -> None:
        if self.deposits < 1:
            raise ValueError("deposits must be at least 1")
        rates = (
            self.mismatch_rate,
            self.missing_rate,
            self.undeposited_rate,
            self.proposed_rate,
            self.ambiguous_rate,
            self.currency_rate,
        )
        if any(r < 0 for r in rates) or sum(rates) > 1:
            raise ValueError("the anomaly rates must not be negative and must add up to at most 1")


def _noisy(reference: str, rng: random.Random) -> str:
    return rng.choice(
        (reference, reference, reference.lower(), reference.replace("-", ""), f" {reference} ")
    )


def generate_batches(config: BatchConfig) -> BatchDataset:
    rng = random.Random(config.seed)  # noqa: S311
    year, month = (int(part) for part in config.period.split("-"))
    first = date(year, month, 1)
    deposits: list[Deposit] = []
    payouts: list[Payout] = []
    matches: list[BatchMatch] = []
    anomalies: list[BatchAnomaly] = []
    counters = {"payout": 0, "deposit": 0}

    def new_id(kind: str, prefix: str) -> str:
        counters[kind] += 1
        return f"{prefix}-{counters[kind]:06d}"

    def seed_anomaly(
        kind: BatchAnomalyKind,
        deposit_ids: tuple[str, ...],
        payout_ids: tuple[str, ...],
        variance: int | None,
        currency: str,
    ) -> None:
        anomalies.append(
            BatchAnomaly(
                f"A{len(anomalies) + 1:04d}",
                kind,
                deposit_ids,
                tuple(sorted(payout_ids)),
                variance,
                currency,
            )
        )

    def amounts(count: int) -> list[int]:
        return [rng.randint(5_000, 500_000) for _ in range(count)]

    def paid_days(count: int, deposit_day: date) -> list[date]:
        return [deposit_day - timedelta(days=rng.randint(1, 7)) for _ in range(count)]

    def add_group(
        processor: str,
        currency: str,
        deposit_day: date,
        parts: list[int],
        *,
        deposit_amount: int,
        deposit_currency: str,
        referenced: bool,
    ) -> tuple[str, list[str]]:
        deposit_id = new_id("deposit", "BNK")
        deposits.append(
            Deposit(deposit_id, processor, deposit_day, deposit_currency, deposit_amount)
        )
        ids = []
        for amount, paid in zip(parts, paid_days(len(parts), deposit_day), strict=True):
            payout_id = new_id("payout", "PO")
            ids.append(payout_id)
            reference = _noisy(deposit_id, rng) if referenced else ""
            payouts.append(Payout(payout_id, reference, processor, paid, currency, amount))
        return deposit_id, ids

    order = (
        (BatchAnomalyKind.DEPOSIT_MISMATCH, config.mismatch_rate),
        (BatchAnomalyKind.MISSING_PAYOUTS, config.missing_rate),
        (BatchAnomalyKind.UNDEPOSITED_PAYOUT, config.undeposited_rate),
        (BatchAnomalyKind.PROPOSED_GROUPING, config.proposed_rate),
        (BatchAnomalyKind.AMBIGUOUS_GROUPING, config.ambiguous_rate),
        (BatchAnomalyKind.CURRENCY_MISMATCH, config.currency_rate),
    )

    for index in range(config.deposits):
        processor, currency = PROCESSORS[index % len(PROCESSORS)]
        day = first + timedelta(days=rng.randint(8, 27))
        roll, threshold = rng.random(), 0.0
        kind: BatchAnomalyKind | None = None
        for candidate, rate in order:
            threshold += rate
            if roll < threshold:
                kind = candidate
                break

        if kind is BatchAnomalyKind.MISSING_PAYOUTS:
            deposit_id = new_id("deposit", "BNK")
            amount = rng.randint(20_000, 800_000)
            deposits.append(Deposit(deposit_id, processor, day, currency, amount, "UNEXPLAINED"))
            seed_anomaly(kind, (deposit_id,), (), amount, currency)
        elif kind is BatchAnomalyKind.UNDEPOSITED_PAYOUT:
            payout_id = new_id("payout", "PO")
            amount = rng.randint(5_000, 500_000)
            paid = day - timedelta(days=rng.randint(1, 7))
            payouts.append(
                Payout(payout_id, f"BNK-9{index:05d}", processor, paid, currency, amount)
            )
            seed_anomaly(kind, (), (payout_id,), -amount, currency)
        elif kind is BatchAnomalyKind.AMBIGUOUS_GROUPING:
            # two different pairs of payouts that each add up to the deposit
            first_pair = amounts(2)
            target = sum(first_pair)
            low = rng.randint(5_000, target - 5_000)
            parts = [*first_pair, low, target - low]
            deposit_id, ids = add_group(
                processor,
                currency,
                day,
                parts,
                deposit_amount=target,
                deposit_currency=currency,
                referenced=False,
            )
            seed_anomaly(kind, (deposit_id,), tuple(ids), target - sum(parts), currency)
        elif kind is BatchAnomalyKind.PROPOSED_GROUPING:
            parts = amounts(rng.randint(2, 4))
            deposit_id, ids = add_group(
                processor,
                currency,
                day,
                parts,
                deposit_amount=sum(parts),
                deposit_currency=currency,
                referenced=False,
            )
            seed_anomaly(kind, (deposit_id,), tuple(ids), 0, currency)
        elif kind is BatchAnomalyKind.DEPOSIT_MISMATCH:
            parts = amounts(rng.randint(1, 4))
            fee = rng.randint(100, max(101, sum(parts) // 10))
            deposit_id, ids = add_group(
                processor,
                currency,
                day,
                parts,
                deposit_amount=sum(parts) - fee,
                deposit_currency=currency,
                referenced=True,
            )
            seed_anomaly(kind, (deposit_id,), tuple(ids), -fee, currency)
        elif kind is BatchAnomalyKind.CURRENCY_MISMATCH:
            parts = amounts(rng.randint(1, 3))
            other = "GBP" if currency != "GBP" else "USD"
            deposit_id, ids = add_group(
                processor,
                currency,
                day,
                parts,
                deposit_amount=sum(parts),
                deposit_currency=other,
                referenced=True,
            )
            seed_anomaly(kind, (deposit_id,), tuple(ids), None, currency)
        else:
            parts = amounts(rng.choices((1, 2, 3, 4, 5), weights=(40, 30, 15, 10, 5))[0])
            deposit_id, ids = add_group(
                processor,
                currency,
                day,
                parts,
                deposit_amount=sum(parts),
                deposit_currency=currency,
                referenced=True,
            )
            matches.append(BatchMatch(deposit_id, tuple(sorted(ids))))

    return BatchDataset(
        config.seed,
        config.period,
        tuple(sorted(deposits, key=lambda d: d.deposit_id)),
        tuple(sorted(payouts, key=lambda p: p.payout_id)),
        tuple(sorted(matches, key=lambda m: m.deposit_id)),
        tuple(anomalies),
    )


# The exports, in the layout a processor and a bank might use.
PAYOUT_HEADER = ("payout_id", "processor", "paid_on", "currency", "amount", "bank_reference")
DEPOSIT_HEADER = ("bank_txn_id", "account", "booked_on", "currency", "amount", "memo")

PAYOUT_MAPPING = ColumnMapping(
    record_id="payout_id",
    counterparty="processor",
    date="paid_on",
    amount="amount",
    currency="currency",
    reference="bank_reference",
    date_format="%Y-%m-%d",
)
DEPOSIT_MAPPING = ColumnMapping(
    record_id="bank_txn_id",
    counterparty="account",
    date="booked_on",
    amount="amount",
    currency="currency",
    text="memo",
    date_format="%Y-%m-%d",
)


def truth_to_dict(dataset: BatchDataset) -> dict[str, Any]:
    return {
        "seed": dataset.seed,
        "period": dataset.period,
        "matches": [
            {"deposit_id": m.deposit_id, "payout_ids": list(m.payout_ids)} for m in dataset.matches
        ],
        "anomalies": [
            {
                "anomaly_id": a.anomaly_id,
                "kind": a.kind.value,
                "deposit_ids": list(a.deposit_ids),
                "payout_ids": list(a.payout_ids),
                "variance_minor": a.variance_minor,
                "currency": a.currency,
            }
            for a in dataset.anomalies
        ],
    }


def write_batches(dataset: BatchDataset, directory: Path) -> None:
    """Write ``payouts.csv``, ``deposits.csv`` and ``ground_truth.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "payouts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(PAYOUT_HEADER)
        for p in dataset.payouts:
            writer.writerow(
                [
                    p.payout_id,
                    p.processor,
                    p.paid_on.isoformat(),
                    p.currency,
                    format_amount(p.amount_minor, p.currency),
                    p.reference,
                ]
            )
    with (directory / "deposits.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(DEPOSIT_HEADER)
        for d in dataset.deposits:
            writer.writerow(
                [
                    d.deposit_id,
                    d.processor,
                    d.booked_on.isoformat(),
                    d.currency,
                    format_amount(d.amount_minor, d.currency),
                    d.memo,
                ]
            )
    text = json.dumps(truth_to_dict(dataset), indent=2, sort_keys=True)
    (directory / "ground_truth.json").write_text(text + "\n", encoding="utf-8")

import hashlib
import json
import re
from pathlib import Path

import pytest

from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.model import Anomaly, AnomalyKind, Dataset
from recon.datagen.text import MEMO_KINDS, PARAPHRASES, SERVICES, add_text
from recon.datagen.writer import write_dataset

SEEDS = range(12)


def dataset_for(seed: int) -> Dataset:
    return generate_dataset(Config(seed=seed, invoices=200, ambiguous_rate=0.06))


def ambiguous(dataset: Dataset) -> list[Anomaly]:
    return [a for a in dataset.truth.anomalies if a.kind is AnomalyKind.AMBIGUOUS_PAYMENT]


def numbers_in(text: str) -> set[int]:
    return {int(run) for run in re.findall(r"\d+", text)}


def number_of(invoice_id: str) -> int:
    return int(invoice_id.rsplit("-", 1)[1])


def test_adding_text_does_not_change_a_single_number_date_or_id(tmp_path: Path) -> None:
    """These are the hashes of the default exports before descriptions and memos existed."""
    dataset = generate_dataset(Config(seed=0, period="2026-01", invoices=120))
    write_dataset(dataset, tmp_path)

    def sha(name: str) -> str:
        return hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()

    assert sha("invoices.csv") == "61d6fd0ea7ee8d543951cb0f5dbf9d8eb0fd798fd6d6ec474d44d56f5f154aa7"
    assert sha("payments.csv") == "e3303915e4435e9311fed5aa1565439d1966fb0ea934d97d1bbbef97e1210fcc"


def test_the_text_is_the_same_every_time() -> None:
    first, second = dataset_for(3), dataset_for(3)

    assert first == second
    assert add_text(first) == add_text(first)


def test_every_invoice_says_what_it_is_for() -> None:
    dataset = dataset_for(1)

    for invoice in dataset.invoices:
        service, month, year = invoice.description.rsplit(" ", 2)
        assert service in SERVICES
        assert month == "Jan"
        assert year == "2026"


@pytest.mark.parametrize("seed", SEEDS)
def test_look_alike_invoices_read_differently(seed: int) -> None:
    dataset = dataset_for(seed)
    descriptions = {i.invoice_id: i.description for i in dataset.invoices}

    for anomaly in ambiguous(dataset):
        group = [descriptions[i] for i in anomaly.invoice_ids]
        assert len(set(group)) == len(group)


def test_all_four_kinds_of_memo_occur_and_are_recorded() -> None:
    kinds = {a.memo_kind for seed in SEEDS for a in ambiguous(dataset_for(seed))}

    assert kinds == set(MEMO_KINDS)
    for seed in SEEDS:
        for anomaly in dataset_for(seed).truth.anomalies:
            if anomaly.kind is not AnomalyKind.AMBIGUOUS_PAYMENT:
                assert anomaly.memo_kind is None


@pytest.mark.parametrize("seed", SEEDS)
def test_an_explicit_memo_names_the_intended_invoice_and_none_of_the_others(seed: int) -> None:
    dataset = dataset_for(seed)
    memos = {p.payment_id: p.memo for p in dataset.payments}

    for anomaly in ambiguous(dataset):
        if anomaly.memo_kind != "explicit":
            continue
        written = numbers_in(memos[anomaly.payment_ids[0]])
        intended = number_of(anomaly.intended_invoice_id or "")
        others = {number_of(i) for i in anomaly.invoice_ids} - {intended}
        assert intended in written
        assert not written & others


@pytest.mark.parametrize("seed", SEEDS)
def test_a_descriptive_memo_matches_only_the_intended_invoice(seed: int) -> None:
    dataset = dataset_for(seed)
    memos = {p.payment_id: p.memo.lower() for p in dataset.payments}
    services = {i.invoice_id: i.description.rsplit(" ", 2)[0].lower() for i in dataset.invoices}

    for anomaly in ambiguous(dataset):
        if anomaly.memo_kind != "descriptive":
            continue
        memo = memos[anomaly.payment_ids[0]]
        matching = [i for i in anomaly.invoice_ids if services[i] in memo]
        assert matching == [anomaly.intended_invoice_id]


@pytest.mark.parametrize("seed", SEEDS)
def test_a_useless_memo_cannot_tell_the_invoices_apart(seed: int) -> None:
    dataset = dataset_for(seed)
    memos = {p.payment_id: p.memo.lower() for p in dataset.payments}
    services = {i.invoice_id: i.description.rsplit(" ", 2)[0].lower() for i in dataset.invoices}

    for anomaly in ambiguous(dataset):
        if anomaly.memo_kind != "useless":
            continue
        memo = memos[anomaly.payment_ids[0]]
        assert not any(services[i] in memo for i in anomaly.invoice_ids)
        assert not numbers_in(memo)


def test_other_payments_carry_generic_text_or_none() -> None:
    dataset = dataset_for(2)
    ambiguous_payments = {pid for a in ambiguous(dataset) for pid in a.payment_ids}

    memos = {p.memo for p in dataset.payments if p.payment_id not in ambiguous_payments}

    assert memos <= {"", "Payment", "Bank transfer", "Thank you", "As per invoice"}


def test_a_corrected_revision_keeps_the_text_it_did_not_touch() -> None:
    first = dataset_for(4)
    second = apply_corrections(first, seed=4)

    before = {p.payment_id: p.memo for p in first.payments}
    after = {p.payment_id: p.memo for p in second.payments}

    assert all(after[pid] == memo for pid, memo in before.items() if pid in after)
    assert second.invoices == first.invoices


def test_the_ground_truth_file_records_the_memo_kind(tmp_path: Path) -> None:
    dataset = dataset_for(5)
    write_dataset(dataset, tmp_path)

    truth = json.loads((tmp_path / "ground_truth.json").read_text(encoding="utf-8"))

    recorded = {a["anomaly_id"]: a["memo_kind"] for a in truth["anomalies"]}
    expected = {a.anomaly_id: a.memo_kind for a in dataset.truth.anomalies}
    assert recorded == expected


def test_no_paraphrase_contains_a_word_of_the_service_it_stands_for() -> None:
    for service, phrases in PARAPHRASES.items():
        words = {w for w in re.findall(r"[a-zà-ÿ]{4,}", service.lower())}
        for phrase in phrases:
            assert not words & set(re.findall(r"[a-zà-ÿ]+", phrase.lower())), (service, phrase)
    assert set(PARAPHRASES) == set(SERVICES)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_paraphrased_memo_names_only_the_intended_service_in_other_words(seed: int) -> None:
    dataset = dataset_for(seed)
    memos = {p.payment_id: p.memo.lower() for p in dataset.payments}
    services = {i.invoice_id: i.description.rsplit(" ", 2)[0] for i in dataset.invoices}

    for anomaly in ambiguous(dataset):
        if anomaly.memo_kind != "paraphrased":
            continue
        memo = memos[anomaly.payment_ids[0]]
        matching = [
            i
            for i in anomaly.invoice_ids
            if any(p.lower() in memo for p in PARAPHRASES[services[i]])
        ]
        assert matching == [anomaly.intended_invoice_id]
        assert not any(services[i].lower() in memo for i in anomaly.invoice_ids)

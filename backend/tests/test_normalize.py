from datetime import date
from pathlib import Path

import pytest

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.model import AnomalyKind
from recon.datagen.writer import write_dataset
from recon.domain.money import Money
from recon.ingestion.normalize import (
    ColumnMapping,
    MappingError,
    NormalizationError,
    NormalizationResult,
    normalize_csv,
    normalize_reference,
    parse_date,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INV-000123", "INV000123"),
        ("inv-000123", "INV000123"),
        ("INV000123", "INV000123"),
        ("INV 000123", "INV000123"),
        ("  INV-000123  ", "INV000123"),
        ("inv_000123.", "INV000123"),
        ("", ""),
        ("---", ""),
    ],
)
def test_normalizes_references(raw: str, expected: str) -> None:
    assert normalize_reference(raw) == expected


def test_parses_dates_with_the_stated_format() -> None:
    assert parse_date("01/02/2026", "%m/%d/%Y") == date(2026, 1, 2)
    assert parse_date("01/02/2026", "%d/%m/%Y") == date(2026, 2, 1)
    assert parse_date(" 2026-03-04 ", "%Y-%m-%d") == date(2026, 3, 4)


@pytest.mark.parametrize("text", ["", "2026-13-01", "13/01/2026", "tomorrow", "02/30/2026"])
def test_rejects_dates_that_do_not_match(text: str) -> None:
    with pytest.raises(NormalizationError):
        parse_date(text, "%m/%d/%Y")


def test_normalizes_the_generated_files_and_agrees_with_the_ground_truth(tmp_path: Path) -> None:
    dataset = generate_dataset(Config(seed=9, invoices=300))
    write_dataset(dataset, tmp_path)

    invoices = normalize_csv(tmp_path / "invoices.csv", INVOICE_MAPPING)
    payments = normalize_csv(tmp_path / "payments.csv", PAYMENT_MAPPING)

    assert invoices.rejected == ()
    assert payments.rejected == ()
    assert [r.record_id for r in invoices.records] == [i.invoice_id for i in dataset.invoices]
    assert [r.record_id for r in payments.records] == [p.payment_id for p in dataset.payments]
    for record, invoice in zip(invoices.records, dataset.invoices, strict=True):
        assert record.amount == Money(invoice.total_minor, invoice.currency)
        assert record.occurred_on == invoice.issue_date
        assert record.counterparty == invoice.customer_id
    for record, payment in zip(payments.records, dataset.payments, strict=True):
        assert record.amount == Money(payment.amount_minor, payment.currency)
        assert record.occurred_on == payment.received_on

    invoice_keys = {r.key for r in invoices.records}
    by_payment = {r.record_id: r for r in payments.records}

    # Every matched payment, including the ones with messy references, resolves to its invoice.
    for match in dataset.truth.matches:
        for payment_id in match.payment_ids:
            assert by_payment[payment_id].reference == normalize_reference(match.invoice_id)
    assert any(m.noisy_reference for m in dataset.truth.matches)

    # Payments that point at no invoice stay unresolved after normalization.
    for anomaly in dataset.truth.anomalies:
        if anomaly.kind in (AnomalyKind.MISREFERENCED_PAYMENT, AnomalyKind.UNMATCHED_PAYMENT):
            assert by_payment[anomaly.payment_ids[0]].reference not in invoice_keys


def test_reports_rejected_rows_with_reasons_and_keeps_the_good_ones(tmp_path: Path) -> None:
    path = tmp_path / "payments.csv"
    path.write_text(
        "payment_id,reference,customer,received_on,ccy,amount_paid\n"
        'P1,INV-1,C1,01/05/2026,USD,"1,000.00"\n'
        "P2,INV-2,C1,01/05/2026,USD,12.345\n"
        "P3,INV-3,C1,31/05/2026,USD,10.00\n"
        "P4,INV-4,C1,01/05/2026,XXX,10.00\n"
        "P1,INV-5,C1,01/05/2026,USD,10.00\n"
        ",INV-6,C1,01/05/2026,USD,10.00\n"
        "P7,INV-7,,01/05/2026,USD,10.00\n"
        "P8,,C2,01/06/2026,eur,5.00\n",
        encoding="utf-8",
    )

    result = normalize_csv(path, PAYMENT_MAPPING)

    assert [r.record_id for r in result.records] == ["P1", "P8"]
    assert result.records[0].amount == Money(100_000, "USD")
    assert result.records[1].amount == Money(500, "EUR")
    assert result.records[1].reference is None
    assert [(r.source_row, r.reason.split(" ")[0]) for r in result.rejected] == [
        (2, "'12.345'"),
        (3, "'31/05/2026'"),
        (4, "unknown"),
        (5, "duplicate"),
        (6, "record"),
        (7, "counterparty"),
    ]
    assert result.rejected[3].raw["payment_id"] == "P1"


def test_missing_columns_fail_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "invoices.csv"
    path.write_text("invoice_id,customer_id\nINV-1,C1\n", encoding="utf-8")

    with pytest.raises(MappingError, match="issue_date, total, currency"):
        normalize_csv(path, INVOICE_MAPPING)


def test_reads_a_file_with_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "invoices.csv"
    path.write_text(
        "invoice_id,customer_id,issue_date,currency,total\nINV-1,C1,2026-01-05,USD,10.00\n",
        encoding="utf-8-sig",
    )

    result = normalize_csv(path, INVOICE_MAPPING)

    assert result.rejected == ()
    assert result.records[0].record_id == "INV-1"


def test_reads_european_number_formats(tmp_path: Path) -> None:
    path = tmp_path / "payments.csv"
    european = ColumnMapping(
        record_id="id",
        counterparty="who",
        date="when",
        amount="sum",
        currency="cur",
        date_format="%d.%m.%Y",
        decimal_separator=",",
    )
    path.write_text('id,who,when,sum,cur\nA1,C1,31.01.2026,"1.234,50",EUR\n', encoding="utf-8")

    result = normalize_csv(path, european)

    assert result.rejected == ()
    assert result.records[0].amount == Money(123_450, "EUR")
    assert result.records[0].occurred_on == date(2026, 1, 31)


def _read(tmp_path: Path, text: str, mapping: ColumnMapping) -> NormalizationResult:
    path = tmp_path / "f.csv"
    path.write_text(text, encoding="utf-8")
    return normalize_csv(path, mapping)


TEXT_MAPPING = ColumnMapping(
    record_id="id",
    counterparty="who",
    date="day",
    amount="amt",
    currency="ccy",
    text="note",
    date_format="%Y-%m-%d",
)


def test_free_text_is_read_with_its_whitespace_collapsed_and_empty_text_is_none(
    tmp_path: Path,
) -> None:
    result = _read(
        tmp_path,
        "id,who,day,amt,ccy,note\n"
        'A1,C1,2026-01-05,10.00,USD,"  pls   apply to\tinv 121 "\n'
        "A2,C1,2026-01-06,10.00,USD,\n",
        TEXT_MAPPING,
    )

    assert [r.text for r in result.records] == ["pls apply to inv 121", None]


def test_a_missing_text_column_is_reported_like_any_other(tmp_path: Path) -> None:
    with pytest.raises(MappingError, match="note"):
        _read(tmp_path, "id,who,day,amt,ccy\nA1,C1,2026-01-05,10.00,USD\n", TEXT_MAPPING)


def test_a_mapping_without_text_reads_none_and_keeps_the_hash_it_always_had(
    tmp_path: Path,
) -> None:
    without = TEXT_MAPPING.model_copy(update={"text": None})
    csv = "id,who,day,amt,ccy,note\nA1,C1,2026-01-05,10.00,USD,hello\n"

    plain = _read(tmp_path, csv, without).records[0]
    texted = _read(tmp_path, csv, TEXT_MAPPING).records[0]

    assert plain.text is None
    assert texted.text == "hello"
    assert plain.content_hash != texted.content_hash
    # the hash of a record without text is the one computed before text existed
    import hashlib
    import json

    legacy = [plain.record_id, plain.reference, plain.counterparty, "2026-01-05", 1000, "USD"]
    expected = hashlib.sha256(json.dumps(legacy, separators=(",", ":")).encode()).hexdigest()
    assert plain.content_hash == expected


def test_changed_text_is_a_changed_record(tmp_path: Path) -> None:
    head = "id,who,day,amt,ccy,note\n"
    a = _read(tmp_path, head + "A1,C1,2026-01-05,10.00,USD,hosting\n", TEXT_MAPPING).records[0]
    b = _read(tmp_path, head + "A1,C1,2026-01-05,10.00,USD,training\n", TEXT_MAPPING).records[0]

    assert a.content_hash != b.content_hash

import csv
import dataclasses
import json
from datetime import datetime
from pathlib import Path

import pytest

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.model import Dataset
from recon.datagen.styles import (
    INVOICE_STYLES,
    PAYMENT_STYLES,
    Column,
    ExportStyles,
    SourceStyle,
    date_format_ambiguous,
    swapped_day_month,
)
from recon.datagen.writer import write_dataset
from recon.domain.money import parse_amount
from recon.ingestion.normalize import ColumnMapping, normalize_csv, normalize_reference

SEEDS = (0, 1, 2)


def dataset_for(seed: int) -> Dataset:
    return generate_dataset(Config(seed=seed, invoices=120))


def read_columns(path: Path) -> dict[str, list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return {header: [row[header] for row in rows] for header in rows[0]}


def test_the_default_styles_are_the_exports_the_demo_and_tests_were_built_on() -> None:
    expected_invoices = ColumnMapping(
        record_id="invoice_id",
        counterparty="customer_id",
        date="issue_date",
        amount="total",
        currency="currency",
        date_format="%Y-%m-%d",
    )
    assert expected_invoices == INVOICE_MAPPING
    expected_payments = ColumnMapping(
        record_id="payment_id",
        counterparty="customer",
        date="received_on",
        amount="amount_paid",
        currency="ccy",
        reference="reference",
        date_format="%m/%d/%Y",
    )
    assert expected_payments == PAYMENT_MAPPING


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(INVOICE_STYLES))
def test_invoices_read_back_exactly_with_the_true_mapping(
    name: str, seed: int, tmp_path: Path
) -> None:
    dataset = dataset_for(seed)
    write_dataset(dataset, tmp_path, ExportStyles(invoices=name))

    result = normalize_csv(tmp_path / "invoices.csv", INVOICE_STYLES[name].mapping())

    assert result.rejected == ()
    assert [
        (r.record_id, r.counterparty, r.occurred_on, r.amount.minor, r.amount.currency)
        for r in result.records
    ] == [
        (i.invoice_id, i.customer_id, i.issue_date, i.total_minor, i.currency)
        for i in dataset.invoices
    ]


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(PAYMENT_STYLES))
def test_payments_read_back_exactly_with_the_true_mapping(
    name: str, seed: int, tmp_path: Path
) -> None:
    dataset = dataset_for(seed)
    write_dataset(dataset, tmp_path, ExportStyles(payments=name))

    result = normalize_csv(tmp_path / "payments.csv", PAYMENT_STYLES[name].mapping())

    has_reference = PAYMENT_STYLES[name].header("reference") is not None
    assert result.rejected == ()
    assert [
        (r.record_id, r.counterparty, r.occurred_on, r.amount.minor, r.amount.currency, r.reference)
        for r in result.records
    ] == [
        (
            p.payment_id,
            p.customer_id,
            # the processor style carries a time of day; reading it back keeps the date
            p.received_on,
            p.amount_minor,
            p.currency,
            (normalize_reference(p.reference) or None) if has_reference else None,
        )
        for p in dataset.payments
    ]


@pytest.mark.parametrize("name", ["german", "spanish"])
def test_a_wrong_decimal_separator_fails_loudly_instead_of_misreading(
    name: str, tmp_path: Path
) -> None:
    dataset = dataset_for(0)
    write_dataset(dataset, tmp_path, ExportStyles(invoices=name))
    wrong = INVOICE_STYLES[name].mapping().model_copy(update={"decimal_separator": "."})

    result = normalize_csv(tmp_path / "invoices.csv", wrong)

    truth = {i.invoice_id: i.total_minor for i in dataset.invoices}
    assert all(r.amount.minor == truth[r.record_id] for r in result.records)
    assert len(result.rejected) == len(dataset.invoices)


@pytest.mark.parametrize("name", sorted(INVOICE_STYLES))
def test_distractor_amounts_add_up_but_are_not_the_gross_amount(name: str, tmp_path: Path) -> None:
    style = INVOICE_STYLES[name]
    net, tax = style.header("net_amount"), style.header("tax_amount")
    if net is None or tax is None:
        pytest.skip("this style has no net and tax columns")
    dataset = dataset_for(0)
    write_dataset(dataset, tmp_path, ExportStyles(invoices=name))
    columns = read_columns(tmp_path / "invoices.csv")

    for index, invoice in enumerate(dataset.invoices):
        parts = [
            parse_amount(
                columns[header][index], invoice.currency, decimal_separator=style.decimal_separator
            )
            for header in (net, tax)
        ]
        assert sum(parts) == invoice.total_minor
        assert parts[0] != invoice.total_minor or parts[1] == 0


@pytest.mark.parametrize("name", sorted(INVOICE_STYLES))
def test_the_due_date_is_after_the_issue_date_in_every_style(name: str, tmp_path: Path) -> None:
    style = INVOICE_STYLES[name]
    due = style.header("due_date")
    if due is None:
        pytest.skip("this style has no due date")
    write_dataset(dataset_for(0), tmp_path, ExportStyles(invoices=name))
    columns = read_columns(tmp_path / "invoices.csv")
    issued = [datetime.strptime(v, style.date_format) for v in columns[style.header("date") or ""]]  # noqa: DTZ007
    due_dates = [datetime.strptime(v, style.date_format) for v in columns[due]]  # noqa: DTZ007

    assert all((d - i).days == 30 for d, i in zip(due_dates, issued, strict=True))


def test_the_processor_fee_is_smaller_than_the_amount(tmp_path: Path) -> None:
    style = PAYMENT_STYLES["processor"]
    dataset = dataset_for(0)
    write_dataset(dataset, tmp_path, ExportStyles(payments="processor"))
    fees = read_columns(tmp_path / "payments.csv")[style.header("fee_amount") or ""]

    for payment, fee in zip(dataset.payments, fees, strict=True):
        assert 0 <= parse_amount(fee, payment.currency) < payment.amount_minor


def test_every_style_hides_the_fields_it_does_not_map() -> None:
    for style in (*INVOICE_STYLES.values(), *PAYMENT_STYLES.values()):
        mapped = set(style.mapping().required_columns())
        distractors = {c.header for c in style.distractor_columns()}
        assert mapped.isdisjoint(distractors)
        assert {c.header for c in style.columns} == mapped | distractors


def test_the_styles_cover_what_a_proposer_must_be_able_to_handle() -> None:
    styles = (*INVOICE_STYLES.values(), *PAYMENT_STYLES.values())

    assert {s.decimal_separator for s in styles} == {".", ","}
    assert len({s.date_format for s in styles}) >= 6
    assert any(s.header("reference") is None for s in PAYMENT_STYLES.values())
    assert any("%H" in s.date_format for s in PAYMENT_STYLES.values())
    assert any(not (h.isascii()) for s in styles for h in (c.header for c in s.columns))


def test_a_style_must_name_each_required_field_once() -> None:
    columns = tuple(
        Column(k, k) for k in ("record_id", "counterparty", "date", "currency", "amount")
    )

    SourceStyle("invoices", columns, "%Y-%m-%d")
    with pytest.raises(ValueError, match="lacks required"):
        SourceStyle("invoices", columns[:-1], "%Y-%m-%d")
    with pytest.raises(ValueError, match="repeats a header"):
        SourceStyle("invoices", (*columns[:-1], Column("amount", "date")), "%Y-%m-%d")
    with pytest.raises(ValueError, match="repeats a column key"):
        SourceStyle("invoices", (*columns, Column("amount", "other")), "%Y-%m-%d")
    with pytest.raises(ValueError, match="unknown columns"):
        SourceStyle("invoices", (*columns, Column("fee_amount", "fee")), "%Y-%m-%d")
    with pytest.raises(ValueError, match="unknown source"):
        SourceStyle("refunds", columns, "%Y-%m-%d")


def test_unknown_style_names_are_refused() -> None:
    with pytest.raises(ValueError, match="invoice style"):
        ExportStyles(invoices="nope")
    with pytest.raises(ValueError, match="payment style"):
        ExportStyles(payments="nope")


def test_the_truth_file_names_the_mapping_and_the_lookalike_columns(tmp_path: Path) -> None:
    write_dataset(dataset_for(0), tmp_path, ExportStyles("erp_english", "processor"))

    truth = json.loads((tmp_path / "mapping_truth.json").read_text(encoding="utf-8"))

    assert ColumnMapping.model_validate(truth["invoices"]["mapping"]).amount == "Gross Amount"
    assert {d["key"]: d["header"] for d in truth["invoices"]["distractors"]} == {
        "due_date": "Due Date",
        "net_amount": "Net Amount",
        "tax_amount": "VAT",
        "status": "Status",
    }
    assert truth["payments"]["style"] == "processor"
    assert truth["payments"]["mapping"]["decimal_separator"] == "."
    assert truth["payments"]["distractors"] == [{"key": "fee_amount", "header": "fee"}]


def test_swapping_day_and_month() -> None:
    assert swapped_day_month("%d/%m/%Y") == "%m/%d/%Y"
    assert swapped_day_month("%m/%d/%Y") == "%d/%m/%Y"
    assert swapped_day_month("%d.%m.%Y") == "%m.%d.%Y"
    assert swapped_day_month("%Y%m%d") == "%Y%d%m"
    assert swapped_day_month("%Y-%m") is None


def test_a_file_is_ambiguous_only_when_the_values_cannot_say_which_order_is_meant(
    tmp_path: Path,
) -> None:
    dataset = dataset_for(1)
    early = dataclasses.replace(
        dataset, invoices=tuple(i for i in dataset.invoices if i.issue_date.day <= 12)
    )
    late = dataclasses.replace(
        dataset, invoices=tuple(i for i in dataset.invoices if i.issue_date.day >= 13)
    )
    assert early.invoices
    assert late.invoices

    for name, data, expected in (("spanish", early, True), ("spanish", late, False)):
        directory = tmp_path / f"{name}-{expected}"
        write_dataset(data, directory, ExportStyles(invoices=name))
        truth = json.loads((directory / "mapping_truth.json").read_text(encoding="utf-8"))
        assert truth["invoices"]["date_format_ambiguous"] is expected

        # check the flag against what actually happens when the file is read the other way
        style = INVOICE_STYLES[name]
        swapped = swapped_day_month(style.date_format)
        assert swapped is not None
        cells = read_columns(directory / "invoices.csv")[style.header("date") or ""]
        parses = []
        for cell in cells:
            try:
                datetime.strptime(cell, swapped)  # noqa: DTZ007
                parses.append(True)
            except ValueError:
                parses.append(False)
        assert all(parses) is expected


def test_a_format_without_day_and_month_is_never_ambiguous() -> None:
    dataset = dataset_for(0)

    assert not date_format_ambiguous([i.issue_date for i in dataset.invoices], "%Y-%m")
    assert not date_format_ambiguous([], "%d/%m/%Y")

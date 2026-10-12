"""How different systems export the same invoices and payments.

An export style fixes the header text, column order, date format and number format of one
source file, and adds columns that look like the fields we need but are not (a due date next
to the issue date, net and tax next to the gross amount, a processor fee next to the amount
paid). Each style knows its true mapping, so a mapping proposed for a file can be scored.

Distractor values are derived from the record itself, so styles never change the generated
data or the random stream.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from recon.datagen.model import Dataset, Invoice, Payment
from recon.domain.money import format_amount
from recon.ingestion.normalize import ColumnMapping

INVOICE_FIELDS = ("record_id", "counterparty", "date", "currency", "amount")
PAYMENT_FIELDS = (*INVOICE_FIELDS, "reference")
INVOICE_DISTRACTORS = ("due_date", "net_amount", "tax_amount", "status", "doc_type", "memo")
PAYMENT_DISTRACTORS = ("value_date", "bank_ref", "fee_amount")


@dataclass(frozen=True)
class Column:
    key: str
    header: str


@dataclass(frozen=True)
class SourceStyle:
    source: str
    columns: tuple[Column, ...]
    date_format: str
    decimal_separator: str = "."
    thousands: bool = False
    currency_case: str = "upper"
    required: tuple[str, ...] = field(init=False, repr=False)
    distractors: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.source not in ("invoices", "payments"):
            raise ValueError(f"unknown source {self.source!r}")
        if self.currency_case not in ("upper", "lower"):
            raise ValueError("currency_case must be 'upper' or 'lower'")
        fields, distractors = (
            (INVOICE_FIELDS, INVOICE_DISTRACTORS)
            if self.source == "invoices"
            else (PAYMENT_FIELDS, PAYMENT_DISTRACTORS)
        )
        object.__setattr__(self, "required", fields)
        object.__setattr__(self, "distractors", distractors)

        keys = [c.key for c in self.columns]
        headers = [c.header for c in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.source} style repeats a column key")
        if len(set(headers)) != len(headers):
            raise ValueError(f"{self.source} style repeats a header")
        unknown = set(keys) - set(fields) - set(distractors)
        if unknown:
            raise ValueError(f"{self.source} style has unknown columns {sorted(unknown)}")
        optional = {"reference"} if self.source == "payments" else set()
        missing = set(fields) - set(keys) - optional
        if missing:
            raise ValueError(f"{self.source} style lacks required columns {sorted(missing)}")

    def header(self, key: str) -> str | None:
        return next((c.header for c in self.columns if c.key == key), None)

    def mapping(self) -> ColumnMapping:
        """The mapping that reads this style's files correctly."""
        return ColumnMapping(
            record_id=self._required("record_id"),
            counterparty=self._required("counterparty"),
            date=self._required("date"),
            amount=self._required("amount"),
            currency=self._required("currency"),
            reference=self.header("reference"),
            date_format=self.date_format,
            decimal_separator=self.decimal_separator,
        )

    def distractor_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.key in self.distractors)

    def _required(self, key: str) -> str:
        header = self.header(key)
        if header is None:
            raise ValueError(f"{self.source} style has no {key} column")
        return header


def _style(
    source: str,
    headers: dict[str, str],
    date_format: str,
    *,
    decimal_separator: str = ".",
    thousands: bool = False,
    currency_case: str = "upper",
) -> SourceStyle:
    columns = tuple(Column(key, header) for key, header in headers.items())
    return SourceStyle(source, columns, date_format, decimal_separator, thousands, currency_case)


INVOICE_STYLES: dict[str, SourceStyle] = {
    "default": _style(
        "invoices",
        {
            "record_id": "invoice_id",
            "counterparty": "customer_id",
            "date": "issue_date",
            "currency": "currency",
            "amount": "total",
        },
        "%Y-%m-%d",
    ),
    "erp_english": _style(
        "invoices",
        {
            "record_id": "Invoice No",
            "counterparty": "Customer Account",
            "date": "Invoice Date",
            "due_date": "Due Date",
            "currency": "Currency",
            "net_amount": "Net Amount",
            "tax_amount": "VAT",
            "amount": "Gross Amount",
            "status": "Status",
        },
        "%d-%b-%Y",
        thousands=True,
    ),
    "terse": _style(
        "invoices",
        {
            "record_id": "inv",
            "counterparty": "cust",
            "date": "dt",
            "due_date": "due",
            "currency": "ccy",
            "amount": "amt",
        },
        "%Y%m%d",
    ),
    "german": _style(
        "invoices",
        {
            "record_id": "Rechnungsnr.",
            "counterparty": "Kunde",
            "date": "Rechnungsdatum",
            "due_date": "Fälligkeitsdatum",
            "currency": "Währung",
            "net_amount": "Nettobetrag",
            "tax_amount": "MwSt",
            "amount": "Bruttobetrag",
        },
        "%d.%m.%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "spanish": _style(
        "invoices",
        {
            "record_id": "N.º de factura",
            "counterparty": "Cliente",
            "date": "Fecha de emisión",
            "due_date": "Fecha de vencimiento",
            "currency": "Moneda",
            "net_amount": "Base imponible",
            "tax_amount": "IVA",
            "amount": "Importe total",
        },
        "%d/%m/%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "sap": _style(
        "invoices",
        {
            "doc_type": "BLART",
            "record_id": "BELNR",
            "counterparty": "KUNNR",
            "date": "BUDAT",
            "due_date": "FAEDT",
            "currency": "WAERS",
            "amount": "WRBTR",
        },
        "%Y%m%d",
    ),
    # Stress styles: vocabularies the baseline mapper's word lists do not cover, and one whose
    # headers say nothing at all. They exist to show where header words stop working.
    "italian": _style(
        "invoices",
        {
            "record_id": "Numero fattura",
            "counterparty": "Ragione sociale",
            "date": "Data documento",
            "due_date": "Scadenza",
            "currency": "Valuta",
            "net_amount": "Imponibile",
            "tax_amount": "Imposta",
            "amount": "Importo totale",
        },
        "%d/%m/%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "dutch": _style(
        "invoices",
        {
            "record_id": "Factuurnummer",
            "counterparty": "Debiteur",
            "date": "Factuurdatum",
            "due_date": "Vervaldatum",
            "currency": "Valuta",
            "net_amount": "Bedrag excl. BTW",
            "tax_amount": "BTW",
            "amount": "Totaalbedrag",
        },
        "%d-%m-%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "generic_columns": _style(
        "invoices",
        {
            "record_id": "Column A",
            "counterparty": "Column B",
            "due_date": "Column C",
            "date": "Column D",
            "currency": "Column E",
            "net_amount": "Column F",
            "tax_amount": "Column G",
            "amount": "Column H",
        },
        "%m/%d/%Y",
    ),
    # Held out: modelled on publicly described layouts of accounting and payment products,
    # added before the baseline mapper was written and never tuned against. The headers
    # come from secondhand descriptions, not from an official export.
    "xero_like": _style(
        "invoices",
        {
            "counterparty": "*ContactName",
            "record_id": "*InvoiceNumber",
            "memo": "Reference",
            "date": "*InvoiceDate",
            "due_date": "*DueDate",
            "currency": "Currency",
            "amount": "Total",
        },
        "%d/%m/%Y",
    ),
}

PAYMENT_STYLES: dict[str, SourceStyle] = {
    "default": _style(
        "payments",
        {
            "record_id": "payment_id",
            "reference": "reference",
            "counterparty": "customer",
            "date": "received_on",
            "currency": "ccy",
            "amount": "amount_paid",
        },
        "%m/%d/%Y",
        thousands=True,
    ),
    "bank_statement": _style(
        "payments",
        {
            "record_id": "Transaction ID",
            "date": "Booking Date",
            "value_date": "Value Date",
            "counterparty": "Payer",
            "reference": "Remittance Info",
            "currency": "Ccy",
            "amount": "Credit Amount",
            "bank_ref": "Bank Ref",
        },
        "%d/%m/%Y",
        thousands=True,
    ),
    "german_bank": _style(
        "payments",
        {
            "record_id": "Buchungs-ID",
            "date": "Buchungstag",
            "value_date": "Wertstellung",
            "counterparty": "Auftraggeber",
            "reference": "Verwendungszweck",
            "currency": "Währung",
            "amount": "Betrag",
        },
        "%d.%m.%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "processor": _style(
        "payments",
        {
            "record_id": "charge_id",
            "date": "created",
            "counterparty": "customer",
            "reference": "invoice_ref",
            "currency": "currency",
            "amount": "amount",
            "fee_amount": "fee",
        },
        "%Y-%m-%dT%H:%M:%SZ",
    ),
    "terse_no_reference": _style(
        "payments",
        {
            "record_id": "id",
            "counterparty": "payer",
            "date": "date",
            "currency": "cur",
            "amount": "amt",
        },
        "%Y-%m-%d",
    ),
    "french_bank": _style(
        "payments",
        {
            "record_id": "Référence opération",
            "date": "Date d'opération",
            "value_date": "Date de valeur",
            "counterparty": "Donneur d'ordre",
            "reference": "Libellé",
            "currency": "Devise",
            "amount": "Montant",
        },
        "%d/%m/%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "dutch_bank": _style(
        "payments",
        {
            "record_id": "Transactienummer",
            "date": "Boekdatum",
            "value_date": "Valutadatum",
            "counterparty": "Naam tegenpartij",
            "reference": "Omschrijving",
            "currency": "Valuta",
            "amount": "Bedrag",
            "bank_ref": "Banknummer",
        },
        "%d-%m-%Y",
        decimal_separator=",",
        thousands=True,
    ),
    "generic_columns": _style(
        "payments",
        {
            "record_id": "Column A",
            "value_date": "Column B",
            "date": "Column C",
            "counterparty": "Column D",
            "reference": "Column E",
            "currency": "Column F",
            "fee_amount": "Column G",
            "amount": "Column H",
        },
        "%m/%d/%Y",
    ),
    "stripe_like": _style(
        "payments",
        {
            "record_id": "id",
            "date": "Created (UTC)",
            "amount": "Amount",
            "currency": "Currency",
            "fee_amount": "Fee",
            "counterparty": "Customer ID",
            "reference": "Description",
        },
        "%Y-%m-%d %H:%M:%S",
        currency_case="lower",
    ),
}

# Styles that were added to measure a mapper on layouts it was not built around.
HELD_OUT_STYLES = {"invoices": ("xero_like",), "payments": ("stripe_like",)}

# Styles built to be hard for a mapper that relies on header words.
STRESS_STYLES = {
    "invoices": ("italian", "dutch", "generic_columns"),
    "payments": ("french_bank", "dutch_bank", "generic_columns"),
}


def style_group(invoices: str, payments: str) -> str:
    """Which evaluation group a pair of styles belongs to: development, held_out or stress."""
    if invoices in STRESS_STYLES["invoices"] or payments in STRESS_STYLES["payments"]:
        return "stress"
    if invoices in HELD_OUT_STYLES["invoices"] or payments in HELD_OUT_STYLES["payments"]:
        return "held_out"
    return "development"


@dataclass(frozen=True)
class ExportStyles:
    invoices: str = "default"
    payments: str = "default"

    def __post_init__(self) -> None:
        if self.invoices not in INVOICE_STYLES:
            raise ValueError(f"unknown invoice style {self.invoices!r}")
        if self.payments not in PAYMENT_STYLES:
            raise ValueError(f"unknown payment style {self.payments!r}")

    @property
    def invoice_style(self) -> SourceStyle:
        return INVOICE_STYLES[self.invoices]

    @property
    def payment_style(self) -> SourceStyle:
        return PAYMENT_STYLES[self.payments]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _date(value: date, style: SourceStyle, salt: str) -> str:
    if "%H" not in style.date_format:
        return value.strftime(style.date_format)
    digits = _digest(salt)
    moment = datetime(  # noqa: DTZ001 - a wall-clock time in a file has no zone to attach
        value.year,
        value.month,
        value.day,
        int(digits[0:2], 16) % 24,
        int(digits[2:4], 16) % 60,
        int(digits[4:6], 16) % 60,
    )
    return moment.strftime(style.date_format)


def _currency(code: str, style: SourceStyle) -> str:
    return code.lower() if style.currency_case == "lower" else code


def _money(minor: int, currency: str, style: SourceStyle) -> str:
    return format_amount(
        minor, currency, thousands=style.thousands, decimal_separator=style.decimal_separator
    )


def _invoice_cell(key: str, invoice: Invoice, style: SourceStyle) -> str:
    tax = invoice.total_minor // 6
    cells: dict[str, Callable[[], str]] = {
        "record_id": lambda: invoice.invoice_id,
        "counterparty": lambda: invoice.customer_id,
        "date": lambda: _date(invoice.issue_date, style, invoice.invoice_id),
        "currency": lambda: _currency(invoice.currency, style),
        "amount": lambda: _money(invoice.total_minor, invoice.currency, style),
        "due_date": lambda: _date(
            invoice.issue_date + timedelta(days=30), style, invoice.invoice_id
        ),
        "tax_amount": lambda: _money(tax, invoice.currency, style),
        "net_amount": lambda: _money(invoice.total_minor - tax, invoice.currency, style),
        "status": lambda: "OPEN",
        "memo": lambda: "Monthly services",
        "doc_type": lambda: "RV",
    }
    return cells[key]()


def _payment_cell(key: str, payment: Payment, style: SourceStyle) -> str:
    cells: dict[str, Callable[[], str]] = {
        "record_id": lambda: payment.payment_id,
        "counterparty": lambda: payment.customer_id,
        "date": lambda: _date(payment.received_on, style, payment.payment_id),
        "currency": lambda: _currency(payment.currency, style),
        "amount": lambda: _money(payment.amount_minor, payment.currency, style),
        "reference": lambda: payment.reference,
        "value_date": lambda: _date(
            payment.received_on + timedelta(days=1), style, payment.payment_id
        ),
        "bank_ref": lambda: "BK" + _digest(payment.payment_id)[:8].upper(),
        "fee_amount": lambda: _money(payment.amount_minor * 29 // 1000, payment.currency, style),
    }
    return cells[key]()


def invoice_rows(dataset: Dataset, style: SourceStyle) -> tuple[list[str], list[list[str]]]:
    header = [c.header for c in style.columns]
    rows = [[_invoice_cell(c.key, i, style) for c in style.columns] for i in dataset.invoices]
    return header, rows


def payment_rows(dataset: Dataset, style: SourceStyle) -> tuple[list[str], list[list[str]]]:
    header = [c.header for c in style.columns]
    rows = [[_payment_cell(c.key, p, style) for c in style.columns] for p in dataset.payments]
    return header, rows


def swapped_day_month(date_format: str) -> str | None:
    """The same format with day and month exchanged, or None when it has no such pair."""
    if "%d" not in date_format or "%m" not in date_format:
        return None
    return date_format.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m")


def date_format_ambiguous(dates: list[date], date_format: str) -> bool:
    """Whether the file's dates read as valid, different dates under the swapped format.

    That is the case when every day is 12 or lower and not every day equals its month, so
    nothing in the values says which of the two orders was meant.
    """
    return (
        swapped_day_month(date_format) is not None
        and bool(dates)
        and all(d.day <= 12 for d in dates)
        and any(d.day != d.month for d in dates)
    )


def export_truth(dataset: Dataset, styles: ExportStyles) -> dict[str, Any]:
    """What the right answer is for each file: the mapping, the lookalike columns to avoid,
    and whether the date format can be told from the values at all."""
    result: dict[str, Any] = {}
    sources = (
        (
            "invoices",
            styles.invoices,
            styles.invoice_style,
            [i.issue_date for i in dataset.invoices],
        ),
        (
            "payments",
            styles.payments,
            styles.payment_style,
            [p.received_on for p in dataset.payments],
        ),
    )
    for source, name, style, dates in sources:
        result[source] = {
            "style": name,
            "mapping": style.mapping().model_dump(mode="json"),
            "distractors": [{"key": c.key, "header": c.header} for c in style.distractor_columns()],
            "date_format_ambiguous": date_format_ambiguous(dates, style.date_format),
        }
    return result

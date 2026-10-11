"""Turn the rows of a source file into typed records, given an explicit column mapping."""

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from recon.domain.money import Money, MoneyError, parse_amount


class MappingError(ValueError):
    """The file cannot be read with the given mapping, for example a column is missing."""


class NormalizationError(ValueError):
    """A single value could not be normalized."""


class ColumnMapping(BaseModel, frozen=True):
    """How one source file's columns map to the canonical fields.

    The date format and decimal separator are stated, never inferred: a value like
    ``01/02/2026`` is a different day under ``%d/%m/%Y`` and ``%m/%d/%Y``.
    """

    record_id: str
    counterparty: str
    date: str
    amount: str
    currency: str
    reference: str | None = None
    date_format: str
    decimal_separator: Literal[".", ","] = "."

    def required_columns(self) -> tuple[str, ...]:
        columns = [self.record_id, self.counterparty, self.date, self.amount, self.currency]
        if self.reference is not None:
            columns.append(self.reference)
        return tuple(columns)


@dataclass(frozen=True)
class NormalizedRecord:
    source_row: int
    record_id: str
    reference: str | None
    counterparty: str
    occurred_on: date
    amount: Money

    @property
    def content_hash(self) -> str:
        """Fingerprint of everything but the row position, to tell whether a record changed."""
        content = [
            self.record_id,
            self.reference,
            self.counterparty,
            self.occurred_on.isoformat(),
            self.amount.minor,
            self.amount.currency,
        ]
        return hashlib.sha256(json.dumps(content, separators=(",", ":")).encode()).hexdigest()

    @property
    def key(self) -> str:
        """The record's id in matching form, comparable with another record's ``reference``."""
        return normalize_reference(self.record_id)


@dataclass(frozen=True)
class RejectedRow:
    source_row: int
    reason: str
    raw: Mapping[str, str]


@dataclass(frozen=True)
class NormalizationResult:
    records: tuple[NormalizedRecord, ...]
    rejected: tuple[RejectedRow, ...]


_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")


def normalize_reference(text: str) -> str:
    """Canonical form of an identifier: upper case, letters and digits only.

    ``INV-000123``, ``inv000123`` and `` INV 000123 `` all become ``INV000123``.
    Two ids that differ only in punctuation or case are treated as the same.
    """
    return _NON_ALPHANUMERIC.sub("", text.upper())


def parse_date(text: str, date_format: str) -> date:
    try:
        return datetime.strptime(text.strip(), date_format).date()  # noqa: DTZ007
    except ValueError as error:
        raise NormalizationError(f"{text!r} does not match date format {date_format!r}") from error


def normalize_csv(path: Path, mapping: ColumnMapping) -> NormalizationResult:
    """Read a CSV file. Rows that cannot be normalized are returned, not dropped silently."""
    return normalize_csv_bytes(path.read_bytes(), path.name, mapping)


def normalize_csv_bytes(data: bytes, name: str, mapping: ColumnMapping) -> NormalizationResult:
    """Normalize CSV content that is already in memory, so what is hashed is what is parsed."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MappingError(f"{name} is not valid UTF-8") from error

    records: list[NormalizedRecord] = []
    rejected: list[RejectedRow] = []
    seen_ids: set[str] = set()

    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in mapping.required_columns() if c not in (reader.fieldnames or [])]
    if missing:
        raise MappingError(f"columns not found in {name}: {', '.join(missing)}")

    for row_number, row in enumerate(reader, start=1):
        cells = {column: (value or "").strip() for column, value in row.items() if column}
        try:
            record = _normalize_row(row_number, cells, mapping)
            if record.record_id in seen_ids:
                raise NormalizationError(f"duplicate record id {record.record_id!r}")
        except (NormalizationError, MoneyError) as error:
            rejected.append(RejectedRow(row_number, str(error), cells))
            continue
        seen_ids.add(record.record_id)
        records.append(record)

    return NormalizationResult(tuple(records), tuple(rejected))


def _normalize_row(
    row_number: int, cells: Mapping[str, str], mapping: ColumnMapping
) -> NormalizedRecord:
    record_id = cells[mapping.record_id]
    if not record_id:
        raise NormalizationError("record id is empty")

    counterparty = cells[mapping.counterparty]
    if not counterparty:
        raise NormalizationError("counterparty is empty")

    currency = cells[mapping.currency].upper()
    minor = parse_amount(
        cells[mapping.amount], currency, decimal_separator=mapping.decimal_separator
    )

    reference = cells[mapping.reference] if mapping.reference is not None else ""
    return NormalizedRecord(
        source_row=row_number,
        record_id=record_id,
        reference=normalize_reference(reference) or None,
        counterparty=counterparty,
        occurred_on=parse_date(cells[mapping.date], mapping.date_format),
        amount=Money(minor, currency),
    )

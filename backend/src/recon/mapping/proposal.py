"""What a mapper says about a pair of source files.

A proposal never silently guesses: each field either has a confident column or says what
the alternatives are, so the user can be asked.
"""

from pydantic import BaseModel, Field

from recon.ingestion.normalize import ColumnMapping

FIELDS = ("record_id", "counterparty", "date", "amount", "currency", "reference", "text")
# Fields a file may have no column for.
OPTIONAL_FIELDS = ("reference", "text")
REQUIRED_FIELDS = ("record_id", "counterparty", "date", "amount", "currency")
SETTING_NAMES = ("date_format", "decimal_separator")


def asked_fields(source: str) -> tuple[str, ...]:
    """The fields a file of this kind is asked about: only payments can carry a reference."""
    return FIELDS if source == "payments" else tuple(f for f in FIELDS if f != "reference")


class FieldChoice(BaseModel, frozen=True):
    """The column chosen for one field, or None, with the other columns that were possible."""

    column: str | None = None
    confident: bool = False
    alternatives: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class ValueChoice(BaseModel, frozen=True):
    """A setting that is not a column: a date format or a decimal separator."""

    value: str | None = None
    confident: bool = False
    alternatives: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def field_choice(
    column: str | None = None,
    confident: bool = False,
    alternatives: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> FieldChoice:
    return FieldChoice(
        column=column, confident=confident, alternatives=alternatives, reasons=reasons
    )


def value_choice(
    value: str | None = None,
    confident: bool = False,
    alternatives: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> ValueChoice:
    return ValueChoice(value=value, confident=confident, alternatives=alternatives, reasons=reasons)


class FileProposal(BaseModel, frozen=True):
    fields: dict[str, FieldChoice] = Field(default_factory=dict)
    date_format: ValueChoice = ValueChoice()
    decimal_separator: ValueChoice = ValueChoice()

    def choice(self, field: str) -> FieldChoice:
        return self.fields.get(field, FieldChoice())

    @property
    def confident(self) -> bool:
        """Every required field has a column, and nothing is left to ask."""
        return (
            all(self.choice(f).column is not None for f in REQUIRED_FIELDS)
            and all(c.confident for c in self.fields.values())
            and self.date_format.confident
            and self.decimal_separator.confident
        )

    def open_items(self, source: str) -> list[str]:
        """What is still to be answered for this file: fields and settings that are not sure."""
        fields = [f for f in asked_fields(source) if not self.choice(f).confident]
        settings = [n for n in SETTING_NAMES if not getattr(self, n).confident]
        return fields + settings

    def to_mapping(self) -> ColumnMapping | None:
        """The mapping this proposal describes, or None when a required part is missing."""
        columns = {f: self.choice(f).column for f in FIELDS}
        if (
            any(columns[f] is None for f in REQUIRED_FIELDS)
            or self.date_format.value is None
            or self.decimal_separator.value not in (".", ",")
        ):
            return None
        return ColumnMapping(
            record_id=columns["record_id"] or "",
            counterparty=columns["counterparty"] or "",
            date=columns["date"] or "",
            amount=columns["amount"] or "",
            currency=columns["currency"] or "",
            reference=columns["reference"],
            text=columns["text"],
            date_format=self.date_format.value,
            decimal_separator=self.decimal_separator.value,
        )


class MappingProposal(BaseModel, frozen=True):
    invoices: FileProposal
    payments: FileProposal

    def open_items(self) -> list[tuple[str, str]]:
        """Every open question as (file, field or setting): invoices first, then payments."""
        return [
            (source, item)
            for source in ("invoices", "payments")
            for item in getattr(self, source).open_items(source)
        ]

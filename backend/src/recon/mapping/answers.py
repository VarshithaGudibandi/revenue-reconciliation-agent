"""What a person has told us about a mapping, and how it constrains the proposal.

An answer pins one field of one file to a column (or to "there is no such column"), or pins a
setting such as the date format. The mapper treats a pin as a fact: the evidence that depends on
it is worked out again from the pinned column, so one answer can settle other questions.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from recon.mapping.proposal import (
    FieldChoice,
    FileProposal,
    MappingProposal,
    ValueChoice,
    field_choice,
    value_choice,
)

SOURCES = ("invoices", "payments")
SETTINGS = ("date_format", "decimal_separator")

Pins = Mapping[tuple[str, str], str | None]
NO_PINS: Pins = {}


@dataclass(frozen=True)
class Answer:
    """``value`` is a column header, a date format or a separator; None means the file has no
    column for the field."""

    source: str
    field: str
    value: str | None

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown file {self.source!r}")


def pins_from(answers: Iterable[Answer]) -> dict[tuple[str, str], str | None]:
    """The pins the answers amount to. A later answer to the same question replaces an earlier."""
    return {(a.source, a.field): a.value for a in answers}


def pinned(pins: Pins, source: str, field: str) -> bool:
    return (source, field) in pins


def _reason(value: str | None) -> str:
    return "Your answer" if value is not None else "You said there is no such column"


def pin_field(choice: FieldChoice, pins: Pins, source: str, field: str) -> FieldChoice:
    """The choice for a field, replaced by the person's answer if there is one."""
    if (source, field) not in pins:
        return choice
    column = pins[(source, field)]
    return field_choice(column, True, reasons=(_reason(column),))


def pin_value(choice: ValueChoice, pins: Pins, source: str, setting: str) -> ValueChoice:
    """A setting, replaced by the person's answer if there is one."""
    if (source, setting) not in pins:
        return choice
    return value_choice(pins[(source, setting)], True, reasons=("Your answer",))


def apply_pins(proposal: MappingProposal, pins: Pins) -> MappingProposal:
    """Override a finished proposal with the answers, for one made without them (a model's)."""
    return MappingProposal(
        invoices=_apply(proposal.invoices, pins, "invoices"),
        payments=_apply(proposal.payments, pins, "payments"),
    )


def _apply(proposal: FileProposal, pins: Pins, source: str) -> FileProposal:
    fields = {name: pin_field(c, pins, source, name) for name, c in proposal.fields.items()}
    for pin_source, name in pins:
        if pin_source == source and name not in SETTINGS and name not in fields:
            fields[name] = pin_field(FieldChoice(), pins, source, name)
    return FileProposal(
        fields=fields,
        date_format=pin_value(proposal.date_format, pins, source, "date_format"),
        decimal_separator=pin_value(proposal.decimal_separator, pins, source, "decimal_separator"),
    )

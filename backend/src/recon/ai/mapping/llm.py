"""A mapper that asks a language model about one file, then checks the answer against the data.

This module holds what the model is asked (the prompt, and an answer schema that lists the
file's own headers as the only values a column field can take, so the model cannot name a
column that does not exist) and the mapper that asks it.

The model proposes; the engine decides how far to trust it. An answer is confident only if
the model says it is sure AND the column's values fit the field. A date format that the
values leave open is never confident, whatever the model says: the engine does not guess
the order of day and month.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from recon.ai.llm.client import JsonModel, LlmError
from recon.ai.mapping.profile import DATE_FORMATS, ColumnProfile, FileProfile
from recon.ai.mapping.proposal import (
    FIELDS,
    OPTIONAL_FIELDS,
    FieldChoice,
    FileProposal,
    ValueChoice,
    field_choice,
    value_choice,
)

PROMPT_VERSION = "mapping-3"
NO_COLUMN = "(none)"
UNKNOWN = "unknown"
Confidence = Literal["high", "medium", "low"]

SYSTEM_PROMPT = """\
You map the columns of one source file from a finance system to the fields a revenue \
reconciliation needs. You are told whether it is an invoice file or a payment file, and you \
get a description of every column: its header, the shapes its values take (digits are 9, \
letters A or a, repeated characters are marked +), how many of its values parse as dates or \
amounts, how unique it is, and a few examples that are partly masked. You never see the \
real data.

Fields:
- record_id: the identifier that is unique per row of this file (invoice number, payment or \
transaction id). Not a customer id, and not a pointer to a document in another file.
- counterparty: the customer or payer identifier that repeats across rows and would appear \
in the other file too.
- date: for invoices, the date the invoice was issued, not the due date. For payments, the \
date the payment was received or booked, not the value date unless it is the only date.
- amount: for invoices, the total payable including tax (the gross amount), not the net \
amount, the tax or an open balance. For payments, the amount paid, not the fee.
- currency: the column of currency codes.
- reference (payment files only): the text or id that says which invoice the payment is for. \
Use "(none)" if there is no such column.
- text (optional): the column of free text the source holds about each record, such as what \
an invoice is for or what the payer wrote. Its values are mostly several words. Use "(none)" \
if there is no such column.
- date_format: one of the listed formats, and only a format the column's date_formats list \
says fits. If more than one fits, for example day-month and month-day, answer "unknown" and \
add a question: never guess the order.
- decimal_separator: the character before the fractional digits of the amount column; take \
it from that column's amount_decimal_separators.

For each field give your confidence. "high" means the description makes the choice \
unambiguous. "medium" means you lean one way but another column is plausible, and then list \
the others in alternatives. "low" means you are guessing. A confident wrong answer is the \
worst outcome and a question to the user is cheap, so use medium or low whenever the headers \
and values do not settle it. Use each column for at most one field. Keep every reason to one \
short sentence.\
"""


class LlmField(BaseModel, frozen=True):
    column: str
    confidence: Confidence
    alternatives: tuple[str, ...]
    reason: str


class LlmSetting(BaseModel, frozen=True):
    value: str
    confidence: Confidence
    reason: str


class LlmFileMapping(BaseModel, frozen=True):
    record_id: LlmField
    counterparty: LlmField
    date: LlmField
    amount: LlmField
    currency: LlmField
    reference: LlmField
    text: LlmField
    date_format: LlmSetting
    decimal_separator: LlmSetting
    questions: tuple[str, ...]


def answer_schema(headers: tuple[str, ...]) -> dict[str, Any]:
    """The JSON schema of the answer for a file with these headers."""
    columns = [*headers, NO_COLUMN]
    field = {
        "type": "object",
        "properties": {
            "column": {"type": "string", "enum": columns},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "alternatives": {"type": "array", "items": {"type": "string", "enum": list(headers)}},
            "reason": {"type": "string"},
        },
        "required": ["column", "confidence", "alternatives", "reason"],
        "additionalProperties": False,
    }

    def setting(values: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string", "enum": values},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
            },
            "required": ["value", "confidence", "reason"],
            "additionalProperties": False,
        }

    properties: dict[str, Any] = {name: field for name in FIELDS}
    properties["date_format"] = setting([*DATE_FORMATS, UNKNOWN])
    properties["decimal_separator"] = setting([".", ",", UNKNOWN])
    properties["questions"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": properties,
        "required": [*FIELDS, "date_format", "decimal_separator", "questions"],
        "additionalProperties": False,
    }


def user_message(profile: FileProfile, role: str) -> str:
    """The request: the role of the file and its description, with masked examples."""
    return json.dumps(
        {"file_role": role, "description": profile.describe(samples=True)},
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )


def _record_id_problem(c: ColumnProfile) -> str | None:
    return None if c.unique_share >= 0.95 else "its values are not unique"


def _counterparty_problem(c: ColumnProfile) -> str | None:
    if c.date_like() or c.amount_like() or c.currency_like():
        return "its values are dates, amounts or currency codes"
    return None if c.distinct >= 2 else "it has a single value"


def _date_problem(c: ColumnProfile) -> str | None:
    return None if c.date_like() else "its values do not read as dates"


def _amount_problem(c: ColumnProfile) -> str | None:
    return None if c.amount_like() else "its values do not read as amounts with a fraction"


def _currency_problem(c: ColumnProfile) -> str | None:
    return None if c.currency_like() else "its values are not currency codes"


def _reference_problem(c: ColumnProfile) -> str | None:
    return "its values are dates or amounts" if c.date_like() or c.amount_like() else None


def _text_problem(c: ColumnProfile) -> str | None:
    return (
        "its values are dates, amounts or currency codes"
        if (c.date_like() or c.amount_like() or c.currency_like())
        else None
    )


CHECKS: dict[str, Callable[[ColumnProfile], str | None]] = {
    "record_id": _record_id_problem,
    "counterparty": _counterparty_problem,
    "date": _date_problem,
    "amount": _amount_problem,
    "currency": _currency_problem,
    "reference": _reference_problem,
    "text": _text_problem,
}


def _choice(
    profile: FileProfile, name: str, answer: LlmField, *, validate: bool, reused: set[str]
) -> FieldChoice:
    headers = set(profile.headers())
    column = None if answer.column == NO_COLUMN or answer.column not in headers else answer.column
    others = tuple(a for a in answer.alternatives if a in headers and a != column)
    reasons = (answer.reason,)
    sure = answer.confidence == "high"
    if not validate:
        return field_choice(column, sure, others, reasons)
    if column is None:
        # "No column" is only an answer for the reference; for the rest it is a gap.
        return field_choice(None, sure and name in OPTIONAL_FIELDS, others, reasons)
    problem = CHECKS[name](profile.column(column))
    if problem is not None:
        return field_choice(None, False, (column, *others), (*reasons, f"Data check: {problem}"))
    if column in reused:
        return field_choice(column, False, others, (*reasons, "The model used it for two fields"))
    return field_choice(column, sure, others, reasons)


def _date_format(
    profile: FileProfile, answer: LlmSetting, date: FieldChoice, *, validate: bool
) -> ValueChoice:
    value = None if answer.value == UNKNOWN else answer.value
    sure = answer.confidence == "high"
    reasons = (answer.reason,)
    if not validate:
        return value_choice(value, sure and value is not None, (), reasons)
    column = date.column
    if column is None:
        return value_choice(None, False, (), (*reasons, "No date column was settled"))
    fits = profile.column(column).date_formats()
    if len(fits) != 1:
        return value_choice(
            None, False, fits, (*reasons, "The values fit more than one date order or format")
        )
    if value != fits[0]:
        return value_choice(None, False, fits, (*reasons, "The values do not fit that format"))
    return value_choice(value, sure, (), reasons)


def _decimal_separator(
    profile: FileProfile, answer: LlmSetting, amount: FieldChoice, *, validate: bool
) -> ValueChoice:
    value = None if answer.value == UNKNOWN else answer.value
    sure = answer.confidence == "high"
    reasons = (answer.reason,)
    if not validate:
        return value_choice(value, sure and value is not None, (), reasons)
    if amount.column is None:
        return value_choice(None, False, (), (*reasons, "No amount column was settled"))
    fits = profile.column(amount.column).amount_separators()
    if value not in fits:
        return value_choice(None, False, fits, (*reasons, "The amounts do not read that way"))
    return value_choice(value, sure, (), reasons)


def to_proposal(
    profile: FileProfile, role: str, answer: LlmFileMapping, *, validate: bool = True
) -> FileProposal:
    """Turn the model's answer into a proposal, checking it against the data unless told not to."""
    names = FIELDS if role == "payments" else tuple(f for f in FIELDS if f != "reference")
    chosen: dict[str, FieldChoice] = {}
    reused: set[str] = set()
    taken: dict[str, str] = {}
    for name in names:
        raw = getattr(answer, name)
        if raw.column != NO_COLUMN:
            if raw.column in taken:
                reused.add(raw.column)
            taken[raw.column] = name
    for name in names:
        chosen[name] = _choice(
            profile, name, getattr(answer, name), validate=validate, reused=reused
        )
    return FileProposal(
        fields=chosen,
        date_format=_date_format(profile, answer.date_format, chosen["date"], validate=validate),
        decimal_separator=_decimal_separator(
            profile, answer.decimal_separator, chosen["amount"], validate=validate
        ),
    )


@dataclass
class LlmMapper:
    """Maps one file at a time with a language model."""

    model: JsonModel
    validate: bool = True
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def propose_file(self, profile: FileProfile, role: str) -> FileProposal:
        try:
            completion = self.model.complete(
                SYSTEM_PROMPT, user_message(profile, role), answer_schema(profile.headers())
            )
            answer = LlmFileMapping.model_validate_json(completion.text)
        except (LlmError, ValidationError) as error:
            self.errors.append(f"{profile.name}: {error}")
            del self.errors[:-50]  # a long-running server keeps only the latest
            return FileProposal()
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        return to_proposal(profile, role, answer, validate=self.validate)

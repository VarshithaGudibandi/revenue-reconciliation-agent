"""A mapper that asks a language model, then checks the answer against the data.

The model proposes; the engine decides how far to trust it. An answer is confident only if
the model says it is sure AND the column's values fit the field. A date format that the
values leave open is never confident, whatever the model says: the engine does not guess
the order of day and month.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from recon.mapping.llm_client import JsonModel, LlmError
from recon.mapping.llm_schema import (
    NO_COLUMN,
    SYSTEM_PROMPT,
    UNKNOWN,
    LlmField,
    LlmFileMapping,
    LlmSetting,
    answer_schema,
    user_message,
)
from recon.mapping.profile import ColumnProfile, FileProfile
from recon.mapping.proposal import (
    FIELDS,
    FieldChoice,
    FileProposal,
    ValueChoice,
    field_choice,
    value_choice,
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


CHECKS: dict[str, Callable[[ColumnProfile], str | None]] = {
    "record_id": _record_id_problem,
    "counterparty": _counterparty_problem,
    "date": _date_problem,
    "amount": _amount_problem,
    "currency": _currency_problem,
    "reference": _reference_problem,
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
        return field_choice(None, sure and name == "reference", others, reasons)
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

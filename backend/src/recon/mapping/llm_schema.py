"""What the model is asked, and the shape it must answer in.

The answer schema lists the file's own headers as the only values a column field can take, so
the model cannot name a column that does not exist.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel

from recon.mapping.profile import DATE_FORMATS, FileProfile
from recon.mapping.proposal import FIELDS

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

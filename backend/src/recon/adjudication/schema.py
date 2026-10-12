"""What the model is asked about a payment, and the shape it must answer in."""

import json
from typing import Any, Literal

from pydantic import BaseModel

from recon.adjudication.case import AdjudicationCase

PROMPT_VERSION = "adjudication-1"
NONE = "none"

SYSTEM_PROMPT = """\
A payment arrived that could settle any one of several invoices. They are all from the same \
customer, for the same amount, and issued days apart, so the amount does not say which one the \
payer meant. Decide which invoice the payer meant, using only the memo the payer wrote and the \
facts given about each candidate invoice: its number, what it is for, and how many days before \
the payment it was issued.

The memo can be in any language, with typos, abbreviations or other words for the same thing. \
It can name an invoice number, say what the payment is for, or say nothing useful, such as \
"Thank you" or an empty memo. Choose a candidate only if the memo points to it. Do not choose \
because of the order of the candidates or because one invoice is older, and do not guess: when \
the memo does not tell the candidates apart, answer "none".

Always quote the words of the memo that your choice rests on, exactly as written, in \
"evidence". Leave it empty when you answer "none". Say "high" for confidence only when the \
memo clearly identifies one invoice, "medium" when it leans one way, and "low" when you are \
guessing. A confident wrong answer is the worst outcome, and "none" costs only a question to \
a person. Keep the reason to one short sentence.\
"""


class LlmRuling(BaseModel, frozen=True):
    choice: str
    confidence: Literal["high", "medium", "low"]
    evidence: str
    reason: str


def answer_schema(labels: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": [*labels, NONE]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["choice", "confidence", "evidence", "reason"],
        "additionalProperties": False,
    }


def user_message(case: AdjudicationCase) -> str:
    return json.dumps(
        {
            "memo": case.memo,
            "candidates": [
                {
                    "label": c.label,
                    "invoice_number": c.number,
                    "for": c.description,
                    "issued_days_before_payment": c.days_before,
                }
                for c in case.candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )

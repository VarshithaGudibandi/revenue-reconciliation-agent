"""What the model is asked about a case, and the shape it must answer in."""

import json
from typing import Any

from pydantic import BaseModel

from recon.domain.money import Money
from recon.explanation.case import ExplainInput, amount_text

PROMPT_VERSION = "explanation-1"

SYSTEM_PROMPT = """\
You explain one exception found while reconciling invoices against payments, for a finance \
person who has to decide what to do about it. You are given the kind of exception, the \
difference the engine calculated, and the records involved. Each record has a reference such \
as R1, an amount, a date, and sometimes a line of free text that the source system held about \
it.

Kinds of exception:
- missing_payment: an invoice has no payment.
- unmatched_payment: a payment matches no invoice.
- amount_mismatch: the payments for an invoice do not add up to the invoice.
- partial_payment: an invoice is only partly paid.
- duplicate_payment: an invoice appears to be paid twice.
- misreferenced_payment: a payment probably pays an invoice, but its reference says otherwise; \
the pairing is a proposal and has not been applied.
- currency_mismatch: an invoice was paid in a different currency, and there is no exchange rate.
- ambiguous_payment: a payment could belong to any of several invoices that look alike.

Rules, which are checked by a program afterwards:
- Use only the facts given. Do not guess a cause that they do not show.
- Refer to a record only by its reference in square brackets, such as [R1]. Never write an id, \
a customer or a name.
- Write amounts exactly as given, with their currency. State no number that is not given: do \
not add, subtract or round anything yourself, and use the difference as calculated.
- You may quote what a record's text says when it matters.
- Say what to check next, concretely. Do not say that anything has been changed or fixed.

Answer with "summary" (one sentence), "why" (at most three sentences) and "next_steps" (one to \
three short items).\
"""


class LlmExplanation(BaseModel, frozen=True):
    summary: str
    why: str
    next_steps: tuple[str, ...]


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "why": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "why", "next_steps"],
    "additionalProperties": False,
}


def user_message(inp: ExplainInput) -> str:
    return json.dumps(
        {
            "kind": inp.kind,
            "difference": (
                None
                if inp.variance is None
                else amount_text(Money(abs(inp.variance.minor), inp.variance.currency))
            ),
            "difference_is": _direction(inp),
            "proposed_invoice": inp.proposed_ref,
            "records": [
                {
                    "ref": r.ref,
                    "role": r.role,
                    "date": r.occurred_on.isoformat() if r.occurred_on else None,
                    "amount": None if r.amount is None else amount_text(r.amount),
                    "text": r.text,
                }
                for r in inp.records
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )


def _direction(inp: ExplainInput) -> str | None:
    if inp.variance is None or inp.variance.minor == 0:
        return None
    return "unpaid" if inp.variance.minor > 0 else "overpaid"

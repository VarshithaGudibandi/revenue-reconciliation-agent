"""An adjudicator that asks a language model, and checks that its evidence is real.

This module holds what the model is asked (the prompt, the answer schema, the message) and the
adjudicator that asks it. The model has to quote the memo. A quote that is not in the memo means
the model made its reason up, and then its choice is not relied on, whatever confidence it
claims.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from recon.ai.adjudication.case import AdjudicationCase, Ruling
from recon.ai.llm.client import JsonModel, LlmError

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


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def to_ruling(case: AdjudicationCase, answer: LlmRuling, *, check: bool = True) -> Ruling:
    """Turn the model's answer into a ruling, relying on it only if its quote is in the memo."""
    if answer.choice == NONE or answer.choice not in case.labels():
        return Ruling(None, False, reasons=(answer.reason,))
    quote = _squash(answer.evidence)
    quoted = bool(quote) and quote in _squash(case.memo)
    if check and not quoted:
        return Ruling(
            answer.choice,
            False,
            evidence=answer.evidence or None,
            reasons=(answer.reason, "The quoted words are not in the memo"),
        )
    return Ruling(
        answer.choice,
        answer.confidence == "high",
        evidence=answer.evidence or None,
        reasons=(answer.reason,),
    )


@dataclass
class LlmAdjudicator:
    model: JsonModel
    check: bool = True
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def adjudicate(self, case: AdjudicationCase) -> Ruling:
        try:
            completion = self.model.complete(
                SYSTEM_PROMPT, user_message(case), answer_schema(case.labels())
            )
            answer = LlmRuling.model_validate_json(completion.text)
        except (LlmError, ValidationError) as error:
            self.errors.append(f"{case.payment_id}: {error}")
            del self.errors[:-50]
            return Ruling(None, False, reasons=("The model gave no usable answer",))
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        return to_ruling(case, answer, check=self.check)

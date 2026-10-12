"""An adjudicator that asks a language model, and checks that its evidence is real.

The model has to quote the memo. A quote that is not in the memo means the model made its
reason up, and then its choice is not relied on, whatever confidence it claims.
"""

import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from recon.adjudication.case import AdjudicationCase, Ruling
from recon.adjudication.schema import (
    NONE,
    SYSTEM_PROMPT,
    LlmRuling,
    answer_schema,
    user_message,
)
from recon.mapping.llm_client import JsonModel, LlmError


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

"""An explainer that asks a language model and keeps its text only if it checks out."""

from dataclasses import dataclass, field

from pydantic import ValidationError

from recon.explanation.case import ExplainInput
from recon.explanation.schema import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    LlmExplanation,
    user_message,
)
from recon.explanation.verify import render, verify
from recon.mapping.llm_client import JsonModel, LlmError


@dataclass(frozen=True)
class Explained:
    """What came back: the text if it was verified, and why not if it was not."""

    text: LlmExplanation | None
    problems: tuple[str, ...] = ()


@dataclass
class LlmExplainer:
    model: JsonModel
    verify_text: bool = True
    calls: int = 0
    rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def explain(self, inp: ExplainInput) -> Explained:
        try:
            completion = self.model.complete(SYSTEM_PROMPT, user_message(inp), ANSWER_SCHEMA)
            answer = LlmExplanation.model_validate_json(completion.text)
        except (LlmError, ValidationError) as error:
            self.errors.append(str(error))
            del self.errors[:-50]
            return Explained(None, ("the model gave no usable answer",))
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        problems = verify(answer, inp) if self.verify_text else []
        if problems:
            self.rejected += 1
            return Explained(None, tuple(problems))
        return Explained(render(answer, inp))

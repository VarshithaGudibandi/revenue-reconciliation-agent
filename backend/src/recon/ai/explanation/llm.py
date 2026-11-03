"""An explainer that asks a language model and keeps its text only if it checks out.

This module holds the explainer and the choice of explainer from configuration. What the model
is asked is in ``schema`` and the checks are in ``verify``.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError

from recon.ai.explanation.case import ExplainInput
from recon.ai.explanation.schema import ANSWER_SCHEMA, SYSTEM_PROMPT, LlmExplanation, user_message
from recon.ai.explanation.verify import render, verify
from recon.ai.llm.client import JsonModel, LlmError
from recon.ai.mapping.service import json_client
from recon.config import Settings


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


@dataclass(frozen=True)
class ExplainerChoice:
    """The model that writes explanations, if one is configured. Without one, only the rules'
    explanation is shown."""

    name: str
    model: str | None
    explainer: LlmExplainer | None

    @property
    def uses_model(self) -> bool:
        return self.explainer is not None


def build_explainer(settings: Settings) -> ExplainerChoice:
    client = json_client(settings)
    if client is None:
        return ExplainerChoice("rules", None, None)
    return ExplainerChoice("model, checked", settings.llm_model_name(), LlmExplainer(client))

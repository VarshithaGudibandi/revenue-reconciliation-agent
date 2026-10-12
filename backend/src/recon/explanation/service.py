"""Choosing who explains a case, from configuration."""

from dataclasses import dataclass

from recon.config import Settings
from recon.explanation.llm import LlmExplainer
from recon.mapping.service import json_client


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
    return ExplainerChoice("model, checked", settings.llm_model, LlmExplainer(client))

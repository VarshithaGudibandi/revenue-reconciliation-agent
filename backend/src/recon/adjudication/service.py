"""Choosing who suggests the invoice for an ambiguous payment, from configuration."""

from collections.abc import Callable
from dataclasses import dataclass

from recon.adjudication.case import AdjudicationCase, Ruling
from recon.adjudication.combine import combine_rulings
from recon.adjudication.llm import LlmAdjudicator
from recon.adjudication.rules import adjudicate_by_rules
from recon.config import Settings
from recon.mapping.service import json_client


@dataclass(frozen=True)
class AdjudicatorChoice:
    """An adjudicator and how to describe it to the user. ``uses_model`` is False for the rules."""

    name: str
    model: str | None
    adjudicate: Callable[[AdjudicationCase], Ruling]

    @property
    def uses_model(self) -> bool:
        return self.model is not None


def build_adjudicator(settings: Settings) -> AdjudicatorChoice:
    client = json_client(settings)
    if client is None:
        return AdjudicatorChoice("rules", None, adjudicate_by_rules)
    llm = LlmAdjudicator(client)

    def hybrid(case: AdjudicationCase) -> Ruling:
        return combine_rulings(adjudicate_by_rules(case), llm.adjudicate(case))

    return AdjudicatorChoice("rules and model", settings.llm_model, hybrid)

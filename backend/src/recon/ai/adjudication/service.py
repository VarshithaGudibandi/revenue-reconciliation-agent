"""Choosing who suggests the invoice for an ambiguous payment, and combining two answers.

A confident rule is not overruled by the model. A model that is sure where the rules abstain is
a suggestion that stays a question. Two confident answers that disagree are a conflict.
"""

from collections.abc import Callable
from dataclasses import dataclass

from recon.ai.adjudication.case import AdjudicationCase, Ruling
from recon.ai.adjudication.llm import LlmAdjudicator
from recon.ai.adjudication.rules import adjudicate_by_rules
from recon.ai.mapping.service import json_client
from recon.config import Settings


def combine_rulings(rules: Ruling, model: Ruling) -> Ruling:
    if rules.confident and model.confident:
        if rules.choice == model.choice:
            return Ruling(rules.choice, True, rules.evidence, (*rules.reasons, *model.reasons))
        both = tuple(c for c in (rules.choice, model.choice) if c is not None)
        return Ruling(
            None,
            False,
            reasons=("The rules and the model disagree", *rules.reasons, *model.reasons),
            alternatives=both,
        )
    if rules.confident:
        return rules
    if model.confident:
        return Ruling(
            model.choice,
            False,
            model.evidence,
            ("Suggested by the model; the rules could not settle it", *model.reasons),
            rules.alternatives,
        )
    choice = model.choice if model.choice is not None else rules.choice
    return Ruling(choice, False, model.evidence, (*rules.reasons, *model.reasons))


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

    return AdjudicatorChoice("rules and model", settings.llm_model_name(), hybrid)

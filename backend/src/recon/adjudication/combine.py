"""Combine what the rules and the model say about one payment.

A confident rule is not overruled by the model. A model that is sure where the rules abstain is
a suggestion that stays a question. Two confident answers that disagree are a conflict.
"""

from recon.adjudication.case import Ruling


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

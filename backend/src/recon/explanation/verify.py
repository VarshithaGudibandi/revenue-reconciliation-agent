"""Checking a model's explanation against the facts it was given.

The model writes about records by alias and may quote only the engine's figures. Anything else,
an alias that is not in the case, a number that is not one of the engine's, a record id written
out, is a reason to throw the text away and use the rule-written one.
"""

import re
from decimal import Decimal, InvalidOperation

from recon.explanation.case import ExplainInput
from recon.explanation.schema import LlmExplanation

_REF = re.compile(r"\[R(\d+)\]")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b[A-Z]{2,6}-?\d{3,}\b")
LIMITS = {"summary": 260, "why": 800, "step": 220}
MAX_STEPS = 3


def _fields(text: LlmExplanation) -> list[tuple[str, str]]:
    return [("summary", text.summary), ("why", text.why)] + [
        ("step", step) for step in text.next_steps
    ]


def verify(text: LlmExplanation, inp: ExplainInput) -> list[str]:
    """What is wrong with an explanation. An empty list means it may be shown."""
    problems: list[str] = []
    known = inp.ids()
    allowed = inp.allowed_figures()
    own_text = inp.texts()
    named: set[str] = set()

    if not text.summary.strip() or not text.why.strip():
        problems.append("the summary or the reason is empty")
    if not 1 <= len(text.next_steps) <= MAX_STEPS:
        problems.append(f"it gives {len(text.next_steps)} next steps, not one to {MAX_STEPS}")

    for where, value in _fields(text):
        if len(value) > LIMITS[where]:
            problems.append(f"the {where} is longer than {LIMITS[where]} characters")
        for match in _REF.finditer(value):
            ref = f"R{match.group(1)}"
            named.add(ref)
            if ref not in known:
                problems.append(f"it cites [{ref}], which is not in the case")
        bare = _REF.sub(" ", value)
        for token in _ID.findall(bare):
            if token not in own_text:
                problems.append(f"it writes the id {token} instead of an alias")
        for token in _NUMBER.findall(_ID.sub(" ", bare)):
            try:
                number = Decimal(token.replace(",", ""))
            except InvalidOperation:
                continue
            if number not in allowed:
                problems.append(f"it states {token}, which is not one of the engine's figures")

    if not named & set(known):
        problems.append("it does not refer to any record of the case")
    return list(dict.fromkeys(problems))


def render(text: LlmExplanation, inp: ExplainInput) -> LlmExplanation:
    """The explanation with each alias replaced by the real id of its record."""
    ids = inp.ids()

    def put_back(value: str) -> str:
        return _REF.sub(lambda m: ids.get(f"R{m.group(1)}", m.group(0)), value)

    return LlmExplanation(
        summary=put_back(text.summary),
        why=put_back(text.why),
        next_steps=tuple(put_back(s) for s in text.next_steps),
    )

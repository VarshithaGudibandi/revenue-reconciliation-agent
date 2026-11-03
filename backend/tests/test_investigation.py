"""The case investigation: its calculator, its aliases, its checks, and the approval graph."""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from recon.ai.investigation.agent import (
    Investigation,
    calculate,
    investigate,
    run_tool,
    user_message,
    verify,
)
from recon.ai.investigation.graph import build_graph
from recon.ai.investigation.ledger import Ledger
from recon.ai.llm.client import LlmError
from recon.ai.llm.tools import Message, Step, Tool, ToolCall
from recon.evaluation.investigation_eval import month, rules_cause


@pytest.fixture(scope="module")
def generated() -> tuple[Ledger, list[Any]]:
    return month(seed=1, size=200)


def fresh(generated: tuple[Ledger, list[Any]]) -> Ledger:
    """A ledger with no aliases handed out yet, over the same month."""
    ledger = generated[0]
    return Ledger(list(ledger.invoices.values()), list(ledger.payments.values()), ledger.cases)


def case_with(generated: tuple[Ledger, list[Any]], cause: str) -> Any:
    return next(item for item in generated[1] if item.cause == cause)


def test_the_calculator_does_arithmetic_and_nothing_else() -> None:
    assert calculate("(286.77 - 277.78) / 286.77 * 100") == pytest.approx(3.1349, abs=1e-4)
    assert calculate("-2 + 1,000.50") == 998.5
    for unsafe in ("__import__('os').system('ls')", "2 ** 1000", "x + 1", "len('a')", "1" * 300):
        with pytest.raises((ValueError, SyntaxError)):
            calculate(unsafe)


def test_the_model_sees_aliases_and_never_a_real_id(generated: Any) -> None:
    ledger = fresh(generated)
    item = case_with(generated, "paid_in_lump_sum")
    shown = [user_message(ledger, item.case)]
    for customer in ("C1", "C2"):
        shown.append(
            json.dumps(run_tool(ledger, ToolCall("customer_records", {"customer": customer})))
        )
    text = "\n".join(shown)
    assert "I1" in text and "C1" in text
    for record_id in [*ledger.invoices, *ledger.payments]:
        assert record_id not in text
    for customer_id in {r.counterparty for r in ledger.invoices.values()}:
        assert customer_id not in text
    # The same questions give the same aliases.
    again = fresh(generated)
    assert user_message(again, item.case) == shown[0]


class Script:
    """Calls the given tools in the first turn, then submits the finding it is given."""

    def __init__(self, first: list[ToolCall], finding: dict[str, Any] | None) -> None:
        self.first = first
        self.finding = finding
        self.offered: list[list[str]] = []

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        self.offered.append([t.name for t in tools])
        if len(messages) == 1 and self.first:
            return Step(tuple(self.first), [], "scripted")
        if self.finding is None:
            return Step((ToolCall("calculate", {"expression": "1 + 1"}),), [], "scripted")
        return Step((ToolCall("submit_finding", self.finding),), [], "scripted")


def lump_finding(**changes: Any) -> dict[str, Any]:
    finding = {
        "cause": "paid_in_lump_sum",
        "confidence": "high",
        "evidence": ["P1", "I1", "I2"],
        "action": "record_finding",
        "note": "P1 covers I1 and I2 together.",
    }
    finding.update(changes)
    return finding


def test_a_finding_that_holds_passes_and_one_that_invents_does_not(generated: Any) -> None:
    item = case_with(generated, "paid_in_lump_sum")
    ledger = fresh(generated)
    look = ToolCall("customer_records", {"customer": "C1"})
    done = investigate(Script([look], lump_finding()), fresh(generated), item.case)
    assert done.verified, done.problems
    assert done.trace[0]["tool"] == "customer_records"

    user_message(ledger, item.case)
    assert "it cites I99, which no tool showed" in verify(
        lump_finding(evidence=["I99"]), ledger, item.case
    )
    invented = verify(lump_finding(note="P1 was 123456.78 too much."), ledger, item.case)
    assert any("123456.78" in p for p in invented)
    # Only the case itself has been shown here, so the note's I1 and I2 are not known either.
    assert verify(lump_finding(evidence=[]), ledger, item.case) == [
        "it cites no evidence",
        "it cites I1, which no tool showed",
        "it cites I2, which no tool showed",
    ]
    missing = case_with(generated, "not_paid")
    assert "only an amount mismatch can be accepted" in verify(
        lump_finding(cause="not_paid", action="accept_variance", evidence=["I1"]),
        ledger,
        missing.case,
    )


def test_on_the_last_turn_only_submitting_is_offered(generated: Any) -> None:
    item = case_with(generated, "not_paid")
    model = Script([], None)
    done = investigate(model, fresh(generated), item.case, max_turns=3)
    assert model.offered[-1] == ["submit_finding"]
    assert done.finding is None and "no finding" in (done.error or "")


def test_a_failing_model_is_an_error_not_a_finding(generated: Any) -> None:
    class Down:
        def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
            raise LlmError("overloaded")

    done = investigate(Down(), fresh(generated), case_with(generated, "not_paid").case)
    assert done.error == "overloaded" and not done.verified


def run_graph(
    investigation: Investigation, *answers: dict[str, Any]
) -> tuple[dict[str, Any], list[Any]]:
    acted: list[Any] = []

    def act(finding: dict[str, Any], decision: dict[str, Any]) -> str:
        acted.append((finding, decision))
        return "applied"

    graph = build_graph(lambda: investigation, act, InMemorySaver())
    config: Any = {"configurable": {"thread_id": "t"}}
    state = graph.invoke({}, config)
    for answer in answers:
        state = graph.invoke(Command(resume=answer), config)
    return state, acted


def test_a_verified_finding_waits_for_a_person_and_is_applied_only_if_approved() -> None:
    good = Investigation(finding=lump_finding())
    paused, acted = run_graph(good)
    assert paused["__interrupt__"][0].value["finding"] == lump_finding()
    assert acted == []

    state, acted = run_graph(good, {"approve": True, "note": "ok"})
    assert state["outcome"] == "applied" and acted == [
        (lump_finding(), {"approve": True, "note": "ok"})
    ]

    state, acted = run_graph(good, {"approve": False})
    assert state["outcome"] == "rejected" and acted == []


def test_an_unverified_finding_is_never_shown() -> None:
    state, acted = run_graph(Investigation(finding=lump_finding(), problems=["it cites I99"]))
    assert state["outcome"] == "not_shown" and "__interrupt__" not in state and acted == []


def test_the_rules_baseline_names_the_other_records_of_a_lump_sum(generated: Any) -> None:
    ledger = generated[0]
    item = case_with(generated, "paid_in_lump_sum")
    cause, found = rules_cause(ledger, item.case)
    assert cause == "paid_in_lump_sum" and item.counterparts <= found

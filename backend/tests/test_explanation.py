import json
from datetime import date
from typing import Any

from recon.domain.money import Money
from recon.explanation.case import ExplainInput, RecordView
from recon.explanation.llm import LlmExplainer
from recon.explanation.schema import ANSWER_SCHEMA, SYSTEM_PROMPT, LlmExplanation, user_message
from recon.explanation.verify import render, verify
from recon.mapping.llm_client import Completion, LlmRefusal


def partial_case() -> ExplainInput:
    return ExplainInput(
        kind="partial_payment",
        variance=Money(40_000, "USD"),
        records=(
            RecordView(
                "R1", "invoice", "INV-000123", date(2026, 1, 5), Money(100_000, "USD"), None
            ),
            RecordView(
                "R2",
                "payment",
                "PAY-000456",
                date(2026, 1, 20),
                Money(60_000, "USD"),
                "part 1 of 2",
            ),
        ),
    )


def good() -> LlmExplanation:
    return LlmExplanation(
        summary="[R1] of 1,000.00 USD is only partly paid.",
        why="[R2] paid 600.00 USD, so 400.00 USD is still open.",
        next_steps=("Ask whether a second payment for [R1] is on its way.",),
    )


def with_(**changes: Any) -> LlmExplanation:
    return good().model_copy(update=changes)


def test_an_explanation_that_cites_the_case_and_states_its_figures_passes() -> None:
    assert verify(good(), partial_case()) == []


def test_an_alias_that_is_not_in_the_case_is_rejected() -> None:
    problems = verify(with_(why="[R7] paid 600.00 USD."), partial_case())

    assert any("R7" in p for p in problems)


def test_a_number_the_engine_did_not_give_is_rejected() -> None:
    problems = verify(with_(why="[R2] paid 600.00 USD, so 350.00 USD is open."), partial_case())

    assert any("350.00" in p for p in problems)


def test_the_engines_difference_may_be_stated_with_or_without_thousands_separators() -> None:
    ok = with_(summary="[R1] of 1000.00 USD is partly paid.", why="[R2] paid 600.00 USD.")

    assert verify(ok, partial_case()) == []


def test_a_real_record_id_written_out_is_rejected() -> None:
    problems = verify(with_(summary="INV-000123 is only partly paid, see [R1]."), partial_case())

    assert any("INV-000123" in p for p in problems)


def test_an_id_the_record_text_itself_contains_may_be_quoted() -> None:
    inp = partial_case()
    records = (
        inp.records[0],
        RecordView(
            "R2", "payment", "PAY-000456", date(2026, 1, 20), Money(60_000, "USD"), "for INV-000123"
        ),
    )
    quoted = ExplainInput(inp.kind, inp.variance, records)
    text = with_(why="[R2] says it is 'for INV-000123' and paid 600.00 USD.")

    assert verify(text, quoted) == []


def test_text_that_cites_no_record_is_rejected() -> None:
    problems = verify(
        LlmExplanation(
            summary="An invoice is partly paid.", why="Money is missing.", next_steps=("Check.",)
        ),
        partial_case(),
    )

    assert any("refer to any record" in p for p in problems)


def test_empty_and_overlong_text_and_too_many_steps_are_rejected() -> None:
    problems = verify(
        LlmExplanation(summary=" ", why="[R1] " + "x" * 900, next_steps=("[R1]",) * 4),
        partial_case(),
    )

    assert any("empty" in p for p in problems)
    assert any("longer than 800" in p for p in problems)
    assert any("4 next steps" in p for p in problems)


def test_no_next_step_is_rejected() -> None:
    assert verify(with_(next_steps=()), partial_case())


def test_rendering_puts_the_real_ids_back() -> None:
    shown = render(good(), partial_case())

    assert shown.summary.startswith("INV-000123 of 1,000.00 USD")
    assert "PAY-000456" in shown.why
    assert "[R" not in shown.summary + shown.why + shown.next_steps[0]


def test_the_model_is_not_told_the_customer_or_the_real_ids() -> None:
    message = user_message(partial_case())

    assert "INV-000123" not in message and "PAY-000456" not in message
    assert '"ref": "R1"' in message
    assert '"difference": "400.00 USD"' in message
    assert '"difference_is": "unpaid"' in message
    assert "part 1 of 2" in message


def test_an_overpayment_is_described_as_overpaid() -> None:
    inp = ExplainInput("amount_mismatch", Money(-500, "USD"), partial_case().records)

    assert '"difference_is": "overpaid"' in user_message(inp)
    assert '"difference": "5.00 USD"' in user_message(inp)


def test_the_prompt_forbids_what_the_verifier_checks() -> None:
    assert "[R1]" in SYSTEM_PROMPT and "State no number" in SYSTEM_PROMPT
    assert ANSWER_SCHEMA["required"] == ["summary", "why", "next_steps"]


class FakeModel:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked = 0

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.asked += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return Completion(text, "fake", 300, 60)


def test_a_verified_answer_comes_back_with_real_ids() -> None:
    explainer = LlmExplainer(FakeModel(good().model_dump()))

    result = explainer.explain(partial_case())

    assert result.text is not None and result.text.summary.startswith("INV-000123")
    assert result.problems == ()
    assert (explainer.calls, explainer.rejected) == (1, 0)
    assert (explainer.input_tokens, explainer.output_tokens) == (300, 60)


def test_an_answer_with_an_invented_figure_is_rejected_and_counted() -> None:
    bad = with_(why="[R2] paid 600.00 USD, so 123.45 USD is open.").model_dump()
    explainer = LlmExplainer(FakeModel(bad))

    result = explainer.explain(partial_case())

    assert result.text is None and result.problems
    assert explainer.rejected == 1


def test_checking_can_be_switched_off_for_comparison() -> None:
    bad = with_(why="[R2] paid 600.00 USD, so 123.45 USD is open.").model_dump()

    assert LlmExplainer(FakeModel(bad), verify_text=False).explain(partial_case()).text is not None


def test_a_refusal_or_malformed_json_gives_no_text_instead_of_an_error() -> None:
    for answer in (LlmRefusal("declined"), "not json", {"summary": "x"}):
        explainer = LlmExplainer(FakeModel(answer))

        result = explainer.explain(partial_case())

        assert result.text is None and result.problems
        assert explainer.calls == 0 and len(explainer.errors) == 1

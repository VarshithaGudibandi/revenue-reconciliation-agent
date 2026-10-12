import json
from typing import Any

import pytest

from recon.adjudication.case import AdjudicationCase, Candidate, Ruling
from recon.adjudication.combine import combine_rulings
from recon.adjudication.llm import LlmAdjudicator, to_ruling
from recon.adjudication.rules import adjudicate_by_rules, numbers_in
from recon.adjudication.schema import (
    NONE,
    SYSTEM_PROMPT,
    LlmRuling,
    answer_schema,
    user_message,
)
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.text import MEMO_KINDS
from recon.evaluation.adjudication_eval import (
    cases_from_dataset,
    judge,
    run_evaluation,
)
from recon.mapping.llm_client import Completion, LlmRefusal


def case(memo: str, *descriptions: str) -> AdjudicationCase:
    services = descriptions or ("Hosting Jan 2026", "Training Jan 2026", "Consulting Jan 2026")
    return AdjudicationCase(
        "PAY-000001",
        memo,
        tuple(Candidate("ABC"[i], f"INV-{120 + i:06d}", d, 10 - i) for i, d in enumerate(services)),
    )


def test_a_memo_that_names_one_invoice_number_decides_it() -> None:
    ruling = adjudicate_by_rules(case("pls apply to inv 121"))

    assert ruling.choice == "B"
    assert ruling.confident
    assert ruling.evidence == "121"


@pytest.mark.parametrize("memo", ["INV-000122", "INV000122", "invoice #122", "Re: inv-000122"])
def test_the_ways_people_write_an_invoice_number_are_all_read(memo: str) -> None:
    assert adjudicate_by_rules(case(memo)).choice == "C"


def test_a_memo_that_names_two_candidates_is_a_conflict_not_a_guess() -> None:
    ruling = adjudicate_by_rules(case("inv 120 and 121"))

    assert ruling.choice is None
    assert not ruling.confident
    assert ruling.alternatives == ("A", "B")


def test_a_year_in_the_memo_is_not_taken_for_an_invoice_number() -> None:
    assert numbers_in("Hosting - 2026") == set()
    assert numbers_in("inv 121 2026") == {121}
    candidates = AdjudicationCase(
        "P",
        "Hosting - 2026",
        (
            Candidate("A", "INV-002026", "Training Jan 2026", 3),
            Candidate("B", "INV-000001", "Hosting Jan 2026", 2),
        ),
    )

    ruling = adjudicate_by_rules(candidates)

    assert ruling.choice == "B"
    assert "word" in ruling.reasons[0]


def test_a_memo_with_the_words_of_one_invoice_decides_it() -> None:
    ruling = adjudicate_by_rules(case("Re: training jan"))

    assert ruling.choice == "B"
    assert ruling.confident


@pytest.mark.parametrize("memo", ["", "Thank you", "Payment", "Beratung", "cloud servers"])
def test_a_memo_that_does_not_separate_the_candidates_gets_no_answer(memo: str) -> None:
    ruling = adjudicate_by_rules(case(memo))

    assert ruling.choice is None
    assert not ruling.confident


def test_the_rules_never_decide_from_the_order_or_the_dates() -> None:
    ruling = adjudicate_by_rules(case("Thank you"))

    assert ruling.choice is None
    assert "separates" in ruling.reasons[0]


def answer(choice: str, evidence: str, confidence: str = "high") -> LlmRuling:
    return LlmRuling(choice=choice, confidence=confidence, evidence=evidence, reason="test")


def test_a_confident_choice_with_a_real_quote_is_relied_on() -> None:
    ruling = to_ruling(case("Beratung Jan"), answer("C", "Beratung"))

    assert ruling.choice == "C"
    assert ruling.confident
    assert ruling.evidence == "Beratung"


def test_the_quote_is_compared_without_regard_to_case_or_spacing() -> None:
    assert to_ruling(case("Re:  Staff   Workshop"), answer("B", "staff workshop")).confident


def test_a_quote_that_is_not_in_the_memo_means_the_choice_is_not_relied_on() -> None:
    checked = to_ruling(case("Thank you"), answer("A", "hosting for January"))
    unchecked = to_ruling(case("Thank you"), answer("A", "hosting for January"), check=False)

    assert checked.choice == "A"
    assert not checked.confident
    assert "not in the memo" in checked.reasons[-1]
    assert unchecked.confident


def test_a_choice_with_no_quote_is_not_relied_on() -> None:
    assert not to_ruling(case("Thank you"), answer("A", "")).confident


def test_none_and_unknown_labels_are_no_answer() -> None:
    assert to_ruling(case("x"), answer(NONE, "")).choice is None
    assert to_ruling(case("x"), answer("Z", "x")).choice is None


def test_medium_confidence_is_a_suggestion() -> None:
    ruling = to_ruling(case("Schulung"), answer("B", "Schulung", "medium"))

    assert ruling.choice == "B"
    assert not ruling.confident


class Scripted:
    def __init__(self, reply: dict[str, Any] | str | Exception) -> None:
        self.reply = reply
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.requests.append((system, user, schema))
        if isinstance(self.reply, Exception):
            raise self.reply
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return Completion(text, "fake", 300, 40)


def test_the_model_adjudicator_asks_with_the_candidates_and_counts_its_use() -> None:
    model = Scripted({"choice": "B", "confidence": "high", "evidence": "Schulung", "reason": "r"})
    adjudicator = LlmAdjudicator(model)

    ruling = adjudicator.adjudicate(case("Schulung Jan"))

    assert ruling.choice == "B"
    assert ruling.confident
    assert adjudicator.calls == 1
    assert adjudicator.input_tokens == 300
    system, user, schema = model.requests[0]
    assert system == SYSTEM_PROMPT
    assert schema["properties"]["choice"]["enum"] == ["A", "B", "C", NONE]
    assert json.loads(user)["memo"] == "Schulung Jan"


@pytest.mark.parametrize("bad", ["not json", "{}", '{"choice": 3}'])
def test_an_unusable_answer_is_no_answer_and_is_recorded(bad: str) -> None:
    adjudicator = LlmAdjudicator(Scripted(bad))

    ruling = adjudicator.adjudicate(case("x"))

    assert ruling.choice is None
    assert len(adjudicator.errors) == 1
    assert adjudicator.calls == 0


def test_a_refusal_is_no_answer_and_is_recorded() -> None:
    adjudicator = LlmAdjudicator(Scripted(LlmRefusal("declined")))

    assert adjudicator.adjudicate(case("x")).choice is None
    assert "declined" in adjudicator.errors[0]


def test_the_request_holds_the_memo_and_invoice_facts_and_nothing_about_the_customer() -> None:
    dataset = generate_dataset(Config(seed=1, invoices=200, ambiguous_rate=0.06))
    customers = {i.customer_id for i in dataset.invoices}
    payment_ids = {p.payment_id for p in dataset.payments}

    for labelled in cases_from_dataset(dataset):
        text = user_message(labelled.case)
        assert not any(c in text for c in customers)
        assert labelled.case.payment_id not in text
        assert payment_ids.isdisjoint(text.split('"'))
        assert set(json.loads(text)) == {"memo", "candidates"}
        for candidate in json.loads(text)["candidates"]:
            assert set(candidate) == {
                "label",
                "invoice_number",
                "for",
                "issued_days_before_payment",
            }


def test_the_prompt_says_to_answer_none_rather_than_guess() -> None:
    assert '"none"' in SYSTEM_PROMPT
    assert "Do not choose because of the order" in SYSTEM_PROMPT
    assert "worst outcome" in SYSTEM_PROMPT
    assert answer_schema(("A", "B"))["additionalProperties"] is False


def ruling(choice: str | None, confident: bool) -> Ruling:
    return Ruling(choice, confident, reasons=("r",))


def test_agreement_is_confident() -> None:
    assert combine_rulings(ruling("A", True), ruling("A", True)).confident


def test_a_confident_rule_is_not_overruled_by_a_model_that_is_not_sure() -> None:
    combined = combine_rulings(ruling("A", True), ruling("B", False))

    assert combined.choice == "A"
    assert combined.confident


def test_a_model_that_is_sure_where_the_rules_abstain_is_only_a_suggestion() -> None:
    combined = combine_rulings(ruling(None, False), ruling("B", True))

    assert combined.choice == "B"
    assert not combined.confident
    assert "Suggested by the model" in combined.reasons[0]


def test_two_confident_answers_that_disagree_leave_the_decision_to_a_person() -> None:
    combined = combine_rulings(ruling("A", True), ruling("B", True))

    assert combined.choice is None
    assert not combined.confident
    assert combined.alternatives == ("A", "B")


def test_neither_sure_offers_the_models_choice_unconfidently() -> None:
    combined = combine_rulings(ruling(None, False), ruling("C", False))

    assert combined.choice == "C"
    assert not combined.confident


def test_cases_from_data_hide_which_invoice_was_meant_and_know_it_for_scoring() -> None:
    dataset = generate_dataset(Config(seed=2, invoices=200, ambiguous_rate=0.06))
    labelled = cases_from_dataset(dataset)

    assert labelled
    positions = {c.intended for c in labelled}
    assert len(positions) > 1  # the intended candidate is not always first
    for item in labelled:
        assert item.memo_kind in MEMO_KINDS
        assert item.intended in item.case.labels()
        assert 2 <= len(item.case.candidates) <= 3


def test_judging_separates_confident_wrong_from_suggestions() -> None:
    assert judge(Ruling("A", True), "A") == "correct"
    assert judge(Ruling("B", True), "A") == "wrong"
    assert judge(Ruling("A", False), "A") == "suggested_right"
    assert judge(Ruling("B", False), "A") == "suggested_wrong"
    assert judge(Ruling(None, False), "A") == "abstained"


def test_the_rules_are_never_confidently_wrong_and_find_what_the_memo_says() -> None:
    results = run_evaluation(adjudicate_by_rules, seeds=range(6))

    assert all(counts["wrong"] == 0 for counts in results.values())
    assert results["explicit"]["correct"] == sum(results["explicit"].values()) > 0
    assert results["descriptive"]["correct"] == sum(results["descriptive"].values()) > 0
    assert results["paraphrased"]["abstained"] == sum(results["paraphrased"].values()) > 0
    assert results["useless"]["abstained"] == sum(results["useless"].values()) > 0


def test_a_model_that_always_answers_a_with_an_invented_quote_is_caught_by_the_check() -> None:
    def always_a(item: AdjudicationCase) -> dict[str, Any]:
        return {
            "choice": "A",
            "confidence": "high",
            "evidence": "the memo said so",
            "reason": "r",
        }

    class Stubborn:
        def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
            return Completion(json.dumps(always_a(case(""))), "fake", 1, 1)

    checked = run_evaluation(LlmAdjudicator(Stubborn()).adjudicate, seeds=range(3))
    unchecked = run_evaluation(LlmAdjudicator(Stubborn(), check=False).adjudicate, seeds=range(3))

    assert sum(c["wrong"] for c in checked.values()) == 0
    assert sum(c["wrong"] for c in unchecked.values()) > 0

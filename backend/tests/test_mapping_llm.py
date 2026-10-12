import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles
from recon.datagen.writer import write_dataset
from recon.evaluation.mapping_score import classify, score_file
from recon.ingestion.normalize import ColumnMapping
from recon.mapping.llm import LlmMapper
from recon.mapping.llm_client import (
    AnthropicJson,
    CassetteMiss,
    Completion,
    LlmError,
    LlmRefusal,
    RecordingJson,
    ReplayJson,
    request_key,
)
from recon.mapping.llm_schema import (
    NO_COLUMN,
    SYSTEM_PROMPT,
    LlmFileMapping,
    answer_schema,
    user_message,
)
from recon.mapping.profile import DATE_FORMATS, FileProfile, mask_value, profile_csv_bytes


def profile_of(style: str, source: str, tmp_path: Path, seed: int = 0) -> FileProfile:
    write_dataset(
        generate_dataset(Config(seed=seed, invoices=100)),
        tmp_path,
        ExportStyles(invoices=style) if source == "invoices" else ExportStyles(payments=style),
    )
    return profile_csv_bytes(f"{source}.csv", (tmp_path / f"{source}.csv").read_bytes())


def answer_for(mapping: ColumnMapping, role: str, **overrides: Any) -> dict[str, Any]:
    """What a perfect model would say, with optional changes to single fields."""

    def field(column: str | None, confidence: str = "high") -> dict[str, Any]:
        return {
            "column": column or NO_COLUMN,
            "confidence": confidence,
            "alternatives": [],
            "reason": "test",
        }

    answer: dict[str, Any] = {
        "record_id": field(mapping.record_id),
        "counterparty": field(mapping.counterparty),
        "date": field(mapping.date),
        "amount": field(mapping.amount),
        "currency": field(mapping.currency),
        "reference": field(mapping.reference if role == "payments" else None),
        "text": field(mapping.text),
        "date_format": {"value": mapping.date_format, "confidence": "high", "reason": "test"},
        "decimal_separator": {
            "value": mapping.decimal_separator,
            "confidence": "high",
            "reason": "test",
        },
        "questions": [],
    }
    answer.update(overrides)
    return answer


class FakeModel:
    def __init__(self, answer: dict[str, Any] | str) -> None:
        self.answer = answer
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.requests.append((system, user, schema))
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return Completion(text, "fake", 100, 20)


def test_the_answer_schema_allows_only_the_files_own_headers(tmp_path: Path) -> None:
    profile = profile_of("german", "invoices", tmp_path)

    schema = answer_schema(profile.headers())

    column = schema["properties"]["amount"]["properties"]["column"]
    assert column["enum"] == [*profile.headers(), NO_COLUMN]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert set(schema["properties"]["date_format"]["properties"]["value"]["enum"]) == {
        *DATE_FORMATS,
        "unknown",
    }


def test_the_prompt_forbids_guessing_the_date_order_and_asks_for_doubt() -> None:
    assert "never guess the order" in SYSTEM_PROMPT
    assert "worst outcome" in SYSTEM_PROMPT


def test_a_perfect_answer_becomes_a_confident_proposal_that_scores_exact(tmp_path: Path) -> None:
    style = INVOICE_STYLES["german"]
    profile = profile_of("german", "invoices", tmp_path)
    mapper = LlmMapper(FakeModel(answer_for(style.mapping(), "invoices")))

    proposal = mapper.propose_file(profile, "invoices")

    assert proposal.confident
    assert proposal.to_mapping() == style.mapping()
    assert classify(score_file(proposal, style.mapping())) == "exact"
    assert mapper.calls == 1
    assert mapper.input_tokens == 100


def test_the_request_describes_columns_without_real_values(tmp_path: Path) -> None:
    profile = profile_of("default", "invoices", tmp_path)
    model = FakeModel(answer_for(INVOICE_STYLES["default"].mapping(), "invoices"))

    LlmMapper(model).propose_file(profile, "invoices")

    _, user, _ = model.requests[0]
    first_customer = profile.cells["customer_id"][0]
    first_invoice = profile.cells["invoice_id"][0]
    assert first_customer not in user
    assert first_invoice not in user
    assert mask_value(first_invoice) in user
    assert '"file_role": "invoices"' in user


def test_a_column_the_file_does_not_have_is_dropped(tmp_path: Path) -> None:
    style = INVOICE_STYLES["default"]
    profile = profile_of("default", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["amount"]["column"] = "grand_total"

    proposal = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")

    assert proposal.choice("amount").column is None
    assert not proposal.choice("amount").confident
    assert proposal.to_mapping() is None


def test_a_confident_answer_the_data_contradicts_is_downgraded_to_a_question(
    tmp_path: Path,
) -> None:
    style = INVOICE_STYLES["default"]
    profile = profile_of("default", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["date"]["column"] = "total"  # an amount column offered as the date

    checked = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")
    unchecked = LlmMapper(FakeModel(answer), validate=False).propose_file(profile, "invoices")

    assert checked.choice("date").column is None
    assert "total" in checked.choice("date").alternatives
    assert not checked.choice("date").confident
    assert classify(score_file(checked, style.mapping())) != "wrong"
    assert unchecked.choice("date").column == "total"
    assert classify(score_file(unchecked, style.mapping())) == "wrong"


def test_a_date_order_the_values_cannot_settle_is_never_confident(tmp_path: Path) -> None:
    import dataclasses

    dataset = generate_dataset(Config(seed=1, invoices=100))
    early = dataclasses.replace(
        dataset, invoices=tuple(i for i in dataset.invoices if i.issue_date.day <= 12)
    )
    write_dataset(early, tmp_path, ExportStyles(invoices="spanish"))
    profile = profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes())
    style = INVOICE_STYLES["spanish"]
    # the model is sure, and may even be right, but the values do not say so
    proposal = LlmMapper(FakeModel(answer_for(style.mapping(), "invoices"))).propose_file(
        profile, "invoices"
    )

    assert proposal.choice("date").confident
    assert not proposal.date_format.confident
    assert proposal.date_format.value is None
    assert set(proposal.date_format.alternatives) == {"%d/%m/%Y", "%m/%d/%Y"}
    assert not proposal.confident


def test_a_decimal_separator_the_amounts_do_not_read_with_is_not_accepted(
    tmp_path: Path,
) -> None:
    style = INVOICE_STYLES["german"]
    profile = profile_of("german", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["decimal_separator"]["value"] = "."

    proposal = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")

    assert not proposal.decimal_separator.confident
    assert proposal.decimal_separator.alternatives == (",",)


def test_one_column_for_two_fields_is_not_trusted(tmp_path: Path) -> None:
    style = INVOICE_STYLES["default"]
    profile = profile_of("default", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["counterparty"]["column"] = style.mapping().record_id

    proposal = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")

    assert not proposal.choice("record_id").confident
    assert not proposal.choice("counterparty").confident


def test_medium_confidence_is_a_question_that_keeps_the_suggestion(tmp_path: Path) -> None:
    style = INVOICE_STYLES["german"]
    profile = profile_of("german", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["amount"]["confidence"] = "medium"
    answer["amount"]["alternatives"] = ["Nettobetrag"]

    proposal = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")

    amount = proposal.choice("amount")
    assert amount.column == "Bruttobetrag"
    assert not amount.confident
    assert amount.alternatives == ("Nettobetrag",)


def test_no_reference_column_is_a_valid_confident_answer_for_payments(tmp_path: Path) -> None:
    style = PAYMENT_STYLES["terse_no_reference"]
    profile = profile_of("terse_no_reference", "payments", tmp_path)

    proposal = LlmMapper(FakeModel(answer_for(style.mapping(), "payments"))).propose_file(
        profile, "payments"
    )

    assert proposal.choice("reference").column is None
    assert proposal.choice("reference").confident
    assert classify(score_file(proposal, style.mapping())) == "exact"


@pytest.mark.parametrize("bad", ["not json", "{}", '{"record_id": 3}'])
def test_an_unusable_answer_leaves_every_field_open_and_is_recorded(
    bad: str, tmp_path: Path
) -> None:
    profile = profile_of("default", "invoices", tmp_path)
    mapper = LlmMapper(FakeModel(bad))

    proposal = mapper.propose_file(profile, "invoices")

    assert proposal.to_mapping() is None
    assert not proposal.confident
    assert len(mapper.errors) == 1
    assert mapper.calls == 0


def test_a_refusal_is_recorded_not_raised(tmp_path: Path) -> None:
    class Refusing:
        def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
            raise LlmRefusal("the model declined the request")

    mapper = LlmMapper(Refusing())

    proposal = mapper.propose_file(profile_of("default", "invoices", tmp_path), "invoices")

    assert not proposal.confident
    assert "declined" in mapper.errors[0]


def test_the_model_answer_schema_is_accepted_by_the_answer_type() -> None:
    answer = answer_for(INVOICE_STYLES["default"].mapping(), "invoices")

    assert LlmFileMapping.model_validate(answer).amount.column == "total"


class StubMessages:
    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self.response


def stub_client(response: SimpleNamespace) -> tuple[SimpleNamespace, StubMessages, StubMessages]:
    plain, beta = StubMessages(response), StubMessages(response)
    return SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta)), plain, beta


def reply(text: str = "{}", stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=340),
        model="claude-opus-5-5",
    )


def test_the_live_client_asks_for_structured_output_at_low_effort_without_forcing_a_tool() -> None:
    client, _, beta = stub_client(reply('{"a": 1}'))
    live = AnthropicJson(client=client)

    completion = live.complete("system text", "user text", {"type": "object"})

    request = beta.calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["system"] == "system text"
    assert request["messages"] == [{"role": "user", "content": "user text"}]
    assert request["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert "tool_choice" not in request
    assert "thinking" not in request
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"
    assert completion == Completion('{"a": 1}', "claude-opus-5-5", 1200, 340)


def test_fallbacks_can_be_turned_off() -> None:
    client, plain, beta = stub_client(reply())

    AnthropicJson(client=client, fallbacks=False).complete("s", "u", {})

    assert len(plain.calls) == 1
    assert not beta.calls
    assert "fallbacks" not in plain.calls[0]
    assert "betas" not in plain.calls[0]


def test_a_refused_or_cut_off_answer_raises() -> None:
    client, _, _ = stub_client(reply(stop_reason="refusal"))
    with pytest.raises(LlmRefusal):
        AnthropicJson(client=client).complete("s", "u", {})

    client, _, _ = stub_client(reply(stop_reason="max_tokens"))
    with pytest.raises(LlmError, match="token limit"):
        AnthropicJson(client=client).complete("s", "u", {})

    empty = SimpleNamespace(
        stop_reason="end_turn",
        content=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client, _, _ = stub_client(empty)
    with pytest.raises(LlmError, match="no text"):
        AnthropicJson(client=client).complete("s", "u", {})


def test_answers_are_recorded_once_and_replayed_without_the_model(tmp_path: Path) -> None:
    cassette = tmp_path / "answers.jsonl"
    live = FakeModel({"x": 1})
    recording = RecordingJson(live, cassette)

    first = recording.complete("sys", "user", {"s": 1})
    again = recording.complete("sys", "user", {"s": 1})
    other = recording.complete("sys", "different", {"s": 1})

    assert first == again
    assert len(live.requests) == 2
    assert len(cassette.read_text(encoding="utf-8").splitlines()) == 2

    replay = ReplayJson(cassette)
    assert len(replay) == 2
    assert replay.complete("sys", "user", {"s": 1}).text == first.text
    assert replay.complete("sys", "different", {"s": 1}).text == other.text
    with pytest.raises(CassetteMiss, match="ANTHROPIC_API_KEY"):
        replay.complete("sys", "never asked", {"s": 1})


def test_a_recording_picks_up_where_a_previous_one_stopped(tmp_path: Path) -> None:
    cassette = tmp_path / "answers.jsonl"
    RecordingJson(FakeModel({"x": 1}), cassette).complete("sys", "user", {})
    second = FakeModel({"y": 2})

    RecordingJson(second, cassette).complete("sys", "user", {})

    assert not second.requests


def test_a_replay_with_nothing_recorded_is_empty_and_a_miss_is_reported(tmp_path: Path) -> None:
    replay = ReplayJson(tmp_path / "missing.jsonl")
    assert len(replay) == 0

    mapper = LlmMapper(replay)
    proposal = mapper.propose_file(profile_of("default", "invoices", tmp_path), "invoices")

    assert not proposal.confident
    assert "not recorded" in mapper.errors[0]


def test_the_request_key_changes_with_every_part_of_the_request() -> None:
    base = request_key("s", "u", {"a": 1})

    assert request_key("s", "u", {"a": 1}) == base
    assert request_key("s2", "u", {"a": 1}) != base
    assert request_key("s", "u2", {"a": 1}) != base
    assert request_key("s", "u", {"a": 2}) != base


def test_masking_keeps_form_and_hides_most_of_the_value() -> None:
    assert mask_value("INV-000123") == "INV-0*****"
    assert mask_value("ACME-CORP") == "A***-C***"
    assert mask_value("1500.00") == "1***.0*"
    assert mask_value("EUR") == "EUR"
    assert mask_value("") == ""


@given(st.text(max_size=40))
def test_masking_never_changes_length_or_reveals_more_than_the_first_digit_of_a_run(
    value: str,
) -> None:
    masked = mask_value(value)

    assert len(masked) == len(value)
    runs = re.findall(r"\d+", value)
    revealed = re.findall(r"\d+", masked)
    assert len(revealed) <= len(runs)
    assert all(len(r) == 1 for r in revealed)


def test_samples_show_short_codes_and_mask_everything_else(tmp_path: Path) -> None:
    profile = profile_of("erp_english", "invoices", tmp_path)

    described = profile.describe(samples=True)
    text = json.dumps(described)
    by_header = {c["header"]: c for c in described["columns"]}

    assert by_header["Currency"]["examples"]["kind"] == "codes"
    assert set(by_header["Currency"]["examples"]["values"]) <= {"EUR", "USD"}
    assert by_header["Status"]["examples"]["values"] == ["OPEN"]
    assert by_header["Invoice No"]["examples"]["kind"] == "masked"
    for header in ("Invoice No", "Customer Account", "Gross Amount", "Invoice Date"):
        for value in profile.cells[header][:20]:
            assert value not in text, f"{header} value leaked: {value}"


def test_the_default_description_still_holds_no_examples(tmp_path: Path) -> None:
    profile = profile_of("default", "invoices", tmp_path)

    assert "examples" not in json.dumps(profile.describe())
    assert user_message(profile, "invoices").count('"examples"') > 0


def test_a_text_column_the_data_contradicts_is_downgraded(tmp_path: Path) -> None:
    style = INVOICE_STYLES["erp_english"]
    profile = profile_of("erp_english", "invoices", tmp_path)
    answer = answer_for(style.mapping(), "invoices")
    answer["text"]["column"] = "Invoice Date"  # a date column offered as free text

    proposal = LlmMapper(FakeModel(answer)).propose_file(profile, "invoices")

    assert proposal.choice("text").column is None
    assert not proposal.choice("text").confident
    assert "Invoice Date" in proposal.choice("text").alternatives


def test_a_perfect_answer_includes_the_text_column_in_the_mapping(tmp_path: Path) -> None:
    style = PAYMENT_STYLES["bank_statement"]
    profile = profile_of("bank_statement", "payments", tmp_path)

    proposal = LlmMapper(FakeModel(answer_for(style.mapping(), "payments"))).propose_file(
        profile, "payments"
    )

    assert proposal.to_mapping() == style.mapping()
    assert style.mapping().text == "Details"


def test_the_description_of_a_file_says_how_much_of_each_column_is_several_words(
    tmp_path: Path,
) -> None:
    profile = profile_of("erp_english", "invoices", tmp_path)

    by_header = {c["header"]: c for c in profile.describe()["columns"]}

    assert by_header["Description"]["share_with_spaces"] == 1.0
    assert by_header["Invoice No"]["share_with_spaces"] == 0.0

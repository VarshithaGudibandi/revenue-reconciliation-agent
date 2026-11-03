"""The mapper in which the model measures both files with tools, with a scripted model."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.styles import ExportStyles
from recon.datagen.writer import write_dataset
from recon.ingestion.normalize import ColumnMapping
from recon.llm.client import LlmError
from recon.llm.tools import Message, Step, Tool, ToolCall, ToolResults
from recon.mapping.agent import AgentMapper, Measurements, user_message
from recon.mapping.profile import FileProfile, profile_csv_bytes
from tests.test_mapping_llm import answer_for


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> tuple[FileProfile, FileProfile, Any]:
    path = tmp_path_factory.mktemp("agent")
    write_dataset(
        generate_dataset(Config(seed=3, invoices=120)), path, ExportStyles("german", "german_bank")
    )
    truth = json.loads((path / "mapping_truth.json").read_text(encoding="utf-8"))
    return (
        profile_csv_bytes("invoices.csv", (path / "invoices.csv").read_bytes()),
        profile_csv_bytes("payments.csv", (path / "payments.csv").read_bytes()),
        truth,
    )


class Script:
    """Measures in the first turn, then submits the given answer; keeps what it was sent."""

    def __init__(self, measure: list[ToolCall], submit: dict[str, Any] | None) -> None:
        self.measure = measure
        self.submit = submit
        self.seen: list[tuple[list[Message], list[str]]] = []

    def step(self, system: str, messages: Any, tools: Any) -> Step:
        self.seen.append((list(messages), [t.name for t in tools]))
        if len(messages) == 1 and self.measure:
            return Step(tuple(self.measure), [], "scripted")
        if self.submit is None:
            return Step(
                (ToolCall("more_examples", {"file": "payments", "column": "Betrag"}),), [], "m"
            )
        return Step((ToolCall("submit_mapping", self.submit),), [], "scripted")


def truth_answer(truth: Any, **payment_overrides: Any) -> dict[str, Any]:
    return {
        "invoices": answer_for(
            ColumnMapping.model_validate(truth["invoices"]["mapping"]), "invoices"
        ),
        "payments": answer_for(
            ColumnMapping.model_validate(truth["payments"]["mapping"]),
            "payments",
            **payment_overrides,
        ),
    }


def test_the_model_measures_then_submits_and_the_answer_is_kept(files: Any) -> None:
    invoices, payments, truth = files
    measure = [
        ToolCall(
            "references_to_invoices",
            {"payment_column": "Verwendungszweck", "invoice_column": "Rechnungsnr."},
        ),
        ToolCall("shared_values", {"invoice_column": "Kunde", "payment_column": "Nope"}),
    ]
    model = Script(measure, truth_answer(truth))
    agent = AgentMapper(model)

    proposal = agent(invoices, payments)

    assert proposal.invoices.confident and proposal.payments.confident
    assert proposal.payments.to_mapping() == ColumnMapping.model_validate(
        truth["payments"]["mapping"]
    )
    results = model.seen[1][0][2]
    assert isinstance(results, ToolResults)
    assert results.results[0][1]["share_naming_an_invoice"] > 0.9
    # A wrong column name is answered with an error the model can read, not an exception.
    assert "no column 'Nope'" in results.results[1][1]["error"]
    assert (agent.mappings, agent.turns, agent.tool_calls) == (1, 2, 2)


def test_a_submitted_answer_is_still_checked_against_the_data(files: Any) -> None:
    invoices, payments, truth = files
    # The model says, confidently, that the booking text is the amount.
    wrong = {"column": "Buchungstext", "confidence": "high", "alternatives": [], "reason": "x"}
    agent = AgentMapper(Script([], truth_answer(truth, amount=wrong)))

    proposal = agent(invoices, payments)

    amount = proposal.payments.choice("amount")
    assert amount.column is None and not amount.confident
    assert any("Data check" in r for r in amount.reasons)


def test_on_the_last_turn_only_submitting_is_offered_and_never_submitting_is_no_answer(
    files: Any,
) -> None:
    invoices, payments, _ = files
    model = Script([], None)
    agent = AgentMapper(model, max_turns=3)

    proposal = agent(invoices, payments)

    assert [names for _, names in model.seen][-1] == ["submit_mapping"]
    assert "submit_mapping" in model.seen[0][1] and len(model.seen[0][1]) == 7
    assert not proposal.invoices.fields and not proposal.payments.fields
    assert "did not submit" in agent.errors[0]


def test_a_failing_model_leaves_every_field_to_the_user(files: Any) -> None:
    invoices, payments, _ = files

    class Broken:
        def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
            raise LlmError("down")

    agent = AgentMapper(Broken())
    proposal = agent(invoices, payments)

    assert not proposal.invoices.confident and agent.errors == [
        "invoices.csv and payments.csv: down"
    ]


def test_no_cell_value_reaches_the_model(files: Any) -> None:
    """Descriptions and tool results carry shapes, masked examples and shares, never values."""
    invoices, payments, _ = files
    measure = Measurements(invoices, payments)
    sent = [user_message(invoices, payments)]
    for column in invoices.headers():
        sent.append(
            json.dumps(
                measure.run(ToolCall("more_examples", {"file": "invoices", "column": column}))
            )
        )
    for column in payments.headers():
        sent.append(
            json.dumps(
                measure.run(ToolCall("more_examples", {"file": "payments", "column": column}))
            )
        )
    text = "\n".join(sent)
    for profile in (invoices, payments):
        for column, cells in profile.cells.items():
            if profile.column(column).currency_like():
                continue  # currency codes are shown as they are
            for value in cells[:20]:
                if len(value) >= 4 and not value.isupper():
                    assert value not in text, (column, value)


def test_compare_dates_tells_a_booking_date_from_the_value_date_after_it(files: Any) -> None:
    payments = files[1]
    measure = Measurements(files[0], payments)

    result = measure.run(
        ToolCall(
            "compare_dates",
            {"file": "payments", "first_column": "Buchungstag", "second_column": "Wertstellung"},
        )
    )

    assert result["share_first_earlier"] == 1.0
    assert result["median_days_second_minus_first"] == 1

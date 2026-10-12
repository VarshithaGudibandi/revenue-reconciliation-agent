from pathlib import Path

import pytest

from recon.datagen.generate import Config, first_days, generate_dataset
from recon.datagen.styles import ExportStyles
from recon.datagen.writer import write_dataset
from recon.mapping.answers import NO_PINS, Answer, Pins, apply_pins, pins_from
from recon.mapping.baseline import propose_mapping
from recon.mapping.profile import FileProfile, profile_csv_bytes
from recon.mapping.proposal import MappingProposal
from recon.mapping.questions import choose_question, settled_by


def profiles(
    tmp_path: Path, styles: ExportStyles, twelve_days: bool = False
) -> tuple[FileProfile, FileProfile]:
    dataset = generate_dataset(Config(seed=1, invoices=120))
    if twelve_days:
        dataset = first_days(dataset, 12)
    write_dataset(dataset, tmp_path, styles)
    return (
        profile_csv_bytes("invoices.csv", (tmp_path / "invoices.csv").read_bytes()),
        profile_csv_bytes("payments.csv", (tmp_path / "payments.csv").read_bytes()),
    )


def test_no_pins_is_the_same_proposal(tmp_path: Path) -> None:
    inv, pay = profiles(
        tmp_path,
        ExportStyles(
            "italian",
            "terse_no_reference",
        ),
        True,
    )

    assert propose_mapping(inv, pay, NO_PINS) == propose_mapping(inv, pay)


def test_an_answer_becomes_a_confident_choice_that_says_so(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("italian", "terse_no_reference"), True)
    open_before = propose_mapping(inv, pay).open_items()
    assert ("invoices", "date") in open_before

    after = propose_mapping(inv, pay, pins_from([Answer("invoices", "date", "Scadenza")]))

    chosen = after.invoices.choice("date")
    assert (chosen.column, chosen.confident, chosen.reasons) == ("Scadenza", True, ("Your answer",))
    assert ("invoices", "date") not in after.open_items()


def test_saying_there_is_no_column_is_an_answer_too(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("default", "default"))

    after = propose_mapping(inv, pay, pins_from([Answer("payments", "reference", None)]))

    reference = after.payments.choice("reference")
    assert reference.column is None and reference.confident
    assert "no such column" in reference.reasons[0]


def test_a_later_answer_replaces_an_earlier_one() -> None:
    pins = pins_from([Answer("invoices", "date", "A"), Answer("invoices", "date", "B")])

    assert pins == {("invoices", "date"): "B"}


def test_an_answer_about_an_unknown_file_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown file"):
        Answer("receipts", "date", "A")


def test_a_pinned_invoice_id_makes_the_payment_reference_be_looked_for_again(
    tmp_path: Path,
) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("default", "default"))
    base = propose_mapping(inv, pay)
    wrong = next(c for c in inv.headers() if c != base.invoices.choice("record_id").column)

    after = propose_mapping(inv, pay, pins_from([Answer("invoices", "record_id", wrong)]))

    assert after.invoices.choice("record_id").column == wrong
    assert after.payments.choice("reference") != base.payments.choice("reference")


def test_answers_override_a_proposal_made_without_them(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("default", "default"))
    base = propose_mapping(inv, pay)

    after = apply_pins(
        base,
        pins_from(
            [Answer("payments", "amount", "x"), Answer("invoices", "decimal_separator", ",")]
        ),
    )

    assert after.payments.choice("amount").column == "x"
    assert after.invoices.decimal_separator.value == ","
    assert after.payments.choice("date") == base.payments.choice("date")


def test_nothing_is_asked_when_nothing_is_open(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("default", "default"))
    headers = {"invoices": inv.headers(), "payments": pay.headers()}

    assert propose_mapping(inv, pay).open_items() == []
    assert choose_question(lambda p: propose_mapping(inv, pay, p), {}, headers) is None


def test_the_question_asked_is_one_that_is_open_and_answering_it_closes_it(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("italian", "terse_no_reference"), True)
    headers = {"invoices": inv.headers(), "payments": pay.headers()}

    def propose(pins: Pins) -> MappingProposal:
        return propose_mapping(inv, pay, pins)

    question = choose_question(propose, {}, headers)

    assert question is not None
    assert (question.source, question.field) in propose({}).open_items()
    answer = Answer(question.source, question.field, question.options[0])
    after = propose(pins_from([answer]))
    assert (question.source, question.field) not in after.open_items()


def test_settled_by_names_what_an_answer_closed_besides_itself(tmp_path: Path) -> None:
    inv, pay = profiles(tmp_path, ExportStyles("italian", "terse_no_reference"), True)
    before = propose_mapping(inv, pay)
    answer = Answer("invoices", "date_format", "%d/%m/%Y")
    after = propose_mapping(inv, pay, pins_from([answer]))

    closed = settled_by(before, after, answer)

    assert ("invoices", "date_format") not in closed
    assert all(item in before.open_items() and item not in after.open_items() for item in closed)

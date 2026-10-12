import dataclasses
from pathlib import Path

import pytest

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, STRESS_STYLES, ExportStyles
from recon.datagen.writer import write_dataset
from recon.evaluation.mapping_eval import run_case, run_evaluation, summarize
from recon.evaluation.mapping_score import classify, judge
from recon.ingestion.normalize import normalize_csv
from recon.mapping.baseline import propose_file, propose_mapping
from recon.mapping.pairing import each_file_alone
from recon.mapping.profile import FileProfile, profile_csv_bytes
from recon.mapping.proposal import FileProposal, MappingProposal, field_choice, value_choice


def proposal_for(styles: ExportStyles, tmp_path: Path, seed: int = 0) -> MappingProposal:
    write_dataset(generate_dataset(Config(seed=seed, invoices=120)), tmp_path, styles)
    return propose_mapping(
        profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes()),
        profile_csv_bytes("p.csv", (tmp_path / "payments.csv").read_bytes()),
    )


@pytest.mark.parametrize("payments", sorted(set(PAYMENT_STYLES) - set(STRESS_STYLES["payments"])))
@pytest.mark.parametrize("invoices", sorted(set(INVOICE_STYLES) - set(STRESS_STYLES["invoices"])))
def test_the_proposed_mapping_reads_the_generated_files_exactly(
    invoices: str, payments: str, tmp_path: Path
) -> None:
    dataset = generate_dataset(Config(seed=3, invoices=120))
    write_dataset(dataset, tmp_path, ExportStyles(invoices, payments))
    proposal = propose_mapping(
        profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes()),
        profile_csv_bytes("p.csv", (tmp_path / "payments.csv").read_bytes()),
    )

    assert proposal.invoices.confident
    assert proposal.payments.confident
    invoice_mapping, payment_mapping = (
        proposal.invoices.to_mapping(),
        proposal.payments.to_mapping(),
    )
    assert invoice_mapping is not None
    assert payment_mapping is not None
    read_invoices = normalize_csv(tmp_path / "invoices.csv", invoice_mapping)
    read_payments = normalize_csv(tmp_path / "payments.csv", payment_mapping)
    assert read_invoices.rejected == ()
    assert read_payments.rejected == ()
    assert [(r.record_id, r.amount.minor, r.occurred_on) for r in read_invoices.records] == [
        (i.invoice_id, i.total_minor, i.issue_date) for i in dataset.invoices
    ]
    assert [(r.record_id, r.amount.minor, r.occurred_on) for r in read_payments.records] == [
        (p.payment_id, p.amount_minor, p.received_on) for p in dataset.payments
    ]


def test_the_gross_amount_is_chosen_over_net_and_tax(tmp_path: Path) -> None:
    proposal = proposal_for(ExportStyles("german", "default"), tmp_path)

    amount = proposal.invoices.choice("amount")
    assert amount.column == "Bruttobetrag"
    assert amount.confident


def test_the_issue_date_is_chosen_over_the_due_date_by_the_data_alone(tmp_path: Path) -> None:
    # "terse" names them dt and due: no header word says which is which
    proposal = proposal_for(ExportStyles("terse", "default"), tmp_path)

    assert proposal.invoices.choice("date").column == "dt"
    assert "after this date" in proposal.invoices.choice("date").reasons[0]


def test_no_reference_column_is_claimed_when_the_payments_have_none(tmp_path: Path) -> None:
    proposal = proposal_for(ExportStyles("default", "terse_no_reference"), tmp_path)

    reference = proposal.payments.choice("reference")
    assert reference.column is None
    assert reference.confident


def test_a_date_that_could_be_read_either_way_is_a_question_not_a_guess(tmp_path: Path) -> None:
    dataset = generate_dataset(Config(seed=1, invoices=120))
    early = dataclasses.replace(
        dataset, invoices=tuple(i for i in dataset.invoices if i.issue_date.day <= 12)
    )
    write_dataset(early, tmp_path, ExportStyles("spanish", "default"))

    proposal = propose_mapping(
        profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes()),
        profile_csv_bytes("p.csv", (tmp_path / "payments.csv").read_bytes()),
    )

    date_format = proposal.invoices.date_format
    assert not date_format.confident
    assert date_format.value is None
    assert set(date_format.alternatives) == {"%d/%m/%Y", "%m/%d/%Y"}
    assert not proposal.invoices.confident
    assert proposal.invoices.to_mapping() is None


def test_two_columns_that_cannot_be_told_apart_are_offered_not_chosen() -> None:
    invoices = profile_csv_bytes(
        "i.csv",
        b"id,customer,when,cur,a,b\n"
        b"I1,C1,2026-01-05,USD,10.00,10.00\nI2,C2,2026-01-06,USD,20.00,20.00\n"
        b"I3,C1,2026-01-07,USD,30.00,30.00\n",
    )
    payments = profile_csv_bytes(
        "p.csv",
        b"pid,ref,payer,paid,cur,amount\n"
        b"P1,I1,C1,2026-01-10,USD,10.00\nP2,I2,C2,2026-01-11,USD,20.00\n"
        b"P3,I3,C1,2026-01-12,USD,30.00\n",
    )

    amount = propose_mapping(invoices, payments).invoices.choice("amount")

    assert not amount.confident
    assert set((amount.column, *amount.alternatives)) == {"a", "b"}


def test_judging_separates_wrong_from_asked_from_missed() -> None:
    assert judge("a", True, (), "a") == "correct"
    assert judge("b", True, (), "a") == "wrong"
    assert judge("b", False, ("a",), "a") == "asked"
    assert judge(None, False, ("a", "b"), "a") == "asked"
    assert judge("b", False, ("c",), "a") == "missed"
    assert judge(None, True, (), None) == "correct"
    assert judge("x", True, (), None) == "wrong"
    assert judge("x", False, (), None) == "asked"


def test_a_file_is_as_bad_as_its_worst_field() -> None:
    assert classify({"a": "correct", "b": "correct"}) == "exact"
    assert classify({"a": "correct", "b": "asked"}) == "needs_user"
    assert classify({"a": "asked", "b": "missed"}) == "missed"
    assert classify({"a": "missed", "b": "wrong"}) == "wrong"


def test_a_confidently_wrong_mapper_is_scored_wrong() -> None:
    def always_the_first_column(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        def side(profile: FileProfile) -> FileProposal:
            first = profile.headers()[0]
            return FileProposal(
                fields={
                    f: field_choice(first, True)
                    for f in (
                        "record_id",
                        "counterparty",
                        "date",
                        "amount",
                        "currency",
                        "reference",
                    )
                },
                date_format=value_choice("%Y-%m-%d", True),
                decimal_separator=value_choice(".", True),
            )

        return MappingProposal(invoices=side(invoices), payments=side(payments))

    result = run_case(always_the_first_column, ExportStyles(), "full_month", 0, 60)

    assert classify(result.invoices) == "wrong"
    assert classify(result.payments) == "wrong"
    assert result.invoices["record_id"] == "correct"
    assert result.invoices["amount"] == "wrong"
    assert result.details


def test_a_mapper_that_always_asks_is_never_wrong() -> None:
    def never_sure(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return MappingProposal(invoices=FileProposal(), payments=FileProposal())

    result = run_case(never_sure, ExportStyles(), "full_month", 0, 60)

    assert "wrong" not in set(result.invoices.values()) | set(result.payments.values())
    assert classify(result.invoices) == "missed"


def test_the_baseline_is_never_confidently_wrong_on_generated_exports() -> None:
    results = run_evaluation(propose_mapping, seeds=range(2), size=100)

    counts = summarize(results)
    for label in ("development", "held_out", "stress"):
        assert counts[label]["files"] > 0
        assert counts[label]["files_wrong"] == 0
        assert counts[label]["files_missed"] == 0
        assert counts[label]["fields_wrong"] == 0
    easy = [r for r in results if r.variant == "full_month" and r.group != "stress"]
    assert all(classify(r.invoices) == "exact" and classify(r.payments) == "exact" for r in easy)
    # the stress styles are there to be hard: some of them must still need the user
    assert counts["stress"]["files_needs_user"] > 0


def test_a_file_alone_is_mapped_by_header_words_and_never_confidently_wrong() -> None:
    results = run_evaluation(each_file_alone(propose_file), seeds=range(2), size=100)

    counts = summarize(results)
    for label in ("development", "held_out", "stress"):
        assert counts[label]["files_wrong"] == 0
        assert counts[label]["files_missed"] == 0
    # without the other file, header words are all there is, and they run out on the stress styles
    stress, development = counts["stress"], counts["development"]
    assert (
        stress["files_needs_user"] / stress["files"]
        > development["files_needs_user"] / development["files"]
    )


def test_a_single_file_picks_the_gross_amount_from_header_words(tmp_path: Path) -> None:
    write_dataset(generate_dataset(Config(seed=0, invoices=100)), tmp_path, ExportStyles("german"))

    proposal = propose_file(
        profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes()), "invoices"
    )

    assert proposal.choice("amount").column == "Bruttobetrag"
    assert proposal.choice("amount").confident
    assert proposal.decimal_separator.value == ","


def test_a_single_file_with_meaningless_headers_asks_and_offers_the_right_column(
    tmp_path: Path,
) -> None:
    write_dataset(
        generate_dataset(Config(seed=0, invoices=100)), tmp_path, ExportStyles("generic_columns")
    )

    proposal = propose_file(
        profile_csv_bytes("i.csv", (tmp_path / "invoices.csv").read_bytes()), "invoices"
    )

    amount = proposal.choice("amount")
    assert not amount.confident
    assert "Column H" in (amount.column, *amount.alternatives)
    assert not proposal.confident


def test_the_date_format_is_settled_when_every_candidate_date_column_shares_it() -> None:
    profile = profile_csv_bytes(
        "i.csv",
        b"a,b,c\n2026-01-05,2026-02-04,x1\n2026-01-06,2026-02-05,x2\n2026-01-07,2026-02-06,x3\n",
    )

    proposal = propose_file(profile, "invoices")

    assert not proposal.choice("date").confident  # which of the two dates is not known
    assert proposal.date_format.value == "%Y-%m-%d"
    assert proposal.date_format.confident

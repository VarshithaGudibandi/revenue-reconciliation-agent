import random
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recon.domain.money import Money
from recon.engine.model import (
    EngineConfig,
    ExceptionKind,
    ReconciliationException,
    ReconciliationResult,
)
from recon.engine.reconcile import reconcile
from recon.ingestion.normalize import NormalizedRecord, normalize_reference


def invoice(
    record_id: str, customer: str, amount: int, day: int = 1, currency: str = "USD"
) -> NormalizedRecord:
    return NormalizedRecord(
        source_row=1,
        record_id=record_id,
        reference=None,
        counterparty=customer,
        occurred_on=date(2026, 1, day),
        amount=Money(amount, currency),
    )


def payment(
    record_id: str,
    reference: str | None,
    customer: str,
    amount: int,
    day: int = 10,
    currency: str = "USD",
) -> NormalizedRecord:
    return NormalizedRecord(
        source_row=1,
        record_id=record_id,
        reference=normalize_reference(reference) if reference else None,
        counterparty=customer,
        occurred_on=date(2026, 1, day),
        amount=Money(amount, currency),
    )


def only(result: ReconciliationResult) -> ReconciliationException:
    assert result.matches == ()
    assert len(result.exceptions) == 1
    return result.exceptions[0]


def test_a_payment_that_equals_the_invoice_matches() -> None:
    result = reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", 1000)])

    assert result.exceptions == ()
    (match,) = result.matches
    assert (match.invoice_id, match.payment_ids, match.variance.minor) == ("INV-1", ("P1",), 0)
    assert match.rule == "reference"


def test_messy_references_still_match() -> None:
    result = reconcile(
        [invoice("INV-000123", "C1", 500)], [payment("P1", " inv 000123 ", "C1", 500)]
    )

    assert result.exceptions == ()
    assert len(result.matches) == 1


def test_split_payments_that_sum_to_the_invoice_match() -> None:
    result = reconcile(
        [invoice("INV-1", "C1", 1000)],
        [payment("P1", "INV-1", "C1", 300), payment("P2", "INV-1", "C1", 700)],
    )

    assert result.exceptions == ()
    assert result.matches[0].payment_ids == ("P1", "P2")


def test_two_full_payments_are_a_duplicate() -> None:
    result = reconcile(
        [invoice("INV-1", "C1", 1000)],
        [payment("P1", "INV-1", "C1", 1000), payment("P2", "INV-1", "C1", 1000)],
    )

    exception = only(result)
    assert exception.kind is ExceptionKind.DUPLICATE_PAYMENT
    assert exception.variance == Money(-1000, "USD")


def test_an_overpayment_that_is_not_a_repeat_is_an_amount_mismatch() -> None:
    exception = only(
        reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", 1050)])
    )

    assert exception.kind is ExceptionKind.AMOUNT_MISMATCH
    assert exception.variance == Money(-50, "USD")


@pytest.mark.parametrize(
    ("paid", "kind"),
    [
        (900, ExceptionKind.AMOUNT_MISMATCH),  # exactly 10% short is still a mismatch
        (899, ExceptionKind.PARTIAL_PAYMENT),
        (500, ExceptionKind.PARTIAL_PAYMENT),
        (999, ExceptionKind.AMOUNT_MISMATCH),
    ],
)
def test_the_partial_threshold_separates_mismatch_from_partial(
    paid: int, kind: ExceptionKind
) -> None:
    exception = only(
        reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", paid)])
    )

    assert exception.kind is kind
    assert exception.variance == Money(1000 - paid, "USD")


def test_the_partial_threshold_is_configurable() -> None:
    config = EngineConfig(partial_threshold_bps=5000)
    exception = only(
        reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", 600)], config)
    )

    assert exception.kind is ExceptionKind.AMOUNT_MISMATCH


def test_a_difference_within_tolerance_is_a_match_with_its_variance_recorded() -> None:
    config = EngineConfig(tolerance_minor=2)
    result = reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", 998)], config)

    assert result.exceptions == ()
    assert result.matches[0].variance == Money(2, "USD")


def test_a_payment_in_another_currency_cannot_be_compared() -> None:
    exception = only(
        reconcile(
            [invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C1", 1000, currency="EUR")]
        )
    )

    assert exception.kind is ExceptionKind.CURRENCY_MISMATCH
    assert exception.variance is None


def test_a_reference_to_another_customers_invoice_is_not_a_match() -> None:
    result = reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-1", "C2", 1000)])

    assert {e.kind for e in result.exceptions} == {
        ExceptionKind.MISSING_PAYMENT,
        ExceptionKind.UNMATCHED_PAYMENT,
    }
    assert result.matches == ()


def test_a_wrong_reference_with_one_candidate_is_proposed_not_applied() -> None:
    exception = only(
        reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-999", "C1", 1000)])
    )

    assert exception.kind is ExceptionKind.MISREFERENCED_PAYMENT
    assert exception.proposed_invoice_id == "INV-1"
    assert (exception.invoice_ids, exception.payment_ids) == (("INV-1",), ("P1",))
    assert exception.variance == Money(0, "USD")


def test_a_payment_without_a_reference_is_paired_the_same_way() -> None:
    exception = only(reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", None, "C1", 1000)]))

    assert exception.kind is ExceptionKind.MISREFERENCED_PAYMENT


def test_a_payment_before_the_invoice_is_not_a_candidate() -> None:
    result = reconcile(
        [invoice("INV-1", "C1", 1000, day=20)], [payment("P1", "INV-999", "C1", 1000, day=10)]
    )

    assert {e.kind for e in result.exceptions} == {
        ExceptionKind.MISSING_PAYMENT,
        ExceptionKind.UNMATCHED_PAYMENT,
    }


def test_a_payment_beyond_the_window_is_not_a_candidate() -> None:
    config = EngineConfig(pairing_window_days=5)
    result = reconcile(
        [invoice("INV-1", "C1", 1000, day=1)],
        [payment("P1", "INV-999", "C1", 1000, day=20)],
        config,
    )

    assert {e.kind for e in result.exceptions} == {
        ExceptionKind.MISSING_PAYMENT,
        ExceptionKind.UNMATCHED_PAYMENT,
    }


def test_several_candidates_are_reported_as_ambiguous_with_no_proposal() -> None:
    exception = only(
        reconcile(
            [invoice("INV-1", "C1", 1000), invoice("INV-2", "C1", 1000, day=3)],
            [payment("P1", "INV-999", "C1", 1000)],
        )
    )

    assert exception.kind is ExceptionKind.AMBIGUOUS_PAYMENT
    assert exception.proposed_invoice_id is None
    assert exception.invoice_ids == ("INV-1", "INV-2")
    assert exception.variance == Money(1000, "USD")  # one invoice stays unpaid either way


def test_invoices_whose_ids_normalize_alike_are_ambiguous_for_a_reference() -> None:
    exception = only(
        reconcile(
            [invoice("INV-1", "C1", 1000), invoice("INV1", "C1", 1000)],
            [payment("P1", "INV-1", "C1", 1000)],
        )
    )

    assert exception.kind is ExceptionKind.AMBIGUOUS_PAYMENT
    assert exception.invoice_ids == ("INV-1", "INV1")


def test_an_unpaid_invoice_and_an_unmatched_payment_are_reported() -> None:
    result = reconcile([invoice("INV-1", "C1", 1000)], [payment("P1", "INV-2", "C1", 777)])

    by_kind = {e.kind: e for e in result.exceptions}
    assert by_kind[ExceptionKind.MISSING_PAYMENT].variance == Money(1000, "USD")
    assert by_kind[ExceptionKind.UNMATCHED_PAYMENT].variance == Money(-777, "USD")


def test_duplicate_record_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="invoice record ids must be unique"):
        reconcile([invoice("INV-1", "C1", 1), invoice("INV-1", "C1", 2)], [])
    with pytest.raises(ValueError, match="payment record ids must be unique"):
        reconcile([], [payment("P1", None, "C1", 1), payment("P1", None, "C1", 2)])


@pytest.mark.parametrize(
    "kwargs",
    [{"tolerance_minor": -1}, {"partial_threshold_bps": 10_001}, {"pairing_window_days": -1}],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must"):
        EngineConfig(**kwargs)


CUSTOMERS = st.sampled_from(["A", "B"])
AMOUNTS = st.sampled_from([100, 200, 300])
CURRENCIES = st.sampled_from(["USD", "EUR"])
DAYS = st.integers(min_value=1, max_value=28)


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[list[NormalizedRecord], list[NormalizedRecord]]:
    invoice_count = draw(st.integers(min_value=0, max_value=6))
    invoices = [
        invoice(f"I{n}", draw(CUSTOMERS), draw(AMOUNTS), draw(DAYS), draw(CURRENCIES))
        for n in range(invoice_count)
    ]
    references = (
        st.sampled_from([*(i.record_id for i in invoices), "X9", None])
        if invoices
        else (st.sampled_from(["X9", None]))
    )
    payment_count = draw(st.integers(min_value=0, max_value=8))
    payments = [
        payment(
            f"P{n}", draw(references), draw(CUSTOMERS), draw(AMOUNTS), draw(DAYS), draw(CURRENCIES)
        )
        for n in range(payment_count)
    ]
    return invoices, payments


def accounted(result: ReconciliationResult) -> tuple[list[str], list[str]]:
    invoice_ids = [m.invoice_id for m in result.matches]
    payment_ids = [p for m in result.matches for p in m.payment_ids]
    for exception in result.exceptions:
        invoice_ids.extend(exception.invoice_ids)
        payment_ids.extend(exception.payment_ids)
    return sorted(invoice_ids), sorted(payment_ids)


@given(scenarios())
def test_every_record_is_accounted_for_exactly_once(
    scenario: tuple[list[NormalizedRecord], list[NormalizedRecord]],
) -> None:
    invoices, payments = scenario

    result = reconcile(invoices, payments)

    assert accounted(result) == (
        sorted(i.record_id for i in invoices),
        sorted(p.record_id for p in payments),
    )


@given(scenarios(), st.randoms(use_true_random=False))
def test_the_result_does_not_depend_on_input_order(
    scenario: tuple[list[NormalizedRecord], list[NormalizedRecord]], shuffler: random.Random
) -> None:
    invoices, payments = scenario
    shuffled_invoices, shuffled_payments = list(invoices), list(payments)
    shuffler.shuffle(shuffled_invoices)
    shuffler.shuffle(shuffled_payments)

    assert reconcile(shuffled_invoices, shuffled_payments) == reconcile(invoices, payments)

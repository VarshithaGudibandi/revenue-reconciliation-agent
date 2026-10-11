import random

from hypothesis import given, settings
from hypothesis import strategies as st

from recon.cases.assign import ACTIVE_STATUSES, CaseView, RecordRef, assign
from recon.cases.signature import exception_signature
from recon.domain.money import Money

STATUSES = ["open", "fix_reported", "accepted", "resolved", "withdrawn"]
RECORDS = [(role, f"{role[0]}{n}") for role in ("invoice", "payment") for n in range(6)]


def case(
    case_id: str, status: str, records: list[RecordRef], rank: int = 0, closed: str | None = None
) -> CaseView:
    return CaseView(case_id, status, closed, rank, frozenset(records))


def refs(*records: RecordRef) -> frozenset[RecordRef]:
    return frozenset(records)


INV1: RecordRef = ("invoice", "INV-1")
INV2: RecordRef = ("invoice", "INV-2")
PAY1: RecordRef = ("payment", "PAY-1")


def test_an_exception_with_no_overlap_gets_a_new_case() -> None:
    result = assign([case("c1", "open", [INV1])], [refs(INV2)])

    assert (result.case_for, result.new, result.unassigned) == ({}, (0,), ("c1",))


def test_a_changed_problem_keeps_its_case() -> None:
    # An unpaid invoice that receives a payment is the same problem, now with a payment.
    result = assign([case("c1", "open", [INV1])], [refs(INV1, PAY1)])

    assert result.case_for == {0: "c1"}
    assert result.new == () and result.unassigned == ()


def test_a_case_with_no_exception_is_reported_as_unassigned() -> None:
    result = assign([case("c1", "open", [INV1]), case("c2", "open", [INV2])], [refs(INV1)])

    assert result.case_for == {0: "c1"}
    assert result.unassigned == ("c2",)


def test_closed_cases_are_not_reported_as_unassigned() -> None:
    cases = [
        case("c1", "resolved", [INV1]),
        case("c2", "withdrawn", [INV2], closed="records_removed"),
    ]

    assert assign(cases, []).unassigned == ()


def test_a_problem_that_returns_reopens_the_closed_case() -> None:
    result = assign([case("c1", "resolved", [INV1])], [refs(INV1)])

    assert result.case_for == {0: "c1"}


def test_an_active_case_beats_a_closed_one() -> None:
    cases = [case("old", "resolved", [INV1, PAY1], rank=0), case("live", "open", [INV1], rank=1)]

    assert assign(cases, [refs(INV1, PAY1)]).case_for == {0: "live"}


def test_more_shared_records_beat_fewer_then_age_breaks_ties() -> None:
    cases = [case("a", "open", [INV1], rank=0), case("b", "open", [INV2, PAY1], rank=1)]

    assert assign(cases, [refs(INV2, PAY1)]).case_for == {0: "b"}
    assert assign(cases, [refs(INV1, INV2)]).case_for == {0: "a"}


def test_a_merged_case_is_never_reused() -> None:
    cases = [case("gone", "withdrawn", [INV1], closed="merged")]

    assert assign(cases, [refs(INV1)]).new == (0,)


def test_two_exceptions_cannot_share_one_case() -> None:
    result = assign([case("c1", "open", [INV1, INV2])], [refs(INV1), refs(INV2)])

    assert result.case_for == {0: "c1"}
    assert result.new == (1,)


def test_a_signature_changes_with_content_variance_and_kind() -> None:
    records = [("invoice", "INV-1", "h1"), ("payment", "PAY-1", "h2")]
    base = exception_signature("amount_mismatch", Money(50, "USD"), records)

    assert base == exception_signature("amount_mismatch", Money(50, "USD"), reversed(records))
    assert base != exception_signature("partial_payment", Money(50, "USD"), records)
    assert base != exception_signature("amount_mismatch", Money(51, "USD"), records)
    assert base != exception_signature("amount_mismatch", None, records)
    assert base != exception_signature(
        "amount_mismatch",
        Money(50, "USD"),
        [("invoice", "INV-1", "h1"), ("payment", "PAY-1", "h3")],
    )


@st.composite
def worlds(draw: st.DrawFn) -> tuple[list[CaseView], list[frozenset[RecordRef]]]:
    record_sets = st.frozensets(st.sampled_from(RECORDS), min_size=1, max_size=3)
    cases = [
        CaseView(
            f"c{n}",
            draw(st.sampled_from(STATUSES)),
            "merged" if draw(st.booleans()) else "records_removed",
            n,
            draw(record_sets),
        )
        for n in range(draw(st.integers(min_value=0, max_value=6)))
    ]
    cases = [
        c
        if c.status == "withdrawn"
        else CaseView(c.case_id, c.status, None, c.created_rank, c.records)
        for c in cases
    ]
    exceptions = draw(st.lists(record_sets, max_size=6))
    return cases, exceptions


@settings(max_examples=300)
@given(worlds())
def test_assignment_invariants(world: tuple[list[CaseView], list[frozenset[RecordRef]]]) -> None:
    cases, exceptions = world
    by_id = {c.case_id: c for c in cases}

    result = assign(cases, exceptions)

    assert set(result.case_for) | set(result.new) == set(range(len(exceptions)))
    assert not set(result.case_for) & set(result.new)
    assert len(set(result.case_for.values())) == len(result.case_for)
    for index, case_id in result.case_for.items():
        assert by_id[case_id].records & exceptions[index]
        assert not (
            by_id[case_id].status == "withdrawn" and by_id[case_id].closed_reason == "merged"
        )
    taken_by = {case_id: index for index, case_id in result.case_for.items()}
    for index in result.new:
        # A new case is only created when every usable case sharing a record with the
        # exception was already taken by an earlier exception.
        for c in cases:
            usable = not (c.status == "withdrawn" and c.closed_reason == "merged")
            if usable and c.records & exceptions[index]:
                assert c.case_id in taken_by and taken_by[c.case_id] < index
    assert set(result.unassigned) == {
        c.case_id
        for c in cases
        if c.status in ACTIVE_STATUSES and c.case_id not in result.case_for.values()
    }


@settings(max_examples=200)
@given(worlds(), st.randoms(use_true_random=False))
def test_assignment_does_not_depend_on_the_order_the_cases_are_listed(
    world: tuple[list[CaseView], list[frozenset[RecordRef]]], rng: random.Random
) -> None:
    cases, exceptions = world
    shuffled = list(cases)
    rng.shuffle(shuffled)

    assert assign(shuffled, exceptions) == assign(cases, exceptions)

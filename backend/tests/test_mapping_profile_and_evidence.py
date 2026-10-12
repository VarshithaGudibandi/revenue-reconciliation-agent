import json

import pytest

from recon.ingestion.normalize import MappingError
from recon.mapping.evidence import (
    amount_agreement,
    containment,
    date_lag_plausibility,
    lexicon_score,
    linked_rows,
    parsed_amounts,
    reference_hits,
)
from recon.mapping.profile import profile_csv_bytes, shape_of


def csv_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def test_shapes_hide_the_values() -> None:
    assert shape_of("INV-000123") == "A+-9+"
    assert shape_of("inv 7") == "a+ 9"
    assert shape_of("12.50") == "9+.9+"
    assert shape_of("") == ""


def test_a_column_is_described_by_what_its_values_can_be() -> None:
    profile = profile_csv_bytes(
        "f.csv",
        csv_bytes(
            "id,day,amount,ccy",
            "A-1,2026-01-05,10.50,usd",
            "A-2,2026-01-06,1200.00,EUR",
            "A-3,2026-01-07,3.25,USD",
        ),
    )

    ids, day, amount, ccy = (profile.column(h) for h in ("id", "day", "amount", "ccy"))
    assert ids.unique_share == 1.0
    assert day.date_formats() == ("%Y-%m-%d",)
    assert amount.amount_like()
    assert amount.amount_separators() == (".",)
    assert ccy.currency_like()
    assert not ids.date_like()
    assert not ids.amount_like()
    assert not day.amount_like()


def test_whole_numbers_are_not_treated_as_amounts() -> None:
    profile = profile_csv_bytes("f.csv", csv_bytes("n", "10001", "10002", "10003"))

    assert not profile.column("n").amount_like()


def test_a_decimal_comma_is_told_from_a_decimal_point() -> None:
    profile = profile_csv_bytes(
        "f.csv", csv_bytes("dot,comma", '"1,200.50","1.200,50"', '"3.25","3,25"')
    )

    assert profile.column("dot").amount_separators() == (".",)
    assert profile.column("comma").amount_separators() == (",",)


def test_a_date_that_could_be_either_order_fits_both_formats() -> None:
    profile = profile_csv_bytes("f.csv", csv_bytes("d", "03/04/2026", "05/06/2026", "11/12/2026"))

    assert set(profile.column("d").date_formats()) == {"%d/%m/%Y", "%m/%d/%Y"}


def test_a_day_above_twelve_settles_the_order() -> None:
    profile = profile_csv_bytes("f.csv", csv_bytes("d", "03/04/2026", "25/06/2026"))

    assert profile.column("d").date_formats() == ("%d/%m/%Y",)


def test_implausible_years_are_not_dates() -> None:
    profile = profile_csv_bytes("f.csv", csv_bytes("n", "12345678", "12345679"))

    assert not profile.column("n").date_like()


def test_the_description_sent_outward_contains_no_cell_values() -> None:
    profile = profile_csv_bytes(
        "f.csv", csv_bytes("customer,total", "ACME-CORP,1500.00", "GLOBEX-LTD,99.50")
    )

    text = json.dumps(profile.describe())

    for secret in ("ACME-CORP", "GLOBEX-LTD", "1500.00", "99.50"):
        assert secret not in text
    assert "A+-A+" in text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"a,a\n1,2\n", "distinct"),
        (b",\n1,2\n", "distinct"),
        (b"\xff\xfe\x00bad", "UTF-8"),
    ],
)
def test_an_unusable_file_is_refused(data: bytes, message: str) -> None:
    with pytest.raises(MappingError, match=message):
        profile_csv_bytes("f.csv", data)


def test_a_byte_order_mark_and_short_rows_are_tolerated() -> None:
    profile = profile_csv_bytes("f.csv", b"\xef\xbb\xbfa,b\n1,2\n3\n")

    assert profile.headers() == ("a", "b")
    assert profile.rows == 2
    assert profile.column("b").empty_share == 0.5


def test_containment_uses_exact_strings() -> None:
    assert containment(["a", "b", "c"], ["b", "c", "d", "e"]) == pytest.approx(2 / 3)
    assert containment(["a"], ["A"]) == 0.0
    assert containment([], ["a"]) == 0.0


def test_references_are_matched_after_normalizing() -> None:
    ids = ["INV-001", "INV-002", "INV-003"]

    assert reference_hits(["inv 001", " INV002 ", "unknown", ""], ids) == pytest.approx(2 / 3)
    assert reference_hits(["x"], []) == 0.0


def test_the_amount_column_that_matches_payments_scores_high_and_net_scores_low() -> None:
    payments = [("USD", 12000), ("USD", 5000), ("EUR", 700)]
    gross = [("USD", 12000), ("USD", 5000), ("EUR", 700), ("USD", 999)]
    net = [("USD", 10000), ("USD", 4167), ("EUR", 584)]

    assert amount_agreement(gross, payments) == 1.0
    assert amount_agreement(net, payments) == 0.0
    assert amount_agreement([("EUR", 12000)], [("USD", 12000)]) == 0.0
    assert amount_agreement([], payments) == 0.0


def test_amounts_are_read_with_their_row_currency_and_unreadable_rows_are_skipped() -> None:
    profile = profile_csv_bytes("f.csv", csv_bytes("amt,cur", "10.50,usd", "oops,usd", "7,EUR"))

    assert parsed_amounts(profile, "amt", "cur", ".") == [("USD", 1050), ("EUR", 700)]


def test_the_issue_date_follows_payments_and_the_due_date_does_not() -> None:
    invoices = profile_csv_bytes(
        "i.csv",
        csv_bytes(
            "id,issued,due",
            "INV-1,2026-01-05,2026-02-04",
            "INV-2,2026-01-10,2026-02-09",
        ),
    )
    payments = profile_csv_bytes(
        "p.csv", csv_bytes("ref,paid", "inv-1,2026-01-20", "inv-2,2026-01-15")
    )
    pairs = linked_rows(invoices, "id", payments, "ref")

    assert pairs == [(0, 0), (1, 1)]
    assert (
        date_lag_plausibility(
            invoices, ("issued", "%Y-%m-%d"), payments, ("paid", "%Y-%m-%d"), pairs
        )
        == 1.0
    )
    assert (
        date_lag_plausibility(invoices, ("due", "%Y-%m-%d"), payments, ("paid", "%Y-%m-%d"), pairs)
        == 0.0
    )
    assert (
        date_lag_plausibility(invoices, ("issued", "%Y-%m-%d"), payments, ("paid", "%Y-%m-%d"), [])
        is None
    )


def test_header_words_point_towards_and_away() -> None:
    assert lexicon_score("Rechnungsdatum", ["datum"], ["fällig"]) == 2
    assert lexicon_score("Fälligkeitsdatum", ["datum"], ["fällig"]) == -1
    assert lexicon_score("Due Date", ["date"], ["due"]) == -1
    assert lexicon_score("Total", ["date"], ["due"]) == 0

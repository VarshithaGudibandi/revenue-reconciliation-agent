import pytest
from hypothesis import given
from hypothesis import strategies as st

from recon.domain.money import (
    CURRENCY_EXPONENTS,
    AmountParseError,
    CurrencyMismatchError,
    Money,
    UnknownCurrencyError,
    format_amount,
    parse_amount,
)


@pytest.mark.parametrize(
    ("text", "currency", "separator", "expected"),
    [
        ("1234.50", "USD", ".", 123_450),
        ("1,234.50", "USD", ".", 123_450),
        ("1,234,567.89", "USD", ".", 123_456_789),
        ("0.5", "USD", ".", 50),
        (".50", "USD", ".", 50),
        ("5", "USD", ".", 500),
        ("  12.30  ", "USD", ".", 1_230),
        ("12.500", "USD", ".", 1_250),
        ("12.5000", "EUR", ".", 1_250),
        ("(1,234.50)", "USD", ".", -123_450),
        ("-5.25", "USD", ".", -525),
        ("5.25-", "USD", ".", -525),
        ("+5.25", "USD", ".", 525),
        ("1.234,50", "EUR", ",", 123_450),
        ("1 234,50", "EUR", ",", 123_450),
        ("1 234,50", "EUR", ",", 123_450),
        ("1'234.50", "USD", ".", 123_450),
        ("1,234", "JPY", ".", 1_234),
        ("1,234.0", "JPY", ".", 1_234),
        ("1.234", "KWD", ".", 1_234),
        ("0", "USD", ".", 0),
        ("-0.00", "USD", ".", 0),
    ],
)
def test_parses_amounts(text: str, currency: str, separator: str, expected: int) -> None:
    assert parse_amount(text, currency, decimal_separator=separator) == expected


@pytest.mark.parametrize(
    ("text", "currency", "separator"),
    [
        ("", "USD", "."),
        ("   ", "USD", "."),
        ("abc", "USD", "."),
        ("$5.00", "USD", "."),
        ("5.00 USD", "USD", "."),
        ("12.345", "USD", "."),
        ("1,234", "USD", ","),  # 1.234 with a decimal comma: more precision than USD allows
        ("12.5", "JPY", "."),
        ("1.2345", "KWD", "."),
        ("1,23", "USD", "."),
        ("12,34,567", "USD", "."),
        ("1,234,5", "USD", "."),
        ("1.2.3", "USD", "."),
        ("1,234.56.78", "USD", "."),
        ("1,234 567", "USD", "."),
        ("(5.00", "USD", "."),
        ("--5", "USD", "."),
        ("(-5.00)", "USD", "."),
        ("5.", "USD", "."),
        ("-", "USD", "."),
        ("1٣", "USD", "."),
        ("1_000", "USD", "."),
    ],
)
def test_rejects_amounts_it_cannot_read_exactly(text: str, currency: str, separator: str) -> None:
    with pytest.raises(AmountParseError):
        parse_amount(text, currency, decimal_separator=separator)


def test_rejects_unknown_currency() -> None:
    with pytest.raises(UnknownCurrencyError):
        parse_amount("1.00", "XXX")
    with pytest.raises(UnknownCurrencyError):
        Money(100, "usd")


def test_rejects_invalid_decimal_separator() -> None:
    with pytest.raises(ValueError, match="decimal_separator"):
        parse_amount("1.00", "USD", decimal_separator=";")


def test_money_arithmetic_is_exact() -> None:
    tenth, fifth = Money(10, "USD"), Money(20, "USD")

    assert tenth + fifth == Money(30, "USD")
    assert fifth - tenth == tenth
    assert -tenth == Money(-10, "USD")
    assert sum([Money(1, "USD")] * 1000, Money(0, "USD")) == Money(1000, "USD")


def test_money_refuses_to_mix_currencies() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") - Money(1, "EUR")


def test_money_formats_with_its_currency() -> None:
    assert str(Money(123_456, "USD")) == "1234.56 USD"
    assert str(Money(-5, "JPY")) == "-5 JPY"


@pytest.mark.parametrize(
    ("minor", "currency", "thousands", "separator", "expected"),
    [
        (123_456, "USD", False, ".", "1234.56"),
        (123_456, "USD", True, ".", "1,234.56"),
        (123_456, "EUR", True, ",", "1.234,56"),
        (5, "USD", False, ".", "0.05"),
        (-5, "USD", False, ".", "-0.05"),
        (1_234, "JPY", True, ".", "1,234"),
        (1_234, "KWD", False, ".", "1.234"),
    ],
)
def test_formats_amounts(
    minor: int, currency: str, thousands: bool, separator: str, expected: str
) -> None:
    assert (
        format_amount(minor, currency, thousands=thousands, decimal_separator=separator) == expected
    )


@given(
    minor=st.integers(min_value=-(10**15), max_value=10**15),
    currency=st.sampled_from(sorted(CURRENCY_EXPONENTS)),
    thousands=st.booleans(),
    separator=st.sampled_from([".", ","]),
)
def test_parsing_inverts_formatting(
    minor: int, currency: str, thousands: bool, separator: str
) -> None:
    text = format_amount(minor, currency, thousands=thousands, decimal_separator=separator)

    assert parse_amount(text, currency, decimal_separator=separator) == minor


@given(
    left=st.integers(min_value=-(10**12), max_value=10**12),
    right=st.integers(min_value=-(10**12), max_value=10**12),
)
def test_money_addition_matches_integer_addition(left: int, right: int) -> None:
    assert (Money(left, "USD") + Money(right, "USD")).minor == left + right
    assert (Money(left, "USD") - Money(right, "USD")).minor == left - right

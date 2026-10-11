"""Exact money arithmetic on integer minor units.

Amounts are integers in the currency's minor unit (cents for USD). Parsing never
guesses: the decimal separator is stated by the caller, grouping must be valid,
and a value with more precision than the currency allows is rejected, not rounded.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

# Minor-unit exponents (ISO 4217). Currencies missing from this table are rejected
# instead of being assumed to have two decimal places.
CURRENCY_EXPONENTS: Final = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "USD",
                "EUR",
                "GBP",
                "CAD",
                "AUD",
                "NZD",
                "CHF",
                "CNY",
                "HKD",
                "SGD",
                "INR",
                "MXN",
                "BRL",
                "ZAR",
                "SEK",
                "NOK",
                "DKK",
                "PLN",
                "CZK",
                "HUF",
                "ILS",
                "TRY",
                "THB",
                "MYR",
                "PHP",
                "IDR",
                "AED",
                "SAR",
                "EGP",
                "NGN",
            ),
            2,
        ),  # fmt: skip
        **dict.fromkeys(("JPY", "KRW", "VND", "CLP", "ISK", "UGX", "PYG", "RWF", "XAF", "XOF"), 0),
        **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
    }
)

_THOUSANDS_SEPARATORS: Final = frozenset(",. '  ")
_DIGITS: Final = re.compile(r"[0-9]+")


class MoneyError(ValueError):
    """Base class for money errors."""


class UnknownCurrencyError(MoneyError):
    pass


class CurrencyMismatchError(MoneyError):
    pass


class AmountParseError(MoneyError):
    pass


def exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise UnknownCurrencyError(f"unknown currency {currency!r}") from None


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        exponent(self.currency)

    def _same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} and {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor, self.currency)

    def __str__(self) -> str:
        return f"{format_amount(self.minor, self.currency)} {self.currency}"


def parse_amount(text: str, currency: str, *, decimal_separator: str = ".") -> int:
    """Parse an amount string into minor units.

    Accepts a leading or trailing minus sign, or parentheses, for negatives. Digits may
    be grouped in threes with any one separator that is not the decimal separator.
    Rejects currency symbols, ambiguous grouping, and non-zero digits beyond the
    currency's precision.
    """
    if decimal_separator not in (".", ","):
        raise ValueError("decimal_separator must be '.' or ','")
    places = exponent(currency)

    body = text.strip()
    if not body:
        raise AmountParseError("empty amount")

    negative = False
    if body.startswith("(") and body.endswith(")"):
        negative, body = True, body[1:-1].strip()
    elif body.startswith("-"):
        negative, body = True, body[1:].strip()
    elif body.endswith("-"):
        negative, body = True, body[:-1].strip()
    elif body.startswith("+"):
        body = body[1:].strip()

    if not body or body[0] in "+-()" or body[-1] in "+-()":
        raise AmountParseError(f"malformed amount {text!r}")

    integer_text, separator, fraction = body.partition(decimal_separator)
    has_fraction = bool(separator)
    if decimal_separator in fraction:
        raise AmountParseError(f"more than one decimal separator in {text!r}")
    if has_fraction and not _DIGITS.fullmatch(fraction):
        raise AmountParseError(f"malformed fraction in {text!r}")

    whole = _parse_integer_part(integer_text, has_fraction, decimal_separator, text)

    if fraction[places:].strip("0"):
        raise AmountParseError(f"{text!r} has more precision than {currency} allows")
    kept = fraction[:places].ljust(places, "0")
    scale: int = 10**places
    minor = whole * scale + (int(kept) if places else 0)
    return -minor if negative else minor


def _parse_integer_part(
    part: str, has_fraction: bool, decimal_separator: str, original: str
) -> int:
    if not part:
        if has_fraction:
            return 0
        raise AmountParseError(f"malformed amount {original!r}")
    if _DIGITS.fullmatch(part):
        return int(part)

    separators = {char for char in part if not ("0" <= char <= "9")}
    if len(separators) != 1:
        raise AmountParseError(f"inconsistent grouping in {original!r}")
    (separator,) = separators
    if separator not in _THOUSANDS_SEPARATORS or separator == decimal_separator:
        raise AmountParseError(f"unexpected character {separator!r} in {original!r}")

    groups = part.split(separator)
    leading, rest = groups[0], groups[1:]
    if not (_DIGITS.fullmatch(leading) and len(leading) <= 3):
        raise AmountParseError(f"invalid grouping in {original!r}")
    if not all(_DIGITS.fullmatch(group) and len(group) == 3 for group in rest):
        raise AmountParseError(f"invalid grouping in {original!r}")
    return int("".join(groups))


def format_amount(
    minor: int, currency: str, *, thousands: bool = False, decimal_separator: str = "."
) -> str:
    """Render minor units as a decimal string, the inverse of ``parse_amount``."""
    places = exponent(currency)
    whole, fraction = divmod(abs(minor), 10**places)
    if thousands:
        group = "," if decimal_separator == "." else "."
        whole_text = f"{whole:,}".replace(",", group)
    else:
        whole_text = str(whole)
    sign = "-" if minor < 0 else ""
    if not places:
        return f"{sign}{whole_text}"
    return f"{sign}{whole_text}{decimal_separator}{fraction:0{places}d}"

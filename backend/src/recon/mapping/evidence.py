"""Numbers that say how well a candidate column fits its field, from the data alone.

Each function answers one question about one or two columns. None of them looks at a
header except ``lexicon_score``, which is deliberately the weakest signal.
"""

import re
from collections.abc import Sequence
from datetime import datetime

from recon.domain.money import MoneyError, parse_amount
from recon.ingestion.normalize import normalize_reference
from recon.mapping.profile import FileProfile


def containment(a: Sequence[str], b: Sequence[str]) -> float:
    """The share of the smaller set of distinct values that also appears in the other.

    Exact strings, because that is how the engine identifies a customer.
    """
    left, right = set(a), set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def reference_hits(payment_values: Sequence[str], invoice_ids: Sequence[str]) -> float:
    """The share of non-empty payment values that name an invoice id, after normalizing."""
    ids = {normalize_reference(v) for v in invoice_ids if v}
    values = [normalize_reference(v) for v in payment_values if v]
    values = [v for v in values if v]
    if not values or not ids:
        return 0.0
    return sum(1 for v in values if v in ids) / len(values)


def parsed_amounts(
    profile: FileProfile, amount: str, currency: str, separator: str
) -> list[tuple[str, int]]:
    """(currency, minor units) for each row whose amount and currency can be read."""
    result: list[tuple[str, int]] = []
    for value, code in zip(profile.cells[amount], profile.cells[currency], strict=True):
        code = code.upper()
        try:
            result.append((code, parse_amount(value, code, decimal_separator=separator)))
        except (MoneyError, ValueError):
            continue
    return result


def amount_agreement(
    invoice_amounts: Sequence[tuple[str, int]], payment_amounts: Sequence[tuple[str, int]]
) -> float:
    """The share of payment amounts that equal some invoice amount in the same currency.

    The right pair of columns scores high because most invoices are paid in full; a net
    amount, a tax amount or a fee scores near zero.
    """
    if not invoice_amounts or not payment_amounts:
        return 0.0
    invoices = set(invoice_amounts)
    return sum(1 for p in payment_amounts if p in invoices) / len(payment_amounts)


def linked_rows(
    invoices: FileProfile, invoice_id: str, payments: FileProfile, reference: str
) -> list[tuple[int, int]]:
    """(invoice row, payment row) for each payment whose reference names an invoice."""
    index: dict[str, int] = {}
    for row, value in enumerate(invoices.cells[invoice_id]):
        key = normalize_reference(value)
        if key:
            index.setdefault(key, row)
    pairs: list[tuple[int, int]] = []
    for row, value in enumerate(payments.cells[reference]):
        invoice_row = index.get(normalize_reference(value))
        if invoice_row is not None:
            pairs.append((invoice_row, row))
    return pairs


def date_lag_plausibility(
    invoices: FileProfile,
    invoice_date: tuple[str, str],
    payments: FileProfile,
    payment_date: tuple[str, str],
    pairs: Sequence[tuple[int, int]],
    max_days: int = 120,
) -> float | None:
    """The share of linked pairs where the payment falls between 0 and ``max_days`` after
    the invoice date. Each date is given as (column, format).

    A payment cannot precede the invoice it pays, so an invoice's due date, which usually
    falls after the payment, scores low and its issue date scores high. None when no pair
    could be read.
    """
    inv_col, inv_fmt = invoice_date
    pay_col, pay_fmt = payment_date
    checked = plausible = 0
    for invoice_row, payment_row in pairs:
        try:
            issued = datetime.strptime(invoices.cells[inv_col][invoice_row], inv_fmt)  # noqa: DTZ007
            paid = datetime.strptime(payments.cells[pay_col][payment_row], pay_fmt)  # noqa: DTZ007
        except ValueError:
            continue
        checked += 1
        if 0 <= (paid.date() - issued.date()).days <= max_days:
            plausible += 1
    return plausible / checked if checked else None


_SPLIT = re.compile(r"[^0-9a-zà-ÿ]+")


def lexicon_score(header: str, positive: Sequence[str], negative: Sequence[str]) -> int:
    """Header words that point towards (+2 each) or away from (-3 each) a field.

    Matched as fragments of the lower-cased header, so ``Rechnungsdatum`` hits ``datum``.
    A weak signal, used only to break ties the data cannot break.
    """
    text = "".join(_SPLIT.split(header.lower()))
    return 2 * sum(1 for word in positive if word in text) - 3 * sum(
        1 for word in negative if word in text
    )

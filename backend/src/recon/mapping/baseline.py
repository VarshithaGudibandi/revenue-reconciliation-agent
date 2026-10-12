"""A mapper that uses no language model: value shapes, cross-file evidence, and header words
last. It says "ask the user" rather than guess, and it is the bar a model has to clear.
"""

from collections.abc import Mapping, Sequence
from itertools import product

from recon.datagen.styles import swapped_day_month
from recon.mapping.evidence import (
    amount_agreement,
    containment,
    date_lag_plausibility,
    lexicon_score,
    linked_rows,
    parsed_amounts,
    reference_hits,
)
from recon.mapping.profile import ColumnProfile, FileProfile
from recon.mapping.proposal import (
    FieldChoice,
    FileProposal,
    MappingProposal,
    ValueChoice,
    field_choice,
    value_choice,
)

MIN_LINK = 0.3
MARGIN = 0.2
LEXICON_MARGIN = 2

INVOICE_ID_WORDS = ("invoice", "inv", "factura", "rechnung", "belnr", "number", "num", "no", "nr")
PAYMENT_ID_WORDS = ("transaction", "txn", "charge", "payment", "buchungs", "pago", "id")
NOT_AN_ID_WORDS = ("ref", "bank", "verwendung", "remit", "customer", "cust", "payer", "kunde")
DATE_WORDS = ("date", "datum", "fecha", "dt", "budat", "created", "issue", "booking", "buchung")
NOT_THE_DATE_WORDS = (
    "due",
    "fällig",
    "fallig",
    "faellig",
    "vencimiento",
    "faedt",
    "value",
    "wertstellung",
    "expir",
)


AMOUNT_WORDS = (
    "total",
    "gross",
    "amount",
    "brutto",
    "bruto",
    "importe",
    "wrbtr",
    "amt",
    "betrag",
    "credit",
)
NOT_THE_AMOUNT_WORDS = (
    "net",
    "netto",
    "tax",
    "vat",
    "iva",
    "mwst",
    "fee",
    "base",
    "imponible",
    "excl",
)
CUSTOMER_WORDS = (
    "customer",
    "cust",
    "cliente",
    "client",
    "kunde",
    "kunnr",
    "payer",
    "auftraggeber",
    "account",
    "buyer",
)
REFERENCE_WORDS = (
    "ref",
    "reference",
    "remit",
    "verwendung",
    "description",
    "memo",
    "invoice",
    "narrative",
)


def _ranked(scores: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _choose(
    scores: Mapping[str, float], *, minimum: float, margin: float, reason: str
) -> FieldChoice:
    """The highest scoring column, confident only when it clearly beats the next one."""
    ranked = [(c, s) for c, s in _ranked(scores) if s >= minimum]
    if not ranked:
        return field_choice(alternatives=tuple(scores))
    best, best_score = ranked[0]
    others = tuple(c for c, _ in ranked[1:])
    clear = len(ranked) == 1 or best_score - ranked[1][1] >= margin
    return field_choice(
        column=best,
        confident=clear,
        alternatives=others,
        reasons=(f"{reason}: {best_score:.2f}",),
    )


def _currency(profile: FileProfile) -> FieldChoice:
    found = [c.header for c in profile.columns if c.currency_like()]
    if len(found) == 1:
        return field_choice(found[0], True, reasons=("values are currency codes",))
    return field_choice(alternatives=tuple(found))


def _separator(profile: FileProfile, currency: str | None) -> ValueChoice:
    votes: dict[str, int] = {".": 0, ",": 0}
    amount_columns = 0
    for column in profile.columns:
        if column.header == currency or not column.amount_like():
            continue
        amount_columns += 1
        separators = column.amount_separators()
        if len(separators) == 1:
            votes[separators[0]] += 1
    if not amount_columns:
        return value_choice()
    if votes["."] and votes[","]:
        return value_choice(alternatives=(".", ","), reasons=("columns disagree",))
    if votes[","]:
        return value_choice(",", True, reasons=("amounts only read with a decimal comma",))
    return value_choice(".", True, reasons=("amounts read with a decimal point",))


def _amount_columns(
    invoices: FileProfile,
    payments: FileProfile,
    currencies: tuple[str | None, str | None],
    separators: tuple[str, str],
) -> tuple[FieldChoice, FieldChoice]:
    inv_cols = [c.header for c in invoices.columns if c.amount_like() and c.header != currencies[0]]
    pay_cols = [c.header for c in payments.columns if c.amount_like() and c.header != currencies[1]]
    if not inv_cols or not pay_cols:
        return field_choice(alternatives=tuple(inv_cols)), field_choice(
            alternatives=tuple(pay_cols)
        )

    agreement: dict[tuple[str, str], float] = {}
    if currencies[0] and currencies[1]:
        for i, p in product(inv_cols, pay_cols):
            agreement[(i, p)] = amount_agreement(
                parsed_amounts(invoices, i, currencies[0], separators[0]),
                parsed_amounts(payments, p, currencies[1], separators[1]),
            )

    def side(columns: list[str], index: int) -> FieldChoice:
        if len(columns) == 1:
            return field_choice(columns[0], True, reasons=("the only amount column",))
        scores = {
            c: max((v for k, v in agreement.items() if k[index] == c), default=0.0) for c in columns
        }
        return _choose(
            scores, minimum=MIN_LINK, margin=MARGIN, reason="amounts equal the other file's"
        )

    return side(inv_cols, 0), side(pay_cols, 1)


def _pool(profile: FileProfile, excluded: set[str]) -> list[ColumnProfile]:
    """Columns left to be an id, a reference or a customer: not a date, an amount or a currency."""
    return [
        c
        for c in profile.columns
        if c.header not in excluded and not (c.date_like() or c.amount_like() or c.currency_like())
    ]


def _link(
    invoices: list[ColumnProfile], payments: list[ColumnProfile]
) -> tuple[FieldChoice, FieldChoice]:
    """The invoice id column and the payment reference column, found together: payments cite
    invoice ids."""
    hits = {
        (i.header, p.header): reference_hits(p.values, i.values)
        for i, p in product(invoices, payments)
        if i.unique_share >= 0.95
    }
    ranked = sorted(hits.items(), key=lambda item: (-item[1], item[0]))
    if ranked and ranked[0][1] >= MIN_LINK:
        (inv, pay), score = ranked[0]
        rival = next(
            (s for (i, p), s in ranked[1:] if i != inv and p != pay and s >= MIN_LINK), 0.0
        )
        confident = score - rival >= MARGIN
        reason = f"{score:.0%} of {pay} values name an invoice in {inv}"
        return field_choice(inv, confident, reasons=(reason,)), field_choice(
            pay, confident, reasons=(reason,)
        )
    weak = ranked[0][1] if ranked else 0.0
    # Nothing in the payments names an invoice: say there is no reference column, unless
    # something weakly resembles one.
    return field_choice(), field_choice(
        None, weak < 0.05, reasons=("no payment value names an invoice",)
    )


def _counterparty(
    invoices: list[ColumnProfile], payments: list[ColumnProfile]
) -> tuple[FieldChoice, FieldChoice]:
    shared = {
        (i.header, p.header): containment(i.values, p.values)
        for i, p in product(invoices, payments)
        if i.distinct >= 2 and p.distinct >= 2 and i.unique_share < 0.95 and p.unique_share < 0.95
    }
    ranked = sorted(shared.items(), key=lambda item: (-item[1], item[0]))
    if ranked and ranked[0][1] >= 0.5:
        (inv, pay), score = ranked[0]
        rival = next((s for (i, p), s in ranked[1:] if i != inv and p != pay and s >= 0.5), 0.0)
        reason = f"{score:.0%} of the values are shared between {inv} and {pay}"
        confident = score - rival >= MARGIN
        return field_choice(inv, confident, reasons=(reason,)), field_choice(
            pay, confident, reasons=(reason,)
        )
    return field_choice(), field_choice()


def _record_id(pool: Sequence[ColumnProfile], words: Sequence[str]) -> FieldChoice:
    unique = [c for c in pool if c.unique_share >= 0.99]
    if not unique:
        return field_choice()
    if len(unique) == 1:
        return field_choice(unique[0].header, True, reasons=("the only column of unique values",))
    scores = {c.header: lexicon_score(c.header, words, NOT_AN_ID_WORDS) for c in unique}
    return _choose(scores, minimum=-100, margin=LEXICON_MARGIN, reason="header words")


def _date_format(column: ColumnProfile | None) -> ValueChoice:
    if column is None:
        return value_choice()
    formats = column.date_formats()
    if len(formats) == 1:
        return value_choice(
            formats[0], True, reasons=("every value reads as a date in this format",)
        )
    if len(formats) == 2 and swapped_day_month(formats[0]) == formats[1]:
        return value_choice(
            None,
            False,
            formats,
            ("every value fits both day-month and month-day: the values cannot say which",),
        )
    return value_choice(None, False, formats, ("more than one format fits",))


def _date_format_for(profile: FileProfile, date: FieldChoice) -> ValueChoice:
    """The date format, even when it is not yet known which date column is meant: if every
    candidate column reads in the same single format, the format is settled either way."""
    headers = [h for h in (date.column, *date.alternatives) if h is not None]
    if not headers:
        return value_choice()
    if len(headers) == 1 or date.confident:
        return _date_format(profile.column(headers[0]))
    choices = [_date_format(profile.column(h)) for h in headers]
    first = choices[0]
    if first.confident and all(c.value == first.value and c.confident for c in choices):
        return value_choice(first.value, True, reasons=("every candidate date column uses it",))
    options = tuple(dict.fromkeys(f for h in headers for f in profile.column(h).date_formats()))
    return value_choice(None, False, options, ("the date column is not settled",))


def _dates(
    invoices: FileProfile,
    payments: FileProfile,
    taken: tuple[set[str], set[str]],
    links: list[tuple[int, int]],
) -> tuple[FieldChoice, FieldChoice]:
    inv_cands = [c for c in invoices.columns if c.date_like() and c.header not in taken[0]]
    pay_cands = [c for c in payments.columns if c.date_like() and c.header not in taken[1]]

    lag: dict[tuple[str, str], float] = {}
    for i, p in product(inv_cands, pay_cands):
        score = date_lag_plausibility(
            invoices,
            (i.header, i.date_formats()[0]),
            payments,
            (p.header, p.date_formats()[0]),
            links,
        )
        if score is not None:
            lag[(i.header, p.header)] = score

    def side(cands: list[ColumnProfile], index: int) -> FieldChoice:
        if not cands:
            return field_choice()
        if len(cands) == 1:
            return field_choice(cands[0].header, True, reasons=("the only date column",))
        by_lag = {
            c.header: max((v for k, v in lag.items() if k[index] == c.header), default=0.0)
            for c in cands
        }
        ranked = _ranked(by_lag)
        if lag and ranked[0][1] - ranked[1][1] >= 0.3:
            return field_choice(
                ranked[0][0],
                True,
                alternatives=tuple(c for c, _ in ranked[1:]),
                reasons=("payments fall after this date, not before it",),
            )
        by_words = {
            c.header: float(lexicon_score(c.header, DATE_WORDS, NOT_THE_DATE_WORDS)) for c in cands
        }
        return _choose(by_words, minimum=-100, margin=LEXICON_MARGIN, reason="header words")

    return side(inv_cands, 0), side(pay_cands, 1)


def propose_mapping(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
    inv_cur, pay_cur = _currency(invoices), _currency(payments)
    inv_sep, pay_sep = _separator(invoices, inv_cur.column), _separator(payments, pay_cur.column)
    inv_amount, pay_amount = _amount_columns(
        invoices,
        payments,
        (inv_cur.column, pay_cur.column),
        (inv_sep.value or ".", pay_sep.value or "."),
    )

    inv_pool, pay_pool = _pool(invoices, set()), _pool(payments, set())
    inv_id, pay_ref = _link(inv_pool, pay_pool)
    inv_rest = [c for c in inv_pool if c.header != inv_id.column]
    pay_rest = [c for c in pay_pool if c.header != pay_ref.column]
    inv_party, pay_party = _counterparty(inv_rest, pay_rest)
    if inv_id.column is None:
        inv_id = _record_id(inv_rest, INVOICE_ID_WORDS)
    pay_id = _record_id([c for c in pay_rest if c.header != pay_party.column], PAYMENT_ID_WORDS)

    links = (
        linked_rows(invoices, inv_id.column, payments, pay_ref.column)
        if inv_id.column and pay_ref.column
        else []
    )
    inv_date, pay_date = _dates(invoices, payments, (set(), set()), links)

    return MappingProposal(
        invoices=FileProposal(
            fields={
                "record_id": inv_id,
                "counterparty": inv_party,
                "date": inv_date,
                "amount": inv_amount,
                "currency": inv_cur,
            },
            date_format=_date_format_for(invoices, inv_date),
            decimal_separator=inv_sep,
        ),
        payments=FileProposal(
            fields={
                "record_id": pay_id,
                "counterparty": pay_party,
                "date": pay_date,
                "amount": pay_amount,
                "currency": pay_cur,
                "reference": pay_ref,
            },
            date_format=_date_format_for(payments, pay_date),
            decimal_separator=pay_sep,
        ),
    )


def _by_words(
    columns: Sequence[ColumnProfile],
    positive: Sequence[str],
    negative: Sequence[str],
    *,
    what: str,
) -> FieldChoice:
    """Pick among columns by header words alone. Confident only when one column clearly
    points the right way; with a single candidate there is nothing to compare."""
    if not columns:
        return field_choice()
    if len(columns) == 1:
        return field_choice(columns[0].header, True, reasons=(f"the only {what} column",))
    scores = {c.header: float(lexicon_score(c.header, positive, negative)) for c in columns}
    choice = _choose(scores, minimum=-100, margin=LEXICON_MARGIN, reason="header words")
    if choice.column is not None and scores[choice.column] <= 0:
        return field_choice(None, False, tuple(scores), ("no header word points to this field",))
    return choice


def propose_file(profile: FileProfile, role: str) -> FileProposal:
    """A proposal for one file seen alone, as when the first of two files is uploaded.

    There is nothing to compare against, so apart from the shapes of the values it is the
    header words that decide, and the answer is often a question.
    """
    currency = _currency(profile)
    separator = _separator(profile, currency.column)
    amounts = [c for c in profile.columns if c.amount_like() and c.header != currency.column]
    amount = _by_words(amounts, AMOUNT_WORDS, NOT_THE_AMOUNT_WORDS, what="amount")
    dates = [c for c in profile.columns if c.date_like()]
    date = _by_words(dates, DATE_WORDS, NOT_THE_DATE_WORDS, what="date")

    pool = _pool(profile, set())
    id_words = INVOICE_ID_WORDS if role == "invoices" else PAYMENT_ID_WORDS
    record_id = _record_id(pool, id_words)
    rest = [c for c in pool if c.header != record_id.column]
    varied = [c for c in rest if c.distinct >= 2 and c.unique_share < 0.95]
    counterparty = _by_words(varied, CUSTOMER_WORDS, (), what="customer")

    fields = {
        "record_id": record_id,
        "counterparty": counterparty,
        "date": date,
        "amount": amount,
        "currency": currency,
    }
    if role == "payments":
        left = [c for c in rest if c.header != counterparty.column]
        reference = _by_words(left, REFERENCE_WORDS, (), what="reference")
        fields["reference"] = reference
    return FileProposal(
        fields=fields,
        date_format=_date_format_for(profile, date),
        decimal_separator=separator,
    )

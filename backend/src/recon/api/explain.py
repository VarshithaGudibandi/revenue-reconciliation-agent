"""Plain-language readings of a case, written by rules from its records.

These are deterministic statements about the data. They say what is there and what
to check next; they do not guess at causes the data cannot show.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from recon.domain.money import Money, format_amount


@dataclass(frozen=True)
class RecordFacts:
    role: str
    record_id: str
    customer: str | None
    amount: Money | None


@dataclass(frozen=True)
class Explanation:
    summary: str
    why: str
    next_steps: tuple[str, ...]


def _money(amount: Money | None) -> str:
    if amount is None:
        return "an unknown amount"
    return f"{format_amount(amount.minor, amount.currency, thousands=True)} {amount.currency}"


def _total(records: Sequence[RecordFacts]) -> Money | None:
    amounts = [r.amount for r in records if r.amount is not None]
    if not amounts or len(amounts) != len(records) or len({a.currency for a in amounts}) != 1:
        return None
    return Money(sum(a.minor for a in amounts), amounts[0].currency)


def _ids(records: Sequence[RecordFacts]) -> str:
    return ", ".join(r.record_id for r in records)


def explain(
    kind: str,
    variance: Money | None,
    records: Sequence[RecordFacts],
    proposed_invoice_id: str | None,
    reconciliation_type: str = "invoice_payment",
) -> Explanation:
    invoices = [r for r in records if r.role == "invoice"]
    payments = [r for r in records if r.role == "payment"]
    invoice, payment = (invoices[0] if invoices else None), (payments[0] if payments else None)
    customer = next((r.customer for r in records if r.customer), None)
    who = f"customer {customer}" if customer else "the customer"
    gap = _money(variance if variance is None else Money(abs(variance.minor), variance.currency))

    if kind == "missing_payment" and invoice:
        return Explanation(
            f"Invoice {invoice.record_id} for {_money(invoice.amount)} has no payment.",
            f"No payment from {who} refers to this invoice, and no unmatched payment from them "
            "has the same amount near its date.",
            (
                f"Check whether {who} paid under another reference or in a later export.",
                "If it is genuinely unpaid, follow up with the customer.",
            ),
        )
    if kind == "unmatched_payment" and payment:
        return Explanation(
            f"Payment {payment.record_id} of {_money(payment.amount)} matches no invoice.",
            f"Its reference does not match an invoice from {who}, and no unpaid invoice has "
            "the same amount within the pairing window.",
            (
                "Look for the invoice it pays, which may be missing from the invoice export.",
                "If it is a deposit or refund, note it and accept the variance.",
            ),
        )
    if kind == "amount_mismatch" and invoice:
        paid = _total(payments)
        direction = "short" if variance and variance.minor > 0 else "over"
        return Explanation(
            f"Invoice {invoice.record_id} is paid {gap} {direction}.",
            f"The invoice is {_money(invoice.amount)} and payments total {_money(paid)}.",
            (
                "Check for a fee, discount or credit note that explains the difference.",
                "If the payment amount is wrong in the source, correct it there.",
            ),
        )
    if kind == "partial_payment" and invoice:
        return Explanation(
            f"Invoice {invoice.record_id} is only partly paid: {gap} is still outstanding.",
            f"The invoice is {_money(invoice.amount)} and payments total "
            f"{_money(_total(payments))}.",
            (
                "Check whether the rest is due later or has been paid under another reference.",
                "If it is an instalment plan, note it and accept the variance.",
            ),
        )
    if kind == "duplicate_payment" and invoice:
        return Explanation(
            f"Invoice {invoice.record_id} appears to be paid twice, {gap} too much.",
            f"Payments {_ids(payments)} together exceed the invoice by an amount equal to one "
            "of them.",
            (
                "Confirm the second payment is a duplicate and refund or credit it.",
                "If it is an advance on another invoice, correct its reference.",
            ),
        )
    if kind == "misreferenced_payment" and invoice and payment:
        return Explanation(
            f"Payment {payment.record_id} probably pays invoice {invoice.record_id}, but its "
            "reference says otherwise.",
            f"The amount ({_money(payment.amount)}), customer and timing agree with the invoice, "
            "and the payment's reference matches no invoice. The pairing is a proposal; "
            "it has not been applied.",
            (
                f"Confirm with {who} and correct the reference on payment {payment.record_id} "
                f"to {proposed_invoice_id or invoice.record_id} in the source system.",
            ),
        )
    if reconciliation_type == "payout_deposit":
        batch = _explain_deposits(kind, variance, gap, invoice, payments, invoices, who)
        if batch is not None:
            return batch

    if kind == "currency_mismatch" and invoice:
        return Explanation(
            f"Invoice {invoice.record_id} was paid in a different currency.",
            f"The invoice is {_money(invoice.amount)} and payment {_ids(payments)} is "
            f"{_money(_total(payments))}. There is no exchange rate to compare them.",
            (
                "Convert at the agreed rate and check the amounts agree.",
                "If the customer paid in the wrong currency, arrange a correction.",
            ),
        )
    if kind == "ambiguous_payment":
        return Explanation(
            f"Payment {_ids(payments)} could belong to any of {len(invoices)} invoices.",
            f"Invoices {_ids(invoices)} are unpaid, from {who}, and have the same amount, so "
            "the payment cannot be assigned from the data. No pairing is proposed.",
            (
                "Find out which invoice the customer meant to pay.",
                "Correct the reference on the payment so it names that invoice.",
            ),
        )
    return Explanation(f"{kind.replace('_', ' ').capitalize()}.", "", ())


def _explain_deposits(
    kind: str,
    variance: Money | None,
    gap: str,
    deposit: RecordFacts | None,
    payouts: Sequence[RecordFacts],
    deposits: Sequence[RecordFacts],
    who: str,
) -> Explanation | None:
    """Readings for bank deposits against payouts. The deposit is the record stored as the
    ``invoice`` side and its payouts are the ``payment`` side."""
    if kind == "missing_payouts" and deposit:
        return Explanation(
            f"Deposit {deposit.record_id} of {_money(deposit.amount)} has no payouts that "
            "explain it.",
            "No payout names this deposit, and no set of payouts from the same account paid out in "
            "the ten days before it adds up to its amount.",
            (
                "Check whether the payouts were reported under another account or in a later "
                "export.",
                "If the deposit is not from a processor (a transfer, interest), note it and accept "
                "the variance.",
            ),
        )
    if kind == "undeposited_payout" and payouts:
        payout = payouts[0]
        return Explanation(
            f"Payout {payout.record_id} of {_money(payout.amount)} is in no deposit.",
            "It names no deposit in the bank export, or names one that is not there, and no "
            "deposit is made up of it.",
            (
                "Check whether its deposit falls in a later period or has not arrived yet.",
                "If the payout was cancelled or sent to another account, note it and accept the "
                "variance.",
            ),
        )
    if kind == "deposit_mismatch" and deposit:
        direction = "short of" if variance and variance.minor < 0 else "more than"
        return Explanation(
            f"Deposit {deposit.record_id} is {gap} {direction} its payouts.",
            f"The deposit is {_money(deposit.amount)} and its payouts ({_ids(payouts)}) total "
            f"{_money(_total(payouts))}.",
            (
                "Check for a fee, reserve or adjustment that the processor took or released.",
                "If the processor's report is wrong, correct it at the source.",
            ),
        )
    if kind == "proposed_grouping" and deposit:
        return Explanation(
            f"Deposit {deposit.record_id} probably holds payouts {_ids(payouts)}, but nothing "
            "says so.",
            f"Those payouts from {who} were paid out in the ten days before the deposit and add up "
            "to its amount, and no other deposit could use them. The grouping is a proposal; it "
            "has not been applied.",
            (
                f"Confirm it against the processor's report, then put {deposit.record_id} in the "
                "reference of those payouts at the source.",
            ),
        )
    if kind == "ambiguous_grouping":
        return Explanation(
            f"Deposit {_ids(deposits)} could be made up of the payouts in more than one way.",
            f"Payouts {_ids(payouts)} from {who} can be combined to the same amount in several "
            "ways, so the data cannot say which belong to which deposit. No grouping is proposed.",
            (
                "Use the processor's report to find which payouts each deposit carries.",
                "Put the deposit's id in the reference of its payouts at the source.",
            ),
        )
    if kind == "currency_mismatch" and deposit:
        return Explanation(
            f"Deposit {deposit.record_id} is in a different currency than its payouts.",
            f"The deposit is {_money(deposit.amount)} and payouts {_ids(payouts)} total "
            f"{_money(_total(payouts))}. There is no exchange rate to compare them.",
            (
                "Convert at the rate the bank applied and check the amounts agree.",
                "If the payout currency is wrong in the processor's report, correct it there.",
            ),
        )
    return None

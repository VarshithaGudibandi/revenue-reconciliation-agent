"""A mapper in which the model reads both files and measures them with tools before it answers.

The model sees a description of each file (headers, value shapes, masked examples) and can ask
questions about the data: how many values two columns share, how many payment values name an
invoice, which pair of amount columns agree, whether payments fall after a date. The code only
measures; what the measurements mean is the model's call. Cell values never leave the machine:
a tool returns counts and shares, and examples are masked as in the single-file mapper.

When the model submits, its answer is checked against the data the same way as the single-file
mapper's (``to_proposal``), so a column that cannot hold a field is never taken for it.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product
from typing import Any

from pydantic import BaseModel, ValidationError

from recon.ai.llm.client import LlmError
from recon.ai.llm.tools import Message, Step, Tool, ToolCall, ToolModel, ToolResults
from recon.ai.mapping.evidence import (
    amount_agreement,
    containment,
    date_lag_plausibility,
    linked_rows,
    parsed_amounts,
    reference_hits,
)
from recon.ai.mapping.llm import LlmFileMapping, answer_schema, to_proposal
from recon.ai.mapping.profile import FileProfile, _is_label_column, mask_value
from recon.ai.mapping.proposal import FileProposal, MappingProposal
from recon.files.normalize import normalize_reference

PROMPT_VERSION = "mapping-agent-2"
# Turns the model may take before it must submit. A turn may call several tools at once.
MAX_TURNS = 6
MAX_EXAMPLES = 8

SYSTEM_PROMPT = """\
You map the columns of two files from finance systems, an invoice export and a payment export, \
to the fields a revenue reconciliation needs. You get a description of every column of both \
files: its header, the shapes its values take (digits are 9, letters A or a, repeated \
characters are marked +), which date formats and decimal separators its values read under, how \
unique it is, and a few masked examples. You never see the real values, but you can measure \
them with the tools.

Fields, for each file:
- record_id: the identifier unique per row (invoice number, payment or transaction id). Not a \
customer id.
- counterparty: the customer or payer identifier. It repeats across rows, and the same values \
appear in both files.
- date: for invoices, the issue date, not the due date. For payments, the date the payment was \
received or booked, not the value date. A bank's value date falls on or a day or two after the \
booking date.
- amount: for invoices, the total payable including tax (gross), not the net amount, the tax or \
a balance. For payments, the amount paid, not a fee.
- currency: the column of currency codes.
- reference (payments only): the column that says which invoice a payment is for. "(none)" if \
there is none. For the invoice file, always answer "(none)".
- text (optional): free text about each record, such as what an invoice is for or what the \
payer wrote; mostly several words per value. "(none)" if there is none.
- date_format: if the date column's date_formats lists exactly one format, that is the answer, \
with high confidence. If it lists more than one, such as day-month and month-day, answer \
"unknown" and ask: never guess the order.
- decimal_separator: if the amount column's amount_decimal_separators lists exactly one, that \
is the answer, with high confidence.

How to work. Headers can be in any language or meaningless (Column A), so settle a field from \
measurements whenever more than one column could hold it:
- Payments name invoices. The invoice id and the payment reference are the pair of columns \
where references_to_invoices is high. A column whose values are sentences can mention invoice \
numbers too; the reference is the column that is mostly just the identifier.
- Most invoices are paid in full, so the right amount columns are the pair where \
amount_agreement is high. A net amount, a tax or a fee agrees with almost nothing.
- The customer columns are the pair where shared_values is high, among columns that are not \
unique per row.
- A payment falls after the invoice is issued, and usually before it is due. With several date \
columns, payment_timing tells the issue date (high share) from the due date (low share).
- With two date columns in one file, compare_dates says which is earlier, row by row.
- Use more_examples to tell free text from codes when the shapes leave it open.
Make every measurement you need in your first turn, as several tool calls at once; measure \
again only if a result leaves a field open. Then call submit_mapping once with both files.

Confidence: "high" when the headers and measurements settle it, "medium" when you lean one way \
and list the others in alternatives, "low" when guessing. A confident wrong answer is the worst \
outcome and a question to the user is cheap. Use each column for at most one field. Keep each \
reason to one short sentence and name the measurement it rests on.

Example, from two other files. An invoice file had Rechnungsnr., Kunde, Rechnungsdatum, \
Fälligkeitsdatum, Währung, Nettobetrag, MwSt, Bruttobetrag and Beschreibung; a payment file had \
Buchungs-ID, Buchungstag, Wertstellung, Auftraggeber, Verwendungszweck, Währung, Betrag and \
Buchungstext. references_to_invoices(Verwendungszweck, Rechnungsnr.) was 0.94 and with \
Buchungstext 0, so those are the invoice id and the reference. amount_agreement(Bruttobetrag, \
Betrag) was 0.79 and Nettobetrag against Betrag 0, so the gross amount is Bruttobetrag. \
shared_values(Kunde, Auftraggeber) was 1.0. payment_timing with Rechnungsdatum was 1.0 and with \
Fälligkeitsdatum 0.02, so the issue date is Rechnungsdatum. Both dates read only as %d.%m.%Y, so \
the format is settled; the amounts read with ",".\
"""


class AgentAnswer(BaseModel, frozen=True):
    invoices: LlmFileMapping
    payments: LlmFileMapping


def _column_args(*names: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
        "required": list(names),
    }


def tools_for(invoices: FileProfile, payments: FileProfile) -> list[Tool]:
    file_arg = {"type": "string", "enum": ["invoices", "payments"]}
    return [
        Tool(
            "shared_values",
            "Share of distinct values that a column of the invoice file and a column of the "
            "payment file have in common (of the smaller set). Customer columns score high.",
            _column_args("invoice_column", "payment_column"),
        ),
        Tool(
            "references_to_invoices",
            "Share of non-empty values in a payment column that name a value of an invoice "
            "column, ignoring case, spaces and punctuation. The payment reference against the "
            "invoice id scores high.",
            _column_args("payment_column", "invoice_column"),
        ),
        Tool(
            "amount_agreement",
            "Share of payment amounts that equal some invoice amount in the same currency, for "
            "an amount and a currency column of each file, under each decimal separator the "
            "columns read with.",
            _column_args(
                "invoice_amount", "invoice_currency", "payment_amount", "payment_currency"
            ),
        ),
        Tool(
            "payment_timing",
            "For the payments whose reference names an invoice, the share paid 0 to 120 days "
            "after the invoice date, under each date format the columns read with. The issue "
            "date scores high and a due date low.",
            _column_args("invoice_id", "payment_reference", "invoice_date", "payment_date"),
        ),
        Tool(
            "compare_dates",
            "For two date columns of the same file, the share of rows where the first is "
            "earlier than, the same as, or later than the second, and the typical difference "
            "in days.",
            {
                "type": "object",
                "properties": {
                    "file": file_arg,
                    "first_column": {"type": "string"},
                    "second_column": {"type": "string"},
                },
                "required": ["file", "first_column", "second_column"],
            },
        ),
        Tool(
            "more_examples",
            f"Up to {MAX_EXAMPLES} distinct values of a column, masked as in the description.",
            {
                "type": "object",
                "properties": {"file": file_arg, "column": {"type": "string"}},
                "required": ["file", "column"],
            },
        ),
        Tool(
            "submit_mapping",
            "The final mapping of both files. Call it once, when you are done.",
            {
                "type": "object",
                "properties": {
                    "invoices": answer_schema(invoices.headers()),
                    "payments": answer_schema(payments.headers()),
                },
                "required": ["invoices", "payments"],
            },
        ),
    ]


class _Unknown(ValueError):
    pass


@dataclass
class Measurements:
    """The tools, over one pair of files."""

    invoices: FileProfile
    payments: FileProfile

    def _col(self, profile: FileProfile, header: Any) -> str:
        if not isinstance(header, str) or header not in profile.cells:
            raise _Unknown(
                f"no column {header!r} in the {profile.name} file; its columns are "
                + ", ".join(profile.headers())
            )
        return header

    def shared_values(self, invoice_column: Any, payment_column: Any) -> dict[str, Any]:
        i = self._col(self.invoices, invoice_column)
        p = self._col(self.payments, payment_column)
        return {
            "shared_share": round(containment(self.invoices.cells[i], self.payments.cells[p]), 3)
        }

    def references_to_invoices(self, payment_column: Any, invoice_column: Any) -> dict[str, Any]:
        p = self._col(self.payments, payment_column)
        i = self._col(self.invoices, invoice_column)
        values = self.payments.cells[p]
        return {
            "share_naming_an_invoice": round(reference_hits(values, self.invoices.cells[i]), 3),
            "non_empty_values": sum(1 for v in values if normalize_reference(v)),
        }

    def amount_agreement(
        self,
        invoice_amount: Any,
        invoice_currency: Any,
        payment_amount: Any,
        payment_currency: Any,
    ) -> dict[str, Any]:
        ia = self._col(self.invoices, invoice_amount)
        ic = self._col(self.invoices, invoice_currency)
        pa = self._col(self.payments, payment_amount)
        pc = self._col(self.payments, payment_currency)
        seps_i = self.invoices.column(ia).amount_separators() or (".",)
        seps_p = self.payments.column(pa).amount_separators() or (".",)
        return {
            "by_decimal_separators": [
                {
                    "invoices": si,
                    "payments": sp,
                    "share_equal_to_an_invoice_amount": round(
                        amount_agreement(
                            parsed_amounts(self.invoices, ia, ic, si),
                            parsed_amounts(self.payments, pa, pc, sp),
                        ),
                        3,
                    ),
                }
                for si, sp in product(seps_i, seps_p)
            ]
        }

    def payment_timing(
        self, invoice_id: Any, payment_reference: Any, invoice_date: Any, payment_date: Any
    ) -> dict[str, Any]:
        iid = self._col(self.invoices, invoice_id)
        ref = self._col(self.payments, payment_reference)
        idate = self._col(self.invoices, invoice_date)
        pdate = self._col(self.payments, payment_date)
        pairs = linked_rows(self.invoices, iid, self.payments, ref)
        formats_i = self.invoices.column(idate).date_formats()
        formats_p = self.payments.column(pdate).date_formats()
        if not formats_i or not formats_p:
            return {"linked_payments": len(pairs), "note": "a column does not read as dates"}
        results = []
        for fi, fp in product(formats_i, formats_p):
            share = date_lag_plausibility(
                self.invoices, (idate, fi), self.payments, (pdate, fp), pairs
            )
            results.append(
                {
                    "invoice_format": fi,
                    "payment_format": fp,
                    "share_paid_0_to_120_days_after": None if share is None else round(share, 3),
                }
            )
        return {"linked_payments": len(pairs), "by_date_formats": results}

    def compare_dates(self, file: Any, first_column: Any, second_column: Any) -> dict[str, Any]:
        profile = self.invoices if file == "invoices" else self.payments
        a = self._col(profile, first_column)
        b = self._col(profile, second_column)
        formats_a, formats_b = profile.column(a).date_formats(), profile.column(b).date_formats()
        if not formats_a or not formats_b:
            return {"note": "a column does not read as dates"}
        days: list[int] = []
        for x, y in zip(profile.cells[a], profile.cells[b], strict=True):
            try:
                first = datetime.strptime(x, formats_a[0])  # noqa: DTZ007 - dates, no time
                second = datetime.strptime(y, formats_b[0])  # noqa: DTZ007
            except ValueError:
                continue
            days.append((second.date() - first.date()).days)
        if not days:
            return {"note": "no row has both dates"}
        n = len(days)
        return {
            "rows": n,
            "share_first_earlier": round(sum(1 for d in days if d > 0) / n, 3),
            "share_same_day": round(sum(1 for d in days if d == 0) / n, 3),
            "share_first_later": round(sum(1 for d in days if d < 0) / n, 3),
            "median_days_second_minus_first": sorted(days)[n // 2],
            "formats_used": [formats_a[0], formats_b[0]],
        }

    def more_examples(self, file: Any, column: Any) -> dict[str, Any]:
        profile = self.invoices if file == "invoices" else self.payments
        header = self._col(profile, column)
        col = profile.column(header)
        distinct = list(dict.fromkeys(col.values))
        if _is_label_column(col):
            return {"kind": "codes", "values": sorted(distinct)}
        return {"kind": "masked", "values": [mask_value(v) for v in distinct[:MAX_EXAMPLES]]}

    def run(self, call: ToolCall) -> dict[str, Any]:
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "shared_values": self.shared_values,
            "references_to_invoices": self.references_to_invoices,
            "amount_agreement": self.amount_agreement,
            "payment_timing": self.payment_timing,
            "compare_dates": self.compare_dates,
            "more_examples": self.more_examples,
        }
        tool = tools.get(call.name)
        if tool is None:
            return {"error": f"there is no tool {call.name!r}"}
        try:
            return tool(**call.arguments)
        except TypeError as error:
            return {"error": f"wrong arguments: {error}"}
        except _Unknown as error:
            return {"error": str(error)}


def user_message(invoices: FileProfile, payments: FileProfile) -> str:
    return json.dumps(
        {
            "invoices": invoices.describe(samples=True),
            "payments": payments.describe(samples=True),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )


@dataclass
class AgentMapper:
    """Maps a pair of files with a model that measures them before it answers."""

    model: ToolModel
    validate: bool = True
    max_turns: int = MAX_TURNS
    mappings: int = 0
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def __call__(self, invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        try:
            answer = self._converse(invoices, payments)
        except (LlmError, ValidationError) as error:
            self.errors.append(f"{invoices.name} and {payments.name}: {error}")
            del self.errors[:-50]
            return MappingProposal(invoices=FileProposal(), payments=FileProposal())
        self.mappings += 1
        return MappingProposal(
            invoices=to_proposal(invoices, "invoices", answer.invoices, validate=self.validate),
            payments=to_proposal(payments, "payments", answer.payments, validate=self.validate),
        )

    def _converse(self, invoices: FileProfile, payments: FileProfile) -> AgentAnswer:
        tools = tools_for(invoices, payments)
        submit_only = [t for t in tools if t.name == "submit_mapping"]
        measure = Measurements(invoices, payments)
        messages: list[Message] = [user_message(invoices, payments)]
        for turn in range(self.max_turns):
            # On the last turn, submitting is the only choice left.
            offered = submit_only if turn == self.max_turns - 1 else tools
            step = self.model.step(SYSTEM_PROMPT, messages, offered)
            self._count(step)
            submitted = next((c for c in step.calls if c.name == "submit_mapping"), None)
            if submitted is not None:
                return AgentAnswer.model_validate(submitted.arguments)
            messages += [step, ToolResults(tuple((c, measure.run(c)) for c in step.calls))]
        raise LlmError(f"the model did not submit a mapping in {self.max_turns} turns")

    def _count(self, step: Step) -> None:
        self.turns += 1
        self.tool_calls += sum(1 for c in step.calls if c.name != "submit_mapping")
        self.input_tokens += step.input_tokens
        self.output_tokens += step.output_tokens

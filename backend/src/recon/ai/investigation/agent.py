"""The investigation: a model reads a case, looks around the period with tools, and says why.

The tools only read. They look up a customer's records, find records of any customer by amount,
find open invoices that add up to an amount, and do arithmetic. What the evidence means is the
model's call. Its finding names a cause from a fixed list for the kind of case, the records it
rests on, and a proposed action; ``verify`` then checks it against what the tools showed. A
finding that fails a check is not shown to anyone, and nothing is ever done without a person's
approval (``graph``).
"""

import ast
import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from recon.ai.investigation.ledger import Ledger, OpenCase
from recon.ai.llm.client import LlmError
from recon.ai.llm.tools import Message, Step, Tool, ToolCall, ToolModel, ToolResults
from recon.datagen.causes import CAUSES

PROMPT_VERSION = "investigation-1"
MAX_TURNS = 8
OTHER = "other"
ACTIONS = ("accept_variance", "record_finding")

SYSTEM_PROMPT = """\
You investigate one open case found while reconciling a month of invoices against payments, and \
say why it arose and what to do about it. The engine has already said what is wrong (the kind of \
case and the difference); your job is the cause. You see the case's records under aliases: I1, \
I2 are invoices, P1, P2 payments, C1, C2 customers. Use the tools to look at the rest of the \
month before you decide, and use calculate for every sum, difference or percentage: never work \
a number out in your head.

What usually lies behind each kind of case:
- amount_mismatch (paid a little more or less than the invoice). processor_fee: a card processor \
kept a percentage of the amount plus a small fixed fee, typically 2% to 3.5% plus a few tenths. \
early_payment_discount: the customer took a round percentage off, such as 1%, 2% or 2.5%. \
bank_charge: a bank kept a round fixed sum on a transfer, such as 10, 15, 20 or 25, whatever the \
amount. keying_error: the amount was typed wrong, for example two digits swapped, so it differs \
by a small amount that is no percentage or round sum.
- missing_payment (an invoice with no payment). paid_by_related_account: a payment of exactly \
the invoice's amount came from another customer (a parent or sister company), and it is itself \
an open unmatched payment. paid_in_lump_sum: one open payment from the same customer covers this \
invoice and one or two other open invoices together. not_paid: nothing in the month accounts \
for it.
- unmatched_payment (a payment that settles nothing). paid_by_related_account: it has exactly the \
amount of an open invoice of another customer with no payment. paid_in_lump_sum: its amount is \
the sum of two or three open invoices of the same customer. unidentified: nothing explains it.
A memo can help, but it can be empty or wrong; the amounts are the evidence.

Then call submit_finding once. The cause must be one of the causes listed for the kind, or \
"other" if none fits. Evidence lists the aliases the cause rests on, including the records outside \
the case that you found. Actions: accept_variance only for an amount_mismatch whose difference is \
a cost the business bears (a processor fee, a discount the customer was entitled to, a bank \
charge); record_finding otherwise. The note says in one or two sentences what to do, citing \
aliases and figures that the tools showed. Confidence "high" only when the numbers settle it.\
"""

_OPERATORS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def calculate(expression: str) -> float:
    """Evaluate + - * / and parentheses over numbers, and nothing else."""

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](ev(node.operand))
        raise ValueError("only numbers, + - * / and parentheses are allowed")

    if len(expression) > 200:
        raise ValueError("the expression is too long")
    return ev(ast.parse(expression.replace(",", ""), mode="eval"))


def tools_for(case: OpenCase) -> list[Tool]:
    causes = [*CAUSES.get(case.kind, ()), OTHER]
    text = {"type": "string"}
    number = {"type": "number"}
    return [
        Tool(
            "customer_records",
            "Every invoice and payment of a customer this month, with each record's state.",
            {"type": "object", "properties": {"customer": text}, "required": ["customer"]},
        ),
        Tool(
            "find_by_amount",
            "Invoices or payments of any customer whose amount is between low and high, in a "
            "currency, with each record's state.",
            {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["invoice", "payment"]},
                    "low": number,
                    "high": number,
                    "currency": text,
                },
                "required": ["role", "low", "high", "currency"],
            },
        ),
        Tool(
            "open_invoices_summing_to",
            "Sets of two or three open invoices of one customer whose amounts add up exactly to "
            "a total.",
            {
                "type": "object",
                "properties": {"customer": text, "total": number, "currency": text},
                "required": ["customer", "total", "currency"],
            },
        ),
        Tool(
            "calculate",
            "Evaluate arithmetic: numbers, + - * / and parentheses.",
            {"type": "object", "properties": {"expression": text}, "required": ["expression"]},
        ),
        Tool(
            "submit_finding",
            "The finding. Call it once, when you are done.",
            {
                "type": "object",
                "properties": {
                    "cause": {"type": "string", "enum": causes},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence": {"type": "array", "items": text},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "note": text,
                },
                "required": ["cause", "confidence", "evidence", "action", "note"],
            },
        ),
    ]


def user_message(ledger: Ledger, case: OpenCase) -> str:
    return json.dumps(
        {
            "case": ledger.show_case(case),
            "month": {
                "invoices": len(ledger.invoices),
                "payments": len(ledger.payments),
                "open_cases": len(ledger.cases),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )


def run_tool(ledger: Ledger, call: ToolCall) -> dict[str, Any]:
    a = call.arguments
    try:
        if call.name == "customer_records":
            return ledger.customer_records(str(a["customer"]))
        if call.name == "find_by_amount":
            return ledger.find(
                str(a["role"]), float(a["low"]), float(a["high"]), str(a["currency"])
            )
        if call.name == "open_invoices_summing_to":
            return ledger.invoice_sums(str(a["customer"]), float(a["total"]), str(a["currency"]))
        if call.name == "calculate":
            value = round(calculate(str(a["expression"])), 6)
            ledger.figures.add(_figure(value))
            return {"result": value}
    except (KeyError, ValueError, TypeError, ZeroDivisionError, SyntaxError) as error:
        return {"error": str(error) or type(error).__name__}
    return {"error": f"there is no tool {call.name!r}"}


@dataclass
class Investigation:
    """What one investigation did: its trace, its finding, and what the checks found."""

    finding: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.finding is not None and not self.problems


def investigate(
    model: ToolModel, ledger: Ledger, case: OpenCase, *, max_turns: int = MAX_TURNS
) -> Investigation:
    result = Investigation()
    tools = tools_for(case)
    submit_only = [t for t in tools if t.name == "submit_finding"]
    messages: list[Message] = [user_message(ledger, case)]
    try:
        for turn in range(max_turns):
            offered = submit_only if turn == max_turns - 1 else tools
            step: Step = model.step(SYSTEM_PROMPT, messages, offered)
            result.turns += 1
            result.input_tokens += step.input_tokens
            result.output_tokens += step.output_tokens
            submitted = next((c for c in step.calls if c.name == "submit_finding"), None)
            if submitted is not None:
                result.finding = dict(submitted.arguments)
                result.problems = verify(result.finding, ledger, case)
                return result
            outputs = tuple((c, run_tool(ledger, c)) for c in step.calls)
            result.trace.extend(
                {"tool": c.name, "arguments": c.arguments, "result": out} for c, out in outputs
            )
            messages += [step, ToolResults(outputs)]
        result.error = f"no finding in {max_turns} turns"
    except LlmError as error:
        result.error = str(error)
    return result


_ALIAS = re.compile(r"\b[IPC]\d+\b")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _figure(value: float) -> str:
    return f"{value:.2f}"


def verify(finding: dict[str, Any], ledger: Ledger, case: OpenCase) -> list[str]:
    """What is wrong with a finding, judged only by what the tools showed. Empty when it holds."""
    problems: list[str] = []
    if finding.get("cause") not in (*CAUSES.get(case.kind, ()), OTHER):
        problems.append(f"the cause {finding.get('cause')!r} is not one for a {case.kind}")
    if finding.get("action") not in ACTIONS:
        problems.append(f"there is no action {finding.get('action')!r}")
    elif finding["action"] == "accept_variance" and case.kind != "amount_mismatch":
        problems.append("only an amount mismatch can be accepted")
    evidence = finding.get("evidence") or []
    note = str(finding.get("note") or "")
    if not evidence:
        problems.append("it cites no evidence")
    for alias in [*evidence, *_ALIAS.findall(note)]:
        if ledger.real(str(alias)) is None:
            problems.append(f"it cites {alias}, which no tool showed")
    shown = {_figure(float(f)) for f in ledger.figures}
    for token in _NUMBER.findall(note):
        value = float(token.replace(",", ""))
        if value <= 100 or _figure(value) in shown:
            continue  # small numbers are percentages, days and counts
        problems.append(f"it states {token}, which no tool showed")
    return list(dict.fromkeys(problems))

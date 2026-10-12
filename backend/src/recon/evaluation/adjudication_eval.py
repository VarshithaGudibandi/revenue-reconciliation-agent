"""Run an adjudicator over the ambiguous payments of generated datasets and report how it does.

    python -m recon.evaluation.adjudication_eval --adjudicator rules

Every case is scored against the invoice the payer really meant, and the results are broken
down by the kind of memo the payer wrote. A confident wrong answer is the failure that matters.
"""

import argparse
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from recon.adjudication.case import AdjudicationCase, Candidate, Ruling
from recon.adjudication.combine import combine_rulings
from recon.adjudication.llm import LlmAdjudicator
from recon.adjudication.rules import adjudicate_by_rules
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.model import AnomalyKind, Dataset
from recon.datagen.text import MEMO_KINDS
from recon.evaluation.llm_cli import add_llm_arguments, cost_line, json_client

Adjudicator = Callable[[AdjudicationCase], Ruling]
OUTCOMES = ("correct", "wrong", "suggested_right", "suggested_wrong", "abstained")


@dataclass(frozen=True)
class LabelledCase:
    case: AdjudicationCase
    intended: str
    memo_kind: str


def cases_from_dataset(dataset: Dataset) -> list[LabelledCase]:
    """One case per ambiguous payment, with the candidates labelled A, B, C in a shuffled order
    so that position says nothing about which one was meant."""
    invoices = {i.invoice_id: i for i in dataset.invoices}
    payments = {p.payment_id: p for p in dataset.payments}
    cases = []
    for anomaly in dataset.truth.anomalies:
        if anomaly.kind is not AnomalyKind.AMBIGUOUS_PAYMENT:
            continue
        payment = payments[anomaly.payment_ids[0]]
        order = list(anomaly.invoice_ids)
        random.Random(f"{dataset.seed}:{anomaly.anomaly_id}").shuffle(order)  # noqa: S311
        candidates = tuple(
            Candidate(
                label="ABCDEFGH"[index],
                invoice_id=invoice_id,
                description=invoices[invoice_id].description,
                days_before=(payment.received_on - invoices[invoice_id].issue_date).days,
            )
            for index, invoice_id in enumerate(order)
        )
        intended = next(c.label for c in candidates if c.invoice_id == anomaly.intended_invoice_id)
        cases.append(
            LabelledCase(
                AdjudicationCase(payment.payment_id, payment.memo, candidates),
                intended,
                anomaly.memo_kind or "none",
            )
        )
    return cases


def judge(ruling: Ruling, intended: str) -> str:
    if ruling.choice is None:
        return "abstained"
    right = ruling.choice == intended
    if ruling.confident:
        return "correct" if right else "wrong"
    return "suggested_right" if right else "suggested_wrong"


def run_evaluation(
    adjudicator: Adjudicator, *, seeds: Sequence[int], size: int = 200
) -> dict[str, Counter[str]]:
    """Outcome counts for each kind of memo."""
    results: dict[str, Counter[str]] = {kind: Counter() for kind in MEMO_KINDS}
    for seed in seeds:
        dataset = generate_dataset(Config(seed=seed, invoices=size, ambiguous_rate=0.06))
        for labelled in cases_from_dataset(dataset):
            outcome = judge(adjudicator(labelled.case), labelled.intended)
            results[labelled.memo_kind][outcome] += 1
    return results


def format_report(results: dict[str, Counter[str]]) -> str:
    header = f"{'memo':<12}{'cases':>6}" + "".join(f"{o:>17}" for o in OUTCOMES)
    lines = [header]
    total: Counter[str] = Counter()
    for kind, counts in results.items():
        total.update(counts)
        lines.append(_row(kind, counts))
    lines.append(_row("all", total))
    return "\n".join(lines)


def _row(label: str, counts: Counter[str]) -> str:
    n = sum(counts.values())
    cells = "".join(
        f"{counts[o]:>10} ({counts[o] / n:>4.0%})" if n else f"{'':>17}" for o in OUTCOMES
    )
    return f"{label:<12}{n:>6}{cells}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.adjudication_eval")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--invoices", type=int, default=200)
    parser.add_argument(
        "--adjudicator",
        choices=["rules", "llm", "llm-unchecked", "hybrid"],
        default="rules",
        help="llm-unchecked does not check that the model's quote is in the memo",
    )
    add_llm_arguments(parser)
    args = parser.parse_args()

    llm: LlmAdjudicator | None = None
    adjudicator: Adjudicator = adjudicate_by_rules
    if args.adjudicator != "rules":
        llm = LlmAdjudicator(json_client(args), check=args.adjudicator != "llm-unchecked")
        model_ruling = llm.adjudicate
        adjudicator = (
            (lambda case: combine_rulings(adjudicate_by_rules(case), model_ruling(case)))
            if args.adjudicator == "hybrid"
            else model_ruling
        )
    print(format_report(run_evaluation(adjudicator, seeds=range(args.seeds), size=args.invoices)))
    if llm is not None:
        print(f"\nmodel calls: {llm.calls}, errors: {len(llm.errors)}")
        for message in llm.errors[:5]:
            print(f"  {message}")
        line = cost_line(args.model, llm.input_tokens, llm.output_tokens)
        if line and llm.calls:
            print(line)


if __name__ == "__main__":
    main()

"""Ask a model to explain the cases the engine finds, and count how many explanations survive
the checks in ``recon.explanation.verify``.

    python -m recon.evaluation.explanation_eval --seeds 2 --per-kind 3

A rejected explanation is one the application would not show: it cited a record that does not
exist, stated a figure the engine did not compute, or wrote a real id. The rejection rate is the
share of answers that were unfaithful in a way a program can see; it is not a measure of whether
the accepted ones are well written.
"""

import argparse
from collections import Counter, defaultdict

from recon.datagen.generate import Config, generate_dataset
from recon.engine.reconcile import reconcile
from recon.evaluation.llm_cli import add_llm_arguments, cost_line, json_client
from recon.evaluation.score import records_from_dataset
from recon.explanation.case import ExplainInput, RecordView
from recon.explanation.llm import LlmExplainer
from recon.ingestion.normalize import NormalizedRecord

MAX_RECORDS = 12


def explain_inputs(seed: int, size: int, per_kind: int) -> list[ExplainInput]:
    """Up to ``per_kind`` cases of each kind the engine reports for one generated month, as the
    application would show them to a model."""
    invoices, payments = records_from_dataset(generate_dataset(Config(seed=seed, invoices=size)))
    by_id: dict[tuple[str, str], NormalizedRecord] = {
        **{("invoice", r.record_id): r for r in invoices},
        **{("payment", r.record_id): r for r in payments},
    }
    taken: Counter[str] = Counter()
    inputs = []
    for exception in reconcile(invoices, payments).exceptions:
        keys = [("invoice", i) for i in exception.invoice_ids] + [
            ("payment", p) for p in exception.payment_ids
        ]
        if taken[exception.kind] >= per_kind or not keys or len(keys) > MAX_RECORDS:
            continue
        taken[exception.kind] += 1
        views = tuple(
            RecordView(
                ref=f"R{index}",
                role=role,
                record_id=record_id,
                occurred_on=by_id[(role, record_id)].occurred_on,
                amount=by_id[(role, record_id)].amount,
                text=by_id[(role, record_id)].text,
                label=role,
            )
            for index, (role, record_id) in enumerate(keys, start=1)
        )
        proposed = next(
            (v.ref for v in views if v.record_id == exception.proposed_invoice_id), None
        )
        inputs.append(ExplainInput(exception.kind, exception.variance, views, proposed))
    return inputs


def _reason(problem: str) -> str:
    """A rejection's category, without the figure or alias it names."""
    for marker, reason in (
        ("cites", "cites a record that is not in the case"),
        ("writes the id", "writes a real id"),
        ("which is not one of the engine's figures", "states a figure the engine did not compute"),
        ("longer than", "too long"),
        ("next steps", "wrong number of next steps"),
    ):
        if marker in problem:
            return reason
    return problem


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.explanation_eval")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--invoices", type=int, default=200)
    parser.add_argument("--per-kind", type=int, default=3)
    add_llm_arguments(parser)
    args = parser.parse_args()

    explainer = LlmExplainer(json_client(args))
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    reasons: Counter[str] = Counter()
    for seed in range(args.seeds):
        for inp in explain_inputs(seed, args.invoices, args.per_kind):
            explained = explainer.explain(inp)
            if explained.text is not None:
                outcomes[inp.kind]["verified"] += 1
            elif explained.problems == ("the model gave no usable answer",):
                outcomes[inp.kind]["no answer"] += 1
            else:
                outcomes[inp.kind]["rejected"] += 1
                reasons.update(_reason(problem) for problem in explained.problems)

    print(f"{'kind':<28}{'cases':>6}{'verified':>10}{'rejected':>10}{'no answer':>11}")
    total: Counter[str] = Counter()
    for kind, counts in sorted(outcomes.items()):
        total.update(counts)
        n = sum(counts.values())
        print(
            f"{kind:<28}{n:>6}{counts['verified']:>10}{counts['rejected']:>10}"
            f"{counts['no answer']:>11}"
        )
    n = sum(total.values())
    print(
        f"{'all':<28}{n:>6}{total['verified']:>10}{total['rejected']:>10}{total['no answer']:>11}"
    )
    if reasons:
        print("\nwhy explanations were rejected:")
        for reason, count in reasons.most_common():
            print(f"  {count:>3}  {reason}")
    print(f"\nmodel calls: {explainer.calls}, errors: {len(explainer.errors)}")
    for message in explainer.errors[:5]:
        print(f"  {message}")
    line = cost_line(args.model, explainer.input_tokens, explainer.output_tokens)
    if line and explainer.calls:
        print(line)


if __name__ == "__main__":
    main()

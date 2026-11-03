"""Score investigations against the causes the generator wrote down.

    python -m recon.evaluation.investigation_eval --seeds 2 --per-kind 10

Each case the engine finds in a month generated with causes is investigated, and the finding is
scored on its cause, whether it found the other records the cause involves, whether its action
fits the cause, and whether it passed the checks. Two baselines are scored on the same cases:
always naming the most common cause for the kind, and rules written for this generator by the
person who wrote it, which is close to the best that knowing the exact formulas can do.
"""

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from recon.ai.investigation.agent import Investigation, investigate
from recon.ai.investigation.ledger import Ledger, OpenCase
from recon.datagen.causes import CAUSES
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.model import Anomaly
from recon.engine.reconcile import reconcile
from recon.evaluation.llm_cli import add_llm_arguments, cost_line, tool_client
from recon.evaluation.score import records_from_dataset
from recon.money import Money

KINDS = tuple(CAUSES)
COSTS = ("processor_fee", "early_payment_discount", "bank_charge")
MAJORITY = {
    "amount_mismatch": "processor_fee",
    "missing_payment": "not_paid",
    "unmatched_payment": "unidentified",
}


@dataclass(frozen=True)
class LabelledCase:
    seed: int
    case: OpenCase
    cause: str
    # The records outside the case that its cause involves, by real id.
    counterparts: frozenset[str]


def month(seed: int, size: int) -> tuple[Ledger, list[LabelledCase]]:
    """A ledger for one generated month, and its cases of the three kinds with their causes."""
    dataset = generate_dataset(Config(seed=seed, invoices=size, causes=True))
    invoices, payments = records_from_dataset(dataset)
    exceptions = reconcile(invoices, payments).exceptions
    cases = [OpenCase(e.kind.value, e.invoice_ids, e.payment_ids, e.variance) for e in exceptions]
    by_records = {(a.invoice_ids, a.payment_ids): a for a in dataset.truth.anomalies}
    by_id = {a.anomaly_id: a for a in dataset.truth.anomalies}

    def records(anomaly: Anomaly) -> set[str]:
        return {*anomaly.invoice_ids, *anomaly.payment_ids}

    labelled = []
    for case in cases:
        truth = by_records.get((case.invoice_ids, case.payment_ids))
        if case.kind not in KINDS or truth is None or truth.cause is None:
            continue
        others = (
            set().union(*(records(by_id[r]) for r in truth.related)) if truth.related else set()
        )
        labelled.append(LabelledCase(seed, case, truth.cause, frozenset(others - records(truth))))
    return Ledger(invoices, payments, cases), labelled


def _amount(ledger: Ledger, role: str, record_id: str) -> Money:
    return ledger.record(role, record_id).amount


def rules_cause(ledger: Ledger, case: OpenCase) -> tuple[str, set[str]]:
    """A cause from arithmetic alone, with the records outside the case it rests on."""
    if case.kind == "amount_mismatch":
        total = _amount(ledger, "invoice", case.invoice_ids[0]).minor
        paid = _amount(ledger, "payment", case.payment_ids[0]).minor
        short = total - paid
        if any(abs(short - round(total * p / 100)) <= 1 for p in (1, 1.5, 2, 2.5, 3)):
            return "early_payment_discount", set()
        if short in (1000, 1500, 2000, 2500, 3500):
            return "bank_charge", set()
        if any(0.024 <= (short - fixed) / total <= 0.0341 for fixed in (20, 25, 30, 35)):
            return "processor_fee", set()
        return "keying_error", set()

    open_invoices = [i for i in ledger.invoices if ("invoice", i) in ledger.state]
    unmatched = [
        p for p in ledger.payments if ledger.state.get(("payment", p)) == "unmatched_payment"
    ]
    if case.kind == "missing_payment":
        invoice = ledger.record("invoice", case.invoice_ids[0])
        for p in unmatched:
            payment = ledger.record("payment", p)
            if payment.amount == invoice.amount and payment.counterparty != invoice.counterparty:
                return "paid_by_related_account", {p}
        same = [
            i
            for i in open_invoices
            if i != invoice.record_id
            and ledger.record("invoice", i).counterparty == invoice.counterparty
        ]
        for p in unmatched:
            payment = ledger.record("payment", p)
            if payment.counterparty != invoice.counterparty:
                continue
            for other in same:
                if (
                    invoice.amount.minor + _amount(ledger, "invoice", other).minor
                    == payment.amount.minor
                ):
                    return "paid_in_lump_sum", {p, other}
        return "not_paid", set()

    payment = ledger.record("payment", case.payment_ids[0])
    for i in open_invoices:
        invoice = ledger.record("invoice", i)
        if (
            ledger.state[("invoice", i)] == "missing_payment"
            and invoice.amount == payment.amount
            and invoice.counterparty != payment.counterparty
        ):
            return "paid_by_related_account", {i}
    mine = [
        i for i in open_invoices if ledger.record("invoice", i).counterparty == payment.counterparty
    ]
    for a_index, a in enumerate(mine):
        for b in mine[a_index + 1 :]:
            if (
                _amount(ledger, "invoice", a).minor + _amount(ledger, "invoice", b).minor
                == payment.amount.minor
            ):
                return "paid_in_lump_sum", {a, b}
    return "unidentified", set()


@dataclass
class Tally:
    cases: Counter[str]
    cause: Counter[str]
    counterparts: Counter[str]
    action: Counter[str]
    verified: Counter[str]


def _tally() -> Tally:
    return Tally(Counter(), Counter(), Counter(), Counter(), Counter())


def _found_all(found: set[str], needed: frozenset[str]) -> bool:
    return needed <= found


def score(
    labelled: list[tuple[Ledger, LabelledCase]],
    agent: Callable[[Ledger, OpenCase], Investigation] | None,
) -> dict[str, Tally]:
    tallies = {name: _tally() for name in ("majority", "rules", "agent")}
    for ledger, item in labelled:
        kind = item.case.kind
        for name in ("majority", "rules"):
            tallies[name].cases[kind] += 1
        tallies["majority"].cause[kind] += MAJORITY[kind] == item.cause
        tallies["majority"].counterparts[kind] += not item.counterparts
        cause, found = rules_cause(ledger, item.case)
        tallies["rules"].cause[kind] += cause == item.cause
        tallies["rules"].counterparts[kind] += _found_all(found, item.counterparts)
        if agent is None:
            continue
        done = agent(ledger, item.case)
        t = tallies["agent"]
        t.cases[kind] += 1
        if done.finding is None:
            continue
        t.verified[kind] += done.verified
        t.cause[kind] += done.verified and done.finding["cause"] == item.cause
        real = {ledger.real(str(a)) for a in done.finding.get("evidence", [])}
        t.counterparts[kind] += done.verified and _found_all(
            {r[1] for r in real if r is not None}, item.counterparts
        )
        expected_action = "accept_variance" if item.cause in COSTS else "record_finding"
        t.action[kind] += done.verified and done.finding["action"] == expected_action
    return tallies


def format_report(tallies: dict[str, Tally]) -> str:
    lines = [
        f"{'':<10}{'kind':<20}{'cases':>6}{'cause':>8}{'records':>9}{'action':>8}{'checked':>9}"
    ]
    for name, t in tallies.items():
        if not sum(t.cases.values()):
            continue
        for kind in (*KINDS, "all"):
            n = sum(t.cases.values()) if kind == "all" else t.cases[kind]
            if not n:
                continue

            def share(c: Counter[str], k: str = kind, n: int = n) -> str:
                v = sum(c.values()) if k == "all" else c[k]
                return f"{v / n:>7.0%}"

            row = f"{name:<10}{kind:<20}{n:>6} {share(t.cause)}"
            row += f" {share(t.counterparts)}"
            row += f" {share(t.action)} {share(t.verified)}" if name == "agent" else ""
            lines.append(row)
    return "\n".join(lines)


def sample(seeds: range, size: int, per_kind: int) -> list[tuple[Ledger, LabelledCase]]:
    """Up to ``per_kind`` cases of each kind and cause mix, in a fixed order, over the seeds."""
    chosen: list[tuple[Ledger, LabelledCase]] = []
    taken: dict[str, int] = defaultdict(int)
    for seed in seeds:
        ledger, cases = month(seed, size)
        for item in cases:
            if taken[item.case.kind] < per_kind:
                taken[item.case.kind] += 1
                chosen.append((ledger, item))
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.investigation_eval")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--invoices", type=int, default=200)
    parser.add_argument("--per-kind", type=int, default=10)
    parser.add_argument("--baselines-only", action="store_true")
    add_llm_arguments(parser)
    args = parser.parse_args()

    chosen = sample(
        range(args.first_seed, args.first_seed + args.seeds), args.invoices, args.per_kind
    )
    done: list[Investigation] = []
    agent = None
    if not args.baselines_only:
        model = tool_client(args)

        def agent(ledger: Ledger, case: OpenCase) -> Investigation:
            result = investigate(model, ledger, case)
            done.append(result)
            return result

    print(format_report(score(chosen, agent)))
    print(f"\ncauses in the sample: {dict(Counter(item.cause for _, item in chosen))}")
    if done:
        errors = [d.error for d in done if d.error]
        rejected = Counter(p.split(",")[0] for d in done for p in d.problems)
        print(
            f"investigations: {len(done)}, turns: {sum(d.turns for d in done)}, "
            f"errors: {len(errors)}"
        )
        for e in errors[:5]:
            print(f"  {e}")
        if rejected:
            print(f"check failures: {dict(rejected)}")
        print(
            cost_line(
                args.model, sum(d.input_tokens for d in done), sum(d.output_tokens for d in done)
            )
        )


if __name__ == "__main__":
    main()

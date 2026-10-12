"""How many questions a person is asked, with and without answers feeding back.

    python -m recon.evaluation.question_eval --seeds 3

A simulated person answers each question with the generator's true mapping, so this measures
the number of questions, not whether people answer correctly. Without feedback, a person is
shown every open question at once. With it, one question is asked at a time, the proposal is
worked out again with the answer pinned, and questions the answer settled are not asked.
"""

import argparse
import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from recon.datagen.generate import Config, first_days, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles, style_group
from recon.datagen.writer import write_dataset
from recon.evaluation.mapping_eval import GROUPS, VARIANTS
from recon.evaluation.mapping_score import score_file
from recon.ingestion.normalize import ColumnMapping
from recon.mapping.answers import Pins
from recon.mapping.baseline import propose_mapping
from recon.mapping.profile import profile_csv_bytes
from recon.mapping.proposal import MappingProposal
from recon.mapping.questions import choose_question

MAX_QUESTIONS = 30


@dataclass(frozen=True)
class Dialogue:
    group: str
    style: str
    open_at_start: int
    asked: tuple[tuple[str, str], ...]
    wrong_after: int
    seconds: float


def true_value(truth: ColumnMapping, field: str) -> str | None:
    value = truth.model_dump()[field]
    return None if value is None else str(value)


def dialogue(
    invoices: bytes, payments: bytes, truth: dict[str, ColumnMapping]
) -> tuple[int, list[tuple[str, str]], MappingProposal, float]:
    inv, pay = (
        profile_csv_bytes("invoices.csv", invoices),
        profile_csv_bytes("payments.csv", payments),
    )
    started = time.perf_counter()

    def propose(pins: Pins) -> MappingProposal:
        return propose_mapping(inv, pay, pins)

    headers = {"invoices": inv.headers(), "payments": pay.headers()}
    pins: dict[tuple[str, str], str | None] = {}
    asked: list[tuple[str, str]] = []
    opened = len(propose(pins).open_items())
    while len(asked) < MAX_QUESTIONS:
        question = choose_question(propose, pins, headers)
        if question is None:
            break
        pins[(question.source, question.field)] = true_value(truth[question.source], question.field)
        asked.append((question.source, question.field))
    return opened, asked, propose(pins), time.perf_counter() - started


def run_case(styles: ExportStyles, variant: str, seed: int, size: int) -> Dialogue:
    dataset = generate_dataset(Config(seed=seed, invoices=size))
    if variant == "first_12_days":
        dataset = first_days(dataset, 12)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        write_dataset(dataset, path, styles)
        raw = json.loads((path / "mapping_truth.json").read_text(encoding="utf-8"))
        truth = {
            s: ColumnMapping.model_validate(raw[s]["mapping"]) for s in ("invoices", "payments")
        }
        opened, asked, final, seconds = dialogue(
            (path / "invoices.csv").read_bytes(), (path / "payments.csv").read_bytes(), truth
        )
    wrong = sum(
        1
        for source, file in (("invoices", final.invoices), ("payments", final.payments))
        for outcome in score_file(file, truth[source]).values()
        if outcome == "wrong"
    )
    return Dialogue(
        style_group(styles.invoices, styles.payments),
        f"{styles.invoices} / {styles.payments} / {variant}",
        opened,
        tuple(asked),
        wrong,
        seconds,
    )


def run_evaluation(
    *, seeds: Sequence[int], size: int = 120, variants: Sequence[str] = VARIANTS
) -> list[Dialogue]:
    return [
        run_case(ExportStyles(i, p), variant, seed, size)
        for seed in seeds
        for variant in variants
        for i in INVOICE_STYLES
        for p in PAYMENT_STYLES
    ]


def format_report(results: Sequence[Dialogue]) -> str:
    lines = []
    for group in GROUPS:
        rows = [r for r in results if r.group == group]
        if not rows:
            continue
        with_questions = [r for r in rows if r.open_at_start]
        shown = sum(r.open_at_start for r in with_questions)
        asked = sum(len(r.asked) for r in with_questions)
        fewer = sum(1 for r in with_questions if len(r.asked) < r.open_at_start)
        lines.append(
            f"{group}: {len(rows)} pairs of files, {len(with_questions)} with an open question\n"
            f"  questions shown at once: {shown}; asked one at a time: {asked}"
            f" ({1 - asked / shown:.0%} fewer)\n"
            f"  pairs that needed fewer questions: {fewer}; "
            f"most asked of one pair: {max((len(r.asked) for r in rows), default=0)}\n"
            f"  confidently wrong after the dialogue: {sum(r.wrong_after for r in rows)}; "
            f"slowest pair: {max(r.seconds for r in rows):.2f}s"
            if shown
            else f"{group}: {len(rows)} pairs of files, none with an open question"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.question_eval")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--invoices", type=int, default=120)
    args = parser.parse_args()
    print(format_report(run_evaluation(seeds=range(args.seeds), size=args.invoices)))


if __name__ == "__main__":
    main()

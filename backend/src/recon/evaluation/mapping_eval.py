"""Run a mapper over every pair of export styles and report how it does.

    python -m recon.evaluation.mapping_eval --seeds 3

Styles are reported in three groups: development (the mapper was built around them), held out
(written before the mapper and never tuned against) and stress (built to defeat header words).
A mapper built around the first group looks better on it than it should.
"""

import argparse
import dataclasses
import json
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from recon.datagen.generate import Config, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles, style_group
from recon.datagen.writer import write_dataset
from recon.evaluation.mapping_score import FILE_OUTCOMES, classify, score_file
from recon.ingestion.normalize import ColumnMapping
from recon.mapping.baseline import propose_file, propose_mapping
from recon.mapping.profile import FileProfile, profile_csv_bytes
from recon.mapping.proposal import FileProposal, MappingProposal

Mapper = Callable[[FileProfile, FileProfile], MappingProposal]
SingleFileMapper = Callable[[FileProfile, str], FileProposal]
GROUPS = ("development", "held_out", "stress")
VARIANTS = ("full_month", "first_12_days")


@dataclass(frozen=True)
class CaseResult:
    invoice_style: str
    payment_style: str
    variant: str
    seed: int
    group: str
    invoices: Mapping[str, str]
    payments: Mapping[str, str]
    details: tuple[str, ...]

    @property
    def file_outcomes(self) -> tuple[str, str]:
        return classify(self.invoices), classify(self.payments)


def each_file_alone(mapper: SingleFileMapper) -> Mapper:
    """Use a one-file mapper as a mapper of two files, giving each no sight of the other."""

    def paired(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return MappingProposal(
            invoices=mapper(invoices, "invoices"), payments=mapper(payments, "payments")
        )

    return paired


def run_case(
    mapper: Mapper, styles: ExportStyles, variant: str, seed: int, size: int
) -> CaseResult:
    dataset = generate_dataset(Config(seed=seed, invoices=size))
    if variant == "first_12_days":
        dataset = dataclasses.replace(
            dataset, invoices=tuple(i for i in dataset.invoices if i.issue_date.day <= 12)
        )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        write_dataset(dataset, path, styles)
        truth = json.loads((path / "mapping_truth.json").read_text(encoding="utf-8"))
        proposal = mapper(
            profile_csv_bytes("invoices.csv", (path / "invoices.csv").read_bytes()),
            profile_csv_bytes("payments.csv", (path / "payments.csv").read_bytes()),
        )
    invoices = score_file(
        proposal.invoices, ColumnMapping.model_validate(truth["invoices"]["mapping"])
    )
    payments = score_file(
        proposal.payments, ColumnMapping.model_validate(truth["payments"]["mapping"])
    )
    details = tuple(
        f"{source}.{field}: {outcome} (proposed {choice!r}, truth {expected!r})"
        for source, outcomes, file_proposal, mapping in (
            ("invoices", invoices, proposal.invoices, truth["invoices"]["mapping"]),
            ("payments", payments, proposal.payments, truth["payments"]["mapping"]),
        )
        for field, outcome in outcomes.items()
        if outcome in ("wrong", "missed")
        for choice, expected in [
            (
                getattr(file_proposal, field).value
                if field in ("date_format", "decimal_separator")
                else file_proposal.choice(field).column,
                mapping[field],
            )
        ]
    )
    return CaseResult(
        styles.invoices,
        styles.payments,
        variant,
        seed,
        style_group(styles.invoices, styles.payments),
        invoices,
        payments,
        details,
    )


def run_evaluation(
    mapper: Mapper, *, seeds: Sequence[int], size: int = 120, variants: Sequence[str] = VARIANTS
) -> list[CaseResult]:
    return [
        run_case(mapper, ExportStyles(i, p), variant, seed, size)
        for seed in seeds
        for variant in variants
        for i in INVOICE_STYLES
        for p in PAYMENT_STYLES
    ]


def summarize(results: Sequence[CaseResult]) -> dict[str, dict[str, int]]:
    """File counts by outcome, and field counts by outcome, for dev and held-out cases."""
    summary: dict[str, dict[str, int]] = {}
    for label in GROUPS:
        chosen = [r for r in results if r.group == label]
        files: Counter[str] = Counter()
        fields: Counter[str] = Counter()
        for r in chosen:
            files.update(r.file_outcomes)
            fields.update(r.invoices.values())
            fields.update(r.payments.values())
        summary[label] = {
            "files": sum(files.values()),
            **{f"files_{o}": files[o] for o in FILE_OUTCOMES},
            "fields": sum(fields.values()),
            **{f"fields_{o}": fields[o] for o in ("correct", "asked", "missed", "wrong")},
        }
    return summary


def format_report(results: Sequence[CaseResult]) -> str:
    lines = []
    for label, counts in summarize(results).items():
        files, fields = counts["files"], counts["fields"]
        if not files:
            continue
        lines.append(f"{label}: {files} files, {fields} fields")
        lines.append(
            "  files:  "
            + ", ".join(
                f"{o} {counts[f'files_{o}']} ({counts[f'files_{o}'] / files:.0%})"
                for o in FILE_OUTCOMES
            )
        )
        lines.append(
            "  fields: "
            + ", ".join(
                f"{o} {counts[f'fields_{o}']} ({counts[f'fields_{o}'] / fields:.1%})"
                for o in ("correct", "asked", "missed", "wrong")
            )
        )
    problems = Counter(
        (r.invoice_style, r.payment_style, d)
        for r in results
        if r.variant == "full_month"
        for d in r.details
    )
    if problems:
        lines.append("")
        lines.append("wrong or missed (full month, all seeds):")
        lines.extend(
            f"  {i} + {p}: {d}" + (f"  x{n}" if n > 1 else "")
            for (i, p, d), n in sorted(problems.items())
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.mapping_eval")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--invoices", type=int, default=120)
    parser.add_argument(
        "--mode",
        choices=["paired", "single"],
        default="paired",
        help="paired: the mapper sees both files; single: each file is mapped on its own",
    )
    args = parser.parse_args()
    mapper = propose_mapping if args.mode == "paired" else each_file_alone(propose_file)
    results = run_evaluation(mapper, seeds=range(args.seeds), size=args.invoices)
    print(format_report(results))


if __name__ == "__main__":
    main()

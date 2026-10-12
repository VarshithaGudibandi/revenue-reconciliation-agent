"""Run a mapper over every pair of export styles and report how it does.

    python -m recon.evaluation.mapping_eval --seeds 3

Styles are reported in three groups: development (the mapper was built around them), held out
(written before the mapper and never tuned against) and stress (built to defeat header words).
A mapper built around the first group looks better on it than it should.
"""

import argparse
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from recon.datagen.generate import Config, first_days, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles, style_group
from recon.datagen.writer import write_dataset
from recon.evaluation.mapping_score import FILE_OUTCOMES, classify, score_file
from recon.ingestion.normalize import ColumnMapping
from recon.mapping.baseline import propose_file, propose_mapping
from recon.mapping.combine import combine
from recon.mapping.llm import LlmMapper
from recon.mapping.llm_client import AnthropicJson, JsonModel, RecordingJson, ReplayJson
from recon.mapping.pairing import Mapper, each_file_alone
from recon.mapping.profile import FileProfile, profile_csv_bytes
from recon.mapping.proposal import MappingProposal

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


def run_case(
    mapper: Mapper, styles: ExportStyles, variant: str, seed: int, size: int
) -> CaseResult:
    dataset = generate_dataset(Config(seed=seed, invoices=size))
    if variant == "first_12_days":
        dataset = first_days(dataset, 12)
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


# List prices in US dollars per million input and output tokens, from the Claude API reference
# cached on 2026-10-06. An estimate for planning: check current pricing before relying on it.
LIST_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def _llm_mapper(args: argparse.Namespace) -> LlmMapper:
    client: JsonModel
    if args.replay:
        client = ReplayJson(Path(args.replay))
        if not len(client):
            raise SystemExit(f"{args.replay} holds no recorded answers")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "No ANTHROPIC_API_KEY is set and no --replay file was given, so there is "
                "nothing to ask. Run with --mapper baseline, or replay recorded answers."
            )
        client = AnthropicJson(model=args.model, effort=args.effort)
        if args.record:
            client = RecordingJson(client, Path(args.record))
    return LlmMapper(client, validate=args.mapper != "llm-unchecked")


def _hybrid(rules: Mapper, model: Mapper) -> Mapper:
    def both(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return combine(rules(invoices, payments), model(invoices, payments))

    return both


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.evaluation.mapping_eval")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--invoices", type=int, default=120)
    parser.add_argument(
        "--mapper",
        choices=["baseline", "llm", "llm-unchecked", "hybrid"],
        default="baseline",
        help="llm-unchecked skips the checks against the data; hybrid combines the "
        "paired rules with the model",
    )
    parser.add_argument(
        "--mode",
        choices=["paired", "single"],
        default="paired",
        help="baseline only. paired: the mapper sees both files; single: each file alone",
    )
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--replay", help="serve answers recorded in this file; no network")
    parser.add_argument("--record", help="append the live answers to this file")
    args = parser.parse_args()

    llm: LlmMapper | None = None
    if args.mapper == "baseline":
        mapper = propose_mapping if args.mode == "paired" else each_file_alone(propose_file)
    else:
        llm = _llm_mapper(args)
        mapper = each_file_alone(llm.propose_file)
        if args.mapper == "hybrid":
            mapper = _hybrid(propose_mapping, mapper)

    results = run_evaluation(mapper, seeds=range(args.seeds), size=args.invoices)
    print(format_report(results))
    if llm is not None:
        print(f"\nmodel calls: {llm.calls}, errors: {len(llm.errors)}")
        for message in llm.errors[:5]:
            print(f"  {message}")
        price = LIST_PRICES.get(args.model)
        if price and llm.calls:
            cost = llm.input_tokens / 1e6 * price[0] + llm.output_tokens / 1e6 * price[1]
            print(
                f"tokens: {llm.input_tokens} in, {llm.output_tokens} out; "
                f"about ${cost:.2f} at list price for {args.model}"
            )


if __name__ == "__main__":
    main()

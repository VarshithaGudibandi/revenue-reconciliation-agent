import argparse
from pathlib import Path

from recon.datagen.generate import Config, apply_corrections, first_days, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles
from recon.datagen.writer import write_dataset


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m recon.datagen",
        description="Write synthetic invoice and payment exports with a known set of problems.",
    )
    parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--period", default="2026-01", help="YYYY-MM")
    parser.add_argument("--invoices", type=int, default=200)
    parser.add_argument(
        "--first-days",
        type=int,
        metavar="N",
        help="keep only invoices issued on days 1 to N; for trying file reading, not scoring",
    )
    parser.add_argument("--invoice-style", choices=sorted(INVOICE_STYLES), default="default")
    parser.add_argument("--payment-style", choices=sorted(PAYMENT_STYLES), default="default")
    parser.add_argument(
        "--no-corrections",
        action="store_true",
        help="skip the second revision in which some problems are fixed at the source",
    )
    args = parser.parse_args()

    config = Config(seed=args.seed, period=args.period, invoices=args.invoices)
    styles = ExportStyles(args.invoice_style, args.payment_style)
    first = generate_dataset(config)
    if args.first_days:
        first = first_days(first, args.first_days)
    write_dataset(first, args.out / "v1", styles)
    print(f"v1: {len(first.invoices)} invoices, {len(first.payments)} payments")

    if not args.no_corrections:
        second = apply_corrections(first, seed=args.seed)
        write_dataset(second, args.out / "v2", styles)
        print(
            f"v2: {len(second.payments)} payments, "
            f"{len(second.truth.resolved)} of {len(first.truth.anomalies)} problems fixed"
        )


if __name__ == "__main__":
    main()

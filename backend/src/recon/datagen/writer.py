import csv
import json
from pathlib import Path
from typing import Any

from recon.datagen.model import Dataset
from recon.domain.money import format_amount


def truth_to_dict(dataset: Dataset) -> dict[str, Any]:
    truth = dataset.truth
    return {
        "revision": dataset.revision,
        "seed": dataset.seed,
        "period": dataset.period,
        "matches": [
            {
                "invoice_id": m.invoice_id,
                "payment_ids": list(m.payment_ids),
                "noisy_reference": m.noisy_reference,
            }
            for m in truth.matches
        ],
        "anomalies": [
            {
                "anomaly_id": a.anomaly_id,
                "kind": a.kind.value,
                "invoice_ids": list(a.invoice_ids),
                "payment_ids": list(a.payment_ids),
                "variance_minor": a.variance_minor,
                "currency": a.currency,
                "intended_invoice_id": a.intended_invoice_id,
            }
            for a in truth.anomalies
        ],
        "resolved": list(truth.resolved),
    }


def write_dataset(dataset: Dataset, directory: Path) -> None:
    """Write the two source exports and the ground truth.

    The invoice system and the payment system deliberately use different column
    names, date formats and number formats, as real exports do.
    """
    directory.mkdir(parents=True, exist_ok=True)

    with (directory / "invoices.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["invoice_id", "customer_id", "issue_date", "currency", "total"])
        for invoice in dataset.invoices:
            writer.writerow(
                [
                    invoice.invoice_id,
                    invoice.customer_id,
                    invoice.issue_date.isoformat(),
                    invoice.currency,
                    format_amount(invoice.total_minor, invoice.currency),
                ]
            )

    with (directory / "payments.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["payment_id", "reference", "customer", "received_on", "ccy", "amount_paid"]
        )
        for payment in dataset.payments:
            writer.writerow(
                [
                    payment.payment_id,
                    payment.reference,
                    payment.customer_id,
                    payment.received_on.strftime("%m/%d/%Y"),
                    payment.currency,
                    format_amount(payment.amount_minor, payment.currency, thousands=True),
                ]
            )

    truth_text = json.dumps(truth_to_dict(dataset), indent=2, sort_keys=True)
    (directory / "ground_truth.json").write_text(truth_text + "\n", encoding="utf-8")

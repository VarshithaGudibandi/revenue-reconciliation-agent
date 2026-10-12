import csv
import json
from pathlib import Path
from typing import Any

from recon.datagen.model import Dataset
from recon.datagen.styles import ExportStyles, export_truth, invoice_rows, payment_rows


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
                "memo_kind": a.memo_kind,
            }
            for a in truth.anomalies
        ],
        "resolved": list(truth.resolved),
    }


def write_dataset(dataset: Dataset, directory: Path, styles: ExportStyles | None = None) -> None:
    """Write the two source exports, the ground truth, and the right mapping of each export.

    The invoice system and the payment system deliberately use different column names,
    date formats and number formats, as real exports do. ``styles`` picks which.
    """
    styles = styles or ExportStyles()
    directory.mkdir(parents=True, exist_ok=True)

    for name, (header, rows) in (
        ("invoices.csv", invoice_rows(dataset, styles.invoice_style)),
        ("payments.csv", payment_rows(dataset, styles.payment_style)),
    ):
        with (directory / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

    mapping_text = json.dumps(export_truth(dataset, styles), indent=2, sort_keys=True)
    (directory / "mapping_truth.json").write_text(mapping_text + "\n", encoding="utf-8")

    truth_text = json.dumps(truth_to_dict(dataset), indent=2, sort_keys=True)
    (directory / "ground_truth.json").write_text(truth_text + "\n", encoding="utf-8")

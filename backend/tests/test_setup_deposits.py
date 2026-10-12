"""Setting up a deposits-and-payouts reconciliation from two sample files."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recon.config import Settings
from recon.datagen.batches import (
    DEPOSIT_MAPPING,
    PAYOUT_MAPPING,
    BatchConfig,
    generate_batches,
    write_batches,
)
from recon.main import create_app
from recon.persistence.models import Profile

PERIOD = "2026-01"


@pytest.fixture
def files(tmp_path: Path) -> Path:
    write_batches(generate_batches(BatchConfig(seed=2, deposits=100)), tmp_path / "data")
    return tmp_path / "data"


def client(url: str, root: Path) -> TestClient:
    return TestClient(create_app(Settings(database_url=url, storage_dir=root / "storage")))


def propose(c: TestClient, data: Path, **form: str) -> Any:
    with (data / "deposits.csv").open("rb") as d, (data / "payouts.csv").open("rb") as p:
        return c.post(
            "/api/setup/proposals",
            files={
                "invoices": ("deposits.csv", d, "text/csv"),
                "payments": ("payouts.csv", p, "text/csv"),
            },
            data={"period": PERIOD, **form},
        )


def test_the_rules_read_the_deposit_and_payout_files_and_say_what_they_are(
    migrated_database_url: str, files: Path, tmp_path: Path
) -> None:
    with client(migrated_database_url, tmp_path) as c:
        response = propose(c, files, type="payout_deposit")

    body = response.json()
    assert response.status_code == 200
    assert body["labels"]["left"] == "Deposit" and body["labels"]["right_plural"] == "payouts"
    assert (body["mapper"], body["model"]) == ("rules", None)
    deposits = {f["field"]: f for f in body["invoices"]["fields"]}
    payouts = {f["field"]: f for f in body["payments"]["fields"]}
    assert deposits["record_id"]["column"] == "bank_txn_id" and deposits["record_id"]["confident"]
    assert deposits["counterparty"]["column"] == "account"
    assert payouts["record_id"]["column"] == "payout_id" and payouts["record_id"]["confident"]
    # the reference column of the payouts is the one that names the deposits
    assert payouts["reference"]["column"] == "bank_reference" and payouts["reference"]["confident"]
    assert body["invoices"]["needs_answer"] == 0 and body["payments"]["needs_answer"] == 0


def test_the_default_type_is_invoices_and_payments(
    migrated_database_url: str, files: Path, tmp_path: Path
) -> None:
    with client(migrated_database_url, tmp_path) as c:
        body = propose(c, files).json()

    assert body["labels"]["type"] == "invoice_payment"


def test_an_unknown_type_is_refused(
    migrated_database_url: str, files: Path, tmp_path: Path
) -> None:
    with client(migrated_database_url, tmp_path) as c:
        response = propose(c, files, type="receipts")

    assert response.status_code == 422


def test_confirming_creates_a_deposits_profile_and_reconciles_the_period(
    migrated_database_url: str, files: Path, tmp_path: Path
) -> None:
    with client(migrated_database_url, tmp_path) as c:
        proposal = propose(c, files, type="payout_deposit").json()
        confirmed = c.post(
            f"/api/setup/proposals/{proposal['id']}/confirm",
            json={
                "name": "Processors and bank",
                "invoices": DEPOSIT_MAPPING.model_dump(mode="json"),
                "payments": PAYOUT_MAPPING.model_dump(mode="json"),
            },
        )
        body = confirmed.json()
        summary = c.get(f"/api/profiles/{body['profile']['id']}/periods/{PERIOD}/summary").json()
        again = c.post(
            f"/api/setup/proposals/{proposal['id']}/confirm",
            json={
                "name": "Processors and bank",
                "invoices": DEPOSIT_MAPPING.model_dump(mode="json"),
                "payments": PAYOUT_MAPPING.model_dump(mode="json"),
            },
        )

    assert confirmed.status_code == 200
    assert body["profile"]["reconciliation_type"] == "payout_deposit"
    assert set(body["records"]) == {"deposits", "payouts"} and body["run"]["status"] == "succeeded"
    assert summary["labels"]["left"] == "Deposit" and summary["state"] == "reconciled"
    assert (summary["invoices"], summary["payments"]) == (
        body["records"]["deposits"],
        body["records"]["payouts"],
    )
    assert again.json()["created"] is False
    engine = create_engine(migrated_database_url)
    with sessionmaker(engine)() as session:
        profile = session.scalars(select(Profile)).one()
    engine.dispose()
    assert set(profile.config["sources"]) == {"deposits", "payouts"}

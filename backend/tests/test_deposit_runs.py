"""Bank deposits against payouts, from files to cases and the API."""

import csv
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.batches import (
    DEPOSIT_MAPPING,
    PAYOUT_MAPPING,
    BatchAnomalyKind,
    BatchConfig,
    BatchDataset,
    generate_batches,
    write_batches,
)
from recon.main import create_app
from recon.persistence.repository import create_profile, register_file
from recon.pipeline import reconcile_period
from recon.profiles import ProfileConfig

PERIOD = "2026-01"


@dataclass
class World:
    url: str
    root: Path
    data: Path
    dataset: BatchDataset
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def client(self) -> TestClient:
        return TestClient(
            create_app(Settings(database_url=self.url, storage_dir=self.root / "storage"))
        )

    def register(self, source: str) -> None:
        with self.sessions.begin() as session:
            register_file(
                session,
                profile_id=self.profile_id,
                source_name=source,
                period=PERIOD,
                path=self.data / f"{source}.csv",
            )

    def cases(self, client: TestClient, **params: Any) -> list[dict[str, Any]]:
        page = client.get(
            f"/api/profiles/{self.profile_id}/periods/{PERIOD}/cases",
            params={"limit": 200, **params},
        ).json()
        return [client.get(f"/api/cases/{c['id']}").json() for c in page["items"]]


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    dataset = generate_batches(BatchConfig(seed=3, deposits=150))
    data = tmp_path / "data"
    write_batches(dataset, data)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile_id = create_profile(
            session,
            name="Processor deposits",
            reconciliation_type="payout_deposit",
            config=ProfileConfig(sources={"deposits": DEPOSIT_MAPPING, "payouts": PAYOUT_MAPPING}),
        ).id
    world = World(migrated_database_url, tmp_path, data, dataset, sessions, profile_id)
    world.register("deposits")
    world.register("payouts")
    yield world
    engine.dispose()


def test_a_profile_of_the_type_reads_deposits_and_payouts_and_nothing_else(world: World) -> None:
    with world.sessions.begin() as session, pytest.raises(ValueError, match="deposits.*payouts"):
        create_profile(
            session,
            name="wrong",
            reconciliation_type="payout_deposit",
            config=ProfileConfig(sources={"invoices": DEPOSIT_MAPPING, "payments": PAYOUT_MAPPING}),
        )


def test_a_run_gives_a_case_for_every_seeded_problem_and_a_match_for_every_clean_deposit(
    world: World,
) -> None:
    outcome = reconcile_period(world.sessions, world.profile_id, PERIOD)
    assert outcome.run.status == "succeeded" and outcome.cases is not None

    with world.client() as client:
        cases = world.cases(client)
        summary = client.get(f"/api/profiles/{world.profile_id}/periods/{PERIOD}/summary").json()

    expected = sorted(a.kind.value for a in world.dataset.anomalies)
    assert sorted(c["kind"] for c in cases) == expected
    assert set(expected) == {k.value for k in BatchAnomalyKind}
    assert summary["state"] == "reconciled"
    assert summary["matched_invoices"] == len(world.dataset.matches)
    assert (summary["invoices"], summary["payments"]) == (
        len(world.dataset.deposits),
        len(world.dataset.payouts),
    )
    assert summary["labels"] == {
        "type": "payout_deposit",
        "title": "Bank deposits and processor payouts",
        "left": "Deposit",
        "left_plural": "deposits",
        "right": "Payout",
        "right_plural": "payouts",
    }


def test_records_are_labelled_as_deposits_and_payouts(world: World) -> None:
    reconcile_period(world.sessions, world.profile_id, PERIOD)
    with world.client() as client:
        case = next(c for c in world.cases(client, kind="deposit_mismatch"))

    labels = {(r["role"], r["label"]) for r in case["records"]}
    assert labels == {("invoice", "Deposit"), ("payment", "Payout")}
    assert case["records"][0]["record_id"].startswith(("BNK-", "PO-"))
    assert all(r["in_current_files"] for r in case["records"])


def test_every_kind_is_explained_in_terms_of_deposits_and_payouts(world: World) -> None:
    reconcile_period(world.sessions, world.profile_id, PERIOD)
    with world.client() as client:
        cases = world.cases(client)

    seen: dict[str, str] = {}
    for case in cases:
        analysis = case["analysis"]
        seen.setdefault(case["kind"], analysis["summary"])
        text = analysis["summary"] + analysis["why"]
        assert "nvoice" not in text and analysis["next_steps"]
    assert set(seen) == {k.value for k in BatchAnomalyKind}
    assert seen["deposit_mismatch"].endswith("its payouts.")
    assert seen["missing_payouts"].endswith("has no payouts that explain it.")
    assert "is in no deposit" in seen["undeposited_payout"]
    assert "nothing says so" in seen["proposed_grouping"]


def test_a_case_is_found_by_the_id_of_a_payout_or_a_deposit(world: World) -> None:
    reconcile_period(world.sessions, world.profile_id, PERIOD)
    anomaly = next(
        a for a in world.dataset.anomalies if a.kind is BatchAnomalyKind.DEPOSIT_MISMATCH
    )
    with world.client() as client:
        by_deposit = world.cases(client, q=anomaly.deposit_ids[0])
        by_payout = world.cases(client, q=anomaly.payout_ids[0])

    assert [c["kind"] for c in by_deposit] == ["deposit_mismatch"]
    assert [c["id"] for c in by_payout] == [c["id"] for c in by_deposit]


def test_putting_the_reference_on_proposed_payouts_resolves_the_case_by_the_data(
    world: World,
) -> None:
    reconcile_period(world.sessions, world.profile_id, PERIOD)
    anomaly = next(
        a for a in world.dataset.anomalies if a.kind is BatchAnomalyKind.PROPOSED_GROUPING
    )
    deposit_id = anomaly.deposit_ids[0]
    path = world.data / "payouts.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    for row in rows[1:]:
        if row[0] in anomaly.payout_ids:
            row[5] = deposit_id
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    world.register("payouts")

    # incremental, and compared with matching everything again: they must agree
    outcome = reconcile_period(world.sessions, world.profile_id, PERIOD, verify=True)

    assert outcome.run.status == "succeeded" and outcome.run.mode == "incremental"
    # only the account whose payouts changed is matched again
    assert (outcome.run.partitions_recomputed, outcome.run.partitions_total) == (1, 3)
    with world.client() as client:
        proposed = world.cases(client, kind="proposed_grouping", status="open")
        resolved = world.cases(client, status="resolved")
    assert deposit_id not in {r["record_id"] for c in proposed for r in c["records"]}
    assert any(
        deposit_id in {r["record_id"] for r in c["records"]} and c["kind"] == "proposed_grouping"
        for c in resolved
    )


def test_the_deposits_demo_is_seeded_once_and_listed_after_the_invoice_demo(
    migrated_database_url: str, tmp_path: Path
) -> None:
    from recon.demo import DEMO_BATCH_PROFILE, DEMO_PERIOD, seed_batch_demo, seed_demo

    settings = Settings(database_url=migrated_database_url, storage_dir=tmp_path, demo=True)
    with TestClient(create_app(settings)) as client:
        sessions = client.app.state.sessions  # type: ignore[attr-defined]
        seed_demo(sessions, tmp_path)
        first = seed_batch_demo(sessions, tmp_path)
        again = seed_batch_demo(sessions, tmp_path)

        profiles = client.get("/api/profiles").json()
        summary = client.get(f"/api/profiles/{first}/periods/{DEMO_PERIOD}/summary").json()
        periods = client.get(f"/api/profiles/{first}/periods").json()

    assert first == again
    assert [p["name"] for p in profiles] == ["Demo: invoices and payments", DEMO_BATCH_PROFILE]
    assert profiles[1]["reconciliation_type"] == "payout_deposit"
    assert summary["state"] == "reconciled" and summary["needs_attention"] > 0
    assert [f["version_number"] for p in periods for f in p["files"]] == [1, 1]

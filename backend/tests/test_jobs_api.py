"""Reconciliation through the job queue: the request returns a job, a worker does the work."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.jobs import queue
from recon.jobs.handlers import RECONCILE_PERIOD, handlers_for
from recon.jobs.worker import Worker
from recon.main import create_app
from recon.persistence.models import Job, Run
from recon.persistence.repository import create_profile, register_file
from recon.profiles import ProfileConfig

PERIOD = "2026-01"


@dataclass
class World:
    url: str
    root: Path
    data: Path
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def client(self, **settings: object) -> TestClient:
        return TestClient(
            create_app(
                Settings(database_url=self.url, storage_dir=self.root / "storage", **settings)  # type: ignore[arg-type]
            )
        )

    def worker(self) -> Worker:
        return Worker(
            self.sessions, handlers_for(self.sessions), name="test-worker", backoff=lambda n: 0.0
        )

    def upload(self, client: TestClient, source: str):  # type: ignore[no-untyped-def]
        with (self.data / f"{source}.csv").open("rb") as handle:
            return client.post(
                f"/api/profiles/{self.profile_id}/periods/{PERIOD}/files/{source}",
                files={"file": (f"{source}.csv", handle, "text/csv")},
            )


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    data = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=3, invoices=120)), data)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile = create_profile(
            session,
            name="queued",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(
                sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
            ),
        )
        profile_id = profile.id
        register_file(
            session,
            profile_id=profile_id,
            source_name="invoices",
            period=PERIOD,
            path=data / "invoices.csv",
        )
    yield World(migrated_database_url, tmp_path, data, sessions, profile_id)
    engine.dispose()


def runs(world: World) -> int:
    with world.sessions() as session:
        return session.scalar(select(func.count()).select_from(Run)) or 0


def test_in_the_request_by_default(world: World) -> None:
    with world.client() as client:
        response = world.upload(client, "payments")

    body = response.json()
    assert response.status_code == 200
    assert body["reconciled"] and body["job"] is None and body["run"]["status"] == "succeeded"


def test_with_a_queue_the_request_returns_a_job_and_nothing_has_run(world: World) -> None:
    with world.client(jobs="queue") as client:
        response = world.upload(client, "payments")
        job = client.get(f"/api/jobs/{response.json()['job']['id']}").json()

    body = response.json()
    assert response.status_code == 202
    assert not body["reconciled"] and body["run"] is None and body["cases"] is None
    assert (body["job"]["status"], body["job"]["kind"]) == ("queued", RECONCILE_PERIOD)
    assert job["status"] == "queued" and job["run"] is None
    assert runs(world) == 0


def test_a_worker_does_the_work_and_the_job_then_carries_the_result(world: World) -> None:
    with world.client(jobs="queue") as client:
        job_id = world.upload(client, "payments").json()["job"]["id"]

        assert world.worker().run_once() == "succeeded"
        done = client.get(f"/api/jobs/{job_id}").json()
        listed = client.get(f"/api/profiles/{world.profile_id}/periods/{PERIOD}/cases").json()

    assert done["status"] == "succeeded" and done["finished_at"] is not None
    assert done["run"]["status"] == "succeeded"
    assert done["cases"]["applied"] and done["cases"]["opened"] == listed["total"] > 0


def test_asking_twice_before_a_worker_starts_is_one_job(world: World) -> None:
    with world.client(jobs="queue") as client:
        first = world.upload(client, "payments").json()["job"]["id"]
        second = world.upload(client, "payments").json()["job"]["id"]

    assert first == second
    with world.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_a_request_made_while_a_worker_is_running_the_job_is_a_new_job(world: World) -> None:
    with world.client(jobs="queue") as client:
        first = world.upload(client, "payments").json()["job"]["id"]
        with world.sessions.begin() as session:
            assert queue.claim(session, "busy", now=datetime.now(UTC), lease=timedelta(seconds=60))
        second = world.upload(client, "payments").json()["job"]["id"]

    assert first != second


def test_a_job_that_cannot_work_says_why(world: World) -> None:
    with world.sessions.begin() as session:
        job, _ = queue.enqueue(
            session,
            RECONCILE_PERIOD,
            "broken",
            {"profile_id": "not-a-uuid", "period": PERIOD},
            now=datetime.now(UTC),
        )
        job_id = job.id

    assert world.worker().run_once() == "failed"
    with world.client(jobs="queue") as client:
        failed = client.get(f"/api/jobs/{job_id}").json()

    assert failed["status"] == "failed" and failed["attempts"] == 1
    assert "no usable profile" in failed["last_error"]
    assert failed["run"] is None


def test_an_unknown_job_is_not_found(world: World) -> None:
    with world.client() as client:
        response = client.get(f"/api/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_first_file_alone_starts_no_job(world: World) -> None:
    # the invoices were registered by the fixture; a repeat of them must not queue work either
    with world.client(jobs="queue") as client:
        response = world.upload(client, "invoices")

    assert response.status_code == 200 and response.json()["job"] is None
    with world.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0

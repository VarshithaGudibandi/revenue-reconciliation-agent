"""Managing alert channels through the API, and alerts after a real reconciliation job."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.jobs.handlers import handlers_for
from recon.jobs.kinds import SEND_ALERTS
from recon.jobs.worker import Worker
from recon.main import create_app
from recon.persistence.models import AlertDelivery, Job
from recon.persistence.repository import create_profile, register_file
from recon.profiles import ProfileConfig
from tests.test_alerts import Hook, hook  # noqa: F401  (the fixture)

HOOK = "https://hooks.slack.test/services/T0/B0/secret"


@dataclass
class World:
    url: str
    root: Path
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def settings(self, **extra: Any) -> Settings:
        return Settings(
            database_url=self.url,
            storage_dir=self.root / "storage",
            alerts_allow_http=True,
            alerts_allow_private=True,
            **extra,
        )

    def client(self, **extra: Any) -> TestClient:
        return TestClient(create_app(self.settings(**extra)))


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    data = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=6, invoices=100)), data)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile = create_profile(
            session,
            name="Acme books",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(
                sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
            ),
        )
        profile_id = profile.id
        for source in ("invoices", "payments"):
            register_file(
                session,
                profile_id=profile_id,
                source_name=source,
                period="2026-01",
                path=data / f"{source}.csv",
            )
    yield World(migrated_database_url, tmp_path, sessions, profile_id)
    engine.dispose()


def make(client: TestClient, **body: Any) -> Any:
    payload = {"name": "ops", "kind": "slack", "target": HOOK, **body}
    return client.post("/api/alert-channels", json=payload)


def test_a_channel_is_listed_with_its_webhook_masked(world: World) -> None:
    with world.client() as client:
        created = make(client)
        listed = client.get("/api/alert-channels").json()

    assert created.status_code == 201
    assert created.json()["target"] == "https://hooks.slack.test/…"
    assert "secret" not in created.text and "secret" not in str(listed)
    assert listed[0]["name"] == "ops" and listed[0]["last_delivery"] is None
    assert (listed[0]["enabled"], listed[0]["on_new_cases"], listed[0]["digest_hour"]) == (
        True,
        True,
        None,
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"kind": "slack", "target": "http://hooks.slack.test/x"}, "https"),
        ({"kind": "slack", "target": "https://127.0.0.1/x"}, "not a public address"),
        ({"kind": "email", "target": "finance@example.com"}, "RECON_SMTP_HOST"),
        ({"kind": "email", "target": "nonsense"}, "email address"),
        ({"kind": "pager", "target": HOOK}, ""),
        ({"digest_hour": 24}, ""),
        ({"name": ""}, ""),
    ],
)
def test_a_channel_that_would_not_work_or_is_not_safe_is_refused(
    world: World, body: dict[str, Any], message: str
) -> None:
    strict = Settings(database_url=world.url, storage_dir=world.root / "storage")
    with TestClient(create_app(strict)) as client:
        response = make(client, **body)

    assert response.status_code == 422
    assert message in response.json()["error"]["message"]


def test_two_channels_cannot_share_a_name(world: World) -> None:
    with world.client() as client:
        make(client)
        again = make(client)

    assert again.status_code == 409 and again.json()["error"]["code"] == "channel_exists"


def test_a_channel_is_switched_off_and_its_digest_set_and_cleared(world: World) -> None:
    with world.client() as client:
        channel = make(client).json()["id"]
        off = client.patch(
            f"/api/alert-channels/{channel}", json={"enabled": False, "digest_hour": 7}
        ).json()
        cleared = client.patch(f"/api/alert-channels/{channel}", json={"digest_hour": None}).json()
        bad = client.patch(f"/api/alert-channels/{channel}", json={"digest_hour": 30})

    assert (off["enabled"], off["digest_hour"]) == (False, 7)
    assert (cleared["enabled"], cleared["digest_hour"]) == (False, None)
    assert bad.status_code == 422


def test_a_channel_is_deleted_with_its_history(world: World, hook: Hook) -> None:  # noqa: F811
    with world.client() as client:
        channel = make(client, target=hook.url).json()["id"]
        client.post(f"/api/alert-channels/{channel}/test")
        deleted = client.delete(f"/api/alert-channels/{channel}")
        again = client.delete(f"/api/alert-channels/{channel}")
        deliveries = client.get("/api/alert-deliveries").json()

    assert (deleted.status_code, again.status_code) == (204, 404)
    assert deliveries == []


def test_a_test_message_says_whether_it_arrived(world: World, hook: Hook) -> None:  # noqa: F811
    with world.client() as client:
        channel = make(client, target=hook.url).json()["id"]
        worked = client.post(f"/api/alert-channels/{channel}/test").json()
        hook.answer_with(404)
        broke = client.post(f"/api/alert-channels/{channel}/test").json()
        listed = client.get("/api/alert-channels").json()[0]
        history = client.get("/api/alert-deliveries").json()

    assert (worked["status"], worked["kind"], worked["error"]) == ("sent", "test", None)
    assert broke["status"] == "failed" and "404" in broke["error"]
    assert listed["last_delivery"]["status"] == "failed"
    assert [d["status"] for d in history] == ["failed", "sent"]
    assert history[0]["channel_name"] == "ops"


def test_a_test_to_a_missing_channel_is_not_found(world: World) -> None:
    with world.client() as client:
        response = client.post(f"/api/alert-channels/{uuid.uuid4()}/test")

    assert response.status_code == 404


def test_a_reconciliation_job_leads_to_one_alert_through_the_worker(
    world: World,
    hook: Hook,  # noqa: F811
) -> None:
    from datetime import UTC, datetime

    from recon.jobs.kinds import enqueue_reconcile

    with world.client() as client:
        make(client, target=hook.url)
    with world.sessions.begin() as session:
        enqueue_reconcile(session, world.profile_id, "2026-01", now=datetime.now(UTC))
    worker = Worker(
        world.sessions,
        handlers_for(world.sessions, None, world.settings()),
        name="w",
        backoff=lambda n: 0.0,
    )

    assert worker.run_once() == "succeeded"  # the reconciliation, which queues the alert
    assert worker.run_once() == "succeeded"  # the alert
    assert worker.run_once() is None

    assert len(hook.received) == 1 and "new exception" in str(hook.received[0]["text"])
    with world.sessions() as session:
        job = session.scalars(select(Job).where(Job.kind == SEND_ALERTS)).one()
        delivery = session.scalars(select(AlertDelivery)).one()
    assert job.status == "succeeded" and job.result == {
        "sent": 1,
        "duplicate": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert delivery.status == "sent" and delivery.kind == "run"

    # reconciling the same files again is the same run, so the same alert, and nothing more is sent
    with world.sessions.begin() as session:
        enqueue_reconcile(session, world.profile_id, "2026-01", now=datetime.now(UTC))
    worker.run_once()
    worker.run_once()
    assert len(hook.received) == 1

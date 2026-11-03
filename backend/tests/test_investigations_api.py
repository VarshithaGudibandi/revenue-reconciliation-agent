"""Investigating a case through the API: starting, checking, deciding, and resuming later."""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from recon.ai.investigation.service import Investigator
from recon.ai.llm.tools import Message, Step, Tool, ToolCall
from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.main import create_app
from recon.persistence.repository import create_profile, register_file
from recon.pipeline import reconcile_period
from recon.profiles import ProfileConfig

PERIOD = "2026-01"


class Investigating:
    """Looks at the case's customer, then submits a finding about the case's own records."""

    def __init__(
        self,
        action: str = "accept_variance",
        note: str = "I1 was paid by P1 less a fee.",
        cause: str = "processor_fee",
        evidence: tuple[str, ...] = ("I1", "P1"),
    ) -> None:
        self.action = action
        self.note = note
        self.cause = cause
        self.evidence = list(evidence)
        self.calls = 0

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        self.calls += 1
        if len(messages) == 1:
            return Step((ToolCall("customer_records", {"customer": "C1"}),), [], "scripted")
        finding = {
            "cause": self.cause,
            "confidence": "high",
            "evidence": self.evidence,
            "action": self.action,
            "note": self.note,
        }
        return Step((ToolCall("submit_finding", finding),), [], "scripted")


@dataclass
class World:
    url: str
    root: Path
    profile_id: uuid.UUID

    def client(self, model: Any = None) -> TestClient:
        app = create_app(Settings(database_url=self.url, storage_dir=self.root / "storage"))
        if model is not None:
            app.state.investigator = Investigator(model, "scripted")
        return TestClient(app)

    def case(self, client: TestClient, kind: str) -> dict[str, Any]:
        listed = client.get(
            f"/api/profiles/{self.profile_id}/periods/{PERIOD}/cases", params={"kind": kind}
        ).json()["items"]
        return dict(client.get(f"/api/cases/{listed[0]['id']}").json())


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    directory = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=1, invoices=200, causes=True)), directory)
    engine = create_engine(migrated_database_url)
    sessions: sessionmaker[Session] = sessionmaker(engine)
    with sessions.begin() as session:
        profile = create_profile(
            session,
            name="investigated",
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
                period=PERIOD,
                path=directory / f"{source}.csv",
            )
    reconcile_period(sessions, profile_id, PERIOD)
    yield World(migrated_database_url, tmp_path, profile_id)
    engine.dispose()


def test_without_a_model_there_is_no_investigation(world: World) -> None:
    with world.client() as client:
        case = world.case(client, "amount_mismatch")
        assert client.get("/api/capabilities").json()["investigator"] == {
            "name": "none",
            "model": None,
        }
        answer = client.post(f"/api/cases/{case['id']}/investigation")
        assert answer.status_code == 409 and answer.json()["error"]["code"] == "not_applicable"


def test_a_checked_finding_waits_and_approving_it_accepts_the_variance(world: World) -> None:
    with world.client(Investigating()) as client:
        case = world.case(client, "amount_mismatch")
        started = client.post(f"/api/cases/{case['id']}/investigation").json()

        assert started["status"] == "awaiting_decision"
        real_ids = {r["record_id"] for r in case["records"]}
        assert set(started["finding"]["evidence"]) == real_ids
        assert not any(alias in started["finding"]["note"] for alias in ("I1", "P1"))
        assert started["trace"][0]["tool"] == "customer_records"
        assert client.get(f"/api/cases/{case['id']}/investigation").json()["id"] == started["id"]
        assert client.get(f"/api/cases/{case['id']}").json()["status"] == "open"

        decided = client.post(
            f"/api/investigations/{started['id']}/decision",
            json={"approve": True, "note": "fee agreed with the processor"},
        ).json()
        assert decided["status"] == "applied" and decided["outcome"] == "accepted"
        after = client.get(f"/api/cases/{case['id']}").json()
        assert after["status"] == "accepted"
        notes = " ".join(n["body"] for n in after["notes"])
        assert "Investigation by scripted, approved: processor fee" in notes
        assert "fee agreed with the processor" in notes

        again = client.post(f"/api/investigations/{started['id']}/decision", json={"approve": True})
        assert again.status_code == 200 and again.json()["decided_at"] == decided["decided_at"]
        other = client.post(
            f"/api/investigations/{started['id']}/decision", json={"approve": False}
        )
        assert other.status_code == 409


def test_rejecting_a_finding_changes_nothing_about_the_case(world: World) -> None:
    with world.client(Investigating()) as client:
        case = world.case(client, "amount_mismatch")
        started = client.post(f"/api/cases/{case['id']}/investigation").json()
        decided = client.post(
            f"/api/investigations/{started['id']}/decision", json={"approve": False}
        ).json()
        assert decided["status"] == "rejected"
        after = client.get(f"/api/cases/{case['id']}").json()
        assert after["status"] == "open" and after["notes"] == []


def test_a_finding_that_states_an_unseen_figure_is_not_shown_and_waits_for_nothing(
    world: World,
) -> None:
    model = Investigating(note="I1 was paid by P1 less a fee of 98765.43.")
    with world.client(model) as client:
        case = world.case(client, "amount_mismatch")
        started = client.post(f"/api/cases/{case['id']}/investigation").json()
        assert started["status"] == "not_shown"
        assert any("98765.43" in p for p in started["problems"])
        answer = client.post(
            f"/api/investigations/{started['id']}/decision", json={"approve": True}
        )
        assert (
            answer.status_code == 409 and answer.json()["error"]["code"] == "not_awaiting_decision"
        )


def test_a_decision_can_come_after_a_restart(world: World) -> None:
    model = Investigating(
        action="record_finding", cause="not_paid", note="Nothing pays I1.", evidence=("I1",)
    )
    with world.client(model) as client:
        case = world.case(client, "missing_payment")
        started = client.post(f"/api/cases/{case['id']}/investigation").json()
    # A new application, as after a restart: the run is resumed from Postgres, not memory, and
    # the model is not asked again.
    with world.client(Investigating()) as later:
        decided = later.post(
            f"/api/investigations/{started['id']}/decision", json={"approve": True}
        ).json()
        assert decided["status"] == "applied" and decided["outcome"] == "noted"
        assert later.get(f"/api/cases/{case['id']}").json()["status"] == "open"
    assert model.calls == 2


def test_a_kind_without_causes_cannot_be_investigated(world: World) -> None:
    with world.client(Investigating()) as client:
        case = world.case(client, "duplicate_payment")
        answer = client.post(f"/api/cases/{case['id']}/investigation")
        assert answer.status_code == 409

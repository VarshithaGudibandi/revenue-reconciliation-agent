"""Explaining a case with a model: asking, checking, keeping, and showing."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.writer import write_dataset
from recon.explanation.llm import LlmExplainer
from recon.explanation.service import ExplainerChoice
from recon.main import create_app
from recon.mapping.llm_client import Completion
from recon.persistence.models import CaseExplanation
from recon.persistence.repository import create_profile, register_file
from recon.pipeline import reconcile_period
from recon.profiles import ProfileConfig

PERIOD = "2026-01"


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@dataclass
class World:
    url: str
    root: Path
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def client(self) -> TestClient:
        return TestClient(
            create_app(Settings(database_url=self.url, storage_dir=self.root / "storage"))
        )

    def case(self, client: TestClient, kind: str) -> dict[str, Any]:
        listed = client.get(
            f"/api/profiles/{self.profile_id}/periods/{PERIOD}/cases", params={"kind": kind}
        ).json()["items"]
        return dict(client.get(f"/api/cases/{listed[0]['id']}").json())


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING

    directory = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=8, invoices=300)), directory)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile = create_profile(
            session,
            name="explained",
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
    yield World(migrated_database_url, tmp_path, sessions, profile_id)
    engine.dispose()


class Writer:
    """A model that explains faithfully from what it is shown, or invents a figure."""

    def __init__(self, invent: bool = False) -> None:
        self.invent = invent
        self.calls = 0
        self.seen: list[str] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.calls += 1
        self.seen.append(user)
        request = json.loads(user)
        first = request["records"][0]
        figure = "999.99 USD" if self.invent else (request["difference"] or first["amount"])
        answer = {
            "summary": f"[{first['ref']}] is part of this exception.",
            "why": f"The engine's figure is {figure} and [{first['ref']}] is {first['amount']}.",
            "next_steps": [f"Look at [{first['ref']}] in the source system."],
        }
        return Completion(json.dumps(answer), "claude-opus-5-5", 500, 80)


def with_model(client: TestClient, writer: Writer) -> None:
    client.app.state.explainer = ExplainerChoice(  # type: ignore[attr-defined]
        "model, checked", "claude-opus-5-5", LlmExplainer(writer)
    )


def count(world: World, *where: Any) -> int:
    with world.sessions() as session:
        return session.scalar(select(func.count()).select_from(CaseExplanation).where(*where)) or 0


def test_without_a_model_there_is_nothing_to_ask(world: World) -> None:
    with world.client() as client:
        case = world.case(client, "missing_payment")
        response = client.post(f"/api/cases/{case['id']}/explanation")
        capabilities = client.get("/api/capabilities").json()

    assert response.status_code == 200
    assert response.json() == {"explanation": None, "problems": []}
    assert capabilities["explainer"] == {"name": "rules", "model": None}
    assert case["explanation"] is None
    assert count(world) == 0


def test_a_checked_explanation_is_shown_with_real_ids_and_kept(world: World) -> None:
    writer = Writer()
    with world.client() as client:
        with_model(client, writer)
        case = world.case(client, "missing_payment")
        first = client.post(f"/api/cases/{case['id']}/explanation").json()
        again = client.post(f"/api/cases/{case['id']}/explanation").json()
        shown = client.get(f"/api/cases/{case['id']}").json()

    invoice_id = case["records"][0]["record_id"]
    assert first["problems"] == []
    assert first["explanation"]["model"] == "claude-opus-5-5"
    assert first["explanation"]["summary"] == f"{invoice_id} is part of this exception."
    assert "[R" not in json.dumps(first)
    assert again == first
    assert shown["explanation"] == first["explanation"]
    assert writer.calls == 1
    assert count(world, CaseExplanation.verified) == 1


def test_the_model_is_never_sent_a_real_id_or_the_customer(world: World) -> None:
    writer = Writer()
    with world.client() as client:
        with_model(client, writer)
        case = world.case(client, "partial_payment")
        client.post(f"/api/cases/{case['id']}/explanation")

    sent = writer.seen[0]
    assert all(r["record_id"] not in sent for r in case["records"])
    assert all(r["customer"] not in sent for r in case["records"] if r["customer"])


def test_an_invented_figure_is_rejected_and_the_rules_stand(world: World) -> None:
    writer = Writer(invent=True)
    with world.client() as client:
        with_model(client, writer)
        case = world.case(client, "missing_payment")
        response = client.post(f"/api/cases/{case['id']}/explanation").json()
        shown = client.get(f"/api/cases/{case['id']}").json()
        retry = client.post(f"/api/cases/{case['id']}/explanation").json()

    assert response["explanation"] is None
    assert any("999.99" in p for p in response["problems"])
    assert shown["explanation"] is None
    assert shown["analysis"]["summary"]
    # a rejection is kept so it can be counted, but is not reused: asking again asks again
    assert retry["explanation"] is None
    assert writer.calls == 2
    assert count(world, ~CaseExplanation.verified) == 2
    assert count(world, CaseExplanation.verified) == 0


def test_an_unknown_case_is_not_found(world: World) -> None:
    with world.client() as client:
        response = client.post("/api/cases/00000000-0000-0000-0000-000000000000/explanation")

    assert response.status_code == 404

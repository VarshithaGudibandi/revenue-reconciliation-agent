"""Suggesting an invoice for an ambiguous payment, from the files to the case view."""

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

from recon.adjudication.llm import LlmAdjudicator
from recon.adjudication.schema import NONE
from recon.api.suggestions import load_input
from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.model import AnomalyKind, Dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles
from recon.datagen.writer import write_dataset
from recon.main import create_app
from recon.mapping.llm_client import Completion, RecordingJson
from recon.persistence.models import CaseSuggestion
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
    dataset: Dataset
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def client(self, **settings: Any) -> TestClient:
        return TestClient(
            create_app(
                Settings(database_url=self.url, storage_dir=self.root / "storage", **settings)
            )
        )

    def cases(self, client: TestClient, kind: str) -> list[dict[str, Any]]:
        listed = client.get(
            f"/api/profiles/{self.profile_id}/periods/{PERIOD}/cases",
            params={"kind": kind, "limit": 200},
        ).json()["items"]
        return [client.get(f"/api/cases/{c['id']}").json() for c in listed]

    def truth(self) -> dict[str, tuple[str, str]]:
        """For each ambiguous payment: the invoice meant and the kind of memo."""
        return {
            a.payment_ids[0]: (a.intended_invoice_id or "", a.memo_kind or "")
            for a in self.dataset.truth.anomalies
            if a.kind is AnomalyKind.AMBIGUOUS_PAYMENT
        }


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    dataset = generate_dataset(Config(seed=6, invoices=400, ambiguous_rate=0.08))
    directory = tmp_path / "data"
    write_dataset(dataset, directory, ExportStyles("german", "german_bank"))
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    config = ProfileConfig(
        sources={
            "invoices": INVOICE_STYLES["german"].mapping(),
            "payments": PAYMENT_STYLES["german_bank"].mapping(),
        }
    )
    with sessions.begin() as session:
        profile = create_profile(
            session, name="texted", reconciliation_type="invoice_payment", config=config
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
    yield World(migrated_database_url, tmp_path, dataset, sessions, profile_id)
    engine.dispose()


def payment_of(case: dict[str, Any]) -> dict[str, Any]:
    return next(r for r in case["records"] if r["role"] == "payment")


def test_the_suggestion_shown_with_a_case_matches_what_the_payer_meant(world: World) -> None:
    truth = world.truth()
    with world.client() as client:
        cases = world.cases(client, "ambiguous_payment")

    assert len(cases) == len(truth) > 20
    seen = set()
    for case in cases:
        intended, kind = truth[payment_of(case)["record_id"]]
        suggestion = case["suggestion"]
        seen.add(kind)
        assert suggestion["source"] == "rules"
        assert suggestion["model"] is None
        if kind in ("explicit", "descriptive"):
            assert suggestion["invoice_id"] == intended, (kind, payment_of(case)["text"])
            assert suggestion["confident"]
            assert suggestion["evidence"]
        else:
            assert suggestion["invoice_id"] is None
            assert not suggestion["confident"]
    assert seen == {"explicit", "descriptive", "paraphrased", "useless"}


def test_the_records_of_a_case_carry_their_text(world: World) -> None:
    with world.client() as client:
        case = world.cases(client, "ambiguous_payment")[0]

    invoices = [r for r in case["records"] if r["role"] == "invoice"]
    assert all(r["text"] for r in invoices)
    assert len({r["text"] for r in invoices}) == len(invoices)
    assert payment_of(case)["text"] is not None


def test_only_ambiguous_payments_get_a_suggestion(world: World) -> None:
    with world.client() as client:
        others = world.cases(client, "missing_payment")
        response = client.post(f"/api/cases/{others[0]['id']}/suggestion")

    assert others
    assert all(c["suggestion"] is None for c in others)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_applicable"


def test_without_a_model_asking_returns_the_rules_and_keeps_nothing(world: World) -> None:
    with world.client() as client:
        case = world.cases(client, "ambiguous_payment")[0]
        response = client.post(f"/api/cases/{case['id']}/suggestion")

    assert response.status_code == 200
    assert response.json()["source"] == "rules"
    with world.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CaseSuggestion)) == 0


def test_an_unknown_case_is_not_found(world: World) -> None:
    with world.client() as client:
        response = client.post("/api/cases/00000000-0000-0000-0000-000000000000/suggestion")

    assert response.status_code == 404


class Scripted:
    """A model that knows what the payer meant, and says so when told to."""

    def __init__(self, world: World, wrong_for: set[str] | None = None) -> None:
        self.meant = {
            frozenset(int(i.rsplit("-", 1)[1]) for i in a.invoice_ids): a.intended_invoice_id
            for a in world.dataset.truth.anomalies
            if a.kind is AnomalyKind.AMBIGUOUS_PAYMENT
        }
        self.wrong_for = wrong_for or set()
        self.calls = 0

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.calls += 1
        request = json.loads(user)
        numbers = frozenset(c["invoice_number"] for c in request["candidates"])
        intended = int((self.meant[numbers] or "-0").rsplit("-", 1)[1])
        by_number = {c["invoice_number"]: c["label"] for c in request["candidates"]}
        if request["memo"] and request["memo"] not in ("Thank you", "Payment"):
            others = [n for n in numbers if n != intended]
            choice = (
                by_number[others[0]]
                if self.meant[numbers] in self.wrong_for
                else by_number[intended]
            )
            answer = {
                "choice": choice,
                "confidence": "high",
                "evidence": request["memo"],
                "reason": "The memo says so",
            }
        else:
            answer = {"choice": NONE, "confidence": "low", "evidence": "", "reason": "No help"}
        return Completion(json.dumps(answer), "claude-opus-5-5", 400, 60)


def record(world: World, cassette: Path, model: Scripted) -> None:
    adjudicator = LlmAdjudicator(RecordingJson(model, cassette))
    with world.client() as client:
        ids = [c["id"] for c in world.cases(client, "ambiguous_payment")]
    with world.sessions() as session:
        for case_id in ids:
            loaded = load_input(session, uuid.UUID(case_id))
            assert loaded is not None
            adjudicator.adjudicate(loaded.case)
    assert not adjudicator.errors


def paraphrased(world: World, client: TestClient) -> list[dict[str, Any]]:
    truth = world.truth()
    return [
        c
        for c in world.cases(client, "ambiguous_payment")
        if truth[payment_of(c)["record_id"]][1] == "paraphrased"
    ]


def test_the_model_resolves_a_paraphrased_memo_once_and_the_answer_is_kept(
    world: World, tmp_path: Path
) -> None:
    cassette = tmp_path / "answers.jsonl"
    record(world, cassette, Scripted(world))
    truth = world.truth()

    with world.client(llm_replay=cassette, mapper="hybrid") as client:
        case = paraphrased(world, client)[0]
        before = client.get(f"/api/cases/{case['id']}").json()["suggestion"]
        first = client.post(f"/api/cases/{case['id']}/suggestion").json()
        again = client.post(f"/api/cases/{case['id']}/suggestion").json()
        shown = client.get(f"/api/cases/{case['id']}").json()["suggestion"]

    intended = truth[payment_of(case)["record_id"]][0]
    assert before["source"] == "rules"
    assert before["invoice_id"] is None
    assert first["source"] == "rules and model"
    assert first["model"] == "claude-opus-5-5"
    assert first["invoice_id"] == intended
    # the rules could not settle it, so what the model found is a suggestion and says so
    assert not first["confident"]
    assert "Suggested by the model" in first["reasons"][0]
    assert first["evidence"] == payment_of(case)["text"]
    assert again == first == shown
    with world.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CaseSuggestion)) == 1


def test_a_confident_model_that_disagrees_with_a_confident_rule_is_a_conflict(
    world: World, tmp_path: Path
) -> None:
    truth = world.truth()
    explicit = [p for p, (_, kind) in truth.items() if kind == "explicit"]
    meant = {truth[p][0] for p in explicit}
    cassette = tmp_path / "answers.jsonl"
    record(world, cassette, Scripted(world, wrong_for=meant))

    with world.client(llm_replay=cassette, mapper="hybrid") as client:
        case = next(
            c
            for c in world.cases(client, "ambiguous_payment")
            if truth[payment_of(c)["record_id"]][1] == "explicit"
        )
        suggestion = client.post(f"/api/cases/{case['id']}/suggestion").json()

    assert suggestion["invoice_id"] is None
    assert not suggestion["confident"]
    assert len(suggestion["alternatives"]) == 2
    assert "disagree" in suggestion["reasons"][0]


def test_a_payment_the_recording_does_not_cover_falls_back_to_the_rules(
    world: World, tmp_path: Path
) -> None:
    cassette = tmp_path / "answers.jsonl"
    cassette.write_text(
        json.dumps(
            {"key": "0" * 64, "model": "m", "text": "{}", "input_tokens": 1, "output_tokens": 1}
        )
        + "\n"
    )

    with world.client(llm_replay=cassette, mapper="hybrid") as client:
        explicit = next(
            c
            for c in world.cases(client, "ambiguous_payment")
            if world.truth()[payment_of(c)["record_id"]][1] == "explicit"
        )
        suggestion = client.post(f"/api/cases/{explicit['id']}/suggestion").json()

    intended = world.truth()[payment_of(explicit)["record_id"]][0]
    assert suggestion["invoice_id"] == intended
    assert suggestion["confident"]


def test_the_interface_can_ask_who_makes_the_suggestions(world: World, tmp_path: Path) -> None:
    with world.client() as client:
        plain = client.get("/api/capabilities").json()
    cassette = tmp_path / "answers.jsonl"
    record(world, cassette, Scripted(world))
    with world.client(llm_replay=cassette) as client:
        both = client.get("/api/capabilities").json()

    assert plain == {
        "mapper": {"name": "rules", "model": None},
        "adjudicator": {"name": "rules", "model": None},
        "explainer": {"name": "rules", "model": None},
    }
    assert both["adjudicator"] == {"name": "rules and model", "model": "claude-opus-5-5"}
    assert both["mapper"]["name"] == "rules and model"


def test_the_model_is_not_asked_twice_for_the_same_input(world: World) -> None:
    from recon.adjudication.case import AdjudicationCase, Ruling
    from recon.adjudication.service import AdjudicatorChoice
    from recon.api.suggestions import ask_adjudicator

    asked: list[str] = []

    def counting(case: AdjudicationCase) -> Ruling:
        asked.append(case.payment_id)
        return Ruling(case.candidates[0].label, True, case.memo, ("counted",))

    choice = AdjudicatorChoice("rules and model", "test-model", counting)
    with world.client() as client:
        case_id = uuid.UUID(world.cases(client, "ambiguous_payment")[0]["id"])

    first = ask_adjudicator(world.sessions, case_id, choice)
    second = ask_adjudicator(world.sessions, case_id, choice)

    assert len(asked) == 1
    assert first == second
    assert first.model == "test-model"


def test_what_an_adjudicator_reads_is_the_same_every_time_so_the_keep_is_found_again(
    world: World,
) -> None:
    with world.client() as client:
        case_id = uuid.UUID(world.cases(client, "ambiguous_payment")[0]["id"])
    with world.sessions() as session:
        first, second = load_input(session, case_id), load_input(session, case_id)

    assert first is not None
    assert first == second
    labels = [c.label for c in first.case.candidates]
    assert labels == sorted(labels)
    assert len(first.input_hash) == 64

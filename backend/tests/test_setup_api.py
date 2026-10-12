"""Setting up a reconciliation over HTTP: propose, preview, correct, confirm."""

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.styles import INVOICE_STYLES, PAYMENT_STYLES, ExportStyles
from recon.datagen.writer import write_dataset
from recon.main import create_app
from recon.mapping.llm import LlmMapper
from recon.mapping.llm_client import Completion, RecordingJson
from recon.mapping.profile import profile_csv_bytes
from recon.mapping.service import build_mapper
from tests.test_mapping_llm import answer_for

PERIOD = "2026-01"


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@dataclasses.dataclass
class Files:
    directory: Path
    styles: ExportStyles

    def upload(self) -> dict[str, Any]:
        return {
            "files": {
                "invoices": ("invoices.csv", (self.directory / "invoices.csv").read_bytes()),
                "payments": ("payments.csv", (self.directory / "payments.csv").read_bytes()),
            },
            "data": {"period": PERIOD},
        }

    def mapping(self) -> dict[str, Any]:
        return {
            "invoices": INVOICE_STYLES[self.styles.invoices].mapping().model_dump(mode="json"),
            "payments": PAYMENT_STYLES[self.styles.payments].mapping().model_dump(mode="json"),
        }


def make_files(
    root: Path,
    invoice_style: str = "default",
    payment_style: str = "default",
    **dataset_changes: Any,
) -> Files:
    dataset = generate_dataset(Config(seed=2, invoices=120))
    if dataset_changes:
        dataset = dataclasses.replace(dataset, **dataset_changes)
    styles = ExportStyles(invoice_style, payment_style)
    write_dataset(dataset, root, styles)
    return Files(root, styles)


def client_for(url: str, tmp_path: Path, **settings: Any) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                database_url=url, storage_dir=tmp_path / "storage", max_upload_mb=1, **settings
            )
        )
    )


@pytest.fixture
def client(migrated_database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with client_for(migrated_database_url, tmp_path) as test_client:
        yield test_client


def propose(client: TestClient, files: Files) -> dict[str, Any]:
    response = client.post("/api/setup/proposals", **files.upload())
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def field(proposal: dict[str, Any], source: str, name: str) -> dict[str, Any]:
    return next(f for f in proposal[source]["fields"] if f["field"] == name)


def test_two_sample_files_get_a_proposal_that_says_who_made_it(
    client: TestClient, tmp_path: Path
) -> None:
    proposal = propose(client, make_files(tmp_path / "f", "german", "bank_statement"))

    assert proposal["status"] == "proposed"
    assert proposal["mapper"] == "rules"
    assert proposal["model"] is None
    assert proposal["period"] == PERIOD
    assert proposal["invoices"]["file_name"] == "invoices.csv"
    assert proposal["invoices"]["rows"] == 120
    assert "Bruttobetrag" in proposal["invoices"]["headers"]
    assert field(proposal, "invoices", "amount")["column"] == "Bruttobetrag"
    assert field(proposal, "invoices", "amount")["confident"]
    assert proposal["invoices"]["decimal_separator"]["value"] == ","
    assert field(proposal, "payments", "reference")["column"] == "Remittance Info"
    assert proposal["invoices"]["needs_answer"] == 0
    assert proposal["payments"]["needs_answer"] == 0
    assert "%d.%m.%Y" in proposal["date_formats"]


def test_a_date_order_the_values_cannot_settle_is_a_question(
    client: TestClient, tmp_path: Path
) -> None:
    dataset = generate_dataset(Config(seed=2, invoices=120))
    early = tuple(i for i in dataset.invoices if i.issue_date.day <= 12)
    files = make_files(tmp_path / "f", "spanish", "default", invoices=early)

    proposal = propose(client, files)

    date_format = proposal["invoices"]["date_format"]
    assert not date_format["confident"]
    assert date_format["value"] is None
    assert set(date_format["alternatives"]) == {"%d/%m/%Y", "%m/%d/%Y"}
    assert proposal["invoices"]["needs_answer"] >= 1


def test_a_proposal_can_be_read_again(client: TestClient, tmp_path: Path) -> None:
    proposal = propose(client, make_files(tmp_path / "f"))

    again = client.get(f"/api/setup/proposals/{proposal['id']}")

    assert again.status_code == 200
    assert again.json() == proposal
    assert (
        client.get("/api/setup/proposals/00000000-0000-0000-0000-000000000000").status_code == 404
    )


@pytest.mark.parametrize(
    ("period", "invoices", "message"),
    [
        ("2026-13", b"a,b\n1,2\n", "period"),
        ("2026-01", b"", "empty"),
        ("2026-01", b"a,a\n1,2\n", "distinct"),
        ("2026-01", b"\xff\xfe\x00\x00", "UTF-8"),
    ],
)
def test_unusable_uploads_are_refused_and_leave_nothing_behind(
    client: TestClient, tmp_path: Path, period: str, invoices: bytes, message: str
) -> None:
    files = make_files(tmp_path / "f")

    response = client.post(
        "/api/setup/proposals",
        files={
            "invoices": ("i.csv", invoices),
            "payments": ("p.csv", (files.directory / "payments.csv").read_bytes()),
        },
        data={"period": period},
    )

    assert response.status_code == 422
    assert message in response.json()["error"]["message"]
    storage = tmp_path / "storage"
    assert not storage.exists() or not [p for p in storage.rglob("*.csv")]


def test_a_file_over_the_limit_is_refused(client: TestClient, tmp_path: Path) -> None:
    files = make_files(tmp_path / "f")
    big = b"a,b\n" + b"1,2\n" * 400_000

    response = client.post(
        "/api/setup/proposals",
        files={
            "invoices": ("i.csv", big),
            "payments": ("p.csv", (files.directory / "payments.csv").read_bytes()),
        },
        data={"period": PERIOD},
    )

    assert response.status_code == 413


def test_the_preview_shows_how_a_mapping_reads_the_file(client: TestClient, tmp_path: Path) -> None:
    files = make_files(tmp_path / "f", "german", "german_bank")
    proposal = propose(client, files)

    preview = client.post(f"/api/setup/proposals/{proposal['id']}/preview", json=files.mapping())

    body = preview.json()
    assert preview.status_code == 200
    assert body["ready"]
    assert body["invoices"]["records_read"] == 120
    assert body["invoices"]["rejected"] == 0
    assert len(body["invoices"]["sample"]) == 8
    first = body["invoices"]["sample"][0]
    assert first["amount"]["currency"] in {"EUR", "USD"}
    assert first["occurred_on"].startswith("2026-")


def test_a_wrong_setting_shows_up_as_rejected_rows_not_as_wrong_numbers(
    client: TestClient, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "german", "default")
    proposal = propose(client, files)
    mapping = files.mapping()
    mapping["invoices"]["decimal_separator"] = "."

    body = client.post(f"/api/setup/proposals/{proposal['id']}/preview", json=mapping).json()

    assert body["invoices"]["records_read"] == 0
    assert body["invoices"]["rejected"] == 120
    assert body["invoices"]["rejections"][0]["reason"]
    assert not body["ready"]


def test_a_mapping_naming_a_missing_column_is_reported_not_crashed(
    client: TestClient, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f")
    proposal = propose(client, files)
    mapping = files.mapping()
    mapping["payments"]["amount"] = "no such column"

    body = client.post(f"/api/setup/proposals/{proposal['id']}/preview", json=mapping).json()

    assert "no such column" in body["payments"]["error"]
    assert not body["ready"]


def confirm(client: TestClient, proposal: dict[str, Any], files: Files, name: str = "Main") -> Any:
    return client.post(
        f"/api/setup/proposals/{proposal['id']}/confirm", json={"name": name, **files.mapping()}
    )


def test_confirming_creates_the_profile_and_reconciles_the_first_period(
    client: TestClient, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "sap", "stripe_like")
    proposal = propose(client, files)

    response = confirm(client, proposal, files)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"]
    assert body["profile"]["name"] == "Main"
    assert body["records"] == {"invoices": 120, "payments": body["records"]["payments"]}
    assert body["run"]["status"] == "succeeded"
    summary = client.get(f"/api/profiles/{body['profile']['id']}/periods/{PERIOD}/summary").json()
    assert summary["state"] == "reconciled"
    assert summary["invoices"] == 120
    assert summary["needs_attention"] > 0
    assert client.get(f"/api/setup/proposals/{proposal['id']}").json()["status"] == "confirmed"


def test_confirming_twice_returns_the_same_profile(client: TestClient, tmp_path: Path) -> None:
    files = make_files(tmp_path / "f")
    proposal = propose(client, files)

    first = confirm(client, proposal, files).json()
    second = confirm(client, proposal, files)

    assert second.status_code == 200
    assert second.json()["profile"] == first["profile"]
    assert not second.json()["created"]
    assert len(client.get("/api/profiles").json()) == 1


def test_a_confirmed_proposal_cannot_be_confirmed_differently(
    client: TestClient, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f")
    proposal = propose(client, files)
    confirm(client, proposal, files)

    other_name = confirm(client, proposal, files, name="Other")

    assert other_name.status_code == 409
    assert other_name.json()["error"]["code"] == "proposal_confirmed"


def test_a_profile_name_can_only_be_used_once(client: TestClient, tmp_path: Path) -> None:
    files = make_files(tmp_path / "f")
    first = propose(client, files)
    second = propose(client, files)
    confirm(client, first, files)

    clash = confirm(client, second, files)

    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "profile_exists"
    assert client.get(f"/api/setup/proposals/{second['id']}").json()["status"] == "proposed"


def test_a_mapping_that_reads_nothing_creates_no_profile(
    client: TestClient, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "german")
    proposal = propose(client, files)
    mapping = files.mapping()
    mapping["invoices"]["decimal_separator"] = "."

    response = client.post(
        f"/api/setup/proposals/{proposal['id']}/confirm", json={"name": "Main", **mapping}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unusable_mapping"
    assert client.get("/api/profiles").json() == []
    assert client.get(f"/api/setup/proposals/{proposal['id']}").json()["status"] == "proposed"


def test_a_confirmation_needs_a_name(client: TestClient, tmp_path: Path) -> None:
    files = make_files(tmp_path / "f")
    proposal = propose(client, files)

    response = client.post(
        f"/api/setup/proposals/{proposal['id']}/confirm", json={"name": "", **files.mapping()}
    )

    assert response.status_code == 422


def test_the_database_ties_a_proposal_status_to_its_profile(
    migrated_database_url: str,
) -> None:
    engine = create_engine(migrated_database_url)
    insert = text(
        "INSERT INTO mapping_proposal (period, mapper, invoices_key, payments_key, "
        "invoices_name, payments_name, proposal, status) "
        "VALUES ('2026-01', 'rules', 'a', 'b', 'a', 'b', '{}', :status)"
    )
    with engine.begin() as connection:
        connection.execute(insert, {"status": "proposed"})
    for status in ("confirmed", "abandoned"):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, {"status": status})
    engine.dispose()


def test_without_credentials_the_rules_are_used_and_a_required_model_is_refused(
    migrated_database_url: str, tmp_path: Path
) -> None:
    settings = Settings(database_url=migrated_database_url, storage_dir=tmp_path)

    assert build_mapper(settings).name == "rules"
    with pytest.raises(ValueError, match="RECON_MAPPER=rules"):
        build_mapper(
            Settings(database_url=migrated_database_url, storage_dir=tmp_path, mapper="hybrid")
        )


def test_with_credentials_the_model_is_added_to_the_rules(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")

    choice = build_mapper(Settings(database_url=migrated_database_url, storage_dir=tmp_path))

    assert choice.name == "rules and model"
    assert choice.model == "claude-opus-5-5"


class ScriptedModel:
    """Answers for a set of files from their true mappings, optionally changing one field."""

    def __init__(self, mappings: dict[str, Any], change: dict[str, Any] | None = None) -> None:
        self.mappings = mappings
        self.change = change or {}

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        role = json.loads(user)["file_role"]
        answer = answer_for(self.mappings[role], role, **self.change.get(role, {}))
        return Completion(json.dumps(answer), "claude-opus-5-5", 900, 150)


def record_answers(files: Files, cassette: Path, change: dict[str, Any] | None = None) -> None:
    from recon.ingestion.normalize import ColumnMapping

    mappings = {
        "invoices": INVOICE_STYLES[files.styles.invoices].mapping(),
        "payments": PAYMENT_STYLES[files.styles.payments].mapping(),
    }
    assert all(isinstance(m, ColumnMapping) for m in mappings.values())
    mapper = LlmMapper(RecordingJson(ScriptedModel(mappings, change), cassette))
    for role in ("invoices", "payments"):
        profile = profile_csv_bytes(f"{role}.csv", (files.directory / f"{role}.csv").read_bytes())
        # the service profiles the stored copy, named after its file in the proposal folder
        mapper.propose_file(profile, role)
    assert not mapper.errors


def test_recorded_model_answers_are_replayed_without_a_key(
    migrated_database_url: str, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "italian", "dutch_bank")
    cassette = tmp_path / "answers.jsonl"
    record_answers(files, cassette)

    with client_for(migrated_database_url, tmp_path, llm_replay=cassette) as client:
        proposal = propose(client, files)

    assert proposal["mapper"] == "rules and model"
    assert proposal["model"] == "claude-opus-5-5"
    assert field(proposal, "invoices", "amount")["column"] == "Importo totale"
    assert field(proposal, "invoices", "amount")["confident"]
    assert proposal["invoices"]["needs_answer"] == 0


def test_a_confident_model_that_disagrees_with_a_confident_rule_is_a_conflict(
    migrated_database_url: str, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "german", "default")
    cassette = tmp_path / "answers.jsonl"
    net = {
        "invoices": {
            "amount": {
                "column": "Nettobetrag",
                "confidence": "high",
                "alternatives": [],
                "reason": "net",
            }
        }
    }
    record_answers(files, cassette, net)

    with client_for(migrated_database_url, tmp_path, llm_replay=cassette) as client:
        proposal = propose(client, files)

    amount = field(proposal, "invoices", "amount")
    # the rules read the gross amount from the payments, the model says net with confidence:
    # nothing is chosen for the user, and both are offered
    assert amount["column"] is None
    assert not amount["confident"]
    assert set(amount["alternatives"]) == {"Bruttobetrag", "Nettobetrag"}
    assert "disagree" in amount["reasons"][0]
    assert proposal["invoices"]["needs_answer"] >= 1


def test_an_unsure_model_does_not_overrule_a_confident_rule(
    migrated_database_url: str, tmp_path: Path
) -> None:
    files = make_files(tmp_path / "f", "german", "default")
    cassette = tmp_path / "answers.jsonl"
    unsure = {
        "invoices": {
            "amount": {
                "column": "Nettobetrag",
                "confidence": "low",
                "alternatives": [],
                "reason": "guess",
            }
        }
    }
    record_answers(files, cassette, unsure)

    with client_for(migrated_database_url, tmp_path, llm_replay=cassette) as client:
        proposal = propose(client, files)

    amount = field(proposal, "invoices", "amount")
    assert amount["column"] == "Bruttobetrag"
    assert amount["confident"]
    assert "Nettobetrag" in amount["alternatives"]


def test_a_replay_with_nothing_recorded_for_these_files_falls_back_to_the_rules(
    migrated_database_url: str, tmp_path: Path
) -> None:
    other = make_files(tmp_path / "other", "erp_english", "default")
    cassette = tmp_path / "answers.jsonl"
    record_answers(other, cassette)
    files = make_files(tmp_path / "f", "german", "default")

    with client_for(migrated_database_url, tmp_path, llm_replay=cassette) as client:
        proposal = propose(client, files)

    assert field(proposal, "invoices", "amount")["column"] == "Bruttobetrag"
    assert field(proposal, "invoices", "amount")["confident"]


def test_simultaneous_confirmations_create_one_profile(client: TestClient, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    files = make_files(tmp_path / "f")
    proposal = propose(client, files)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: confirm(client, proposal, files), range(6)))

    assert [r.status_code for r in responses] == [200] * 6
    assert sum(1 for r in responses if r.json()["created"]) == 1
    assert len({r.json()["profile"]["id"] for r in responses}) == 1
    assert len(client.get("/api/profiles").json()) == 1

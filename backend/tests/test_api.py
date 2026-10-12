"""The HTTP interface, over a real database and the real pipeline."""

import threading
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from recon.api.explain import RecordFacts, explain
from recon.config import Settings
from recon.domain.money import Money
from recon.main import create_app
from recon.pipeline import reconcile_period
from tests.conftest import PERIOD, Env, make_env
from tests.test_incremental_runs import Rows, rewrite


@dataclass
class Api:
    env: Env
    client: TestClient
    storage: Path

    @property
    def period_url(self) -> str:
        return f"/api/profiles/{self.env.profile_id}/periods/{PERIOD}"

    def settle(self, revision: int = 0) -> None:
        self.env.register("invoices", 0)
        self.env.register("payments", revision)
        reconcile_period(self.env.sessions, self.env.profile_id, PERIOD)

    def cases(self, **params: Any) -> dict[str, Any]:
        response = self.client.get(f"{self.period_url}/cases", params={"limit": 200, **params})
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    def first_case(self, **params: Any) -> dict[str, Any]:
        return self.cases(**params)["items"][0]  # type: ignore[no-any-return]


@pytest.fixture
def api(migrated_database_url: str, tmp_path: Path) -> Iterator[Api]:
    with make_env(migrated_database_url, tmp_path / "data", seed=14) as env:
        settings = Settings(
            database_url=migrated_database_url, storage_dir=tmp_path / "storage", max_upload_mb=1
        )
        with TestClient(create_app(settings)) as client:
            yield Api(env, client, tmp_path / "storage")


def test_profiles_and_periods_are_listed(api: Api) -> None:
    api.settle()

    profiles = api.client.get("/api/profiles").json()
    periods = api.client.get(f"/api/profiles/{api.env.profile_id}/periods").json()

    assert [p["name"] for p in profiles] == ["demo"]
    assert [p["period"] for p in periods] == [PERIOD]
    assert {f["source"] for f in periods[0]["files"]} == {"invoices", "payments"}


def test_the_summary_matches_the_ground_truth(api: Api) -> None:
    api.settle()

    summary = api.client.get(f"{api.period_url}/summary").json()

    truth = api.env.first.truth
    assert summary["state"] == "reconciled"
    assert summary["invoices"] == len(api.env.first.invoices)
    assert summary["matched_invoices"] == len(truth.matches)
    assert summary["needs_attention"] == len(truth.anomalies)
    assert summary["reconciliation_rate"] == pytest.approx(len(truth.matches) / summary["invoices"])
    by_kind = {row["kind"]: row["count"] for row in summary["by_kind"]}
    assert by_kind == dict(Counter(a.kind.value for a in truth.anomalies))
    assert summary["run"]["mode"] == "full"
    assert summary["cases_up_to_date"] is True
    # Totals are absolute net differences; a misreferenced payment nets to zero.
    assert all(a["minor"] >= 0 for row in summary["by_kind"] for a in row["amounts"])
    stakes = {row["kind"]: [a["minor"] for a in row["amounts"]] for row in summary["by_kind"]}
    assert all(m == 0 for m in stakes["misreferenced_payment"])
    assert any(m > 0 for m in stakes["missing_payment"])


def test_the_summary_reports_where_a_period_stands(api: Api) -> None:
    url = f"{api.period_url}/summary"
    assert api.client.get(url).json()["state"] == "no_files"

    api.env.register("invoices", 0)
    assert api.client.get(url).json()["state"] == "waiting_for_files"

    api.env.register("payments", 0)
    assert api.client.get(url).json()["state"] == "not_reconciled"

    reconcile_period(api.env.sessions, api.env.profile_id, PERIOD)
    assert api.client.get(url).json()["state"] == "reconciled"


def test_matches_are_paged_and_searchable(api: Api) -> None:
    api.settle()
    truth = api.env.first.truth

    page = api.client.get(f"{api.period_url}/matches", params={"limit": 10}).json()
    second = api.client.get(f"{api.period_url}/matches", params={"limit": 10, "offset": 10}).json()
    found = api.client.get(
        f"{api.period_url}/matches", params={"q": page["items"][0]["invoice_id"]}
    ).json()

    assert page["total"] == len(truth.matches)
    assert len(page["items"]) == 10 and len(second["items"]) == 10
    assert not {m["invoice_id"] for m in page["items"]} & {m["invoice_id"] for m in second["items"]}
    assert found["total"] >= 1
    row = page["items"][0]
    assert row["variance"]["minor"] == 0
    assert row["paid"]["minor"] == row["amount"]["minor"]
    assert row["amount"]["display"].endswith(row["amount"]["currency"])


def test_cases_can_be_filtered_sorted_and_searched(api: Api) -> None:
    api.settle()
    everything = api.cases()
    truth = api.env.first.truth

    assert everything["total"] == len(truth.anomalies)
    missing = api.cases(kind="missing_payment")
    assert {c["kind"] for c in missing["items"]} == {"missing_payment"}
    assert missing["total"] == sum(a.kind.value == "missing_payment" for a in truth.anomalies)
    assert api.cases(status="resolved")["total"] == 0
    assert api.cases(kind=["missing_payment", "amount_mismatch"])["total"] > missing["total"]

    by_amount = [abs(c["variance"]["minor"]) for c in everything["items"] if c["variance"]]
    assert by_amount == sorted(by_amount, reverse=True)
    numbers = [c["number"] for c in api.cases(sort="number")["items"]]
    assert numbers == sorted(numbers)

    case = everything["items"][0]
    for term in (
        case["customer"],
        (case["invoice_ids"] or case["payment_ids"])[0],
        f"#{case['number']}",
    ):
        assert case["id"] in {c["id"] for c in api.cases(q=term)["items"]}, term
    assert api.cases(q="no-such-thing")["total"] == 0


def test_paging_cases_does_not_repeat_or_skip(api: Api) -> None:
    api.settle()
    seen: list[str] = []
    for offset in range(0, 60, 20):
        page = api.cases(limit=20, offset=offset, sort="number")
        seen.extend(c["id"] for c in page["items"])

    assert len(seen) == len(set(seen)) == min(60, api.cases()["total"])


def test_a_case_has_records_an_analysis_and_a_history(api: Api) -> None:
    api.settle()
    row = api.first_case(kind="amount_mismatch")

    detail = api.client.get(f"/api/cases/{row['id']}").json()

    assert detail["number"] == row["number"] and detail["status"] == "open"
    assert [r["role"] for r in detail["records"]] == ["invoice", "payment"]
    assert all(r["in_current_files"] and r["amount"] for r in detail["records"])
    assert detail["analysis"]["summary"] and detail["analysis"]["next_steps"]
    assert detail["analysis"]["why"].count("USD") + detail["analysis"]["why"].count("EUR") >= 2
    assert [e["kind"] for e in detail["events"]] == ["opened"]
    assert detail["notes"] == []


def test_every_case_kind_has_an_analysis(api: Api) -> None:
    api.settle()
    for kind in {c["kind"] for c in api.cases()["items"]}:
        row = api.first_case(kind=kind)
        detail = api.client.get(f"/api/cases/{row['id']}").json()
        assert detail["analysis"]["summary"].strip(), kind
        assert detail["analysis"]["next_steps"], kind


def test_the_explainer_states_the_numbers_it_was_given() -> None:
    facts = [
        RecordFacts("invoice", "INV-1", "CUST-1", Money(100_000, "USD")),
        RecordFacts("payment", "PAY-1", "CUST-1", Money(90_000, "USD")),
    ]

    result = explain("amount_mismatch", Money(10_000, "USD"), facts, None)

    assert result.summary == "Invoice INV-1 is paid 100.00 USD short."
    assert "1,000.00 USD" in result.why and "900.00 USD" in result.why


def test_reporting_a_fix_changes_the_case_and_returns_it(api: Api) -> None:
    api.settle()
    row = api.first_case(kind="amount_mismatch")

    response = api.client.post(
        f"/api/cases/{row['id']}/report-fix", json={"expected_version": row["version"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fix_reported" and body["version"] > row["version"]
    assert [e["kind"] for e in body["events"]] == ["opened", "fix_reported"]


def test_acting_on_a_stale_version_is_a_conflict_that_names_the_current_one(api: Api) -> None:
    api.settle()
    row = api.first_case()
    url = f"/api/cases/{row['id']}"
    api.client.post(f"{url}/report-fix", json={})
    api.client.post(f"{url}/retract-fix", json={})

    response = api.client.post(f"{url}/report-fix", json={"expected_version": row["version"]})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "stale_case" and error["current_version"] > row["version"]


def test_an_action_that_makes_no_sense_is_a_conflict(api: Api) -> None:
    api.settle()
    row = api.first_case()
    api.client.post(f"/api/cases/{row['id']}/report-fix", json={})

    response = api.client.post(f"/api/cases/{row['id']}/reopen", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"
    # Repeating an action whose result already holds is not a conflict.
    assert api.client.post(f"/api/cases/{row['id']}/report-fix", json={}).status_code == 200


def test_accepting_needs_a_valid_reason_and_a_note(api: Api) -> None:
    api.settle()
    base = f"/api/cases/{api.first_case()['id']}"
    url = f"{base}/accept"

    assert api.client.post(url, json={"reason": "write_off"}).status_code == 422
    assert api.client.post(url, json={"reason": "write_off", "note": ""}).status_code == 422
    assert api.client.post(url, json={"reason": "nonsense", "note": "x"}).status_code == 422
    blank = api.client.post(url, json={"reason": "write_off", "note": "   "})
    assert blank.status_code == 422 and blank.json()["error"]["code"] == "invalid_request"

    ok = api.client.post(url, json={"reason": "write_off", "note": "customer refunded"})
    assert ok.status_code == 200
    body = ok.json()
    assert (body["status"], body["accepted_reason"]) == ("accepted", "write_off")
    assert [n["body"] for n in body["notes"]] == ["customer refunded"]
    assert api.client.post(f"{base}/reopen", json={}).json()["status"] == "open"


def test_notes_can_be_added(api: Api) -> None:
    api.settle()
    url = f"/api/cases/{api.first_case()['id']}/notes"

    first = api.client.post(url, json={"body": "asked billing"}).json()
    second = api.client.post(url, json={"body": "they will reply Monday"}).json()

    assert [n["body"] for n in second["notes"]] == ["asked billing", "they will reply Monday"]
    assert second["version"] == first["version"]
    assert api.client.post(url, json={"body": ""}).status_code == 422


def test_unknown_things_are_not_found_with_the_same_error_shape(api: Api) -> None:
    missing = "00000000-0000-0000-0000-000000000000"

    for response in (
        api.client.get(f"/api/cases/{missing}"),
        api.client.post(f"/api/cases/{missing}/report-fix", json={}),
        api.client.get(f"/api/profiles/{missing}/periods"),
        api.client.get(f"/api/profiles/{missing}/periods/{PERIOD}/summary"),
    ):
        assert response.status_code == 404, response.url
        assert response.json()["error"]["code"] == "not_found"
    assert api.client.get("/api/cases/not-a-uuid").status_code == 422
    assert (
        api.client.get(f"/api/profiles/{api.env.profile_id}/periods/2026-13/summary").status_code
        == 422
    )
    assert api.client.get(f"{api.period_url}/cases", params={"limit": 0}).status_code == 422
    assert api.client.get(f"{api.period_url}/cases", params={"sort": "colour"}).status_code == 422


def test_a_retry_with_the_same_key_gets_the_same_answer_and_acts_once(api: Api) -> None:
    api.settle()
    case_id = api.first_case()["id"]
    url = f"/api/cases/{case_id}/notes"
    headers = {"Idempotency-Key": "retry-1"}

    first = api.client.post(url, json={"body": "asked billing"}, headers=headers)
    again = api.client.post(url, json={"body": "asked billing"}, headers=headers)

    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert len(api.client.get(f"/api/cases/{case_id}").json()["notes"]) == 1
    # Without a key the same request is a second note: a note is not naturally idempotent,
    # which is what the key is for.
    api.client.post(url, json={"body": "asked billing"})
    assert len(api.client.get(f"/api/cases/{case_id}").json()["notes"]) == 2


def test_a_key_cannot_be_reused_for_a_different_request(api: Api) -> None:
    api.settle()
    cases = api.cases()["items"]
    headers = {"Idempotency-Key": "shared-key"}
    ok = api.client.post(f"/api/cases/{cases[0]['id']}/report-fix", json={}, headers=headers)

    clash = api.client.post(f"/api/cases/{cases[1]['id']}/report-fix", json={}, headers=headers)

    assert ok.status_code == 200
    assert clash.status_code == 422 and clash.json()["error"]["code"] == "idempotency_key"
    assert api.client.get(f"/api/cases/{cases[1]['id']}").json()["status"] == "open"


def test_a_failed_request_does_not_burn_its_key(api: Api) -> None:
    api.settle()
    row = api.first_case()
    api.client.post(f"/api/cases/{row['id']}/report-fix", json={})
    url = f"/api/cases/{row['id']}/reopen"  # only an accepted case can be reopened
    headers = {"Idempotency-Key": "second-chance"}

    failed = api.client.post(url, json={}, headers=headers)
    api.client.post(f"/api/cases/{row['id']}/accept", json={"reason": "other", "note": "n"})
    retried = api.client.post(url, json={}, headers=headers)

    assert failed.status_code == 409
    assert retried.status_code == 200 and retried.json()["status"] == "open"


def test_malformed_keys_are_refused(api: Api) -> None:
    api.settle()
    url = f"/api/cases/{api.first_case()['id']}/report-fix"

    assert api.client.post(url, json={}, headers={"Idempotency-Key": "x" * 300}).status_code == 422


def test_concurrent_requests_with_one_key_act_once(api: Api) -> None:
    api.settle()
    case_id = api.first_case()["id"]
    url = f"/api/cases/{case_id}/notes"
    barrier = threading.Barrier(6)

    def send(_: int) -> dict[str, Any]:
        barrier.wait()
        response = api.client.post(
            url, json={"body": "only once"}, headers={"Idempotency-Key": "burst"}
        )
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    with ThreadPoolExecutor(6) as pool:
        bodies = list(pool.map(send, range(6)))

    assert all(body == bodies[0] for body in bodies)
    assert len(api.client.get(f"/api/cases/{case_id}").json()["notes"]) == 1


def upload(api: Api, source: str, path: Path, **kwargs: Any) -> Any:
    with path.open("rb") as handle:
        return api.client.post(
            f"{api.period_url}/files/{source}",
            files={"file": (path.name, handle, "text/csv")},
            **kwargs,
        )


def test_uploading_files_registers_them_and_reconciles_once_both_are_present(api: Api) -> None:
    first = upload(api, "invoices", api.env.directories[0] / "invoices.csv")
    assert first.status_code == 200
    assert (first.json()["created"], first.json()["reconciled"], first.json()["run"]) == (
        True,
        False,
        None,
    )

    second = upload(api, "payments", api.env.directories[0] / "payments.csv")
    body = second.json()
    assert body["reconciled"] and body["run"]["status"] == "succeeded"
    assert body["cases"]["applied"] and body["cases"]["opened"] == len(
        api.env.first.truth.anomalies
    )
    assert api.client.get(f"{api.period_url}/summary").json()["state"] == "reconciled"


def test_uploading_a_corrected_file_resolves_cases_incrementally(api: Api) -> None:
    api.settle()
    before = api.cases()["total"]

    response = upload(api, "payments", api.env.directories[1] / "payments.csv")

    body = response.json()
    assert body["created"] and body["file"]["version_number"] == 2
    assert body["run"]["mode"] == "incremental"
    assert body["cases"]["resolved"] > 0
    assert api.cases(status="resolved")["total"] == body["cases"]["resolved"]
    assert api.cases(status=["open", "fix_reported"])["total"] < before


def test_uploading_the_same_content_again_changes_nothing(api: Api) -> None:
    api.settle()
    path = api.env.directories[0] / "payments.csv"

    again = upload(api, "payments", path)

    assert again.status_code == 200 and again.json()["created"] is False
    stored = [p for p in api.storage.rglob("*.csv")]
    assert len(stored) == 0, "a duplicate upload must not be kept"


def test_a_file_that_cannot_be_read_is_refused_and_leaves_nothing(api: Api, tmp_path: Path) -> None:
    api.settle()
    bad = tmp_path / "bad.csv"
    bad.write_text("payment_id,reference\nP1,INV-1\n")

    response = upload(api, "payments", bad)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unreadable_file"
    assert not list(api.storage.rglob("*.csv"))
    files = api.client.get(f"/api/profiles/{api.env.profile_id}/periods").json()[0]["files"]
    assert {f["source"]: f["version_number"] for f in files} == {"invoices": 1, "payments": 1}


def test_upload_requests_are_checked(api: Api, tmp_path: Path) -> None:
    path = api.env.directories[0] / "invoices.csv"

    assert upload(api, "refunds", path).status_code == 404
    assert (
        api.client.post(
            f"/api/profiles/{api.env.profile_id}/periods/2026-13/files/invoices",
            files={"file": ("x.csv", b"a,b\n", "text/csv")},
        ).status_code
        == 422
    )
    assert api.client.post(f"{api.period_url}/files/invoices").status_code == 422

    big = tmp_path / "big.csv"
    big.write_bytes(b"x" * (1024 * 1024 + 10))
    response = upload(api, "invoices", big)
    assert response.status_code == 413
    assert not list(api.storage.rglob("*.csv"))


def test_the_demo_endpoints_are_absent_unless_the_demo_is_on(api: Api) -> None:
    assert api.client.get("/api/demo").status_code == 404
    assert api.client.post("/api/demo/load-corrected-export").status_code == 404


def test_the_demo_can_be_seeded_and_corrected(migrated_database_url: str, tmp_path: Path) -> None:
    settings = Settings(database_url=migrated_database_url, storage_dir=tmp_path, demo=True)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/demo").json()["profile_id"] is None
        assert client.post("/api/demo/load-corrected-export").status_code == 409

        from recon.demo import DEMO_PERIOD, seed_demo

        seed_demo(client.app.state.sessions, tmp_path)  # type: ignore[attr-defined]
        state = client.get("/api/demo").json()
        assert state["corrected_files_loaded"] is False
        base = f"/api/profiles/{state['profile_id']}/periods/{DEMO_PERIOD}"
        before = client.get(f"{base}/summary").json()
        assert before["state"] == "reconciled" and before["needs_attention"] > 20

        loaded = client.post("/api/demo/load-corrected-export").json()
        assert loaded["run"]["mode"] == "incremental" and loaded["cases"]["resolved"] > 0
        assert client.get("/api/demo").json()["corrected_files_loaded"] is True
        after = client.get(f"{base}/summary").json()
        assert after["needs_attention"] < before["needs_attention"]
        # Seeding again is harmless.
        seed_demo(client.app.state.sessions, tmp_path)  # type: ignore[attr-defined]


def test_the_openapi_description_covers_every_endpoint(api: Api) -> None:
    paths = set(api.client.get("/openapi.json").json()["paths"])

    assert {
        "/api/profiles",
        "/api/profiles/{profile_id}/periods",
        "/api/profiles/{profile_id}/periods/{period}/summary",
        "/api/profiles/{profile_id}/periods/{period}/matches",
        "/api/profiles/{profile_id}/periods/{period}/cases",
        "/api/profiles/{profile_id}/periods/{period}/files/{source}",
        "/api/cases/{case_id}",
        "/api/cases/{case_id}/report-fix",
        "/api/cases/{case_id}/retract-fix",
        "/api/cases/{case_id}/accept",
        "/api/cases/{case_id}/reopen",
        "/api/cases/{case_id}/notes",
    } <= paths


def test_resolving_edits_in_the_source_show_up_through_the_api(api: Api, tmp_path: Path) -> None:
    """The whole loop a person goes through: see a problem, report a fix, load new data."""
    api.settle()
    row = api.first_case(kind="amount_mismatch")
    detail = api.client.get(f"/api/cases/{row['id']}").json()
    payment_id = next(r["record_id"] for r in detail["records"] if r["role"] == "payment")
    invoice_amount = next(r["amount"] for r in detail["records"] if r["role"] == "invoice")
    api.client.post(f"/api/cases/{row['id']}/report-fix", json={})

    def correct(rows: Rows) -> Rows:
        for r in rows:
            if r["payment_id"] == payment_id:
                r["amount_paid"] = f"{invoice_amount['minor'] / 100:,.2f}"
        return rows

    fixed = tmp_path / "fixed" / "payments.csv"
    rewrite(api.env.directories[0] / "payments.csv", fixed, correct)
    response = upload(api, "payments", fixed)

    assert response.json()["run"]["partitions_recomputed"] == 1
    after = api.client.get(f"/api/cases/{row['id']}").json()
    assert after["status"] == "resolved" and after["resolved_at"]
    assert [e["kind"] for e in after["events"]] == ["opened", "fix_reported", "resolved"]

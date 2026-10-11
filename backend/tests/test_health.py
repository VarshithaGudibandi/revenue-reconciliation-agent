from fastapi.testclient import TestClient

from recon.config import Settings
from recon.main import create_app


def test_health_reports_ok(database_url: str) -> None:
    client = TestClient(create_app(Settings(database_url=database_url)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ok_when_database_is_reachable(database_url: str) -> None:
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_ready_reports_unavailable_when_database_is_unreachable() -> None:
    unreachable = "postgresql+psycopg://recon:recon@127.0.0.1:1/recon"
    with TestClient(create_app(Settings(database_url=unreachable))) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}

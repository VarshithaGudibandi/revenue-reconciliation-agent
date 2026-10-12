"""Deleting what is finished with."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from recon.config import Settings
from recon.jobs import queue
from recon.mapping.proposal import MappingProposal
from recon.persistence.models import IdempotencyRecord, Job, MappingProposalRow
from recon.persistence.setup import create_proposal
from recon.retention import purge

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
HASH = "a" * 64


def test_only_what_is_old_and_finished_is_deleted(
    migrated_database_url: str, tmp_path: Path
) -> None:
    settings = Settings(database_url=migrated_database_url, storage_dir=tmp_path)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)

    with sessions.begin() as session:

        def job(key: str, finished_days_ago: float | None, status: str) -> uuid.UUID:
            made, _ = queue.enqueue(session, "work", key, {}, now=NOW)
            if status == "succeeded":
                made.status, made.result = "succeeded", {}
                made.finished_at = NOW - timedelta(days=finished_days_ago or 0)
            elif status == "failed":
                made.status, made.last_error = "failed", "boom"
                made.finished_at = NOW - timedelta(days=finished_days_ago or 0)
            session.flush()
            return made.id

        old_done = job("old-done", 30, "succeeded")
        old_failed = job("old-failed", 30, "failed")
        recent_done = job("recent-done", 2, "succeeded")
        waiting = job("waiting", None, "queued")

        for key, days in (("old-key", 30), ("new-key", 1)):
            session.add(
                IdempotencyRecord(
                    key=key,
                    request_hash=HASH,
                    status_code=200,
                    response={},
                    created_at=NOW - timedelta(days=days),
                )
            )
        for days in (30, 1):
            folder = f"proposals/p{days}"
            (tmp_path / folder).mkdir(parents=True)
            (tmp_path / folder / "invoices.csv").write_text("a\n")
            row = create_proposal(
                session,
                period="2026-01",
                mapper="rules",
                model=None,
                keys={"invoices": f"{folder}/invoices.csv", "payments": f"{folder}/payments.csv"},
                names={"invoices": "i.csv", "payments": "p.csv"},
                proposal=MappingProposal.model_validate({"invoices": {}, "payments": {}}),
            )
            session.flush()
            session.execute(
                text("UPDATE mapping_proposal SET created_at = :at WHERE id = :id"),
                {"at": NOW - timedelta(days=days), "id": row.id},
            )

    counts = purge(sessions, tmp_path, settings, now=NOW)

    with sessions() as session:
        left = set(session.scalars(select(Job.id)))
        keys = set(session.scalars(select(IdempotencyRecord.key)))
        proposals = session.scalar(select(func.count()).select_from(MappingProposalRow))
    engine.dispose()
    assert counts == {"jobs": 2, "idempotency_records": 1, "alert_deliveries": 0, "proposals": 1}
    assert left == {recent_done, waiting} and old_done not in left and old_failed not in left
    assert keys == {"new-key"} and proposals == 1
    assert not (tmp_path / "proposals/p30").exists() and (tmp_path / "proposals/p1").exists()


def test_running_it_again_deletes_nothing_more(migrated_database_url: str, tmp_path: Path) -> None:
    settings = Settings(database_url=migrated_database_url, storage_dir=tmp_path)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)

    assert purge(sessions, tmp_path, settings, now=NOW) == {
        "jobs": 0,
        "idempotency_records": 0,
        "alert_deliveries": 0,
        "proposals": 0,
    }
    engine.dispose()

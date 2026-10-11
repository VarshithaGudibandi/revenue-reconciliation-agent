import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.model import Dataset
from recon.datagen.writer import write_dataset
from recon.persistence.repository import create_profile, register_file
from recon.persistence.runs import get_or_create_run
from recon.profiles import ProfileConfig


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of a Postgres server the tests may create scratch databases on."""
    url = os.environ.get("RECON_DATABASE_URL")
    if not url:
        pytest.fail("RECON_DATABASE_URL must point at a running Postgres server")
    return url


@pytest.fixture
def scratch_database_url(database_url: str) -> Iterator[str]:
    """An empty database created for one test and dropped afterwards."""
    name = f"recon_test_{uuid.uuid4().hex[:12]}"
    base = make_url(database_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def migrated_database_url(scratch_database_url: str) -> str:
    """A scratch database migrated to the latest revision."""
    config = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", scratch_database_url)
    command.upgrade(config, "head")
    return scratch_database_url


@pytest.fixture
def session(migrated_database_url: str) -> Iterator[Session]:
    engine = create_engine(migrated_database_url)
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


PERIOD = "2026-01"


@dataclass
class Env:
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID
    first: Dataset
    second: Dataset
    directories: tuple[Path, Path]

    def register(self, source: str, revision: int) -> None:
        with self.sessions.begin() as session:
            register_file(
                session,
                profile_id=self.profile_id,
                source_name=source,
                period=PERIOD,
                path=self.directories[revision] / f"{source}.csv",
            )

    def register_path(self, source: str, path: Path) -> None:
        with self.sessions.begin() as session:
            register_file(
                session,
                profile_id=self.profile_id,
                source_name=source,
                period=PERIOD,
                path=path,
            )

    def run_id(self) -> uuid.UUID:
        with self.sessions.begin() as session:
            return get_or_create_run(session, profile_id=self.profile_id, period=PERIOD).run.id


@contextmanager
def make_env(database_url: str, root: Path, seed: int) -> Iterator[Env]:
    first = generate_dataset(Config(seed=seed, invoices=200, ambiguous_rate=0.04))
    second = apply_corrections(first, seed=seed)
    write_dataset(first, root / "v1")
    write_dataset(second, root / "v2")

    engine = create_engine(database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile_id = create_profile(
            session,
            name="demo",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(
                sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
            ),
        ).id
    try:
        yield Env(sessions, profile_id, first, second, (root / "v1", root / "v2"))
    finally:
        engine.dispose()


@pytest.fixture
def env(migrated_database_url: str, tmp_path: Path) -> Iterator[Env]:
    with make_env(migrated_database_url, tmp_path, seed=14) as environment:
        yield environment


def count(env: Env, model: type) -> int:
    with env.sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0

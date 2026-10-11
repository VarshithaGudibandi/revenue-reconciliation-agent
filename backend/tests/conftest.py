import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


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
def session(scratch_database_url: str) -> Iterator[Session]:
    """A session on a scratch database migrated to the latest revision."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", scratch_database_url)
    command.upgrade(config, "head")

    engine = create_engine(scratch_database_url)
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()

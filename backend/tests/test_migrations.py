from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def current_revisions(url: str) -> list[str]:
    engine = create_engine(url)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT version_num FROM alembic_version"))
        revisions = [row[0] for row in rows]
    engine.dispose()
    return revisions


def test_upgrade_to_head_records_the_revision(scratch_database_url: str) -> None:
    command.upgrade(alembic_config(scratch_database_url), "head")

    assert current_revisions(scratch_database_url) == ["0001"]


def test_upgrade_is_repeatable(scratch_database_url: str) -> None:
    config = alembic_config(scratch_database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert current_revisions(scratch_database_url) == ["0001"]


def test_downgrade_to_base_clears_the_revision(scratch_database_url: str) -> None:
    config = alembic_config(scratch_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert current_revisions(scratch_database_url) == []

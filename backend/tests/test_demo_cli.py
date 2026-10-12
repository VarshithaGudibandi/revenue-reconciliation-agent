"""Starting with and without sample data."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from recon.demo import main
from recon.persistence.models import Profile


def profiles(url: str) -> int:
    engine = create_engine(url)
    with sessionmaker(engine)() as session:
        count = session.scalar(select(func.count()).select_from(Profile)) or 0
    engine.dispose()
    return count


@pytest.fixture
def stack(migrated_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("RECON_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("RECON_STORAGE_DIR", str(tmp_path))
    return migrated_database_url


def test_with_demo_mode_off_the_stack_starts_empty(
    stack: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RECON_DEMO", "false")

    main(["--only-in-demo-mode"])

    assert profiles(stack) == 0
    assert "no sample data loaded" in capsys.readouterr().out


def test_with_demo_mode_on_both_sample_profiles_are_loaded(
    stack: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECON_DEMO", "true")

    main(["--only-in-demo-mode"])
    main(["--only-in-demo-mode"])

    assert profiles(stack) == 2


def test_asked_for_by_hand_it_loads_the_sample_data_whatever_the_mode(
    stack: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECON_DEMO", raising=False)

    main([])

    assert profiles(stack) == 2

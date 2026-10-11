import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.ingestion.normalize import MappingError, normalize_csv
from recon.persistence.models import SourceFileVersion, SourceRecord
from recon.persistence.repository import (
    ProfileExistsError,
    UnknownProfileError,
    UnknownSourceError,
    create_profile,
    latest_file_versions,
    load_records,
    load_rejected,
    register_file,
)
from recon.profiles import EngineSettings, ProfileConfig

PROFILE_CONFIG = ProfileConfig(sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING})


def make_profile(session: Session, name: str = "demo") -> uuid.UUID:
    return create_profile(
        session, name=name, reconciliation_type="invoice_payment", config=PROFILE_CONFIG
    ).id


@pytest.fixture
def exports(tmp_path: Path) -> tuple[Path, Path]:
    """The generator's first and corrected revisions, written as two directories."""
    first = generate_dataset(Config(seed=5, invoices=120))
    write_dataset(first, tmp_path / "v1")
    write_dataset(apply_corrections(first, seed=5), tmp_path / "v2")
    return tmp_path / "v1", tmp_path / "v2"


def test_a_profile_stores_its_configuration(session: Session) -> None:
    profile = create_profile(
        session,
        name="demo",
        reconciliation_type="invoice_payment",
        config=ProfileConfig(
            sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING},
            engine=EngineSettings(tolerance_minor=5),
        ),
    )
    session.flush()
    session.refresh(profile)

    assert ProfileConfig.model_validate(profile.config).engine_config().tolerance_minor == 5


def test_profile_names_must_be_unique(session: Session) -> None:
    make_profile(session, "demo")

    with pytest.raises(ProfileExistsError):
        make_profile(session, "demo")
    # The failed attempt did not poison the session.
    make_profile(session, "other")


def test_a_profile_needs_a_known_type_and_its_sources(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown reconciliation type"):
        create_profile(session, name="a", reconciliation_type="bank", config=PROFILE_CONFIG)
    with pytest.raises(ValueError, match="reads sources"):
        create_profile(
            session,
            name="b",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(sources={"invoices": INVOICE_MAPPING}),
        )


def test_invalid_engine_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfileConfig(sources={}, engine=EngineSettings(partial_threshold_bps=20_000))


def test_the_config_fingerprint_changes_with_any_setting() -> None:
    changed = ProfileConfig(
        sources=PROFILE_CONFIG.sources, engine=EngineSettings(pairing_window_days=10)
    )

    assert PROFILE_CONFIG.fingerprint() == PROFILE_CONFIG.fingerprint()
    assert changed.fingerprint() != PROFILE_CONFIG.fingerprint()


def test_registering_a_file_stores_its_records(
    session: Session, exports: tuple[Path, Path]
) -> None:
    profile_id = make_profile(session)
    path = exports[0] / "payments.csv"

    registration = register_file(
        session, profile_id=profile_id, source_name="payments", period="2026-01", path=path
    )

    expected = normalize_csv(path, PAYMENT_MAPPING)
    version = registration.version
    assert registration.created
    assert version.version_number == 1
    assert version.record_count == len(expected.records)
    assert version.size_bytes == path.stat().st_size
    assert version.object_key == "payments.csv"
    assert load_records(session, version.id) == list(expected.records)


def test_registering_the_same_content_again_changes_nothing(
    session: Session, exports: tuple[Path, Path]
) -> None:
    profile_id = make_profile(session)
    path = exports[0] / "invoices.csv"
    first = register_file(
        session, profile_id=profile_id, source_name="invoices", period="2026-01", path=path
    )
    again = register_file(
        session, profile_id=profile_id, source_name="invoices", period="2026-01", path=path
    )

    assert (again.created, again.version.id) == (False, first.version.id)
    assert session.scalar(select(func.count()).select_from(SourceFileVersion)) == 1
    assert (
        session.scalar(select(func.count()).select_from(SourceRecord)) == first.version.record_count
    )


def test_changed_content_becomes_a_new_version_and_the_old_one_is_kept(
    session: Session, exports: tuple[Path, Path]
) -> None:
    profile_id = make_profile(session)
    first = register_file(
        session,
        profile_id=profile_id,
        source_name="payments",
        period="2026-01",
        path=exports[0] / "payments.csv",
    )
    second = register_file(
        session,
        profile_id=profile_id,
        source_name="payments",
        period="2026-01",
        path=exports[1] / "payments.csv",
    )

    assert second.created
    assert second.version.version_number == 2
    assert load_records(session, first.version.id) == list(
        normalize_csv(exports[0] / "payments.csv", PAYMENT_MAPPING).records
    )
    assert load_records(session, second.version.id) == list(
        normalize_csv(exports[1] / "payments.csv", PAYMENT_MAPPING).records
    )


def test_reverting_to_earlier_content_is_a_new_latest_version(
    session: Session, exports: tuple[Path, Path]
) -> None:
    profile_id = make_profile(session)
    versions = [
        register_file(
            session,
            profile_id=profile_id,
            source_name="payments",
            period="2026-01",
            path=exports[index] / "payments.csv",
        ).version
        for index in (0, 1, 0)
    ]

    assert [v.version_number for v in versions] == [1, 2, 3]
    assert versions[2].content_sha256 == versions[0].content_sha256
    latest = latest_file_versions(session, profile_id, "2026-01")
    assert latest["payments"].id == versions[2].id


def test_latest_versions_are_per_source_and_period(
    session: Session, exports: tuple[Path, Path]
) -> None:
    profile_id = make_profile(session)
    for source, directory in (("invoices", 0), ("payments", 0), ("payments", 1)):
        register_file(
            session,
            profile_id=profile_id,
            source_name=source,
            period="2026-01",
            path=exports[directory] / f"{source}.csv",
        )
    register_file(
        session,
        profile_id=profile_id,
        source_name="invoices",
        period="2026-02",
        path=exports[0] / "invoices.csv",
    )

    january = latest_file_versions(session, profile_id, "2026-01")
    february = latest_file_versions(session, profile_id, "2026-02")

    assert {name: v.version_number for name, v in january.items()} == {"invoices": 1, "payments": 2}
    assert set(february) == {"invoices"}
    assert latest_file_versions(session, profile_id, "2026-03") == {}


def test_rejected_rows_are_stored_with_their_reasons(session: Session, tmp_path: Path) -> None:
    profile_id = make_profile(session)
    path = tmp_path / "payments.csv"
    path.write_text(
        "payment_id,reference,customer,received_on,ccy,amount_paid\n"
        "P1,INV-1,C1,01/05/2026,USD,10.00\n"
        "P2,INV-2,C1,01/05/2026,USD,12.345\n",
        encoding="utf-8",
    )

    version = register_file(
        session, profile_id=profile_id, source_name="payments", period="2026-01", path=path
    ).version

    rejected = load_rejected(session, version.id)
    assert (version.record_count, version.rejected_count) == (1, 1)
    assert [(r.source_row, r.raw["payment_id"]) for r in rejected] == [(2, "P2")]
    assert "precision" in rejected[0].reason


def test_a_file_that_cannot_be_read_leaves_nothing_behind(session: Session, tmp_path: Path) -> None:
    profile_id = make_profile(session)
    path = tmp_path / "payments.csv"
    path.write_text("payment_id,reference\nP1,INV-1\n", encoding="utf-8")

    with pytest.raises(MappingError):
        register_file(
            session, profile_id=profile_id, source_name="payments", period="2026-01", path=path
        )

    assert session.scalar(select(func.count()).select_from(SourceFileVersion)) == 0


def test_registration_rejects_bad_requests(session: Session, exports: tuple[Path, Path]) -> None:
    profile_id = make_profile(session)
    path = exports[0] / "invoices.csv"

    with pytest.raises(ValueError, match="YYYY-MM"):
        register_file(
            session, profile_id=profile_id, source_name="invoices", period="2026-13", path=path
        )
    with pytest.raises(UnknownSourceError):
        register_file(
            session, profile_id=profile_id, source_name="refunds", period="2026-01", path=path
        )
    with pytest.raises(UnknownProfileError):
        register_file(
            session, profile_id=uuid.uuid4(), source_name="invoices", period="2026-01", path=path
        )


def test_a_record_hash_ignores_row_position_but_not_content(tmp_path: Path) -> None:
    header = "invoice_id,customer_id,issue_date,currency,total\n"
    one, two = "INV-1,C1,2026-01-05,USD,10.00\n", "INV-2,C1,2026-01-06,USD,20.00\n"
    first, reordered, changed = (tmp_path / n for n in ("a.csv", "b.csv", "c.csv"))
    first.write_text(header + one + two, encoding="utf-8")
    reordered.write_text(header + two + one, encoding="utf-8")
    changed.write_text(header + one + "INV-2,C1,2026-01-06,USD,20.01\n", encoding="utf-8")

    hashes = [
        {r.record_id: r.content_hash for r in normalize_csv(path, INVOICE_MAPPING).records}
        for path in (first, reordered, changed)
    ]

    assert hashes[0] == hashes[1]
    assert hashes[0]["INV-1"] == hashes[2]["INV-1"]
    assert hashes[0]["INV-2"] != hashes[2]["INV-2"]


def test_concurrent_registrations_of_one_file_create_one_version(
    migrated_database_url: str, exports: tuple[Path, Path]
) -> None:
    engine = create_engine(migrated_database_url)
    with Session(engine) as setup:
        profile_id = make_profile(setup)
        setup.commit()
    barrier = threading.Barrier(8)

    def register(_: int) -> tuple[uuid.UUID, bool]:
        with Session(engine) as worker:
            barrier.wait()
            result = register_file(
                worker,
                profile_id=profile_id,
                source_name="invoices",
                period="2026-01",
                path=exports[0] / "invoices.csv",
            )
            worker.commit()
            return result.version.id, result.created

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(register, range(8)))

    assert len({version_id for version_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    engine.dispose()


def test_concurrent_registrations_of_different_content_get_distinct_versions(
    migrated_database_url: str, tmp_path: Path
) -> None:
    engine = create_engine(migrated_database_url)
    with Session(engine) as setup:
        profile_id = make_profile(setup)
        setup.commit()
    header = "invoice_id,customer_id,issue_date,currency,total\n"
    paths = []
    for index in range(6):
        path = tmp_path / f"invoices_{index}.csv"
        path.write_text(header + f"INV-1,C1,2026-01-05,USD,{index + 1}.00\n", encoding="utf-8")
        paths.append(path)
    barrier = threading.Barrier(len(paths))

    def register(path: Path) -> int:
        with Session(engine) as worker:
            barrier.wait()
            result = register_file(
                worker, profile_id=profile_id, source_name="invoices", period="2026-01", path=path
            )
            worker.commit()
            return result.version.version_number

    with ThreadPoolExecutor(len(paths)) as pool:
        numbers = list(pool.map(register, paths))

    assert sorted(numbers) == [1, 2, 3, 4, 5, 6]
    engine.dispose()

import uuid
from datetime import date

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recon.persistence.models import (
    Profile,
    ResultException,
    Run,
    SourceFileVersion,
    SourceRecord,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def add_profile(session: Session, name: str = "demo") -> Profile:
    profile = Profile(name=name, reconciliation_type="invoice_payment", config={})
    session.add(profile)
    session.flush()
    return profile


def add_version(
    session: Session,
    profile: Profile,
    *,
    sha: str = SHA_A,
    number: int = 1,
    period: str = "2026-01",
) -> SourceFileVersion:
    version = SourceFileVersion(
        profile_id=profile.id,
        source_name="invoices",
        period=period,
        version_number=number,
        content_sha256=sha,
        object_key="invoices.csv",
        size_bytes=10,
        record_count=0,
        rejected_count=0,
    )
    session.add(version)
    session.flush()
    return version


def add_record(session: Session, version: SourceFileVersion, record_id: str = "INV-1") -> None:
    session.add(
        SourceRecord(
            file_version_id=version.id,
            row_number=1,
            record_id=record_id,
            reference=None,
            counterparty="C1",
            occurred_on=date(2026, 1, 5),
            amount_minor=1000,
            currency="USD",
            content_hash=SHA_A,
        )
    )
    session.flush()


def test_database_generates_ids_and_timestamps(session: Session) -> None:
    profile = add_profile(session)

    assert isinstance(profile.id, uuid.UUID)
    assert profile.created_at is not None


def test_profile_names_are_unique(session: Session) -> None:
    add_profile(session, "demo")

    with pytest.raises(IntegrityError):
        add_profile(session, "demo")


def test_the_same_content_may_appear_in_several_versions(session: Session) -> None:
    profile = add_profile(session)
    add_version(session, profile, sha=SHA_A, number=1)
    add_version(session, profile, sha=SHA_B, number=2)

    add_version(session, profile, sha=SHA_A, number=3)


def test_version_numbers_are_unique_per_source_and_period(session: Session) -> None:
    profile = add_profile(session)
    add_version(session, profile, sha=SHA_A, number=1)

    with pytest.raises(IntegrityError):
        add_version(session, profile, sha=SHA_B, number=1)


def test_a_new_period_starts_again_at_version_one(session: Session) -> None:
    profile = add_profile(session)
    add_version(session, profile, sha=SHA_A, number=1, period="2026-01")

    add_version(session, profile, sha=SHA_A, number=1, period="2026-02")


@pytest.mark.parametrize("period", ["2026-13", "2026-1", "202601", "January"])
def test_periods_must_look_like_year_and_month(session: Session, period: str) -> None:
    profile = add_profile(session)

    with pytest.raises(IntegrityError):
        add_version(session, profile, period=period)


def test_hashes_must_be_lowercase_sha256_hex(session: Session) -> None:
    profile = add_profile(session)

    with pytest.raises(IntegrityError):
        add_version(session, profile, sha="A" * 64)


def test_a_record_id_is_unique_within_a_file_version(session: Session) -> None:
    profile = add_profile(session)
    version = add_version(session, profile)
    add_record(session, version, "INV-1")

    with pytest.raises(IntegrityError):
        add_record(session, version, "INV-1")


def test_the_same_record_id_may_appear_in_another_version(session: Session) -> None:
    profile = add_profile(session)
    add_record(session, add_version(session, profile, sha=SHA_A, number=1), "INV-1")

    add_record(session, add_version(session, profile, sha=SHA_B, number=2), "INV-1")


def test_deleting_a_file_version_deletes_its_records(session: Session) -> None:
    profile = add_profile(session)
    version = add_version(session, profile)
    add_record(session, version)

    session.execute(delete(SourceFileVersion).where(SourceFileVersion.id == version.id))

    assert session.scalar(select(func.count()).select_from(SourceRecord)) == 0


def test_currency_codes_must_be_three_capital_letters(session: Session) -> None:
    profile = add_profile(session)
    version = add_version(session, profile)

    with pytest.raises(IntegrityError):
        session.add(
            SourceRecord(
                file_version_id=version.id,
                row_number=1,
                record_id="INV-1",
                counterparty="C1",
                occurred_on=date(2026, 1, 5),
                amount_minor=1,
                currency="usd",
                content_hash=SHA_A,
            )
        )
        session.flush()


def test_a_run_defaults_to_pending_and_is_unique_per_inputs(session: Session) -> None:
    profile = add_profile(session)
    run = Run(
        profile_id=profile.id,
        period="2026-01",
        input_hash=SHA_A,
        config_hash=SHA_B,
        config_snapshot={},
    )
    session.add(run)
    session.flush()
    session.refresh(run)

    assert run.status == "pending"
    with pytest.raises(IntegrityError):
        session.add(
            Run(
                profile_id=profile.id,
                period="2026-01",
                input_hash=SHA_A,
                config_hash=SHA_B,
                config_snapshot={},
            )
        )
        session.flush()


def test_a_run_status_must_be_known(session: Session) -> None:
    profile = add_profile(session)

    with pytest.raises(IntegrityError):
        session.add(
            Run(
                profile_id=profile.id,
                period="2026-01",
                input_hash=SHA_A,
                config_hash=SHA_B,
                config_snapshot={},
                status="done",
            )
        )
        session.flush()


def test_a_variance_needs_a_currency_and_the_reverse(session: Session) -> None:
    profile = add_profile(session)
    run = Run(
        profile_id=profile.id,
        period="2026-01",
        input_hash=SHA_A,
        config_hash=SHA_B,
        config_snapshot={},
    )
    session.add(run)
    session.flush()

    session.add(ResultException(run_id=run.id, kind="x", variance_minor=None, currency=None))
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(ResultException(run_id=run.id, kind="x", variance_minor=5, currency=None))
        session.flush()

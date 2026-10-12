"""Sample data for trying the application: a profile, a month of invoices and payments
with known problems, and a corrected export to load afterwards.

Everything is generated, so nothing here is real financial data.

    python -m recon.demo        # create the demo profile and load the first export
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.db import create_db_engine
from recon.persistence.models import Profile
from recon.persistence.repository import create_profile, latest_file_versions, register_file
from recon.pipeline import PeriodOutcome, reconcile_period
from recon.profiles import ProfileConfig

DEMO_PROFILE = "Demo: invoices and payments"
DEMO_PERIOD = "2026-01"
DEMO_SEED = 7


@dataclass(frozen=True)
class DemoState:
    profile_id: uuid.UUID | None
    corrected_files_loaded: bool


def demo_directories(storage_dir: Path) -> tuple[Path, Path]:
    return storage_dir / "demo" / "first", storage_dir / "demo" / "corrected"


def _write_files(storage_dir: Path) -> tuple[Path, Path]:
    first_dir, corrected_dir = demo_directories(storage_dir)
    first = generate_dataset(
        Config(seed=DEMO_SEED, period=DEMO_PERIOD, invoices=400, ambiguous_rate=0.03)
    )
    write_dataset(first, first_dir)
    write_dataset(apply_corrections(first, seed=DEMO_SEED), corrected_dir)
    return first_dir, corrected_dir


def _find_profile(session: Session) -> Profile | None:
    return session.scalars(select(Profile).where(Profile.name == DEMO_PROFILE)).first()


def demo_state(sessions: sessionmaker[Session], storage_dir: Path) -> DemoState:
    with sessions() as session:
        profile = _find_profile(session)
        if profile is None:
            return DemoState(None, False)
        versions = latest_file_versions(session, profile.id, DEMO_PERIOD)
        payments = versions.get("payments")
        corrected = demo_directories(storage_dir)[1] / "payments.csv"
        loaded = (
            payments is not None
            and corrected.exists()
            and payments.object_key.startswith("demo/corrected/")
        )
        return DemoState(profile.id, loaded)


def seed_demo(sessions: sessionmaker[Session], storage_dir: Path) -> uuid.UUID:
    """Create the demo profile and load the first export. Safe to run again."""
    first_dir, _ = _write_files(storage_dir)
    with sessions.begin() as session:
        profile = _find_profile(session)
        if profile is None:
            profile = create_profile(
                session,
                name=DEMO_PROFILE,
                reconciliation_type="invoice_payment",
                config=ProfileConfig(
                    sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
                ),
            )
        profile_id = profile.id
        if not latest_file_versions(session, profile_id, DEMO_PERIOD):
            for source in ("invoices", "payments"):
                register_file(
                    session,
                    profile_id=profile_id,
                    source_name=source,
                    period=DEMO_PERIOD,
                    path=first_dir / f"{source}.csv",
                    object_key=f"demo/first/{source}.csv",
                )
    reconcile_period(sessions, profile_id, DEMO_PERIOD)
    return profile_id


def load_corrected_export(
    sessions: sessionmaker[Session], storage_dir: Path, profile_id: uuid.UUID
) -> PeriodOutcome:
    """Load the corrected payments export, as if the source system had been fixed."""
    _, corrected_dir = demo_directories(storage_dir)
    if not (corrected_dir / "payments.csv").exists():
        _write_files(storage_dir)
    with sessions.begin() as session:
        register_file(
            session,
            profile_id=profile_id,
            source_name="payments",
            period=DEMO_PERIOD,
            path=corrected_dir / "payments.csv",
            object_key="demo/corrected/payments.csv",
        )
    return reconcile_period(sessions, profile_id, DEMO_PERIOD)


def main() -> None:
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    profile_id = seed_demo(sessionmaker(engine), settings.storage_dir)
    print(f"demo profile {profile_id}, period {DEMO_PERIOD}")
    engine.dispose()


if __name__ == "__main__":
    main()

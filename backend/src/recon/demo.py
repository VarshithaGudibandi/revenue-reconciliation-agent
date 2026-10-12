"""Sample data for trying the application: a profile, a month of invoices and payments
with known problems, and a corrected export to load afterwards.

Everything is generated, so nothing here is real financial data.

    python -m recon.demo        # create the demo profile and load the first export
"""

import argparse
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recon.config import Settings
from recon.datagen.batches import (
    DEPOSIT_MAPPING,
    PAYOUT_MAPPING,
    BatchConfig,
    generate_batches,
    write_batches,
)
from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.db import create_db_engine
from recon.persistence.models import Profile
from recon.persistence.repository import create_profile, latest_file_versions, register_file
from recon.pipeline import PeriodOutcome, reconcile_period
from recon.profiles import ProfileConfig

DEMO_PROFILE = "Demo: invoices and payments"
DEMO_BATCH_PROFILE = "Demo: payouts and deposits"
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
    # Real reconciliations are mostly clean, so the demo seeds far fewer problems than the
    # generator's defaults: about one invoice in ten does not reconcile.
    first = generate_dataset(
        Config(
            seed=DEMO_SEED,
            period=DEMO_PERIOD,
            invoices=400,
            missing_rate=0.02,
            amount_mismatch_rate=0.015,
            duplicate_rate=0.01,
            partial_rate=0.01,
            misreferenced_rate=0.01,
            currency_mismatch_rate=0.005,
            ambiguous_rate=0.01,
            unmatched_payment_rate=0.01,
        )
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


def seed_batch_demo(sessions: sessionmaker[Session], storage_dir: Path) -> uuid.UUID:
    """Create a second demo profile, bank deposits against processor payouts, and reconcile it.
    Safe to run again."""
    directory = storage_dir / "demo" / "batches"
    write_batches(
        generate_batches(BatchConfig(seed=DEMO_SEED, period=DEMO_PERIOD, deposits=120)), directory
    )
    with sessions.begin() as session:
        profile = session.scalars(select(Profile).where(Profile.name == DEMO_BATCH_PROFILE)).first()
        if profile is None:
            profile = create_profile(
                session,
                name=DEMO_BATCH_PROFILE,
                reconciliation_type="payout_deposit",
                config=ProfileConfig(
                    sources={"deposits": DEPOSIT_MAPPING, "payouts": PAYOUT_MAPPING}
                ),
            )
        profile_id = profile.id
        if not latest_file_versions(session, profile_id, DEMO_PERIOD):
            for source in ("deposits", "payouts"):
                register_file(
                    session,
                    profile_id=profile_id,
                    source_name=source,
                    period=DEMO_PERIOD,
                    path=directory / f"{source}.csv",
                    object_key=f"demo/batches/{source}.csv",
                )
    reconcile_period(sessions, profile_id, DEMO_PERIOD)
    return profile_id


def register_corrected_export(
    sessions: sessionmaker[Session], storage_dir: Path, profile_id: uuid.UUID
) -> None:
    """Store the corrected payments export as a new version of the payments file."""
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


def load_corrected_export(
    sessions: sessionmaker[Session], storage_dir: Path, profile_id: uuid.UUID
) -> PeriodOutcome:
    """Load the corrected payments export, as if the source system had been fixed."""
    register_corrected_export(sessions, storage_dir, profile_id)
    return reconcile_period(sessions, profile_id, DEMO_PERIOD)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m recon.demo")
    parser.add_argument(
        "--only-in-demo-mode",
        action="store_true",
        help="do nothing unless RECON_DEMO is true, so that a stack can start with no sample data",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    if args.only_in_demo_mode and not settings.demo:
        print("demo mode is off (RECON_DEMO=false); no sample data loaded")
        return
    engine = create_db_engine(settings.database_url)
    sessions = sessionmaker(engine)
    profile_id = seed_demo(sessions, settings.storage_dir)
    print(f"demo profile {profile_id}, period {DEMO_PERIOD}")
    batch_id = seed_batch_demo(sessions, settings.storage_dir)
    print(f"demo profile {batch_id} (deposits and payouts), period {DEMO_PERIOD}")
    engine.dispose()


if __name__ == "__main__":
    main()

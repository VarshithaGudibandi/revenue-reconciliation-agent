"""Run a job worker: python -m recon.worker"""

import logging
import signal
import threading
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from recon.config import Settings
from recon.db import create_db_engine
from recon.ingest.store import S3Store
from recon.jobs.handlers import handlers_for
from recon.jobs.kinds import SCAN_BUCKET
from recon.jobs.worker import Periodic, Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    sessions = sessionmaker(engine)
    store = None
    periodic: list[Periodic] = []
    if settings.s3_bucket:
        store = S3Store(
            settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key.get_secret_value()
            if settings.s3_secret_key
            else None,
        )
        periodic.append(
            Periodic(
                SCAN_BUCKET,
                settings.s3_bucket,
                {"bucket": settings.s3_bucket},
                timedelta(seconds=settings.s3_poll_seconds),
            )
        )
    worker = Worker(
        sessions,
        handlers_for(sessions, store, settings),
        periodic=periodic,
        lease=timedelta(seconds=settings.job_lease_seconds),
        heartbeat_every=timedelta(seconds=settings.job_heartbeat_seconds),
        poll_seconds=settings.job_poll_seconds,
    )
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        worker.run_forever(stop)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

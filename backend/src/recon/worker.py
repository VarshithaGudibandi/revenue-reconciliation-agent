"""Run a job worker: python -m recon.worker"""

import logging
import signal
import threading
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from recon.config import Settings
from recon.db import create_db_engine
from recon.jobs.handlers import handlers_for
from recon.jobs.worker import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    sessions = sessionmaker(engine)
    worker = Worker(
        sessions,
        handlers_for(sessions),
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

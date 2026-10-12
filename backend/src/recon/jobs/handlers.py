"""What each kind of job does."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from recon.alerts.service import RetryableDeliveryError, send_digests, send_run_alerts
from recon.config import Settings
from recon.ingest.scan import scan_bucket
from recon.ingest.store import ObjectStore
from recon.jobs import queue
from recon.jobs.kinds import RECONCILE_PERIOD, SCAN_BUCKET, SEND_ALERTS, SEND_DIGEST
from recon.jobs.worker import Handler, Permanent, Retry
from recon.persistence.repository import check_period
from recon.pipeline import reconcile_period

log = logging.getLogger("recon.worker")


def handle_reconcile(sessions: sessionmaker[Session]) -> Handler:
    """Reconcile the latest files of a period and bring its cases up to date."""

    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            profile_id = uuid.UUID(payload["profile_id"])
            period = str(payload["period"])
            check_period(period)
        except (KeyError, ValueError) as error:
            raise Permanent(f"the job has no usable profile and period: {error}") from error
        outcome = reconcile_period(sessions, profile_id, period)
        if outcome.run.status != "succeeded":
            raise Retry(f"the run is {outcome.run.status}, not finished")
        result = outcome.as_result()
        if result["cases"] is not None:
            with sessions.begin() as session:
                queue.enqueue(
                    session,
                    SEND_ALERTS,
                    result["run"]["id"],
                    {
                        "profile_id": str(profile_id),
                        "period": period,
                        "run_id": result["run"]["id"],
                        "cases": result["cases"],
                    },
                    now=datetime.now(UTC),
                )
        return result

    return handle


def handle_scan(sessions: sessionmaker[Session], store: ObjectStore, settings: Settings) -> Handler:
    """Register the files that have arrived in the bucket, and queue the periods they complete."""

    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        result = scan_bucket(
            sessions,
            store,
            prefix=settings.s3_prefix,
            storage_dir=settings.storage_dir,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
            max_attempts=settings.job_max_attempts,
        )
        for reason in result.reasons[:20]:
            log.info("not ingested: %s", reason)
        return result.as_dict()

    return handle


def handle_alerts(sessions: sessionmaker[Session], settings: Settings) -> Handler:
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return dict(send_run_alerts(sessions, settings, payload, now=datetime.now(UTC)))
        except RetryableDeliveryError as error:
            raise Retry(str(error)) from error

    return handle


def handle_digest(sessions: sessionmaker[Session], settings: Settings) -> Handler:
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return dict(send_digests(sessions, settings, now=datetime.now(UTC)))
        except RetryableDeliveryError as error:
            raise Retry(str(error)) from error

    return handle


def handlers_for(
    sessions: sessionmaker[Session],
    store: ObjectStore | None = None,
    settings: Settings | None = None,
) -> dict[str, Handler]:
    handlers = {RECONCILE_PERIOD: handle_reconcile(sessions)}
    if settings is not None:
        handlers[SEND_ALERTS] = handle_alerts(sessions, settings)
        handlers[SEND_DIGEST] = handle_digest(sessions, settings)
        if store is not None:
            handlers[SCAN_BUCKET] = handle_scan(sessions, store, settings)
    return handlers

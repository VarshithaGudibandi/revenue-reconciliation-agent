"""Delivering alerts once, to the channels that want them.

A delivery is claimed before it is sent and marked afterwards, under a unique key per channel and
event, so a job that is retried does not send what it already sent. If a worker dies between
sending and marking, the claim expires and the message is sent again: delivery is at-least-once.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from recon.alerts.message import Alert, digest, run_alert
from recon.alerts.notifiers import DeliveryError, Notifier, notifier_for
from recon.config import Settings
from recon.persistence.models import AlertChannel, AlertDelivery

log = logging.getLogger("recon.alerts")

CLAIM_TIMEOUT = timedelta(minutes=2)
ERROR_LIMIT = 500
Result = Literal["sent", "duplicate", "busy", "failed", "skipped"]
NotifierFactory = Callable[[AlertChannel, Settings], Notifier]


class RetryableDeliveryError(Exception):
    """A message could not be delivered now and may be later."""


def deliver(
    sessions: sessionmaker[Session],
    channel_id: uuid.UUID,
    alert: Alert,
    *,
    kind: str,
    dedupe_key: str,
    settings: Settings,
    now: datetime,
    notifier_factory: NotifierFactory = notifier_for,
) -> Result:
    """Send one alert to one channel unless that was already done for this key.

    Raises ``RetryableDeliveryError`` when trying again could help, after recording the failure.
    """
    with sessions.begin() as session:
        channel = session.get(AlertChannel, channel_id)
        if channel is None or (not channel.enabled and kind != "test"):
            return "skipped"
        session.execute(
            insert(AlertDelivery)
            .values(
                channel_id=channel_id,
                kind=kind,
                dedupe_key=dedupe_key,
                subject=alert.subject,
                status="sending",
                attempts=0,
            )
            .on_conflict_do_nothing(constraint="uq_alert_delivery_once")
        )
        row = session.scalars(
            select(AlertDelivery)
            .where(AlertDelivery.channel_id == channel_id, AlertDelivery.dedupe_key == dedupe_key)
            .with_for_update()
        ).one()
        if row.status == "sent":
            return "duplicate"
        recently_claimed = (
            row.status == "sending"
            and row.claimed_at is not None
            and now - row.claimed_at < CLAIM_TIMEOUT
        )
        if row.attempts and recently_claimed:
            return "busy"
        row.status = "sending"
        row.attempts += 1
        row.claimed_at = now
        row.error = None
        delivery_id = row.id
        notifier = notifier_factory(channel, settings)

    try:
        notifier.send(alert)
    except DeliveryError as error:
        with sessions.begin() as session:
            failed = session.get_one(AlertDelivery, delivery_id)
            failed.status = "failed"
            failed.error = str(error)[:ERROR_LIMIT]
            failed.claimed_at = None
        log.warning("could not deliver %r to channel %s: %s", alert.subject, channel_id, error)
        if error.retryable:
            raise RetryableDeliveryError(str(error)) from error
        return "failed"
    with sessions.begin() as session:
        sent = session.get_one(AlertDelivery, delivery_id)
        sent.status = "sent"
        sent.sent_at = now
        sent.claimed_at = None
    return "sent"


def send_run_alerts(
    sessions: sessionmaker[Session],
    settings: Settings,
    payload: dict[str, Any],
    *,
    now: datetime,
    notifier_factory: NotifierFactory = notifier_for,
) -> dict[str, int]:
    """After a run: tell the channels that asked for new cases, if the run found any."""
    profile_id = uuid.UUID(payload["profile_id"])
    cases: dict[str, int] = payload["cases"]
    counts = {"sent": 0, "duplicate": 0, "failed": 0, "skipped": 0}
    if cases.get("opened", 0) + cases.get("reopened", 0) == 0:
        return counts
    with sessions() as session:
        alert = run_alert(session, profile_id, payload["period"], cases, settings.public_url)
        channels = list(
            session.scalars(
                select(AlertChannel.id).where(
                    AlertChannel.enabled.is_(True), AlertChannel.on_new_cases.is_(True)
                )
            )
        )
    retry: list[str] = []
    for channel_id in channels:
        try:
            result = deliver(
                sessions,
                channel_id,
                alert,
                kind="run",
                dedupe_key=f"run:{payload['run_id']}",
                settings=settings,
                now=now,
                notifier_factory=notifier_factory,
            )
        except RetryableDeliveryError as error:
            retry.append(str(error))
            continue
        counts[result if result in counts else "skipped"] += 1
    if retry:
        # the ones that were delivered are not sent again when the job comes round
        raise RetryableDeliveryError("; ".join(retry))
    return counts


def send_digests(
    sessions: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime,
    notifier_factory: NotifierFactory = notifier_for,
) -> dict[str, int]:
    """Send the daily digest to each channel whose hour (UTC) has come and which has not had
    today's. A worker that was down at the hour sends it when it is back."""
    counts = {"sent": 0, "duplicate": 0, "failed": 0, "skipped": 0}
    with sessions() as session:
        due = [
            c
            for c in session.scalars(
                select(AlertChannel).where(
                    AlertChannel.enabled.is_(True), AlertChannel.digest_hour.is_not(None)
                )
            )
            if c.digest_hour is not None and now.hour >= c.digest_hour
        ]
        due_ids = [c.id for c in due]
        alert = digest(session, settings.public_url) if due_ids else None
    if alert is None:
        return counts
    retry: list[str] = []
    for channel_id in due_ids:
        try:
            result = deliver(
                sessions,
                channel_id,
                alert,
                kind="digest",
                dedupe_key=f"digest:{now.date().isoformat()}",
                settings=settings,
                now=now,
                notifier_factory=notifier_factory,
            )
        except RetryableDeliveryError as error:
            retry.append(str(error))
            continue
        counts[result if result in counts else "skipped"] += 1
    if retry:
        raise RetryableDeliveryError("; ".join(retry))
    return counts

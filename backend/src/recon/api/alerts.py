"""Setting up where alerts go, trying a channel, and seeing what was sent."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recon.alerts.message import Alert
from recon.alerts.service import RetryableDeliveryError, deliver
from recon.alerts.targets import check_email, check_webhook, mask
from recon.api.routes import ERROR_RESPONSES, AppSettings, Sessions
from recon.api.schemas import ChannelBody, ChannelOut, ChannelPatch, DeliveryOut
from recon.persistence.models import AlertChannel, AlertDelivery

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)


class UnknownChannelError(LookupError):
    pass


class ChannelExistsError(ValueError):
    pass


def _delivery_out(row: AlertDelivery, name: str) -> DeliveryOut:
    return DeliveryOut(
        id=row.id,
        channel_id=row.channel_id,
        channel_name=name,
        kind=row.kind,
        subject=row.subject,
        status=row.status,
        attempts=row.attempts,
        error=row.error,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )


def _channel_out(session: Session, channel: AlertChannel) -> ChannelOut:
    last = session.scalars(
        select(AlertDelivery)
        .where(AlertDelivery.channel_id == channel.id)
        .order_by(AlertDelivery.created_at.desc())
        .limit(1)
    ).first()
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        target=mask(channel.kind, channel.target),
        enabled=channel.enabled,
        on_new_cases=channel.on_new_cases,
        digest_hour=channel.digest_hour,
        created_at=channel.created_at,
        last_delivery=None if last is None else _delivery_out(last, channel.name),
    )


def _get(session: Session, channel_id: uuid.UUID) -> AlertChannel:
    channel = session.get(AlertChannel, channel_id)
    if channel is None:
        raise UnknownChannelError(f"no channel {channel_id}")
    return channel


@router.get("/alert-channels", response_model=list[ChannelOut])
def list_channels(sessions: Sessions) -> list[ChannelOut]:
    with sessions() as session:
        channels = session.scalars(select(AlertChannel).order_by(AlertChannel.created_at))
        return [_channel_out(session, c) for c in channels]


@router.post("/alert-channels", response_model=ChannelOut, status_code=201)
def create_channel(body: ChannelBody, sessions: Sessions, settings: AppSettings) -> ChannelOut:
    """Add a place to send alerts. A webhook must be https and must not point at an internal
    address; an email channel needs a mail server (RECON_SMTP_HOST)."""
    target = body.target.strip()
    if body.kind == "email":
        check_email(target)
        if not settings.smtp_host:
            raise ValueError("email needs a mail server: set RECON_SMTP_HOST")
    else:
        check_webhook(
            target,
            allow_http=settings.alerts_allow_http,
            allow_private=settings.alerts_allow_private,
        )
    try:
        with sessions.begin() as session:
            channel = AlertChannel(
                name=body.name.strip(),
                kind=body.kind,
                target=target,
                enabled=body.enabled,
                on_new_cases=body.on_new_cases,
                digest_hour=body.digest_hour,
            )
            session.add(channel)
            session.flush()
            return _channel_out(session, channel)
    except IntegrityError as error:
        raise ChannelExistsError(f"a channel named {body.name!r} already exists") from error


@router.patch("/alert-channels/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: uuid.UUID, body: ChannelPatch, sessions: Sessions) -> ChannelOut:
    changes = body.model_dump(exclude_unset=True)
    with sessions.begin() as session:
        channel = _get(session, channel_id)
        for name, value in changes.items():
            setattr(channel, name, value)
        session.flush()
        return _channel_out(session, channel)


@router.delete("/alert-channels/{channel_id}", status_code=204)
def delete_channel(channel_id: uuid.UUID, sessions: Sessions) -> Response:
    with sessions.begin() as session:
        session.delete(_get(session, channel_id))
    return Response(status_code=204)


@router.post("/alert-channels/{channel_id}/test", response_model=DeliveryOut)
def test_channel(channel_id: uuid.UUID, sessions: Sessions, settings: AppSettings) -> DeliveryOut:
    """Send a test message now and say how it went."""
    key = f"test:{uuid.uuid4()}"
    alert = Alert(
        subject="Test message from the revenue reconciliation agent",
        lines=("If you can read this, alerts to this channel work.",),
    )
    with sessions() as session:
        _get(session, channel_id)
    # a failure is recorded on the delivery, which is what is returned
    with contextlib.suppress(RetryableDeliveryError):
        deliver(
            sessions,
            channel_id,
            alert,
            kind="test",
            dedupe_key=key,
            settings=settings,
            now=datetime.now(UTC),
        )
    with sessions() as session:
        row = session.scalars(
            select(AlertDelivery).where(
                AlertDelivery.channel_id == channel_id, AlertDelivery.dedupe_key == key
            )
        ).one()
        return _delivery_out(row, _get(session, channel_id).name)


@router.get("/alert-deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    sessions: Sessions, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[DeliveryOut]:
    """The latest messages, newest first, whether they went or not."""
    with sessions() as session:
        rows = session.execute(
            select(AlertDelivery, AlertChannel.name)
            .join(AlertChannel, AlertChannel.id == AlertDelivery.channel_id)
            .order_by(AlertDelivery.created_at.desc())
            .limit(limit)
        ).all()
        return [_delivery_out(row, name) for row, name in rows]

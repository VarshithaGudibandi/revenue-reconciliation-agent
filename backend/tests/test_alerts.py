"""Alerts: where they may go, how they are sent, and that each is sent once."""

import json
import socket
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from recon.alerts.message import Alert
from recon.alerts.notifiers import (
    DeliveryError,
    EmailNotifier,
    WebhookNotifier,
    slack_payload,
    teams_payload,
)
from recon.alerts.service import (
    CLAIM_TIMEOUT,
    RetryableDeliveryError,
    deliver,
    send_digests,
    send_run_alerts,
)
from recon.alerts.targets import UnsafeTargetError, check_email, check_webhook, mask
from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.persistence.models import AlertChannel, AlertDelivery
from recon.persistence.repository import create_profile, register_file
from recon.pipeline import reconcile_period
from recon.profiles import ProfileConfig

ALERT = Alert("Acme: 3 new exceptions in 2026-01", ("Opened 3.", "Open now: 5."), "https://x.test/")


# --- where an alert may go ---------------------------------------------------------------------


def test_a_public_https_webhook_is_accepted() -> None:
    check_webhook("https://8.8.8.8/hook", allow_http=False, allow_private=False)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://8.8.8.8/hook", "https"),
        ("ftp://8.8.8.8/hook", "https URL"),
        ("not a url", "https URL"),
        ("https://user:pw@8.8.8.8/hook", "user name"),
        ("https://127.0.0.1/hook", "not a public address"),
        ("https://10.1.2.3/hook", "not a public address"),
        ("https://192.168.0.5/hook", "not a public address"),
        ("https://169.254.169.254/latest/meta-data", "not a public address"),
        ("https://[::1]/hook", "not a public address"),
        ("https://localhost/hook", "not a public address"),
        ("https://nothing.invalid/hook", "does not resolve"),
    ],
)
def test_a_webhook_that_could_reach_the_servers_own_network_is_refused(url: str, why: str) -> None:
    with pytest.raises(UnsafeTargetError, match=why):
        check_webhook(url, allow_http=False, allow_private=False)


def test_the_checks_can_be_relaxed_for_a_local_setup() -> None:
    check_webhook("http://127.0.0.1:9000/hook", allow_http=True, allow_private=True)


def test_an_email_address_is_checked_and_a_webhook_is_masked() -> None:
    check_email("finance@example.com")
    with pytest.raises(UnsafeTargetError):
        check_email("finance")
    assert (
        mask("slack", "https://hooks.slack.com/services/T0/B0/secret")
        == "https://hooks.slack.com/…"
    )
    assert mask("email", "finance@example.com") == "finance@example.com"


# --- what is sent ------------------------------------------------------------------------------


def test_the_slack_message_carries_the_subject_the_facts_and_a_link() -> None:
    text = str(slack_payload(ALERT)["text"])

    assert text.startswith("*Acme: 3 new exceptions in 2026-01*")
    assert "Open now: 5." in text and "<https://x.test/|Open the dashboard>" in text


def test_the_teams_message_is_an_adaptive_card_in_a_message() -> None:
    payload = teams_payload(ALERT)

    assert payload["type"] == "message"
    attachment = payload["attachments"][0]  # type: ignore[index]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert [b["text"] for b in card["body"]] == [ALERT.subject, *ALERT.lines]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open the dashboard", "url": "https://x.test/"}
    ]


def test_a_message_without_a_link_has_no_action() -> None:
    plain = Alert("s", ("l",))

    assert "actions" not in teams_payload(plain)["attachments"][0]["content"]  # type: ignore[index]
    assert "Open the dashboard" not in str(slack_payload(plain)["text"])


# --- sending over real sockets -----------------------------------------------------------------


@dataclass
class Hook:
    url: str
    received: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[int] = field(default_factory=lambda: [200])
    location: str | None = None

    def answer_with(self, *statuses: int) -> None:
        self.statuses = list(statuses)


@pytest.fixture
def hook() -> Iterator[Hook]:
    state = Hook("")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.received.append(json.loads(body))
            status = state.statuses.pop(0) if len(state.statuses) > 1 else state.statuses[0]
            self.send_response(status)
            if status in (301, 302):
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}/hook"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


LOCAL = Settings(
    database_url="postgresql+psycopg://unused:unused@localhost/unused",
    alerts_allow_http=True,
    alerts_allow_private=True,
)


def webhook(url: str, settings: Settings = LOCAL) -> WebhookNotifier:
    return WebhookNotifier(url, settings, slack_payload)


def test_a_webhook_gets_the_message_as_json(hook: Hook) -> None:
    webhook(hook.url).send(ALERT)

    assert hook.received == [slack_payload(ALERT)]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(500, True), (503, True), (429, True), (404, False), (400, False), (302, False)],
)
def test_whether_a_failure_is_worth_retrying_follows_the_answer(
    hook: Hook, status: int, retryable: bool
) -> None:
    hook.answer_with(status)

    with pytest.raises(DeliveryError) as raised:
        webhook(hook.url).send(ALERT)

    assert raised.value.retryable is retryable


def test_a_redirect_is_not_followed(hook: Hook) -> None:
    hook.answer_with(302)

    with pytest.raises(DeliveryError, match="302"):
        webhook(hook.url).send(ALERT)

    assert len(hook.received) == 1


def test_an_unreachable_webhook_is_worth_retrying() -> None:
    with pytest.raises(DeliveryError) as raised:
        webhook("http://127.0.0.1:1/hook").send(ALERT)

    assert raised.value.retryable


def test_a_webhook_on_the_private_network_is_not_called_unless_allowed(hook: Hook) -> None:
    strict = Settings(database_url=LOCAL.database_url, alerts_allow_http=True)

    with pytest.raises(DeliveryError, match="not a public address") as raised:
        webhook(hook.url, strict).send(ALERT)

    assert not raised.value.retryable and hook.received == []


@dataclass
class Mailbox:
    port: int
    messages: list[Message] = field(default_factory=list)


@pytest.fixture
def mailbox() -> Iterator[Mailbox]:
    box = Mailbox(0)

    class Collect:
        async def handle_DATA(self, server: object, session: object, envelope: Any) -> str:  # noqa: N802
            from email import message_from_bytes

            box.messages.append(message_from_bytes(envelope.content))
            return "250 OK"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        box.port = probe.getsockname()[1]
    controller = Controller(Collect(), hostname="127.0.0.1", port=box.port)
    controller.start()
    yield box
    controller.stop()


def mail_settings(port: int) -> Settings:
    return Settings(
        database_url=LOCAL.database_url,
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_starttls=False,
        smtp_from="recon@example.com",
    )


def test_an_email_arrives_with_the_subject_and_the_facts(mailbox: Mailbox) -> None:
    EmailNotifier("finance@example.com", mail_settings(mailbox.port)).send(ALERT)

    (message,) = mailbox.messages
    assert message["Subject"] == ALERT.subject
    assert (message["To"], message["From"]) == ("finance@example.com", "recon@example.com")
    body = message.get_payload()
    assert "Open now: 5." in str(body) and "https://x.test/" in str(body)


def test_email_without_a_mail_server_cannot_be_sent() -> None:
    with pytest.raises(DeliveryError, match="RECON_SMTP_HOST") as raised:
        EmailNotifier("a@b.co", Settings(database_url=LOCAL.database_url)).send(ALERT)

    assert not raised.value.retryable


def test_a_mail_server_that_is_down_is_worth_retrying() -> None:
    with pytest.raises(DeliveryError) as raised:
        EmailNotifier("a@b.co", mail_settings(1)).send(ALERT)

    assert raised.value.retryable


# --- sending once ------------------------------------------------------------------------------


@dataclass
class World:
    url: str
    root: Path
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID

    def settings(self, **extra: Any) -> Settings:
        return Settings(
            database_url=self.url,
            storage_dir=self.root / "storage",
            alerts_allow_http=True,
            alerts_allow_private=True,
            **extra,
        )

    def channel(self, target: str, **fields: Any) -> uuid.UUID:
        with self.sessions.begin() as session:
            channel = AlertChannel(
                name=fields.pop("name", f"c{uuid.uuid4().hex[:6]}"),
                kind=fields.pop("kind", "slack"),
                target=target,
                **fields,
            )
            session.add(channel)
            session.flush()
            return channel.id

    def deliveries(self, *where: Any) -> list[AlertDelivery]:
        with self.sessions() as session:
            rows = list(session.scalars(select(AlertDelivery).where(*where)))
            session.expunge_all()
            return rows


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    data = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=5, invoices=100)), data)
    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile = create_profile(
            session,
            name="Acme books",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(
                sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
            ),
        )
        profile_id = profile.id
        for source in ("invoices", "payments"):
            register_file(
                session,
                profile_id=profile_id,
                source_name=source,
                period="2026-01",
                path=data / f"{source}.csv",
            )
    yield World(migrated_database_url, tmp_path, sessions, profile_id)
    engine.dispose()


NOW = datetime(2026, 2, 3, 9, 30, tzinfo=UTC)


def send(world: World, channel: uuid.UUID, hook_url: str, key: str = "k", **kw: Any) -> str:
    return deliver(
        world.sessions,
        channel,
        ALERT,
        kind=kw.pop("kind", "run"),
        dedupe_key=key,
        settings=world.settings(),
        now=kw.pop("now", NOW),
    )


def test_a_message_is_sent_once_however_often_the_job_comes_round(world: World, hook: Hook) -> None:
    channel = world.channel(hook.url)

    results = [send(world, channel, hook.url) for _ in range(3)]

    assert results == ["sent", "duplicate", "duplicate"]
    assert len(hook.received) == 1
    (row,) = world.deliveries()
    assert (row.status, row.attempts, row.sent_at) == ("sent", 1, NOW)


def test_a_failure_that_may_pass_is_recorded_raised_and_sent_on_the_retry(
    world: World, hook: Hook
) -> None:
    channel = world.channel(hook.url)
    hook.answer_with(503, 200)

    with pytest.raises(RetryableDeliveryError):
        send(world, channel, hook.url)
    (failed,) = world.deliveries()
    assert (failed.status, failed.attempts) == ("failed", 1) and "503" in (failed.error or "")

    assert send(world, channel, hook.url) == "sent"
    (sent,) = world.deliveries()
    assert (sent.status, sent.attempts, sent.error) == ("sent", 2, None)


def test_a_failure_that_will_not_pass_is_recorded_and_not_raised(world: World, hook: Hook) -> None:
    channel = world.channel(hook.url)
    hook.answer_with(404)

    assert send(world, channel, hook.url) == "failed"
    assert world.deliveries()[0].status == "failed"


def test_a_message_another_worker_is_sending_is_left_to_it(world: World, hook: Hook) -> None:
    channel = world.channel(hook.url)
    with world.sessions.begin() as session:
        session.add(
            AlertDelivery(
                channel_id=channel,
                kind="run",
                dedupe_key="k",
                subject="s",
                status="sending",
                attempts=1,
                claimed_at=NOW - timedelta(seconds=10),
            )
        )

    assert send(world, channel, hook.url) == "busy"
    assert hook.received == []


def test_a_claim_that_was_never_finished_expires_and_the_message_goes(
    world: World, hook: Hook
) -> None:
    channel = world.channel(hook.url)
    with world.sessions.begin() as session:
        session.add(
            AlertDelivery(
                channel_id=channel,
                kind="run",
                dedupe_key="k",
                subject="s",
                status="sending",
                attempts=1,
                claimed_at=NOW - CLAIM_TIMEOUT - timedelta(seconds=1),
            )
        )

    assert send(world, channel, hook.url) == "sent"
    assert world.deliveries()[0].attempts == 2


def test_a_disabled_channel_gets_nothing_but_can_still_be_tested(world: World, hook: Hook) -> None:
    channel = world.channel(hook.url, enabled=False)

    assert send(world, channel, hook.url) == "skipped"
    assert send(world, channel, hook.url, key="t", kind="test") == "sent"
    assert len(hook.received) == 1


def run_cases(world: World) -> dict[str, Any]:
    outcome = reconcile_period(world.sessions, world.profile_id, "2026-01")
    result = outcome.as_result()
    return {
        "profile_id": str(world.profile_id),
        "period": "2026-01",
        "run_id": result["run"]["id"],
        "cases": result["cases"],
    }


def test_a_run_that_found_new_cases_alerts_the_channels_that_asked(
    world: World, hook: Hook
) -> None:
    wants = world.channel(hook.url, name="wants")
    world.channel(hook.url, name="quiet", on_new_cases=False)
    world.channel(hook.url, name="off", enabled=False)
    payload = run_cases(world)
    assert payload["cases"]["opened"] > 0

    counts = send_run_alerts(world.sessions, world.settings(), payload, now=NOW)

    assert counts["sent"] == 1 and len(hook.received) == 1
    text = str(hook.received[0]["text"])
    assert text.startswith(f"*Acme books: {payload['cases']['opened']} new exception")
    assert "at stake" in text
    assert [d.channel_id for d in world.deliveries()] == [wants]


def test_a_run_that_found_nothing_new_says_nothing(world: World, hook: Hook) -> None:
    world.channel(hook.url)
    payload = run_cases(world)
    payload["cases"] = {**payload["cases"], "opened": 0, "reopened": 0}

    send_run_alerts(world.sessions, world.settings(), payload, now=NOW)

    assert hook.received == [] and world.deliveries() == []


def test_when_one_channel_fails_the_others_are_not_sent_to_twice_on_the_retry(
    world: World, hook: Hook
) -> None:
    good = world.channel(hook.url, name="a-good")
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as dead:
        dead_url = f"http://127.0.0.1:{dead.server_port}/x"
    bad = world.channel(dead_url, name="b-bad")
    payload = run_cases(world)

    with pytest.raises(RetryableDeliveryError):
        send_run_alerts(world.sessions, world.settings(), payload, now=NOW)
    assert len(hook.received) == 1

    with world.sessions.begin() as session:
        session.get_one(AlertChannel, bad).target = hook.url
    counts = send_run_alerts(world.sessions, world.settings(), payload, now=NOW)

    assert counts["sent"] == 1 and counts["duplicate"] == 1
    assert len(hook.received) == 2
    assert {d.channel_id: d.status for d in world.deliveries()} == {good: "sent", bad: "sent"}


def test_the_digest_goes_once_a_day_to_a_channel_once_its_hour_has_come(
    world: World, hook: Hook
) -> None:
    world.channel(hook.url, digest_hour=8)
    world.channel(hook.url, name="late", digest_hour=17)
    world.channel(hook.url, name="never")
    run_cases(world)
    settings = world.settings()

    assert send_digests(world.sessions, settings, now=NOW.replace(hour=7))["sent"] == 0
    assert send_digests(world.sessions, settings, now=NOW.replace(hour=9))["sent"] == 1
    assert send_digests(world.sessions, settings, now=NOW.replace(hour=12))["duplicate"] == 1
    assert send_digests(world.sessions, settings, now=NOW.replace(hour=18))["sent"] == 1
    assert send_digests(world.sessions, settings, now=NOW.replace(hour=19))["duplicate"] == 2
    assert send_digests(world.sessions, settings, now=NOW + timedelta(days=1))["sent"] == 1
    # day one: the morning channel at 9, the evening one at 18; day two: the morning one again
    assert len(hook.received) == 3
    assert "Acme books, 2026-01:" in str(hook.received[0]["text"])


def test_there_is_no_digest_when_nothing_is_open(world: World, hook: Hook) -> None:
    world.channel(hook.url, digest_hour=0)

    counts = send_digests(world.sessions, world.settings(), now=NOW)

    assert counts["sent"] == 0 and hook.received == []
    with world.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AlertDelivery)) == 0

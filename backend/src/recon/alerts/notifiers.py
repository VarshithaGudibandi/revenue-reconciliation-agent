"""Sending a message to a person: Slack, Teams or email, behind one interface.

``DeliveryError.retryable`` says whether trying again can help: a timeout, a refused connection or a
5xx can; a 4xx (a webhook that was revoked, a bad address) cannot.
"""

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

import httpx

from recon.alerts.message import Alert
from recon.alerts.targets import UnsafeTargetError, check_webhook
from recon.config import Settings
from recon.persistence.models import AlertChannel

TIMEOUT_SECONDS = 10.0


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


def slack_payload(alert: Alert) -> dict[str, object]:
    text = f"*{alert.subject}*\n" + "\n".join(alert.lines)
    if alert.link:
        text += f"\n<{alert.link}|Open the dashboard>"
    return {"text": text}


def teams_payload(alert: Alert) -> dict[str, object]:
    """An Adaptive Card in a message, which is what a Teams Workflows webhook takes."""
    body: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": alert.subject,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        *({"type": "TextBlock", "text": line, "wrap": True} for line in alert.lines),
    ]
    card: dict[str, object] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if alert.link:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open the dashboard", "url": alert.link}
        ]
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


class WebhookNotifier:
    def __init__(
        self, url: str, settings: Settings, payload: Callable[[Alert], dict[str, object]]
    ) -> None:
        self.url = url
        self.settings = settings
        self.payload = payload

    def send(self, alert: Alert) -> None:
        try:
            check_webhook(
                self.url,
                allow_http=self.settings.alerts_allow_http,
                allow_private=self.settings.alerts_allow_private,
            )
        except UnsafeTargetError as error:
            raise DeliveryError(str(error), retryable=False) from error
        try:
            response = httpx.post(
                self.url,
                json=self.payload(alert),
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise DeliveryError(f"{type(error).__name__}: {error}", retryable=True) from error
        if response.status_code >= 500 or response.status_code == 429:
            raise DeliveryError(f"the webhook answered {response.status_code}", retryable=True)
        if response.status_code >= 300:
            raise DeliveryError(f"the webhook answered {response.status_code}", retryable=False)


class EmailNotifier:
    def __init__(self, address: str, settings: Settings) -> None:
        self.address = address
        self.settings = settings

    def send(self, alert: Alert) -> None:
        settings = self.settings
        if not settings.smtp_host:
            raise DeliveryError("no mail server is configured (RECON_SMTP_HOST)", retryable=False)
        message = EmailMessage()
        message["Subject"] = alert.subject
        message["From"] = settings.smtp_from
        message["To"] = self.address
        message.set_content(alert.text())
        try:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS
            ) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if settings.smtp_user and settings.smtp_password:
                    smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
                smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as error:
            raise DeliveryError(f"the address was refused: {error}", retryable=False) from error
        except (smtplib.SMTPException, OSError) as error:
            raise DeliveryError(f"{type(error).__name__}: {error}", retryable=True) from error


def notifier_for(channel: AlertChannel, settings: Settings) -> Notifier:
    if channel.kind == "slack":
        return WebhookNotifier(channel.target, settings, slack_payload)
    if channel.kind == "teams":
        return WebhookNotifier(channel.target, settings, teams_payload)
    if channel.kind == "email":
        return EmailNotifier(channel.target, settings)
    raise DeliveryError(f"unknown channel kind {channel.kind!r}", retryable=False)

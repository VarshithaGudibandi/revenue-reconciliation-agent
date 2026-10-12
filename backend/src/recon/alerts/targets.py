"""Checking where an alert may be sent.

A webhook URL is typed in by whoever sets up the channel and the server will POST to it, so it is
checked: https only, and the host must not resolve to a loopback, private, link-local or
otherwise internal address. Without that, a channel is a way to make the server call things on
its own network. The check is repeated when a message is sent, because DNS can change.

One gap remains: the name is resolved here and again by the HTTP client, so a hostile DNS server
could answer differently the second time. Redirects are not followed, which closes the easier
route.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UnsafeTargetError(ValueError):
    """The target is not acceptable. The message says why."""


def check_webhook(url: str, *, allow_http: bool, allow_private: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UnsafeTargetError("the webhook must be a full https URL")
    if parts.scheme == "http" and not allow_http:
        raise UnsafeTargetError("the webhook must use https")
    if parts.username or parts.password:
        raise UnsafeTargetError("the webhook URL must not carry a user name or password")
    if allow_private:
        return
    try:
        found = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise UnsafeTargetError(f"the host {parts.hostname!r} does not resolve") from error
    for entry in found:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise UnsafeTargetError(
                f"the host {parts.hostname!r} resolves to {address}, which is not a public address"
            )


def check_email(address: str) -> None:
    if not _EMAIL.match(address):
        raise UnsafeTargetError("that does not look like an email address")


def mask(kind: str, target: str) -> str:
    """The target as it may be shown: a webhook keeps its host and hides the path, which is
    where the secret is."""
    if kind == "email":
        return target
    parts = urlsplit(target)
    return f"{parts.scheme}://{parts.hostname}/…" if parts.hostname else "…"

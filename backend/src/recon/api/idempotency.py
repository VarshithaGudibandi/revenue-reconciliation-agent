"""Replay the stored response of a request that was already handled.

A client that retries a request with the same ``Idempotency-Key`` gets the first
response back and the action is not repeated. The key is claimed in the same
transaction as the action, so either both are kept or neither is, and two requests
with one key cannot both run: the second waits for the first to finish.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from recon.persistence.models import IdempotencyRecord

MAX_KEY_LENGTH = 255


class IdempotencyKeyError(ValueError):
    """The key is malformed, or was already used for a different request."""


def validate_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise IdempotencyKeyError("Idempotency-Key must be 1 to 255 printable characters")
    return key


def request_hash(method: str, path: str, body: str) -> str:
    return hashlib.sha256(f"{method} {path}\n{body}".encode()).hexdigest()


def run_once(
    sessions: sessionmaker[Session],
    key: str | None,
    fingerprint: str,
    action: Callable[[Session], dict[str, Any]],
) -> dict[str, Any]:
    """Run ``action`` in a transaction, or replay what it returned for this key before."""
    with sessions.begin() as session:
        if key is None:
            return action(session)

        claimed = session.scalar(
            pg_insert(IdempotencyRecord)
            .values(key=key, request_hash=fingerprint)
            .on_conflict_do_nothing()
            .returning(IdempotencyRecord.key)
        )
        if claimed is None:
            stored = session.get_one(IdempotencyRecord, key)
            if stored.request_hash != fingerprint:
                raise IdempotencyKeyError("this Idempotency-Key was used for a different request")
            if stored.response is None:
                raise IdempotencyKeyError("the request with this Idempotency-Key did not finish")
            return stored.response

        response = action(session)
        session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.key == key)
            .values(status_code=200, response=response)
        )
        return response

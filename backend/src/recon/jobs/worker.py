"""A worker that takes jobs from the queue and runs them.

The job runs in the worker's own thread. Another thread extends the lease while it works, so a
job can take longer than the lease. If the worker dies, the lease runs out and another worker puts
the job back. If the worker is merely cut off from the database for longer than the lease, it
loses the job; when it finishes it finds that out and drops its result.

A job cannot be cancelled from outside, so a worker that has lost a job still runs it to the end.
"""

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from recon.jobs import queue

log = logging.getLogger("recon.worker")

Handler = Callable[[dict[str, Any]], dict[str, Any]]
Outcome = Literal["succeeded", "retry", "failed", "lost"]


class Retry(Exception):
    """The work could not be done now but may be later."""


class Permanent(Exception):
    """The work will not succeed however often it is tried."""


def _utc() -> datetime:
    return datetime.now(UTC)


def _default_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Periodic:
    """Work that comes round on a timer: a job of this kind is queued every ``every``. Several
    workers may each queue it; jobs for the same key that are still waiting count as one."""

    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    every: timedelta
    max_attempts: int = 3


@dataclass
class Worker:
    sessions: sessionmaker[Session]
    handlers: Mapping[str, Handler]
    name: str = field(default_factory=_default_name)
    lease: timedelta = timedelta(seconds=60)
    heartbeat_every: timedelta = timedelta(seconds=15)
    poll_seconds: float = 1.0
    clock: Callable[[], datetime] = _utc
    backoff: Callable[[int], float] = queue.backoff_seconds
    periodic: Sequence[Periodic] = ()
    _queued_at: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def _queue_periodic(self, session: Session, now: datetime) -> None:
        for item in self.periodic:
            key = f"{item.kind}/{item.dedupe_key}"
            last = self._queued_at.get(key)
            if last is not None and now - last < item.every:
                continue
            queue.enqueue(
                session,
                item.kind,
                item.dedupe_key,
                item.payload,
                now=now,
                max_attempts=item.max_attempts,
            )
            self._queued_at[key] = now

    def run_once(self) -> Outcome | None:
        """Take one due job and run it. None if there was nothing to do."""
        now = self.clock()
        with self.sessions.begin() as session:
            self._queue_periodic(session, now)
            recovered = queue.recover_expired(session, now=now)
            claimed = queue.claim(session, self.name, now=now, lease=self.lease)
        if recovered:
            log.warning("put back %d job(s) whose worker stopped answering", recovered)
        if claimed is None:
            return None

        handler = self.handlers.get(claimed.kind)
        stop = threading.Event()
        beat = threading.Thread(
            target=self._keep_alive, args=(claimed.id, stop), daemon=True, name="heartbeat"
        )
        beat.start()
        try:
            if handler is None:
                raise Permanent(f"no handler for jobs of kind {claimed.kind!r}")
            result = handler(claimed.payload)
        except Permanent as error:
            return self._fail(claimed, str(error), retry=False, stop=stop, beat=beat)
        except Exception as error:  # noqa: BLE001 - whatever a handler raises is a failed attempt
            log.exception(
                "job %s (%s) attempt %d failed", claimed.id, claimed.kind, claimed.attempts
            )
            return self._fail(
                claimed, f"{type(error).__name__}: {error}", retry=True, stop=stop, beat=beat
            )
        stop.set()
        beat.join()
        with self.sessions.begin() as session:
            done = queue.complete(session, claimed.id, self.name, result, now=self.clock())
        if not done:
            log.warning(
                "job %s finished after its lease was lost; the result is dropped", claimed.id
            )
            return "lost"
        return "succeeded"

    def _fail(
        self,
        claimed: queue.Claimed,
        error: str,
        *,
        retry: bool,
        stop: threading.Event,
        beat: threading.Thread,
    ) -> Outcome:
        stop.set()
        beat.join()
        with self.sessions.begin() as session:
            status = queue.fail(
                session,
                claimed.id,
                self.name,
                error,
                now=self.clock(),
                retry=retry,
                backoff=timedelta(seconds=self.backoff(claimed.attempts)),
            )
        if status is None:
            return "lost"
        return "retry" if status == "queued" else "failed"

    def _keep_alive(self, job_id: uuid.UUID, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_every.total_seconds()):
            try:
                with self.sessions.begin() as session:
                    held = queue.heartbeat(
                        session, job_id, self.name, now=self.clock(), lease=self.lease
                    )
            except OperationalError:
                log.warning("could not extend the lease of job %s; will try again", job_id)
                continue
            if not held:
                log.warning("job %s is no longer held by this worker", job_id)
                return

    def run_forever(self, stop: threading.Event) -> None:
        """Work until ``stop`` is set. A job in progress is finished first."""
        log.info("worker %s started", self.name)
        while not stop.is_set():
            try:
                if self.run_once() is None:
                    stop.wait(self.poll_seconds)
            except OperationalError:
                log.exception("the database is not reachable; waiting")
                stop.wait(max(self.poll_seconds, 5.0))
        log.info("worker %s stopped", self.name)

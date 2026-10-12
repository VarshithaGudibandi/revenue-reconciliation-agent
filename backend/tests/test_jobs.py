"""The job queue: claiming, leases, retries, and a worker that runs jobs."""

import random
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from recon.jobs import queue
from recon.jobs.worker import Permanent, Retry, Worker
from recon.persistence.models import Job

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LEASE = timedelta(seconds=60)


def later(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def sessions(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(migrated_database_url)
    yield sessionmaker(engine)
    engine.dispose()


def add(
    sessions: sessionmaker[Session], key: str = "a", *, at: datetime = T0, max_attempts: int = 5
) -> Job:
    with sessions.begin() as session:
        job, _ = queue.enqueue(
            session, "work", key, {"key": key}, now=at, max_attempts=max_attempts
        )
        session.expunge(job)
        return job


class Made(NamedTuple):
    id: uuid.UUID
    created: bool


def enqueue(sessions: sessionmaker[Session], key: str = "a", *, at: datetime = T0) -> Made:
    with sessions.begin() as session:
        job, created = queue.enqueue(session, "work", key, {"key": key}, now=at)
        return Made(job.id, created)


def take(
    sessions: sessionmaker[Session], worker: str = "w1", *, at: datetime = T0
) -> queue.Claimed | None:
    with sessions.begin() as session:
        return queue.claim(session, worker, now=at, lease=LEASE)


def row(sessions: sessionmaker[Session], job_id: Any) -> Job:
    with sessions() as session:
        job = session.get_one(Job, job_id)
        session.expunge(job)
        return job


def test_a_second_request_for_work_that_is_waiting_joins_it(
    sessions: sessionmaker[Session],
) -> None:
    first = add(sessions)

    second = enqueue(sessions, at=later(5))

    assert not second.created and second.id == first.id


def test_a_request_made_while_the_work_is_running_is_new_work(
    sessions: sessionmaker[Session],
) -> None:
    first = add(sessions)
    assert take(sessions) is not None

    second = enqueue(sessions, at=later(5))

    assert second.created and second.id != first.id


def test_the_same_key_does_not_run_twice_at_once(sessions: sessionmaker[Session]) -> None:
    first = add(sessions)
    held = take(sessions, "w1")
    assert held is not None and held.id == first.id
    second = add(sessions, at=later(1))

    assert take(sessions, "w2", at=later(2)) is None

    with sessions.begin() as session:
        assert queue.complete(session, first.id, "w1", {"ok": True}, now=later(3))
    claimed = take(sessions, "w2", at=later(4))
    assert claimed is not None and claimed.id == second.id


def test_nothing_is_claimed_before_it_is_due_and_the_oldest_due_goes_first(
    sessions: sessionmaker[Session],
) -> None:
    late = add(sessions, "late", at=later(100))
    older = add(sessions, "older", at=later(1))
    newer = add(sessions, "newer", at=later(2))

    assert take(sessions, at=later(0)) is None
    assert [take(sessions, at=later(50)).id for _ in range(2)] == [older.id, newer.id]  # type: ignore[union-attr]
    assert take(sessions, at=later(50)) is None
    claimed = take(sessions, at=later(100))
    assert claimed is not None and claimed.id == late.id


def test_a_claim_counts_as_an_attempt_and_sets_the_lease(sessions: sessionmaker[Session]) -> None:
    job = add(sessions)

    claimed = take(sessions, "w1", at=later(10))

    assert claimed is not None and claimed.attempts == 1 and claimed.payload == {"key": "a"}
    held = row(sessions, job.id)
    assert (held.status, held.locked_by, held.lease_expires_at) == ("running", "w1", later(70))


def test_two_workers_never_get_the_same_job(sessions: sessionmaker[Session]) -> None:
    add(sessions, "a")
    add(sessions, "b")
    first_claim = sessions()
    second_claim = sessions()
    try:
        with first_claim.begin():
            one = queue.claim(first_claim, "w1", now=T0, lease=LEASE)
            # w1 has not committed; w2 must skip the row it holds, not wait for it
            with second_claim.begin():
                second_claim.execute(text("SET LOCAL lock_timeout = '2s'"))
                two = queue.claim(second_claim, "w2", now=T0, lease=LEASE)
                third = queue.claim(second_claim, "w2", now=T0, lease=LEASE)
    finally:
        first_claim.close()
        second_claim.close()

    assert one is not None and two is not None and one.id != two.id
    assert third is None


def test_the_holder_extends_the_lease_and_nobody_else_can(sessions: sessionmaker[Session]) -> None:
    job = add(sessions)
    take(sessions, "w1")

    with sessions.begin() as session:
        assert queue.heartbeat(session, job.id, "w1", now=later(30), lease=LEASE)
        assert not queue.heartbeat(session, job.id, "w2", now=later(30), lease=LEASE)
    assert row(sessions, job.id).lease_expires_at == later(90)


def test_a_finished_job_keeps_its_result_and_cannot_be_extended(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions)
    take(sessions, "w1")

    with sessions.begin() as session:
        assert queue.complete(session, job.id, "w1", {"opened": 3}, now=later(5))
        assert not queue.heartbeat(session, job.id, "w1", now=later(6), lease=LEASE)
        assert not queue.complete(session, job.id, "w1", {"opened": 9}, now=later(7))
    done = row(sessions, job.id)
    assert (done.status, done.result, done.finished_at, done.locked_by) == (
        "succeeded",
        {"opened": 3},
        later(5),
        None,
    )


def test_a_failed_attempt_waits_then_returns_until_attempts_run_out(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions, max_attempts=2)
    take(sessions, "w1")

    with sessions.begin() as session:
        assert (
            queue.fail(session, job.id, "w1", "boom", now=later(1), backoff=timedelta(seconds=30))
            == "queued"
        )
    waiting = row(sessions, job.id)
    assert (waiting.status, waiting.run_after, waiting.last_error, waiting.attempts) == (
        "queued",
        later(31),
        "boom",
        1,
    )
    assert take(sessions, "w1", at=later(20)) is None
    assert take(sessions, "w1", at=later(31)) is not None

    with sessions.begin() as session:
        assert queue.fail(session, job.id, "w1", "boom again", now=later(40)) == "failed"
    dead = row(sessions, job.id)
    assert (dead.status, dead.last_error, dead.finished_at, dead.attempts) == (
        "failed",
        "boom again",
        later(40),
        2,
    )


def test_a_failure_that_will_not_get_better_is_not_retried(sessions: sessionmaker[Session]) -> None:
    job = add(sessions)
    take(sessions)

    with sessions.begin() as session:
        assert (
            queue.fail(session, job.id, "w1", "bad payload", now=later(1), retry=False) == "failed"
        )


def test_a_worker_that_lost_the_job_cannot_fail_it_either(sessions: sessionmaker[Session]) -> None:
    job = add(sessions)
    take(sessions, "w1")

    with sessions.begin() as session:
        assert queue.fail(session, job.id, "w2", "x", now=later(1)) is None
    assert row(sessions, job.id).status == "running"


def test_a_job_whose_worker_went_quiet_is_put_back_and_the_old_worker_is_fenced_out(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions)
    take(sessions, "w1", at=T0)

    with sessions.begin() as session:
        assert queue.recover_expired(session, now=later(59)) == 0
        assert queue.recover_expired(session, now=later(61)) == 1
    back = row(sessions, job.id)
    assert (back.status, back.locked_by, back.attempts) == ("queued", None, 1)
    assert "stopped answering" in (back.last_error or "")

    second = take(sessions, "w2", at=later(62))
    assert second is not None and second.attempts == 2
    with sessions.begin() as session:
        assert not queue.complete(session, job.id, "w1", {"late": True}, now=later(63))
        assert queue.complete(session, job.id, "w2", {"ok": True}, now=later(64))
    assert row(sessions, job.id).result == {"ok": True}


def test_a_job_that_keeps_killing_its_worker_ends_as_failed(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions, max_attempts=2)
    for attempt, at in enumerate((0, 100), start=1):
        assert take(sessions, f"w{attempt}", at=later(at)) is not None
        with sessions.begin() as session:
            assert queue.recover_expired(session, now=later(at + 70)) == 1

    dead = row(sessions, job.id)
    assert dead.status == "failed" and dead.attempts == 2 and dead.finished_at == later(170)


def test_backoff_doubles_to_a_cap_with_a_little_jitter() -> None:
    rng = random.Random(1)  # noqa: S311
    delays = [queue.backoff_seconds(n, base=5, cap=60, rng=rng) for n in range(1, 8)]

    for attempt, delay in enumerate(delays, start=1):
        base = min(60, 5 * 2 ** (attempt - 1))
        assert base <= delay <= base * 1.2
    assert delays[-1] <= 72


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE job SET status = 'running' WHERE id = :id",  # running without a lease
        "UPDATE job SET status = 'succeeded', finished_at = now() WHERE id = :id",  # no result
        "UPDATE job SET status = 'failed', finished_at = now() WHERE id = :id",  # no error
        "UPDATE job SET attempts = 9 WHERE id = :id",  # more than max_attempts
        "UPDATE job SET status = 'sleeping' WHERE id = :id",
    ],
)
def test_the_database_refuses_a_job_in_a_state_that_makes_no_sense(
    sessions: sessionmaker[Session], statement: str
) -> None:
    job = add(sessions)

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.execute(text(statement), {"id": job.id})


class Recorder:
    """A handler that records what it was asked and can be told to fail."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_times = fail_times

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise Retry("not yet")
        return {"seen": payload["key"]}


def worker(sessions: sessionmaker[Session], handler: Any, name: str = "w1", **kw: Any) -> Worker:
    return Worker(
        sessions,
        {"work": handler},
        name=name,
        lease=kw.pop("lease", LEASE),
        heartbeat_every=kw.pop("heartbeat_every", timedelta(seconds=15)),
        backoff=lambda attempts: 0.0,
        **kw,
    )


def test_a_worker_runs_a_due_job_and_keeps_the_result(sessions: sessionmaker[Session]) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1))
    handler = Recorder()
    w = worker(sessions, handler)

    assert w.run_once() == "succeeded"
    assert w.run_once() is None

    done = row(sessions, job.id)
    assert (done.status, done.result, handler.calls) == ("succeeded", {"seen": "a"}, [{"key": "a"}])


def test_a_job_that_fails_is_tried_again_and_then_succeeds(sessions: sessionmaker[Session]) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1))
    w = worker(sessions, Recorder(fail_times=2))

    assert [w.run_once() for _ in range(3)] == ["retry", "retry", "succeeded"]
    done = row(sessions, job.id)
    assert (done.status, done.attempts, done.last_error) == ("succeeded", 3, None)


def test_a_job_that_always_fails_ends_as_failed_with_the_reason(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1), max_attempts=3)
    w = worker(sessions, Recorder(fail_times=99))

    assert [w.run_once() for _ in range(3)] == ["retry", "retry", "failed"]
    dead = row(sessions, job.id)
    assert (
        dead.status == "failed"
        and dead.attempts == 3
        and "Retry: not yet" in (dead.last_error or "")
    )


def test_a_job_nobody_can_handle_fails_at_once(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        job, _ = queue.enqueue(
            session, "mystery", "x", {}, now=datetime.now(UTC) - timedelta(seconds=1)
        )
        job_id = job.id

    assert worker(sessions, Recorder()).run_once() == "failed"
    dead = row(sessions, job_id)
    assert dead.attempts == 1 and "no handler" in (dead.last_error or "")


def test_a_handler_can_say_that_retrying_is_pointless(sessions: sessionmaker[Session]) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1))

    def refuse(payload: dict[str, Any]) -> dict[str, Any]:
        raise Permanent("the payload is wrong")

    assert worker(sessions, refuse).run_once() == "failed"
    assert row(sessions, job.id).attempts == 1


def test_a_slow_job_keeps_its_lease_while_it_works(sessions: sessionmaker[Session]) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1))
    recovered: list[int] = []

    def slow(payload: dict[str, Any]) -> dict[str, Any]:
        # three times the lease, with another worker looking for abandoned jobs throughout
        for _ in range(9):
            time.sleep(0.2)
            with sessions.begin() as session:
                recovered.append(queue.recover_expired(session, now=datetime.now(UTC)))
        return {"slow": True}

    w = worker(sessions, slow, lease=timedelta(seconds=0.6), heartbeat_every=timedelta(seconds=0.1))

    assert w.run_once() == "succeeded"
    assert sum(recovered) == 0
    assert row(sessions, job.id).attempts == 1


def test_a_worker_that_died_loses_its_job_to_the_next_worker(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(minutes=10))
    # a worker claimed it long ago and was never heard from again
    assert take(sessions, "dead-worker", at=datetime.now(UTC) - timedelta(minutes=5)) is not None
    handler = Recorder()

    assert worker(sessions, handler, "w2").run_once() == "succeeded"

    done = row(sessions, job.id)
    assert (done.status, done.attempts, handler.calls) == ("succeeded", 2, [{"key": "a"}])


def test_a_worker_that_lost_its_job_while_running_drops_its_result(
    sessions: sessionmaker[Session],
) -> None:
    job = add(sessions, at=datetime.now(UTC) - timedelta(seconds=1))

    def takeover(payload: dict[str, Any]) -> dict[str, Any]:
        # while this handler runs, its lease runs out and another worker takes the job
        with sessions.begin() as session:
            session.execute(text("UPDATE job SET lease_expires_at = now() - interval '1 second'"))
        with sessions.begin() as session:
            queue.recover_expired(session, now=datetime.now(UTC))
            assert queue.claim(session, "w2", now=datetime.now(UTC), lease=LEASE) is not None
        return {"from": "the slow worker"}

    assert worker(sessions, takeover, "w1").run_once() == "lost"

    held = row(sessions, job.id)
    assert (held.status, held.locked_by, held.result) == ("running", "w2", None)


def test_many_workers_run_every_job_exactly_once_even_when_some_fail_first(
    sessions: sessionmaker[Session],
) -> None:
    total = 40
    for n in range(total):
        add(sessions, f"job-{n}", at=datetime.now(UTC) - timedelta(seconds=1))
    lock = threading.Lock()
    succeeded: list[str] = []
    tried: dict[str, int] = {}

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        key = payload["key"]
        with lock:
            tried[key] = tried.get(key, 0) + 1
            first = tried[key] == 1
        if first and int(key.split("-")[1]) % 4 == 0:
            raise Retry("first attempt fails")
        with lock:
            succeeded.append(key)
        return {"seen": key}

    stop = threading.Event()
    workers = [worker(sessions, handler, f"w{n}", poll_seconds=0.01) for n in range(4)]
    threads = [threading.Thread(target=w.run_forever, args=(stop,)) for w in workers]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            with sessions() as session:
                done = session.execute(
                    text("SELECT count(*) FROM job WHERE status = 'succeeded'")
                ).scalar_one()
            if done == total:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        for t in threads:
            t.join(10)

    assert sorted(succeeded) == sorted(f"job-{n}" for n in range(total))
    with sessions() as session:
        states = session.execute(text("SELECT status, count(*) FROM job GROUP BY status")).all()
    assert dict(states) == {"succeeded": total}
    assert sum(tried.values()) == total + total // 4

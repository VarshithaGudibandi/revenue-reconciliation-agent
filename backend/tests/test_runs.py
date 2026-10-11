import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import recon.persistence.runs as runs_module
from recon.datagen.generate import Config, apply_corrections, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.model import Dataset
from recon.datagen.writer import write_dataset
from recon.engine.reconcile import reconcile
from recon.evaluation.score import records_from_dataset, score
from recon.persistence.models import (
    Profile,
    ResultException,
    ResultMatch,
    Run,
    RunInput,
)
from recon.persistence.repository import create_profile, register_file
from recon.persistence.runs import (
    MissingInputsError,
    UnknownRunError,
    get_or_create_run,
    input_hash,
    load_result,
    run_reconciliation,
    summarize,
)
from recon.profiles import EngineSettings, ProfileConfig

PERIOD = "2026-01"


@dataclass
class Env:
    sessions: sessionmaker[Session]
    profile_id: uuid.UUID
    first: Dataset
    second: Dataset
    directories: tuple[Path, Path]

    def register(self, source: str, revision: int) -> None:
        with self.sessions.begin() as session:
            register_file(
                session,
                profile_id=self.profile_id,
                source_name=source,
                period=PERIOD,
                path=self.directories[revision] / f"{source}.csv",
            )

    def run_id(self) -> uuid.UUID:
        with self.sessions.begin() as session:
            return get_or_create_run(session, profile_id=self.profile_id, period=PERIOD).run.id


@pytest.fixture
def env(migrated_database_url: str, tmp_path: Path) -> Iterator[Env]:
    first = generate_dataset(Config(seed=14, invoices=200, ambiguous_rate=0.04))
    second = apply_corrections(first, seed=14)
    write_dataset(first, tmp_path / "v1")
    write_dataset(second, tmp_path / "v2")

    engine = create_engine(migrated_database_url)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        profile_id = create_profile(
            session,
            name="demo",
            reconciliation_type="invoice_payment",
            config=ProfileConfig(
                sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
            ),
        ).id
    yield Env(sessions, profile_id, first, second, (tmp_path / "v1", tmp_path / "v2"))
    engine.dispose()


def count(env: Env, model: type) -> int:
    with env.sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_the_run_identity_depends_on_content_and_configuration() -> None:
    sources = {"invoices": "a" * 64, "payments": "b" * 64}

    assert input_hash(sources, "c" * 64) == input_hash(dict(reversed(sources.items())), "c" * 64)
    assert input_hash(sources, "c" * 64) != input_hash(sources, "d" * 64)
    assert input_hash(sources, "c" * 64) != input_hash({**sources, "payments": "e" * 64}, "c" * 64)


def test_a_run_needs_every_source(env: Env) -> None:
    env.register("invoices", 0)

    with pytest.raises(MissingInputsError, match="payments"):
        env.run_id()


def test_asking_again_for_the_same_inputs_returns_the_same_run(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)

    with env.sessions.begin() as session:
        first = get_or_create_run(session, profile_id=env.profile_id, period=PERIOD)
        run_id, status = first.run.id, first.run.status
    with env.sessions.begin() as session:
        again = get_or_create_run(session, profile_id=env.profile_id, period=PERIOD)
        again_id = again.run.id

    assert (first.created, again.created) == (True, False)
    assert again_id == run_id
    assert status == "pending"
    assert count(env, Run) == 1
    assert count(env, RunInput) == 2


def test_new_file_content_makes_a_new_run_and_a_revert_reuses_the_old_one(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    first = env.run_id()
    env.register("payments", 1)
    second = env.run_id()
    env.register("payments", 0)
    third = env.run_id()

    assert len({first, second}) == 2
    assert third == first
    assert count(env, Run) == 2


def test_a_configuration_change_makes_a_new_run(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    first = env.run_id()

    with env.sessions.begin() as session:
        profile = session.get_one(Profile, env.profile_id)
        profile.config = ProfileConfig(
            sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING},
            engine=EngineSettings(tolerance_minor=5),
        ).model_dump(mode="json")

    assert env.run_id() != first


def test_a_run_is_stored_and_agrees_with_the_ground_truth(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    run_id = env.run_id()

    summary = run_reconciliation(env.sessions, run_id)

    assert (summary.status, summary.error) == ("succeeded", None)
    assert summary.finished_at is not None
    invoices, payments = records_from_dataset(env.first)
    with env.sessions() as session:
        stored = load_result(session, run_id)
    assert stored == reconcile(invoices, payments)
    assert score(env.first, stored).perfect


def test_each_revision_keeps_its_own_results(env: Env) -> None:
    for source in ("invoices", "payments"):
        env.register(source, 0)
    first = env.run_id()
    run_reconciliation(env.sessions, first)
    env.register("payments", 1)
    second = env.run_id()
    run_reconciliation(env.sessions, second)

    with env.sessions() as session:
        first_result, second_result = load_result(session, first), load_result(session, second)

    assert score(env.first, first_result).perfect
    assert score(env.second, second_result).perfect
    assert len(second_result.exceptions) < len(first_result.exceptions)


def test_executing_a_finished_run_again_does_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    run_id = env.run_id()
    first = run_reconciliation(env.sessions, run_id)
    matches, exceptions = count(env, ResultMatch), count(env, ResultException)

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a finished run must not be recomputed")

    monkeypatch.setattr(runs_module, "reconcile", refuse)
    again = run_reconciliation(env.sessions, run_id)

    assert again == first
    assert (count(env, ResultMatch), count(env, ResultException)) == (matches, exceptions)


def test_a_failed_run_records_the_error_stores_nothing_and_can_be_retried(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    run_id = env.run_id()
    real_reconcile = reconcile

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(runs_module, "reconcile", broken)
    with pytest.raises(RuntimeError, match="engine exploded"):
        run_reconciliation(env.sessions, run_id)

    with env.sessions() as session:
        failed = summarize(session, run_id)
    assert failed.status == "failed"
    assert failed.error is not None and "RuntimeError: engine exploded" in failed.error
    assert (count(env, ResultMatch), count(env, ResultException)) == (0, 0)

    monkeypatch.setattr(runs_module, "reconcile", real_reconcile)
    retried = run_reconciliation(env.sessions, run_id)
    assert (retried.status, retried.error) == ("succeeded", None)
    with env.sessions() as session:
        assert score(env.first, load_result(session, run_id)).perfect


def test_concurrent_executions_do_the_work_once(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    run_id = env.run_id()
    real_reconcile = reconcile
    calls: list[int] = []
    barrier = threading.Barrier(6)

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real_reconcile(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runs_module, "reconcile", counting)

    def execute(_: int) -> str:
        barrier.wait()
        return run_reconciliation(env.sessions, run_id).status

    with ThreadPoolExecutor(6) as pool:
        statuses = list(pool.map(execute, range(6)))

    assert len(calls) == 1
    assert statuses.count("succeeded") >= 1
    assert set(statuses) <= {"running", "succeeded"}
    with env.sessions() as session:
        assert summarize(session, run_id).status == "succeeded"
        assert score(env.first, load_result(session, run_id)).perfect


def test_an_unknown_run_is_reported(env: Env) -> None:
    with env.sessions() as session, pytest.raises(UnknownRunError):
        summarize(session, uuid.uuid4())

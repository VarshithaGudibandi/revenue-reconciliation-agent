"""Incremental runs against a real database, checked against a full recompute."""

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

import recon.persistence.runs as runs_module
from recon.datagen.generate import apply_corrections
from recon.datagen.writer import write_dataset
from recon.engine.incremental import merge_incremental
from recon.engine.model import ReconciliationResult
from recon.evaluation.score import score
from recon.persistence.models import Profile
from recon.persistence.runs import (
    IncrementalMismatchError,
    RunSummary,
    load_result,
    run_reconciliation,
    summarize,
)
from recon.profiles import EngineSettings, ProfileConfig
from tests.conftest import Env, make_env


def execute(env: Env, *, mode: Literal["auto", "full"] = "auto") -> RunSummary:
    return run_reconciliation(env.sessions, env.run_id(), mode=mode, verify=True)


Rows = list[dict[str, str]]


def rewrite(source: Path, target: Path, edit: Callable[[Rows], Rows]) -> None:
    """Copy a CSV, letting ``edit`` change its rows."""
    with source.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames is not None
        rows = list(reader)
        header = list(reader.fieldnames)
    rows = edit(rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def lossy_merge(*args: Any, **kwargs: Any) -> ReconciliationResult:
    """A merge with a bug: it drops one exception."""
    merged = merge_incremental(*args, **kwargs)
    return ReconciliationResult(merged.matches, merged.exceptions[1:])


@pytest.mark.parametrize("seed", [3, 14, 27])
def test_an_incremental_run_equals_a_full_run_and_the_ground_truth(
    migrated_database_url: str, tmp_path: Path, seed: int
) -> None:
    with make_env(migrated_database_url, tmp_path, seed) as env:
        for source in ("invoices", "payments"):
            env.register(source, 0)
        first = execute(env)
        env.register("payments", 1)
        second = execute(env)

        assert first.mode == "full"
        assert (second.mode, second.base_run_id) == ("incremental", first.run_id)
        assert second.partitions_total is not None and second.partitions_recomputed is not None
        assert 0 < second.partitions_recomputed < second.partitions_total
        with env.sessions() as session:
            assert score(env.second, load_result(session, second.run_id)).perfect


def test_a_chain_of_revisions_each_builds_on_the_one_before(env: Env, tmp_path: Path) -> None:
    third = apply_corrections(env.second, seed=99, fix_fraction=1.0)
    write_dataset(third, tmp_path / "v3")
    env.register("invoices", 0)
    env.register("payments", 0)
    first = execute(env)
    env.register("payments", 1)
    second = execute(env)
    env.register_path("payments", tmp_path / "v3" / "payments.csv")
    last = execute(env)

    assert [s.mode for s in (first, second, last)] == ["full", "incremental", "incremental"]
    assert second.base_run_id == first.run_id
    assert last.base_run_id == second.run_id
    with env.sessions() as session:
        assert score(env.first, load_result(session, first.run_id)).perfect
        assert score(env.second, load_result(session, second.run_id)).perfect
        assert score(third, load_result(session, last.run_id)).perfect


def test_one_changed_payment_recomputes_one_customer(env: Env, tmp_path: Path) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)

    def bump_one(rows: Rows) -> Rows:
        rows[0]["amount_paid"] = "1.00"
        return rows

    changed = tmp_path / "edited" / "payments.csv"
    rewrite(env.directories[0] / "payments.csv", changed, bump_one)
    env.register_path("payments", changed)
    summary = execute(env)

    assert summary.mode == "incremental"
    assert summary.partitions_recomputed == 1


def test_edits_to_invoices_are_handled_too(env: Env, tmp_path: Path) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)

    def edit(rows: Rows) -> Rows:
        rows[0]["total"] = "999.99"  # changed amount
        rows[1]["customer_id"] = "CUST-0001"  # moved to another customer
        del rows[2:4]  # removed
        rows.append(  # added
            {
                "invoice_id": "INV-900001",
                "customer_id": "CUST-0002",
                "issue_date": "2026-01-15",
                "currency": "USD",
                "total": "123.45",
            }
        )
        return rows

    edited = tmp_path / "edited" / "invoices.csv"
    rewrite(env.directories[0] / "invoices.csv", edited, edit)
    env.register_path("invoices", edited)

    summary = execute(env)  # verify=True fails the run if it differs from a full recompute

    assert summary.mode == "incremental"
    assert summary.status == "succeeded"


def test_a_forced_full_run_ignores_earlier_runs(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)
    env.register("payments", 1)

    summary = execute(env, mode="full")

    assert (summary.mode, summary.base_run_id) == ("full", None)
    assert summary.partitions_recomputed == summary.partitions_total
    with env.sessions() as session:
        assert score(env.second, load_result(session, summary.run_id)).perfect


def test_a_changed_configuration_does_not_reuse_earlier_results(env: Env) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)
    with env.sessions.begin() as session:
        profile = session.get_one(Profile, env.profile_id)
        config = ProfileConfig.model_validate(profile.config)
        profile.config = ProfileConfig(
            sources=config.sources, engine=EngineSettings(tolerance_minor=5)
        ).model_dump(mode="json")

    summary = execute(env)

    assert (summary.mode, summary.base_run_id) == ("full", None)


def test_the_safety_net_fails_a_run_whose_incremental_result_is_wrong(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)
    env.register("payments", 1)
    monkeypatch.setattr(runs_module, "merge_incremental", lossy_merge)
    run_id = env.run_id()
    with pytest.raises(IncrementalMismatchError):
        run_reconciliation(env.sessions, run_id, verify=True)

    with env.sessions() as session:
        failed = summarize(session, run_id)
    assert failed.status == "failed"
    assert failed.error is not None and "IncrementalMismatchError" in failed.error

    monkeypatch.undo()
    retried = run_reconciliation(env.sessions, run_id, mode="full")
    assert retried.status == "succeeded"
    with env.sessions() as session:
        assert score(env.second, load_result(session, run_id)).perfect


def test_without_the_safety_net_a_wrong_merge_would_go_unnoticed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why verification exists: this documents what an unverified bug looks like."""
    env.register("invoices", 0)
    env.register("payments", 0)
    execute(env)
    env.register("payments", 1)
    monkeypatch.setattr(runs_module, "merge_incremental", lossy_merge)
    summary = run_reconciliation(env.sessions, env.run_id(), verify=False)

    assert summary.status == "succeeded"
    with env.sessions() as session:
        assert not score(env.second, load_result(session, summary.run_id)).perfect

"""Running an investigation for a case in the application, and acting on a person's decision.

The month the case belongs to is read from the latest files of its period, and the open cases of
that period are what the tools call open. A run is a LangGraph thread checkpointed in Postgres,
so the decision can come in a later request, after a restart. The model is called outside any
transaction; acting on an approved finding is one transaction, through the same case actions a
person uses, so it is locked and versioned like theirs.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from recon.ai.investigation.agent import PROMPT_VERSION, Investigation, investigate
from recon.ai.investigation.graph import build_graph
from recon.ai.investigation.ledger import Ledger, OpenCase
from recon.ai.llm.tools import ToolModel
from recon.cases.actions import AcceptReason, accept_variance, add_note
from recon.config import Settings
from recon.datagen.causes import CAUSES
from recon.files.normalize import NormalizedRecord
from recon.money import Money
from recon.persistence.models import CaseInvestigation, CaseRecord, ExceptionCase, SourceRecord
from recon.persistence.repository import latest_file_versions, role_sources_of

OPEN = ("open", "fix_reported")
_ALIAS = re.compile(r"\b[IPC]\d+\b")


class NotInvestigableError(ValueError):
    """The case is not one an investigation can look into."""


class DecisionError(ValueError):
    """The investigation is not waiting for a decision."""


@dataclass(frozen=True)
class Investigator:
    """The model that investigates, if one is configured."""

    model: ToolModel | None
    name: str | None

    @property
    def available(self) -> bool:
        return self.model is not None


def build_investigator(settings: Settings) -> Investigator:
    from recon.ai.llm.providers import live_client

    if settings.llm_key() is None:
        return Investigator(None, None)
    client = live_client(
        settings.llm_provider,
        model=settings.llm_model,
        key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        base_url=settings.llm_base_url,
        effort=settings.llm_effort,
    )
    return Investigator(client, settings.llm_model_name())


def checkpoint_url(database_url: str) -> str:
    """The database URL for psycopg, with LangGraph's tables in their own schema."""
    plain = re.sub(r"^postgresql\+psycopg://", "postgresql://", database_url)
    joiner = "&" if "?" in plain else "?"
    return f"{plain}{joiner}options=-csearch_path%3Dlanggraph"


_SET_UP: set[str] = set()


@contextmanager
def checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    url = checkpoint_url(database_url)
    with PostgresSaver.from_conn_string(url) as saver:
        if url not in _SET_UP:
            saver.setup()
            _SET_UP.add(url)
        yield saver


def _records(session: Session, file_version_id: uuid.UUID) -> list[NormalizedRecord]:
    rows = session.scalars(
        select(SourceRecord)
        .where(SourceRecord.file_version_id == file_version_id)
        .order_by(SourceRecord.row_number)
    )
    return [
        NormalizedRecord(
            source_row=r.row_number,
            record_id=r.record_id,
            reference=r.reference,
            counterparty=r.counterparty,
            occurred_on=r.occurred_on,
            amount=Money(r.amount_minor, r.currency),
            text=r.free_text,
        )
        for r in rows
    ]


def _open_case(case: ExceptionCase, records: list[CaseRecord]) -> OpenCase:
    variance = (
        None
        if case.variance_minor is None or case.currency is None
        else Money(case.variance_minor, case.currency)
    )
    return OpenCase(
        case.kind,
        tuple(sorted(r.record_id for r in records if r.role == "invoice")),
        tuple(sorted(r.record_id for r in records if r.role == "payment")),
        variance,
    )


def load_month(session: Session, case_id: uuid.UUID) -> tuple[Ledger, OpenCase]:
    case = session.get(ExceptionCase, case_id)
    if case is None:
        raise NotInvestigableError("there is no such case")
    if case.kind not in CAUSES:
        raise NotInvestigableError(f"a {case.kind} case cannot be investigated yet")
    if case.status not in OPEN:
        raise NotInvestigableError(f"a {case.status} case has nothing left to investigate")
    versions = latest_file_versions(session, case.profile_id, case.period)
    sources = role_sources_of(session, case.profile_id)
    invoices = _records(session, versions[sources["invoice"]].id)
    payments = _records(session, versions[sources["payment"]].id)
    cases = list(
        session.scalars(
            select(ExceptionCase).where(
                ExceptionCase.profile_id == case.profile_id,
                ExceptionCase.period == case.period,
                ExceptionCase.status.in_(OPEN),
            )
        )
    )
    records: dict[uuid.UUID, list[CaseRecord]] = {c.id: [] for c in cases}
    for row in session.scalars(select(CaseRecord).where(CaseRecord.case_id.in_(records))):
        records[row.case_id].append(row)
    open_cases = {c.id: _open_case(c, records[c.id]) for c in cases}
    return Ledger(invoices, payments, open_cases.values()), open_cases[case.id]


def real_text(text: str, aliases: dict[str, list[str]]) -> str:
    """The text with each alias replaced by the id it stands for."""
    return _ALIAS.sub(
        lambda m: aliases[m.group(0)][1] if m.group(0) in aliases else m.group(0), text
    )


def _aliases(ledger: Ledger) -> dict[str, list[str]]:
    return {alias: [role, real] for alias, (role, real) in ledger.aliases.to_real.items()}


def start(
    sessions: sessionmaker[Session],
    case_id: uuid.UUID,
    investigator: Investigator,
    database_url: str,
) -> uuid.UUID:
    """Investigate the case, and stop to wait for a person if the finding holds."""
    if investigator.model is None or investigator.name is None:
        raise NotInvestigableError("no language model is configured")
    model = investigator.model
    with sessions() as session:
        ledger, case = load_month(session, case_id)
    thread_id = f"investigation-{uuid.uuid4()}"
    done: list[Investigation] = []

    def run() -> Investigation:
        done.append(investigate(model, ledger, case))
        return done[-1]

    with checkpointer(database_url) as saver:
        graph = build_graph(run, _never_acts, saver)
        state = graph.invoke({}, {"configurable": {"thread_id": thread_id}})
    result = done[0]
    status = (
        "failed"
        if result.error
        else "awaiting_decision"
        if "__interrupt__" in state
        else "not_shown"
    )
    row = CaseInvestigation(
        case_id=case_id,
        thread_id=thread_id,
        status=status,
        model=investigator.name,
        prompt_version=PROMPT_VERSION,
        finding=result.finding,
        problems=result.problems,
        trace=result.trace,
        aliases=_aliases(ledger),
        error=result.error,
    )
    with sessions.begin() as session:
        session.add(row)
        session.flush()
        return row.id


def _never_acts(finding: dict[str, Any], decision: dict[str, Any]) -> str:
    raise RuntimeError("a run that was just started cannot have been decided")


def decide(
    sessions: sessionmaker[Session],
    investigation_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    expected_version: int | None,
    database_url: str,
) -> uuid.UUID:
    """Resume the run with a person's decision. Deciding again the same way is a no-op.

    The investigation's row is locked for the whole decision, so two people deciding at once
    take turns, and the second finds it decided. If acting fails (the case changed since it was
    read), nothing is recorded and the run can be decided again.
    """
    with sessions.begin() as session:
        row = session.get(CaseInvestigation, investigation_id, with_for_update=True)
        if row is None:
            raise DecisionError("there is no such investigation")
        if row.status in ("applied", "rejected"):
            if (row.status == "applied") == approve:
                return row.id
            raise DecisionError(f"the investigation was already {row.status}")
        if row.status != "awaiting_decision":
            raise DecisionError(f"an investigation that is {row.status} waits for no decision")
        aliases, model_name, case_id = row.aliases, row.model, row.case_id

        def act(finding: dict[str, Any], decision: dict[str, Any]) -> str:
            body = (
                f"Investigation by {model_name}, approved: {finding['cause'].replace('_', ' ')}. "
                + real_text(str(finding["note"]), aliases)
                + (f" Reviewer: {note}" if note else "")
            )
            with sessions.begin() as acting:
                if finding["action"] == "accept_variance":
                    accept_variance(
                        acting,
                        case_id,
                        reason=AcceptReason.WRITE_OFF,
                        note=body,
                        expected_version=expected_version,
                    )
                    return "accepted"
                add_note(acting, case_id, body)
                return "noted"

        with checkpointer(database_url) as saver:
            graph = build_graph(_already_investigated, act, saver)
            state = graph.invoke(
                Command(resume={"approve": approve, "note": note}),
                {"configurable": {"thread_id": row.thread_id}},
            )
        row.status = "applied" if approve else "rejected"
        row.outcome = state.get("outcome")
        row.decision_note = note
        row.decided_at = datetime.now(UTC)
        return row.id


def _already_investigated() -> Investigation:
    raise RuntimeError("a resumed run does not investigate again")


def latest(session: Session, case_id: uuid.UUID) -> CaseInvestigation | None:
    return session.scalars(
        select(CaseInvestigation)
        .where(CaseInvestigation.case_id == case_id)
        .order_by(CaseInvestigation.created_at.desc())
        .limit(1)
    ).first()

"""Explaining a case with a language model, and keeping what it wrote.

The rule-written reading of a case is always shown. A model's is made on request, kept for the
exact input it read, and shown in addition to it, only if every check in ``verify`` passed.
"""

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from recon.api.records import source_records
from recon.api.schemas import AskExplanationOut, ExplanationOut
from recon.api.suggestions import NotApplicableError
from recon.domain.money import Money
from recon.explanation.case import ExplainInput, RecordView
from recon.explanation.schema import PROMPT_VERSION, user_message
from recon.explanation.service import ExplainerChoice
from recon.persistence.case_actions import get_case_detail
from recon.persistence.models import CaseExplanation
from recon.persistence.repository import latest_file_versions

MAX_RECORDS = 12


@dataclass(frozen=True)
class LoadedCase:
    input: ExplainInput
    input_hash: str


def load_input(session: Session, case_id: uuid.UUID) -> LoadedCase | None:
    """What the model would read for a case: its records that are in the current files, under
    aliases, with the amounts and text. None when there is nothing to explain from."""
    detail = get_case_detail(session, case_id)
    case = detail.case
    versions = latest_file_versions(session, case.profile_id, case.period)
    found = source_records(session, versions, detail.records)
    records = [
        (role, found[(role, record_id)])
        for role, record_id in detail.records
        if (role, record_id) in found
    ]
    if not records or len(records) > MAX_RECORDS:
        return None
    views = tuple(
        RecordView(
            ref=f"R{index}",
            role=role,
            record_id=record.record_id,
            occurred_on=record.occurred_on,
            amount=Money(record.amount_minor, record.currency),
            text=record.free_text,
        )
        for index, (role, record) in enumerate(records, start=1)
    )
    proposed = next(
        (v.ref for v in views if v.role == "invoice" and v.record_id == case.proposed_invoice_id),
        None,
    )
    variance = (
        None
        if case.variance_minor is None or case.currency is None
        else Money(case.variance_minor, case.currency)
    )
    inp = ExplainInput(case.kind, variance, views, proposed)
    digest = hashlib.sha256(f"{PROMPT_VERSION}\n{user_message(inp)}".encode()).hexdigest()
    return LoadedCase(inp, digest)


def _out(row: CaseExplanation) -> ExplanationOut:
    if row.summary is None or row.why is None or row.next_steps is None:
        raise ValueError("an explanation marked verified has no text")
    return ExplanationOut(
        model=row.model, summary=row.summary, why=row.why, next_steps=list(row.next_steps)
    )


def _verified(session: Session, case_id: uuid.UUID, loaded: LoadedCase) -> CaseExplanation | None:
    return session.scalars(
        select(CaseExplanation).where(
            CaseExplanation.case_id == case_id,
            CaseExplanation.input_hash == loaded.input_hash,
            CaseExplanation.prompt_version == PROMPT_VERSION,
            CaseExplanation.verified,
        )
    ).first()


def current_explanation(session: Session, case_id: uuid.UUID) -> ExplanationOut | None:
    """The model's verified explanation of what the case holds now, if one was made."""
    loaded = load_input(session, case_id)
    if loaded is None:
        return None
    row = _verified(session, case_id, loaded)
    return None if row is None else _out(row)


def ask_explainer(
    sessions_factory: sessionmaker[Session], case_id: uuid.UUID, choice: ExplainerChoice
) -> AskExplanationOut:
    """Ask the model once for what the case holds now. Asking again for the same input returns
    the verified answer that was kept. The model is called outside any transaction."""
    with sessions_factory() as session:
        loaded = load_input(session, case_id)
        if loaded is None:
            raise NotApplicableError("the case has no records in the current files to explain")
        row = _verified(session, case_id, loaded)
        if row is not None:
            return AskExplanationOut(explanation=_out(row), problems=[])
    if choice.explainer is None or choice.model is None:
        return AskExplanationOut(explanation=None, problems=[])

    answer = choice.explainer.explain(loaded.input)
    values = {
        "case_id": case_id,
        "input_hash": loaded.input_hash,
        "prompt_version": PROMPT_VERSION,
        "model": choice.model,
        "verified": answer.text is not None,
        "summary": None if answer.text is None else answer.text.summary,
        "why": None if answer.text is None else answer.text.why,
        "next_steps": None if answer.text is None else list(answer.text.next_steps),
        "problems": list(answer.problems),
    }
    with sessions_factory.begin() as session:
        session.execute(
            insert(CaseExplanation)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["case_id", "input_hash", "prompt_version"],
                index_where=text("verified"),
            )
        )
    with sessions_factory() as session:
        row = _verified(session, case_id, loaded)
    return AskExplanationOut(
        explanation=None if row is None else _out(row), problems=list(answer.problems)
    )

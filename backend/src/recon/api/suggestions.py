"""Suggesting which invoice an ambiguous payment was meant for.

The suggestion shown with a case is the rules' reading of the memo, which is cheap and always
available. A suggestion from the model is made on request, kept for the exact input it read,
and shown in its place.
"""

import dataclasses
import hashlib
import json
import random
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from recon.adjudication.case import AdjudicationCase, Candidate, Ruling
from recon.adjudication.rules import adjudicate_by_rules
from recon.adjudication.service import AdjudicatorChoice
from recon.api.records import source_records
from recon.api.schemas import SuggestionOut
from recon.persistence.case_actions import get_case_detail
from recon.persistence.models import CaseSuggestion
from recon.persistence.repository import latest_file_versions

MODEL_ADJUDICATOR: Literal["rules and model"] = "rules and model"


class NotApplicableError(ValueError):
    """The case is not an ambiguous payment, or lacks what an adjudicator needs."""


@dataclass(frozen=True)
class AdjudicationInput:
    case: AdjudicationCase
    invoice_of: dict[str, str]
    input_hash: str


def load_input(session: Session, case_id: uuid.UUID) -> AdjudicationInput | None:
    """What an adjudicator would read for a case, or None if it is not an ambiguous payment with
    one payment and its look-alike invoices all present in the current files."""
    detail = get_case_detail(session, case_id)
    case = detail.case
    if case.kind != "ambiguous_payment":
        return None
    versions = latest_file_versions(session, case.profile_id, case.period)
    found = source_records(session, versions, detail.records)
    invoices = sorted(
        (
            found[(role, rid)]
            for role, rid in detail.records
            if role == "invoice" and (role, rid) in found
        ),
        key=lambda r: r.record_id,
    )
    payments = [
        found[(role, rid)]
        for role, rid in detail.records
        if role == "payment" and (role, rid) in found
    ]
    if len(payments) != 1 or not 2 <= len(invoices) <= 8:
        return None
    payment = payments[0]
    # A fixed shuffle, so that the position of an invoice says nothing about which was meant.
    random.Random(",".join(r.record_id for r in invoices)).shuffle(invoices)  # noqa: S311
    candidates = tuple(
        Candidate(
            label="ABCDEFGH"[index],
            invoice_id=record.record_id,
            description=record.free_text or "",
            days_before=(payment.occurred_on - record.occurred_on).days,
        )
        for index, record in enumerate(invoices)
    )
    adjudication = AdjudicationCase(payment.record_id, payment.free_text or "", candidates)
    digest = hashlib.sha256(
        json.dumps(
            {
                "memo": adjudication.memo,
                "candidates": [dataclasses.asdict(c) for c in candidates],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return AdjudicationInput(adjudication, {c.label: c.invoice_id for c in candidates}, digest)


def _out(
    loaded: AdjudicationInput,
    ruling: Ruling,
    source: Literal["rules", "rules and model"],
    model: str | None,
) -> SuggestionOut:
    return SuggestionOut(
        source=source,
        model=model,
        invoice_id=loaded.invoice_of.get(ruling.choice) if ruling.choice else None,
        confident=ruling.confident,
        evidence=ruling.evidence,
        reasons=list(ruling.reasons),
        alternatives=[loaded.invoice_of[a] for a in ruling.alternatives if a in loaded.invoice_of],
    )


def _stored(
    session: Session, case_id: uuid.UUID, loaded: AdjudicationInput
) -> CaseSuggestion | None:
    return session.scalars(
        select(CaseSuggestion).where(
            CaseSuggestion.case_id == case_id,
            CaseSuggestion.input_hash == loaded.input_hash,
            CaseSuggestion.adjudicator == MODEL_ADJUDICATOR,
        )
    ).first()


def _from_row(loaded: AdjudicationInput, row: CaseSuggestion) -> SuggestionOut:
    data = row.ruling
    ruling = Ruling(
        data["choice"],
        data["confident"],
        data["evidence"],
        tuple(data["reasons"]),
        tuple(data["alternatives"]),
    )
    return _out(loaded, ruling, MODEL_ADJUDICATOR, row.model)


def current_suggestion(session: Session, case_id: uuid.UUID) -> SuggestionOut | None:
    """The model's suggestion if one was made for what the case holds now, else the rules'."""
    loaded = load_input(session, case_id)
    if loaded is None:
        return None
    row = _stored(session, case_id, loaded)
    if row is not None:
        return _from_row(loaded, row)
    return _out(loaded, adjudicate_by_rules(loaded.case), "rules", None)


def ask_adjudicator(
    sessions_factory: sessionmaker[Session], case_id: uuid.UUID, adjudicator: AdjudicatorChoice
) -> SuggestionOut:
    """Ask the configured adjudicator once for what the case holds now.

    The model is called outside any transaction. Asking again for the same input returns what
    was kept, so a repeated request costs nothing.
    """
    with sessions_factory() as session:
        loaded = load_input(session, case_id)
        if loaded is None:
            raise NotApplicableError(
                "only an ambiguous payment with its invoices can be asked about"
            )
        row = _stored(session, case_id, loaded)
        if row is not None:
            return _from_row(loaded, row)
    if not adjudicator.uses_model:
        return _out(loaded, adjudicate_by_rules(loaded.case), "rules", None)

    ruling = adjudicator.adjudicate(loaded.case)
    with sessions_factory.begin() as session:
        session.execute(
            insert(CaseSuggestion)
            .values(
                case_id=case_id,
                input_hash=loaded.input_hash,
                adjudicator=MODEL_ADJUDICATOR,
                model=adjudicator.model,
                ruling=dataclasses.asdict(ruling),
            )
            .on_conflict_do_nothing(constraint="uq_case_suggestion_input")
        )
    with sessions_factory() as session:
        row = _stored(session, case_id, loaded)
        if row is None:
            raise LookupError("the suggestion that was just kept is missing")
        return _from_row(loaded, row)

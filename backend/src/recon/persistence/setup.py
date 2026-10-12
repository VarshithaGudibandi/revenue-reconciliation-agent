"""Mapping proposals and the confirmation that turns one into a profile.

Functions take a session and never commit. Confirming locks the proposal row, so two
confirmations of one proposal cannot both create a profile.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from recon.ingestion.normalize import ColumnMapping, normalize_csv
from recon.mapping.proposal import MappingProposal
from recon.persistence.models import MappingProposalRow, Profile
from recon.persistence.repository import create_profile, register_file
from recon.profiles import ProfileConfig, reconciliation_type


class UnknownProposalError(LookupError):
    pass


class ProposalConflictError(ValueError):
    """The proposal was already confirmed, with a different name or mapping."""


class UnusableMappingError(ValueError):
    """A mapping reads no records from its file."""


@dataclass(frozen=True)
class Confirmation:
    profile: Profile
    period: str
    created: bool
    records: dict[str, int]
    rejected: dict[str, int]


def create_proposal(
    session: Session,
    *,
    period: str,
    mapper: str,
    model: str | None,
    keys: dict[str, str],
    names: dict[str, str],
    proposal: MappingProposal,
    reconciliation_type: str = "invoice_payment",
) -> MappingProposalRow:
    row = MappingProposalRow(
        reconciliation_type=reconciliation_type,
        period=period,
        mapper=mapper,
        model=model,
        invoices_key=keys["invoices"],
        payments_key=keys["payments"],
        invoices_name=names["invoices"],
        payments_name=names["payments"],
        proposal=proposal.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row


def get_proposal(
    session: Session, proposal_id: uuid.UUID, *, lock: bool = False
) -> MappingProposalRow:
    statement = select(MappingProposalRow).where(MappingProposalRow.id == proposal_id)
    if lock:
        statement = statement.with_for_update()
    row = session.scalars(statement).first()
    if row is None:
        raise UnknownProposalError(f"no proposal {proposal_id}")
    return row


def confirm_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    *,
    name: str,
    mappings: dict[str, ColumnMapping],
    storage_dir: Path,
) -> Confirmation:
    """Create the profile from the confirmed mappings and register the two sample files.

    Each mapping must read at least one record from its file. Confirming again with the same
    name and mappings returns the profile that already exists instead of making another.
    """
    row = get_proposal(session, proposal_id, lock=True)
    wanted = {source: mapping.model_dump(mode="json") for source, mapping in mappings.items()}
    if row.status == "confirmed":
        profile = session.get(Profile, row.profile_id)
        if profile is None or profile.name != name or row.confirmed_mapping != wanted:
            raise ProposalConflictError("this proposal was already confirmed differently")
        return Confirmation(profile, row.period, False, {}, {})

    keys = {"invoices": row.invoices_key, "payments": row.payments_key}
    records: dict[str, int] = {}
    rejected: dict[str, int] = {}
    kind = reconciliation_type(row.reconciliation_type)
    source_of = {"invoices": kind.left, "payments": kind.right}
    for slot, mapping in mappings.items():
        result = normalize_csv(storage_dir / keys[slot], mapping)
        if not result.records:
            raise UnusableMappingError(
                f"the {source_of[slot]} mapping reads no records from the file"
            )
        records[source_of[slot]], rejected[source_of[slot]] = (
            len(result.records),
            len(result.rejected),
        )

    profile = create_profile(
        session,
        name=name,
        reconciliation_type=kind.key,
        config=ProfileConfig(sources={source_of[slot]: m for slot, m in mappings.items()}),
    )
    for slot in ("invoices", "payments"):
        register_file(
            session,
            profile_id=profile.id,
            source_name=source_of[slot],
            period=row.period,
            path=storage_dir / keys[slot],
            object_key=keys[slot],
        )
    row.status = "confirmed"
    row.profile_id = profile.id
    row.confirmed_mapping = wanted
    row.confirmed_at = datetime.now(UTC)
    session.flush()
    return Confirmation(profile, row.period, True, records, rejected)

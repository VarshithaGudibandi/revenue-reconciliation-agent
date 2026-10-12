"""Setting up a reconciliation: upload two sample files, review the proposed column mapping,
correct it, and confirm. Confirming creates the profile and reconciles the first period."""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from recon.api.routes import (
    ERROR_RESPONSES,
    AppSettings,
    Sessions,
    _outcome_out,
    _save_upload,
)
from recon.api.schemas import (
    ColumnChoiceOut,
    ConfirmBody,
    ConfirmOut,
    FilePreview,
    FileProposalOut,
    MappingBody,
    PreviewBody,
    PreviewOut,
    PreviewRecord,
    PreviewRejection,
    ProfileOut,
    ProposalOut,
    SettingChoiceOut,
    amount_of,
)
from recon.config import Settings
from recon.ingestion.normalize import ColumnMapping, MappingError, normalize_csv
from recon.mapping.profile import DATE_FORMATS, FileProfile, profile_csv_bytes
from recon.mapping.proposal import FIELDS, FileProposal, MappingProposal
from recon.mapping.service import MapperChoice
from recon.persistence.models import MappingProposalRow
from recon.persistence.repository import check_period
from recon.persistence.setup import (
    confirm_proposal,
    create_proposal,
    get_proposal,
)
from recon.pipeline import reconcile_period

router = APIRouter(prefix="/api/setup", responses=ERROR_RESPONSES)

SAMPLE_ROWS = 8
REJECTION_EXAMPLES = 3
SOURCES = ("invoices", "payments")


def get_mapper(request: Request) -> MapperChoice:
    mapper: MapperChoice = request.app.state.mapper
    return mapper


Mapper = Annotated[MapperChoice, Depends(get_mapper)]


def _file_out(
    source: str, name: str, profile: FileProfile, proposal: FileProposal
) -> FileProposalOut:
    names = FIELDS if source == "payments" else tuple(f for f in FIELDS if f != "reference")
    fields = [
        ColumnChoiceOut(
            field=n,
            column=proposal.choice(n).column,
            confident=proposal.choice(n).confident,
            alternatives=list(proposal.choice(n).alternatives),
            reasons=list(proposal.choice(n).reasons),
        )
        for n in names
    ]
    settings = (proposal.date_format, proposal.decimal_separator)
    return FileProposalOut(
        source=source,
        file_name=name,
        rows=profile.rows,
        headers=list(profile.headers()),
        fields=fields,
        date_format=SettingChoiceOut(
            value=proposal.date_format.value,
            confident=proposal.date_format.confident,
            alternatives=list(proposal.date_format.alternatives),
            reasons=list(proposal.date_format.reasons),
        ),
        decimal_separator=SettingChoiceOut(
            value=proposal.decimal_separator.value,
            confident=proposal.decimal_separator.confident,
            alternatives=list(proposal.decimal_separator.alternatives),
            reasons=list(proposal.decimal_separator.reasons),
        ),
        needs_answer=sum(1 for f in fields if not f.confident)
        + sum(1 for s in settings if not s.confident),
    )


def _profile_of(settings: Settings, key: str) -> FileProfile:
    return profile_csv_bytes(Path(key).name, (settings.storage_dir / key).read_bytes())


def _proposal_out(row: MappingProposalRow, settings: Settings) -> ProposalOut:
    stored = MappingProposal.model_validate(row.proposal)
    return ProposalOut(
        id=row.id,
        period=row.period,
        status=row.status,
        mapper=row.mapper,
        model=row.model,
        invoices=_file_out(
            "invoices", row.invoices_name, _profile_of(settings, row.invoices_key), stored.invoices
        ),
        payments=_file_out(
            "payments", row.payments_name, _profile_of(settings, row.payments_key), stored.payments
        ),
        date_formats=list(DATE_FORMATS),
        profile_id=row.profile_id,
    )


@router.post("/proposals", response_model=ProposalOut)
def propose(
    sessions: Sessions,
    settings: AppSettings,
    mapper: Mapper,
    invoices: Annotated[UploadFile, File()],
    payments: Annotated[UploadFile, File()],
    period: Annotated[str, Form()],
) -> ProposalOut:
    """Read two sample files and propose how their columns map to the fields we need.

    Nothing is configured yet: the proposal is kept until it is confirmed.
    """
    check_period(period)
    folder = f"proposals/{uuid.uuid4().hex}"
    keys = {"invoices": f"{folder}/invoices.csv", "payments": f"{folder}/payments.csv"}
    uploads = {"invoices": invoices, "payments": payments}
    try:
        for source in SOURCES:
            _save_upload(
                uploads[source],
                settings.storage_dir / keys[source],
                settings.max_upload_mb * 1024 * 1024,
            )
        profiles = {s: _profile_of(settings, keys[s]) for s in SOURCES}
        proposal = mapper.propose(profiles["invoices"], profiles["payments"])
        with sessions.begin() as session:
            row = create_proposal(
                session,
                period=period,
                mapper=mapper.name,
                model=mapper.model,
                keys=keys,
                names={s: Path(uploads[s].filename or f"{s}.csv").name for s in SOURCES},
                proposal=proposal,
            )
            return _proposal_out(row, settings)
    except BaseException:
        for key in keys.values():
            (settings.storage_dir / key).unlink(missing_ok=True)
        raise


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def read_proposal(proposal_id: uuid.UUID, sessions: Sessions, settings: AppSettings) -> ProposalOut:
    with sessions() as session:
        return _proposal_out(get_proposal(session, proposal_id), settings)


def _mapping(body: MappingBody) -> ColumnMapping:
    return ColumnMapping(**body.model_dump())


def _preview_file(source: str, path: Path, body: MappingBody) -> FilePreview:
    try:
        result = normalize_csv(path, _mapping(body))
    except MappingError as error:
        return FilePreview(
            source=source,
            error=str(error),
            records_read=0,
            rejected=0,
            sample=[],
            rejections=[],
        )
    return FilePreview(
        source=source,
        error=None,
        records_read=len(result.records),
        rejected=len(result.rejected),
        sample=[
            PreviewRecord(
                row=r.source_row,
                record_id=r.record_id,
                counterparty=r.counterparty,
                occurred_on=r.occurred_on,
                amount=amount_of(r.amount.minor, r.amount.currency),
                reference=r.reference,
            )
            for r in result.records[:SAMPLE_ROWS]
        ],
        rejections=[
            PreviewRejection(row=r.source_row, reason=r.reason)
            for r in result.rejected[:REJECTION_EXAMPLES]
        ],
    )


@router.post("/proposals/{proposal_id}/preview", response_model=PreviewOut)
def preview(
    proposal_id: uuid.UUID, body: PreviewBody, sessions: Sessions, settings: AppSettings
) -> PreviewOut:
    """Show how the sample files would be read with a mapping, before anything is saved."""
    with sessions() as session:
        row = get_proposal(session, proposal_id)
        keys = {"invoices": row.invoices_key, "payments": row.payments_key}
    previews = {
        source: _preview_file(source, settings.storage_dir / keys[source], getattr(body, source))
        for source in SOURCES
    }
    return PreviewOut(
        invoices=previews["invoices"],
        payments=previews["payments"],
        ready=all(p.error is None and p.records_read > 0 for p in previews.values()),
    )


@router.post("/proposals/{proposal_id}/confirm", response_model=ConfirmOut)
def confirm(
    proposal_id: uuid.UUID, body: ConfirmBody, sessions: Sessions, settings: AppSettings
) -> ConfirmOut:
    """Create the profile from the confirmed mapping, register the two sample files and
    reconcile the first period. Confirming the same proposal again with the same name and
    mapping returns the profile that exists."""
    mappings = {"invoices": _mapping(body.invoices), "payments": _mapping(body.payments)}
    with sessions.begin() as session:
        confirmation = confirm_proposal(
            session,
            proposal_id,
            name=body.name,
            mappings=mappings,
            storage_dir=settings.storage_dir,
        )
        profile_out = ProfileOut(
            id=confirmation.profile.id,
            name=confirmation.profile.name,
            reconciliation_type=confirmation.profile.reconciliation_type,
        )
    outcome = reconcile_period(sessions, profile_out.id, confirmation.period)
    run, cases = _outcome_out(outcome)
    return ConfirmOut(
        profile=profile_out,
        period=confirmation.period,
        created=confirmation.created,
        records=confirmation.records,
        rejected=confirmation.rejected,
        run=run,
        cases=cases,
    )

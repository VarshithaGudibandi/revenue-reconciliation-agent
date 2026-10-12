"""HTTP endpoints. Handlers parse the request, call the same functions the tests call,
and shape the response; they hold no matching or case logic of their own."""

import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session, sessionmaker

from recon.api import queries
from recon.api.idempotency import request_hash, run_once, validate_key
from recon.api.schemas import (
    AcceptBody,
    ActionBody,
    CaseDetailOut,
    CasePage,
    DemoOut,
    MatchPage,
    NoteBody,
    PeriodOut,
    PeriodSummary,
    ProfileOut,
    RunOut,
    SyncOut,
    UploadOut,
)
from recon.config import Settings
from recon.demo import DEMO_PERIOD, demo_state, load_corrected_export
from recon.persistence.case_actions import (
    AcceptReason,
    accept_variance,
    add_note,
    reopen_accepted,
    report_fix,
    retract_fix,
)
from recon.persistence.repository import (
    check_period,
    get_profile,
    latest_file_versions,
    register_file,
)
from recon.pipeline import PeriodOutcome, reconcile_period
from recon.profiles import RECONCILIATION_SOURCES

router = APIRouter(prefix="/api")

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def get_sessions(request: Request) -> sessionmaker[Session]:
    sessions: sessionmaker[Session] = request.app.state.sessions
    return sessions


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


Sessions = Annotated[sessionmaker[Session], Depends(get_sessions)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.get("/profiles", response_model=list[ProfileOut])
def profiles(sessions: Sessions) -> list[ProfileOut]:
    with sessions() as session:
        return [
            ProfileOut(id=p.id, name=p.name, reconciliation_type=p.reconciliation_type)
            for p in queries.list_profiles(session)
        ]


@router.get("/profiles/{profile_id}/periods", response_model=list[PeriodOut])
def periods(profile_id: uuid.UUID, sessions: Sessions) -> list[PeriodOut]:
    with sessions() as session:
        return queries.list_periods(session, profile_id)


@router.get("/profiles/{profile_id}/periods/{period}/summary", response_model=PeriodSummary)
def summary(profile_id: uuid.UUID, period: str, sessions: Sessions) -> PeriodSummary:
    with sessions() as session:
        return queries.period_summary(session, profile_id, period)


@router.get("/profiles/{profile_id}/periods/{period}/matches", response_model=MatchPage)
def matches(
    profile_id: uuid.UUID,
    period: str,
    sessions: Sessions,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MatchPage:
    with sessions() as session:
        return queries.list_matches(
            session, profile_id, period, search=q, limit=limit, offset=offset
        )


@router.get("/profiles/{profile_id}/periods/{period}/cases", response_model=CasePage)
def cases(
    profile_id: uuid.UUID,
    period: str,
    sessions: Sessions,
    status: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    sort: Literal["amount", "number", "updated"] = "amount",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CasePage:
    with sessions() as session:
        return queries.list_case_page(
            session,
            profile_id,
            period,
            statuses=status,
            kinds=kind,
            search=q,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@router.get("/cases/{case_id}", response_model=CaseDetailOut)
def case_detail(case_id: uuid.UUID, sessions: Sessions) -> CaseDetailOut:
    with sessions() as session:
        return queries.case_detail_out(session, case_id)


def _act(
    sessions: sessionmaker[Session],
    request: Request,
    key: str | None,
    case_id: uuid.UUID,
    body_json: str,
    do: Any,
) -> dict[str, Any]:
    fingerprint = request_hash(request.method, request.url.path, body_json)

    def action(session: Session) -> dict[str, Any]:
        do(session)
        return queries.case_detail_out(session, case_id).model_dump(mode="json")

    return run_once(sessions, None if key is None else validate_key(key), fingerprint, action)


@router.post("/cases/{case_id}/report-fix", response_model=CaseDetailOut)
def post_report_fix(
    case_id: uuid.UUID,
    body: ActionBody,
    request: Request,
    sessions: Sessions,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return _act(
        sessions, request, idempotency_key, case_id, body.model_dump_json(),
        lambda s: report_fix(s, case_id, expected_version=body.expected_version),
    )  # fmt: skip


@router.post("/cases/{case_id}/retract-fix", response_model=CaseDetailOut)
def post_retract_fix(
    case_id: uuid.UUID,
    body: ActionBody,
    request: Request,
    sessions: Sessions,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return _act(
        sessions, request, idempotency_key, case_id, body.model_dump_json(),
        lambda s: retract_fix(s, case_id, expected_version=body.expected_version),
    )  # fmt: skip


@router.post("/cases/{case_id}/accept", response_model=CaseDetailOut)
def post_accept(
    case_id: uuid.UUID,
    body: AcceptBody,
    request: Request,
    sessions: Sessions,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return _act(
        sessions, request, idempotency_key, case_id, body.model_dump_json(),
        lambda s: accept_variance(
            s, case_id, reason=AcceptReason(body.reason), note=body.note,
            expected_version=body.expected_version,
        ),
    )  # fmt: skip


@router.post("/cases/{case_id}/reopen", response_model=CaseDetailOut)
def post_reopen(
    case_id: uuid.UUID,
    body: ActionBody,
    request: Request,
    sessions: Sessions,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return _act(
        sessions, request, idempotency_key, case_id, body.model_dump_json(),
        lambda s: reopen_accepted(s, case_id, expected_version=body.expected_version),
    )  # fmt: skip


@router.post("/cases/{case_id}/notes", response_model=CaseDetailOut)
def post_note(
    case_id: uuid.UUID,
    body: NoteBody,
    request: Request,
    sessions: Sessions,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return _act(
        sessions, request, idempotency_key, case_id, body.model_dump_json(),
        lambda s: add_note(s, case_id, body.body),
    )  # fmt: skip


def _outcome_out(outcome: PeriodOutcome) -> tuple[RunOut, SyncOut | None]:
    run = outcome.run
    cases = outcome.cases
    return (
        RunOut(
            id=run.run_id,
            status=run.status,
            mode=run.mode,
            partitions_total=run.partitions_total,
            partitions_recomputed=run.partitions_recomputed,
            finished_at=run.finished_at,
        ),
        None
        if cases is None
        else SyncOut(
            applied=cases.applied,
            opened=cases.opened,
            updated=cases.updated,
            reopened=cases.reopened,
            resolved=cases.resolved,
            withdrawn=cases.withdrawn,
            fix_not_verified=cases.fix_not_verified,
        ),
    )


def _save_upload(upload: UploadFile, destination: Path, limit_bytes: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit_bytes:
                raise HTTPException(status_code=413, detail="file too large")
            handle.write(chunk)


@router.post("/profiles/{profile_id}/periods/{period}/files/{source}", response_model=UploadOut)
def upload_file(
    profile_id: uuid.UUID,
    period: str,
    source: str,
    sessions: Sessions,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> UploadOut:
    """Register a source file for a period and, once every source is present, reconcile it.

    Reconciliation runs inside the request until a job queue takes it over.
    """
    check_period(period)
    key = f"{profile_id}/{period}/{source}/{uuid.uuid4().hex}.csv"
    destination = settings.storage_dir / key
    try:
        _save_upload(file, destination, settings.max_upload_mb * 1024 * 1024)
        with sessions.begin() as session:
            registration = register_file(
                session,
                profile_id=profile_id,
                source_name=source,
                period=period,
                path=destination,
                object_key=key,
            )
            file_info = queries.file_out(registration.version)
            created = registration.created
            versions = latest_file_versions(session, profile_id, period)
            complete = all(name in versions for name in _sources_of(session, profile_id))
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if not created:
        destination.unlink(missing_ok=True)  # the same content is already stored

    run_info: RunOut | None = None
    sync_info: SyncOut | None = None
    if complete:
        run_info, sync_info = _outcome_out(reconcile_period(sessions, profile_id, period))
    return UploadOut(
        file=file_info, created=created, reconciled=complete, run=run_info, cases=sync_info
    )


def _sources_of(session: Session, profile_id: uuid.UUID) -> tuple[str, ...]:
    return RECONCILIATION_SOURCES[get_profile(session, profile_id).reconciliation_type]


demo_router = APIRouter(prefix="/api/demo")


@demo_router.get("", response_model=DemoOut)
def demo(sessions: Sessions, settings: AppSettings) -> DemoOut:
    state = demo_state(sessions, settings.storage_dir)
    return DemoOut(
        enabled=True,
        profile_id=state.profile_id,
        period=DEMO_PERIOD,
        corrected_files_loaded=state.corrected_files_loaded,
    )


@demo_router.post("/load-corrected-export", response_model=UploadOut)
def load_corrected(sessions: Sessions, settings: AppSettings) -> UploadOut:
    state = demo_state(sessions, settings.storage_dir)
    if state.profile_id is None:
        raise HTTPException(status_code=409, detail="the demo has not been seeded")
    outcome = load_corrected_export(sessions, settings.storage_dir, state.profile_id)
    with sessions() as session:
        version = latest_file_versions(session, state.profile_id, DEMO_PERIOD)["payments"]
        file_info = queries.file_out(version)
    run_info, sync_info = _outcome_out(outcome)
    return UploadOut(file=file_info, created=True, reconciled=True, run=run_info, cases=sync_info)

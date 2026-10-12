"""The shapes the API sends and receives."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from recon.domain.money import format_amount


class Amount(BaseModel):
    """Money as exact minor units, with a display string so clients never do the arithmetic."""

    minor: int
    currency: str
    display: str


def amount_of(minor: int, currency: str) -> Amount:
    return Amount(
        minor=minor,
        currency=currency,
        display=f"{format_amount(minor, currency, thousands=True)} {currency}",
    )


class ProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    reconciliation_type: str


class FileOut(BaseModel):
    source: str
    version_number: int
    record_count: int
    rejected_count: int
    size_bytes: int
    object_key: str
    registered_at: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    mode: str | None
    partitions_total: int | None
    partitions_recomputed: int | None
    finished_at: datetime | None


class PeriodOut(BaseModel):
    period: str
    files: list[FileOut]


class KindTotal(BaseModel):
    kind: str
    count: int
    # Absolute variance, summed per currency, for the cases that still need attention.
    amounts: list[Amount]


class PeriodSummary(BaseModel):
    profile_id: uuid.UUID
    period: str
    state: Literal["no_files", "waiting_for_files", "not_reconciled", "reconciled"]
    files: list[FileOut]
    run: RunOut | None
    invoices: int
    payments: int
    matched_invoices: int
    reconciliation_rate: float | None
    needs_attention: int
    unverified_fixes: int
    status_counts: dict[str, int]
    by_kind: list[KindTotal]
    cases_up_to_date: bool


class RecordOut(BaseModel):
    role: Literal["invoice", "payment"]
    record_id: str
    customer: str | None
    occurred_on: date | None
    amount: Amount | None
    in_current_files: bool


class CaseRow(BaseModel):
    id: uuid.UUID
    number: int
    status: str
    kind: str
    summary: str
    customer: str | None
    variance: Amount | None
    invoice_ids: list[str]
    payment_ids: list[str]
    proposed_invoice_id: str | None
    fix_unverified: bool
    accepted_reason: str | None
    version: int
    updated_at: datetime


class CasePage(BaseModel):
    total: int
    items: list[CaseRow]


class MatchRow(BaseModel):
    invoice_id: str
    customer: str
    issue_date: date
    amount: Amount
    payment_ids: list[str]
    paid: Amount
    variance: Amount
    rule: str


class MatchPage(BaseModel):
    total: int
    items: list[MatchRow]


class EventOut(BaseModel):
    id: int
    kind: str
    from_status: str | None
    to_status: str | None
    run_id: uuid.UUID | None
    detail: dict[str, object]
    created_at: datetime


class NoteOut(BaseModel):
    id: int
    body: str
    created_at: datetime


class AnalysisOut(BaseModel):
    """A plain-language reading of the case, written by rules from the records."""

    summary: str
    why: str
    next_steps: list[str]


class CaseDetailOut(BaseModel):
    id: uuid.UUID
    number: int
    profile_id: uuid.UUID
    period: str
    status: str
    kind: str
    variance: Amount | None
    proposed_invoice_id: str | None
    fix_unverified: bool
    accepted_reason: str | None
    closed_reason: str | None
    successor_case_id: uuid.UUID | None
    resolved_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    analysis: AnalysisOut
    records: list[RecordOut]
    events: list[EventOut]
    notes: list[NoteOut]


class ActionBody(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)


class AcceptBody(ActionBody):
    reason: Literal[
        "timing_difference", "rounding_difference", "write_off", "known_data_issue", "other"
    ]
    note: str = Field(min_length=1, max_length=2000)


class NoteBody(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class SyncOut(BaseModel):
    applied: bool
    opened: int
    updated: int
    reopened: int
    resolved: int
    withdrawn: int
    fix_not_verified: int


class UploadOut(BaseModel):
    file: FileOut
    created: bool
    reconciled: bool
    run: RunOut | None
    cases: SyncOut | None


class DemoOut(BaseModel):
    enabled: bool
    profile_id: uuid.UUID | None
    period: str
    corrected_files_loaded: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    current_version: int | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail


class ColumnChoiceOut(BaseModel):
    field: str
    column: str | None
    confident: bool
    alternatives: list[str]
    reasons: list[str]


class SettingChoiceOut(BaseModel):
    value: str | None
    confident: bool
    alternatives: list[str]
    reasons: list[str]


class FileProposalOut(BaseModel):
    source: Literal["invoices", "payments"]
    file_name: str
    rows: int
    headers: list[str]
    fields: list[ColumnChoiceOut]
    date_format: SettingChoiceOut
    decimal_separator: SettingChoiceOut
    needs_answer: int


class ProposalOut(BaseModel):
    id: uuid.UUID
    period: str
    status: Literal["proposed", "confirmed"]
    mapper: str
    model: str | None
    invoices: FileProposalOut
    payments: FileProposalOut
    date_formats: list[str]
    profile_id: uuid.UUID | None


class MappingBody(BaseModel):
    record_id: str = Field(min_length=1)
    counterparty: str = Field(min_length=1)
    date: str = Field(min_length=1)
    amount: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    reference: str | None = None
    date_format: str = Field(min_length=1)
    decimal_separator: Literal[".", ","]


class PreviewBody(BaseModel):
    invoices: MappingBody
    payments: MappingBody


class PreviewRecord(BaseModel):
    row: int
    record_id: str
    counterparty: str
    occurred_on: date
    amount: Amount
    reference: str | None


class PreviewRejection(BaseModel):
    row: int
    reason: str


class FilePreview(BaseModel):
    source: Literal["invoices", "payments"]
    error: str | None
    records_read: int
    rejected: int
    sample: list[PreviewRecord]
    rejections: list[PreviewRejection]


class PreviewOut(BaseModel):
    invoices: FilePreview
    payments: FilePreview
    ready: bool


class ConfirmBody(PreviewBody):
    name: str = Field(min_length=1, max_length=120)


class ConfirmOut(BaseModel):
    profile: ProfileOut
    period: str
    created: bool
    records: dict[str, int]
    rejected: dict[str, int]
    run: RunOut | None
    cases: SyncOut | None

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


class TypeLabels(BaseModel):
    """How to talk about the two sides of a type of reconciliation."""

    type: str
    title: str
    left: str
    left_plural: str
    right: str
    right_plural: str


class PeriodSummary(BaseModel):
    """``invoices``, ``payments`` and ``matched_invoices`` count the left file, the right file
    and the left records that reconciled, whatever the type; ``labels`` says what they are."""

    profile_id: uuid.UUID
    labels: TypeLabels
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
    """``role`` is the side of the match whatever the type (``invoice`` is the one record,
    ``payment`` one of the several that settle it); ``label`` is what to call it: Invoice,
    Payment, Deposit or Payout."""

    role: Literal["invoice", "payment"]
    label: str
    record_id: str
    customer: str | None
    occurred_on: date | None
    amount: Amount | None
    in_current_files: bool
    text: str | None = None


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


class ExplanationOut(BaseModel):
    """A language model's reading of a case. Shown only after every figure, record and id in it
    was checked against the case."""

    model: str
    summary: str
    why: str
    next_steps: list[str]


class AskExplanationOut(BaseModel):
    """The answer to asking for an explanation: the checked text, or why there is none."""

    explanation: ExplanationOut | None
    problems: list[str]


class SuggestionOut(BaseModel):
    """Which invoice an ambiguous payment was probably meant for. Advice for a person, never a
    match: nothing is changed by it."""

    source: Literal["rules", "rules and model"]
    model: str | None
    invoice_id: str | None
    confident: bool
    evidence: str | None
    reasons: list[str]
    alternatives: list[str]


class ToolOut(BaseModel):
    name: str
    model: str | None


class CapabilitiesOut(BaseModel):
    """Who makes the suggestions, so the interface can say so and offer to ask the model."""

    mapper: ToolOut
    adjudicator: ToolOut
    explainer: ToolOut


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
    suggestion: SuggestionOut | None = None
    explanation: ExplanationOut | None = None
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


class JobOut(BaseModel):
    """Work handed to a worker. ``run`` and ``cases`` are set once it has succeeded."""

    id: uuid.UUID
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
    run: RunOut | None
    cases: SyncOut | None


class UploadOut(BaseModel):
    """``reconciled`` is true when the run was done inside the request. With a job queue the
    request returns ``job`` instead, with status 202, and ``run`` and ``cases`` come later."""

    file: FileOut
    created: bool
    reconciled: bool
    run: RunOut | None
    cases: SyncOut | None
    job: JobOut | None = None


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
    """``invoices`` is the first file and ``payments`` the second, whatever the type; ``labels``
    says what they are (deposits and payouts, for instance)."""

    id: uuid.UUID
    labels: TypeLabels
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
    text: str | None = None
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
    job: JobOut | None = None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    channel_id: uuid.UUID
    channel_name: str
    kind: Literal["run", "digest", "test"]
    subject: str
    status: Literal["sending", "sent", "failed"]
    attempts: int
    error: str | None
    created_at: datetime
    sent_at: datetime | None


class ChannelOut(BaseModel):
    """Where alerts go. The target is masked: a webhook URL is a secret."""

    id: uuid.UUID
    name: str
    kind: Literal["email", "slack", "teams"]
    target: str
    enabled: bool
    on_new_cases: bool
    digest_hour: int | None
    created_at: datetime
    last_delivery: DeliveryOut | None


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["email", "slack", "teams"]
    target: str = Field(min_length=3, max_length=2000)
    enabled: bool = True
    on_new_cases: bool = True
    digest_hour: int | None = Field(default=None, ge=0, le=23)


class ChannelPatch(BaseModel):
    """Only the fields that are sent change. A ``digest_hour`` of null turns the digest off."""

    enabled: bool | None = None
    on_new_cases: bool | None = None
    digest_hour: int | None = Field(default=None, ge=0, le=23)

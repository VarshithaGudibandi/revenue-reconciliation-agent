"""Relational schema.

Source files are immutable versions. Each version is normalized into a snapshot of
records, so two versions can be compared with a join. A run is identified by the
file versions and configuration it read, and its results hang off it.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from recon.db import Base

SHA256_HEX = r"^[0-9a-f]{64}$"
CURRENCY_CODE = r"^[A-Z]{3}$"
PERIOD = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def identity_pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Profile(Base):
    """A reconciliation setup: its type, column mappings and matching thresholds."""

    __tablename__ = "profile"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    reconciliation_type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at()


class SourceFileVersion(Base):
    """One state of a source file for a period. Never modified once written.

    Versions form a history: registering content equal to the latest version changes
    nothing, but content that matches an older version becomes a new version, so the
    latest version is always what the source currently holds.
    """

    __tablename__ = "source_file_version"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "source_name",
            "period",
            "version_number",
            name="uq_source_file_version_number",
        ),
        CheckConstraint(f"period ~ '{PERIOD}'", name="period_format"),
        CheckConstraint(f"content_sha256 ~ '{SHA256_HEX}'", name="sha256_format"),
        CheckConstraint("version_number >= 1", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id"), nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at()


class SourceRecord(Base):
    """A normalized row of one file version."""

    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint("file_version_id", "record_id", name="uq_source_record_identity"),
        Index("ix_source_record_version_counterparty", "file_version_id", "counterparty"),
        CheckConstraint(f"currency ~ '{CURRENCY_CODE}'", name="currency_format"),
        CheckConstraint(f"content_hash ~ '{SHA256_HEX}'", name="content_hash_format"),
    )

    id: Mapped[int] = identity_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_file_version.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    record_id: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str | None] = mapped_column(Text)
    free_text: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class RejectedRow(Base):
    """A row that could not be normalized, kept so nothing is dropped silently."""

    __tablename__ = "rejected_row"

    id: Mapped[int] = identity_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_file_version.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)


class Run(Base):
    """One reconciliation of a period, identified by the file versions and config it read."""

    __tablename__ = "run"
    __table_args__ = (
        UniqueConstraint("profile_id", "period", "input_hash", name="uq_run_inputs"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name="status_known"
        ),
        CheckConstraint(f"input_hash ~ '{SHA256_HEX}'", name="input_hash_format"),
        CheckConstraint(f"config_hash ~ '{SHA256_HEX}'", name="config_hash_format"),
        CheckConstraint("mode IN ('full', 'incremental')", name="mode_known"),
        CheckConstraint(
            "(mode = 'incremental') = (base_run_id IS NOT NULL)", name="incremental_has_base"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How a finished run got its result: from scratch, or by reusing the unchanged
    # partitions of an earlier run.
    mode: Mapped[str | None] = mapped_column(Text)
    base_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    partitions_total: Mapped[int | None] = mapped_column(Integer)
    partitions_recomputed: Mapped[int | None] = mapped_column(Integer)


class RunInput(Base):
    """The file versions a run read."""

    __tablename__ = "run_input"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), primary_key=True
    )
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_file_version.id"), primary_key=True
    )


class ResultMatch(Base):
    __tablename__ = "result_match"
    __table_args__ = (
        UniqueConstraint("run_id", "invoice_record_id", name="uq_result_match_invoice"),
        CheckConstraint(f"currency ~ '{CURRENCY_CODE}'", name="currency_format"),
    )

    id: Mapped[int] = identity_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    invoice_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"), nullable=False)
    variance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rule: Mapped[str] = mapped_column(Text, nullable=False)


class ResultMatchPayment(Base):
    __tablename__ = "result_match_payment"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("result_match.id", ondelete="CASCADE"), primary_key=True
    )
    payment_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"), primary_key=True)


class ResultException(Base):
    __tablename__ = "result_exception"
    __table_args__ = (
        CheckConstraint(
            "(variance_minor IS NULL) = (currency IS NULL)", name="variance_has_currency"
        ),
    )

    id: Mapped[int] = identity_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    variance_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    proposed_invoice_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))


class ResultExceptionRecord(Base):
    __tablename__ = "result_exception_record"
    __table_args__ = (CheckConstraint("role IN ('invoice', 'payment')", name="role_known"),)

    exception_id: Mapped[int] = mapped_column(
        ForeignKey("result_exception.id", ondelete="CASCADE"), primary_key=True
    )
    record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)


CASE_STATUSES = ("open", "fix_reported", "accepted", "resolved", "withdrawn")


class ExceptionCase(Base):
    """A problem tracked across runs.

    A case is matched to the exceptions of later runs by the records they share, so it
    keeps its identity, notes and history while its kind, amounts and records change.
    The check constraints tie each status to the fields that justify it.
    """

    __tablename__ = "exception_case"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'fix_reported', 'accepted', 'resolved', 'withdrawn')",
            name="status_known",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(variance_minor IS NULL) = (currency IS NULL)", name="variance_has_currency"
        ),
        CheckConstraint(f"signature ~ '{SHA256_HEX}'", name="signature_format"),
        CheckConstraint(
            "(status = 'resolved') = (resolved_run_id IS NOT NULL)", name="resolved_has_run"
        ),
        CheckConstraint(
            "(status = 'fix_reported') = (fix_reported_run_id IS NOT NULL)", name="fix_has_run"
        ),
        CheckConstraint(
            "(status = 'accepted') = (accepted_reason IS NOT NULL)", name="accepted_has_reason"
        ),
        CheckConstraint(
            "(accepted_reason IS NULL) = (accepted_signature IS NULL)",
            name="accepted_has_signature",
        ),
        CheckConstraint(
            "(status = 'withdrawn') = (closed_reason IS NOT NULL)", name="withdrawn_has_reason"
        ),
        CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('records_removed', 'merged')",
            name="closed_reason_known",
        ),
        CheckConstraint(
            "(closed_reason = 'merged') = (successor_case_id IS NOT NULL)",
            name="merged_has_successor",
        ),
        CheckConstraint(
            "NOT fix_unverified OR status = 'open'",
            name="unverified_fix_is_open",
        ),
        UniqueConstraint("number", name="uq_exception_case_number"),
        Index("ix_exception_case_profile_period", "profile_id", "period", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Creation order, and a short reference people can quote.
    number: Mapped[int] = mapped_column(BigInteger, Identity(always=False), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    variance_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    proposed_invoice_id: Mapped[str | None] = mapped_column(Text)
    # Fingerprint of the kind, variance and content of the records involved. It is what an
    # accepted case is compared against to notice that the data changed.
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    first_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), nullable=False)
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), nullable=False)
    resolved_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_reason: Mapped[str | None] = mapped_column(Text)
    accepted_signature: Mapped[str | None] = mapped_column(String(64))
    fix_reported_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    fix_unverified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    closed_reason: Mapped[str | None] = mapped_column(Text)
    successor_case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exception_case.id"))


class CaseRecord(Base):
    """A record currently involved in a case. Ids are the source's own, stable across versions."""

    __tablename__ = "case_record"
    __table_args__ = (
        CheckConstraint("role IN ('invoice', 'payment')", name="role_known"),
        Index("ix_case_record_lookup", "role", "record_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception_case.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    record_id: Mapped[str] = mapped_column(Text, primary_key=True)


class CaseNote(Base):
    __tablename__ = "case_note"
    __table_args__ = (CheckConstraint("length(btrim(body)) > 0", name="body_not_blank"),)

    id: Mapped[int] = identity_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception_case.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()


class CaseEvent(Base):
    """Audit log. The database refuses to change or delete a row once written."""

    __tablename__ = "case_event"

    id: Mapped[int] = identity_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exception_case.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()


class CaseSync(Base):
    """The run whose results the cases of a period currently reflect."""

    __tablename__ = "case_sync"

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), nullable=False)
    synced_at: Mapped[datetime] = created_at()


class IdempotencyRecord(Base):
    """The stored response of a request that carried an idempotency key."""

    __tablename__ = "idempotency_record"
    __table_args__ = (
        CheckConstraint(f"request_hash ~ '{SHA256_HEX}'", name="request_hash_format"),
        CheckConstraint("(status_code IS NULL) = (response IS NULL)", name="response_complete"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


class MappingProposalRow(Base):
    """Two sample files and the column mapping proposed for them, until the user confirms it.

    A proposal never changes how a file is read. Only confirming it creates a profile, and the
    confirmed mapping is stored next to what was proposed.
    """

    __tablename__ = "mapping_proposal"
    __table_args__ = (
        CheckConstraint(f"period ~ '{PERIOD}'", name="period_format"),
        CheckConstraint("status IN ('proposed', 'confirmed')", name="status_known"),
        CheckConstraint(
            "(status = 'confirmed') = (profile_id IS NOT NULL)", name="profile_iff_confirmed"
        ),
        CheckConstraint(
            "(status = 'confirmed') = (confirmed_mapping IS NOT NULL)",
            name="mapping_iff_confirmed",
        ),
        CheckConstraint(
            "(status = 'confirmed') = (confirmed_at IS NOT NULL)", name="time_iff_confirmed"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    mapper: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    # The first file is kept in the invoices slot and the second in the payments slot, whatever
    # the type: the left side of the reconciliation and the right side.
    reconciliation_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'invoice_payment'")
    )
    invoices_key: Mapped[str] = mapped_column(Text, nullable=False)
    payments_key: Mapped[str] = mapped_column(Text, nullable=False)
    invoices_name: Mapped[str] = mapped_column(Text, nullable=False)
    payments_name: Mapped[str] = mapped_column(Text, nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'proposed'"))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profile.id"))
    confirmed_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class CaseSuggestion(Base):
    """What an adjudicator suggested for an ambiguous payment, kept for the exact input it read.

    The input hash covers the memo and everything the adjudicator saw about the candidates, so a
    changed file gives a new row and an unchanged one is never asked twice.
    """

    __tablename__ = "case_suggestion"
    __table_args__ = (
        UniqueConstraint("case_id", "input_hash", "adjudicator", name="uq_case_suggestion_input"),
        CheckConstraint(f"input_hash ~ '{SHA256_HEX}'", name="input_hash_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception_case.id", ondelete="CASCADE"), nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    adjudicator: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    ruling: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at()


class CaseExplanation(Base):
    """An explanation a language model wrote for a case, kept for the exact input it read.

    Only a verified one is reused; a rejected attempt is kept with what the checks found.
    """

    __tablename__ = "case_explanation"
    __table_args__ = (
        Index(
            "uq_case_explanation_verified",
            "case_id",
            "input_hash",
            "prompt_version",
            unique=True,
            postgresql_where=text("verified"),
        ),
        CheckConstraint(f"input_hash ~ '{SHA256_HEX}'", name="input_hash_format"),
        CheckConstraint(
            "verified = (summary IS NOT NULL AND why IS NOT NULL AND next_steps IS NOT NULL)",
            name="text_iff_verified",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception_case.id", ondelete="CASCADE"), nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    why: Mapped[str | None] = mapped_column(Text)
    next_steps: Mapped[list[str] | None] = mapped_column(JSONB)
    problems: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at()


class Job(Base):
    """A unit of work for a worker: what to do, who holds it and until when, how it ended.

    A queued job is claimed with ``FOR UPDATE SKIP LOCKED``; the claim sets a lease that the
    worker extends while it works. A job whose lease runs out is queued again by the next worker
    that looks, so a crashed worker loses nothing.
    """

    __tablename__ = "job"
    __table_args__ = (
        Index(
            "uq_job_queued_per_key",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_job_claimable",
            "run_after",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_job_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_known"
        ),
        CheckConstraint(
            "(status = 'running') = (locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_iff_running",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name="finished_iff_done",
        ),
        CheckConstraint(
            "(status = 'succeeded') = (result IS NOT NULL)", name="result_iff_succeeded"
        ),
        CheckConstraint("status <> 'failed' OR last_error IS NOT NULL", name="failed_has_error"),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="attempts_in_range",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestedObject(Base):
    """What a bucket scan did with one version of an object (its key and ETag).

    Registered, unchanged and failed are final for that version. A skipped object is looked at
    again on every scan, because the reason (no such profile yet, say) may stop applying.
    """

    __tablename__ = "ingested_object"
    __table_args__ = (
        UniqueConstraint("bucket", "object_key", "etag", name="uq_ingested_object_version"),
        CheckConstraint(
            "status IN ('registered', 'unchanged', 'skipped', 'failed')", name="status_known"
        ),
        CheckConstraint(
            "(status IN ('skipped', 'failed')) = (reason IS NOT NULL)", name="reason_iff_not_read"
        ),
        CheckConstraint(
            "(status IN ('registered', 'unchanged')) = (file_version_id IS NOT NULL)",
            name="version_iff_read",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profile.id", ondelete="CASCADE")
    )
    period: Mapped[str | None] = mapped_column(String(7))
    source: Mapped[str | None] = mapped_column(Text)
    file_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file_version.id", ondelete="CASCADE")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertChannel(Base):
    """Somewhere to send word of what a run found: an email address or a chat webhook.

    The target is stored as given. A webhook URL is a secret and is not encrypted at rest; the
    API shows it masked.
    """

    __tablename__ = "alert_channel"
    __table_args__ = (
        CheckConstraint("kind IN ('email', 'slack', 'teams')", name="kind_known"),
        CheckConstraint(
            "digest_hour IS NULL OR (digest_hour >= 0 AND digest_hour <= 23)",
            name="digest_hour_range",
        ),
        CheckConstraint("name <> ''", name="name_not_empty"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    on_new_cases: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    digest_hour: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = created_at()


class AlertDelivery(Base):
    """One message to one channel, and how sending it went.

    The unique key (channel, dedupe key) is what stops a retried job from sending twice. A
    delivery is claimed before it is sent and marked after, so it is at-least-once: a worker that
    dies between the send and the mark causes a second message when the claim expires.
    """

    __tablename__ = "alert_delivery"
    __table_args__ = (
        UniqueConstraint("channel_id", "dedupe_key", name="uq_alert_delivery_once"),
        CheckConstraint("kind IN ('run', 'digest', 'test')", name="kind_known"),
        CheckConstraint("status IN ('sending', 'sent', 'failed')", name="status_known"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_iff_sent_at"),
        CheckConstraint("status <> 'failed' OR error IS NOT NULL", name="failed_has_error"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_channel.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()

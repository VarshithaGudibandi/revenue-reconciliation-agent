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

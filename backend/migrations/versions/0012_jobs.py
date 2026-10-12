"""jobs

A queue in the database: work to do, who holds it and until when, and how it ended.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_job_status_known"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_job_lease_iff_running"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_job_finished_iff_done"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (result IS NOT NULL)",
            name=op.f("ck_job_result_iff_succeeded"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL", name=op.f("ck_job_failed_has_error")
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name=op.f("ck_job_attempts_in_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )
    # Work that has not started yet is one job per key: a second request for the same thing
    # joins it. Work that is running does not count, because it may already have read its input.
    op.create_index(
        "uq_job_queued_per_key",
        "job",
        ["kind", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_job_claimable",
        "job",
        ["run_after", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_job_lease",
        "job",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_lease", table_name="job")
    op.drop_index("ix_job_claimable", table_name="job")
    op.drop_index("uq_job_queued_per_key", table_name="job")
    op.drop_table("job")

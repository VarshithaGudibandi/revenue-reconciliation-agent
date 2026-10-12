"""mapping proposals

Two sample files and the column mapping proposed for them, until the user confirms it.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mapping_proposal",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("mapper", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("invoices_key", sa.Text(), nullable=False),
        sa.Column("payments_key", sa.Text(), nullable=False),
        sa.Column("invoices_name", sa.Text(), nullable=False),
        sa.Column("payments_name", sa.Text(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name=op.f("ck_mapping_proposal_period_format"),
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed')", name=op.f("ck_mapping_proposal_status_known")
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (profile_id IS NOT NULL)",
            name=op.f("ck_mapping_proposal_profile_iff_confirmed"),
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_mapping IS NOT NULL)",
            name=op.f("ck_mapping_proposal_mapping_iff_confirmed"),
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_at IS NOT NULL)",
            name=op.f("ck_mapping_proposal_time_iff_confirmed"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profile.id"], name=op.f("fk_mapping_proposal_profile_id_profile")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mapping_proposal")),
    )


def downgrade() -> None:
    op.drop_table("mapping_proposal")

"""alerts

Where to send word of what a run found, and a log of what was sent, so that a retried job does
not send twice.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_channel",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("on_new_cases", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("digest_hour", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('email', 'slack', 'teams')", name=op.f("ck_alert_channel_kind_known")
        ),
        sa.CheckConstraint(
            "digest_hour IS NULL OR (digest_hour >= 0 AND digest_hour <= 23)",
            name=op.f("ck_alert_channel_digest_hour_range"),
        ),
        sa.CheckConstraint("name <> ''", name=op.f("ck_alert_channel_name_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_channel")),
        sa.UniqueConstraint("name", name=op.f("uq_alert_channel_name")),
    )
    op.create_table(
        "alert_delivery",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('run', 'digest', 'test')", name=op.f("ck_alert_delivery_kind_known")
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed')", name=op.f("ck_alert_delivery_status_known")
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name=op.f("ck_alert_delivery_sent_iff_sent_at"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL",
            name=op.f("ck_alert_delivery_failed_has_error"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channel.id"],
            name=op.f("fk_alert_delivery_channel_id_alert_channel"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_delivery")),
        sa.UniqueConstraint("channel_id", "dedupe_key", name="uq_alert_delivery_once"),
    )


def downgrade() -> None:
    op.drop_table("alert_delivery")
    op.drop_table("alert_channel")

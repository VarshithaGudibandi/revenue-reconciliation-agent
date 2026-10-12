"""ingested objects

What the bucket scan did with each object it saw: registered it as a file version, found it
unchanged, skipped it for a reason that may stop applying, or failed to read it.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingested_object",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period", sa.String(length=7), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('registered', 'unchanged', 'skipped', 'failed')",
            name=op.f("ck_ingested_object_status_known"),
        ),
        sa.CheckConstraint(
            "(status IN ('skipped', 'failed')) = (reason IS NOT NULL)",
            name=op.f("ck_ingested_object_reason_iff_not_read"),
        ),
        sa.CheckConstraint(
            "(status IN ('registered', 'unchanged')) = (file_version_id IS NOT NULL)",
            name=op.f("ck_ingested_object_version_iff_read"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profile.id"],
            name=op.f("fk_ingested_object_profile_id_profile"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["source_file_version.id"],
            name=op.f("fk_ingested_object_file_version_id_source_file_version"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingested_object")),
        sa.UniqueConstraint("bucket", "object_key", "etag", name="uq_ingested_object_version"),
    )


def downgrade() -> None:
    op.drop_table("ingested_object")

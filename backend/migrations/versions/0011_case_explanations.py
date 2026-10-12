"""case explanations

Explanations a language model wrote for a case, kept for the exact input it read. A rejected
attempt is kept too, with what the checks found, so that the rate of rejection can be measured.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_explanation",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("why", sa.Text(), nullable=True),
        sa.Column("next_steps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_case_explanation_input_hash_format")
        ),
        sa.CheckConstraint(
            "verified = (summary IS NOT NULL AND why IS NOT NULL AND next_steps IS NOT NULL)",
            name=op.f("ck_case_explanation_text_iff_verified"),
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["exception_case.id"],
            name=op.f("fk_case_explanation_case_id_exception_case"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_explanation")),
    )
    op.create_index(
        "uq_case_explanation_verified",
        "case_explanation",
        ["case_id", "input_hash", "prompt_version"],
        unique=True,
        postgresql_where=sa.text("verified"),
    )


def downgrade() -> None:
    op.drop_index("uq_case_explanation_verified", table_name="case_explanation")
    op.drop_table("case_explanation")

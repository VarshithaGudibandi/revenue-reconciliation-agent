"""case investigations, and a schema for LangGraph's checkpoints

An investigation is a LangGraph run that can stop to wait for a person. LangGraph keeps the
state of its runs in tables it creates and migrates itself; they go in their own schema so they
stay apart from the tables these migrations own.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
    op.create_table(
        "case_investigation",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("finding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('awaiting_decision', 'not_shown', 'applied', 'rejected', 'failed')",
            name=op.f("ck_case_investigation_status_known"),
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'rejected')) = (decided_at IS NOT NULL)",
            name=op.f("ck_case_investigation_decided_iff_decided_at"),
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["exception_case.id"],
            name=op.f("fk_case_investigation_case_id_exception_case"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_investigation")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_case_investigation_thread_id")),
    )
    op.create_index("ix_case_investigation_case", "case_investigation", ["case_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_case_investigation_case", table_name="case_investigation")
    op.drop_table("case_investigation")
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")

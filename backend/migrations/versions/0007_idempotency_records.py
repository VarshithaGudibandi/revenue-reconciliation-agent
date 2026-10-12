"""idempotency records

Requests that carry an idempotency key store their response here, so a retry gets the
same answer without repeating the action.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_record",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_idempotency_record_request_hash_format"),
        ),
        sa.CheckConstraint(
            "(status_code IS NULL) = (response IS NULL)",
            name=op.f("ck_idempotency_record_response_complete"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_idempotency_record")),
    )


def downgrade() -> None:
    op.drop_table("idempotency_record")

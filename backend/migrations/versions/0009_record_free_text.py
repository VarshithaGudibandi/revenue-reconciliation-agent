"""free text on source records

What the source holds about a record in words: what an invoice is for, or what a payer wrote.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_record", sa.Column("free_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_record", "free_text")

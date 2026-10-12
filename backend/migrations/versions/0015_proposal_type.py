"""the type of reconciliation a mapping proposal is for

The first file of a proposal is stored in the invoices slot and the second in the payments slot
whatever the type; this says what they are.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mapping_proposal",
        sa.Column(
            "reconciliation_type", sa.Text(), server_default="invoice_payment", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("mapping_proposal", "reconciliation_type")

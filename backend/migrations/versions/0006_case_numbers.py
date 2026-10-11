"""case numbers

Cases get a number that records creation order and gives people a short reference.
Ordering by it is deterministic, unlike ordering by a timestamp that cases created in
one transaction share.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exception_case",
        sa.Column("number", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_unique_constraint("uq_exception_case_number", "exception_case", ["number"])


def downgrade() -> None:
    op.drop_constraint("uq_exception_case_number", "exception_case", type_="unique")
    op.drop_column("exception_case", "number")

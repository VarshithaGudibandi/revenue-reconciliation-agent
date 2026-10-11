"""run statistics and partition index

Runs record how their result was computed. The index serves loading the records of
a few counterparties from one file version.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("mode", sa.Text(), nullable=True))
    op.add_column("run", sa.Column("base_run_id", sa.UUID(), nullable=True))
    op.add_column("run", sa.Column("partitions_total", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("partitions_recomputed", sa.Integer(), nullable=True))
    op.create_foreign_key(op.f("fk_run_base_run_id_run"), "run", "run", ["base_run_id"], ["id"])
    # Alembic does not detect check constraints, so these are written by hand.
    op.create_check_constraint(op.f("ck_run_mode_known"), "run", "mode IN ('full', 'incremental')")
    op.create_check_constraint(
        op.f("ck_run_incremental_has_base"),
        "run",
        "(mode = 'incremental') = (base_run_id IS NOT NULL)",
    )
    op.create_index(
        "ix_source_record_version_counterparty",
        "source_record",
        ["file_version_id", "counterparty"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_record_version_counterparty", table_name="source_record")
    op.drop_constraint(op.f("ck_run_incremental_has_base"), "run", type_="check")
    op.drop_constraint(op.f("ck_run_mode_known"), "run", type_="check")
    op.drop_constraint(op.f("fk_run_base_run_id_run"), "run", type_="foreignkey")
    op.drop_column("run", "partitions_recomputed")
    op.drop_column("run", "partitions_total")
    op.drop_column("run", "base_run_id")
    op.drop_column("run", "mode")

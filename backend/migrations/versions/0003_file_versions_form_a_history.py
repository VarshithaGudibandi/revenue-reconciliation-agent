"""file versions form a history

A file reverted to earlier content is recorded as a new version, so the same
content may appear in several versions of one source and period.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_source_file_version_content"), "source_file_version", type_="unique"
    )


def downgrade() -> None:
    # Fails if any source was reverted to earlier content, since that repeats a hash.
    op.create_unique_constraint(
        op.f("uq_source_file_version_content"),
        "source_file_version",
        ["profile_id", "source_name", "period", "content_sha256"],
    )

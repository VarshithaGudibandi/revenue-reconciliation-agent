"""exception cases

Cases are tracked across runs by the records they involve. The audit log is
append-only: a trigger makes the database refuse to change or delete its rows.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_sync",
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profile.id"], name=op.f("fk_case_sync_profile_id_profile")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name=op.f("fk_case_sync_run_id_run")),
        sa.PrimaryKeyConstraint("profile_id", "period", name=op.f("pk_case_sync")),
    )
    op.create_table(
        "exception_case",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("variance_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("proposed_invoice_id", sa.Text(), nullable=True),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_run_id", sa.UUID(), nullable=False),
        sa.Column("last_run_id", sa.UUID(), nullable=False),
        sa.Column("resolved_run_id", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_reason", sa.Text(), nullable=True),
        sa.Column("accepted_signature", sa.String(length=64), nullable=True),
        sa.Column("fix_reported_run_id", sa.UUID(), nullable=True),
        sa.Column("fix_unverified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("successor_case_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "(closed_reason = 'merged') = (successor_case_id IS NOT NULL)",
            name=op.f("ck_exception_case_merged_has_successor"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (accepted_reason IS NOT NULL)",
            name=op.f("ck_exception_case_accepted_has_reason"),
        ),
        sa.CheckConstraint(
            "(accepted_reason IS NULL) = (accepted_signature IS NULL)",
            name=op.f("ck_exception_case_accepted_has_signature"),
        ),
        sa.CheckConstraint(
            "(status = 'fix_reported') = (fix_reported_run_id IS NOT NULL)",
            name=op.f("ck_exception_case_fix_has_run"),
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolved_run_id IS NOT NULL)",
            name=op.f("ck_exception_case_resolved_has_run"),
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (closed_reason IS NOT NULL)",
            name=op.f("ck_exception_case_withdrawn_has_reason"),
        ),
        sa.CheckConstraint(
            "NOT fix_unverified OR status = 'open'",
            name=op.f("ck_exception_case_unverified_fix_is_open"),
        ),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('records_removed', 'merged')",
            name=op.f("ck_exception_case_closed_reason_known"),
        ),
        sa.CheckConstraint(
            "signature ~ '^[0-9a-f]{64}$'", name=op.f("ck_exception_case_signature_format")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'fix_reported', 'accepted', 'resolved', 'withdrawn')",
            name=op.f("ck_exception_case_status_known"),
        ),
        sa.CheckConstraint(
            "(variance_minor IS NULL) = (currency IS NULL)",
            name=op.f("ck_exception_case_variance_has_currency"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_exception_case_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_run_id"], ["run.id"], name=op.f("fk_exception_case_first_run_id_run")
        ),
        sa.ForeignKeyConstraint(
            ["fix_reported_run_id"],
            ["run.id"],
            name=op.f("fk_exception_case_fix_reported_run_id_run"),
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"], ["run.id"], name=op.f("fk_exception_case_last_run_id_run")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profile.id"], name=op.f("fk_exception_case_profile_id_profile")
        ),
        sa.ForeignKeyConstraint(
            ["resolved_run_id"], ["run.id"], name=op.f("fk_exception_case_resolved_run_id_run")
        ),
        sa.ForeignKeyConstraint(
            ["successor_case_id"],
            ["exception_case.id"],
            name=op.f("fk_exception_case_successor_case_id_exception_case"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exception_case")),
    )
    op.create_index(
        "ix_exception_case_profile_period",
        "exception_case",
        ["profile_id", "period", "status"],
        unique=False,
    )
    op.create_table(
        "case_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["exception_case.id"], name=op.f("fk_case_event_case_id_exception_case")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name=op.f("fk_case_event_run_id_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_event")),
    )
    op.create_table(
        "case_note",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(body)) > 0", name=op.f("ck_case_note_body_not_blank")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["exception_case.id"],
            name=op.f("fk_case_note_case_id_exception_case"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_note")),
    )
    op.create_table(
        "case_record",
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('invoice', 'payment')", name=op.f("ck_case_record_role_known")
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["exception_case.id"],
            name=op.f("fk_case_record_case_id_exception_case"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("case_id", "role", "record_id", name=op.f("pk_case_record")),
    )
    op.create_index("ix_case_record_lookup", "case_record", ["role", "record_id"], unique=False)

    # Alembic does not generate triggers, so the append-only rule is written by hand.
    op.execute(
        """
        CREATE FUNCTION case_event_is_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'case_event is append-only (% refused)', TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_event_append_only
        BEFORE UPDATE OR DELETE ON case_event
        FOR EACH ROW EXECUTE FUNCTION case_event_is_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_event_no_truncate
        BEFORE TRUNCATE ON case_event
        FOR EACH STATEMENT EXECUTE FUNCTION case_event_is_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER case_event_no_truncate ON case_event")
    op.execute("DROP TRIGGER case_event_append_only ON case_event")
    op.execute("DROP FUNCTION case_event_is_append_only()")
    op.drop_index("ix_case_record_lookup", table_name="case_record")
    op.drop_table("case_record")
    op.drop_table("case_note")
    op.drop_table("case_event")
    op.drop_index("ix_exception_case_profile_period", table_name="exception_case")
    op.drop_table("exception_case")
    op.drop_table("case_sync")

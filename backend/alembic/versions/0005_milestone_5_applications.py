"""Milestone 5 application automation, encrypted answers, and immutable approval records

Revision ID: 0005_milestone_5
Revises: 0004_milestone_4
Create Date: 2026-11-26 02:30:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import JSONVariant

revision = "0005_milestone_5"
down_revision = "0004_milestone_4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "resume_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resumes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_job_key", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), unique=True, nullable=False),
        sa.Column(
            "application_mode",
            sa.String(length=32),
            server_default="human_approved_browser",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), server_default="RESUME_READY", nullable=False),
        sa.Column("destination_url", sa.String(length=1000), nullable=False),
        sa.Column("thread_id", sa.String(length=160), unique=True, nullable=False),
        sa.Column("external_ref_id", sa.String(length=255), nullable=True),
        sa.Column("confirmation_evidence", JSONVariant, server_default="{}", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_followup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "application_mode IN ('official_api', 'human_approved_browser', 'manual')",
            name="ck_applications_mode",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'ELIGIBLE', 'MATCHED', 'RESUME_READY', 'NEEDS_USER_INPUT', 'AWAITING_APPROVAL', "
            "'APPROVED', 'SUBMISSION_PENDING', 'SUBMITTED', 'USER_REPORTED_SUBMITTED', "
            "'UNVERIFIED', 'CONFIRMED', 'INTERVIEW', 'REJECTED', 'WITHDRAWN', 'EXPIRED', "
            "'FAILED', 'DUPLICATE', 'MANUAL_ACTION_REQUIRED'"
            ")",
            name="ck_applications_status",
        ),
    )
    op.create_index("ix_applications_user_id", "applications", ["user_id"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_resume_id", "applications", ["resume_id"])
    op.create_index("ix_applications_canonical_job_key", "applications", ["canonical_job_key"])
    op.create_index("ix_applications_status", "applications", ["status"])
    op.create_index(
        "uq_applications_active_user_canonical_job",
        "applications",
        ["user_id", "canonical_job_key"],
        unique=True,
        postgresql_where=sa.text(
            "user_id IS NOT NULL AND deleted_at IS NULL AND status NOT IN ('FAILED', 'EXPIRED', 'REJECTED')"
        ),
        sqlite_where=sa.text(
            "user_id IS NOT NULL AND deleted_at IS NULL AND status NOT IN ('FAILED', 'EXPIRED', 'REJECTED')"
        ),
    )

    op.create_table(
        "application_answers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "application_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question_key", sa.String(length=160), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("is_sensitive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("answer_encrypted", sa.Text(), nullable=True),
        sa.Column("answer_plaintext", sa.Text(), nullable=True),
        sa.Column("key_version", sa.String(length=32), nullable=True),
        sa.Column("evidence_source", sa.String(length=160), nullable=True),
        sa.Column("user_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(NOT is_sensitive) OR ("
            "answer_encrypted IS NOT NULL AND length(answer_encrypted) > 0 "
            "AND answer_plaintext IS NULL AND key_version IS NOT NULL"
            ")",
            name="ck_app_answers_sensitive_encrypted",
        ),
        sa.CheckConstraint(
            "is_sensitive OR (answer_plaintext IS NOT NULL AND answer_encrypted IS NULL)",
            name="ck_app_answers_nonsensitive_plain",
        ),
    )
    op.create_index("ix_application_answers_application_id", "application_answers", ["application_id"])

    op.create_table(
        "approval_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "application_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("resume_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("resume_checksum", sa.String(length=64), nullable=False),
        sa.Column("destination_url", sa.String(length=1000), nullable=False),
        sa.Column("application_mode", sa.String(length=32), nullable=False),
        sa.Column("answers_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="ck_approval_records_decision"),
        sa.CheckConstraint(
            "application_mode IN ('official_api', 'human_approved_browser', 'manual')",
            name="ck_approval_records_mode",
        ),
    )
    op.create_index("ix_approval_records_application_id", "approval_records", ["application_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION applypilot_prevent_append_only_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only and immutable', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_approval_records_immutable
            BEFORE UPDATE OR DELETE ON approval_records
            FOR EACH ROW EXECUTE FUNCTION applypilot_prevent_append_only_mutation();
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION applypilot_prevent_append_only_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;")
        op.execute("DROP TRIGGER IF EXISTS trg_approval_records_immutable ON approval_records;")
        op.execute("DROP FUNCTION IF EXISTS applypilot_prevent_append_only_mutation();")

    op.drop_index("ix_approval_records_application_id", table_name="approval_records")
    op.drop_table("approval_records")

    op.drop_index("ix_application_answers_application_id", table_name="application_answers")
    op.drop_table("application_answers")

    op.drop_index("uq_applications_active_user_canonical_job", table_name="applications")
    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_index("ix_applications_canonical_job_key", table_name="applications")
    op.drop_index("ix_applications_resume_id", table_name="applications")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_index("ix_applications_user_id", table_name="applications")
    op.drop_table("applications")

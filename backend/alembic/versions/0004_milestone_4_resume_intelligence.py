"""Milestone 4 resume intelligence outbox, quota ledger, version mappings, and validation storage

Revision ID: 0004_milestone_4
Revises: 0003_milestone_3
Create Date: 2026-11-26 00:10:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import JSONVariant

revision = "0004_milestone_4"
down_revision = "0003_milestone_3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.add_column(sa.Column("master_resume_id", sa.Uuid(as_uuid=True), nullable=True))
        batch_op.add_column(
            sa.Column("generation_status", sa.String(length=32), server_default="uploaded", nullable=False)
        )
        batch_op.add_column(
            sa.Column("validation_report", JSONVariant, server_default="{}", nullable=False)
        )
        batch_op.add_column(
            sa.Column("evaluation_report", JSONVariant, server_default="{}", nullable=False)
        )
        batch_op.add_column(
            sa.Column("structured_content", JSONVariant, server_default="{}", nullable=False)
        )
        batch_op.create_foreign_key(
            "fk_resumes_master_resume_id",
            "resumes",
            ["master_resume_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_resumes_generation_status",
            "generation_status IN ('uploaded', 'completed', 'validation_failed', 'failed')",
        )
    op.create_index("ix_resumes_master_resume_id", "resumes", ["master_resume_id"])

    op.create_table(
        "auto_tailor_daily_quotas",
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("quota_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("reserved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_count >= 0", name="ck_auto_tailor_quota_nonneg"),
    )

    op.create_table(
        "resume_generation_jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "master_resume_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resumes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger_mode", sa.String(length=24), server_default="auto", nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("quota_date", sa.Date(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("worker_id", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("thread_id", sa.String(length=160), nullable=False),
        sa.Column(
            "resume_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resumes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'validation_failed', 'failed')",
            name="ck_resume_gen_jobs_status",
        ),
        sa.CheckConstraint(
            "trigger_mode IN ('auto', 'manual', 'regenerate')",
            name="ck_resume_gen_jobs_trigger_mode",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_resume_gen_jobs_attempts",
        ),
    )
    op.create_index("ix_resume_generation_jobs_user_id", "resume_generation_jobs", ["user_id"])
    op.create_index("ix_resume_generation_jobs_profile_id", "resume_generation_jobs", ["profile_id"])
    op.create_index("ix_resume_generation_jobs_job_id", "resume_generation_jobs", ["job_id"])
    op.create_index(
        "ix_resume_generation_jobs_evidence_fingerprint",
        "resume_generation_jobs",
        ["evidence_fingerprint"],
    )
    op.create_index("ix_resume_generation_jobs_status", "resume_generation_jobs", ["status"])
    op.create_index(
        "ix_resume_generation_jobs_lease_expires_at",
        "resume_generation_jobs",
        ["lease_expires_at"],
    )
    op.create_index(
        "uq_resume_gen_jobs_active_idem",
        "resume_generation_jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('queued', 'running', 'completed')"
        ),
        sqlite_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('queued', 'running', 'completed')"
        ),
    )

    op.create_table(
        "resume_job_mappings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "resume_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resumes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "master_resume_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resumes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "generation_job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("resume_generation_jobs.id", ondelete="SET NULL"),
            unique=True,
            nullable=True,
        ),
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("trigger_mode", sa.String(length=24), server_default="manual", nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approval_status", sa.String(length=32), server_default="pending_review", nullable=False),
        sa.Column("approval_notes", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tailoring_diff", JSONVariant, server_default="{}", nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("token_usage", JSONVariant, server_default="{}", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "profile_id", "version_number", name="uq_resume_job_mapping_version"),
        sa.CheckConstraint(
            "status IN ('completed', 'validation_failed', 'failed')",
            name="ck_resume_job_mapping_status",
        ),
        sa.CheckConstraint(
            "approval_status IN ('pending_review', 'approved', 'rejected')",
            name="ck_resume_job_mapping_approval_status",
        ),
        sa.CheckConstraint(
            "approval_status != 'approved' OR status = 'completed'",
            name="ck_resume_job_mapping_approved_requires_completed",
        ),
    )
    op.create_index("ix_resume_job_mappings_resume_id", "resume_job_mappings", ["resume_id"])
    op.create_index("ix_resume_job_mappings_job_id", "resume_job_mappings", ["job_id"])
    op.create_index("ix_resume_job_mappings_profile_id", "resume_job_mappings", ["profile_id"])
    op.create_index("ix_resume_job_mappings_run_id", "resume_job_mappings", ["run_id"])
    op.create_index(
        "ix_resume_job_mappings_evidence_fingerprint",
        "resume_job_mappings",
        ["evidence_fingerprint"],
    )
    op.create_index(
        "ix_resume_job_mapping_fp_lookup",
        "resume_job_mappings",
        ["job_id", "profile_id", "evidence_fingerprint", "status"],
    )
    op.create_index(
        "uq_resume_job_mapping_active_completed",
        "resume_job_mappings",
        ["job_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("is_active = TRUE AND status = 'completed'"),
        sqlite_where=sa.text("is_active = 1 AND status = 'completed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_resume_job_mapping_active_completed", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mapping_fp_lookup", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mappings_evidence_fingerprint", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mappings_run_id", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mappings_profile_id", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mappings_job_id", table_name="resume_job_mappings")
    op.drop_index("ix_resume_job_mappings_resume_id", table_name="resume_job_mappings")
    op.drop_table("resume_job_mappings")

    op.drop_index("uq_resume_gen_jobs_active_idem", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_lease_expires_at", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_status", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_evidence_fingerprint", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_job_id", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_profile_id", table_name="resume_generation_jobs")
    op.drop_index("ix_resume_generation_jobs_user_id", table_name="resume_generation_jobs")
    op.drop_table("resume_generation_jobs")

    op.drop_table("auto_tailor_daily_quotas")

    op.drop_index("ix_resumes_master_resume_id", table_name="resumes")
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_constraint("ck_resumes_generation_status", type_="check")
        batch_op.drop_constraint("fk_resumes_master_resume_id", type_="foreignkey")
        batch_op.drop_column("structured_content")
        batch_op.drop_column("evaluation_report")
        batch_op.drop_column("validation_report")
        batch_op.drop_column("generation_status")
        batch_op.drop_column("master_resume_id")

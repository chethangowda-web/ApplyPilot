"""Milestone 2 job discovery tables

Revision ID: 0002_milestone_2
Revises: 0001_milestone_1
Create Date: 2026-11-25 23:20:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import EmbeddingVector, JSONVariant

revision = "0002_milestone_2"
down_revision = "0001_milestone_1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_name", sa.String(length=160), nullable=False, unique=True),
        sa.Column("connector_type", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("config", JSONVariant, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("rate_limit_config", JSONVariant, nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_status", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "source_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("job_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_job_id", sa.String(length=255), nullable=True),
        sa.Column("canonical_url", sa.String(length=1000), nullable=False, unique=True),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("company", sa.String(length=250), nullable=False),
        sa.Column("role_title", sa.String(length=300), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("remote_policy", sa.String(length=32), nullable=False),
        sa.Column("is_remote", sa.Boolean(), nullable=True),
        sa.Column("employment_type", sa.String(length=64), nullable=False),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=16), nullable=True),
        sa.Column("salary_text", sa.String(length=160), nullable=True),
        sa.Column("raw_jd", sa.Text(), nullable=False),
        sa.Column("application_url", sa.String(length=1000), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_source", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repost_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_provenance", JSONVariant, nullable=False),
        sa.Column("jd_embedding", EmbeddingVector(768), nullable=True),
        sa.UniqueConstraint("source_id", "source_job_id", name="uq_jobs_source_external_id"),
    )
    op.create_index("ix_jobs_source_id", "jobs", ["source_id"])
    op.create_index("ix_jobs_dedup_key", "jobs", ["dedup_key"])
    op.create_index("ix_jobs_content_checksum", "jobs", ["content_checksum"])
    op.create_index("ix_jobs_company", "jobs", ["company"])
    op.create_index("ix_jobs_role_title", "jobs", ["role_title"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_requirements",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("role_category", sa.String(length=120), nullable=False),
        sa.Column("required_skills", JSONVariant, nullable=False),
        sa.Column("preferred_skills", JSONVariant, nullable=False),
        sa.Column("trainable_skills", JSONVariant, nullable=False),
        sa.Column("responsibilities", JSONVariant, nullable=False),
        sa.Column("technical_stack", JSONVariant, nullable=False),
        sa.Column("experience_min", sa.Float(), nullable=True),
        sa.Column("experience_max", sa.Float(), nullable=True),
        sa.Column("education_requirements", JSONVariant, nullable=False),
        sa.Column("internship_eligible", sa.Boolean(), nullable=True),
        sa.Column("fresher_eligible", sa.Boolean(), nullable=True),
        sa.Column("sponsorship_required", sa.Boolean(), nullable=True),
        sa.Column("uncertainties", JSONVariant, nullable=False),
        sa.Column("prompt_injection_detected", sa.Boolean(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "job_matches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
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
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("hard_constraints_passed", sa.Boolean(), nullable=False),
        sa.Column("matched_skills", JSONVariant, nullable=False),
        sa.Column("missing_required_skills", JSONVariant, nullable=False),
        sa.Column("preferred_gaps", JSONVariant, nullable=False),
        sa.Column("score_breakdown", JSONVariant, nullable=False),
        sa.Column("recommendation", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "profile_id", name="uq_job_matches_job_profile"),
    )
    op.create_index("ix_job_matches_job_id", "job_matches", ["job_id"])
    op.create_index("ix_job_matches_profile_id", "job_matches", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_job_matches_profile_id", table_name="job_matches")
    op.drop_index("ix_job_matches_job_id", table_name="job_matches")
    op.drop_table("job_matches")
    op.drop_table("job_requirements")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_role_title", table_name="jobs")
    op.drop_index("ix_jobs_company", table_name="jobs")
    op.drop_index("ix_jobs_content_checksum", table_name="jobs")
    op.drop_index("ix_jobs_dedup_key", table_name="jobs")
    op.drop_index("ix_jobs_source_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("job_sources")

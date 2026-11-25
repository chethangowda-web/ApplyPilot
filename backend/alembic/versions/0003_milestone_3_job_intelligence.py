"""Milestone 3 job intelligence extensions, workflow runs, and analysis history

Revision ID: 0003_milestone_3
Revises: 0002_milestone_2
Create Date: 2026-11-25 23:35:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import EmbeddingVector, JSONVariant

revision = "0003_milestone_3"
down_revision = "0002_milestone_2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("embedding", EmbeddingVector(768), nullable=True))
    op.add_column("experiences", sa.Column("embedding", EmbeddingVector(768), nullable=True))
    op.add_column("certifications", sa.Column("embedding", EmbeddingVector(768), nullable=True))

    op.add_column("job_requirements", sa.Column("location", sa.String(length=300), nullable=True))
    op.add_column(
        "job_requirements",
        sa.Column("remote_policy", sa.String(length=32), server_default="unknown", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("employment_type", sa.String(length=64), server_default="unknown", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("graduation_criteria", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("mandatory_qualifications", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("preferred_qualifications", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("application_instructions", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("evidence_items", JSONVariant, server_default="{}", nullable=False),
    )
    op.add_column(
        "job_requirements",
        sa.Column("extraction_mode", sa.String(length=48), server_default="deterministic", nullable=False),
    )

    op.add_column(
        "job_matches",
        sa.Column("required_skill_coverage", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "job_matches",
        sa.Column("preferred_skill_coverage", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column("job_matches", sa.Column("semantic_similarity", sa.Float(), nullable=True))
    op.add_column(
        "job_matches",
        sa.Column("evidence_mapping", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_matches",
        sa.Column("missing_evidence", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_matches",
        sa.Column("eligibility_blockers", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_matches",
        sa.Column("uncertainties", JSONVariant, server_default="[]", nullable=False),
    )
    op.add_column(
        "job_matches",
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
    )

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("thread_id", sa.String(length=160), nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("current_stage", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checkpoint_metadata", JSONVariant, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workflow_runs_user_id", "workflow_runs", ["user_id"])
    op.create_index("ix_workflow_runs_job_id", "workflow_runs", ["job_id"])

    op.create_table(
        "job_analysis_history",
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
        sa.Column("run_id", sa.String(length=160), nullable=True),
        sa.Column("extraction_mode", sa.String(length=48), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=True),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("snapshot", JSONVariant, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_analysis_history_job_id", "job_analysis_history", ["job_id"])
    op.create_index("ix_job_analysis_history_profile_id", "job_analysis_history", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_job_analysis_history_profile_id", table_name="job_analysis_history")
    op.drop_index("ix_job_analysis_history_job_id", table_name="job_analysis_history")
    op.drop_table("job_analysis_history")
    op.drop_index("ix_workflow_runs_job_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_user_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")

    for col in (
        "confidence",
        "uncertainties",
        "eligibility_blockers",
        "missing_evidence",
        "evidence_mapping",
        "semantic_similarity",
        "preferred_skill_coverage",
        "required_skill_coverage",
    ):
        op.drop_column("job_matches", col)

    for col in (
        "extraction_mode",
        "evidence_items",
        "application_instructions",
        "preferred_qualifications",
        "mandatory_qualifications",
        "graduation_criteria",
        "employment_type",
        "remote_policy",
        "location",
    ):
        op.drop_column("job_requirements", col)

    op.drop_column("certifications", "embedding")
    op.drop_column("experiences", "embedding")
    op.drop_column("projects", "embedding")

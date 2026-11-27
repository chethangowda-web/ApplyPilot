"""Milestone 6 career intelligence: follow-ups, interview kits, mock interviews, and interview memory

Revision ID: 0007_milestone_6_career
Revises: 0006_milestone_6
Create Date: 2026-11-27 13:50:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import JSONVariant

revision = "0007_milestone_6_career"
down_revision = "0006_milestone_6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_followups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("application_id", sa.Uuid(as_uuid=True), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reminder_type", sa.String(length=32), server_default="initial_followup", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interval_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("draft_subject", sa.String(length=300), nullable=False),
        sa.Column("draft_body", sa.Text(), nullable=False),
        sa.Column("recruiter_context", JSONVariant, server_default="{}", nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reminder_type IN ('initial_followup', 'second_followup', 'interview_thank_you', 'custom')",
            name="ck_app_followups_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'snoozed', 'cancelled')",
            name="ck_app_followups_status",
        ),
    )
    op.create_index("ix_app_followups_app_id", "application_followups", ["application_id"])
    op.create_index("ix_app_followups_user_id", "application_followups", ["user_id"])
    op.create_index("ix_app_followups_user_status_due", "application_followups", ["user_id", "status", "due_at"])

    op.create_table(
        "interview_kits",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("application_id", sa.Uuid(as_uuid=True), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("resume_id", sa.Uuid(as_uuid=True), sa.ForeignKey("resumes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("resume_checksum", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("modules_json", JSONVariant, server_default="{}", nullable=False),
        sa.Column("progress_json", JSONVariant, server_default="{}", nullable=False),
        sa.Column("user_feedback", sa.Text(), nullable=True),
        sa.Column("readiness_estimate", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_interview_kits_user_id", "interview_kits", ["user_id"])
    op.create_index("ix_interview_kits_app_id", "interview_kits", ["application_id"])
    op.create_index("ix_interview_kits_user_app_active", "interview_kits", ["user_id", "application_id", "is_active"])

    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kit_id", sa.Uuid(as_uuid=True), sa.ForeignKey("interview_kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_type", sa.String(length=32), server_default="mixed", nullable=False),
        sa.Column("difficulty", sa.String(length=16), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("current_question_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_question_json", JSONVariant, server_default="{}", nullable=False),
        sa.Column("report_json", JSONVariant, server_default="{}", nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "round_type IN ('technical', 'dsa', 'backend_swe', 'ai_ml', 'resume_deep_dive', 'behavioral', 'mixed')",
            name="ck_interview_sessions_round",
        ),
        sa.CheckConstraint("difficulty IN ('easy', 'medium', 'hard')", name="ck_interview_sessions_difficulty"),
        sa.CheckConstraint("status IN ('active', 'completed')", name="ck_interview_sessions_status"),
    )
    op.create_index("ix_interview_sessions_user_id", "interview_sessions", ["user_id"])
    op.create_index("ix_interview_sessions_kit_id", "interview_sessions", ["kit_id"])

    op.create_table(
        "interview_attempts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("session_id", sa.Uuid(as_uuid=True), sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kit_id", sa.Uuid(as_uuid=True), sa.ForeignKey("interview_kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("module_key", sa.String(length=64), nullable=False),
        sa.Column("difficulty", sa.String(length=24), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("expected_structure", JSONVariant, server_default="[]", nullable=False),
        sa.Column("hints", JSONVariant, server_default="[]", nullable=False),
        sa.Column("candidate_answer", sa.Text(), nullable=False),
        sa.Column("feedback_json", JSONVariant, server_default="{}", nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("weak_topics", JSONVariant, server_default="[]", nullable=False),
        sa.Column("strong_topics", JSONVariant, server_default="[]", nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_interview_attempts_session_id", "interview_attempts", ["session_id"])
    op.create_index("ix_interview_attempts_user_id", "interview_attempts", ["user_id"])

    op.create_table(
        "interview_memories",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("topic", sa.String(length=160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.7", nullable=False),
        sa.Column("evidence_refs", JSONVariant, server_default="[]", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "memory_type IN ('weak_topic', 'recurring_mistake', 'strong_area', 'behavioral_example', 'asked_question', 'feedback_note')",
            name="ck_interview_memories_type",
        ),
    )
    op.create_index("ix_interview_memories_user_id", "interview_memories", ["user_id"])
    op.create_index("ix_interview_memories_topic", "interview_memories", ["topic"])
    op.create_index("ix_interview_memories_user_type", "interview_memories", ["user_id", "memory_type"])


def downgrade() -> None:
    op.drop_index("ix_interview_memories_user_type", table_name="interview_memories")
    op.drop_index("ix_interview_memories_topic", table_name="interview_memories")
    op.drop_index("ix_interview_memories_user_id", table_name="interview_memories")
    op.drop_table("interview_memories")

    op.drop_index("ix_interview_attempts_user_id", table_name="interview_attempts")
    op.drop_index("ix_interview_attempts_session_id", table_name="interview_attempts")
    op.drop_table("interview_attempts")

    op.drop_index("ix_interview_sessions_kit_id", table_name="interview_sessions")
    op.drop_index("ix_interview_sessions_user_id", table_name="interview_sessions")
    op.drop_table("interview_sessions")

    op.drop_index("ix_interview_kits_user_app_active", table_name="interview_kits")
    op.drop_index("ix_interview_kits_app_id", table_name="interview_kits")
    op.drop_index("ix_interview_kits_user_id", table_name="interview_kits")
    op.drop_table("interview_kits")

    op.drop_index("ix_app_followups_user_status_due", table_name="application_followups")
    op.drop_index("ix_app_followups_user_id", table_name="application_followups")
    op.drop_index("ix_app_followups_app_id", table_name="application_followups")
    op.drop_table("application_followups")

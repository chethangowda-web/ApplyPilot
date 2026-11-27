"""Milestone 6 Stage 1.1 Gmail OAuth connections and email confirmation events

Revision ID: 0006_milestone_6
Revises: 0005_milestone_5
Create Date: 2026-11-27 13:20:00
"""

from alembic import op
import sqlalchemy as sa
from app.database.types import JSONVariant

revision = "0006_milestone_6"
down_revision = "0005_milestone_5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), server_default="google_gmail", nullable=False),
        sa.Column("mailbox_email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="connected", nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encryption_key_version", sa.String(length=32), nullable=True),
        sa.Column("granted_scopes", JSONVariant, server_default="[]", nullable=False),
        sa.Column("last_history_id", sa.String(length=128), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=32), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "mailbox_email",
            name="uq_oauth_connections_user_provider_mailbox",
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'revoked', 'expired', 'disconnected', 'error')",
            name="ck_oauth_connections_status",
        ),
        sa.CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status IN ('idle', 'running', 'succeeded', 'failed')",
            name="ck_oauth_connections_sync_status",
        ),
        sa.CheckConstraint(
            "status != 'connected' OR ("
            "encrypted_refresh_token IS NOT NULL AND length(encrypted_refresh_token) > 0 "
            "AND encryption_key_version IS NOT NULL"
            ")",
            name="ck_oauth_connections_connected_token_required",
        ),
    )
    op.create_index("ix_oauth_connections_user_id", "oauth_connections", ["user_id"])
    op.create_index(
        "ix_oauth_connections_user_provider_status",
        "oauth_connections",
        ["user_id", "provider", "status"],
    )

    op.create_table(
        "email_confirmation_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "oauth_connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("oauth_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=128), nullable=True),
        sa.Column("sender_domain", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("match_status", sa.String(length=32), server_default="unmatched", nullable=False),
        sa.Column(
            "matched_application_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("applications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("match_signals", JSONVariant, server_default="{}", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "oauth_connection_id",
            "gmail_message_id",
            name="uq_email_conf_events_conn_msg",
        ),
        sa.CheckConstraint(
            "match_status IN ('matched', 'confirmed', 'ambiguous', 'rejected', 'unmatched')",
            name="ck_email_conf_events_match_status",
        ),
    )
    op.create_index("ix_email_conf_events_conn_id", "email_confirmation_events", ["oauth_connection_id"])
    op.create_index("ix_email_conf_events_user_id", "email_confirmation_events", ["user_id"])
    op.create_index(
        "ix_email_conf_events_matched_app_id",
        "email_confirmation_events",
        ["matched_application_id"],
    )
    op.create_index(
        "ix_email_conf_events_user_status",
        "email_confirmation_events",
        ["user_id", "match_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_conf_events_user_status", table_name="email_confirmation_events")
    op.drop_index("ix_email_conf_events_matched_app_id", table_name="email_confirmation_events")
    op.drop_index("ix_email_conf_events_user_id", table_name="email_confirmation_events")
    op.drop_index("ix_email_conf_events_conn_id", table_name="email_confirmation_events")
    op.drop_table("email_confirmation_events")

    op.drop_index("ix_oauth_connections_user_provider_status", table_name="oauth_connections")
    op.drop_index("ix_oauth_connections_user_id", table_name="oauth_connections")
    op.drop_table("oauth_connections")

from datetime import datetime, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from cryptography.exceptions import InvalidTag
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    decrypt_oauth_token,
    decrypt_sensitive_answer,
    encrypt_oauth_token,
    encrypt_sensitive_answer,
    rotate_oauth_token_ciphertext,
)
from app.database.models import (
    EmailConfirmationEvent,
    OAuthConnection,
    User,
)
from app.schemas.integrations import (
    GmailConnectionSummary,
    SanitizedEmailConfirmationMetadata,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_gmail_oauth_settings_defaults(monkeypatch):
    for key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "GMAIL_CONFIRMATION_WINDOW_DAYS",
        "GMAIL_SYNC_INTERVAL_MINUTES",
        "GMAIL_SYNC_MAX_MESSAGES",
        "GMAIL_SYNC_BEAT_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = Settings(_env_file=None)
    assert cfg.google_client_id == ""
    assert cfg.google_client_secret == ""
    assert cfg.google_oauth_redirect_uri == "http://localhost:3000/api/integrations/gmail/callback"
    assert cfg.gmail_confirmation_window_days == 14
    assert cfg.gmail_sync_interval_minutes == 15
    assert cfg.gmail_sync_max_messages == 50
    assert cfg.gmail_sync_beat_enabled is False


def test_oauth_token_encryption_aad_tamper_rotation_and_fail_closed():
    cfg_v1 = Settings(
        _env_file=None,
        ENCRYPTION_KEY="stage1-oauth-secret-key-v1-32bytes",
        ENCRYPTION_KEY_VERSION="v1",
    )
    user_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    refresh_token = "1//0g-synthetic-refresh-token-value"

    ct, ver = encrypt_oauth_token(
        refresh_token,
        user_id=user_id,
        provider="google_gmail",
        token_kind="refresh_token",
        settings=cfg_v1,
    )
    assert ver == "v1"
    assert refresh_token not in ct
    assert (
        decrypt_oauth_token(
            ct,
            key_version=ver,
            user_id=user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v1,
        )
        == refresh_token
    )

    # Wrong AAD components must fail authentication
    with pytest.raises(InvalidTag):
        decrypt_oauth_token(
            ct,
            key_version=ver,
            user_id=other_user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v1,
        )
    with pytest.raises(InvalidTag):
        decrypt_oauth_token(
            ct,
            key_version=ver,
            user_id=user_id,
            provider="other_provider",
            token_kind="refresh_token",
            settings=cfg_v1,
        )
    with pytest.raises(InvalidTag):
        decrypt_oauth_token(
            ct,
            key_version=ver,
            user_id=user_id,
            provider="google_gmail",
            token_kind="access_token",
            settings=cfg_v1,
        )

    # Cross-domain confusion with encrypt_sensitive_answer must fail
    with pytest.raises(InvalidTag):
        decrypt_sensitive_answer(
            ct,
            key_version=ver,
            application_id=user_id,
            question_key="google_gmail",
            settings=cfg_v1,
        )
    ans_ct, ans_ver = encrypt_sensitive_answer(
        "synthetic-ssn",
        application_id=user_id,
        question_key="google_gmail",
        settings=cfg_v1,
    )
    with pytest.raises(InvalidTag):
        decrypt_oauth_token(
            ans_ct,
            key_version=ans_ver,
            user_id=user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v1,
        )

    # Tampering ciphertext fails closed
    tampered = ct[:-2] + ("AA" if ct[-2:] != "AA" else "BB")
    with pytest.raises(Exception):
        decrypt_oauth_token(
            tampered,
            key_version=ver,
            user_id=user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v1,
        )

    # Key rotation v1 -> v2
    cfg_v2 = Settings(
        _env_file=None,
        ENCRYPTION_KEY="stage1-oauth-secret-key-v2-32bytes",
        ENCRYPTION_KEY_VERSION="v2",
        ENCRYPTION_PREVIOUS_KEYS='{"v1":"stage1-oauth-secret-key-v1-32bytes"}',
    )
    rotated_ct, rotated_ver = rotate_oauth_token_ciphertext(
        ct,
        old_key_version="v1",
        user_id=user_id,
        provider="google_gmail",
        token_kind="refresh_token",
        settings=cfg_v2,
    )
    assert rotated_ver == "v2"
    assert (
        decrypt_oauth_token(
            rotated_ct,
            key_version="v2",
            user_id=user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v2,
        )
        == refresh_token
    )

    # Unknown key version & missing ENCRYPTION_KEY fail closed
    with pytest.raises(ValueError):
        decrypt_oauth_token(
            rotated_ct,
            key_version="v99",
            user_id=user_id,
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg_v2,
        )
    cfg_empty = Settings(_env_file=None, ENCRYPTION_KEY="", ENCRYPTION_KEY_VERSION="v1")
    with pytest.raises(ValueError):
        encrypt_oauth_token(
            refresh_token,
            user_id=user_id,
            provider="google_gmail",
            settings=cfg_empty,
        )


def test_oauth_connection_and_confirmation_event_constraints(tmp_path):
    db_file = tmp_path / "m6_stage1_constraints.db"
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(alembic_cfg, "0006_milestone_6")

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert "oauth_connections" in inspector.get_table_names()
        assert "email_confirmation_events" in inspector.get_table_names()

    with Session(engine) as db:
        user = User(email="m6-stage1@example.com", hashed_password="hashed-password")
        db.add(user)
        db.flush()

        # Multiple Gmail connections per user (different mailboxes) are allowed
        c1 = OAuthConnection(
            user_id=user.id,
            provider="google_gmail",
            mailbox_email="primary@gmail.com",
            status="connected",
            encrypted_refresh_token="enc-ct-1",
            encryption_key_version="v1",
            granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        c2 = OAuthConnection(
            user_id=user.id,
            provider="google_gmail",
            mailbox_email="secondary@gmail.com",
            status="connected",
            encrypted_refresh_token="enc-ct-2",
            encryption_key_version="v1",
            granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        db.add_all([c1, c2])
        db.commit()

        # Duplicate (user_id, provider, mailbox_email) rejected
        dup_conn = OAuthConnection(
            user_id=user.id,
            provider="google_gmail",
            mailbox_email="primary@gmail.com",
            status="connected",
            encrypted_refresh_token="enc-ct-dup",
            encryption_key_version="v1",
        )
        db.add(dup_conn)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Connected status without encrypted token rejected
        missing_tok = OAuthConnection(
            user_id=user.id,
            provider="google_gmail",
            mailbox_email="notoken@gmail.com",
            status="connected",
            encrypted_refresh_token=None,
            encryption_key_version=None,
        )
        db.add(missing_tok)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Revoked status without token allowed
        revoked_conn = OAuthConnection(
            user_id=user.id,
            provider="google_gmail",
            mailbox_email="revoked@gmail.com",
            status="revoked",
            encrypted_refresh_token=None,
            encryption_key_version=None,
            revoked_at=datetime.now(timezone.utc),
        )
        db.add(revoked_conn)
        db.commit()

        # Sanitized schema omits sensitive/internal fields
        summary = GmailConnectionSummary.model_validate(c1).model_dump()
        assert "encrypted_refresh_token" not in summary
        assert "encryption_key_version" not in summary
        assert "last_history_id" not in summary

        # EmailConfirmationEvent unique (oauth_connection_id, gmail_message_id)
        now = datetime.now(timezone.utc)
        ev1 = EmailConfirmationEvent(
            oauth_connection_id=c1.id,
            user_id=user.id,
            gmail_message_id="msg-abc-123",
            gmail_thread_id="thr-xyz-999",
            sender_domain="greenhouse.io",
            received_at=now,
            match_status="unmatched",
            match_signals={"domain_match": True},
        )
        db.add(ev1)
        db.commit()

        sanitized_ev = SanitizedEmailConfirmationMetadata.model_validate(ev1).model_dump()
        assert "gmail_message_id" not in sanitized_ev
        assert "gmail_thread_id" not in sanitized_ev
        assert sanitized_ev["sender_domain"] == "greenhouse.io"

        ev_dup = EmailConfirmationEvent(
            oauth_connection_id=c1.id,
            user_id=user.id,
            gmail_message_id="msg-abc-123",
            sender_domain="greenhouse.io",
            received_at=now,
            match_status="unmatched",
        )
        db.add(ev_dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Invalid match_status rejected
        ev_bad_status = EmailConfirmationEvent(
            oauth_connection_id=c1.id,
            user_id=user.id,
            gmail_message_id="msg-bad-status",
            sender_domain="greenhouse.io",
            received_at=now,
            match_status="bogus",
        )
        db.add(ev_bad_status)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    engine.dispose()


def test_migration_0006_upgrade_downgrade_sqlite_and_offline_postgres(tmp_path):
    db_file = tmp_path / "m6_mig_roundtrip.db"
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(alembic_cfg, "0006_milestone_6")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert {"oauth_connections", "email_confirmation_events"}.issubset(tables)
    engine.dispose()

    command.downgrade(alembic_cfg, "0005_milestone_5")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert "oauth_connections" not in tables
        assert "email_confirmation_events" not in tables
        assert "applications" in tables
    engine.dispose()

    command.upgrade(alembic_cfg, "0006_milestone_6")

    # Offline PostgreSQL DDL generation check
    pg_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    pg_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    pg_cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pass@localhost:5432/applypilot")
    command.upgrade(pg_cfg, "0005_milestone_5:0006_milestone_6", sql=True)

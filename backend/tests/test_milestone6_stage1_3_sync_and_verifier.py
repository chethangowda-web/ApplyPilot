from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.routes.integrations import get_gmail_sync_service
from app.core.config import Settings
from app.core.security import create_access_token, encrypt_oauth_token
from app.database.models import (
    Application,
    CandidateProfile,
    EmailConfirmationEvent,
    Job,
    JobSource,
    OAuthConnection,
    Resume,
    User,
)
from app.database.session import get_db
from app.main import app
from app.services.gmail_confirmation_verifier import register_gmail_confirmation_verifier
from app.services.gmail_oauth_service import GMAIL_READONLY_SCOPE
from app.services.gmail_sync_service import GmailSyncService

BACKEND_DIR = Path(__file__).resolve().parents[1]


class _FakeSyncRedis:
    def __init__(self) -> None:
        self.locks: set[str] = set()

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.locks:
            return False
        self.locks.add(key)
        return True

    def delete(self, key: str) -> int:
        self.locks.discard(key)
        return 1


def _auth_hdr(uid: uuid.UUID) -> dict[str, str]:
    tok, _ = create_access_token(str(uid))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def sync_test_env(tmp_path):
    db_file = tmp_path / "m6_stage1_3_sync.db"
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(alembic_cfg, "0006_milestone_6")

    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    cfg = Settings(
        _env_file=None,
        GOOGLE_CLIENT_ID="sync-client-id",
        GOOGLE_CLIENT_SECRET="sync-client-secret",
        ENCRYPTION_KEY="stage1-3-sync-encryption-key-32bytes",
        ENCRYPTION_KEY_VERSION="v1",
        GMAIL_CONFIRMATION_WINDOW_DAYS=14,
        GMAIL_SYNC_MAX_MESSAGES=10,
    )
    now = datetime.now(timezone.utc)

    with TestingSession() as db:
        u1 = User(email="sync-u1@example.com", hashed_password="hashed-password")
        u2 = User(email="sync-u2@example.com", hashed_password="hashed-password")
        db.add_all([u1, u2])
        db.flush()

        p1 = CandidateProfile(user_id=u1.id, full_name="Sync Candidate", email=u1.email)
        src = JobSource(source_name="gh-sync", connector_type="greenhouse")
        db.add_all([p1, src])
        db.flush()

        j_acme = Job(
            source_id=src.id,
            source_job_id="REQ-7788",
            canonical_url="https://boards.greenhouse.io/acme/jobs/7788",
            dedup_key="d" * 64,
            content_checksum="c" * 64,
            company="Acme Robotics",
            role_title="Backend Python Engineer",
            raw_jd="Python FastAPI",
        )
        j_beta_a = Job(
            source_id=src.id,
            source_job_id="BETA-1",
            canonical_url="https://jobs.lever.co/beta/1",
            dedup_key="e" * 64,
            content_checksum="f" * 64,
            company="Beta Cloud",
            role_title="Software Engineer",
            raw_jd="Go",
        )
        j_beta_b = Job(
            source_id=src.id,
            source_job_id="BETA-2",
            canonical_url="https://jobs.lever.co/beta/2",
            dedup_key="g" * 64,
            content_checksum="h" * 64,
            company="Beta Cloud",
            role_title="AI Engineer",
            raw_jd="PyTorch",
        )
        db.add_all([j_acme, j_beta_a, j_beta_b])
        db.flush()

        res = Resume(
            profile_id=p1.id,
            is_master=False,
            original_filename="tailored.pdf",
            content_type="application/pdf",
            file_size_bytes=256,
            version_label="tailored-1",
            sha256_checksum="a" * 64,
        )
        db.add(res)
        db.flush()

        app_acme = Application(
            user_id=u1.id,
            job_id=j_acme.id,
            resume_id=res.id,
            canonical_job_key="k" * 64,
            idempotency_key="i" * 64,
            status="USER_REPORTED_SUBMITTED",
            destination_url="https://boards.greenhouse.io/acme/jobs/7788",
            thread_id="thr-acme",
            submitted_at=now - timedelta(hours=2),
        )
        app_beta_a = Application(
            user_id=u1.id,
            job_id=j_beta_a.id,
            resume_id=res.id,
            canonical_job_key="l" * 64,
            idempotency_key="j" * 64,
            status="UNVERIFIED",
            destination_url="https://jobs.lever.co/beta/1",
            thread_id="thr-beta-a",
            submitted_at=now - timedelta(hours=2),
        )
        app_beta_b = Application(
            user_id=u1.id,
            job_id=j_beta_b.id,
            resume_id=res.id,
            canonical_job_key="m" * 64,
            idempotency_key="n" * 64,
            status="UNVERIFIED",
            destination_url="https://jobs.lever.co/beta/2",
            thread_id="thr-beta-b",
            submitted_at=now - timedelta(hours=2),
        )
        db.add_all([app_acme, app_beta_a, app_beta_b])

        enc_rt, key_ver = encrypt_oauth_token(
            "sync-refresh-token",
            user_id=str(u1.id),
            provider="google_gmail",
            token_kind="refresh_token",
            settings=cfg,
        )
        conn = OAuthConnection(
            user_id=u1.id,
            provider="google_gmail",
            mailbox_email="sync-u1@gmail.com",
            status="connected",
            encrypted_refresh_token=enc_rt,
            encryption_key_version=key_ver,
            granted_scopes=[GMAIL_READONLY_SCOPE],
        )
        db.add(conn)
        db.commit()
        for obj in (u1, u2, app_acme, app_beta_a, app_beta_b, conn):
            db.refresh(obj)

    state: dict[str, object] = {
        "messages": {},
        "history_404": False,
        "rate_limit_once": False,
        "invalid_grant": False,
        "now": now,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            if state["invalid_grant"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "sync-access-tok"})
        if url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/history"):
            if state["history_404"]:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(
                200,
                json={
                    "historyId": "200",
                    "history": [{"messagesAdded": [{"message": {"id": mid}}]} for mid in state["messages"]],
                },
            )
        if url.split("?")[0].rstrip("/").endswith("/messages"):
            if state["rate_limit_once"]:
                state["rate_limit_once"] = False
                return httpx.Response(429, json={"error": "rateLimitExceeded"})
            return httpx.Response(
                200,
                json={"messages": [{"id": mid} for mid in state["messages"]]},
            )
        if "/messages/" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            msg = state["messages"].get(mid)
            if not msg:
                return httpx.Response(404)
            return httpx.Response(200, json=msg)
        return httpx.Response(404)

    fake_redis = _FakeSyncRedis()
    svc = GmailSyncService(
        settings=cfg,
        redis_client=fake_redis,
        http_transport=httpx.MockTransport(_handler),
    )

    def _override_db():
        with TestingSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_gmail_sync_service] = lambda: svc
    register_gmail_confirmation_verifier()

    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "u1": u1,
                "u2": u2,
                "app_acme": app_acme,
                "app_beta_a": app_beta_a,
                "app_beta_b": app_beta_b,
                "conn": conn,
                "state": state,
                "redis": fake_redis,
                "Session": TestingSession,
            }
    finally:
        from app.services import application_policy_service as policy_service
        from app.services.gmail_confirmation_verifier import GmailConfirmationVerifier

        policy_service._CONFIRMATION_VERIFIER_REGISTRY.pop(GmailConfirmationVerifier.verifier_id, None)
        app.dependency_overrides.clear()
        engine.dispose()


def test_gmail_sync_confirms_exact_match_quarantines_ambiguous_and_rejects_forged_reconcile(sync_test_env):
    c: TestClient = sync_test_env["client"]
    u1 = sync_test_env["u1"]
    u2 = sync_test_env["u2"]
    app_acme = sync_test_env["app_acme"]
    app_beta_a = sync_test_env["app_beta_a"]
    app_beta_b = sync_test_env["app_beta_b"]
    state = sync_test_env["state"]
    TestingSession = sync_test_env["Session"]
    now: datetime = state["now"]

    # Seed 4 emails:
    # 1. Legit Acme Greenhouse confirmation -> confirms app_acme
    # 2. Ambiguous Beta Cloud confirmation (matches both Software Engineer and AI Engineer equally) -> ambiguous
    # 3. Rejection email -> rejected
    # 4. Bounce email -> rejected
    date_str = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    state["rate_limit_once"] = True
    state["messages"] = {
        "gmail-msg-acme-1": {
            "id": "gmail-msg-acme-1",
            "threadId": "thr-1",
            "historyId": "150",
            "snippet": "Thank you for applying to Acme Robotics for Backend Python Engineer (REQ-7788). We have received your application.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Acme Recruiting <no-reply@greenhouse.io>"},
                    {"name": "Subject", "value": "Application received: Acme Robotics"},
                    {"name": "Date", "value": date_str},
                    {"name": "Authentication-Results", "value": "mx.google.com; dkim=pass"},
                ]
            },
        },
        "gmail-msg-beta-ambig": {
            "id": "gmail-msg-beta-ambig",
            "threadId": "thr-2",
            "historyId": "151",
            "snippet": "Thank you for applying to Beta Cloud. Your application has been received.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Beta <no-reply@lever.co>"},
                    {"name": "Subject", "value": "Application Confirmation - Beta Cloud"},
                    {"name": "Date", "value": date_str},
                ]
            },
        },
        "gmail-msg-reject": {
            "id": "gmail-msg-reject",
            "threadId": "thr-3",
            "historyId": "152",
            "snippet": "Thank you for applying to Acme Robotics. Unfortunately we are not moving forward.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "no-reply@greenhouse.io"},
                    {"name": "Subject", "value": "Update on your application"},
                    {"name": "Date", "value": date_str},
                ]
            },
        },
        "gmail-msg-bounce": {
            "id": "gmail-msg-bounce",
            "threadId": "thr-4",
            "historyId": "153",
            "snippet": "Delivery Status Notification (Failure) - Thank you for applying",
            "payload": {
                "headers": [
                    {"name": "From", "value": "mailer-daemon@googlemail.com"},
                    {"name": "Subject", "value": "Undeliverable"},
                    {"name": "Date", "value": date_str},
                ]
            },
        },
    }

    hdr1 = _auth_hdr(u1.id)
    hdr2 = _auth_hdr(u2.id)

    sync_resp = c.post("/api/integrations/gmail/sync", headers=hdr1)
    assert sync_resp.status_code == 200
    assert "confirmed 1 application" in sync_resp.json()["message"]

    with TestingSession() as db:
        acme_refreshed = db.scalar(select(Application).where(Application.id == app_acme.id))
        assert acme_refreshed.status == "CONFIRMED"
        assert acme_refreshed.external_ref_id and acme_refreshed.external_ref_id.startswith("gmail_event:")
        assert "gmail-msg-acme-1" not in str(acme_refreshed.confirmation_evidence)

        # Ambiguous emails never confirm either Beta application
        beta_a = db.scalar(select(Application).where(Application.id == app_beta_a.id))
        beta_b = db.scalar(select(Application).where(Application.id == app_beta_b.id))
        assert beta_a.status == "UNVERIFIED"
        assert beta_b.status == "UNVERIFIED"

        events = {e.gmail_message_id: e for e in db.scalars(select(EmailConfirmationEvent))}
        assert events["gmail-msg-acme-1"].match_status == "confirmed"
        assert events["gmail-msg-beta-ambig"].match_status == "ambiguous"
        assert events["gmail-msg-reject"].match_status == "rejected"
        assert events["gmail-msg-bounce"].match_status == "rejected"

    # Sanitized events endpoint omits raw Gmail IDs
    ev_resp = c.get("/api/integrations/gmail/events", headers=hdr1)
    assert ev_resp.status_code == 200
    assert len(ev_resp.json()) == 4
    assert "gmail-msg-acme-1" not in ev_resp.text
    assert "gmail_thread_id" not in ev_resp.text

    # Forged reconcile request from client with fake event ID fails closed (422)
    forged_reconcile = c.post(
        f"/api/applications/{app_beta_a.id}/reconcile",
        headers=hdr1,
        json={"confirmation_evidence": {"email_confirmation_event_id": str(uuid.uuid4())}},
    )
    assert forged_reconcile.status_code == 422

    # Cross-user events endpoint returns empty for u2
    assert c.get("/api/integrations/gmail/events", headers=hdr2).json() == []


def test_gmail_sync_history_fallback_lock_and_revocation(sync_test_env):
    c: TestClient = sync_test_env["client"]
    u1 = sync_test_env["u1"]
    conn = sync_test_env["conn"]
    state = sync_test_env["state"]
    fake_redis: _FakeSyncRedis = sync_test_env["redis"]
    TestingSession = sync_test_env["Session"]

    hdr1 = _auth_hdr(u1.id)

    # Set an expired historyId -> triggers 404 fallback to bounded query
    with TestingSession() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.id == conn.id))
        row.last_history_id = "10"
        db.commit()

    state["history_404"] = True
    resp = c.post("/api/integrations/gmail/sync", headers=hdr1)
    assert resp.status_code == 200

    # Lock held -> sync skips safely
    fake_redis.locks.add(f"applypilot:oauth:gmail:sync_lock:{conn.id}")
    locked_resp = c.post("/api/integrations/gmail/sync", headers=hdr1)
    assert locked_resp.status_code == 200
    fake_redis.locks.clear()

    # invalid_grant during refresh -> marks connection revoked & clears encrypted token
    state["invalid_grant"] = True
    revoked_resp = c.post("/api/integrations/gmail/sync", headers=hdr1)
    assert revoked_resp.status_code == 200
    with TestingSession() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.id == conn.id))
        assert row.status == "revoked"
        assert row.encrypted_refresh_token is None
        assert row.revoked_at is not None

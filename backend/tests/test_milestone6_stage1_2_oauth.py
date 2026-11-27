import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.routes.integrations import get_gmail_oauth_service
from app.core.config import Settings
from app.core.security import create_access_token, decrypt_oauth_token
from app.database.models import AuditLog, OAuthConnection, User
from app.database.session import get_db
from app.main import app
from app.services.gmail_oauth_service import (
    GMAIL_READONLY_SCOPE,
    GmailOAuthService,
)

BACKEND_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def getdel(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        return self.store.pop(key, None)


def _auth_header(user_id: uuid.UUID) -> tuple[dict[str, str], str]:
    tok, _ = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {tok}"}, tok


@pytest.fixture()
def oauth_test_env(tmp_path):
    db_file = tmp_path / "m6_stage1_2_oauth.db"
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(alembic_cfg, "0006_milestone_6")

    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with TestingSession() as db:
        u1 = User(email="gmail-owner@example.com", hashed_password="hashed-password")
        u2 = User(email="gmail-other@example.com", hashed_password="hashed-password")
        db.add_all([u1, u2])
        db.commit()
        db.refresh(u1)
        db.refresh(u2)

    fake_redis = _FakeRedis()
    cfg = Settings(
        _env_file=None,
        GOOGLE_CLIENT_ID="synthetic-google-client-id",
        GOOGLE_CLIENT_SECRET="synthetic-google-client-secret",
        GOOGLE_OAUTH_REDIRECT_URI="http://localhost:3000/api/integrations/gmail/callback",
        ENCRYPTION_KEY="stage1-2-oauth-encryption-key-32bytes",
        ENCRYPTION_KEY_VERSION="v1",
    )

    transport_state: dict[str, object] = {
        "token_status": 200,
        "token_body": {
            "access_token": "synthetic-access-token-1",
            "refresh_token": "synthetic-refresh-token-1",
            "scope": GMAIL_READONLY_SCOPE,
            "token_type": "Bearer",
            "expires_in": 3600,
        },
        "profile_status": 200,
        "profile_body": {"emailAddress": "Candidate.Mailbox@Gmail.COM", "historyId": "9001"},
        "revoke_status": 200,
        "token_calls": [],
        "revoke_calls": [],
        "raise_timeout": False,
        "raise_revoke_error": False,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            if transport_state["raise_timeout"]:
                raise httpx.ReadTimeout("timeout", request=request)
            body = parse_qs(request.content.decode("utf-8"))
            transport_state["token_calls"].append(body)
            return httpx.Response(
                int(transport_state["token_status"]),
                json=transport_state["token_body"],
            )
        if url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/profile"):
            return httpx.Response(
                int(transport_state["profile_status"]),
                json=transport_state["profile_body"],
            )
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            transport_state["revoke_calls"].append(request.content.decode("utf-8"))
            if transport_state["raise_revoke_error"]:
                raise httpx.ConnectError("revoke unreachable", request=request)
            return httpx.Response(int(transport_state["revoke_status"]), text="{}")
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(_handler)
    service = GmailOAuthService(settings=cfg, redis_client=fake_redis, http_transport=mock_transport)

    def _override_db():
        with TestingSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_gmail_oauth_service] = lambda: service

    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "u1": u1,
                "u2": u2,
                "redis": fake_redis,
                "cfg": cfg,
                "transport": transport_state,
                "Session": TestingSession,
            }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_gmail_oauth_pkce_state_callback_encryption_and_no_secret_leaks(oauth_test_env, caplog):
    c: TestClient = oauth_test_env["client"]
    u1 = oauth_test_env["u1"]
    r: _FakeRedis = oauth_test_env["redis"]
    cfg: Settings = oauth_test_env["cfg"]
    t_state = oauth_test_env["transport"]
    TestingSession = oauth_test_env["Session"]

    # Unauthenticated requests fail closed
    assert c.post("/api/integrations/gmail/connect/start").status_code == 401
    assert c.post("/api/integrations/gmail/callback", json={"code": "c", "state": "s" * 20}).status_code == 401
    assert c.get("/api/integrations/gmail/status").status_code == 401
    assert c.post("/api/integrations/gmail/disconnect").status_code == 401

    hdr1, _ = _auth_header(u1.id)
    caplog.clear()
    start_resp = c.post("/api/integrations/gmail/connect/start", headers=hdr1)
    assert start_resp.status_code == 200
    start_data = start_resp.json()
    assert start_data["scopes"] == [GMAIL_READONLY_SCOPE]

    parsed = urlparse(start_data["authorization_url"])
    qs = parse_qs(parsed.query)
    assert qs["scope"] == [GMAIL_READONLY_SCOPE]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["include_granted_scopes"] == ["false"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["redirect_uri"] == [cfg.google_oauth_redirect_uri]
    state_val = qs["state"][0]
    assert len(state_val) >= 32
    assert "code_verifier" not in start_resp.text

    # Verify Redis state payload & TTL & PKCE S256 match
    redis_key = f"applypilot:oauth:gmail:state:{state_val}"
    assert r.ttls[redis_key] == 600
    stored = json.loads(r.store[redis_key])
    assert stored["user_id"] == str(u1.id)
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(stored["code_verifier"].encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert qs["code_challenge"] == [expected_challenge]

    # Complete callback
    cb_resp = c.post(
        "/api/integrations/gmail/callback",
        headers=hdr1,
        json={"code": "auth-code-secret-123", "state": state_val},
    )
    assert cb_resp.status_code == 200
    cb_body = cb_resp.json()
    assert cb_body["mailbox_email"] == "candidate.mailbox@gmail.com"
    assert cb_body["status"] == "connected"
    assert cb_body["granted_scopes"] == [GMAIL_READONLY_SCOPE]
    for secret in (
        "synthetic-refresh-token-1",
        "synthetic-access-token-1",
        "synthetic-google-client-secret",
        "auth-code-secret-123",
        "encrypted_refresh_token",
        "encryption_key_version",
        "last_history_id",
    ):
        assert secret not in cb_resp.text

    # Token exchange sent exact redirect_uri and PKCE verifier
    assert len(t_state["token_calls"]) == 1
    assert t_state["token_calls"][0]["code_verifier"] == [stored["code_verifier"]]
    assert t_state["token_calls"][0]["redirect_uri"] == [cfg.google_oauth_redirect_uri]

    # DB holds encrypted refresh token (never plaintext)
    with TestingSession() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.user_id == u1.id))
        assert row is not None
        assert row.encrypted_refresh_token
        assert "synthetic-refresh-token-1" not in row.encrypted_refresh_token
        assert (
            decrypt_oauth_token(
                row.encrypted_refresh_token,
                key_version=row.encryption_key_version,
                user_id=str(u1.id),
                provider="google_gmail",
                token_kind="refresh_token",
                settings=cfg,
            )
            == "synthetic-refresh-token-1"
        )

    # Duplicate / replay callback with same state rejected (state consumed atomically)
    replay = c.post(
        "/api/integrations/gmail/callback",
        headers=hdr1,
        json={"code": "auth-code-secret-123", "state": state_val},
    )
    assert replay.status_code == 400

    # Logs never contain secrets
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "synthetic-refresh-token-1" not in msg
        assert "synthetic-access-token-1" not in msg
        assert "auth-code-secret-123" not in msg
        assert stored["code_verifier"] not in msg


def test_gmail_oauth_cross_user_session_binding_and_error_cases(oauth_test_env):
    c: TestClient = oauth_test_env["client"]
    u1 = oauth_test_env["u1"]
    u2 = oauth_test_env["u2"]
    r: _FakeRedis = oauth_test_env["redis"]
    t_state = oauth_test_env["transport"]

    hdr1, _ = _auth_header(u1.id)
    hdr2, _ = _auth_header(u2.id)

    # 1. Cross-user callback rejected with 403
    s1 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state1 = parse_qs(urlparse(s1["authorization_url"]).query)["state"][0]
    cross_cb = c.post(
        "/api/integrations/gmail/callback",
        headers=hdr2,
        json={"code": "c1", "state": state1},
    )
    assert cross_cb.status_code == 403

    # 2. Different session for same user rejected with 403
    s1b = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state1b = parse_qs(urlparse(s1b["authorization_url"]).query)["state"][0]
    other_tok_u1, _ = create_access_token(str(u1.id), expires_minutes=15)
    # Ensure token differs from hdr1
    if other_tok_u1 == hdr1["Authorization"].split(" ", 1)[1]:
        other_tok_u1, _ = create_access_token(str(u1.id), expires_minutes=20)
    mismatch_cb = c.post(
        "/api/integrations/gmail/callback",
        headers={"Authorization": f"Bearer {other_tok_u1}"},
        json={"code": "c1b", "state": state1b},
    )
    assert mismatch_cb.status_code == 403

    # 3. Broader unexpected scope rejected
    s2 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state2 = parse_qs(urlparse(s2["authorization_url"]).query)["state"][0]
    t_state["token_body"] = {
        "access_token": "tok",
        "refresh_token": "rt",
        "scope": f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/gmail.send",
    }
    assert (
        c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c2", "state": state2}).status_code
        == 400
    )

    # 4. Missing gmail.readonly scope rejected
    s3 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state3 = parse_qs(urlparse(s3["authorization_url"]).query)["state"][0]
    t_state["token_body"] = {"access_token": "tok", "refresh_token": "rt", "scope": "openid email"}
    assert (
        c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c3", "state": state3}).status_code
        == 400
    )

    # 5. Token exchange invalid_grant & timeout
    s4 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state4 = parse_qs(urlparse(s4["authorization_url"]).query)["state"][0]
    t_state["token_status"] = 400
    t_state["token_body"] = {"error": "invalid_grant"}
    assert (
        c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "bad", "state": state4}).status_code
        == 400
    )

    s5 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state5 = parse_qs(urlparse(s5["authorization_url"]).query)["state"][0]
    t_state["token_status"] = 200
    t_state["raise_timeout"] = True
    assert (
        c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c5", "state": state5}).status_code
        == 504
    )
    t_state["raise_timeout"] = False

    # 6. First connect missing refresh_token fails
    s6 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state6 = parse_qs(urlparse(s6["authorization_url"]).query)["state"][0]
    t_state["token_body"] = {"access_token": "tok", "scope": GMAIL_READONLY_SCOPE}
    assert (
        c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c6", "state": state6}).status_code
        == 400
    )

    # 7. Redis outage fails closed (503)
    r.fail = True
    assert c.post("/api/integrations/gmail/connect/start", headers=hdr1).status_code == 503
    r.fail = False


def test_gmail_reconnect_preserves_refresh_token_status_and_idempotent_disconnect(oauth_test_env):
    c: TestClient = oauth_test_env["client"]
    u1 = oauth_test_env["u1"]
    u2 = oauth_test_env["u2"]
    cfg: Settings = oauth_test_env["cfg"]
    t_state = oauth_test_env["transport"]
    TestingSession = oauth_test_env["Session"]

    hdr1, _ = _auth_header(u1.id)
    hdr2, _ = _auth_header(u2.id)

    # Connect u1
    s1 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state1 = parse_qs(urlparse(s1["authorization_url"]).query)["state"][0]
    conn_resp = c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c1", "state": state1})
    assert conn_resp.status_code == 200
    conn_id = conn_resp.json()["id"]

    # Reconnect u1 when Google omits refresh_token -> preserves existing encrypted refresh token
    s2 = c.post("/api/integrations/gmail/connect/start", headers=hdr1).json()
    state2 = parse_qs(urlparse(s2["authorization_url"]).query)["state"][0]
    t_state["token_body"] = {"access_token": "new-access", "scope": GMAIL_READONLY_SCOPE}
    reconn = c.post("/api/integrations/gmail/callback", headers=hdr1, json={"code": "c2", "state": state2})
    assert reconn.status_code == 200
    with TestingSession() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.id == uuid.UUID(conn_id)))
        assert row is not None and row.encrypted_refresh_token
        assert (
            decrypt_oauth_token(
                row.encrypted_refresh_token,
                key_version=row.encryption_key_version,
                user_id=str(u1.id),
                provider="google_gmail",
                token_kind="refresh_token",
                settings=cfg,
            )
            == "synthetic-refresh-token-1"
        )

    # Status shows connected for u1 and disconnected for u2
    st1 = c.get("/api/integrations/gmail/status", headers=hdr1)
    assert st1.status_code == 200
    assert st1.json()["connected"] is True
    assert len(st1.json()["connections"]) == 1
    assert "encrypted_refresh_token" not in st1.text

    st2 = c.get("/api/integrations/gmail/status", headers=hdr2)
    assert st2.status_code == 200
    assert st2.json() == {"connected": False, "connections": []}

    # Cross-user disconnect fails with 404
    assert (
        c.post("/api/integrations/gmail/disconnect", headers=hdr2, json={"connection_id": conn_id}).status_code
        == 404
    )

    # Disconnect succeeds even when remote Google revocation fails
    t_state["raise_revoke_error"] = True
    disc1 = c.post("/api/integrations/gmail/disconnect", headers=hdr1, json={"connection_id": conn_id})
    assert disc1.status_code == 200
    assert disc1.json()["status"] == "disconnected"
    assert len(t_state["revoke_calls"]) == 1

    # Local encrypted token cleared & audit logged
    with TestingSession() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.id == uuid.UUID(conn_id)))
        assert row.status == "disconnected"
        assert row.encrypted_refresh_token is None
        assert row.encryption_key_version is None
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.user_id == u1.id, AuditLog.action == "oauth.disconnected"))
        )
        assert len(audits) == 1

    # Repeated disconnect is idempotent
    disc2 = c.post("/api/integrations/gmail/disconnect", headers=hdr1, json={"connection_id": conn_id})
    assert disc2.status_code == 200
    assert disc2.json()["status"] == "disconnected"

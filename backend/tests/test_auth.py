import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token
from app.database.base import Base
from app.database.session import get_db
from app.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def test_register_login_and_me(client: TestClient):
    reg = client.post(
        "/api/auth/register",
        json={"email": "Chethan@Example.com", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    reg_body = reg.json()
    assert reg_body["email"] == "chethan@example.com"
    assert "hashed_password" not in reg_body
    assert "password" not in reg_body

    dup = client.post(
        "/api/auth/register",
        json={"email": "chethan@example.com", "password": "StrongPassword123!"},
    )
    assert dup.status_code == 409

    login = client.post(
        "/api/auth/login",
        json={"email": "chethan@example.com", "password": "StrongPassword123!"},
    )
    assert login.status_code == 200
    token_data = login.json()
    assert token_data["token_type"] == "bearer"

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_data['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "chethan@example.com"


def test_auth_rejects_invalid_credentials_and_tokens(client: TestClient):
    client.post(
        "/api/auth/register",
        json={"email": "user@example.com", "password": "StrongPassword123!"},
    )

    bad_pw = client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "WrongPassword"},
    )
    assert bad_pw.status_code == 401

    no_hdr = client.get("/api/auth/me")
    assert no_hdr.status_code == 401

    bad_tok = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert bad_tok.status_code == 401

    expired_tok, _ = create_access_token(subject="00000000-0000-0000-0000-000000000001", expires_minutes=-5)
    exp_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_tok}"})
    assert exp_resp.status_code == 401

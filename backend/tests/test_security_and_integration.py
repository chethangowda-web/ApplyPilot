import logging
import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.logging import StructuredJsonFormatter
from app.core.security import create_access_token, hash_password, verify_password
from app.database.base import Base
from app.database.models import AuditLog
from app.database.session import get_db
from app.main import app
from app.services.resume_vault_service import resolve_owned_resume_path


def _sample_pdf(tag: str = "CANARY_RESUME_CONTENT") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        f"{tag}\nPROFESSIONAL SUMMARY\nPython FastAPI LangGraph\nTECHNICAL SKILLS\nPython, SQL\n",
    )
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    monkeypatch.setenv("STORAGE_DIR", str(storage_dir))
    get_settings.cache_clear()

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
        yield tc, TestingSession, storage_dir
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    engine.dispose()


def _auth_headers(client: TestClient, email: str) -> dict[str, str]:
    assert client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"}).status_code == 201
    tok = client.post("/api/auth/login", json={"email": email, "password": "StrongPassword123!"})
    return {"Authorization": f"Bearer {tok.json()['access_token']}"}


def test_production_secret_key_enforcement(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-only-secret-key-change-me-minimum-32-bytes")
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings()

    monkeypatch.setenv("SECRET_KEY", "short-key")
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings()

    monkeypatch.setenv("SECRET_KEY", "9f3c7e1a5b8d2f4c6e0a9b1d3f5a7c9e2b4d6f8a0c1e3b5d")
    prod_settings = Settings()
    assert prod_settings.environment == "production"


def test_password_hashing_salted_and_bounded():
    h1 = hash_password("MySecurePass#2026")
    h2 = hash_password("MySecurePass#2026")
    assert h1 != h2
    assert verify_password("MySecurePass#2026", h1) is True
    assert verify_password("WrongPass", h1) is False
    with pytest.raises(ValueError):
        hash_password("x" * 73)


def test_duplicate_resume_checksum_and_cross_user_isolation(api_env):
    client, _, storage_dir = api_env
    alice = _auth_headers(client, "alice_sec@example.com")
    bob = _auth_headers(client, "bob_sec@example.com")

    pdf = _sample_pdf("ALICE_SECRET_TEXT")
    first = client.post(
        "/api/resume/upload",
        headers=alice,
        files={"file": ("../../etc/passwd/../../../Master.pdf", pdf, "application/pdf")},
        data={"is_master": "true"},
    )
    assert first.status_code == 201
    r1 = first.json()
    assert r1["original_filename"] == "Master.pdf"

    # Duplicate upload by same user -> 409 Conflict
    dup = client.post(
        "/api/resume/upload",
        headers=alice,
        files={"file": ("Master_copy.pdf", pdf, "application/pdf")},
        data={"is_master": "true"},
    )
    assert dup.status_code == 409

    # Different user uploading identical bytes succeeds independently with matching SHA-256
    bob_up = client.post(
        "/api/resume/upload",
        headers=bob,
        files={"file": ("BobMaster.pdf", pdf, "application/pdf")},
        data={"is_master": "true"},
    )
    assert bob_up.status_code == 201
    assert bob_up.json()["sha256_checksum"] == r1["sha256_checksum"]

    # Path traversal check on storage resolver
    outside = storage_dir.parent / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(Exception):
        resolve_owned_resume_path(storage_dir, str(outside))


def test_invalid_tokens_corrupted_files_and_log_redaction(api_env):
    client, SessionLocal, _ = api_env
    headers = _auth_headers(client, "logger_sec@example.com")

    # Corrupted DOCX (valid ZIP header but invalid DOCX XML)
    bad_docx = client.post(
        "/api/resume/upload",
        headers=headers,
        files={
            "file": (
                "corrupt.docx",
                b"PK\x03\x04corrupted-zip-body",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert bad_docx.status_code == 400

    # Empty file
    empty_up = client.post(
        "/api/resume/upload",
        headers=headers,
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert empty_up.status_code == 400

    # Non-UUID subject in JWT
    bad_sub_tok, _ = create_access_token(subject="not-a-uuid")
    assert client.get("/api/profile", headers={"Authorization": f"Bearer {bad_sub_tok}"}).status_code == 401

    # Structured formatter redacts PII and secrets
    fmt = StructuredJsonFormatter()
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Auth Bearer secret.jwt.token for user@example.com",
        args=(),
        exc_info=None,
    )
    rec.extra_fields = {"password": "pw", "phone": "9999999999", "resume_text": "CANARY"}
    rendered = fmt.format(rec)
    assert "secret.jwt.token" not in rendered
    assert "user@example.com" not in rendered
    assert "9999999999" not in rendered
    assert "CANARY" not in rendered

    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog)).all()
        assert len(logs) >= 2
        for entry in logs:
            assert "StrongPassword123!" not in str(entry.event_metadata)

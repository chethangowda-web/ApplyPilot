import io
import logging
import docx
import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.database.base import Base
from app.database.models import AuditLog
from app.database.session import get_db
from app.main import app


def _make_sample_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "SECRET_CANARY_NAME\n"
        "PROFESSIONAL SUMMARY\n"
        "Python and FastAPI developer.\n"
        "TECHNICAL SKILLS\n"
        "Python, FastAPI, PostgreSQL\n"
        "PROJECTS\n"
        "ORBIT https://github.com/example/orbit\n",
    )
    buf = doc.tobytes()
    doc.close()
    return buf


def _make_sample_docx_bytes() -> bytes:
    d = docx.Document()
    d.add_paragraph("EDUCATION")
    d.add_paragraph("B.Tech Computer Science")
    d.add_paragraph("EXPERIENCE")
    d.add_paragraph("Backend Intern at Example Corp https://linkedin.com/in/example")
    bio = io.BytesIO()
    d.save(bio)
    return bio.getvalue()


@pytest.fixture()
def test_env(tmp_path, monkeypatch):
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


def _register_and_login(client: TestClient, email: str) -> dict[str, str]:
    assert client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"}).status_code == 201
    tok = client.post("/api/auth/login", json={"email": email, "password": "StrongPassword123!"})
    return {"Authorization": f"Bearer {tok.json()['access_token']}"}


def test_upload_pdf_and_docx_preserves_master_and_parses_metadata(test_env, caplog):
    client, SessionLocal, storage_dir = test_env
    caplog.set_level(logging.INFO)
    headers = _register_and_login(client, "owner@example.com")

    pdf_bytes = _make_sample_pdf_bytes()
    res1 = client.post(
        "/api/resume/upload",
        headers=headers,
        files={"file": ("../../Master_Resume.pdf", pdf_bytes, "application/pdf")},
        data={"is_master": "true"},
    )
    assert res1.status_code == 201
    r1 = res1.json()
    assert r1["original_filename"] == "Master_Resume.pdf"
    assert r1["version_label"] == "master-v1"
    assert r1["validation_passed"] is True
    assert r1["parsed_metadata"]["page_count"] == 1
    assert set(r1["parsed_metadata"]["detected_sections"]) >= {"summary", "skills", "projects"}
    assert "https://github.com/example/orbit" in r1["parsed_metadata"]["public_links"]

    docx_bytes = _make_sample_docx_bytes()
    res2 = client.post(
        "/api/resume/upload",
        headers=headers,
        files={"file": ("Resume_v2.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"is_master": "true"},
    )
    assert res2.status_code == 201
    r2 = res2.json()
    assert r2["version_label"] == "master-v2"
    assert set(r2["parsed_metadata"]["detected_sections"]) >= {"education", "experience"}

    versions = client.get("/api/resume/versions", headers=headers)
    assert versions.status_code == 200
    assert len(versions.json()) == 2

    dl = client.get(f"/api/resume/versions/{r1['id']}/download", headers=headers)
    assert dl.status_code == 200
    assert dl.content == pdf_bytes

    # Verify audit logs and logs never expose raw resume contents
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog)).all()
        assert any(log.action == "resume_uploaded" for log in logs)
        for log in logs:
            assert "SECRET_CANARY_NAME" not in str(log.event_metadata)
    assert "SECRET_CANARY_NAME" not in caplog.text


def test_resume_rejects_invalid_files_and_enforces_user_isolation(test_env, monkeypatch):
    client, _, _ = test_env
    alice = _register_and_login(client, "alice_res@example.com")
    bob = _register_and_login(client, "bob_res@example.com")

    # Reject non-PDF/DOCX extension
    bad_ext = client.post(
        "/api/resume/upload",
        headers=alice,
        files={"file": ("evil.exe", b"MZ12345", "application/octet-stream")},
    )
    assert bad_ext.status_code == 400

    # Reject spoofed PDF magic bytes
    bad_magic = client.post(
        "/api/resume/upload",
        headers=alice,
        files={"file": ("fake.pdf", b"not-a-real-pdf", "application/pdf")},
    )
    assert bad_magic.status_code == 400

    # Reject oversized upload (DOCX is ~36 KB > 1024 bytes)
    monkeypatch.setenv("MAX_RESUME_SIZE_BYTES", "1024")
    get_settings.cache_clear()
    big_docx = _make_sample_docx_bytes()
    assert len(big_docx) > 1024
    oversized = client.post(
        "/api/resume/upload",
        headers=alice,
        files={
            "file": (
                "big.docx",
                big_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert oversized.status_code == 413
    monkeypatch.setenv("MAX_RESUME_SIZE_BYTES", "5242880")
    get_settings.cache_clear()

    # Valid upload by Alice
    ok = client.post(
        "/api/resume/upload",
        headers=alice,
        files={"file": ("alice.pdf", _make_sample_pdf_bytes(), "application/pdf")},
    )
    assert ok.status_code == 201
    alice_resume_id = ok.json()["id"]

    # Bob cannot list, read, or download Alice's resume
    assert client.get("/api/resume/versions", headers=bob).json() == []
    assert client.get(f"/api/resume/versions/{alice_resume_id}", headers=bob).status_code == 404
    assert client.get(f"/api/resume/versions/{alice_resume_id}/download", headers=bob).status_code == 404

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import socket
import threading
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import psycopg
import pytest
import redis as redis_lib
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.database.base import Base
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    AuditLog,
    CandidateProfile,
    Job,
    JobMatch,
    JobSource,
    Resume,
    ResumeJobMapping,
    User,
)
from app.database.session import get_db
from app.main import app
from app.api.routes.applications import get_application_execution_service
from app.schemas.applications import ApplicationAnswerInput
import app.services.application_policy_service as policy_mod
from app.services.application_execution_service import ApplicationExecutionService
from app.services.application_policy_service import (
    ConfirmationProofIssuer,
    VerifiedConfirmationProof,
)
from app.services.browser_executor import (
    BrowserHandoffManager,
    LocalSSRFEgressProxy,
    SubmitGuardState,
    build_protected_chromium_args,
    create_isolated_browser_context,
    install_submit_protection,
)


def _auth_header(user_id: uuid.UUID) -> dict[str, str]:
    token, _ttl = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _seed_user_job_resume(
    db,
    *,
    storage_root: Path,
    email: str,
    dest_url: str,
    connector_type: str = "greenhouse",
    source_config: dict | None = None,
    raw_jd: str = "Requirements: Python and FastAPI.",
) -> tuple[User, CandidateProfile, Job, Resume, ResumeJobMapping]:
    user = User(email=email, hashed_password="h", is_active=True)
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Chethan Candidate",
        email=email,
        phone="+91-9999999999",
        location="Bengaluru",
        summary="Python FastAPI Engineer",
    )
    db.add(profile)
    db.flush()

    source = JobSource(
        id=uuid.uuid4(),
        source_name=f"src-{uuid.uuid4().hex[:8]}",
        connector_type=connector_type,
        config=source_config or {},
    )
    db.add(source)
    db.flush()

    job = Job(
        id=uuid.uuid4(),
        source_id=source.id,
        source_job_id=f"job-{uuid.uuid4().hex[:8]}",
        canonical_url=dest_url,
        application_url=dest_url,
        dedup_key=hashlib.sha256(f"{email}:{dest_url}:{uuid.uuid4()}".encode()).hexdigest(),
        content_checksum="e" * 64,
        company="Acme Corp",
        role_title="Backend Engineer",
        raw_jd=raw_jd,
        status="active",
        source_provenance={"requisition_id": f"REQ-{uuid.uuid4().hex[:6]}"},
    )
    db.add(job)
    db.flush()

    match = JobMatch(
        job_id=job.id,
        profile_id=profile.id,
        score=92.0,
        eligible=True,
        hard_constraints_passed=True,
        recommendation="strong_match",
        explanation="Strong Python match",
    )
    db.add(match)

    pdf_bytes = b"%PDF-1.4\n%tailored-checkpoint-d-resume\n%%EOF\n"
    pdf_path = storage_root / f"tailored_{user.id.hex[:8]}.pdf"
    pdf_path.write_bytes(pdf_bytes)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    resume = Resume(
        id=uuid.uuid4(),
        profile_id=profile.id,
        is_master=False,
        original_filename="tailored.pdf",
        content_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        pdf_path=str(pdf_path),
        version_label="v1",
        sha256_checksum=checksum,
        generation_status="completed",
        validation_passed=True,
    )
    db.add(resume)
    db.flush()

    mapping = ResumeJobMapping(
        id=uuid.uuid4(),
        resume_id=resume.id,
        job_id=job.id,
        profile_id=profile.id,
        run_id=f"run-{uuid.uuid4().hex[:6]}",
        version_number=1,
        is_active=True,
        evidence_fingerprint="f" * 64,
        status="completed",
        approval_status="approved",
    )
    db.add(mapping)
    db.commit()
    return user, profile, job, resume, mapping


@pytest.fixture()
def checkpoint_d_env(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_DIR", str(storage_root))
    monkeypatch.setenv("ENCRYPTION_KEY", "checkpoint-d-encryption-key-secret!")
    monkeypatch.setenv("ENCRYPTION_KEY_VERSION", "v1")
    get_settings.cache_clear()

    db_path = tmp_path / "checkpoint_d.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def _override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    server_hits: list[tuple[str, str]] = []

    class _AppFormHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            server_hits.append(("GET", self.path))
            html = (
                b"<!doctype html><html><body>"
                b"<form id='app-form' method='POST' action='/apply' enctype='multipart/form-data'>"
                b"<label for='f_name'>Full Name</label><input id='f_name' name='full_name' required />"
                b"<label for='f_email'>Email</label><input id='f_email' name='email' type='email' required />"
                b"<label for='f_nid'>National ID</label><input id='f_nid' name='national_id' />"
                b"<label for='f_cv'>Resume</label><input id='f_cv' name='resume' type='file' />"
                b"<button id='sub-btn' type='submit'>Submit Application</button>"
                b"</form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            server_hits.append(("POST", self.path))
            body = b"submitted-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _AppFormHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{fixture.server_port}/apply-form/owner"
    dest_u2 = f"http://127.0.0.1:{fixture.server_port}/apply-form/other"

    cfg = Settings(
        _env_file=None,
        encryption_key="checkpoint-d-encryption-key-secret!",
        encryption_key_version="v1",
        storage_dir=str(storage_root),
        browser_execution_timeout_seconds=45,
        browser_handoff_ttl_seconds=600,
        require_os_egress_firewall=False,
    )
    mgr = BrowserHandoffManager(storage_root=storage_root)
    svc = ApplicationExecutionService(
        settings=cfg,
        handoff_manager=mgr,
        _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
    )

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_application_execution_service] = lambda: svc

    with TestingSession() as db:
        u1, p1, j1, r1, m1 = _seed_user_job_resume(
            db,
            storage_root=storage_root,
            email="owner-d@example.com",
            dest_url=dest,
        )
        u2, p2, j2, r2, m2 = _seed_user_job_resume(
            db,
            storage_root=storage_root,
            email="attacker-d@example.com",
            dest_url=dest_u2,
        )

    client = TestClient(app)
    try:
        yield {
            "client": client,
            "TestingSession": TestingSession,
            "storage_root": storage_root,
            "cfg": cfg,
            "mgr": mgr,
            "svc": svc,
            "dest": dest,
            "fixture_port": fixture.server_port,
            "server_hits": server_hits,
            "u1": u1,
            "p1": p1,
            "j1": j1,
            "r1": r1,
            "m1": m1,
            "u2": u2,
            "p2": p2,
            "j2": j2,
            "r2": r2,
            "m2": m2,
        }
    finally:
        for sess in list(mgr._sessions):
            try:
                mgr.close_session(handoff_session_id=sess, user_id=u1.id)
            except Exception:
                pass
            try:
                mgr.close_session(handoff_session_id=sess, user_id=u2.id)
            except Exception:
                pass
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        fixture.shutdown()
        fixture.server_close()


def test_checkpoint_d_auth_cross_user_isolation_and_schema_forbid(checkpoint_d_env):
    c: TestClient = checkpoint_d_env["client"]
    u1, j1, r1 = checkpoint_d_env["u1"], checkpoint_d_env["j1"], checkpoint_d_env["r1"]
    u2 = checkpoint_d_env["u2"]

    # 1. Unauthenticated caller denied cleanly (401)
    assert c.post("/api/applications/prepare", json={"job_id": str(j1.id), "resume_id": str(r1.id)}).status_code == 401
    assert c.get("/api/applications").status_code == 401

    # 2. Prepare authenticated application for u1
    prep = c.post(
        "/api/applications/prepare",
        headers=_auth_header(u1.id),
        json={"job_id": str(j1.id), "resume_id": str(r1.id)},
    )
    assert prep.status_code == 201
    app_id = prep.json()["application_id"]

    # 3. Cross-user access denied across ALL application/handoff/artifact endpoints
    u2_hdr = _auth_header(u2.id)
    assert c.get("/api/applications", headers=u2_hdr).json() == []
    assert c.get(f"/api/applications/{app_id}", headers=u2_hdr).status_code == 404
    assert (
        c.post(
            f"/api/applications/{app_id}/answers",
            headers=u2_hdr,
            json={"answers": [{"question_key": "full_name", "question": "Full Name", "answer": "Attacker"}]},
        ).status_code
        == 404
    )
    assert c.post(f"/api/applications/{app_id}/approve", headers=u2_hdr).status_code == 404
    assert c.post(f"/api/applications/{app_id}/execute", headers=u2_hdr).status_code == 404
    assert (
        c.post(
            f"/api/applications/{app_id}/authorize-manual-submit",
            headers=u2_hdr,
            json={
                "handoff_session_id": str(uuid.uuid4()),
                "pending_submission_id": str(uuid.uuid4()),
                "expected_form_action": checkpoint_d_env["dest"],
                "expected_http_method": "POST",
                "expected_payload_sha256": "a" * 64,
            },
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/applications/{app_id}/submit-outcome",
            headers=u2_hdr,
            json={"outcome": "submitted_in_browser"},
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/applications/{app_id}/reconcile",
            headers=u2_hdr,
            json={"confirmation_evidence": {"verification_source": "official_api_receipt"}},
        ).status_code
        == 404
    )
    assert c.get(f"/api/applications/{app_id}/artifacts/{uuid.uuid4()}", headers=u2_hdr).status_code == 404

    # 4. Caller-supplied user_id / token / consumed overrides are forbidden
    u1_hdr = _auth_header(u1.id)
    assert (
        c.post(
            f"/api/applications/{app_id}/answers",
            headers=u1_hdr,
            json={"user_id": str(u2.id), "answers": [{"question_key": "q", "question": "q", "answer": "a"}]},
        ).status_code
        == 422
    )
    assert (
        c.post(
            f"/api/applications/{app_id}/authorize-manual-submit",
            headers=u1_hdr,
            json={
                "handoff_session_id": str(uuid.uuid4()),
                "pending_submission_id": str(uuid.uuid4()),
                "expected_form_action": checkpoint_d_env["dest"],
                "expected_http_method": "POST",
                "expected_payload_sha256": "a" * 64,
                "authorization_token": "forged",
                "consumed": False,
            },
        ).status_code
        == 422
    )
    assert (
        c.post(
            f"/api/applications/{app_id}/authorize-manual-submit",
            headers=u1_hdr,
            json={
                "handoff_session_id": str(uuid.uuid4()),
                "pending_submission_id": str(uuid.uuid4()),
                "expected_form_action": checkpoint_d_env["dest"],
                "expected_http_method": "POST",
            },
        ).status_code
        == 422
    )


def test_checkpoint_d_end_to_end_visible_handoff_auth_binding_and_no_auto_submit(checkpoint_d_env, caplog):
    c: TestClient = checkpoint_d_env["client"]
    u1, j1, r1 = checkpoint_d_env["u1"], checkpoint_d_env["j1"], checkpoint_d_env["r1"]
    u2 = checkpoint_d_env["u2"]
    port = checkpoint_d_env["fixture_port"]
    hits = checkpoint_d_env["server_hits"]
    TestingSession = checkpoint_d_env["TestingSession"]
    hdr = _auth_header(u1.id)

    prep = c.post(
        "/api/applications/prepare",
        headers=hdr,
        json={"job_id": str(j1.id), "resume_id": str(r1.id)},
    )
    assert prep.status_code == 201
    app_id = uuid.UUID(prep.json()["application_id"])

    # Submit verified answers including a sensitive field where caller falsely sets is_sensitive=False
    synthetic_ssn = "SYNTHETIC-SSN-CHECKPOINT-D-4455"
    ans_resp = c.post(
        f"/api/applications/{app_id}/answers",
        headers=hdr,
        json={
            "answers": [
                {"question_key": "full_name", "question": "Full Name", "answer": "Chethan Candidate"},
                {"question_key": "email", "question": "Email", "answer": "owner-d@example.com"},
                {
                    "question_key": "national_id",
                    "question": "National ID / SSN",
                    "answer": synthetic_ssn,
                    "is_sensitive": False,  # server classification MUST force AES-256-GCM encryption
                },
            ]
        },
    )
    assert ans_resp.status_code == 200
    assert synthetic_ssn not in ans_resp.text
    detail_answers = {a["question_key"]: a for a in ans_resp.json()["answers"]}
    assert detail_answers["national_id"]["is_sensitive"] is True
    assert detail_answers["national_id"]["answer_preview"] == "[REDACTED — VERIFIED]"

    with TestingSession() as db:
        db_row = db.scalar(
            select(ApplicationAnswer).where(
                ApplicationAnswer.application_id == app_id,
                ApplicationAnswer.question_key == "national_id",
            )
        )
        assert db_row is not None
        assert db_row.is_sensitive is True
        assert db_row.answer_plaintext is None
        assert db_row.answer_encrypted and synthetic_ssn not in db_row.answer_encrypted

    # Approve -> creates immutable ApprovalRecord
    appr = c.post(f"/api/applications/{app_id}/approve", headers=hdr, json={"notes": "Reviewed"})
    assert appr.status_code == 200
    assert appr.json()["decision"] == "approved"

    # Execute in visible_handoff mode -> prefills, captures masked artifact, stops BEFORE submit!
    caplog.clear()
    hits.clear()
    exec_resp = c.post(
        f"/api/applications/{app_id}/execute",
        headers=hdr,
        json={"options": {"browser_mode": "visible_handoff", "capture_artifacts": True}},
    )
    assert exec_resp.status_code == 200
    body = exec_resp.json()
    assert body["stopped_before_submit"] is True
    assert body["status"] == "MANUAL_ACTION_REQUIRED"
    assert body["resume_attached"] is True
    assert body["handoff_session"] is not None
    sid = uuid.UUID(body["handoff_session"]["handoff_session_id"])
    assert body["handoff_session"]["manual_submit_armed"] is False

    # Server NEVER received a POST submit automatically
    assert [h for h in hits if h[0] != "GET"] == []

    # Second concurrent/duplicate /execute call is blocked (status is MANUAL_ACTION_REQUIRED != APPROVED)
    assert c.post(f"/api/applications/{app_id}/execute", headers=hdr).status_code == 409

    # Masked PNG artifact retrieval enforces ownership and never leaks storage path
    assert len(body["artifact_ids"]) == 1
    art_id = body["artifact_ids"][0]
    assert str(checkpoint_d_env["storage_root"]) not in exec_resp.text
    png_resp = c.get(f"/api/applications/{app_id}/artifacts/{art_id}", headers=hdr)
    assert png_resp.status_code == 200
    assert png_resp.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert c.get(f"/api/applications/{app_id}/artifacts/{art_id}", headers=_auth_header(u2.id)).status_code == 404

    # Wrong form_action / method binding rejected
    bad_action = c.post(
        f"/api/applications/{app_id}/authorize-manual-submit",
        headers=hdr,
        json={
            "handoff_session_id": str(sid),
            "pending_submission_id": str(uuid.uuid4()),
            "expected_form_action": f"http://127.0.0.1:{port}/tampered",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
        },
    )
    assert bad_action.status_code == 409

    # Premature authorize-manual-submit BEFORE user clicks Submit MUST fail closed (409)
    premature = c.post(
        f"/api/applications/{app_id}/authorize-manual-submit",
        headers=hdr,
        json={
            "handoff_session_id": str(sid),
            "pending_submission_id": str(uuid.uuid4()),
            "expected_form_action": f"http://127.0.0.1:{port}/apply",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
        },
    )
    assert premature.status_code == 409
    assert "No intercepted pending user submission" in premature.json()["detail"]

    # Recording user-reported submission sets USER_REPORTED_SUBMITTED (NEVER CONFIRMED!) and closes handoff
    outcome_resp = c.post(
        f"/api/applications/{app_id}/submit-outcome",
        headers=hdr,
        json={
            "outcome": "submitted_in_browser",
            "handoff_session_id": str(sid),
            "notes": "User clicked submit button and saw thank you page",
        },
    )
    assert outcome_resp.status_code == 200
    assert outcome_resp.json()["status"] == "USER_REPORTED_SUBMITTED"
    assert outcome_resp.json()["confirmed_at"] is None

    # No sensitive PII or authorization_token in logs or AuditLog metadata
    assert synthetic_ssn not in caplog.text
    assert "authorization_token" not in caplog.text
    with TestingSession() as db:
        audits = list(db.scalars(select(AuditLog).where(AuditLog.resource_id == str(app_id))))
        assert len(audits) >= 3
        for a in audits:
            assert synthetic_ssn not in str(a.event_metadata)


def test_checkpoint_d_expired_approval_tamper_pause_and_direct_worker_arm_block(checkpoint_d_env):
    c: TestClient = checkpoint_d_env["client"]
    u1, j1, r1 = checkpoint_d_env["u1"], checkpoint_d_env["j1"], checkpoint_d_env["r1"]
    TestingSession = checkpoint_d_env["TestingSession"]
    port = checkpoint_d_env["fixture_port"]
    svc: ApplicationExecutionService = checkpoint_d_env["svc"]
    hdr = _auth_header(u1.id)

    prep = c.post(
        "/api/applications/prepare",
        headers=hdr,
        json={"job_id": str(j1.id), "resume_id": str(r1.id)},
    )
    app_id = uuid.UUID(prep.json()["application_id"])
    c.post(
        f"/api/applications/{app_id}/answers",
        headers=hdr,
        json={
            "answers": [
                {"question_key": "full_name", "question": "Full Name", "answer": "Chethan Candidate"},
                {"question_key": "email", "question": "Email", "answer": "owner-d@example.com"},
            ]
        },
    )
    c.post(f"/api/applications/{app_id}/approve", headers=hdr)
    exec_resp = c.post(
        f"/api/applications/{app_id}/execute",
        headers=hdr,
        json={"options": {"browser_mode": "visible_handoff"}},
    )
    assert exec_resp.status_code == 200
    sid = uuid.UUID(exec_resp.json()["handoff_session"]["handoff_session_id"])

    # 1. Global pause toggle blocks authorize-manual-submit (403)
    svc.policy_router.settings = svc.settings.model_copy(update={"pause_all_submissions": True})
    paused_resp = c.post(
        f"/api/applications/{app_id}/authorize-manual-submit",
        headers=hdr,
        json={
            "handoff_session_id": str(sid),
            "pending_submission_id": str(uuid.uuid4()),
            "expected_form_action": f"http://127.0.0.1:{port}/apply",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
        },
    )
    assert paused_resp.status_code == 403
    svc.policy_router.settings = svc.settings

    # 2. Direct forged/expired ARM_MANUAL_SUBMIT sent straight to worker socket fails closed
    sess_obj = checkpoint_d_env["mgr"]._sessions[sid]
    sock: socket.socket = sess_obj.ctrl_sock
    buf: bytearray = sess_obj.ctrl_buf
    forged_payload = {
        "cmd": "ARM_MANUAL_SUBMIT",
        "pending_submission_id": str(uuid.uuid4()),
        "authorization": {
            "application_id": str(app_id),
            "handoff_session_id": str(sid),
            "user_id": str(uuid.uuid4()),  # wrong user injected over socket!
            "destination_url": checkpoint_d_env["dest"],
            "form_action": f"http://127.0.0.1:{port}/apply",
            "http_method": "POST",
            "answers_hash": sess_obj.answers_hash,
            "resume_checksum": sess_obj.resume_checksum,
            "approval_snapshot_hash": sess_obj.approval_snapshot_hash,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        },
    }
    import json as _json
    sock.sendall((_json.dumps(forged_payload) + "\n").encode("utf-8"))
    while b"\n" not in buf:
        buf.extend(sock.recv(4096))
    line = bytes(buf[: buf.find(b"\n")])
    del buf[: buf.find(b"\n") + 1]
    assert _json.loads(line.decode("utf-8"))["ok"] is False

    # 3. Answer tampering + expired handoff session + expired approval checks
    with TestingSession() as db:
        ans = db.scalar(
            select(ApplicationAnswer).where(
                ApplicationAnswer.application_id == app_id,
                ApplicationAnswer.question_key == "full_name",
            )
        )
        assert ans is not None
        ans.answer_plaintext = "Tampered Name"
        db.commit()

    tampered = c.post(
        f"/api/applications/{app_id}/authorize-manual-submit",
        headers=hdr,
        json={
            "handoff_session_id": str(sid),
            "pending_submission_id": str(uuid.uuid4()),
            "expected_form_action": f"http://127.0.0.1:{port}/apply",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
        },
    )
    assert tampered.status_code == 409

    # Restore answer and expire the visible handoff session explicitly -> 409
    with TestingSession() as db:
        ans = db.scalar(
            select(ApplicationAnswer).where(
                ApplicationAnswer.application_id == app_id,
                ApplicationAnswer.question_key == "full_name",
            )
        )
        ans.answer_plaintext = "Chethan Candidate"
        db.commit()
    sess_obj.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    expired_sess_auth = c.post(
        f"/api/applications/{app_id}/authorize-manual-submit",
        headers=hdr,
        json={
            "handoff_session_id": str(sid),
            "pending_submission_id": str(uuid.uuid4()),
            "expected_form_action": f"http://127.0.0.1:{port}/apply",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
        },
    )
    assert expired_sess_auth.status_code == 409
    checkpoint_d_env["mgr"].sweep_expired_sessions(now=datetime.now(timezone.utc) + timedelta(minutes=1))

    # 4. Interrupted outcome moves application explicitly to UNVERIFIED without auto-retry
    interrupted = c.post(
        f"/api/applications/{app_id}/submit-outcome",
        headers=hdr,
        json={"outcome": "interrupted", "handoff_session_id": str(sid), "notes": "Network dropped"},
    )
    assert interrupted.status_code == 200
    assert interrupted.json()["status"] == "UNVERIFIED"
    # Cannot approve/retry from UNVERIFIED
    assert c.post(f"/api/applications/{app_id}/approve", headers=hdr).status_code == 409

    # 5. Expired ApprovalRecord blocks execute (insert fresh expired record directly to verify execute guard)
    with TestingSession() as db:
        u_exp, _pe, je, re_obj, _me = _seed_user_job_resume(
            db,
            storage_root=checkpoint_d_env["storage_root"],
            email="expired-appr@example.com",
            dest_url=f"http://127.0.0.1:{port}/apply-form/expired-{uuid.uuid4().hex[:6]}",
        )
    hdr_exp = _auth_header(u_exp.id)
    prep_exp = c.post("/api/applications/prepare", headers=hdr_exp, json={"job_id": str(je.id), "resume_id": str(re_obj.id)})
    app_exp_id = uuid.UUID(prep_exp.json()["application_id"])
    c.post(
        f"/api/applications/{app_exp_id}/answers",
        headers=hdr_exp,
        json={
            "answers": [
                {"question_key": "full_name", "question": "Full Name", "answer": "Chethan Candidate"},
                {"question_key": "email", "question": "Email", "answer": "expired-appr@example.com"},
            ]
        },
    )
    with TestingSession() as db:
        a_exp = db.get(Application, app_exp_id)
        a_exp.status = "APPROVED"
        ans_rows = list(db.scalars(select(ApplicationAnswer).where(ApplicationAnswer.application_id == app_exp_id)))
        from app.schemas.applications import compute_answers_hash, compute_approval_snapshot_hash
        ah = compute_answers_hash([(x.question_key, x.answer_plaintext or "") for x in ans_rows])
        sh = compute_approval_snapshot_hash(
            application_id=app_exp_id,
            user_id=u_exp.id,
            resume_id=re_obj.id,
            resume_checksum=re_obj.sha256_checksum,
            canonical_job_key=a_exp.canonical_job_key,
            destination_url=a_exp.destination_url,
            answers_hash=ah,
            application_mode=a_exp.application_mode,
        )
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        db.add(
            ApprovalRecord(
                application_id=app_exp_id,
                decision="approved",
                actor=f"user:{u_exp.id}",
                resume_id=re_obj.id,
                resume_checksum=re_obj.sha256_checksum,
                destination_url=a_exp.destination_url,
                application_mode=a_exp.application_mode,
                answers_hash=ah,
                approval_snapshot_hash=sh,
                decided_at=old,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
    assert c.post(f"/api/applications/{app_exp_id}/execute", headers=hdr_exp).status_code == 409


def test_checkpoint_d_confirmation_reconciliation_official_api_guard_and_failed_post_fire_rule(checkpoint_d_env):
    c: TestClient = checkpoint_d_env["client"]
    u1, j1, r1 = checkpoint_d_env["u1"], checkpoint_d_env["j1"], checkpoint_d_env["r1"]
    TestingSession = checkpoint_d_env["TestingSession"]
    hdr = _auth_header(u1.id)

    # 1. Prepare + approve + headless execute -> MANUAL_ACTION_REQUIRED
    prep = c.post("/api/applications/prepare", headers=hdr, json={"job_id": str(j1.id), "resume_id": str(r1.id)})
    app_id = uuid.UUID(prep.json()["application_id"])
    c.post(
        f"/api/applications/{app_id}/answers",
        headers=hdr,
        json={
            "answers": [
                {"question_key": "full_name", "question": "Full Name", "answer": "Chethan Candidate"},
                {"question_key": "email", "question": "Email", "answer": "owner-d@example.com"},
            ]
        },
    )
    c.post(f"/api/applications/{app_id}/approve", headers=hdr)
    ex = c.post(
        f"/api/applications/{app_id}/execute",
        headers=hdr,
        json={"options": {"browser_mode": "headless"}},
    )
    assert ex.status_code == 200
    assert ex.json()["status"] == "MANUAL_ACTION_REQUIRED"

    # Simulate user reporting submission in browser
    rep = c.post(
        f"/api/applications/{app_id}/submit-outcome",
        headers=hdr,
        json={"outcome": "submitted_in_browser"},
    )
    assert rep.status_code == 200
    assert rep.json()["status"] == "USER_REPORTED_SUBMITTED"

    # Attempting "failed" after submitted_at is set MUST force UNVERIFIED (not FAILED retry path!)
    # First test reconcile with untrusted/empty verifier -> remains UNVERIFIED (422)
    untrusted = c.post(
        f"/api/applications/{app_id}/reconcile",
        headers=hdr,
        json={"confirmation_evidence": {"source": "browser_success_banner", "message": "Thanks!"}},
    )
    assert untrusted.status_code == 422
    detail = c.get(f"/api/applications/{app_id}", headers=hdr).json()
    assert detail["application"]["status"] == "UNVERIFIED"
    assert detail["application"]["confirmed_at"] is None

    # 2. Register temporary server-side trusted verifier -> forged source still fails, trusted HMAC proof sets CONFIRMED
    class _TrustedTestVerifier:
        verifier_id = "test_verified_receipt_v1"
        supported_modes = frozenset({"human_approved_browser"})

        def verify(self, *, application: Application, evidence: dict, issuer: ConfirmationProofIssuer) -> VerifiedConfirmationProof:
            if evidence.get("receipt_sig") != "valid-dkim-receipt":
                raise ValueError("Untrusted or unverified receipt signature")
            return issuer.issue(
                application=application,
                verification_source="verified_email_confirmation",
                external_ref_id=str(evidence["external_ref_id"]),
                confirmed_at=datetime.now(timezone.utc),
                raw_evidence={"dkim": "pass"},
            )

    policy_mod._CONFIRMATION_VERIFIER_REGISTRY["test_verified_receipt_v1"] = _TrustedTestVerifier()
    try:
        bad_proof = c.post(
            f"/api/applications/{app_id}/reconcile",
            headers=hdr,
            json={"confirmation_evidence": {"receipt_sig": "forged", "external_ref_id": "gh-123"}},
        )
        assert bad_proof.status_code == 422
        assert c.get(f"/api/applications/{app_id}", headers=hdr).json()["application"]["status"] == "UNVERIFIED"

        good_proof = c.post(
            f"/api/applications/{app_id}/reconcile",
            headers=hdr,
            json={"confirmation_evidence": {"receipt_sig": "valid-dkim-receipt", "external_ref_id": "gh-ref-999"}},
        )
        assert good_proof.status_code == 200
        assert good_proof.json()["status"] == "CONFIRMED"
        assert good_proof.json()["external_ref_id"] == "gh-ref-999"
        assert good_proof.json()["confirmed_at"] is not None
    finally:
        policy_mod._CONFIRMATION_VERIFIER_REGISTRY.pop("test_verified_receipt_v1", None)

    # 3. Official API mode remains strictly disabled when no adapter is implemented/registered
    with TestingSession() as db:
        u3, _p3, j3, r3, _m3 = _seed_user_job_resume(
            db,
            storage_root=checkpoint_d_env["storage_root"],
            email="api-disabled@example.com",
            dest_url="https://boards.greenhouse.io/acme/jobs/123",
            connector_type="greenhouse",
            source_config={"submission_api_authorized": True},
        )
    assert (
        c.post(
            "/api/applications/prepare",
            headers=_auth_header(u3.id),
            json={"job_id": str(j3.id), "resume_id": str(r3.id), "preferred_mode": "official_api"},
        ).status_code
        == 400
    )


def test_checkpoint_d_live_postgres_and_redis_integration(tmp_path):
    """Verify Checkpoint D full persistence + LangGraph PostgresSaver + Redis connectivity against live local stack."""
    admin_url = "postgresql://applypilot:applypilot@localhost:5434/postgres"
    db_name = f"applypilot_chk_d_{uuid.uuid4().hex[:8]}"
    target_url = f"postgresql+psycopg://applypilot:applypilot@localhost:5434/{db_name}"
    target_psycopg = f"postgresql://applypilot:applypilot@localhost:5434/{db_name}"

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")

    # Verify live Redis ping alongside PostgreSQL
    r_client = redis_lib.from_url("redis://localhost:6381/0", socket_connect_timeout=3, socket_timeout=3)
    assert r_client.ping() is True
    r_client.close()

    storage_root = tmp_path / "pg_storage"
    storage_root.mkdir()
    backend_dir = Path(__file__).resolve().parents[1]
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", target_url)
    prev_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = target_url
    try:
        command.upgrade(alembic_cfg, "head")
    finally:
        if prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_db_url

    engine = create_engine(target_url, poolclass=NullPool)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    cfg = Settings(
        _env_file=None,
        database_url=target_url,
        encryption_key="live-pg-checkpoint-d-key-secret!!",
        encryption_key_version="v1",
        storage_dir=str(storage_root),
        require_os_egress_firewall=False,
    )
    svc = ApplicationExecutionService(settings=cfg)

    try:
        with SessionLocal() as db:
            user, _prof, job, resume, _map = _seed_user_job_resume(
                db,
                storage_root=storage_root,
                email="livepg-d@example.com",
                dest_url="https://boards.greenhouse.io/acme/jobs/777",
            )
            prep = svc.prepare_application(db, user=user, job_id=job.id, resume_id=resume.id)
            app_id = prep.application_id
            svc.submit_user_answers(
                db,
                user=user,
                application_id=app_id,
                items=[
                    ApplicationAnswerInput(
                        question_key="full_name",
                        question="Full Name",
                        answer="Chethan Candidate",
                    ),
                    ApplicationAnswerInput(
                        question_key="email",
                        question="Email",
                        answer="livepg-d@example.com",
                    ),
                ],
            )
            # Test explicit manual fallback execution with PostgresSaver checkpointer
            db_app = db.get(Application, app_id)
            assert db_app is not None
            db_app.application_mode = "manual"
            db.commit()

            # Reset ApprovalRecord bound to manual mode
            appr = svc.approve_application(db, user=user, application_id=app_id, notes="PG verified")
            assert appr.decision == "approved"
            ex = svc.execute_application(db, user=user, application_id=app_id)
            assert ex.status == "MANUAL_ACTION_REQUIRED"
            assert ex.stopped_before_submit is True

            # Verify PostgresSaver checkpoint tables exist cleanly
            with psycopg.connect(target_psycopg) as pg_conn:
                row = pg_conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s", (f"app-{app_id}",)).fetchone()
                assert row is not None and int(row[0]) >= 1
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f"DROP DATABASE IF EXISTS {db_name}")


def test_checkpoint_d_adversarial_hold_and_confirm_distinguishes_js_from_user_action(tmp_path, monkeypatch):
    """Adversarial audit proving background JS/fetch/XHR/timers/synthetic events/redirects cannot consume authorization."""
    from playwright.sync_api import sync_playwright
    from app.schemas.applications import compute_answers_hash, compute_approval_snapshot_hash
    from app.services.browser_executor import _ManagedHandoffSession, _send_ctrl, _recv_ctrl, Win32JobSupervisor

    storage_root = tmp_path / "adv_storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_DIR", str(storage_root))
    monkeypatch.setenv("ENCRYPTION_KEY", "adv-checkpoint-d-secret-key-32bytes!")
    monkeypatch.setenv("ENCRYPTION_KEY_VERSION", "v1")
    get_settings.cache_clear()

    db_path = tmp_path / "adv_d.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def _override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    server_hits: list[tuple[str, str]] = []

    class _AdversarialHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            server_hits.append(("GET", self.path))
            html = (
                b"<!doctype html><html><body>"
                b"<form id='app-form' method='POST' action='/apply'>"
                b"<input name='full_name' value='Chethan Candidate' />"
                b"<button id='sub-btn' type='submit'>Submit</button>"
                b"</form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            if self.path == "/redir":
                self.send_response(307)
                self.send_header("Location", "/apply")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            server_hits.append(("POST", self.path))
            body = b"accepted"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _AdversarialHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    port = fixture.server_port
    dest = f"http://127.0.0.1:{port}/apply-form"
    form_action = f"http://127.0.0.1:{port}/apply"

    cfg = Settings(
        _env_file=None,
        encryption_key="adv-checkpoint-d-secret-key-32bytes!",
        encryption_key_version="v1",
        storage_dir=str(storage_root),
        require_os_egress_firewall=False,
    )
    mgr = BrowserHandoffManager(storage_root=storage_root)
    svc = ApplicationExecutionService(
        settings=cfg,
        handoff_manager=mgr,
        _allowed_test_origins=frozenset({("127.0.0.1", port)}),
    )
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_application_execution_service] = lambda: svc

    with TestingSession() as db:
        user, _prof, job, resume, _map = _seed_user_job_resume(
            db,
            storage_root=storage_root,
            email="adv-d@example.com",
            dest_url=dest,
        )
        aid = uuid.uuid4()
        app_row = Application(
            id=aid,
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key="greenhouse:adv-1",
            idempotency_key="z" * 64,
            application_mode="human_approved_browser",
            status="MANUAL_ACTION_REQUIRED",
            destination_url=dest,
            thread_id=f"app-{aid}",
        )
        db.add(app_row)
        db.add(
            ApplicationAnswer(
                application_id=aid,
                question_key="full_name",
                question="Full Name",
                is_sensitive=False,
                answer_plaintext="Chethan Candidate",
                user_verified=True,
            )
        )
        ahash = compute_answers_hash([("full_name", "Chethan Candidate")])
        shash = compute_approval_snapshot_hash(
            application_id=aid,
            user_id=user.id,
            resume_id=resume.id,
            resume_checksum=resume.sha256_checksum,
            canonical_job_key=app_row.canonical_job_key,
            destination_url=dest,
            answers_hash=ahash,
            application_mode="human_approved_browser",
        )
        now = datetime.now(timezone.utc)
        db.add(
            ApprovalRecord(
                application_id=aid,
                decision="approved",
                actor=f"user:{user.id}",
                resume_id=resume.id,
                resume_checksum=resume.sha256_checksum,
                destination_url=dest,
                application_mode="human_approved_browser",
                answers_hash=ahash,
                approval_snapshot_hash=shash,
                decided_at=now,
                expires_at=now + timedelta(hours=4),
            )
        )
        db.commit()

    proxy = LocalSSRFEgressProxy(_allowed_test_origins=frozenset({("127.0.0.1", port)}))
    proxy.start()
    srv_ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_ctrl.bind(("127.0.0.1", 0))
    srv_ctrl.listen(1)
    sid = uuid.uuid4()
    stop_pw = threading.Event()
    pw_ready = threading.Event()
    pw_actions: list[str] = []

    # Connect loopback socket simulating supervised worker event loop with live Playwright context
    cli_ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli_ctrl.connect(("127.0.0.1", srv_ctrl.getsockname()[1]))
    parent_ctrl, _ = srv_ctrl.accept()
    parent_ctrl.settimeout(5.0)
    srv_ctrl.close()

    def _playwright_handoff_thread():
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            state.set_live_bindings(answers_hash=ahash, resume_checksum=resume.sha256_checksum, approval_snapshot_hash=shash)
            install_submit_protection(ctx, state=state)
            page = ctx.new_page()
            page.goto(dest)
            state.mark_prefill_complete(final_url=dest, detected_form_action=form_action, detected_http_method="POST")
            server_hits.clear()
            pw_ready.set()

            cli_ctrl.settimeout(0.05)
            cli_buf = bytearray()
            while not stop_pw.is_set():
                page.wait_for_timeout(30)
                if pw_actions:
                    act = pw_actions.pop(0)
                    if act == "JS_ATTACKS":
                        attack_tab = ctx.new_page()
                        attack_tab.goto(dest)
                        try:
                            attack_tab.evaluate(
                                """() => {
                                    try { document.getElementById('app-form').submit(); } catch (_) {}
                                    try { document.getElementById('app-form').requestSubmit(); } catch (_) {}
                                    document.getElementById('app-form').dispatchEvent(new SubmitEvent('submit', {bubbles:true, cancelable:true}));
                                    document.getElementById('sub-btn').click();
                                    fetch('/apply', {method:'POST', body:'js-fetch'}).catch(() => {});
                                    fetch('/apply?evil=1', {method:'POST', body:'js-variant'}).catch(() => {});
                                    fetch('/apply-evil', {method:'POST', body:'js-alt'}).catch(() => {});
                                    fetch('/redir', {method:'POST', body:'js-redir'}).catch(() => {});
                                    const x = new XMLHttpRequest(); x.open('POST', '/apply'); x.send('xhr');
                                    navigator.sendBeacon('/apply', 'beacon');
                                    setTimeout(() => { fetch('/apply', {method:'POST', body:'timer'}).catch(() => {}); }, 10);
                                    window.addEventListener('custom-auto', () => { fetch('/apply', {method:'POST', body:'evt'}).catch(() => {}); });
                                    window.dispatchEvent(new Event('custom-auto'));
                                }"""
                            )
                            attack_tab.wait_for_timeout(80)
                        finally:
                            attack_tab.close()
                    elif act == "USER_CLICK":
                        page.locator("#sub-btn").click(no_wait_after=True)
                    elif act == "REPLAY_CLICK":
                        tab = ctx.new_page()
                        tab.goto(dest)
                        try:
                            tab.locator("#sub-btn").click(no_wait_after=True)
                            tab.wait_for_timeout(100)
                        finally:
                            tab.close()

                try:
                    msg = _recv_ctrl(cli_ctrl, cli_buf)
                except socket.timeout:
                    continue
                except Exception:
                    break
                cmd = msg.get("cmd")
                if cmd == "STATUS":
                    pinfo = state.get_pending_submission_info()
                    _send_ctrl(
                        cli_ctrl,
                        {
                            "ok": True,
                            "connected": True,
                            "blocked_submit_attempts": state.blocked_attempts,
                            "authorized_submitted": state.authorized_submissions,
                            "pending_submission": pinfo.model_dump(mode="json") if pinfo else None,
                        },
                    )
                elif cmd == "ARM_MANUAL_SUBMIT":
                    try:
                        araw = msg["authorization"]
                        from app.services.execution_adapters import ManualSubmitAuthorization
                        auth_obj = ManualSubmitAuthorization(
                            application_id=uuid.UUID(araw["application_id"]),
                            handoff_session_id=uuid.UUID(araw["handoff_session_id"]),
                            user_id=uuid.UUID(araw["user_id"]),
                            destination_url=araw["destination_url"],
                            form_action=araw["form_action"],
                            http_method=araw["http_method"],
                            answers_hash=araw["answers_hash"],
                            resume_checksum=araw["resume_checksum"],
                            approval_snapshot_hash=araw["approval_snapshot_hash"],
                            issued_at=datetime.fromisoformat(araw["issued_at"]),
                            expires_at=datetime.fromisoformat(araw["expires_at"]),
                        )
                        rt = state.release_held_submission(
                            auth=auth_obj,
                            pending_submission_id=uuid.UUID(msg["pending_submission_id"]),
                            live_answers_hash=ahash,
                            live_resume_checksum=resume.sha256_checksum,
                            live_approval_snapshot_hash=shash,
                        )
                        rt.fallback()
                        _send_ctrl(cli_ctrl, {"ok": True})
                    except Exception as exc:
                        _send_ctrl(cli_ctrl, {"ok": False, "error": str(exc)})
                elif cmd == "CLOSE":
                    state.abort_pending_submission()
                    _send_ctrl(cli_ctrl, {"ok": True})
                    break
            ctx.close()
            browser.close()

    worker_thr = threading.Thread(target=_playwright_handoff_thread, daemon=True)
    worker_thr.start()
    assert pw_ready.wait(timeout=15)

    mgr.register_session(
        _ManagedHandoffSession(
            handoff_session_id=sid,
            application_id=aid,
            user_id=user.id,
            destination_url=dest,
            form_action=form_action,
            http_method="POST",
            answers_hash=ahash,
            resume_checksum=resume.sha256_checksum,
            approval_snapshot_hash=shash,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            supervisor=Win32JobSupervisor(),
            process_registry=mgr.process_registry,
            ctrl_sock=parent_ctrl,
            ctrl_buf=bytearray(),
        )
    )
    client = TestClient(app)
    hdr = _auth_header(user.id)

    try:
        # Step 1-2: Fire ALL background/JS/fetch/XHR/timer/synthetic/redirect attacks BEFORE user click
        pw_actions.append("JS_ATTACKS")
        import time
        time.sleep(0.35)
        st_pre = client.get(f"/api/applications/{aid}/handoff/{sid}", headers=hdr).json()
        assert st_pre["pending_submission"] is None
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 3: Genuine user click holds request WITHOUT submitting (isTrusted alone is insufficient!)
        pw_actions.append("USER_CLICK")
        pend_info = None
        for _ in range(25):
            time.sleep(0.05)
            st_hold = client.get(f"/api/applications/{aid}/handoff/{sid}", headers=hdr).json()
            if st_hold["pending_submission"] is not None:
                pend_info = st_hold["pending_submission"]
                break
        assert pend_info is not None
        assert pend_info["target_url"] == form_action
        assert pend_info["http_method"] == "POST"
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 4: While valid submission is HELD, JS/timer/XHR/fetch/redirect attacks CANNOT hijack or consume it!
        pw_actions.append("JS_ATTACKS")
        time.sleep(0.35)
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 5: Wrong pending_submission_id or wrong form_action cannot confirm
        wrong_pid = client.post(
            f"/api/applications/{aid}/authorize-manual-submit",
            headers=hdr,
            json={
                "handoff_session_id": str(sid),
                "pending_submission_id": str(uuid.uuid4()),
                "expected_form_action": form_action,
                "expected_http_method": "POST",
                "expected_payload_sha256": pend_info["payload_sha256"],
            },
        )
        assert wrong_pid.status_code == 409
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 5b: Wrong expected_payload_sha256 cannot confirm
        wrong_sha = client.post(
            f"/api/applications/{aid}/authorize-manual-submit",
            headers=hdr,
            json={
                "handoff_session_id": str(sid),
                "pending_submission_id": pend_info["pending_submission_id"],
                "expected_form_action": form_action,
                "expected_http_method": "POST",
                "expected_payload_sha256": "0" * 64,
            },
        )
        assert wrong_sha.status_code == 409
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 5c: Omitting expected_payload_sha256 fails closed at API boundary (422) without releasing held submit
        missing_sha = client.post(
            f"/api/applications/{aid}/authorize-manual-submit",
            headers=hdr,
            json={
                "handoff_session_id": str(sid),
                "pending_submission_id": pend_info["pending_submission_id"],
                "expected_form_action": form_action,
                "expected_http_method": "POST",
            },
        )
        assert missing_sha.status_code == 422
        assert [h for h in server_hits if h[0] != "GET"] == []

        # Step 6: Concurrent authenticated confirmation on exact pending_submission_id -> 1 succeeds, 3 fail!
        def _race(_i: int) -> int:
            return client.post(
                f"/api/applications/{aid}/authorize-manual-submit",
                headers=hdr,
                json={
                    "handoff_session_id": str(sid),
                    "pending_submission_id": pend_info["pending_submission_id"],
                    "expected_form_action": form_action,
                    "expected_http_method": "POST",
                    "expected_payload_sha256": pend_info["payload_sha256"],
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            race_codes = list(pool.map(_race, range(4)))
        assert race_codes.count(200) == 1
        assert race_codes.count(409) == 3

        time.sleep(0.3)
        assert [h for h in server_hits if h[0] == "POST"] == [("POST", "/apply")]

        # Step 7-8: Replays, new JS attacks, and new click attempts all stay blocked
        assert (
            client.post(
                f"/api/applications/{aid}/authorize-manual-submit",
                headers=hdr,
                json={
                    "handoff_session_id": str(sid),
                    "pending_submission_id": pend_info["pending_submission_id"],
                    "expected_form_action": form_action,
                    "expected_http_method": "POST",
                    "expected_payload_sha256": pend_info["payload_sha256"],
                },
            ).status_code
            == 409
        )
        pw_actions.append("JS_ATTACKS")
        pw_actions.append("REPLAY_CLICK")
        time.sleep(0.4)
        assert [h for h in server_hits if h[0] == "POST"] == [("POST", "/apply")]
    finally:
        stop_pw.set()
        try:
            mgr.close_session(handoff_session_id=sid, user_id=user.id)
        except Exception:
            pass
        cli_ctrl.close()
        worker_thr.join(timeout=5)
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        proxy.stop()
        fixture.shutdown()
        fixture.server_close()


@pytest.mark.parametrize("redirect_status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("cross_origin", [False, True])
def test_checkpoint_d_server_redirect_containment_301_302_303_307_308(redirect_status: int, cross_origin: bool):
    """Server-side HTTP 301/302/303/307/308 redirects (same-origin and cross-origin) MUST NOT be followed."""
    from playwright.sync_api import sync_playwright
    from app.services.browser_executor import forward_held_route_safely
    from app.services.execution_adapters import ManualSubmitAuthorization, SubmitBlockedViolationError

    primary_hits: list[tuple[str, str]] = []
    secondary_hits: list[tuple[str, str]] = []

    class _SecondaryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            secondary_hits.append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"x2")

        def do_POST(self):
            secondary_hits.append(("POST", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"p2")

        def log_message(self, *a):
            return

    sec_srv = ThreadingHTTPServer(("127.0.0.1", 0), _SecondaryHandler)
    threading.Thread(target=sec_srv.serve_forever, daemon=True).start()

    class _PrimaryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            primary_hits.append(("GET", self.path))
            if self.path == "/apply-page":
                html = b"<!doctype html><html><body><form method=POST action=/apply><input name=a value=1><button id=go type=submit>Go</button></form></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            primary_hits.append(("GET_UNEXPECTED", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"no")

        def do_POST(self):
            primary_hits.append(("POST", self.path))
            if self.path == "/apply":
                target = f"http://127.0.0.1:{sec_srv.server_port}/cross-steal" if cross_origin else "/second-submit?auto=1"
                self.send_response(redirect_status)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            return

    pri_srv = ThreadingHTTPServer(("127.0.0.1", 0), _PrimaryHandler)
    threading.Thread(target=pri_srv.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{pri_srv.server_port}/apply-page"
    action = f"http://127.0.0.1:{pri_srv.server_port}/apply"

    allowed = frozenset({("127.0.0.1", pri_srv.server_port), ("127.0.0.1", sec_srv.server_port)})
    proxy = LocalSSRFEgressProxy(_allowed_test_origins=allowed)
    proxy.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            install_submit_protection(ctx, state=state)
            page = ctx.new_page()
            page.goto(dest)
            state.mark_prefill_complete(final_url=dest, detected_form_action=action, detected_http_method="POST")
            primary_hits.clear()

            page.locator("#go").click(no_wait_after=True)
            for _ in range(25):
                page.wait_for_timeout(30)
                if state.get_pending_submission_info() is not None:
                    break
            pend = state.get_pending_submission_info()
            assert pend is not None

            now = datetime.now(timezone.utc)
            auth = ManualSubmitAuthorization(
                application_id=uuid.uuid4(),
                handoff_session_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                destination_url=dest,
                form_action=action,
                http_method="POST",
                answers_hash="a" * 64,
                resume_checksum="b" * 64,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            held_route = state.release_held_submission(
                auth=auth,
                pending_submission_id=pend.pending_submission_id,
                live_answers_hash="a" * 64,
                live_resume_checksum="b" * 64,
                live_approval_snapshot_hash="c" * 64,
                expected_payload_sha256=pend.payload_sha256,
            )
            fwd_ok, redir_code, _safe_loc = forward_held_route_safely(
                held_route,
                state=state,
                egress_proxy=proxy,
            )
            page.wait_for_timeout(200)

            assert fwd_ok is False
            assert redir_code == redirect_status
            assert state.blocked_redirects == 1
            assert auth.consumed is True
            # Redirect destination MUST NOT receive ANY GET or POST!
            assert secondary_hits == []
            assert primary_hits == [("POST", "/apply")]

            # Authorization cannot be replayed after blocked redirect
            with pytest.raises(SubmitBlockedViolationError, match="already consumed"):
                state.release_held_submission(
                    auth=auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            ctx.close()
            browser.close()
    finally:
        proxy.stop()
        pri_srv.shutdown()
        pri_srv.server_close()
        sec_srv.shutdown()
        sec_srv.server_close()


def test_checkpoint_d_held_request_body_integrity_and_csrf_allowlist(tmp_path, caplog):
    """Verify actual wire POST body matches approved answers + PDF + explicit baseline CSRF token only."""
    from playwright.sync_api import sync_playwright
    from app.services._browser_worker import prefill_page_safely
    from app.services.browser_executor import forward_held_route_safely
    from app.services.execution_adapters import ManualSubmitAuthorization, SubmitBlockedViolationError

    synthetic_ssn = "SYNTHETIC-SSN-BODY-8877"
    csrf_val = "baseline-csrf-xyz-99"
    received_posts: list[bytes] = []

    class _FormHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = (
                b"<!doctype html><html><body>"
                b"<form id='app-form' method='POST' action='/apply' enctype='multipart/form-data'>"
                b"<input type='hidden' name='csrf_token' value='" + csrf_val.encode() + b"' />"
                b"<label>Full Name <input name='full_name' required /></label>"
                b"<label>National ID <input name='national_id' /></label>"
                b"<label>Experience Tier <select name='experience_tier'><option value='Senior'>Senior</option><option value='Junior'>Junior</option></select></label>"
                b"<label>Resume <input type='file' name='resume' /></label>"
                b"<button id='sub' type='submit'>Submit</button>"
                b"</form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or "0")
            received_posts.append(self.rfile.read(ln))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FormHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{srv.server_port}/form"
    action = f"http://127.0.0.1:{srv.server_port}/apply"
    proxy = LocalSSRFEgressProxy(_allowed_test_origins=frozenset({("127.0.0.1", srv.server_port)}))
    proxy.start()
    resume_bytes = b"%PDF-1.4\n%verified-body-integrity\n%%EOF\n"
    resume_sha = hashlib.sha256(resume_bytes).hexdigest()

    try:
        caplog.clear()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            install_submit_protection(ctx, state=state)

            def _run_tampered_attempt(js_mutator: str) -> None:
                t = ctx.new_page()
                prefill_page_safely(
                    page=t,
                    destination_url=dest,
                    verified_resume_bytes=resume_bytes,
                    verified_resume_filename="tailored.pdf",
                    expected_resume_checksum=resume_sha,
                    verified_answers={
                        "full_name": "Chethan Candidate",
                        "national_id": synthetic_ssn,
                        "experience_tier": "Senior",
                    },
                    verified_keys={"full_name", "national_id", "experience_tier"},
                    unverified_keys=set(),
                    sensitive_unconfirmed_keys=set(),
                    submit_state=state,
                    egress_proxy=proxy,
                    egress_timeout_pause_ms=0,
                )
                t.evaluate(js_mutator)
                t.locator("#sub").click(no_wait_after=True)
                t.wait_for_timeout(120)
                try:
                    assert state.get_pending_submission_info() is None
                finally:
                    t.close()

            # All adversarial DOM/body mutations MUST be rejected BEFORE hold!
            _run_tampered_attempt("() => { document.querySelector('[name=full_name]').value = 'Evil'; }")
            _run_tampered_attempt("() => { document.querySelector('[name=national_id]').value = 'TAMPERED-SSN'; }")
            _run_tampered_attempt("() => { document.querySelector('[name=experience_tier]').value = 'Junior'; }")
            _run_tampered_attempt("() => { document.querySelector('[name=csrf_token]').value = 'forged-csrf'; }")
            _run_tampered_attempt(
                "() => { const i = document.createElement('input'); i.type='hidden'; i.name='injected_hidden'; i.value='1'; document.getElementById('app-form').appendChild(i); }"
            )
            _run_tampered_attempt("() => { document.getElementById('app-form').action = '/apply?evil=1'; }")
            _run_tampered_attempt("() => { document.getElementById('app-form').method = 'GET'; }")
            _run_tampered_attempt(
                "() => { const dt = new DataTransfer(); dt.items.add(new File(['evil'], 'bad.pdf', {type:'application/pdf'})); document.querySelector('[name=resume]').files = dt.files; }"
            )
            assert received_posts == []

            # Clean prefilled submission WITH approved answers + PDF + unmodified baseline csrf_token holds cleanly
            clean_page = ctx.new_page()
            prefill_page_safely(
                page=clean_page,
                destination_url=dest,
                verified_resume_bytes=resume_bytes,
                verified_resume_filename="tailored.pdf",
                expected_resume_checksum=resume_sha,
                verified_answers={
                    "full_name": "Chethan Candidate",
                    "national_id": synthetic_ssn,
                    "experience_tier": "Senior",
                },
                verified_keys={"full_name", "national_id", "experience_tier"},
                unverified_keys=set(),
                sensitive_unconfirmed_keys=set(),
                submit_state=state,
                egress_proxy=proxy,
                egress_timeout_pause_ms=0,
            )
            clean_page.locator("#sub").click(no_wait_after=True)
            for _ in range(25):
                clean_page.wait_for_timeout(30)
                if state.get_pending_submission_info() is not None:
                    break
            pend = state.get_pending_submission_info()
            assert pend is not None
            assert received_posts == []

            now = datetime.now(timezone.utc)
            auth = ManualSubmitAuthorization(
                application_id=uuid.uuid4(),
                handoff_session_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                destination_url=dest,
                form_action=action,
                http_method="POST",
                answers_hash="a" * 64,
                resume_checksum=resume_sha,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            # Mismatched payload digest MUST fail closed
            with pytest.raises(SubmitBlockedViolationError, match="payload_sha256"):
                state.release_held_submission(
                    auth=auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum=resume_sha,
                    live_approval_snapshot_hash="c" * 64,
                    expected_payload_sha256="f" * 64,
                )
            assert auth.consumed is False

            # Matching payload digest releases cleanly and delivers intact verified payload
            held_route = state.release_held_submission(
                auth=auth,
                pending_submission_id=pend.pending_submission_id,
                live_answers_hash="a" * 64,
                live_resume_checksum=resume_sha,
                live_approval_snapshot_hash="c" * 64,
                expected_payload_sha256=pend.payload_sha256,
            )
            ok_fwd, status_code, _ = forward_held_route_safely(held_route, state=state, egress_proxy=proxy)
            clean_page.wait_for_timeout(150)
            assert ok_fwd is True
            assert status_code == 200
            assert len(received_posts) == 1
            assert synthetic_ssn.encode() in received_posts[0]
            # Sensitive PII MUST NOT leak into logs or pending info!
            assert synthetic_ssn not in caplog.text
            assert synthetic_ssn not in pend.model_dump_json()

            ctx.close()
            browser.close()
    finally:
        proxy.stop()
        srv.shutdown()
        srv.server_close()

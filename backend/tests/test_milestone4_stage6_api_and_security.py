from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid
import fitz
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import create_access_token
from app.database.base import Base
from app.database.models import (
    AuditLog,
    AutoTailorDailyQuota,
    CandidateProfile,
    CandidateSkill,
    Experience,
    Job,
    JobMatch,
    JobRequirement,
    JobSource,
    Project,
    Resume,
    ResumeJobMapping,
    User,
)
from app.database.session import get_db
from app.main import app
from app.services.resume_intelligence_service import (
    approve_tailored_resume,
    reject_tailored_resume,
    require_approved_tailored_resume_for_submission,
)
from app.services.resume_vault_service import compute_sha256, store_resume_bytes
from app.workers.resume_tasks import execute_resume_generation_job


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()

    db_path = tmp_path / "stage6_api.db"
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

    app.dependency_overrides[get_db] = _override_db
    # Prevent SQLite tests from hitting live Redis if running locally
    monkeypatch.setattr("app.services.outbox_service.dispatch_outbox_job_to_broker", lambda *_a, **_kw: True)

    with TestingSession() as db:
        u1 = User(email="owner@example.com", hashed_password="h")
        u2 = User(email="other@example.com", hashed_password="h")
        db.add_all([u1, u2])
        db.flush()

        p1 = CandidateProfile(
            user_id=u1.id,
            full_name="Chethan Gowda",
            email="chethan.private@example.com",
            phone="+91-9999999999",
            location="Bengaluru, India",
            summary="Backend & AI Engineer experienced in Python, FastAPI, and LangGraph.",
            education=[{"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"}],
            achievements=["SIH Finalist 2025"],
        )
        p2 = CandidateProfile(
            user_id=u2.id,
            full_name="Other Candidate",
            email="other.private@example.com",
        )
        db.add_all([p1, p2])
        db.flush()

        db.add_all(
            [
                CandidateSkill(profile_id=p1.id, skill_name="Python", category="Languages", verified=True),
                CandidateSkill(profile_id=p1.id, skill_name="LangGraph", category="AI", verified=True),
                CandidateSkill(profile_id=p1.id, skill_name="FastAPI", category="Backend", verified=True),
            ]
        )
        db.add(
            Project(
                profile_id=p1.id,
                name="ORBIT",
                summary="Multi-agent workflow orchestrator built with Python and LangGraph",
                technologies=["Python", "LangGraph", "FastAPI"],
                implemented_features=["Built checkpointed state graph for deterministic workflows"],
                verified_metrics=["Processed 500 workflows cleanly"],
            )
        )
        db.add(
            Experience(
                profile_id=p1.id,
                company="Acme AI",
                role="Backend Intern",
                start_date="2025-05",
                end_date="2025-08",
                verified_responsibilities=["Implemented FastAPI services"],
            )
        )

        m_bytes = _make_pdf_bytes("SUMMARY\nPython\nSKILLS\nPython, FastAPI\n")
        m_id = uuid.uuid4()
        m_path = store_resume_bytes(
            storage_root=tmp_path,
            user_id=u1.id,
            resume_id=m_id,
            safe_filename="master.pdf",
            is_master=True,
            data=m_bytes,
        )
        master_res = Resume(
            id=m_id,
            profile_id=p1.id,
            is_master=True,
            original_filename="master.pdf",
            content_type="application/pdf",
            file_size_bytes=len(m_bytes),
            version_label="master-v1",
            pdf_path=str(m_path),
            sha256_checksum=compute_sha256(m_bytes),
            validation_passed=True,
        )
        db.add(master_res)

        src = JobSource(source_name="gh-stage6", connector_type="greenhouse")
        db.add(src)
        db.flush()

        eligible_job = Job(
            source_id=src.id,
            source_job_id="601",
            canonical_url="https://boards.greenhouse.io/s6/jobs/601",
            dedup_key="a" * 64,
            content_checksum="b" * 64,
            company="Stage6AI",
            role_title="Applied AI Engineer",
            location="Bengaluru, India",
            raw_jd="Python, LangGraph, FastAPI. 0-1 yrs.",
        )
        ineligible_job = Job(
            source_id=src.id,
            source_job_id="602",
            canonical_url="https://boards.greenhouse.io/s6/jobs/602",
            dedup_key="c" * 64,
            content_checksum="d" * 64,
            company="LegacyCorp",
            role_title="Staff Architect",
            location="Bengaluru, India",
            raw_jd="Requires 8+ years experience.",
        )
        db.add_all([eligible_job, ineligible_job])
        db.flush()

        db.add(
            JobRequirement(
                job_id=eligible_job.id,
                role_category="Applied AI Engineer",
                required_skills=["Python", "LangGraph", "FastAPI"],
                technical_stack=["Python", "LangGraph", "FastAPI"],
                experience_min=0.0,
                experience_max=1.0,
            )
        )
        db.add(
            JobRequirement(
                job_id=ineligible_job.id,
                role_category="Staff Architect",
                required_skills=["Python"],
                experience_min=8.0,
            )
        )
        db.add(
            JobMatch(
                job_id=ineligible_job.id,
                profile_id=p1.id,
                score=20.0,
                eligible=False,
                hard_constraints_passed=False,
                eligibility_blockers=["experience_exceeds_fresher_window"],
                recommendation="skip",
                explanation="Requires 8+ yrs.",
            )
        )
        db.commit()

        tok1, _ = create_access_token(str(u1.id))
        tok2, _ = create_access_token(str(u2.id))
        yield {
            "client": TestClient(app),
            "factory": TestingSession,
            "tmp_path": tmp_path,
            "u1": u1,
            "u2": u2,
            "p1": p1,
            "master_res": master_res,
            "eligible_job": eligible_job,
            "ineligible_job": ineligible_job,
            "h1": {"Authorization": f"Bearer {tok1}"},
            "h2": {"Authorization": f"Bearer {tok2}"},
        }

    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def test_stage6_full_tailor_retrieve_diff_validation_download_approve_and_regenerate_flow(api_env):
    client: TestClient = api_env["client"]
    factory = api_env["factory"]
    tmp_path: Path = api_env["tmp_path"]
    h1 = api_env["h1"]
    h2 = api_env["h2"]
    job = api_env["eligible_job"]
    ineligible_job = api_env["ineligible_job"]
    master_res = api_env["master_res"]

    # 1. Ineligible job blocked from tailoring
    r_inel = client.post(f"/api/jobs/{ineligible_job.id}/tailor", headers=h1, json={"use_llm": False})
    assert r_inel.status_code == 400

    # 2. Enqueue manual tailoring -> 202 Accepted, NO auto quota consumed
    r_tailor = client.post(
        f"/api/jobs/{job.id}/tailor",
        headers=h1,
        json={"use_llm": False, "force_regenerate": False},
    )
    assert r_tailor.status_code == 202
    body1 = r_tailor.json()
    assert body1["created"] is True
    assert body1["status"] == "queued"
    gen_id = uuid.UUID(body1["generation_job_id"])

    with factory() as db:
        assert db.scalar(select(AutoTailorDailyQuota)) is None

    # 3. Duplicate non-forced request reuses existing logical request (200 OK, idempotency_hit=True)
    r_dup = client.post(
        f"/api/jobs/{job.id}/tailor",
        headers=h1,
        json={"use_llm": False, "force_regenerate": False},
    )
    assert r_dup.status_code == 200
    assert r_dup.json()["created"] is False
    assert r_dup.json()["idempotency_hit"] is True
    assert r_dup.json()["generation_job_id"] == str(gen_id)

    # Check history while queued
    r_hist_q = client.get(f"/api/jobs/{job.id}/resumes/history", headers=h1)
    assert r_hist_q.status_code == 200
    assert len(r_hist_q.json()["outbox_jobs"]) == 1
    assert r_hist_q.json()["outbox_jobs"][0]["status"] == "queued"

    # 4. Execute worker -> produces completed validated tailored resume v1 (pending_review)
    w_res = execute_resume_generation_job(
        gen_id,
        worker_id="api-test-w1",
        session_factory=factory,
        use_llm=False,
        storage_dir=tmp_path,
    )
    assert w_res["status"] == "completed"
    v1_id = uuid.UUID(w_res["resume_id"])

    # 5. Retrieval endpoints
    r_list = client.get("/api/resumes/tailored", headers=h1)
    assert r_list.status_code == 200 and len(r_list.json()) == 1
    assert r_list.json()[0]["resume_id"] == str(v1_id)
    assert r_list.json()[0]["approval_status"] == "pending_review"
    assert r_list.json()[0]["version_number"] == 1

    r_detail = client.get(f"/api/resumes/{v1_id}/tailored", headers=h1)
    assert r_detail.status_code == 200
    assert r_detail.json()["validation_passed"] is True
    # Ensure no raw PII leaked in serialized detail response
    assert "chethan.private@example.com" not in r_detail.text
    assert "+91-9999999999" not in r_detail.text

    r_diff = client.get(f"/api/resumes/{v1_id}/diff", headers=h1)
    assert r_diff.status_code == 200
    assert "skills_reordered" in r_diff.json()["tailoring_diff"]

    r_val = client.get(f"/api/resumes/{v1_id}/validation", headers=h1)
    assert r_val.status_code == 200
    assert r_val.json()["validation_passed"] is True

    r_hist = client.get(f"/api/jobs/{job.id}/resumes/history", headers=h1)
    assert r_hist.status_code == 200
    assert len(r_hist.json()["versions"]) == 1
    assert r_hist.json()["versions"][0]["approval_status"] == "pending_review"

    # 6. Download before approval: approved_only=true blocked (403); normal download passes + checksum matches
    assert client.get(f"/api/resumes/{v1_id}/download?approved_only=true", headers=h1).status_code == 403
    dl1 = client.get(f"/api/resumes/{v1_id}/download", headers=h1)
    assert dl1.status_code == 200
    assert dl1.headers["content-type"] == "application/pdf"
    assert compute_sha256(dl1.content) == r_detail.json()["sha256_checksum"]

    # Server-side submission gate MUST fail before approval regardless of client flags
    with factory() as db:
        with pytest.raises(HTTPException) as exc_unapproved:
            require_approved_tailored_resume_for_submission(
                db, user=api_env["u1"], job_id=job.id, resume_id=v1_id
            )
        assert exc_unapproved.value.status_code == 403

    # 7. Cross-user access -> strictly 404 across ALL endpoints
    assert client.get("/api/resumes/tailored", headers=h2).json() == []
    assert client.get(f"/api/resumes/{v1_id}/tailored", headers=h2).status_code == 404
    assert client.get(f"/api/resumes/{v1_id}/diff", headers=h2).status_code == 404
    assert client.get(f"/api/resumes/{v1_id}/validation", headers=h2).status_code == 404
    assert client.get(f"/api/resumes/{v1_id}/download", headers=h2).status_code == 404
    assert client.post(f"/api/resumes/{v1_id}/approve", headers=h2).status_code == 404
    assert client.post(f"/api/resumes/{v1_id}/reject", headers=h2).status_code == 404
    assert client.post(f"/api/resumes/{v1_id}/regenerate", headers=h2).status_code == 404

    # 8. Master resume CANNOT be regenerated, approved, or rejected
    assert client.post(f"/api/resumes/{master_res.id}/regenerate", headers=h1).status_code == 400
    assert client.post(f"/api/resumes/{master_res.id}/approve", headers=h1).status_code == 400
    assert client.post(f"/api/resumes/{master_res.id}/reject", headers=h1).status_code == 400

    # 9. Owner Approval & Idempotency
    r_app1 = client.post(f"/api/resumes/{v1_id}/approve", headers=h1, json={"notes": "Looks accurate"})
    assert r_app1.status_code == 200
    assert r_app1.json()["approval_status"] == "approved"
    assert r_app1.json()["approved_at"] is not None
    assert r_app1.json()["rejected_at"] is None
    assert r_app1.json()["idempotent"] is False

    # Repeated approval is idempotent
    r_app2 = client.post(f"/api/resumes/{v1_id}/approve", headers=h1)
    assert r_app2.status_code == 200
    assert r_app2.json()["idempotent"] is True
    assert r_app2.json()["approved_at"] == r_app1.json()["approved_at"]

    # Rejecting an already-approved version is blocked (409) to prevent contradictory states
    assert client.post(f"/api/resumes/{v1_id}/reject", headers=h1).status_code == 409

    # Download with approved_only=true + downstream submission check now succeed
    assert client.get(f"/api/resumes/{v1_id}/download?approved_only=true", headers=h1).status_code == 200
    with factory() as db:
        res_ok, map_ok = require_approved_tailored_resume_for_submission(
            db, user=api_env["u1"], job_id=job.id, resume_id=v1_id
        )
        assert res_ok.id == v1_id and map_ok.approval_status == "approved"

    # 10. Explicit Regenerate creates new outbox job & v2 without affecting v1 until v2 completes
    r_regen = client.post(f"/api/resumes/{v1_id}/regenerate", headers=h1)
    assert r_regen.status_code == 202
    assert r_regen.json()["created"] is True
    gen2_id = uuid.UUID(r_regen.json()["generation_job_id"])
    assert gen2_id != gen_id

    w_res2 = execute_resume_generation_job(
        gen2_id, worker_id="api-test-w2", session_factory=factory, use_llm=False, storage_dir=tmp_path
    )
    assert w_res2["status"] == "completed"
    v2_id = uuid.UUID(w_res2["resume_id"])
    assert v2_id != v1_id

    # v2 starts in pending_review and can be rejected cleanly without modifying PDF
    r_rej = client.post(f"/api/resumes/{v2_id}/reject", headers=h1, json={"notes": "Want tighter summary"})
    assert r_rej.status_code == 200
    assert r_rej.json()["approval_status"] == "rejected"
    assert r_rej.json()["rejected_at"] is not None
    assert r_rej.json()["approved_at"] is None

    # Approving rejected v2 is blocked (409)
    assert client.post(f"/api/resumes/{v2_id}/approve", headers=h1).status_code == 409

    # Audit logs must NEVER contain raw resume text, phone, email, or secret keys
    with factory() as db:
        logs = list(db.scalars(select(AuditLog).where(AuditLog.user_id == api_env["u1"].id)).all())
        assert logs
        for entry in logs:
            serialized = str(entry.event_metadata)
            assert "chethan.private@example.com" not in serialized
            assert "+91-9999999999" not in serialized


def test_failed_and_tampered_resumes_blocked_from_approval_and_download(api_env):
    client: TestClient = api_env["client"]
    factory = api_env["factory"]
    tmp_path: Path = api_env["tmp_path"]
    p1 = api_env["p1"]
    job = api_env["eligible_job"]
    h1 = api_env["h1"]

    with factory() as db:
        # 1. Validation-failed resume
        failed_res = Resume(
            profile_id=p1.id,
            is_master=False,
            original_filename="failed_v1.pdf",
            content_type="application/pdf",
            file_size_bytes=0,
            version_label="tailored-v9-failed",
            generation_status="validation_failed",
            sha256_checksum="0" * 64,
            validation_passed=False,
            validation_report={"validation_passed": False, "unsupported_count": 1},
        )
        db.add(failed_res)
        db.flush()
        failed_map = ResumeJobMapping(
            resume_id=failed_res.id,
            job_id=job.id,
            profile_id=p1.id,
            run_id="res-failed-stage6",
            trigger_mode="manual",
            version_number=9,
            is_active=False,
            evidence_fingerprint="e" * 64,
            status="validation_failed",
            approval_status="pending_review",
        )
        db.add(failed_map)

        # 2. Completed resume whose file checksum is later corrupted on disk
        ok_bytes = _make_pdf_bytes("Valid Tailored Content")
        c_id = uuid.uuid4()
        c_path = store_resume_bytes(
            storage_root=tmp_path,
            user_id=api_env["u1"].id,
            resume_id=c_id,
            safe_filename="tampered.pdf",
            is_master=False,
            data=ok_bytes,
        )
        corrupt_res = Resume(
            id=c_id,
            profile_id=p1.id,
            is_master=False,
            original_filename="tampered.pdf",
            content_type="application/pdf",
            file_size_bytes=len(ok_bytes),
            version_label="tailored-v10",
            pdf_path=str(c_path),
            sha256_checksum=compute_sha256(ok_bytes),
            generation_status="completed",
            validation_passed=True,
        )
        db.add(corrupt_res)
        db.flush()
        corrupt_map = ResumeJobMapping(
            resume_id=corrupt_res.id,
            job_id=job.id,
            profile_id=p1.id,
            run_id="res-corrupt-stage6",
            trigger_mode="manual",
            version_number=10,
            is_active=True,
            evidence_fingerprint="f" * 64,
            status="completed",
            approval_status="pending_review",
        )
        db.add(corrupt_map)
        db.commit()

    # Validation report accessible on failed resume, but approval & download strictly blocked (409)
    assert client.get(f"/api/resumes/{failed_res.id}/validation", headers=h1).status_code == 200
    assert client.post(f"/api/resumes/{failed_res.id}/approve", headers=h1).status_code == 409
    assert client.get(f"/api/resumes/{failed_res.id}/download", headers=h1).status_code == 409

    # Tamper bytes on disk -> SHA-256 integrity guard blocks download (409)
    Path(c_path).write_bytes(b"%PDF-corrupted-bytes")
    assert client.get(f"/api/resumes/{corrupt_res.id}/download", headers=h1).status_code == 409


def test_concurrent_approve_reject_prevention_of_contradictory_state(api_env):
    client: TestClient = api_env["client"]
    factory = api_env["factory"]
    tmp_path: Path = api_env["tmp_path"]
    job = api_env["eligible_job"]
    h1 = api_env["h1"]

    r = client.post(f"/api/jobs/{job.id}/tailor", headers=h1, json={"force_regenerate": False})
    gen_id = uuid.UUID(r.json()["generation_job_id"])
    w_res = execute_resume_generation_job(
        gen_id, worker_id="conc-w1", session_factory=factory, use_llm=False, storage_dir=tmp_path
    )
    res_id = uuid.UUID(w_res["resume_id"])

    def _call(action: str) -> int:
        with factory() as db:
            try:
                if action == "approve":
                    approve_tailored_resume(db, user=api_env["u1"], resume_id=res_id, notes="ok")
                else:
                    reject_tailored_resume(db, user=api_env["u1"], resume_id=res_id, notes="no")
                return 200
            except HTTPException as exc:
                return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(_call, ["approve", "reject", "approve", "reject"]))

    assert 200 in statuses
    with factory() as db:
        mp = db.scalar(select(ResumeJobMapping).where(ResumeJobMapping.resume_id == res_id))
        assert mp is not None
        assert mp.approval_status in ("approved", "rejected")
        if mp.approval_status == "approved":
            assert mp.approved_at is not None and mp.rejected_at is None
        else:
            assert mp.rejected_at is not None and mp.approved_at is None

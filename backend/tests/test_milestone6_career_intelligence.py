from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.security import create_access_token
from app.database.models import (
    Application,
    CandidateProfile,
    CandidateSkill,
    Job,
    JobRequirement,
    JobSource,
    Project,
    Resume,
    User,
)
from app.database.session import get_db
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _hdr(uid: uuid.UUID) -> dict[str, str]:
    tok, _ = create_access_token(str(uid))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def career_env(tmp_path):
    db_file = tmp_path / "m6_career.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(cfg, "0007_milestone_6_career")

    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    now = datetime.now(timezone.utc)

    with TestingSession() as db:
        u1 = User(email="career-u1@example.com", hashed_password="hashed-password")
        u2 = User(email="career-u2@example.com", hashed_password="hashed-password")
        db.add_all([u1, u2])
        db.flush()

        p1 = CandidateProfile(user_id=u1.id, full_name="Career Candidate", email=u1.email)
        db.add(p1)
        db.flush()

        db.add(CandidateSkill(profile_id=p1.id, skill_name="FastAPI", verified=True))
        db.add(
            Project(
                profile_id=p1.id,
                name="ApplyPilot Core",
                summary="Built durable PostgreSQL outbox and FastAPI services.",
                technologies=["Python", "FastAPI", "PostgreSQL"],
            )
        )
        src = JobSource(source_name="gh-career", connector_type="greenhouse")
        db.add(src)
        db.flush()

        job = Job(
            source_id=src.id,
            source_job_id="JOB-900",
            canonical_url="https://boards.greenhouse.io/career/jobs/900",
            dedup_key="a" * 64,
            content_checksum="b" * 64,
            company="CareerCorp",
            role_title="Backend AI Engineer",
            raw_jd="Python, FastAPI, PostgreSQL",
        )
        db.add(job)
        db.flush()

        db.add(
            JobRequirement(
                job_id=job.id,
                role_category="Backend",
                required_skills=["Python", "FastAPI", "PostgreSQL"],
                preferred_skills=["Redis"],
            )
        )
        res = Resume(
            profile_id=p1.id,
            is_master=False,
            original_filename="tailored.pdf",
            content_type="application/pdf",
            file_size_bytes=512,
            version_label="v1",
            sha256_checksum="f" * 64,
        )
        db.add(res)
        db.flush()

        app_row = Application(
            user_id=u1.id,
            job_id=job.id,
            resume_id=res.id,
            canonical_job_key="c" * 64,
            idempotency_key="d" * 64,
            status="CONFIRMED",
            destination_url=job.canonical_url,
            thread_id="thr-career",
            confirmed_at=now - timedelta(days=8),
        )
        db.add(app_row)
        db.commit()
        for obj in (u1, u2, app_row):
            db.refresh(obj)

    def _override_db():
        with TestingSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            yield {"client": client, "u1": u1, "u2": u2, "app": app_row}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_followups_interview_kit_mock_and_memory_end_to_end(career_env):
    c: TestClient = career_env["client"]
    u1 = career_env["u1"]
    u2 = career_env["u2"]
    app_row = career_env["app"]
    h1 = _hdr(u1.id)
    h2 = _hdr(u2.id)

    # 1. Follow-up creation (idempotent), snooze, edit draft, complete
    fu1 = c.post("/api/followups", headers=h1, json={"application_id": str(app_row.id), "interval_days": 7})
    assert fu1.status_code == 201
    fu_id = fu1.json()["id"]
    assert "CareerCorp" in fu1.json()["draft_subject"]

    # Cross-user blocked
    assert c.patch(f"/api/followups/{fu_id}", headers=h2, json={"snooze_days": 3}).status_code == 404

    fu_upd = c.patch(f"/api/followups/{fu_id}", headers=h1, json={"snooze_days": 3, "draft_subject": "Custom Subject"})
    assert fu_upd.status_code == 200
    assert fu_upd.json()["status"] == "snoozed"
    assert fu_upd.json()["draft_subject"] == "Custom Subject"

    fu_done = c.post(f"/api/followups/{fu_id}/complete", headers=h1)
    assert fu_done.status_code == 200
    assert fu_done.json()["status"] == "completed"

    # 2. 8-module Interview Kit grounded in resume checksum + export + progress
    kit_resp = c.post("/api/interviews/kits/generate", headers=h1, json={"application_id": str(app_row.id)})
    assert kit_resp.status_code == 201
    kit = kit_resp.json()
    assert kit["resume_checksum"] == "f" * 64
    assert len(kit["modules_json"]) == 8
    assert kit["modules_json"]["company_role_research"]["verified_facts"]

    # Cross-user kit blocked
    assert c.get(f"/api/interviews/kits/{kit['id']}", headers=h2).status_code == 404

    prog = c.patch(
        f"/api/interviews/kits/{kit['id']}/progress",
        headers=h1,
        json={"completed_topics": ["cs_fundamentals"], "user_feedback": "Helpful"},
    )
    assert prog.status_code == 200
    assert prog.json()["readiness_estimate"] > 35.0
    assert c.get(f"/api/interviews/kits/{kit['id']}/export", headers=h1).status_code == 200

    # 3. Adaptive Mock Interview + Memory updates + User-controlled Memory CRUD
    sess = c.post(
        "/api/interviews/sessions/start",
        headers=h1,
        json={"kit_id": kit["id"], "round_type": "mixed", "difficulty": "medium"},
    )
    assert sess.status_code == 201
    sid = sess.json()["id"]

    ans1 = c.post(
        f"/api/interviews/sessions/{sid}/answer",
        headers=h1,
        json={"answer": "Because we use PostgreSQL transactions and Redis locks to prevent race conditions, the trade-off is latency vs consistency."},
    )
    assert ans1.status_code == 200
    assert len(ans1.json()["attempts"]) == 1

    done = c.post(f"/api/interviews/sessions/{sid}/complete", headers=h1)
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["overall_score"] is not None

    mems = c.get("/api/interviews/memory", headers=h1).json()
    assert len(mems) >= 1
    assert c.get("/api/interviews/memory", headers=h2).json() == []

    # User-controlled create/edit/delete memory
    m_create = c.post(
        "/api/interviews/memory",
        headers=h1,
        json={"memory_type": "behavioral_example", "topic": "Ownership", "summary": "Led migration audit", "confidence": 0.9},
    )
    assert m_create.status_code == 201
    mid = m_create.json()["id"]
    assert c.patch(f"/api/interviews/memory/{mid}", headers=h1, json={"summary": "Updated summary"}).status_code == 200
    assert c.delete(f"/api/interviews/memory/{mid}", headers=h1).status_code == 204

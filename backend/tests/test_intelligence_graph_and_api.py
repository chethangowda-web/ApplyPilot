from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import Job, JobSource, WorkflowRun
from app.database.session import get_db
from app.graphs.intelligence_graph import run_job_intelligence_workflow
from app.main import app


@pytest.fixture()
def intel_env():
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
        yield tc, TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_user_and_job(client: TestClient, SessionLocal) -> tuple[dict[str, str], dict[str, str], str]:
    client.post("/api/auth/register", json={"email": "alice_m3@example.com", "password": "StrongPassword123!"})
    tok = client.post("/api/auth/login", json={"email": "alice_m3@example.com", "password": "StrongPassword123!"})
    alice_h = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    client.post(
        "/api/profile",
        headers=alice_h,
        json={
            "full_name": "Alice AI",
            "email": "alice_m3@example.com",
            "location": "Bengaluru, India",
            "education": [{"institution": "Presidency", "degree": "B.Tech", "field_of_study": "CSE"}],
        },
    )
    for sk in ("Python", "FastAPI", "LangGraph", "PostgreSQL"):
        client.post("/api/profile/skills", headers=alice_h, json={"skill_name": sk, "category": "AI", "evidence_ref": "ORBIT"})
    client.post(
        "/api/profile/projects",
        headers=alice_h,
        json={
            "name": "ORBIT",
            "summary": "Multi-agent system with LangGraph and FastAPI.",
            "technologies": ["Python", "FastAPI", "LangGraph", "PostgreSQL"],
            "implemented_features": ["LangGraph supervisor"],
            "planned_features": ["MCP tools"],
        },
    )

    client.post("/api/auth/register", json={"email": "bob_m3@example.com", "password": "StrongPassword123!"})
    tok2 = client.post("/api/auth/login", json={"email": "bob_m3@example.com", "password": "StrongPassword123!"})
    bob_h = {"Authorization": f"Bearer {tok2.json()['access_token']}"}

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        src = JobSource(source_name="gh-m3", connector_type="greenhouse", config={"board_token": "m3"})
        db.add(src)
        db.flush()
        job = Job(
            source_id=src.id,
            source_job_id="m3-1",
            canonical_url="https://boards.greenhouse.io/m3/jobs/1",
            dedup_key="m3k1",
            content_checksum="m3c1",
            company="Discover AI",
            role_title="AI Engineer Intern",
            location="Bengaluru, India",
            remote_policy="hybrid",
            is_remote=False,
            employment_type="internship",
            raw_jd=(
                "Responsibilities:\n- Build LangGraph workflows with Python and FastAPI.\n"
                "Required: Python, FastAPI, LangGraph, MCP. 0-1 years experience. B.Tech in CS.\n"
                "Good to have: Docker.\nHow to apply: Include GitHub links."
            ),
            posted_at=now,
            first_seen_at=now,
            last_seen_at=now,
            status="active",
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    return alice_h, bob_h, job_id


def test_job_intelligence_endpoints_history_and_workflow_isolation(intel_env):
    client, SessionLocal = intel_env
    alice_h, bob_h, job_id = _seed_user_and_job(client, SessionLocal)

    # Trigger intelligence analysis via API
    ana = client.post(f"/api/jobs/{job_id}/analyze?use_llm=false", headers=alice_h)
    assert ana.status_code == 200
    body = ana.json()
    assert body["eligible"] is True
    assert body["required_skill_coverage"] == 0.75  # 3/4 (Python, FastAPI, LangGraph; MCP planned_only)
    assert "MCP" in body["missing_required_skills"]
    assert any(m["item"] == "MCP" for m in body["missing_evidence"])

    # Dedicated intelligence endpoint
    intel = client.get(f"/api/jobs/{job_id}/intelligence", headers=alice_h)
    assert intel.status_code == 200
    ij = intel.json()
    assert ij["skill_coverage"]["required_skill_coverage"] == 0.75
    assert "not a hiring probability" in ij["score_disclaimer"]
    assert ij["requirements"]["evidence_items"]["required_skills"]

    # Evidence endpoint
    ev = client.get(f"/api/jobs/{job_id}/evidence", headers=alice_h)
    assert ev.status_code == 200
    assert len(ev.json()["evidence_mapping"]) >= 4

    # Eligibility endpoint
    elig = client.get(f"/api/jobs/{job_id}/eligibility", headers=alice_h)
    assert elig.status_code == 200
    assert elig.json()["eligible"] is True
    assert elig.json()["eligibility_blockers"] == []

    # Trigger second analysis & check history preserves both snapshots
    assert client.post(f"/api/jobs/{job_id}/analyze?use_llm=false", headers=alice_h).status_code == 200
    hist = client.get(f"/api/jobs/{job_id}/history", headers=alice_h)
    assert hist.status_code == 200
    assert len(hist.json()) == 2
    run_id = hist.json()[0]["run_id"]

    # Workflow endpoint accessible to Alice, 404 for Bob
    wf_alice = client.get(f"/api/workflows/{run_id}", headers=alice_h)
    assert wf_alice.status_code == 200
    assert wf_alice.json()["status"] == "completed"
    assert client.get(f"/api/workflows/{run_id}", headers=bob_h).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/history", headers=bob_h).json() == []


@pytest.mark.asyncio
async def test_intelligence_graph_idempotency_and_failure_tracking(intel_env):
    client, SessionLocal = intel_env
    alice_h, _, job_id = _seed_user_and_job(client, SessionLocal)
    me = client.get("/api/auth/me", headers=alice_h).json()

    import uuid

    with SessionLocal() as db:
        r1 = await run_job_intelligence_workflow(
            db,
            user_id=uuid.UUID(me["id"]),
            job_id=uuid.UUID(job_id),
            run_id="intel-fixed-thread",
            use_llm=False,
            allow_reanalysis=False,
        )
        # Repeated invocation on same thread_id with allow_reanalysis=False returns idempotently
        r2 = await run_job_intelligence_workflow(
            db,
            user_id=uuid.UUID(me["id"]),
            job_id=uuid.UUID(job_id),
            run_id="intel-fixed-thread",
            use_llm=False,
            allow_reanalysis=False,
        )
        assert r1["overall_score"] == r2["overall_score"]
        wf = db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == "intel-fixed-thread"))
        assert wf is not None and wf.status == "completed"
